"""Regression tests. Every case here is a bug that actually shipped.

Run: ./run-tests.sh      (or: python3 -m pytest tests/ -q)

The rule for this file: a test earns its place by having caught something. Each
one names the defect it locks down, so a future change that reintroduces it
fails loudly instead of silently costing the user a job they never saw.

Nothing here touches the network or the user's real database.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A throwaway database. Never the user's."""
    monkeypatch.setenv("CAREERKIT_HOME", str(tmp_path))
    from engine import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    return store.connect()


def J(company="Acme", title="Product Manager", url="https://b/1", *, gate="QUALIFIED",
      score=70, location="", source="greenhouse", external_id="", description="d" * 400):
    from engine.models import Job
    j = Job(company=company, title=title, url=url, source=source, location=location,
            description=description, external_id=external_id)
    j.gate, j.score, j.reasons = gate, score, ["ok"]
    return j


# --------------------------------------------------------------------------
# score._compile_alt
# Shipped defect: one \b wrapped the WHOLE alternation. A term edged with
# punctuation could never match, and an empty alternative matched everything.
# In an exclusion list the second one silently returned zero jobs forever.
# --------------------------------------------------------------------------

def test_ordinary_terms_still_word_bounded():
    from engine.score import _compile_alt
    p = _compile_alt(["manager"])
    assert p.search("Senior Manager")
    assert not p.search("Management consultant")


def test_term_ending_in_punctuation_is_still_matchable():
    """A metro written ', CO' worked only because the char before it was a word
    char. Keep that working."""
    from engine.score import _compile_alt
    p = _compile_alt(["Denver", ", CO"])
    assert p.search("Denver, CO")
    assert p.search("Boulder, CO")
    assert not p.search("Bogota, COL")


@pytest.mark.parametrize("term,text", [
    (".NET", "5 years of .NET experience"),
    ("C++", "strong C++ background"),
    ("(remote)", "Data Analyst (remote)"),
    ("#1", "we are #1 in the market"),
])
def test_punctuation_edged_terms_match(term, text):
    from engine.score import _compile_alt
    assert _compile_alt([term]).search(text), f"{term!r} silently never fires"


@pytest.mark.parametrize("terms", [
    ["ok", ""],            # a stray empty string in the YAML list
    ["/manager|/"],        # a raw regex ending in a pipe
    ["ok", None],          # a bare "- " list item parses as None
])
def test_empty_alternative_never_matches_everything(terms):
    """The worst possible silent failure: in exclusions this gates out every
    job and the user sees an empty report that looks like a quiet day."""
    from engine.score import _compile_alt
    p = _compile_alt(terms)
    assert not (p and p.search("Chief Financial Officer"))
    assert not (p and p.search("Barista"))


def test_all_terms_unusable_yields_none_not_match_all():
    from engine.score import _compile_alt
    assert _compile_alt(["", None]) is None


def test_malformed_raw_regex_is_dropped_not_raised():
    from engine.score import _compile_alt
    p = _compile_alt(["/valid/", "/unclosed(/"])
    assert p is not None and p.search("valid")


# --------------------------------------------------------------------------
# models.uid
# Shipped defect: uid was company|title only, so two distinct requisitions at
# one employer collapsed into a single row. The second one's URL and location
# were discarded, and marking the first 'applied' hid every sibling forever.
# --------------------------------------------------------------------------

def test_distinct_requisitions_do_not_collapse():
    a = J(url="https://b/111", location="Denver, CO", external_id="111")
    b = J(url="https://b/222", location="Austin, TX", external_id="222")
    assert a.uid != b.uid, "two real openings merged into one"
    assert a.group_key == b.group_key, "report can still show them as one entry"


def test_same_role_from_an_aggregator_still_dedupes():
    """Aggregators mint their own ids, so they must NOT split a role into
    duplicates. This is the property the original key existed to protect."""
    a = J(source="remotive", external_id="agg-1")
    b = J(source="remotive", external_id="agg-2")
    assert a.uid == b.uid


