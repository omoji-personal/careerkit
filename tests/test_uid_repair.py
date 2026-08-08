"""Regression coverage for identity migrations that must preserve user state."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _db(tmp_path, monkeypatch):
    from engine import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    return store.connect()


def _job():
    from engine.models import Job
    return Job(
        company="Acme",
        title="Platform Manager (Customer Portal) (Remote)",
        url="https://boards.example.test/jobs/123",
        source="greenhouse",
        external_id="123",
        board="greenhouse:acme",
    )


def test_same_url_uid_change_adopts_the_human_status(tmp_path, monkeypatch):
    """Changing title normalization produced a fresh NEW row for a requisition
    that was already rejected under its previous UID."""
    from engine import store
    job = _job()
    con = _db(tmp_path, monkeypatch)
    store.upsert(con, [job], run_id=1)
    old_uid = job.legacy_external_uid
    con.execute("UPDATE jobs SET uid=?, status='rejected', notes='rejected by email', "
                "first_seen='2026-07-01' WHERE uid=?", (old_uid, job.uid))
    con.execute("UPDATE sightings SET uid=? WHERE uid=?", (old_uid, job.uid))
    con.execute("INSERT INTO events(uid,at,kind,detail) VALUES "
                "(?,'2026-07-02','status:rejected','email')", (old_uid,))
    con.commit()

    store.upsert(con, [job], run_id=2)

    rows = con.execute("SELECT * FROM jobs WHERE url=?", (job.url,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["uid"] == job.uid
    assert rows[0]["status"] == "rejected"
    assert rows[0]["notes"] == "rejected by email"
    assert rows[0]["first_seen"] == "2026-07-01"
    assert con.execute("SELECT uid FROM events").fetchone()["uid"] == job.uid


def test_same_url_uid_change_merges_an_existing_fresh_duplicate(tmp_path, monkeypatch):
    """If a pull already inserted the new UID, repair both rows in place and
    keep the terminal status and notes from the stranded historical row."""
    from engine import store
    job = _job()
    con = _db(tmp_path, monkeypatch)
    store.upsert(con, [job], run_id=2)

    columns = [r["name"] for r in con.execute("PRAGMA table_info(jobs)") if r["name"] != "uid"]
    names = ",".join(columns)
    con.execute(f"INSERT INTO jobs(uid,{names}) SELECT ?,{names} FROM jobs WHERE uid=?",
                (job.legacy_external_uid, job.uid))
    con.execute("UPDATE jobs SET status='rejected', notes='rejected by email', "
                "first_seen='2026-07-01', seen_count=5 WHERE uid=?",
                (job.legacy_external_uid,))
    con.execute("INSERT INTO sightings(uid,source,url,seen_on) VALUES "
                "(?,'greenhouse',?,'2026-07-01')", (job.legacy_external_uid, job.url))
    con.execute("INSERT INTO events(uid,at,kind,detail) VALUES "
                "(?,'2026-07-02','status:rejected','email')", (job.legacy_external_uid,))
    con.commit()

    store.upsert(con, [job], run_id=3)

    rows = con.execute("SELECT * FROM jobs WHERE url=?", (job.url,)).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["uid"] == job.uid
    assert row["status"] == "rejected"
    assert "rejected by email" in row["notes"]
    assert row["first_seen"] == "2026-07-01"
    assert row["seen_count"] >= 6
    assert con.execute("SELECT uid FROM events").fetchone()["uid"] == job.uid
