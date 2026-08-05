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
  group_key      TEXT,
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
"""

# Indexes are applied AFTER migrations, never in SCHEMA. CREATE TABLE IF NOT
# EXISTS is a no-op on an existing v1 table, so an index naming a column added
# later ("no such column: group_key") aborts connect() for every existing user.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_jobs_gate ON jobs(gate, score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_first ON jobs(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_group ON jobs(group_key);
"""

# Columns added after v1. CREATE TABLE IF NOT EXISTS does nothing to a table
# that already exists, so an existing database would keep the old shape and
# every query naming a new column would fail. Applied on every connect().
_MIGRATIONS = [
    ("jobs", "group_key", "TEXT"),
    ("jobs", "delisted_on", "TEXT"),
    ("jobs", "misses", "INTEGER DEFAULT 0"),
]


def _migrate(con: sqlite3.Connection) -> None:
    for table, col, decl in _MIGRATIONS:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # A pre-2026-08-05 row's uid was sha256(company|normalized_title), which is
    # exactly what group_key is now. Backfilling it lets upsert recognise an
    # existing posting under the new uid scheme instead of treating all of them
    # as brand new, which would erase every first_seen date and detach any
    # applied/rejected status the user had recorded.
    con.execute("UPDATE jobs SET group_key = uid WHERE group_key IS NULL")
    con.commit()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    con.executescript(INDEXES)
    return con


def upsert(con: sqlite3.Connection, jobs: list[Job]) -> tuple[list[Job], list[Job]]:
    """Insert or refresh. Returns (new_jobs, seen_again)."""
    today = date.today().isoformat()
    new, again, adopted = [], [], []
    with closing(con.cursor()) as cur:
        for j in jobs:
            cur.execute("SELECT uid, status FROM jobs WHERE uid=?", (j.uid,))
            row = cur.fetchone()
            if row is None and j.uid != j.group_key:
                # No row under the new uid. Look for a legacy row in the same
                # group - one still keyed the old way (uid == group_key) - and
                # adopt it, so its first_seen, status and notes carry forward.
                # Only the first requisition of a group can adopt; genuinely
                # distinct siblings insert fresh, which is the point of the
                # new key.
                cur.execute("SELECT uid FROM jobs WHERE group_key=? AND uid=group_key "
                            "LIMIT 1", (j.group_key,))
                legacy = cur.fetchone()
                if legacy:
                    try:
                        cur.execute("UPDATE jobs SET uid=? WHERE uid=?", (j.uid, legacy["uid"]))
                        cur.execute("UPDATE OR REPLACE sightings SET uid=? WHERE uid=?",
                                    (j.uid, legacy["uid"]))
                        adopted.append(j.uid)
                        cur.execute("SELECT uid, status FROM jobs WHERE uid=?", (j.uid,))
                        row = cur.fetchone()
                    except sqlite3.IntegrityError:
                        row = None
            if row:
                # url/title/location/description are REFRESHED, not frozen at
                # first sighting. A board that edits a title or re-issues a
                # posting under a new URL used to leave the row pointing at a
                # dead link forever. delisted_on is cleared: seeing it again
                # means it is live again.
                cur.execute(
                    "UPDATE jobs SET last_seen=?, seen_count=seen_count+1, score=?, "
                    "gate=?, reasons=?, comp_min=COALESCE(?,comp_min), "
                    "comp_max=COALESCE(?,comp_max), url=?, title=?, location=?, "
                    "description=COALESCE(NULLIF(?,''),description), "
                    "group_key=?, delisted_on=NULL, misses=0 WHERE uid=?",
                    (today, j.score, j.gate, " | ".join(j.reasons),
                     j.comp_min, j.comp_max, j.url, j.title, j.location,
                     j.description[:20000], j.group_key, j.uid),
                )
                again.append(j)
            else:
                cur.execute(
                    "INSERT INTO jobs (uid,group_key,company,title,url,location,source,lane,"
                    "employer_tier,posted_at,department,comp_min,comp_max,comp_text,"
                    "score,gate,reasons,description,first_seen,last_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (j.uid, j.group_key, j.company, j.title, j.url, j.location, j.source,
                     j.lane, j.employer_tier, j.posted_at, j.department, j.comp_min,
                     j.comp_max, j.comp_text, j.score, j.gate, " | ".join(j.reasons),
                     j.description[:20000], today, today),
                )
                new.append(j)
            cur.execute(
                "INSERT OR REPLACE INTO sightings (uid,source,url,seen_on) VALUES (?,?,?,?)",
                (j.uid, j.source, j.url, today),
            )
    con.commit()
    return new, again