def test_applying_to_one_req_leaves_siblings_visible(db):
    from engine import store
    a = J(url="https://b/111", location="Denver, CO", external_id="111")
    b = J(url="https://b/222", location="Austin, TX", external_id="222")
    store.upsert(db, [a, b])
    store.set_status(db, a.uid, "applied")
    assert any(r["uid"] == b.uid for r in store.query(db))


# --------------------------------------------------------------------------
# store.upsert / reconcile
# Shipped defect: only QUALIFIED/VERIFY were upserted, so a posting that
# stopped qualifying was never written back; and a posting removed from its
# board was never marked closed. Both kept being presented as live. This put a
# dead requisition in front of the user in July 2026.
# --------------------------------------------------------------------------

def test_mutable_fields_refresh_on_resight(db):
    from engine import store
    store.upsert(db, [J(url="https://old/1", location="Remote", external_id="1")])
    store.upsert(db, [J(url="https://new/1", location="Remote, US", external_id="1")])
    row = db.execute("SELECT url, location FROM jobs").fetchone()
    assert row["url"] == "https://new/1", "report would link to a dead URL"
    assert row["location"] == "Remote, US"


def test_role_that_stops_qualifying_is_written_back(db):
    from engine import store
    j = J(external_id="9")
    store.upsert(db, [j])
    store.reconcile(db, {j.uid: (0, "EXCLUDED", "comp below floor")}, {("greenhouse", "Acme")}, set())
    assert db.execute("SELECT gate FROM jobs").fetchone()["gate"] == "EXCLUDED"
    assert not store.query(db)


def test_delisted_posting_stops_surfacing(db):
    from engine import store
    j = J(external_id="7")
    store.upsert(db, [j])
    assert store.query(db)
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    delisted, _ = store.reconcile(db, {}, {("greenhouse", "Acme")}, set())
    assert delisted == 0, "retired on a single miss; a transient would hide live jobs"
    assert store.query(db), "still shown after one miss, correctly"
    db.execute("UPDATE jobs SET miss_on='2000-01-02'")      # next day
    db.commit()
    delisted, _ = store.reconcile(db, {}, {("greenhouse", "Acme")}, set())
    assert delisted == 1, "should retire on the second consecutive miss"
    assert not store.query(db)


def test_broken_board_does_not_delist_its_jobs(db):
    """A source that failed must never be read as 'every job there closed'."""
    from engine import store
    j = J(external_id="5")
    store.upsert(db, [j])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    delisted, _ = store.reconcile(db, {}, set(), set())   # nothing reported OK
    assert delisted == 0
    assert store.query(db)


def test_reseeing_a_delisted_posting_revives_it(db):
    from engine import store
    j = J(external_id="3")
    store.upsert(db, [j])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    store.reconcile(db, {}, {("greenhouse", "Acme")}, set())
    db.execute("UPDATE jobs SET miss_on='2000-01-02'")               # next day
    db.commit()
    store.reconcile(db, {}, {("greenhouse", "Acme")}, set())         # two misses -> retired
    assert not store.query(db)
    store.upsert(db, [j])                                  # board listed it again
    assert store.query(db)
    assert db.execute("SELECT misses FROM jobs").fetchone()["misses"] == 0


# --------------------------------------------------------------------------
# store.set_status
# Shipped defect: any string was accepted, but query() understands five. A
# typo left the job in the active list while the user believed it was filed.
# --------------------------------------------------------------------------

def test_typo_status_is_rejected(db):
    from engine import store
    j = J(external_id="1")
    store.upsert(db, [j])
    with pytest.raises(ValueError):
        store.set_status(db, j.uid, "apllied")


def test_unknown_uid_is_rejected(db):
    from engine import store
    with pytest.raises(KeyError):
        store.set_status(db, "deadbeef", "applied")


# --------------------------------------------------------------------------
# store migration
# Shipped defect risk: CREATE TABLE IF NOT EXISTS is a no-op on an existing
# table, so a column added later never appears for existing users and every
# query naming it fails. An index naming the new column fails even earlier.
# --------------------------------------------------------------------------

