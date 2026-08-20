"""Focused regressions for core state, scoring, and report correctness."""
from __future__ import annotations

import os
import builtins
import errno
from datetime import date
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERKIT_HOME", str(tmp_path))
    from engine import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    con = store.connect()
    yield con
    con.close()


def _job(*, company="Acme", title="Product Manager", url="https://jobs.test/1",
         source="greenhouse", external_id="1", location="Remote, US",
         gate="QUALIFIED", score=73, description="d" * 400):
    from engine.models import Job

    job = Job(company=company, title=title, url=url, source=source,
              external_id=external_id, location=location, description=description)
    job.gate, job.score, job.reasons = gate, score, ["original verdict"]
    return job


def test_duplicate_surfaced_uids_collapse_to_strongest_richest_order_invariant(db):
    from engine import store
    from engine.pull import pick_surfaced, record_surfaced_sightings

    # These are three copies of the same source-local opening, not three
    # requisitions. Different feeds/opening IDs now remain distinct so a real
    # sibling cannot disappear merely because its title matches.
    verify = _job(source="remotive", external_id="same-opening", gate="VERIFY",
                  score=99, url="https://remotive.test/acme-pm?card=verify",
                  location="", description="thin")
    qualified_bare = _job(source="remotive", external_id="same-opening",
                          gate="QUALIFIED", score=82,
                          url="https://remotive.test/acme-pm?card=bare",
                          location="Remote, US", description="short")
    qualified_rich = _job(source="remotive", external_id="same-opening",
                          gate="QUALIFIED", score=82,
                          url="https://remotive.test/acme-pm?card=rich",
                          location="Remote, US", description="rich body " * 100)
    qualified_rich.comp_min = 150_000
    qualified_rich.comp_max = 190_000
    qualified_rich.comp_text = "$150,000-$190,000"
    qualified_rich.comp_source = "board"
    verify.url_direct = "https://acme.test/jobs/pm/apply"
    qualified_bare.company_site = "https://acme.test"
    assert len({j.uid for j in (verify, qualified_bare, qualified_rich)}) == 1

    snapshots = []
    for order in ((verify, qualified_bare, qualified_rich),
                  (qualified_rich, qualified_bare, verify)):
        chosen = pick_surfaced(list(order))
        assert len(chosen) == 1
        job = chosen[0]
        snapshots.append(job.to_row())
        assert (job.gate, job.score, job.source) == (
            "QUALIFIED", 82, "remotive")
        assert job.comp_min == 150_000 and "rich body" in job.description
        assert job.url_direct == "https://acme.test/jobs/pm/apply"
        assert job.company_site == "https://acme.test"

        store.upsert(db, chosen)
        record_surfaced_sightings(db, list(order), {job.uid})
        sources = {
            row["source"]: row["url"] for row in db.execute(
                "SELECT source,url FROM sightings WHERE uid=?", (job.uid,))
        }
        assert sources == {
            "remotive": "https://remotive.test/acme-pm?card=bare",
        }

    assert snapshots[0] == snapshots[1]


def test_non_object_jsonl_records_are_reported_and_loader_continues(db, tmp_path):
    from engine import applied

    evidence = tmp_path / "applications.jsonl"
    evidence.write_text(
        '[]\nnull\n"string"\n42\n'
        '{"company":"Unseen Co","status":"applied"}\n')

    records = applied.load_evidence(evidence)
    assert [r["_line"] for r in records if r.get("_bad")] == [1, 2, 3, 4]
    result = applied.reconcile(db, records)
    assert len(result["problems"]) == 4
    assert all("not valid JSON" in problem for problem in result["problems"])
    assert len(result["unmatched"]) == 1
    assert result["unmatched"][0]["company"] == "Unseen Co"


