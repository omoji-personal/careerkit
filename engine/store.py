"""SQLite persistence.

The point of persisting is `first_seen`. Without it every run re-reports the
same postings and there is no way to answer the only question that matters on a
daily run: what is NEW since last time. It also gives dedupe across sources
(the same role arrives via the ATS, an aggregator, and a repost) while keeping
provenance for each sighting.
"""
from __future__ import annotations

import errno
import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from .models import Job, ATS_SOURCES, _norm_company

DB_PATH = Path(os.environ.get("CAREERKIT_HOME") or Path(__file__).resolve().parent.parent) / "data" / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  uid            TEXT PRIMARY KEY,
  group_key      TEXT,
  board          TEXT,
  company        TEXT, title TEXT, url TEXT, location TEXT,
  source         TEXT, lane TEXT, employer_tier TEXT,
  posted_at      TEXT, department TEXT,
  comp_min       INTEGER, comp_max INTEGER, comp_text TEXT, comp_source TEXT,
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
CREATE TABLE IF NOT EXISTS employer_history (
  history_id INTEGER PRIMARY KEY AUTOINCREMENT,
  company TEXT, company_key TEXT, at TEXT, kind TEXT,
  contact TEXT DEFAULT '', contact_email TEXT DEFAULT '', detail TEXT DEFAULT ''
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
CREATE INDEX IF NOT EXISTS idx_employer_history_key ON employer_history(company_key, at);
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
    # The board's own remote claim. Twelve adapters and feeds set it and
    # location_verdict trusts it, but it was never a column, so rescore rebuilt
    # every Job with remote_flag=None and re-judged it without that evidence. A
    # remote-board posting whose location string is just "USA" passed at pull and
    # became VERIFY on the next rescore, with no change to the profile and no way
    # to tell from the report that the run had lost information.
    ("jobs", "remote_flag", "INTEGER"),
    # The employer-registry carve-out ("judge this employer on fit, not on the
    # mechanical rails"). score() re-derives it for anyone named in the profile's
    # dream_companies, which hid the gap: an exemption that came from
    # employers.yaml was lost on rescore and the role went QUALIFIED to EXCLUDED.
    ("jobs", "rails_exempt", "INTEGER"),
    # The employer's lane AS THE REGISTRY STATES IT. `lane` cannot serve this
    # purpose: score() overwrites it with the lane key the title matched, so the
    # registry value was gone after the first scoring pass. lane_title_context is
    # keyed on it, which meant rescore stopped injecting the implicit company
    # context and demoted every such posting to a body-only fit. Seen live
    # 2026-08-10: five Salesforce reqs fell QUALIFIED 73 -> VERIFY 31 on a rescore
    # with no profile change, all reading "fit only in body, not title".
    ("jobs", "registry_lane", "TEXT"),
    # Evidence for the ghost-listing score. JobSpy resolves both and the feed
    # was discarding them, which left the check with no data source.
    ("jobs", "url_direct", "TEXT"),
    ("jobs", "company_site", "TEXT"),
    # Confidence/provenance for the displayed compensation: an employer board
    # field is not the same claim as a number parsed from free-form body text.
    # Existing figures cannot be reconstructed reliably, so migrated rows are
    # marked unknown until a fresh sighting supplies authoritative provenance.
    ("jobs", "comp_source", "TEXT"),
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
    # The old schema retained the numbers but not enough evidence to reconstruct
    # whether they came from a board field or prose.  Mark that uncertainty
    # explicitly; calling every migrated figure "board" would invent confidence.
    job_cols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
    if {"comp_min", "comp_max", "comp_source"}.issubset(job_cols):
        con.execute("UPDATE jobs SET comp_source='unknown' "
                    "WHERE (comp_min IS NOT NULL OR comp_max IS NOT NULL) "
                    "AND COALESCE(comp_source,'')='' ")
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

    Fails closed. If the lock cannot be established, starting a second writer is
    more dangerous than refusing the run: SQLite serializes statements, not the
    multi-minute reconcile transaction this lock protects."""

    def __init__(self, path: Path | None = None):
        self.path = path or DB_PATH.with_suffix(".lock")
        self._fh = None
        self._unlock = None

    def _acquire_platform_lock(self) -> None:
        """Acquire a nonblocking native lock, POSIX first and Windows second."""
        try:
            import fcntl
        except ImportError:
            try:
                import msvcrt
            except ImportError as windows_error:
                raise ImportError(
                    "neither fcntl nor msvcrt file locking is available"
                ) from windows_error

            # msvcrt locks a byte range from the current file position.  A new
            # zero-length file needs one byte to lock; never write this sentinel
            # into a nonempty file, which may contain an active owner's PID.
            self._fh.seek(0, os.SEEK_END)
            if self._fh.tell() == 0:
                self._fh.write("\0")
                self._fh.flush()
            self._fh.seek(0)
            try:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                # CPython commonly maps ERROR_LOCK_VIOLATION to EACCES, but
                # retain the Windows codes as well so contention consistently
                # reaches the ordinary "already writing" path.
                if (e.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                        or getattr(e, "winerror", None) in {32, 33, 36}):
                    raise BlockingIOError(e.errno or errno.EACCES, str(e)) from e
                raise

            def unlock_windows(fh):
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

            self._unlock = unlock_windows
            return

        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def unlock_posix(fh):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        self._unlock = unlock_posix

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # `w` truncates before flock runs, so a losing contender erased the
            # active owner's PID. Append/update opens without mutating; only the
            # process that acquired the lock may replace the diagnostic content.
            self._fh = open(self.path, "a+", encoding="utf-8")
            self._acquire_platform_lock()
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
        except BlockingIOError:
            if self._fh:
                self._fh.close()
                self._fh = None
            self._unlock = None
            raise RuntimeError(
                f"another CareerKit run is already writing to {DB_PATH.name}. "
                "Wait for that run to finish before trying again.")
        except (OSError, ImportError) as e:
            if self._fh:
                self._fh.close()
                self._fh = None
            self._unlock = None
            raise RuntimeError(
                f"cannot acquire CareerKit write lock at {self.path}: {e}; "
                "refusing to write without serialization") from e
        return self

    def __exit__(self, *exc):
        if self._fh:
            try:
                if self._unlock:
                    self._unlock(self._fh)
            finally:
                self._fh.close()
                self._fh = None
                self._unlock = None
        return False


_STATUS_RANK = {"new": 0, "reviewed": 1, "ignored": 2, "applied": 3, "rejected": 4}


def _merge_duplicate_job_row(cur: sqlite3.Cursor, target_uid: str,
                             old: sqlite3.Row) -> None:
    """Merge one exact duplicate into an existing canonical job row."""
    cur.execute("SELECT * FROM jobs WHERE uid=?", (target_uid,))
    current = cur.fetchone()
    if current is None:
        return
    current_rank = _STATUS_RANK.get(current["status"] or "new", 0)
    old_rank = _STATUS_RANK.get(old["status"] or "new", 0)
    status = old["status"] if old_rank > current_rank else current["status"]
    notes = []
    for value in (current["notes"], old["notes"]):
        value = (value or "").strip()
        if value and value not in notes:
            notes.append(value)
    first_runs = [v for v in (current["first_seen_run"], old["first_seen_run"])
                  if v is not None]
    last_runs = [v for v in (current["last_seen_run"], old["last_seen_run"])
                 if v is not None]
    # The third write path, and the same rule as the other two: a field score()
    # reads has to survive every way a row can be written. This merge used to
    # carry only status, notes, the dates and the counters, so a duplicate that
    # knew the role was remote folded into a row that did not and the knowledge
    # was gone - invisible, because the merged row still looks complete.
    # COALESCE keeps whatever the canonical row already established; the
    # duplicate only fills gaps.
    def _keep(col):
        cur_val, old_val = current[col], old[col] if col in old.keys() else None
        return cur_val if cur_val not in (None, "") else old_val

    def _evidence(col):
        """Prefer found evidence, while preserving looked-vs-not-looked."""
        cur_val = current[col] if col in current.keys() else None
        old_val = old[col] if col in old.keys() else None
        if cur_val not in (None, ""):
            return cur_val
        if old_val not in (None, ""):
            return old_val
        # NULL means this evidence source was never checked; empty means it was
        # checked and nothing was found. Either row having been checked is useful
        # information and must survive the identity repair.
        return "" if cur_val is not None or old_val is not None else None

    current_has_comp = current["comp_min"] is not None or current["comp_max"] is not None
    old_has_comp = old["comp_min"] is not None or old["comp_max"] is not None
    if current_has_comp:
        comp_min, comp_max = current["comp_min"], current["comp_max"]
        comp_text = current["comp_text"]
        comp_source = current["comp_source"] or "unknown"
    elif old_has_comp:
        comp_min, comp_max = old["comp_min"], old["comp_max"]
        comp_text = old["comp_text"]
        comp_source = ((old["comp_source"] or "unknown")
                       if "comp_source" in old.keys() else "unknown")
    else:
        comp_min = comp_max = None
        comp_text = _keep("comp_text")
        comp_source = _keep("comp_source")

    cur.execute(
        "UPDATE jobs SET status=?, notes=?, first_seen=MIN(first_seen,?), "
        "last_seen=MAX(last_seen,?), seen_count=COALESCE(seen_count,0)+?, "
        "first_seen_run=?, last_seen_run=?, remote_flag=?, rails_exempt=?, "
        "employer_tier=?, department=?, comp_min=?, comp_max=?, comp_text=?, "
        "comp_source=?, registry_lane=?, url_direct=?, company_site=? WHERE uid=?",
        (status, " | ".join(notes), old["first_seen"], old["last_seen"],
         old["seen_count"] or 0,
         min(first_runs) if first_runs else None,
         max(last_runs) if last_runs else None,
         _keep("remote_flag"), _keep("rails_exempt"), _keep("employer_tier"),
         _keep("department"), comp_min, comp_max,
         comp_text, comp_source, _keep("registry_lane"),
         _evidence("url_direct"), _evidence("company_site"),
         target_uid),
    )
    for sighting in cur.execute(
            "SELECT source,url,seen_on FROM sightings WHERE uid=?", (old["uid"],)).fetchall():
        cur.execute(
            "INSERT INTO sightings (uid,source,url,seen_on) VALUES (?,?,?,?) "
            "ON CONFLICT(uid,source) DO UPDATE SET url=excluded.url, "
            "seen_on=MAX(sightings.seen_on,excluded.seen_on)",
            (target_uid, sighting["source"], sighting["url"], sighting["seen_on"]),
        )
    cur.execute("DELETE FROM sightings WHERE uid=?", (old["uid"],))
    cur.execute("UPDATE events SET uid=? WHERE uid=?", (target_uid, old["uid"]))
    cur.execute("DELETE FROM jobs WHERE uid=?", (old["uid"],))


def _repair_blank_company_identity(cur: sqlite3.Cursor, job: Job) -> None:
    """Adopt the exact legacy row after blank-company UIDs were disambiguated.

    Older aggregator rows with no employer name used title alone as both UID
    and group key, so unrelated anonymous "Product Manager" postings collapsed.
    The model now adds source + normalized URL to that identity.  Preserve the
    old row's application state and history only when its raw source and URL
    exactly match this fresh sighting; title-only matching would repeat the bug
    this migration repairs.
    """
    legacy_uid = job.legacy_blank_company_uid
    legacy_group = job.legacy_blank_company_group_key
    if not legacy_uid or not legacy_group or legacy_uid == job.uid:
        return
    cur.execute(
        "SELECT * FROM jobs WHERE uid=? AND group_key=? AND source=? AND url=?",
        (legacy_uid, legacy_group, job.source, job.url),
    )
    old = cur.fetchone()
    if old is None:
        return

    cur.execute("SELECT * FROM jobs WHERE uid=?", (job.uid,))
    current = cur.fetchone()
    if current is None:
        try:
            cur.execute(
                "UPDATE jobs SET uid=?, group_key=?, schema_v=2 WHERE uid=?",
                (job.uid, job.group_key, old["uid"]),
            )
            cur.execute("UPDATE sightings SET uid=? WHERE uid=?",
                        (job.uid, old["uid"]))
            cur.execute("UPDATE events SET uid=? WHERE uid=?",
                        (job.uid, old["uid"]))
            return
        except sqlite3.IntegrityError:
            # A canonical row or sighting may already have appeared between the
            # reads.  Resolve through the same lossless merge used by the other
            # identity migrations.
            cur.execute("SELECT * FROM jobs WHERE uid=?", (job.uid,))
            current = cur.fetchone()

    if current is not None:
        _merge_duplicate_job_row(cur, job.uid, old)


def _repair_same_url_identity(cur: sqlite3.Cursor, job: Job) -> None:
    """Collapse rows stranded by an earlier ATS UID algorithm.

    The 2026-08-06 identity fix deliberately started preserving meaningful
    parenthesized title text. A requisition first seen under the older title
    normalizer therefore got a new UID even though its employer, source,
    group, and exact posting URL were unchanged. If the old row carried an
    applied/rejected status, the fresh duplicate resurfaced as NEW.

    Exact URL + source + group is a much stronger identity statement than a
    title comparison. Adopt the old row when the new UID does not exist; if
    both already exist, merge history into the current UID without losing the
    strongest human-set status or either row's notes.
    """
    # Do not merge merely because two rows share a URL: some ATS platforms use
    # one generic apply URL for several real requisitions with the same title.
    # The historical UID is deterministic from the current job's requisition
    # id, so repair only that exact transition row.
    legacy_uid = job.legacy_external_uid
    if not job.external_id or legacy_uid == job.uid:
        return
    cur.execute(
        "SELECT * FROM jobs WHERE uid=? AND group_key=? AND source=? AND url=?",
        (legacy_uid, job.group_key, job.source, job.url),
    )
    duplicates = cur.fetchall()
    for old in duplicates:
        cur.execute("SELECT * FROM jobs WHERE uid=?", (job.uid,))
        current = cur.fetchone()
        if current is None:
            try:
                cur.execute("UPDATE jobs SET uid=?, schema_v=2 WHERE uid=?",
                            (job.uid, old["uid"]))
                cur.execute("UPDATE sightings SET uid=? WHERE uid=?",
                            (job.uid, old["uid"]))
                cur.execute("UPDATE events SET uid=? WHERE uid=?",
                            (job.uid, old["uid"]))
                continue
            except sqlite3.IntegrityError:
                # Another duplicate may already have claimed the new UID in
                # this loop. Fall through to the explicit merge path.
                cur.execute("SELECT * FROM jobs WHERE uid=?", (job.uid,))
                current = cur.fetchone()

        if current is not None:
            _merge_duplicate_job_row(cur, job.uid, old)


def _remote_sql_args(flag: bool | None, source: str,
                     board: str = "") -> tuple[int | None, int | None, int]:
    """Arguments for the authoritative-ATS/monotonic-aggregator merge.

    An employer ATS saying False is an actual edit to the posting and may clear
    an earlier True. Aggregator adapters also use False for a missing field, so
    they may establish False from NULL but cannot erase known remote evidence.
    """
    raw = None if flag is None else int(bool(flag))
    base_source = (source or "").lower().split(":", 1)[0]
    # A registry-backed board id proves this is the employer adapter path. Source
    # names alone are insufficient because callers and imported aggregator rows
    # can retain an ATS-looking label while carrying False-for-missing semantics.
    authoritative = base_source in ATS_SOURCES and bool((board or "").strip())
    return raw, raw, int(authoritative)


def _resolved_comp(job: Job) -> int:
    """Whether this sighting carries one authoritative compensation band."""
    return int(job.comp_source in {"board", "body", "unknown"}
               and (job.comp_min is not None or job.comp_max is not None))


def _recover_abandoned_newness(cur: sqlite3.Cursor, row: sqlite3.Row,
                               run_id: int | None) -> bool:
    """Move a first sighting out of an interrupted run into the retrying run."""
    first_run = row["first_seen_run"] if "first_seen_run" in row.keys() else None
    if run_id is None or first_run is None or first_run == run_id:
        return False
    prior = cur.execute("SELECT finished, state FROM runs WHERE run_id=?",
                        (first_run,)).fetchone()
    if prior is None or prior["finished"] is not None:
        return False
    # Tests and library callers may open two logical runs in one process without
    # finishing the first. A real retry after process death has a different PID;
    # legacy unfinished rows have no marker and are also safely recoverable.
    return (prior["state"] or "") != f"running:{os.getpid()}"


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
            _repair_blank_company_identity(cur, j)
            _repair_same_url_identity(cur, j)
            cur.execute("SELECT uid, status, first_seen_run FROM jobs WHERE uid=?", (j.uid,))
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
                        cur.execute("SELECT uid, status, first_seen_run FROM jobs WHERE uid=?",
                                    (j.uid,))
                        row = cur.fetchone()
                    except sqlite3.IntegrityError:
                        row = None
            if row:
                recovered_new = _recover_abandoned_newness(cur, row, run_id)
                # url/title/location/description are REFRESHED, not frozen at
                # first sighting. A board that edits a title or re-issues a
                # posting under a new URL used to leave the row pointing at a
                # dead link forever. delisted_on is cleared: seeing it again
                # means it is live again.
                cur.execute(
                    "UPDATE jobs SET last_seen=?, seen_count=seen_count+1, "
                    # Human action turns the verdict into historical evidence:
                    # what the role looked like when it was applied to, rejected,
                    # or ignored. A later pull still refreshes whether the posting
                    # is alive and what the board currently says, but must not
                    # rewrite that recorded decision underneath the user.
                    "score=CASE WHEN status IN ('applied','rejected','ignored') "
                    "THEN score ELSE ? END, "
                    "gate=CASE WHEN status IN ('applied','rejected','ignored') "
                    "THEN gate ELSE ? END, "
                    "reasons=CASE WHEN status IN ('applied','rejected','ignored') "
                    "THEN reasons ELSE ? END, "
                    # A band is one claim, not two independent facts. Fieldwise
                    # COALESCE combined a fresh max-only board value with a stale
                    # old minimum and invented a range no source had published.
                    "comp_min=CASE WHEN ?=1 THEN ? ELSE comp_min END, "
                    "comp_max=CASE WHEN ?=1 THEN ? ELSE comp_max END, "
                    "url=?, title=?, location=?, "
                    "description=COALESCE(NULLIF(?,''),description), "
                    "source=?, board=COALESCE(NULLIF(?,''),board), group_key=?, "
                    "first_seen_run=CASE WHEN ?=1 THEN ? ELSE first_seen_run END, "
                    "last_seen_run=COALESCE(?,last_seen_run), "
                    # Heal rows stored before this column existed, but never let a
                    # later aggregator sighting (which carries no registry lane)
                    # erase the one an ATS sighting established.
                    "registry_lane=COALESCE(NULLIF(?,''),registry_lane), "
                    # Identical reasoning, and the field the comment at the top of
                    # this module already names as the original example - which is
                    # exactly why its absence here went unseen. remote_flag was
                    # written by the INSERT and by nothing else, so every row
                    # predating the column kept NULL however often its board
                    # re-reported it as remote, and each `rescore` re-judged it as
                    # onsite and dropped it on location.
                    #
                    # Aggregator evidence is deliberately ONE-WAY. Several feeds
                    # encode both "not remote" and "field absent" as False, so
                    # they may establish False from NULL but never erase a known
                    # True. A registry-backed employer ATS is authoritative in the
                    # other direction: its explicit True -> False edit must clear
                    # stale state or rescore will keep treating an onsite role as
                    # remote. The third argument below selects that trusted path.
                    "remote_flag=CASE WHEN ? IS NULL THEN remote_flag "
                    "WHEN ? = 1 THEN 1 "
                    "WHEN ? = 1 THEN 0 "
                    "WHEN remote_flag IS NULL THEN 0 ELSE remote_flag END, "
                    # The same omission, three more times over. Every one of
                    # these is read by score(), so leaving it out of the UPDATE
                    # makes `pull` and `rescore` disagree on a row nobody
                    # touched: employer_tier is worth +6 (A) or +3 (B) and can
                    # cross a company floor on its own; rails_exempt waives the
                    # location rails entirely; department is part of Job.text and
                    # so feeds the domain rail; comp_text is what the report
                    # quotes back. NULLIF so a sighting that carries nothing
                    # cannot blank what another one established.
                    "employer_tier=COALESCE(NULLIF(?,''),employer_tier), "
                    "department=COALESCE(NULLIF(?,''),department), "
                    "comp_text=CASE WHEN ?=1 THEN ? "
                    "ELSE COALESCE(NULLIF(?,''),comp_text) END, "
                    # A fresh resolved band replaces the value above, so its
                    # provenance must travel with it.  `absent` never erases a
                    # source already established by an earlier sighting.
                    "comp_source=CASE WHEN ?=1 THEN ? "
                    "WHEN comp_min IS NULL AND comp_max IS NULL AND ?='absent' "
                    "THEN 'absent' ELSE comp_source END, "
                    "rails_exempt=COALESCE(?,rails_exempt), "
                    # Evidence is tri-state: NULL means never checked, empty means
                    # checked with no result, non-empty is a resolved employer URL.
                    # A blank sighting may advance NULL -> empty, but never erases a
                    # URL another sighting already resolved.
                    "url_direct=CASE WHEN ? IS NULL THEN url_direct "
                    "WHEN ? <> '' THEN ? WHEN url_direct IS NULL THEN '' "
                    "ELSE url_direct END, "
                    "company_site=CASE WHEN ? IS NULL THEN company_site "
                    "WHEN ? <> '' THEN ? WHEN company_site IS NULL THEN '' "
                    "ELSE company_site END, "
                    "delisted_on=NULL, misses=0, miss_on=NULL WHERE uid=?",
                    (today, j.score, j.gate, " | ".join(j.reasons),
                     _resolved_comp(j), j.comp_min, _resolved_comp(j), j.comp_max,
                     j.url, j.title, j.location,
                     j.description[:DESCRIPTION_LIMIT], j.source, j.board, j.group_key,
                     int(recovered_new), run_id, run_id,
                     j.registry_lane,
                     *_remote_sql_args(j.remote_flag, j.source, j.board),
                     j.employer_tier, j.department,
                     _resolved_comp(j), j.comp_text, j.comp_text,
                     _resolved_comp(j), j.comp_source, j.comp_source,
                     None if j.rails_exempt is None else int(j.rails_exempt),
                     j.url_direct, j.url_direct, j.url_direct,
                     j.company_site, j.company_site, j.company_site,
                     j.uid),
                )
                (new if recovered_new else again).append(j)
            else:
                cur.execute(
                    "INSERT INTO jobs (uid,group_key,board,company,title,url,location,source,lane,"
                    "employer_tier,posted_at,department,comp_min,comp_max,comp_text,comp_source,"
                    "score,gate,reasons,description,first_seen,last_seen,first_seen_run,last_seen_run,"
                    "remote_flag,rails_exempt,url_direct,company_site,registry_lane) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (j.uid, j.group_key, j.board, j.company, j.title, j.url, j.location, j.source,
                     j.lane, j.employer_tier, j.posted_at, j.department, j.comp_min,
                     j.comp_max, j.comp_text, j.comp_source, j.score, j.gate,
                     " | ".join(j.reasons),
                     j.description[:DESCRIPTION_LIMIT], today, today, run_id, run_id,
                     None if j.remote_flag is None else int(j.remote_flag),
                     int(bool(j.rails_exempt)), j.url_direct, j.company_site,
                     j.registry_lane),
                )
                new.append(j)
            cur.execute(
                "INSERT OR REPLACE INTO sightings (uid,source,url,seen_on) VALUES (?,?,?,?)",
                (j.uid, j.source, j.url, today),
            )
    con.commit()
    return new, again


VALID_STATUS = ("new", "reviewed", "applied", "rejected", "ignored")


DESCRIPTION_LIMIT = 20000
"""Longest posting body kept. Applied by pull BEFORE scoring, not only here:
truncating at storage while score() read the full fetched text meant a comp band
or signal phrase past this offset was counted on the pull and gone on the
rescore. Truncate once, judge what you keep."""


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
        for uid, dj in demoted.items():
            # Accepts a Job. A demoted sighting was still REPORTED by its board,
            # so it carries the same fresh detail a surfaced one does - but it
            # never passes through upsert (pull filters it out of `keep`), so
            # every field heal there missed it and the row's scorer inputs stayed
            # frozen. That is a rescore waiting to disagree with a pull, and the
            # company floor makes it the ordinary case rather than a rare one.
            score, gate, reasons = dj.score, dj.gate, " | ".join(dj.reasons or [])
            # misses/miss_on are cleared for the same reason upsert clears them:
            # a demoted row WAS reported by its board this run, it just scored
            # below a gate. Leaving the counter standing turned "two consecutive
            # misses" into "two misses ever", so a single later absence delisted
            # a live posting. The company floor makes this the ordinary path
            # rather than a rare one - every floored row is re-sighted and
            # demoted on every pull.
            # Same status guard as the two statements below, and for the same
            # reason: you do not rewrite the record of a posting the user acted
            # on. Without it a criteria change edits history - pausing two
            # employers left two REAL submitted applications reading EXCLUDED
            # with score 0, and a company floor left a rejected req reading
            # SLOT-BLOCKED. stats() counts by gate, so an application the user
            # made is then tallied as a role that never qualified.
            cur.execute(
                        "UPDATE jobs SET "
                        "score=CASE WHEN status IN ('applied','rejected','ignored') "
                        "THEN score ELSE ? END, "
                        "gate=CASE WHEN status IN ('applied','rejected','ignored') "
                        "THEN gate ELSE ? END, "
                        "reasons=CASE WHEN status IN ('applied','rejected','ignored') "
                        "THEN reasons ELSE ? END, last_seen=?, "
                        "delisted_on=NULL, misses=0, miss_on=NULL, "
                        # A demotion IS a sighting. Without this, last_seen
                        # advanced on every pull while seen_count stood still,
                        # and provenance - which `audit` and the ghost-listing
                        # check both read - froze at the pre-demotion state.
                        "seen_count=COALESCE(seen_count,0)+1, "
                        "employer_tier=COALESCE(NULLIF(?,''),employer_tier), "
                        "department=COALESCE(NULLIF(?,''),department), "
                        # Demoted sightings never pass through upsert, but the
                        # scorer may have resolved a fresh band before a company
                        # floor moved the role to SLOT-BLOCKED.  Persist the
                        # inputs used for that verdict or the next rescore reads
                        # an older posting than the row claims to describe.
                        "comp_min=CASE WHEN ?=1 THEN ? ELSE comp_min END, "
                        "comp_max=CASE WHEN ?=1 THEN ? ELSE comp_max END, "
                        "comp_text=CASE WHEN ?=1 THEN ? "
                        "ELSE COALESCE(NULLIF(?,''),comp_text) END, "
                        "comp_source=CASE WHEN ?=1 THEN ? "
                        "WHEN comp_min IS NULL AND comp_max IS NULL AND ?='absent' "
                        "THEN 'absent' ELSE comp_source END, "
                        "location=COALESCE(NULLIF(?,''),location), "
                        # The BODY is a scorer input too - lanes fall back to
                        # matching it when the title does not - so leaving it out
                        # of this refresh stored a verdict computed from the new
                        # posting beside the old text, and the next rescore
                        # re-judged from that old text and disagreed. Five rows
                        # drifted this way on 2026-08-12. (The title cannot drift:
                        # it is part of the uid, so a retitled posting is a
                        # different row.)
                        "description=COALESCE(NULLIF(?,''),description), "
                        "url=COALESCE(NULLIF(?,''),url), "
                        "registry_lane=COALESCE(NULLIF(?,''),registry_lane), "
                        "rails_exempt=COALESCE(?,rails_exempt), "
                        "remote_flag=CASE WHEN ? IS NULL THEN remote_flag "
                        "WHEN ? = 1 THEN 1 "
                        "WHEN ? = 1 THEN 0 "
                        "WHEN remote_flag IS NULL THEN 0 ELSE remote_flag END, "
                        "url_direct=CASE WHEN ? IS NULL THEN url_direct "
                        "WHEN ? <> '' THEN ? WHEN url_direct IS NULL THEN '' "
                        "ELSE url_direct END, "
                        "company_site=CASE WHEN ? IS NULL THEN company_site "
                        "WHEN ? <> '' THEN ? WHEN company_site IS NULL THEN '' "
                        "ELSE company_site END WHERE uid=?",
                        (score, gate, reasons, today,
                         dj.employer_tier, dj.department,
                         _resolved_comp(dj), dj.comp_min,
                         _resolved_comp(dj), dj.comp_max,
                         _resolved_comp(dj), dj.comp_text, dj.comp_text,
                         _resolved_comp(dj), dj.comp_source, dj.comp_source,
                         dj.location,
                         (dj.description or "")[:DESCRIPTION_LIMIT], dj.url,
                         dj.registry_lane,
                         None if dj.rails_exempt is None else int(dj.rails_exempt),
                         *_remote_sql_args(dj.remote_flag, dj.source, dj.board),
                         dj.url_direct, dj.url_direct, dj.url_direct,
                         dj.company_site, dj.company_site, dj.company_site,
                         uid))
            updated = cur.rowcount
            if updated and dj.source:
                cur.execute("INSERT OR REPLACE INTO sightings (uid, source, url, seen_on) "
                            "VALUES (?,?,?,?)", (uid, dj.source, dj.url, today))
            dem += updated
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
            # NOT 'ignored': that means "not interested now", and a hidden posting
            # can still die. applied/rejected ARE history and stay guarded.
            "AND status NOT IN ('applied','rejected') "
            "AND (" + " OR ".join(clauses) + ")",
            (today, today, today, *params),
        )
        cur.execute(
            "UPDATE jobs SET delisted_on=? WHERE delisted_on IS NULL AND misses >= 2 "
            "AND status NOT IN ('applied','rejected')",
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
    cur = con.execute("INSERT INTO runs (started, state) VALUES (?,?)",
                      (datetime.now().isoformat(timespec="seconds"),
                       f"running:{os.getpid()}"))
    con.commit()
    return cur.lastrowid


def finish_run(con: sqlite3.Connection, run_id: int, pulled: int, new: int,
               qualified: int, detail: dict) -> None:
    con.execute(
        "UPDATE runs SET finished=?, pulled=?, new=?, qualified=?, detail=?, state='finished' "
        "WHERE run_id=?",
        (datetime.now().isoformat(timespec="seconds"), pulled, new, qualified,
         json.dumps(detail)[:60000], run_id),
    )
    con.commit()


def query(con: sqlite3.Connection, *, gates: tuple[str, ...] = ("QUALIFIED", "VERIFY"),
          min_score: int = 0, new_only: bool = False, since: str | None = None,
          limit: int | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT * FROM jobs WHERE gate IN (%s) AND score >= ? AND status NOT IN "
           "('rejected','ignored','applied') AND delisted_on IS NULL"
           % ",".join("?" * len(gates)))
    params: list = [*gates, min_score]
    if new_only:
        sql += " AND first_seen = last_seen"
    if since:
        sql += " AND first_seen >= ?"
        params.append(since)
    sql += " ORDER BY score DESC, first_seen DESC"
    if limit is not None:
        if limit < 0:
            raise ValueError("limit must be non-negative or None")
        sql += " LIMIT ?"
        params.append(limit)
    return list(con.execute(sql, params))


def is_new_this_run(row, run_id: int | None) -> bool:
    """New means first seen in THIS run, not merely first seen today.

    first_seen == last_seen made a row inserted an hour ago still read NEW, and
    made two runs on one day indistinguishable."""
    if run_id is not None and row["first_seen_run"] is not None:
        return row["first_seen_run"] == run_id
    return row["first_seen"] == row["last_seen"]      # pre-run-id rows


def _event_time(value: str | None = None) -> str:
    """Normalise a user-supplied ISO day/datetime for the event trail."""
    if not value:
        return datetime.now().isoformat(timespec="seconds")
    text = str(value).strip()
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(hour=12).isoformat(timespec="seconds")
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(timespec="seconds")
    except ValueError:
        raise ValueError(f"invalid event date {value!r}; use YYYY-MM-DD or an ISO datetime")


def set_status(con: sqlite3.Connection, uid: str, status: str, notes: str = "", *,
               at: str | None = None) -> None:
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
                    (uid, _event_time(at),
                     f"status:{status}", notes or ""))
    con.commit()


APPLICATION_STAGES = ("applied", "interviewing", "offer", "rejected", "withdrawn")
_APPLICATION_DB_STATUS = {
    "applied": "applied",
    "interviewing": "applied",
    "offer": "applied",
    "rejected": "rejected",
    # A withdrawn application must stay suppressed. `applied` is the durable
    # "submitted pipeline" state; the richer current stage lives in events.
    "withdrawn": "applied",
}


def record_application_stage(con: sqlite3.Connection, uid: str, stage: str, *,
                             on: str | None = None, notes: str = "",
                             detail: str | None = None) -> bool:
    """Record a dated pipeline transition and suppress the posting safely.

    Returns True when a new application event was written. Repeating the exact
    command is idempotent, which matters when evidence reconciliation is run on
    the same append-only file every week.
    """
    stage = (stage or "").lower()
    if stage not in APPLICATION_STAGES:
        raise ValueError(f"unknown application stage {stage!r}. Use one of: "
                         f"{', '.join(APPLICATION_STAGES)}")
    event_at = _event_time(on)
    kind = f"application:{stage}"
    event_detail = notes if detail is None else detail
    exists = con.execute(
        "SELECT 1 FROM events WHERE uid=? AND kind=? AND substr(at,1,10)=substr(?,1,10) "
        "AND detail=? LIMIT 1", (uid, kind, event_at, event_detail or "")).fetchone()
    if exists:
        return False
    latest = con.execute(
        "SELECT at FROM events WHERE uid=? AND kind LIKE 'application:%' "
        "ORDER BY at DESC, event_id DESC LIMIT 1", (uid,)).fetchone()
    # Evidence files are often backfilled out of order. Keep every historical
    # event, but only the chronologically newest one may drive the row's CURRENT
    # suppression status. Replaying an old applied entry after a later rejection
    # used to regress rejected -> applied before discovering it was a duplicate.
    if latest is None or event_at >= latest["at"]:
        set_status(con, uid, _APPLICATION_DB_STATUS[stage], notes, at=event_at)
    elif con.execute("SELECT 1 FROM jobs WHERE uid=?", (uid,)).fetchone() is None:
        raise KeyError(f"no posting with uid {uid!r} (check the id in the report)")
    con.execute("INSERT INTO events (uid, at, kind, detail) VALUES (?,?,?,?)",
                (uid, event_at, kind, event_detail or ""))
    con.commit()
    return True


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


RELATIONSHIP_KINDS = ("recruiter", "referral", "contact", "interview",
                      "invitation", "rejection", "note")


def _canonical_history_company(con: sqlite3.Connection, company: str) -> tuple[str, str]:
    """Adopt one unambiguous stored employer name, including common acronyms."""
    key = _norm_company(company)
    stored = [r[0] for r in con.execute("SELECT DISTINCT company FROM jobs WHERE company<>''")]
    exact = [name for name in stored if _norm_company(name) == key]
    if len(exact) == 1:
        return exact[0], key
    compact = "".join(c for c in key if c.isalnum())
    if " " not in key and 3 <= len(compact) <= 8:
        def acronym(name: str) -> str:
            words = [w for w in _norm_company(name).split() if w]
            return "".join(w[0] for w in words)
        aliases = [name for name in stored if acronym(name) == compact]
        if len(aliases) == 1:
            return aliases[0], _norm_company(aliases[0])
    return company, key


def add_relationship(con: sqlite3.Connection, company: str, kind: str, *,
                     on: str | None = None, contact: str = "",
                     contact_email: str = "", detail: str = "") -> bool:
    """Remember employer-level context that is not tied to one requisition."""
    company = (company or "").strip()
    kind = (kind or "").lower()
    if not company:
        raise ValueError("company is required")
    if kind not in RELATIONSHIP_KINDS:
        raise ValueError(f"unknown relationship kind {kind!r}. Use one of: "
                         f"{', '.join(RELATIONSHIP_KINDS)}")
    at = _event_time(on)
    company, key = _canonical_history_company(con, company)
    values = (key, kind, at, contact or "", contact_email or "", detail or "")
    exists = con.execute(
        "SELECT 1 FROM employer_history WHERE company_key=? AND kind=? "
        "AND substr(at,1,10)=substr(?,1,10) AND contact=? AND contact_email=? "
        "AND detail=? LIMIT 1", values).fetchone()
    if exists:
        return False
    con.execute(
        "INSERT INTO employer_history "
        "(company,company_key,at,kind,contact,contact_email,detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (company, key, at, kind, contact or "", contact_email or "", detail or ""))
    con.commit()
    return True


def relationships(con: sqlite3.Connection, company: str | None = None,
                  limit: int = 200) -> list[sqlite3.Row]:
    if company:
        _, key = _canonical_history_company(con, company)
        return list(con.execute(
            "SELECT * FROM employer_history WHERE company_key=? "
            "ORDER BY at DESC, history_id DESC LIMIT ?",
            (key, limit)))
    return list(con.execute(
        "SELECT * FROM employer_history ORDER BY at DESC, history_id DESC LIMIT ?",
        (limit,)))