def test_v1_database_upgrades_in_place(tmp_path, monkeypatch):
    from engine import store
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE jobs (uid TEXT PRIMARY KEY, company TEXT, title TEXT, "
                "gate TEXT, score INT, status TEXT DEFAULT 'new', first_seen TEXT, "
                "last_seen TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", old)
    up = store.connect()
    cols = {r["name"] for r in up.execute("PRAGMA table_info(jobs)")}
    assert {"group_key", "delisted_on"} <= cols


# --------------------------------------------------------------------------
# strip_html
# Shipped defect (2026-08-03): Greenhouse ships `content` HTML-escaped, so
# stripping before unescaping left tag fragments in the text and scorer
# regexes matched across them.
# --------------------------------------------------------------------------

def test_escaped_html_is_unescaped_before_tags_are_stripped():
    from engine.models import strip_html
    out = strip_html("&lt;p&gt;Own the &lt;strong&gt;roadmap&lt;/strong&gt;&lt;/p&gt;")
    assert "<" not in out and "strong" not in out
    assert "Own the roadmap" in out


def test_list_items_become_separate_lines():
    from engine.models import strip_html
    out = strip_html("<ul><li>First</li><li>Second</li></ul>")
    assert "First" in out and "Second" in out
    assert "FirstSecond" not in out.replace(" ", "").replace("\n", "X")


# --------------------------------------------------------------------------
# score.extract_comp
# --------------------------------------------------------------------------

def test_hourly_board_comp_is_annualised():
    from engine.score import extract_comp
    j = J()
    j.comp_min, j.comp_max = 60, 80
    lo, hi = extract_comp(j)
    assert lo == 60 * 2080 and hi == 80 * 2080


def test_comp_range_is_read_from_the_body():
    from engine.score import extract_comp
    j = J(description="The salary range for this role is $120,000 - $150,000 per year.")
    lo, hi = extract_comp(j)
    assert (lo, hi) == (120_000, 150_000)


def test_a_year_number_is_not_read_as_salary():
    from engine.score import extract_comp
    j = J(description="Founded in 2019. Base salary depends on experience.")
    assert extract_comp(j) == (None, None)


# --------------------------------------------------------------------------
# score.location_verdict
# --------------------------------------------------------------------------

@pytest.mark.parametrize("loc,expected", [
    ("Remote (US)", "pass"),
    ("Bangalore, India", "fail"),
    ("London, United Kingdom", "fail"),
])
def test_location_verdicts(loc, expected):
    from engine.score import Profile, location_verdict
    v, _ = location_verdict(J(location=loc), Profile())
    assert v == expected


def test_a_us_state_is_not_read_as_a_foreign_country():
    """', ca' is California as often as Canada. US evidence in the same string
    must win over the two-letter country-code heuristic."""
    from engine.score import Profile, location_verdict
    v, why = location_verdict(J(location="Los Angeles, ca"), Profile())
    assert "non-US" not in why, why


def test_profile_with_no_usable_lanes_does_not_match_everything():
    from engine.score import Profile, score
    p = Profile()
    j = score(J(title="Underwater Basket Weaver"), p)
    assert j.gate == "EXCLUDED"


# --------------------------------------------------------------------------
# Legacy adoption
# The uid formula changed on 2026-08-05. A pre-change row's uid is exactly the
# new group_key, so an existing posting must be recognised rather than
# re-inserted, or every first_seen date resets and applied/rejected status
# detaches from the job it belongs to.
# --------------------------------------------------------------------------

def test_legacy_row_is_adopted_not_duplicated(db):
    from engine import store
    j = J(external_id="111", location="Denver, CO")
    # simulate a v1 row: keyed by group_key, with history
    db.execute("INSERT INTO jobs (uid, group_key, company, title, url, source, gate, "
               "score, status, first_seen, last_seen, schema_v) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
               (j.group_key, j.group_key, j.company, j.title, "https://old/1",
                "greenhouse", "QUALIFIED", 70, "applied", "2026-07-01", "2026-07-30"))
    db.commit()
    new, again = store.upsert(db, [j])
    assert len(new) == 0 and len(again) == 1, "the legacy row was duplicated"
    rows = list(db.execute("SELECT uid, status, first_seen FROM jobs"))
    assert len(rows) == 1
    assert rows[0]["uid"] == j.uid, "row was not re-keyed to the new uid"
    assert rows[0]["status"] == "applied", "applied status was lost"
    assert rows[0]["first_seen"] == "2026-07-01", "first_seen was reset"