def test_applied_cli_reports_non_object_jsonl_without_crashing(
        db, tmp_path, monkeypatch, capsys):
    import argparse
    import careerkit

    evidence = tmp_path / "applications.jsonl"
    evidence.write_text('[]\nnull\n"string"\n42\n')
    monkeypatch.setattr(careerkit.store, "connect", lambda: db)

    careerkit.cmd_applied(argparse.Namespace(file=str(evidence), apply=False))
    output = capsys.readouterr().out
    for raw in ("[]", "null", '"string"', "42"):
        assert f"not valid JSON: {raw}" in output
    assert "0 matched, 0 ambiguous, 0 unmatched, 0 pre-submission" in output


def test_blank_company_uid_migration_adopts_exact_source_url_and_history(db):
    from engine import store

    fresh = _job(company="", title="Product Manager", source="remotive",
                 external_id="aggregator-id", url="https://jobs.test/anonymous")
    assert fresh.legacy_blank_company_uid
    assert fresh.uid != fresh.legacy_blank_company_uid
    db.execute(
        "INSERT INTO jobs (uid,group_key,company,title,url,source,first_seen,last_seen,"
        "seen_count,status,notes,schema_v) VALUES (?,?,?,?,?,?,?, ?,?,?,?,1)",
        (fresh.legacy_blank_company_uid, fresh.legacy_blank_company_group_key,
         "", fresh.title, fresh.url, fresh.source, "2026-01-01", "2026-01-02",
         4, "applied", "preserve legacy application"),
    )
    db.execute(
        "INSERT INTO sightings(uid,source,url,seen_on) VALUES (?,?,?,?)",
        (fresh.legacy_blank_company_uid, fresh.source, fresh.url, "2026-01-02"),
    )
    db.execute(
        "INSERT INTO events(uid,at,kind,detail) VALUES (?,?,?,?)",
        (fresh.legacy_blank_company_uid, "2026-01-02T00:00:00",
         "application:applied", "legacy event"),
    )
    db.commit()

    store.upsert(db, [fresh])
    row = db.execute("SELECT * FROM jobs").fetchone()
    assert (row["uid"], row["group_key"]) == (fresh.uid, fresh.group_key)
    assert (row["status"], row["notes"]) == (
        "applied", "preserve legacy application")
    assert row["seen_count"] == 5
    assert not db.execute("SELECT 1 FROM jobs WHERE uid=?",
                          (fresh.legacy_blank_company_uid,)).fetchone()
    assert db.execute("SELECT 1 FROM sightings WHERE uid=?", (fresh.uid,)).fetchone()
    assert db.execute("SELECT 1 FROM events WHERE uid=?", (fresh.uid,)).fetchone()


def test_blank_company_uid_migration_refuses_title_only_adoption(db):
    from engine import store

    fresh = _job(company="", title="Product Manager", source="remoteok",
                 url="https://jobs.test/new-anonymous")
    db.execute(
        "INSERT INTO jobs (uid,group_key,company,title,url,source,first_seen,last_seen,"
        "seen_count,status,schema_v) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
        (fresh.legacy_blank_company_uid, fresh.legacy_blank_company_group_key,
         "", fresh.title, "https://jobs.test/different-anonymous", fresh.source,
         "2026-01-01", "2026-01-02", 1, "rejected"),
    )
    db.commit()

    store.upsert(db, [fresh])
    rows = list(db.execute("SELECT uid,status,url FROM jobs ORDER BY uid"))
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"new", "rejected"}
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (fresh.uid,)).fetchone()[0] == "new"


def test_blank_company_uid_migration_merges_when_both_rows_already_exist(db):
    from engine import store

    fresh = _job(company="", title="Product Manager", source="remoteok",
                 url="https://jobs.test/same-anonymous")
    store.upsert(db, [fresh])
    db.execute(
        "INSERT INTO jobs (uid,group_key,company,title,url,source,first_seen,last_seen,"
        "seen_count,status,notes,schema_v) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
        (fresh.legacy_blank_company_uid, fresh.legacy_blank_company_group_key,
         "", fresh.title, fresh.url, fresh.source, "2025-12-01", "2026-01-02",
         3, "rejected", "older human decision",),
    )
    db.execute(
        "INSERT INTO events(uid,at,kind,detail) VALUES (?,?,?,?)",
        (fresh.legacy_blank_company_uid, "2026-01-02T00:00:00",
         "application:rejected", "preserve me"),
    )
    db.commit()

    store.upsert(db, [fresh])
    rows = list(db.execute("SELECT * FROM jobs"))
    assert len(rows) == 1
    assert (rows[0]["uid"], rows[0]["group_key"], rows[0]["status"]) == (
        fresh.uid, fresh.group_key, "rejected")
    assert rows[0]["notes"] == "older human decision"
    assert rows[0]["first_seen"] == "2025-12-01"
    assert db.execute(
        "SELECT 1 FROM events WHERE uid=? AND kind='application:rejected'",
        (fresh.uid,),
    ).fetchone()


