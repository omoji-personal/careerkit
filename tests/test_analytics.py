"""Pipeline intelligence: calculations must stay honest and local."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERKIT_HOME", str(tmp_path))
    from engine import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    return store.connect()


def job(company, title, url, *, lane="crm", score=80):
    from engine.models import Job
    j = Job(company=company, title=title, url=url, source="greenhouse",
            location="Remote", description="A complete role description")
    j.gate, j.score, j.lane, j.reasons = "QUALIFIED", score, lane, ["fit"]
    return j


def test_pipeline_metrics_use_real_stage_dates_and_surface_followups(db):
    from engine import analytics, store
    closed = job("Acme", "CRM Lead", "https://example.test/1")
    quiet = job("Beta", "Platform Manager", "https://example.test/2")
    store.upsert(db, [closed, quiet])

    store.record_application_stage(db, closed.uid, "applied", on="2026-07-01")
    store.record_application_stage(db, closed.uid, "interviewing", on="2026-07-05")
    store.record_application_stage(db, closed.uid, "rejected", on="2026-07-10")
    store.record_application_stage(db, quiet.uid, "applied", on="2026-07-20")

    snap = analytics.build_snapshot(db, as_of=date(2026, 8, 8), follow_up_days=14)
    assert snap["summary"] == {
        "submitted": 2, "active": 1, "interviewed": 1, "offers": 0,
        "rejected": 1, "withdrawn": 0, "known_response_rate": 50.0,
        "interview_rate": 50.0, "offer_rate": 0.0, "follow_up_due": 1,
        "median_days_to_interview": 4, "median_days_to_outcome": 9,
    }
    assert snap["follow_ups"][0]["company"] == "Beta"
    assert snap["follow_ups"][0]["days_silent"] == 19
    assert snap["by_lane"] == [{"lane": "crm", "submitted": 2,
                                 "interviewed": 1, "interview_rate": 50.0,
                                 "offers": 0}]
    json.dumps(snap)  # public result must remain scheduler/API friendly


def test_unmapped_evidence_counts_without_being_attached_to_the_wrong_job(db):
    from engine import analytics
    evidence = [{"company": "Outside Search", "title": "Director of CRM",
                 "status": "applied", "on": "2026-08-01", "_line": 1}]
    snap = analytics.build_snapshot(db, evidence, as_of=date(2026, 8, 8))
    assert snap["summary"]["submitted"] == 1
    assert snap["applications"][0]["mapped"] is False
    assert snap["data_quality"]["unmapped_evidence"] == 1


def test_direct_evidence_without_line_numbers_keeps_each_match_separate(db):
    from engine import analytics, store
    a = job("Acme", "Architect", "https://example.test/no-line-a")
    b = job("Beta", "Manager", "https://example.test/no-line-b")
    store.upsert(db, [a, b])
    snap = analytics.build_snapshot(db, [
        {"company": "Acme", "title": "Architect", "status": "applied",
         "on": "2026-08-01"},
        {"company": "Beta", "title": "Manager", "status": "applied",
         "on": "2026-08-02"},
    ], as_of=date(2026, 8, 8))
    assert {x["uid"] for x in snap["applications"]} == {a.uid, b.uid}


def test_explicit_applied_on_makes_later_evidence_timing_measurable(db):
    from engine import analytics, store
    j = job("Acme", "Architect", "https://example.test/a")
    store.upsert(db, [j])
    evidence = [{"company": "Acme", "title": "Architect", "status": "interviewing",
                 "applied_on": "2026-07-01", "on": "2026-07-08", "_line": 1}]
    snap = analytics.build_snapshot(db, evidence, as_of=date(2026, 8, 8))
    assert snap["summary"]["median_days_to_interview"] == 7
    assert snap["applications"][0]["current_stage"] == "interviewing"


def test_application_progress_is_dated_idempotent_and_preserves_existing_notes(db):
    from engine import store
    j = job("Acme", "Architect", "https://example.test/b")
    store.upsert(db, [j])
    store.set_status(db, j.uid, "reviewed", "recruiter context")
    assert store.record_application_stage(
        db, j.uid, "interviewing", on="2026-08-03", detail="phone screen") is True
    assert store.record_application_stage(
        db, j.uid, "interviewing", on="2026-08-03", detail="phone screen") is False
    row = db.execute("SELECT status,notes FROM jobs WHERE uid=?", (j.uid,)).fetchone()
    assert dict(row) == {"status": "applied", "notes": "recruiter context"}
    events = [e for e in store.history(db, j.uid)
              if e["kind"] == "application:interviewing"]
    assert len(events) == 1 and events[0]["at"].startswith("2026-08-03")


def test_bad_progress_date_writes_nothing(db):
    from engine import store
    j = job("Acme", "Architect", "https://example.test/c")
    store.upsert(db, [j])
    with pytest.raises(ValueError, match="invalid event date"):
        store.record_application_stage(db, j.uid, "applied", on="next Thursday")
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (j.uid,)).fetchone()[0] == "new"
    assert store.history(db, j.uid) == []


def test_evidence_reconciliation_uses_evidence_day_not_reconciliation_day(db):
    from engine import applied, store
    j = job("Acme", "Architect", "https://example.test/d")
    store.upsert(db, [j])
    result = applied.reconcile(db, [{"company": "Acme", "title": "Architect",
                                     "status": "rejected", "on": "2026-07-17",
                                     "source": "gmail", "_line": 1}], apply=True)
    assert result["problems"] == []
    event = next(e for e in store.history(db, j.uid)
                 if e["kind"] == "application:rejected")
    assert event["at"].startswith("2026-07-17")


def test_dashboard_escapes_board_data_and_loads_nothing_remote(db, tmp_path):
    from engine import analytics, store
    safe = job("<script>alert(1)</script>", "CRM & Platform", "https://example.test/job")
    unsafe = job("Unsafe URL", "Director", "https://example.test/unsafe")
    store.upsert(db, [safe, unsafe])
    db.execute("UPDATE jobs SET url='javascript:alert(1)' WHERE uid=?", (unsafe.uid,))
    db.commit()
    rows = store.query(db, limit=20)
    snap = analytics.build_snapshot(db, as_of=date(2026, 8, 8))
    out = analytics.write_dashboard(db, snap, rows, filename="test-dashboard.html")
    page = out.read_text()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert 'href="javascript:' not in page
    assert 'href="https://example.test/job"' in page
    assert "default-src 'none'" in page
    assert "no telemetry" in page


def test_terminal_formatter_calls_out_data_limitations(db):
    from engine import analytics
    snap = analytics.build_snapshot(
        db, [{"company": "Unknown", "status": "rejected", "_line": 1}],
        as_of=date(2026, 8, 8))
    text = analytics.format_text(snap)
    assert "Pipeline as of 2026-08-08" in text
    assert "Data notes:" in text
    assert "1 application(s) lack a usable date" in text


def test_employer_relationship_memory_is_idempotent_and_annotates_roles(db):
    from engine import analytics, store
    j = job("Acme, Inc.", "CRM Director", "https://example.test/relationship")
    store.upsert(db, [j])
    assert store.add_relationship(
        db, "Acme", "invitation", on="2025-11-04", contact="Jane Recruiter",
        detail="Reach out directly for future CRM roles") is True
    assert store.add_relationship(
        db, "Acme", "invitation", on="2025-11-04", contact="Jane Recruiter",
        detail="Reach out directly for future CRM roles") is False
    assert len(store.relationships(db, "Acme Inc")) == 1

    snap = analytics.build_snapshot(db, as_of=date(2026, 8, 8))
    out = analytics.write_dashboard(db, snap, store.query(db),
                                    filename="relationship-dashboard.html")
    page = out.read_text()
    assert "Relationship memory" in page
    assert "Jane Recruiter" in page
    assert "1 relationship note" in page


def test_relationship_context_can_exist_before_any_posting(db):
    from engine import store
    store.add_relationship(db, "Future Employer", "referral", on="2026-08-01",
                           contact="Alex", contact_email="alex@example.test")
    row = store.relationships(db, "Future Employer")[0]
    assert row["contact"] == "Alex"
    assert row["contact_email"] == "alex@example.test"


def test_an_unambiguous_employer_acronym_adopts_the_stored_name(db):
    from engine import store
    j = job("National Public Radio", "CRM Manager", "https://example.test/npr")
    store.upsert(db, [j])
    store.add_relationship(db, "NPR", "invitation", on="2025-11-04")
    row = store.relationships(db, "National Public Radio")[0]
    assert row["company"] == "National Public Radio"


def test_dormant_optional_feeds_are_not_counted_as_broken_sources(db):
    from engine import analytics
    db.execute("INSERT INTO source_health "
               "(source,last_count,last_error,consecutive_failures) VALUES (?,?,?,?)",
               ("feed:optional", 0, "dormant: add api_key", 20))
    db.commit()
    page = analytics.write_dashboard(
        db, analytics.build_snapshot(db, as_of=date(2026, 8, 8)), [],
        filename="dormant-dashboard.html").read_text()
    card = page.split('data-testid="source-failures"', 1)[1].split("</article>", 1)[0]
    assert "Source failures" in card
    assert "<strong>0</strong>" in card


@pytest.mark.parametrize("text,expected", [
    ("Apply by July 30, 2026 to be considered.", "2026-07-30"),
    ("Application deadline: 2026-08-01", "2026-08-01"),
    ("Applications will be accepted through 08/05/2026.", "2026-08-05"),
    ("The posting closes on Aug 7th, 2026.", "2026-08-07"),
])
def test_explicit_application_deadlines_are_parsed_without_guessing(text, expected):
    from engine.score import posting_deadline
    assert posting_deadline(text, today=date(2026, 8, 8))[0].isoformat() == expected


def test_open_until_filled_and_unrelated_dates_are_not_deadlines():
    from engine.score import posting_deadline
    text = "Open until filled. Benefits begin January 1, 2026. Copyright 2026."
    assert posting_deadline(text, today=date(2026, 8, 8)) is None


def test_an_expired_listing_is_excluded_even_for_a_dream_employer():
    from engine.score import Profile, score
    j = job("Dream Co", "CRM Director", "https://example.test/expired")
    j.description = "Apply by January 2, 2000. Lead our Salesforce program."
    profile = Profile.load(ROOT / "profile.example" / "profile.yaml")
    profile.dream_companies.add("dream co")
    score(j, profile)
    assert j.gate == "EXCLUDED"
    assert "posting closed 2000-01-02" in j.reasons[0]