def test_only_one_sibling_adopts_the_legacy_row(db):
    """The second requisition in a group must insert fresh, not steal history."""
    from engine import store
    a = J(external_id="111", location="Denver, CO")
    b = J(external_id="222", location="Austin, TX")
    db.execute("INSERT INTO jobs (uid, group_key, company, title, url, source, gate, "
               "score, status, first_seen, last_seen, schema_v) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
               (a.group_key, a.group_key, a.company, a.title, "https://old/1",
                "greenhouse", "QUALIFIED", 70, "applied", "2026-07-01", "2026-07-30"))
    db.commit()
    store.upsert(db, [a, b])
    rows = {r["uid"]: r for r in db.execute("SELECT uid, status FROM jobs")}
    assert len(rows) == 2
    assert rows[a.uid]["status"] == "applied"
    assert rows[b.uid]["status"] == "new", "sibling wrongly inherited applied"


def test_aggregator_rows_need_no_adoption(db):
    from engine import store
    j = J(source="remotive", external_id="agg-1")
    assert j.uid == j.group_key
    db.execute("INSERT INTO jobs (uid, group_key, company, title, url, source, gate, "
               "score, status, first_seen, last_seen, schema_v) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
               (j.group_key, j.group_key, j.company, j.title, "u", "remotive",
                "QUALIFIED", 70, "reviewed", "2026-07-01", "2026-07-30"))
    db.commit()
    new, again = store.upsert(db, [j])
    assert len(new) == 0 and len(again) == 1


def test_titleless_posting_is_rejected():
    """Seen live 2026-08-05: a Salesforce Workday row arrived with no title, no
    body, and the board root as its URL. lane_title_context prefixed the lane
    word onto the empty title, the lane matched on that word alone, and the
    junk row surfaced as VERIFY."""
    from engine.score import Profile, score
    p = Profile()
    p.lanes = [(50, __import__('re').compile(r"(salesforce)", 2), "sf")]
    p.lane_title_context = {"sf-direct": "Salesforce"}
    j = J(company="Salesforce", title="", description="")
    j.lane = "sf-direct"
    out = score(j, p)
    assert out.gate == "EXCLUDED", f"junk row scored {out.gate}"


def test_context_prefix_still_enriches_a_real_title():
    from engine.score import Profile, score
    import re
    p = Profile()
    p.lanes = [(50, re.compile(r"(salesforce solution architect)", re.I), "sf")]
    p.lane_title_context = {"sf-direct": "Salesforce"}
    p.domain_terms = None
    j = J(company="Salesforce", title="Solution Architect", description="d" * 400,
          location="Remote, US")
    j.lane = "sf-direct"
    out = score(j, p)
    assert out.gate in ("QUALIFIED", "VERIFY"), out.reasons


# --------------------------------------------------------------------------
# HTTP cache eviction
# Nothing ever removed expired entries. One real run left 734 MB / 4,913 files.
# --------------------------------------------------------------------------

def test_expired_cache_entries_are_pruned(tmp_path, monkeypatch):
    import time
    from engine import http
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    fresh, stale = tmp_path / "fresh.json", tmp_path / "stale.json"
    fresh.write_text("{}"); stale.write_text("{}")
    old = time.time() - (http.CACHE_TTL + 60)
    os.utime(stale, (old, old))
    removed = http.prune_cache()
    assert removed == 1
    assert fresh.exists() and not stale.exists()


# --------------------------------------------------------------------------
# Red-team findings, 2026-08-05 round 2. Both are regressions introduced BY
# the fixes: reporting an empty result as healthy removed the false alarms and
# in doing so made two genuine failures silent.
# --------------------------------------------------------------------------