def _force_windows_lock_backend(monkeypatch, locking):
    """Simulate native Windows on POSIX without weakening the real POSIX path."""
    original_import = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("simulated native Windows")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        LK_NBLCK=1, LK_UNLCK=2, locking=locking))


def test_run_lock_uses_nonblocking_windows_fallback(tmp_path, monkeypatch):
    from engine import store

    calls = []

    def locking(fd, mode, length):
        calls.append((mode, length, os.lseek(fd, 0, os.SEEK_CUR)))

    _force_windows_lock_backend(monkeypatch, locking)
    path = tmp_path / "jobs.lock"
    with store.RunLock(path):
        assert path.read_text() == str(os.getpid())
        assert calls == [(1, 1, 0)]
    assert calls[-1] == (2, 1, 0)


def test_windows_lock_contender_preserves_active_pid(tmp_path, monkeypatch):
    from engine import store

    path = tmp_path / "jobs.lock"
    path.write_text("4242")

    def busy(_fd, mode, _length):
        if mode == 1:
            raise OSError(errno.EACCES, "simulated lock contention")

    _force_windows_lock_backend(monkeypatch, busy)
    with pytest.raises(RuntimeError, match="already writing"):
        with store.RunLock(path):
            pass
    assert path.read_text() == "4242"


@pytest.mark.parametrize("status", ["applied", "rejected", "ignored"])
def test_upsert_preserves_protected_verdict_but_refreshes_liveness(db, status):
    from engine import store

    original = _job()
    store.upsert(db, [original])
    store.set_status(db, original.uid, status)
    db.execute("UPDATE jobs SET last_seen='2020-01-01', misses=1, "
               "miss_on='2020-01-02', delisted_on='2020-01-03' WHERE uid=?",
               (original.uid,))
    db.commit()
    before = db.execute("SELECT seen_count FROM jobs WHERE uid=?",
                        (original.uid,)).fetchone()[0]

    fresh = _job(url="https://jobs.test/1-current", location="New York, NY",
                 gate="VERIFY", score=41, description="fresh body " + "x" * 400)
    fresh.reasons = ["new verdict must not replace history"]
    store.upsert(db, [fresh])

    row = db.execute("SELECT * FROM jobs WHERE uid=?", (original.uid,)).fetchone()
    assert (row["status"], row["gate"], row["score"], row["reasons"]) == (
        status, "QUALIFIED", 73, "original verdict")
    assert row["last_seen"] == date.today().isoformat()
    assert row["seen_count"] == before + 1
    assert (row["misses"], row["miss_on"], row["delisted_on"]) == (0, None, None)
    assert (row["url"], row["location"]) == (
        "https://jobs.test/1-current", "New York, NY")


