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

from .models import Job, ATS_SOURCES

DB_PATH = Path(os.environ.get("CAREERKIT_HOME") or Path(__file__).resolve().parent.parent) / "data" / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  uid            TEXT PRIMARY KEY,
  group_key      TEXT,
  board          TEXT,
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
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT, at TEXT, kind TEXT, detail TEXT
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
CREATE INDEX IF NOT EXISTS idx_events_uid ON events(uid, at);
"""

# Columns added after v1. CREATE TABLE IF NOT EXISTS does nothing to a table
# that already exists, so an existing database would keep the old shape and
# every query naming a new column would fail. Applied on every connect().
_MIGRATIONS = [
    ("jobs", "group_key", "TEXT"),
    ("jobs", "delisted_on", "TEXT"),
    ("jobs", "misses", "INTEGER DEFAULT 0"),
    ("source_health", "prev_count", "INTEGER"),
    ("jobs", "schema_v", "INTEGER DEFAULT 2"),
    ("jobs", "miss_on", "TEXT"),
    ("jobs", "board", "TEXT"),
    ("jobs", "first_seen_run", "INTEGER"),
    ("jobs", "last_seen_run", "INTEGER"),
    ("runs", "state", "TEXT"),
]


def _needs_migration(con: sqlite3.Connection) -> bool:
    for table, col, _ in _MIGRATIONS:
        try:
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return True
        if col not in cols:
            return True
    return False


def _backup_before_migration(con: sqlite3.Connection) -> Path | None:
    """Integrity-check and copy the database before altering it.

    A migration that corrupts a user's job history is unrecoverable: the rows
    are months of first_seen dates and application status that cannot be
    re-derived from any board. Cheap insurance, taken only when a migration is
    actually pending."""
    try:
        row = con.execute("PRAGMA quick_check").fetchone()
        if row and str(row[0]).lower() != "ok":
            raise sqlite3.DatabaseError(f"quick_check: {row[0]}")
    except sqlite3.Error:
        raise
    dest = DB_PATH.with_suffix(f".pre-migration-{date.today().isoformat()}.db")
    try:
        with sqlite3.connect(dest) as bck:
            con.backup(bck)
        return dest
    except Exception:
        return None       # a failed backup must not block the user's run


def _migrate(con: sqlite3.Connection) -> None:
    # A brand-new database needs every migration, so a first-time user's very
    # first command announced "backed up before migrating" and wrote a snapshot
    # of an empty file. Alarming, and about nothing. Back up only when there are
    # rows that a bad migration could actually destroy.
    try:
        has_rows = con.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        has_rows = False
    if has_rows and _needs_migration(con) and DB_PATH.exists():
        b = _backup_before_migration(con)
        if b:
            print(f"  (database backed up to {b.name} before migrating)")
    for table, col, decl in _MIGRATIONS:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError as e:
                # Two first runs can both pass the PRAGMA check and both ALTER.
                # The loser is not an error; the column exists either way.
                if "duplicate column" not in str(e).lower():
                    raise
    # A pre-2026-08-05 row's uid was sha256(company|normalized_title), which is
    # exactly what group_key is now. Backfilling it lets upsert recognise an
    # existing posting under the new uid scheme instead of treating all of them
    # as brand new, which would erase every first_seen date and detach any
    # applied/rejected status the user had recorded.
    # A row that predates the key change is exactly one whose group_key was
    # never set. Stamp those, and only those, as adoption-eligible.
    con.execute("UPDATE jobs SET group_key = uid, schema_v = 1 WHERE group_key IS NULL")
    # Repair for databases migrated by the first 2026-08-05 build, which
    # backfilled group_key for every row before schema_v existed. Legacy rows
    # that had not yet been re-sighted were left marked as already-migrated and
    # could never adopt, so the next sighting of the same requisition would
    # insert a fresh row and strand the user's applied status on the old one.
    # A row whose uid still equals its group_key, from an employer ATS, is
    # exactly that case. Idempotent: adoption sets schema_v=2 and changes uid.
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    already = con.execute("SELECT 1 FROM meta WHERE key='legacy_repair'").fetchone()
    cols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
    if not already and "source" in cols:
        # Runs EXACTLY ONCE per database. It ran on every connect before, and a
        # date bound cannot separate the two cases, because a legacy row written
        # by the old build and a fresh row written by the new one share today's
        # date. Left unbounded it stamps a fresh-install ATS row that merely has
        # an empty external_id, and a later distinct requisition then adopts and
        # hijacks its history.
        marks = ",".join("?" * len(ATS_SOURCES))
        con.execute(f"UPDATE jobs SET schema_v = 1 WHERE uid = group_key AND schema_v = 2 "
                    f"AND source IN ({marks})", tuple(sorted(ATS_SOURCES)))
    con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('legacy_repair', ?)",
                (date.today().isoformat(),))
    con.commit()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    # A pull writes for minutes. Under the default rollback journal an
    # interruption mid-run (closed laptop, killed terminal) can leave the
    # database needing recovery, and the file holds months of first_seen dates
    # and application status that no job board can re-derive. WAL also lets
    # `status` read while a pull is writing. Best-effort: a filesystem that
    # cannot do WAL (some network mounts) keeps the default rather than failing.
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError:
        pass
    con.executescript(SCHEMA)
    _migrate(con)
    con.executescript(INDEXES)
    return con


class RunLock:
    """One mutating run at a time, per database.

    A pull takes minutes and writes throughout. Two of them (a scheduled run and
    the user asking Claude to search) interleaved: both read the same rows, both
    counted misses against them, and reconcile() retired postings that the other
    run had just re-sighted. SQLite's own locking serializes individual writes,
    which is not the same as serializing a run.

    Advisory and best-effort by design. If the lock file cannot be created the
    run proceeds rather than refusing to work."""

    def __init__(self, path: Path | None = None):
        self.path = path or DB_PATH.with_suffix(".lock")
        self._fh = None

    def __enter__(self):
        import fcntl
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "w")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.write(str(os.getpid()))
            self._fh.flush()
        except BlockingIOError:
            if self._fh:
                self._fh.close()
                self._fh = None
            raise RuntimeError(
                f"another CareerKit run is already writing to {DB_PATH.name}. "
                f"Wait for it to finish, or remove {self.path} if no run is active.")
        except (OSError, ImportError):
            self._fh = None      # unlockable filesystem: proceed unprotected
        return self

    def __exit__(self, *exc):
        if self._fh:
            import fcntl
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        return False


def upsert(con: sqlite3.Connection, jobs: list[Job],
           run_id: int | None = None) -> tuple[list[Job], list[Job]]:
    """Insert or refresh. Returns (new_jobs, seen_again)."""
    today = date.today().isoformat()
    new, again = [], []
    # A legacy row can be adopted by exactly one requisition, and the batch
    # order decides which. Put the req whose URL matches the stored row first,
    # so the user's "applied" status follows the job they actually applied to
    # rather than whichever one the board happened to list first.
    legacy_urls = {r["group_key"]: r["url"] for r in
                   con.execute("SELECT group_key, url FROM jobs WHERE schema_v=1")}
    if legacy_urls:
        jobs = sorted(jobs, key=lambda j: 0 if legacy_urls.get(j.group_key) == j.url else 1)
    with closing(con.cursor()) as cur:
        for j in jobs:
            cur.execute("SELECT uid, status FROM jobs WHERE uid=?", (j.uid,))
            row = cur.fetchone()
            if row is None and j.uid != j.group_key:
                # No row under the new uid. Look for a LEGACY row in this group
                # - one written before the key change, marked schema_v=1 by the
                # migration - and adopt it, so its first_seen, status and notes
                # carry forward.
                #
                # Two constraints, both from the 2026-08-05 red team:
                #   - Only schema_v=1 rows are eligible. Matching on
                #     "uid == group_key" also matched MODERN aggregator rows,
                #     whose uid is legitimately the bare group_key, so an ATS
                #     requisition could hijack a live aggregator row and leave a
                #     permanent duplicate behind.
                #   - When several requisitions share a group, prefer the one
                #     whose URL matches the stored row. Otherwise whichever the
                #     board happened to list first inherits the user's "applied"
                #     status, attaching it to a job they never applied to while
                #     the one they did apply to reappears as NEW.
                cur.execute("SELECT uid, url FROM jobs WHERE group_key=? AND schema_v=1 "
                            "LIMIT 1", (j.group_key,))
                legacy = cur.fetchone()
                if legacy:
                    try:
                        cur.execute("UPDATE jobs SET uid=?, schema_v=2 WHERE uid=?",
                                    (j.uid, legacy["uid"]))
                        cur.execute("UPDATE OR REPLACE sightings SET uid=? WHERE uid=?",
                                    (j.uid, legacy["uid"]))
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
                    "source=?, board=COALESCE(NULLIF(?,''),board), group_key=?, "
                    "last_seen_run=COALESCE(?,last_seen_run), "
                    "delisted_on=NULL, misses=0, miss_on=NULL WHERE uid=?",
                    (today, j.score, j.gate, " | ".join(j.reasons),
                     j.comp_min, j.comp_max, j.url, j.title, j.location,
                     j.description[:20000], j.source, j.board, j.group_key, run_id, j.uid),
                )
                again.append(j)
            else:
                cur.execute(
                    "INSERT INTO jobs (uid,group_key,board,company,title,url,location,source,lane,"
                    "employer_tier,posted_at,department,comp_min,comp_max,comp_text,"
                    "score,gate,reasons,description,first_seen,last_seen,first_seen_run,last_seen_run) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (j.uid, j.group_key, j.board, j.company, j.title, j.url, j.location, j.source,
                     j.lane, j.employer_tier, j.posted_at, j.department, j.comp_min,
                     j.comp_max, j.comp_text, j.score, j.gate, " | ".join(j.reasons),
                     j.description[:20000], today, today, run_id, run_id),
                )
                new.append(j)
            cur.execute(
                "INSERT OR REPLACE INTO sightings (uid,source,url,seen_on) VALUES (?,?,?,?)",
                (j.uid, j.source, j.url, today),
            )
    con.commit()
    return new, again


VALID_STATUS = ("new", "reviewed", "applied", "rejected", "ignored")


def reconcile(con: sqlite3.Connection, demoted: dict[str, tuple[int, str, str]],
              healthy_boards: set[tuple[str, str]],
              healthy_feeds: set[str],
              known_boards: set[tuple[str, str]] | None = None) -> tuple[int, int]:
    """Close the loop after a run. Returns (delisted, demoted).

    Two holes this fills, both of which surfaced dead or wrong rows as live:

    1. cmd_pull only upserts QUALIFIED/VERIFY, so a posting that stops
       qualifying (the user tightened their profile, or the board edited the
       body) was never written back and kept surfacing at its old gate forever.
    2. A posting removed from its board is simply absent from the next pull.
       Nothing ever marked it closed, so it stayed QUALIFIED indefinitely and
       the report presented it as live. This is the defect that put a dead
       requisition in front of the user in July 2026.

    A row is only delisted when THE BOARD IT CAME FROM reported successfully
    this run. Health must be tracked per board, not per ATS platform: dozens of
    employers share the "greenhouse" platform, so marking the platform healthy
    because one company answered would retire the jobs of every company that
    404'd, and `pull --tier A` would retire everything from the tiers it did
    not poll. Feeds have no per-employer identity, so they match on source
    alone; employer boards match on source AND company.
    """
    today = date.today().isoformat()
    dem = 0
    with closing(con.cursor()) as cur:
        for uid, (score, gate, reasons) in demoted.items():
            cur.execute("UPDATE jobs SET score=?, gate=?, reasons=?, last_seen=?, "
                        "delisted_on=NULL WHERE uid=?", (score, gate, reasons, today, uid))
            dem += cur.rowcount
        if not healthy_boards and not healthy_feeds:
            con.commit()
            return 0, dem
        # Two matching paths, deliberately. `board` is the stable id
        # (platform:slug) and survives an employer being renamed in the
        # registry. Rows written before that column existed carry none, so they
        # still match on (source, company); dropping that path would freeze
        # every pre-existing row as permanently un-retirable.
        #
        # healthy_boards entries are (source, company) or (source, company,
        # board_id). Normalise once here rather than branching below.
        norm = [(t[0], t[1], (t[2] if len(t) > 2 else "")) for t in healthy_boards]
        clauses, params = [], []
        board_ids = sorted({b for _, _, b in norm if b})
        if board_ids:
            clauses.append("board IN (%s)" % ",".join("?" * len(board_ids)))
            params += board_ids
        pairs = sorted({f"{src}\x00{co}" for src, co, _ in norm})
        if pairs:
            clauses.append("((board IS NULL OR board = '') AND "
                           "(source || x'00' || company) IN (%s))"
                           % ",".join("?" * len(pairs)))
            params += pairs
        if healthy_feeds:
            # A feed may namespace its rows ("jobspy:indeed") while the registry
            # knows it as "jobspy", so compare the part before the colon.
            # Without this those rows never accumulate a miss and dead postings
            # pile up silently.
            clauses.append(
                "(CASE WHEN instr(source, ':') > 0 "
                "THEN substr(source, 1, instr(source, ':') - 1) ELSE source END) IN (%s)"
                % ",".join("?" * len(healthy_feeds)))
            params += sorted(healthy_feeds)
        # Orphaned employer rows. Renaming a company in the registry changes the
        # board identity, so the rows written under the OLD name match no
        # healthy board and would never be retired - they would accumulate as
        # permanently live jobs, the exact inverse of the bug this key fixed.
        # A row from an employer ATS whose board is in no registry entry at all
        # can never be re-sighted, so it is eligible. Aggregator rows are
        # excluded: their company is the employer named by the feed and is not
        # expected in the registry.
        if known_boards is not None:
            known = [f"{a}\x00{b}" for a, b in known_boards]
            ats = sorted(ATS_SOURCES)
            orphan = ("(source IN (%s) AND (source || x'00' || company) NOT IN (%s))"
                      % (",".join("?" * len(ats)), ",".join("?" * len(known)) or "''"))
            clauses.append(orphan)
            params += ats + known
        # Count the miss first; only retire on the SECOND consecutive one.
        # Board counts are stable run to run (15,879 / 15,932 / 15,935 across
        # three real runs), so a single absence is decent evidence - but not
        # good enough. A partial or transient response would retire live jobs,
        # and hiding a live job is the exact failure this tool exists to
        # prevent, whereas showing a dead one for one more run is cheap.
        # At most ONE miss per calendar day. "Two consecutive misses" is meant
        # to mean two days of absence; without this, running pull twice in one
        # afternoon retires everything that happened to be absent from both.
        cur.execute(
            "UPDATE jobs SET misses = misses + 1, miss_on = ? WHERE delisted_on IS NULL "
            "AND last_seen < ? AND COALESCE(miss_on,'') <> ? "
            "AND status NOT IN ('applied','rejected','ignored') "
            "AND (" + " OR ".join(clauses) + ")",
            (today, today, today, *params),
        )
        cur.execute(
            "UPDATE jobs SET delisted_on=? WHERE delisted_on IS NULL AND misses >= 2 "
            "AND status NOT IN ('applied','rejected','ignored')",
            (today,),
        )
        delisted = cur.rowcount
    con.commit()
    return delisted, dem


def dropped_to_zero(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Sources that returned nothing this run but had postings last run.

    Reporting an empty result as healthy stopped the false alarms, but it also
    made a real failure mode silent: when a board changes its JSON shape the
    fetch still returns 200, the adapter maps nothing, and "0 jobs, no error"
    is indistinguishable from "no openings today". Comparing against the
    previous count catches exactly that, and never fires for a board that is
    simply always empty."""
    return list(con.execute(
        "SELECT source, prev_count, last_ok FROM source_health "
        "WHERE last_count = 0 AND COALESCE(prev_count,0) > 0 AND last_error IS NULL "
        "ORDER BY prev_count DESC"))