def test_source_that_never_fetched_does_not_inherit_the_previous_status():
    """run_adapter reads last_status() after the adapter returns. A board that
    early-returns without any HTTP call used to inherit the previous board's
    200 and be reported healthy when it never ran."""
    from engine import http, adapters
    http._local.last_status = 200                    # previous board succeeded

    @adapters.adapter("_t_nofetch")
    def _nofetch(cfg):
        return []                                    # config guard, no request

    jobs, err = adapters.run_adapter({"ats": "_t_nofetch", "name": "X"})
    assert err is not None, "silently reported healthy after making no request"


def test_drop_to_zero_is_flagged(db):
    """A board changing its JSON shape returns 200, maps nothing, and looks
    exactly like 'no openings today'. Comparing to the previous count is what
    tells them apart."""
    from engine import store
    store.record_health(db, "greenhouse:Big", 470, None)
    assert not store.dropped_to_zero(db)
    store.record_health(db, "greenhouse:Big", 0, None)      # schema changed
    flagged = store.dropped_to_zero(db)
    assert len(flagged) == 1 and flagged[0]["prev_count"] == 470


def test_always_empty_board_is_not_flagged(db):
    """Must not re-introduce the false alarms the empty-is-healthy fix removed."""
    from engine import store
    store.record_health(db, "greenhouse:Quiet", 0, None)
    store.record_health(db, "greenhouse:Quiet", 0, None)
    assert not store.dropped_to_zero(db)


# --------------------------------------------------------------------------
# TAA round 2, all three lenses converged on this one: health was tracked per
# ATS PLATFORM while jobs belong to individual BOARDS.
# --------------------------------------------------------------------------

def test_one_board_failing_does_not_retire_a_sibling_boards_jobs(db):
    """greenhouse:CompanyA succeeds, greenhouse:CompanyB 404s. CompanyB's jobs
    must survive: its board never reported successfully."""
    from engine import store
    a = J(company="CompanyA", external_id="a1")
    b = J(company="CompanyB", external_id="b1")
    store.upsert(db, [a, b])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    store.upsert(db, [a])                                   # only A re-sighted
    healthy = {("greenhouse", "CompanyA")}                  # B errored
    for d in range(3):
        db.execute("UPDATE jobs SET miss_on=NULL")
        db.commit()
        store.reconcile(db, {}, healthy, set())
    rows = {r["company"]: r for r in db.execute("SELECT company, delisted_on FROM jobs")}
    assert rows["CompanyB"]["delisted_on"] is None, "retired a job from a board that never answered"
    assert rows["CompanyA"]["delisted_on"] is None


def test_a_tier_filtered_run_does_not_retire_unpolled_employers(db):
    from engine import store
    polled = J(company="TierA", external_id="1")
    skipped = J(company="TierC", external_id="2")
    store.upsert(db, [polled, skipped])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    store.upsert(db, [polled])
    for d in range(3):
        db.execute("UPDATE jobs SET miss_on=NULL")
        db.commit()
        store.reconcile(db, {}, {("greenhouse", "TierA")}, set())
    got = {r["company"]: r["delisted_on"] for r in db.execute("SELECT company, delisted_on FROM jobs")}
    assert got["TierC"] is None, "--tier run retired employers it never polled"


def test_a_modern_aggregator_row_cannot_be_hijacked(db):
    """An aggregator uid IS the bare group_key, which the old adoption probe
    also matched. An ATS req could steal that live row and leave a permanent
    duplicate behind."""
    from engine import store
    agg = J(source="remotive", external_id="agg1")
    store.upsert(db, [agg])
    ats = J(source="greenhouse", external_id="777")
    assert ats.group_key == agg.group_key and ats.uid != agg.uid
    store.upsert(db, [ats])
    rows = list(db.execute("SELECT uid, source FROM jobs ORDER BY source"))
    assert len(rows) == 2, "the ATS req hijacked the aggregator row"
    assert {r["source"] for r in rows} == {"remotive", "greenhouse"}