def test_ignored_demoted_sighting_refreshes_liveness_without_rewriting_verdict(db):
    from engine import store

    original = _job(external_id="ignored")
    store.upsert(db, [original])
    store.set_status(db, original.uid, "ignored")
    db.execute("UPDATE jobs SET last_seen='2020-01-01', misses=1, "
               "miss_on='2020-01-02', delisted_on='2020-01-03' WHERE uid=?",
               (original.uid,))
    db.commit()
    before = db.execute("SELECT seen_count FROM jobs WHERE uid=?",
                        (original.uid,)).fetchone()[0]

    fresh = _job(external_id="ignored", url="https://jobs.test/ignored-current",
                 location="London, UK", gate="EXCLUDED", score=0,
                 description="fresh demoted body " + "x" * 400)
    fresh.reasons = ["non-US"]
    store.reconcile(db, {fresh.uid: fresh}, {("greenhouse", "Acme")}, set())

    row = db.execute("SELECT * FROM jobs WHERE uid=?", (original.uid,)).fetchone()
    assert (row["status"], row["gate"], row["score"], row["reasons"]) == (
        "ignored", "QUALIFIED", 73, "original verdict")
    assert row["last_seen"] == date.today().isoformat()
    assert row["seen_count"] == before + 1
    assert (row["misses"], row["miss_on"], row["delisted_on"]) == (0, None, None)
    assert "fresh demoted body" in row["description"]
    assert db.execute("SELECT 1 FROM sightings WHERE uid=? AND source='greenhouse'",
                      (original.uid,)).fetchone()


def test_replayed_or_backfilled_application_events_do_not_regress_current_status(db):
    from engine import store

    job = _job(external_id="application-history")
    store.upsert(db, [job])
    assert store.record_application_stage(db, job.uid, "applied", on="2026-08-01")
    assert store.record_application_stage(db, job.uid, "rejected", on="2026-08-10")
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (job.uid,)).fetchone()[0] == "rejected"

    # Exact replay is idempotent and must not run set_status before discovering it.
    assert not store.record_application_stage(db, job.uid, "applied", on="2026-08-01")
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (job.uid,)).fetchone()[0] == "rejected"

    # A newly discovered older event belongs in history but is not the current stage.
    assert store.record_application_stage(
        db, job.uid, "interviewing", on="2026-08-05", notes="backfilled")
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (job.uid,)).fetchone()[0] == "rejected"


def test_retry_after_interrupted_run_still_reports_first_sighting_as_new(db):
    from engine import store

    abandoned = store.start_run(db)
    job = _job(external_id="abandoned-run")
    store.upsert(db, [job], run_id=abandoned)
    # No finish_run: the process died after persisting the posting.
    db.execute("UPDATE runs SET state='running:999999' WHERE run_id=?", (abandoned,))
    db.commit()

    retry = store.start_run(db)
    new, again = store.upsert(db, [_job(external_id="abandoned-run")], run_id=retry)
    assert [j.uid for j in new] == [job.uid]
    assert again == []
    row = db.execute("SELECT * FROM jobs WHERE uid=?", (job.uid,)).fetchone()
    assert row["first_seen_run"] == retry
    assert store.is_new_this_run(row, retry)


def test_remote_false_is_authoritative_for_ats_but_not_aggregators(db):
    from engine import store

    ats = _job(external_id="ats-remote")
    ats.board = "greenhouse:acme"
    ats.remote_flag = True
    store.upsert(db, [ats])
    ats_now_onsite = _job(external_id="ats-remote", location="New York, NY")
    ats_now_onsite.board = "greenhouse:acme"
    ats_now_onsite.remote_flag = False
    store.upsert(db, [ats_now_onsite])
    assert db.execute("SELECT remote_flag FROM jobs WHERE uid=?",
                      (ats.uid,)).fetchone()[0] == 0

    agg = _job(company="Aggregator Co", title="Program Manager",
               url="https://aggregator.test/a", source="jobspy:indeed",
               external_id="ignored")
    agg.remote_flag = True
    store.upsert(db, [agg])
    agg_missing_flag = _job(company="Aggregator Co", title="Program Manager",
                            url="https://aggregator.test/a", source="jobspy:indeed",
                            external_id="also-ignored")
    agg_missing_flag.remote_flag = False
    store.upsert(db, [agg_missing_flag])
    assert db.execute("SELECT remote_flag FROM jobs WHERE uid=?",
                      (agg.uid,)).fetchone()[0] == 1

    ats_demoted = _job(company="ATS Demoted", external_id="ats-demoted")
    ats_demoted.board = "greenhouse:ats-demoted"
    ats_demoted.remote_flag = True
    store.upsert(db, [ats_demoted])
    fresh = _job(company="ATS Demoted", external_id="ats-demoted",
                 location="Boston, MA", gate="EXCLUDED", score=0)
    fresh.board = "greenhouse:ats-demoted"
    fresh.remote_flag = False
    store.reconcile(db, {fresh.uid: fresh}, {("greenhouse", "ATS Demoted")}, set())
    assert db.execute("SELECT remote_flag FROM jobs WHERE uid=?",
                      (fresh.uid,)).fetchone()[0] == 0


