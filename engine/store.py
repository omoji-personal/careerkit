"""SQLite persistence.

The point of persisting is `first_seen`. Without it every run re-reports the
same postings and there is no way to answer the only question that matters on a
daily run: what is NEW since last time. It also gives dedupe across sources
(the same role arrives via the ATS, an aggregator, and a repost) while keeping
provenance for each sighting.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from .models import Job

DB_PATH = Path(os.environ.get("CAREERKIT_HOME") or Path(__file__).resolve().parent.parent) / "data" / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  uid            TEXT PRIMARY KEY,
  company        TEXT, title TEXT, url TEXT, location TEXT,
  source         TEXT, lane TEXT, employer_tier TEXT,
  posted_at      TEXT, department TEXT,
  comp_min       INTEGER, comp_max INTEGER, comp_text TEXT,
  score          INTEGER, gate TEXT, reasons TEXT,
  description    TEXT,
  first_seen     TEXT, last_seen TEXT, seen_count INTEGER DEFAULT 1,
  status         TEXT DEFAULT 'new',      -- new|reviewed|applied|rejected|ignored
  notes          TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sightings (
  uid TEXT, source TEXT, url TEXT, seen_on TEXT,
  PRIMARY KEY (uid, source)
);
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  started TEXT, finished TEXT, pulled INTEGER, new INTEGER, qualified INTEGER,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS source_health (
  source TEXT PRIMARY KEY, last_ok TEXT, last_count INTEGER,
  last_error TEXT, consecutive_failures INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_gate ON jobs(gate, score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_first ON jobs(first_seen DESC);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert(con: sqlite3.Connection, jobs: list[Job]) -> tuple[list[Job], list[Job]]:
    """Insert or refresh. Returns (new_jobs, seen_again)."""
    today = date.today().isoformat()
    new, again = [], []
    with closing(con.cursor()) as cur:
        for j in jobs:
            cur.execute("SELECT uid, status FROM jobs WHERE uid=?", (j.uid,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE jobs SET last_seen=?, seen_count=seen_count+1, score=?, "
                    "gate=?, reasons=?, comp_min=COALESCE(?,comp_min), "
                    "comp_max=COALESCE(?,comp_max) WHERE uid=?",
                    (today, j.score, j.gate, " | ".join(j.reasons),
                     j.comp_min, j.comp_max, j.uid),
                )
                again.append(j)
            else:
                cur.execute(
                    "INSERT INTO jobs (uid,company,title,url,location,source,lane,"
                    "employer_tier,posted_at,department,comp_min,comp_max,comp_text,"
                    "score,gate,reasons,description,first_seen,last_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (j.uid, j.company, j.title, j.url, j.location, j.source, j.lane,
                     j.employer_tier, j.posted_at, j.department, j.comp_min, j.comp_max,
                     j.comp_text, j.score, j.gate, " | ".join(j.reasons),
                     j.description[:20000], today, today),
                )
                new.append(j)
            cur.execute(
                "INSERT OR REPLACE INTO sightings (uid,source,url,seen_on) VALUES (?,?,?,?)",
                (j.uid, j.source, j.url, today),
            )
    con.commit()
    return new, again


def record_health(con: sqlite3.Connection, source: str, count: int, error: str | None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with closing(con.cursor()) as cur:
        if error:
            cur.execute(
                "INSERT INTO source_health (source,last_ok,last_count,last_error,consecutive_failures) "
                "VALUES (?,NULL,?,?,1) ON CONFLICT(source) DO UPDATE SET "
                "last_count=excluded.last_count, last_error=excluded.last_error, "
                "consecutive_failures=source_health.consecutive_failures+1",
                (source, count, error[:300]),
            )
        else:
            cur.execute(
                "INSERT INTO source_health (source,last_ok,last_count,last_error,consecutive_failures) "
                "VALUES (?,?,?,NULL,0) ON CONFLICT(source) DO UPDATE SET "
                "last_ok=excluded.last_ok, last_count=excluded.last_count, "
                "last_error=NULL, consecutive_failures=0",
                (source, now, count),
            )
    con.commit()


def start_run(con: sqlite3.Connection) -> int:
    cur = con.execute("INSERT INTO runs (started) VALUES (?)",
                      (datetime.now().isoformat(timespec="seconds"),))
    con.commit()
    return cur.lastrowid


def finish_run(con: sqlite3.Connection, run_id: int, pulled: int, new: int,
               qualified: int, detail: dict) -> None:
    con.execute(
        "UPDATE runs SET finished=?, pulled=?, new=?, qualified=?, detail=? WHERE run_id=?",
        (datetime.now().isoformat(timespec="seconds"), pulled, new, qualified,
         json.dumps(detail)[:60000], run_id),
    )
    con.commit()


def query(con: sqlite3.Connection, *, gates: tuple[str, ...] = ("QUALIFIED", "VERIFY"),
          min_score: int = 0, new_only: bool = False, since: str | None = None,
          limit: int = 200) -> list[sqlite3.Row]:
    sql = ("SELECT * FROM jobs WHERE gate IN (%s) AND score >= ? AND status NOT IN "
           "('rejected','ignored','applied')" % ",".join("?" * len(gates)))
    params: list = [*gates, min_score]
    if new_only:
        sql += " AND first_seen = last_seen"
    if since:
        sql += " AND first_seen >= ?"
        params.append(since)
    sql += " ORDER BY score DESC, first_seen DESC LIMIT ?"
    params.append(limit)
    return list(con.execute(sql, params))


def set_status(con: sqlite3.Connection, uid: str, status: str, notes: str = "") -> None:
    con.execute("UPDATE jobs SET status=?, notes=COALESCE(NULLIF(?,''),notes) WHERE uid=?",
                (status, notes, uid))
    con.commit()


def stats(con: sqlite3.Connection) -> dict:
    g = {r["gate"]: r["n"] for r in con.execute("SELECT gate, COUNT(*) n FROM jobs GROUP BY gate")}
    s = {r["source"]: r["n"] for r in
         con.execute("SELECT source, COUNT(*) n FROM jobs GROUP BY source ORDER BY n DESC")}
    total = con.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    runs = con.execute("SELECT COUNT(*) n FROM runs WHERE finished IS NOT NULL").fetchone()["n"]
    return {"total": total, "by_gate": g, "by_source": s, "runs": runs}