def test_adoption_prefers_the_requisition_the_user_applied_to(db):
    """Two reqs share a group and a legacy row is marked applied. The URL is
    the only evidence of WHICH one the user applied to; without it whichever
    the board listed first inherits the status."""
    from engine import store
    applied_to = J(url="https://b/sf", external_id="sf", location="San Francisco")
    other = J(url="https://b/nyc", external_id="nyc", location="New York")
    db.execute("INSERT INTO jobs (uid, group_key, company, title, url, source, gate, "
               "score, status, first_seen, last_seen, schema_v) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
               (applied_to.group_key, applied_to.group_key, applied_to.company,
                applied_to.title, "https://b/sf", "greenhouse", "QUALIFIED", 70,
                "applied", "2026-07-01", "2026-07-30"))
    db.commit()
    store.upsert(db, [other, applied_to])          # NYC listed first on purpose
    got = {r["uid"]: r["status"] for r in db.execute("SELECT uid, status FROM jobs")}
    assert got[applied_to.uid] == "applied", "applied landed on the wrong requisition"
    assert got[other.uid] == "new"


def test_cache_hit_records_its_status(tmp_path, monkeypatch):
    """Only 200s are cached, so a cache hit IS a success. Returning without
    recording it left run_adapter blaming a healthy board for another's error."""
    import json as _json
    from engine import http
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    key = "GET|https://example.test/x|"
    http._cache_path(key).write_text(_json.dumps({"status": 200, "text": "{}"}))
    http._local.last_status = 403                              # previous failure
    http._pruned = True
    st, _ = http.fetch("https://example.test/x")
    assert st == 200 and http.last_status() == 200


@pytest.mark.parametrize("loc", ["Munich, DE", "Bangalore, IN", "Tel Aviv, IL"])
def test_foreign_iso_codes_are_not_read_as_us_states(loc):
    """AR/CO/DE/ID/IL/IN/MA are US state codes AND ISO country codes, and real
    boards write both in caps. Treating them as US evidence suppressed the
    non-US rail and presented foreign postings as US offices."""
    from engine.score import Profile, location_verdict
    p = Profile()
    p.relocation = True
    v, why = location_verdict(J(location=loc), p)
    assert v == "fail", f"{loc} surfaced as {v}: {why}"


def test_us_state_codes_still_work():
    from engine.score import Profile, location_verdict
    p = Profile()
    for loc in ("Denver, CO", "Chicago, IL", "Boston, MA", "Indianapolis, IN"):
        v, why = location_verdict(J(location=loc), p)
        assert "non-US" not in why, f"{loc}: {why}"


def test_a_broken_exclusion_regex_fails_loudly(tmp_path):
    """Dropping an unusable exclusion term fails OPEN: the rail vanishes and
    everything the user banned starts surfacing."""
    from engine.score import Profile, ProfileError
    bad = tmp_path / "p.yaml"
    bad.write_text('lanes:\n  - {key: a, weight: 40, titles: ["manager"]}\n'
                   'exclusions:\n  titles: ["/(contract|interim/"]\n')
    with pytest.raises(ProfileError):
        Profile.load(bad)


def test_a_regex_matching_everything_is_rejected():
    from engine.score import _compile_alt
    for pattern in ("/.+/", "/ /", "/\\\\b/"):
        p = _compile_alt([pattern], where="t")
        assert not (p and p.search("Chief Financial Officer")), pattern


def test_a_foreign_copy_of_a_role_does_not_delete_the_qualified_one(db):
    """Aggregator sightings of one role share a uid. A copy listed under a
    foreign location scores EXCLUDED while the clean copy scores QUALIFIED;
    writing the demotion back blindly deleted the role on the run it was found.
    Best gate must win."""
    from engine import store
    good = J(source="remotive", location="Remote, US")
    bad = J(source="remoteok", location="Toronto, Canada")
    assert good.uid == bad.uid, "precondition: aggregator copies share a uid"
    bad.gate, bad.score, bad.reasons = "EXCLUDED", 52, ["non-US: Toronto, Canada"]

    keep = [good]
    store.upsert(db, keep)
    kept = {j.uid for j in keep}
    demoted = {j.uid: (j.score, j.gate, " | ".join(j.reasons))
               for j in [good, bad]
               if j.gate in ("EXCLUDED", "SLOT-BLOCKED") and j.uid not in kept}
    store.reconcile(db, demoted, {("greenhouse", "Acme")}, {"remotive", "remoteok"})
    assert store.query(db), "a live US-remote role was deleted by its foreign twin"