def test_ghost_evidence_merges_across_upsert_reconcile_and_identity_repair(db):
    from engine import store

    surfaced = _job(external_id="evidence")
    surfaced.url_direct = surfaced.company_site = None
    store.upsert(db, [surfaced])
    looked = _job(external_id="evidence")
    looked.url_direct = looked.company_site = ""
    store.upsert(db, [looked])
    assert tuple(db.execute("SELECT url_direct, company_site FROM jobs WHERE uid=?",
                            (surfaced.uid,)).fetchone()) == ("", "")

    found = _job(external_id="evidence")
    found.url_direct = "https://acme.test/jobs/evidence"
    found.company_site = "https://acme.test"
    store.upsert(db, [found])
    blank_again = _job(external_id="evidence")
    store.upsert(db, [blank_again])
    assert tuple(db.execute("SELECT url_direct, company_site FROM jobs WHERE uid=?",
                            (surfaced.uid,)).fetchone()) == (
        "https://acme.test/jobs/evidence", "https://acme.test")

    demoted = _job(company="Demoted Evidence", external_id="demoted-evidence",
                   gate="VERIFY", score=40)
    demoted.url_direct = demoted.company_site = None
    store.upsert(db, [demoted])
    demoted_fresh = _job(company="Demoted Evidence", external_id="demoted-evidence",
                         gate="EXCLUDED", score=0)
    demoted_fresh.url_direct = "https://demoted.test/apply"
    demoted_fresh.company_site = "https://demoted.test"
    store.reconcile(db, {demoted.uid: demoted_fresh},
                    {("greenhouse", "Demoted Evidence")}, set())
    assert tuple(db.execute("SELECT url_direct, company_site FROM jobs WHERE uid=?",
                            (demoted.uid,)).fetchone()) == (
        "https://demoted.test/apply", "https://demoted.test")

    canonical = _job(company="Merged Evidence", external_id="canonical")
    canonical.url_direct = canonical.company_site = ""
    store.upsert(db, [canonical])
    db.execute(
        "INSERT INTO jobs (uid, company, title, url, source, first_seen, last_seen, "
        "seen_count, status, url_direct, company_site) VALUES "
        "('old-evidence','Merged Evidence','Product Manager','https://old.test',"
        "'greenhouse','2026-01-01','2026-01-02',1,'new',?,?)",
        ("https://merged.test/apply", "https://merged.test"),
    )
    old = db.execute("SELECT * FROM jobs WHERE uid='old-evidence'").fetchone()
    store._merge_duplicate_job_row(db.cursor(), canonical.uid, old)
    assert tuple(db.execute("SELECT url_direct, company_site FROM jobs WHERE uid=?",
                            (canonical.uid,)).fetchone()) == (
        "https://merged.test/apply", "https://merged.test")


def _profile(*, floor=0):
    from engine.score import Profile

    profile = Profile(screen_floor=floor, accept_floor=floor)
    profile.lanes = [(50, re.compile(r"product manager", re.I), "pm")]
    profile.domain_terms = None
    return profile


def test_dream_company_exemption_is_not_persisted_on_the_posting(db):
    from engine import pull, store
    from engine.score import score

    profile = _profile()
    profile.dream_companies = {"dream co"}
    job = _job(company="Dream Co", location="London, UK", external_id="dream")
    score(job, profile)
    assert job.gate in ("QUALIFIED", "VERIFY")
    assert job.rails_exempt is False
    store.upsert(db, [job])
    row = db.execute("SELECT * FROM jobs WHERE uid=?", (job.uid,)).fetchone()
    assert row["rails_exempt"] == 0

    rebuilt = pull.job_from_row(row)
    score(rebuilt, _profile())
    assert rebuilt.gate == "EXCLUDED"
    assert any("non-US" in reason for reason in rebuilt.reasons)


