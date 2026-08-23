"""Application evidence may auto-write only with exact opening identity."""
from __future__ import annotations

import argparse
import json

import pytest

import careerkit
from engine import applied, store
from engine.models import Job


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    con = store.connect()
    yield con
    con.close()


def _job(company: str, title: str, url: str, external_id: str) -> Job:
    return Job(
        company=company, title=title, url=url, source="greenhouse",
        board="greenhouse:example", external_id=external_id,
    )


def test_explicit_different_greenhouse_url_never_falls_back_to_same_title(db):
    job = _job(
        "Example AI", "CRM Platform Lead",
        "https://job-boards.greenhouse.io/example/jobs/5366149008", "5366149008",
    )
    store.upsert(db, [job])
    result = applied.reconcile(db, [{
        "company": "Example AI", "title": "CRM Platform Lead",
        "url": "https://job-boards.greenhouse.io/example/jobs/5394958008",
        "status": "applied", "on": "2026-08-20", "_line": 1,
    }], apply=True)

    assert result["matched"] == []
    assert len(result["unmatched"]) == 1
    assert "posting URL does not match" in result["unmatched"][0]["why"]
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (job.uid,)).fetchone()[0] == "new"


def test_fragment_routed_requisition_ids_never_collapse_to_the_same_url(db):
    job = _job(
        "Example AI", "CRM Platform Lead",
        "https://careers.example.test/app#/jobs/REQ-1", "REQ-1",
    )
    store.upsert(db, [job])

    result = applied.reconcile(db, [{
        "company": "Example AI", "title": "CRM Platform Lead",
        "url": "https://careers.example.test/app#/jobs/REQ-2",
        "status": "applied", "on": "2026-08-20", "_line": 1,
    }], apply=True)

    assert result["matched"] == []
    assert len(result["unmatched"]) == 1
    assert applied._posting_url_key(job.url) != applied._posting_url_key(
        "https://careers.example.test/app#/jobs/REQ-2"
    )
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (job.uid,)).fetchone()[0] == "new"


def test_company_only_single_posting_is_manual_not_auto_written(db):
    job = _job("Acme", "Architect", "https://example.test/jobs/one", "one")
    store.upsert(db, [job])

    result = applied.reconcile(db, [{
        "company": "Acme", "status": "applied", "_line": 1,
    }], apply=True)

    assert result["matched"] == []
    assert len(result["ambiguous"]) == 1
    assert "company-only evidence is never auto-written" in result["ambiguous"][0]["why"]
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (job.uid,)).fetchone()[0] == "new"


def test_older_applied_evidence_is_synced_after_a_newer_rejection(db):
    job = _job("Acme", "Architect", "https://example.test/jobs/two", "two")
    store.upsert(db, [job])
    evidence = [
        {"company": "Acme", "title": "Architect", "url": job.url,
         "status": "applied", "on": "2026-08-05", "_line": 1},
        {"company": "Acme", "title": "Architect", "url": job.url,
         "status": "rejected", "on": "2026-08-12", "_line": 2},
    ]
    applied.reconcile(db, evidence, apply=True)
    mapped = applied.reconcile(db, evidence, apply=False)["matched"]

    assert db.execute("SELECT status FROM jobs WHERE uid=?", (job.uid,)).fetchone()[0] == "rejected"
    assert all(applied.application_history_synchronized(db, item) for item in mapped)


def test_reapplying_exact_evidence_repairs_status_without_duplicate_event(db):
    job = _job("Acme", "Architect", "https://example.test/jobs/repair", "repair")
    store.upsert(db, [job])
    evidence = [{
        "company": "Acme", "title": "Architect", "url": job.url,
        "status": "applied", "on": "2026-08-05", "_line": 1,
    }]
    applied.reconcile(db, evidence, apply=True)
    db.execute("UPDATE jobs SET status='new' WHERE uid=?", (job.uid,))
    db.commit()

    result = applied.reconcile(db, evidence, apply=True)

    assert result["matched"][0]["repaired"] is True
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (job.uid,)).fetchone()[0] == "applied"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE uid=? AND kind='application:applied'",
        (job.uid,),
    ).fetchone()[0] == 1


def test_pinpoint_locale_alias_is_the_same_exact_posting(db):
    job = Job(
        company="Acme Foundation", title="CRM Consultant",
        url="https://acme.pinpointhq.com/postings/34e0a0872a51",
        source="pinpoint", board="pinpoint:acme", external_id="34e0a0872a51",
    )
    store.upsert(db, [job])

    result = applied.reconcile(db, [{
        "company": "Acme Foundation", "title": "CRM Consultant",
        "url": "https://acme.pinpointhq.com/en/postings/34e0a0872a51/",
        "status": "applied", "on": "2026-08-20", "_line": 1,
    }], apply=False)

    assert len(result["matched"]) == 1
    assert result["matched"][0]["uid"] == job.uid
    assert result["matched"][0]["safe_to_apply"] is True


def test_applied_cli_distinguishes_exact_manual_written_and_already_synced(
        db, tmp_path, capsys):
    exact = _job("Acme", "Architect", "https://example.test/jobs/exact", "exact")
    manual = _job("Beta", "CRM Lead", "https://example.test/jobs/manual", "manual")
    store.upsert(db, [exact, manual])
    evidence_path = tmp_path / "applications.jsonl"
    evidence_path.write_text("\n".join([
        json.dumps({
            "company": "Acme", "title": "Architect", "url": exact.url,
            "status": "applied", "on": "2026-08-20",
        }),
        json.dumps({
            "company": "Beta", "title": "CRM Lead",
            "status": "applied", "on": "2026-08-20",
        }),
    ]) + "\n", encoding="utf-8")

    careerkit.cmd_applied(argparse.Namespace(file=str(evidence_path), apply=False))
    preview = capsys.readouterr().out
    assert "would record" in preview
    assert "review only" in preview
    assert "1 exact/safe" in preview

    careerkit.cmd_applied(argparse.Namespace(file=str(evidence_path), apply=True))
    first_apply = capsys.readouterr().out
    assert "recorded" in first_apply
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (exact.uid,)).fetchone()[0] == "applied"
    assert db.execute("SELECT status FROM jobs WHERE uid=?", (manual.uid,)).fetchone()[0] == "new"

    careerkit.cmd_applied(argparse.Namespace(file=str(evidence_path), apply=True))
    assert "already synced" in capsys.readouterr().out


def test_write_evidence_keeps_distinct_urls_and_dates(tmp_path):
    path = tmp_path / "profile" / "applications.jsonl"
    records = [
        {
            "company": "Acme", "title": "Architect",
            "url": "https://example.test/jobs/one",
            "status": "applied", "on": "2026-08-01",
        },
        {
            "company": "Acme", "title": "Architect",
            "url": "https://example.test/jobs/two",
            "status": "applied", "on": "2026-08-01",
        },
        {
            "company": "Acme", "title": "Architect",
            "url": "https://example.test/jobs/two",
            "status": "applied", "on": "2026-08-15",
        },
    ]

    assert applied.write_evidence(path, records) == 3
    assert len(applied.load_evidence(path)) == 3
    assert applied.write_evidence(path, records) == 0