def record_health(con: sqlite3.Connection, source: str, count: int, error: str | None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with closing(con.cursor()) as cur:
        cur.execute("UPDATE source_health SET prev_count = last_count WHERE source = ?", (source,))
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


def is_new_this_run(row, run_id: int | None) -> bool:
    """New means first seen in THIS run, not merely first seen today.

    first_seen == last_seen made a row inserted an hour ago still read NEW, and
    made two runs on one day indistinguishable."""
    if run_id is not None and row["first_seen_run"] is not None:
        return row["first_seen_run"] == run_id
    return row["first_seen"] == row["last_seen"]      # pre-run-id rows


def set_status(con: sqlite3.Connection, uid: str, status: str, notes: str = "") -> None:
    """Validated on purpose. Any string used to be accepted, but query() only
    understands five, so a typo ('apllied') silently left the job in the active
    list and the user believed it was filed."""
    if status not in VALID_STATUS:
        raise ValueError(f"unknown status {status!r}. Use one of: {', '.join(VALID_STATUS)}")
    prev = con.execute("SELECT status FROM jobs WHERE uid=?", (uid,)).fetchone()
    cur = con.execute(
        "UPDATE jobs SET status=?, notes=COALESCE(NULLIF(?,''),notes) WHERE uid=?",
        (status, notes, uid))
    if cur.rowcount == 0:
        con.rollback()
        raise KeyError(f"no posting with uid {uid!r} (check the id in the report)")
    # The row holds only the CURRENT status, so every previous state was
    # overwritten: the date you applied, when it was rejected, and how long the
    # gap was could not be recovered afterwards. The job search is a process and
    # the interesting questions are all about its history.
    if prev is None or prev["status"] != status:
        con.execute("INSERT INTO events (uid, at, kind, detail) VALUES (?,?,?,?)",
                    (uid, datetime.now().isoformat(timespec="seconds"),
                     f"status:{status}", notes or ""))
    con.commit()


def stats(con: sqlite3.Connection) -> dict:
    g = {r["gate"]: r["n"] for r in con.execute("SELECT gate, COUNT(*) n FROM jobs GROUP BY gate")}
    s = {r["source"]: r["n"] for r in
         con.execute("SELECT source, COUNT(*) n FROM jobs GROUP BY source ORDER BY n DESC")}
    total = con.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    runs = con.execute("SELECT COUNT(*) n FROM runs WHERE finished IS NOT NULL").fetchone()["n"]
    return {"total": total, "by_gate": g, "by_source": s, "runs": runs}


def log_event(con: sqlite3.Connection, uid: str, kind: str, detail: str = "") -> None:
    """Record something that happened to one posting (a reply, a screen, an
    interview). Free-form on purpose: the workflows that call it know more about
    the shape of a job search than this table should."""
    con.execute("INSERT INTO events (uid, at, kind, detail) VALUES (?,?,?,?)",
                (uid, datetime.now().isoformat(timespec="seconds"), kind, detail))
    con.commit()


def history(con: sqlite3.Connection, uid: str | None = None, limit: int = 200) -> list:
    q = ("SELECT e.*, j.company, j.title FROM events e "
         "LEFT JOIN jobs j ON j.uid = e.uid ")
    if uid:
        return list(con.execute(q + "WHERE e.uid=? ORDER BY e.at DESC LIMIT ?", (uid, limit)))
    return list(con.execute(q + "ORDER BY e.at DESC LIMIT ?", (limit,)))