def test_max_only_comp_is_screened_and_rendered_as_a_ceiling(db):
    from engine import report, store
    from engine.score import score

    low = _job(external_id="max-only")
    low.comp_max, low.comp_source = 80_000, "board"
    score(low, _profile(floor=100_000))
    assert low.gate == "EXCLUDED"
    assert "ceiling $80,000 below $100,000" in low.reasons[0]
    store.upsert(db, [low])
    row = db.execute("SELECT * FROM jobs WHERE uid=?", (low.uid,)).fetchone()
    assert report._comp_display(row) == "up to $80,000 (board field)"


def test_compensation_bounds_are_replaced_as_one_claim(db):
    from engine import store

    surfaced = _job(company="Atomic Comp", external_id="atomic")
    surfaced.comp_min, surfaced.comp_max = 120_000, 160_000
    surfaced.comp_text, surfaced.comp_source = "old full band", "board"
    store.upsert(db, [surfaced])

    max_only = _job(company="Atomic Comp", external_id="atomic")
    max_only.comp_min, max_only.comp_max = None, 150_000
    max_only.comp_text, max_only.comp_source = "", "board"
    store.upsert(db, [max_only])
    row = db.execute("SELECT comp_min, comp_max, comp_text, comp_source FROM jobs "
                     "WHERE uid=?", (surfaced.uid,)).fetchone()
    assert tuple(row) == (None, 150_000, "", "board")

    demoted = _job(company="Atomic Demoted", external_id="atomic-demoted")
    demoted.comp_min, demoted.comp_max = 100_000, 140_000
    demoted.comp_source = "board"
    store.upsert(db, [demoted])
    demoted_max = _job(company="Atomic Demoted", external_id="atomic-demoted",
                       gate="EXCLUDED", score=0)
    demoted_max.comp_max, demoted_max.comp_source = 130_000, "body"
    store.reconcile(db, {demoted.uid: demoted_max},
                    {("greenhouse", "Atomic Demoted")}, set())
    row = db.execute("SELECT comp_min, comp_max, comp_source FROM jobs WHERE uid=?",
                     (demoted.uid,)).fetchone()
    assert tuple(row) == (None, 130_000, "body")


@pytest.mark.parametrize(
    "cfg, message",
    [
        ({"lanes": [{"key": "pm", "weight": 50, "titles": "product manager"}]},
         "titles must be a list"),
        ({"comp": {"screen_floor": True},
          "lanes": [{"key": "pm", "weight": 50, "titles": ["product manager"]}]},
         "comp.screen_floor"),
        ({"lanes": [{"key": "pm", "weight": -10, "titles": ["product manager"]}]},
         "0-100"),
        ({"dream_companies": [None],
          "lanes": [{"key": "pm", "weight": 50, "titles": ["product manager"]}]},
         "dream_companies[0]"),
        ({"signals": [{"terms": "product led", "points": 5}],
          "lanes": [{"key": "pm", "weight": 50, "titles": ["product manager"]}]},
         "signals[0].terms"),
        ({"exclusions": {"body_patterns": [{"terms": "must code"}]},
          "lanes": [{"key": "pm", "weight": 50, "titles": ["product manager"]}]},
         "body_patterns[0].terms"),
    ],
)
def test_nested_profile_values_fail_closed(tmp_path, cfg, message):
    from engine.score import Profile, ProfileError

    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ProfileError, match=re.escape(message)):
        Profile.load(path)


def test_remote_comma_us_is_positive_us_evidence():
    from engine.score import Profile, location_verdict

    job = _job(location="Remote, US")
    assert location_verdict(job, Profile())[0] == "pass"