VALID_STATUS = ("new", "reviewed", "applied", "rejected", "ignored")


def reconcile(con: sqlite3.Connection, seen_uids: set[str], demoted: dict[str, tuple[int, str, str]],
              healthy_sources: set[str]) -> tuple[int, int]:
    """Close the loop after a run. Returns (delisted, demoted).

    Two holes this fills, both of which surfaced dead or wrong rows as live:

    1. cmd_pull only upserts QUALIFIED/VERIFY, so a posting that stops
       qualifying (the user tightened their profile, or the board edited the
       body) was never written back and kept surfacing at its old gate forever.
    2. A posting removed from its board is simply absent from the next pull.
       Nothing ever marked it closed, so it stayed QUALIFIED indefinitely and
       the report presented it as live. This is the defect that put a dead
       requisition in front of the user in July 2026.

    A row is only delisted when its source actually reported successfully this
    run. A broken board must never be read as "every job there closed".
    """
    today = date.today().isoformat()
    dem = 0
    with closing(con.cursor()) as cur:
        for uid, (score, gate, reasons) in demoted.items():
            cur.execute("UPDATE jobs SET score=?, gate=?, reasons=?, last_seen=?, "
                        "delisted_on=NULL WHERE uid=?", (score, gate, reasons, today, uid))
            dem += cur.rowcount
        if not healthy_sources:
            con.commit()
            return 0, dem
        marks = ",".join("?" * len(healthy_sources))
        # Count the miss first; only retire on the SECOND consecutive one.
        # Board counts are stable run to run (15,879 / 15,932 / 15,935 across
        # three real runs), so a single absence is decent evidence - but not
        # good enough. A partial or transient response would retire live jobs,
        # and hiding a live job is the exact failure this tool exists to
        # prevent, whereas showing a dead one for one more run is cheap.
        cur.execute(
            f"UPDATE jobs SET misses = misses + 1 WHERE delisted_on IS NULL "
            f"AND last_seen < ? AND source IN ({marks}) "
            f"AND status NOT IN ('applied','rejected','ignored')",
            (today, *healthy_sources),
        )
        cur.execute(
            "UPDATE jobs SET delisted_on=? WHERE delisted_on IS NULL AND misses >= 2",
            (today,),
        )
        delisted = cur.rowcount
    con.commit()
    return delisted, dem


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
           "('rejected','ignored','applied') AND delisted_on IS NULL"
           % ",".join("?" * len(gates)))
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
    """Validated on purpose. Any string used to be accepted, but query() only
    understands five, so a typo ('apllied') silently left the job in the active
    list and the user believed it was filed."""
    if status not in VALID_STATUS:
        raise ValueError(f"unknown status {status!r}. Use one of: {', '.join(VALID_STATUS)}")
    cur = con.execute(
        "UPDATE jobs SET status=?, notes=COALESCE(NULLIF(?,''),notes) WHERE uid=?",
        (status, notes, uid))
    con.commit()
    if cur.rowcount == 0:
        raise KeyError(f"no posting with uid {uid!r} (check the id in the report)")


def stats(con: sqlite3.Connection) -> dict:
    g = {r["gate"]: r["n"] for r in con.execute("SELECT gate, COUNT(*) n FROM jobs GROUP BY gate")}
    s = {r["source"]: r["n"] for r in
         con.execute("SELECT source, COUNT(*) n FROM jobs GROUP BY source ORDER BY n DESC")}
    total = con.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    runs = con.execute("SELECT COUNT(*) n FROM runs WHERE finished IS NOT NULL").fetchone()["n"]
    return {"total": total, "by_gate": g, "by_source": s, "runs": runs}
