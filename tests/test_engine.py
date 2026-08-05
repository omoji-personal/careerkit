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
    store.reconcile(db, {j.uid}, {j.uid: (0, "EXCLUDED", "comp below floor")}, {"greenhouse"})
    assert db.execute("SELECT gate FROM jobs").fetchone()["gate"] == "EXCLUDED"
    assert not store.query(db)


def test_delisted_posting_stops_surfacing(db):
    from engine import store
    j = J(external_id="7")
    store.upsert(db, [j])
    assert store.query(db)
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    delisted, _ = store.reconcile(db, set(), {}, {"greenhouse"})
    assert delisted == 1
    assert not store.query(db)


def test_broken_board_does_not_delist_its_jobs(db):
    """A source that failed must never be read as 'every job there closed'."""
    from engine import store
    j = J(external_id="5")
    store.upsert(db, [j])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    delisted, _ = store.reconcile(db, set(), {}, set())   # nothing reported OK
    assert delisted == 0
    assert store.query(db)


def test_reseeing_a_delisted_posting_revives_it(db):
    from engine import store
    j = J(external_id="3")
    store.upsert(db, [j])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()
    store.reconcile(db, set(), {}, {"greenhouse"})
    assert not store.query(db)
    store.upsert(db, [j])                                  # board listed it again
    assert store.query(db)


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
               "score, status, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
               "score, status, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
               "score, status, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (j.group_key, j.group_key, j.company, j.title, "u", "remotive",
                "QUALIFIED", 70, "reviewed", "2026-07-01", "2026-07-30"))
    db.commit()
    new, again = store.upsert(db, [j])
    assert len(new) == 0 and len(again) == 1