def test_consistency_checks_hyphenated_companies_locations_and_verify_tail(
        db, tmp_path, monkeypatch):
    from engine import consistency, report, store

    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    full = _job(company="Acme-Co", external_id="full", score=70,
                location="Atlanta, GA")
    full.lane = "pm"
    tail = _job(company="Tail-Co", title="Salesforce Administrator",
                external_id="tail", url="https://jobs.test/tail",
                gate="VERIFY", score=40)
    tail.reasons = ["NEEDS CHECK: comp unstated"]
    store.upsert(db, [full, tail])
    rows = store.query(db)
    path = report.write_report(db, rows, health=[],
                               run_detail={"pulled": 2, "sources_ok": 1},
                               filename="consistency.md")
    text = path.read_text()
    assert f"- **UID** `{full.uid}`" in text
    assert consistency.check_report(db, path) == []

    db.execute("UPDATE jobs SET location='London, UK' WHERE uid=?", (full.uid,))
    db.commit()
    problems = consistency.check_report(db, path)
    assert any("location" in problem and "London, UK" in problem for problem in problems)

    db.execute("UPDATE jobs SET location='Atlanta, GA', company='Changed Company', "
               "title='Changed Title', source='lever', lane='other', reasons='changed' "
               "WHERE uid=?", (full.uid,))
    db.commit()
    problems = consistency.check_report(db, path)
    assert any("heading" in problem for problem in problems)
    assert any("source" in problem for problem in problems)
    assert any("lane" in problem for problem in problems)
    assert any("reason" in problem for problem in problems)

    db.execute("UPDATE jobs SET company='Acme-Co', title='Product Manager', "
               "source='greenhouse', lane='pm', reasons='original verdict' "
               "WHERE uid=?", (full.uid,))
    db.commit()
    tampered = (text.replace("- 40 ·", "- 99 ·")
                .replace("https://jobs.test/tail", "https://jobs.test/not-the-tail")
                .replace("**Title** Salesforce Administrator", "**Title** Fake Title")
                .replace("**Company** Tail-Co", "**Company** Fake-Co")
                .replace("**Check** comp unstated", "**Check** made up"))
    path.write_text(tampered)
    problems = consistency.check_report(db, path)
    assert any("report says score 99" in problem for problem in problems)
    assert any("not-the-tail" in problem and "URL" in problem for problem in problems)
    assert any("VERIFY tail shows title" in problem for problem in problems)
    assert any("VERIFY tail shows company" in problem for problem in problems)
    assert any("VERIFY tail shows reason" in problem for problem in problems)


def test_wal_mtime_marks_an_older_report_stale(db, tmp_path):
    from engine import consistency, store

    report_path = tmp_path / "report.md"
    report_path.write_text("# report\n")
    job = _job(external_id="wal")
    store.upsert(db, [job])
    db_file = Path(db.execute("PRAGMA database_list").fetchone()[2])
    wal = Path(str(db_file) + "-wal")
    assert wal.exists(), "fixture must exercise SQLite WAL mode"

    now = time.time_ns()
    # All three writes are within 200ms. The former one-second grace period
    # incorrectly blessed this report even though the WAL commit is newer.
    os.utime(db_file, ns=(now - 200_000_000, now - 200_000_000))
    os.utime(report_path, ns=(now - 100_000_000, now - 100_000_000))
    os.utime(wal, ns=(now, now))
    assert "older than the database" in consistency.report_is_stale(db, report_path)


def test_logical_report_marker_survives_checkpoint_mtime_noise(db, tmp_path, monkeypatch):
    """Checkpointing rendered WAL data after the write must not make it stale."""
    from engine import consistency, report

    monkeypatch.setattr(report, "OUT_DIR", tmp_path)
    job = _job(external_id="checkpoint-fresh")
    job.score, job.gate, job.reasons = 70, "QUALIFIED", ["fit"]
    from engine import store
    store.upsert(db, [job])
    row = db.execute("SELECT * FROM jobs WHERE uid=?", (job.uid,)).fetchone()
    path = report.write_report(db, [row], health=[], run_detail={})

    future = path.stat().st_mtime_ns + 1_000_000_000
    db_file = Path(db.execute("PRAGMA database_list").fetchone()[2])
    os.utime(db_file, ns=(future, future))
    wal = Path(str(db_file) + "-wal")
    if wal.exists():
        os.utime(wal, ns=(future, future))

    assert consistency.report_is_stale(db, path) is None