def test_two_runs_in_one_day_count_as_one_miss(db):
    """'Two consecutive misses' means two DAYS of absence. Running pull twice
    in an afternoon must not retire everything absent from both."""
    from engine import store
    j = J(external_id="1")
    store.upsert(db, [j])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    healthy = {("greenhouse", "Acme")}
    for _ in range(4):                                   # four runs, same day
        store.reconcile(db, {}, healthy, set())
    assert db.execute("SELECT misses FROM jobs").fetchone()["misses"] == 1
    assert store.query(db), "same-day re-runs retired a live posting"


def test_renamed_employer_rows_are_not_stranded(db):
    """Renaming a company in the registry changes the board identity, so rows
    written under the old name match no healthy board. Without an orphan rule
    they accumulate as permanently live jobs - the inverse of the bug the
    per-board key fixed."""
    from engine import store
    old = J(company="Acme Corp", external_id="1")
    store.upsert(db, [old])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    healthy = {("greenhouse", "Acme Inc")}          # registry renamed
    known = {("greenhouse", "Acme Inc")}
    for day in ("2000-01-02", "2000-01-03"):
        db.execute("UPDATE jobs SET miss_on=?", (day,))
        db.commit()
        store.reconcile(db, {}, healthy, set(), known_boards=known)
    assert not store.query(db), "rows under the old company name never retired"


def test_aggregator_rows_are_not_treated_as_orphans(db):
    """A feed's company is the employer it names, which is not expected in the
    registry. The orphan rule must not sweep those up."""
    from engine import store
    j = J(company="SomeStartup", source="remotive")
    store.upsert(db, [j])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    for day in ("2000-01-02", "2000-01-03"):
        db.execute("UPDATE jobs SET miss_on=?", (day,))
        db.commit()
        store.reconcile(db, {}, set(), set(), known_boards={("greenhouse", "Acme")})
    assert store.query(db), "an aggregator row was retired as an orphan"


def test_repair_does_not_stamp_a_fresh_install_row(db):
    """The one-time legacy repair ran on every connect. On a fresh install an
    ATS row with an empty external_id has uid == group_key, and stamping it
    legacy let a later distinct requisition adopt and hijack its history."""
    from engine import store
    fresh = J(external_id="")                     # no req id -> uid == group_key
    assert fresh.uid == fresh.group_key
    store.upsert(db, [fresh])
    store._migrate(db)                            # repair runs again
    v = db.execute("SELECT schema_v FROM jobs").fetchone()["schema_v"]
    assert v == 2, "a fresh-install row was marked adoption-eligible"


def test_a_negative_lookahead_allow_list_is_accepted():
    """An allow-list ('exclude everything that is NOT X') legitimately matches
    the sentinels; only an accidental match-everything should be rejected."""
    from engine.score import _compile_alt
    p = _compile_alt(["/^(?!.*Salesforce).*$/"], where="t")
    assert p is not None
    assert p.search("Barista")
    assert not p.search("Salesforce Administrator")


def test_namespaced_feed_rows_can_retire(db):
    """jobspy writes source='jobspy:indeed' while the registry knows the feed
    as 'jobspy'. Matching on the exact string meant those rows never
    accumulated a miss and dead postings piled up silently."""
    from engine import store
    j = J(source="jobspy:indeed", company="SomeCo")
    store.upsert(db, [j])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    for day in ("2000-01-02", "2000-01-03"):
        db.execute("UPDATE jobs SET miss_on=?", (day,))
        db.commit()
        store.reconcile(db, {}, set(), {"jobspy"})
    assert not store.query(db), "a namespaced feed row could never be retired"