def test_logical_report_marker_detects_real_change_despite_future_report_mtime(
        db, tmp_path, monkeypatch):
    from engine import consistency, report, store

    monkeypatch.setattr(report, "OUT_DIR", tmp_path)
    job = _job(external_id="logical-change")
    job.score, job.gate, job.reasons = 70, "QUALIFIED", ["fit"]
    store.upsert(db, [job])
    row = db.execute("SELECT * FROM jobs WHERE uid=?", (job.uid,)).fetchone()
    path = report.write_report(db, [row], health=[], run_detail={})

    db.execute("UPDATE jobs SET score=71 WHERE uid=?", (job.uid,))
    db.commit()
    future = path.stat().st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(future, future))

    stale = consistency.report_is_stale(db, path)
    assert stale and "older than the database" in stale


def test_consistency_repair_never_claims_the_old_report_still_agrees(
        db, tmp_path, monkeypatch, capsys):
    import careerkit
    from engine import report, store

    monkeypatch.setattr(report, "OUT_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    monkeypatch.setattr(careerkit.store, "connect", lambda: db)
    job = _job(external_id="repair-stales-report")
    store.upsert(db, [job])
    db.execute("UPDATE jobs SET comp_min=1000, comp_max=20000, comp_source='body' "
               "WHERE uid=?", (job.uid,))
    db.commit()
    row = db.execute("SELECT * FROM jobs WHERE uid=?", (job.uid,)).fetchone()
    path = report.write_report(db, [row], health=[], run_detail={})

    careerkit.cmd_consistency(SimpleNamespace(report=str(path), repair=True))
    output = capsys.readouterr().out
    assert "stale report was skipped" in output
    assert "Consistent." not in output


def test_report_sanitizes_health_scraper_and_discovery_metadata(
        db, tmp_path, monkeypatch):
    from engine import aggregators, report

    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(aggregators, "policy", lambda _source: {"kind": "scraping"})
    health = db.execute(
        "SELECT ? AS source, ? AS last_ok, 0 AS last_count, "
        "2 AS consecutive_failures, ? AS last_error",
        ("feed:evil|scraper\n## forged", "2026-08-20", "boom | injected\n## forged"),
    ).fetchall()
    path = report.write_report(
        db, [], health=health,
        run_detail={
            "pulled": 0,
            "sources_ok": 0,
            "discovered": [{
                "name": "Bad Co\n## forged [click](javascript:alert(1))",
                "ats": "evil|ats",
                "slug": "slug`break",
                "open_roles": "1|2",
            }],
        },
        filename="sanitized.md",
    )
    text = path.read_text()
    assert "\n## forged" not in text
    assert "evil&#124;scraper" in text
    assert "boom &#124; injected" in text
    assert "evil&#124;ats" in text
    assert "&#91;click&#93;" in text
    assert "javascript:alert" in text  # visible evidence, never an active link


def test_report_omits_unsafe_primary_sibling_and_tail_urls(db):
    from engine import report, store

    first = _job(company="Unsafe URL Co", external_id="unsafe-a",
                 url="javascript:alert(1)")
    second = _job(company="Unsafe URL Co", external_id="unsafe-b",
                  url="http://127.0.0.1/private")
    store.upsert(db, [first, second])
    rows = [db.execute("SELECT * FROM jobs WHERE uid=?", (j.uid,)).fetchone()
            for j in (first, second)]

    full = report._row_block(rows, 1)
    tail = report._tail_line([rows[0]])
    assert full.count(report._URL_OMITTED) == 2
    assert report._URL_OMITTED in tail
    assert "javascript:" not in full + tail
    assert "127.0.0.1" not in full + tail
