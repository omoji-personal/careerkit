"""Assert that the report agrees with the database it was rendered from.

This exists because of a defect that shipped: score() resolved a salary band,
used it to make the gate decision, put it in the reasons string, and never wrote
it to the row. The report then printed "Comp not stated" in a header and
"comp $150,000-$208,000" in the line directly beneath it, and the CSV export sent
an empty comp column for 55 of 418 rows. Nothing crashed. Nothing was flagged.

The general shape of that bug is a value that reaches the reader by one path and
the database by another, so the two can disagree without anyone noticing. Rather
than testing each field forever, this walks a rendered report, pulls every value
back out, and compares it to the row it claims to describe.

It is deliberately dumb. It re-derives what the renderer should have produced
from the stored row and compares strings. If the renderer changes, this has to
change with it, and that is the point: the check is a second opinion, not a
shared helper. A check that imports the thing it is checking cannot fail.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from .models import sanitize_external, sanitize_external_url

# "### 3. Senior Lifecycle Marketing Manager - Chime  (2 open reqs)"
# Company names legitimately contain hyphens. UID, not this display heading, is
# the authoritative row identity; the heading only marks a block boundary.
_HEAD = re.compile(r"^### \d+\. (?P<label>.+)$")
_SCORE = re.compile(r"^- \*\*Score\*\* (?P<score>\d+) \| \*\*(?P<gate>[A-Z-]+)\*\*")
_UID = re.compile(r"^- \*\*UID\*\* `(?P<uid>[^`]+)`$")
_LOCCOMP = re.compile(r"^- \*\*Location\*\* (?P<loc>.*?) \| \*\*Comp\*\* (?P<comp>.*)$")
_SOURCE = re.compile(r"^- \*\*Source\*\* (?P<source>.*?)(?: \| lane: (?P<lane>.*))?$")
_WHY = re.compile(r"^- \*\*Why\*\* (?P<why>.*)$")
_URL_OMITTED = "URL omitted (invalid or unsafe external target)"
_URL = re.compile(r"^- (?P<url>https?://\S+|URL omitted \(invalid or unsafe external target\))$")
_TAIL = re.compile(
    r"^- (?P<score>\d+) · \*\*Title\*\* (?P<title>.*?) · "
    r"\*\*Company\*\* (?P<company>.*?)(?: \(\+\d+ reqs\))? · "
    r"\*\*Check\*\* (?P<reason>.*?) · \*\*UID\*\* `(?P<uid>[^`]+)`$")
_STATE = re.compile(
    r"^<!-- careerkit-report-state-v1: (?P<digest>[0-9a-f]{64}) -->$",
    re.MULTILINE,
)


def report_state_fingerprint(con: sqlite3.Connection) -> str:
    """Hash the logical database state that can change a Markdown report.

    File mtimes cannot distinguish a real commit from SQLite checkpointing WAL
    frames after the report was written.  A digest of the values the renderer
    actually consumes survives that storage-only rewrite while still changing
    when a posting, source-health row, or reported run changes.
    """
    digest = hashlib.sha256()
    queries = (
        (
            "jobs",
            "SELECT uid, group_key, company, title, url, location, source, lane, "
            "comp_min, comp_max, comp_source, score, gate, reasons, status, "
            "first_seen, last_seen, first_seen_run, last_seen_run, delisted_on, "
            "length(COALESCE(description,'')) AS description_length "
            "FROM jobs ORDER BY uid",
        ),
        ("source_health", "SELECT * FROM source_health ORDER BY source"),
        (
            "latest_finished_run",
            "SELECT run_id, detail FROM runs WHERE finished IS NOT NULL "
            "ORDER BY run_id DESC LIMIT 1",
        ),
    )
    # Legacy newness falls back to today's date, so a yesterday report should
    # not claim to be a fresh rendering merely because no row changed overnight.
    digest.update(("report-day\0" + date.today().isoformat() + "\n").encode())
    for label, sql in queries:
        cur = con.execute(sql)
        columns = [item[0] for item in cur.description or ()]
        digest.update(json.dumps([label, columns], separators=(",", ":")).encode())
        digest.update(b"\n")
        for row in cur:
            values = [value.hex() if isinstance(value, bytes) else value for value in row]
            digest.update(json.dumps(values, ensure_ascii=False,
                                     separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _expected_url(row: sqlite3.Row) -> str:
    return sanitize_external_url(row["url"]) or _URL_OMITTED


def _field(value, limit: int) -> str:
    return sanitize_external(value, limit).replace("|", "&#124;")


def _tail_reason(row: sqlite3.Row) -> str:
    reason = row["reasons"] or ""
    marker = "NEEDS CHECK: "
    if marker in reason:
        reason = reason.split(marker, 1)[1].split(" | ", 1)[0]
    else:
        reason = reason.split(" | ", 1)[0]
    return sanitize_external(reason, 70)


def _expected_comp(row: sqlite3.Row) -> str:
    """What the renderer should print for this row's compensation."""
    lo, hi = row["comp_min"], row["comp_max"]
    if lo is not None or hi is not None:
        if lo is None:
            value = f"up to ${hi:,}"
        elif hi is None:
            value = f"${lo:,}+"
        else:
            value = f"${lo:,} - ${hi:,}"
        source = ((row["comp_source"] or "unknown")
                  if "comp_source" in row.keys() else "unknown")
        label = {"board": "board field", "body": "parsed from body",
                 "unknown": "source unknown"}.get(source, "source unknown")
        return f"{value} ({label})"
    return "not stated"


def _blocks(text: str) -> list[dict]:
    """Split a report into per-posting blocks, keeping the raw lines."""
    out, cur = [], None
    for line in text.splitlines():
        m = _HEAD.match(line)
        if m:
            if cur:
                out.append(cur)
            label = re.sub(
                r" ⚠ STALE \(not sighted in 2\+ days - verify live before acting\)$",
                "", m.group("label")).strip()
            label = re.sub(r"  \(\d+ open reqs\)$", "", label).strip()
            cur = {"label": label, "lines": []}
            continue
        # The low-score VERIFY summary is also a level-three heading, but not a
        # posting. End the prior block here so its one-line tail cannot be folded
        # into the last full posting and silently skipped.
        if line.startswith("### "):
            if cur:
                out.append(cur)
            cur = None
            continue
        if cur is not None:
            if line.startswith("## ") or line.startswith("---"):
                out.append(cur)
                cur = None
            else:
                cur["lines"].append(line)
    if cur:
        out.append(cur)
    return out


def _tail_entries(text: str) -> list[dict]:
    """Parse the compact VERIFY tail, which has no level-three job heading."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = _TAIL.match(line)
        if not m:
            continue
        url = ""
        if i + 1 < len(lines):
            shown = lines[i + 1].strip()
            if re.match(r"^https?://\S+$", shown) or shown == _URL_OMITTED:
                url = shown
        out.append({"uid": m.group("uid"), "score": int(m.group("score")),
                    "url": url, "title": m.group("title"),
                    "company": m.group("company"), "reason": m.group("reason")})
    return out


def report_is_stale(con: sqlite3.Connection, report_path: str | Path) -> str | None:
    """Was the report rendered before the last write to the database?

    Without this the checker cries wolf. Marking a posting applied, or running a
    rescore and not regenerating, makes every changed row look like a renderer
    bug. A stale report is a different problem with a different fix, so say which
    one it is.
    """
    path = Path(report_path)
    if not path.exists():
        return None
    # New reports carry a logical-state marker. Prefer it over filesystem
    # timestamps: closing the report process may checkpoint already-rendered
    # WAL frames into jobs.db afterwards, making an unchanged report look old.
    # Archived reports predate the marker, so keep the mtime fallback for them.
    marker = _STATE.search(path.read_text(encoding="utf-8"))
    if marker:
        if marker.group("digest") == report_state_fingerprint(con):
            return None
        return (f"the report is older than the database "
                f"({path.name}); regenerate with `report` before trusting a diff")
    db_file = Path(con.execute("PRAGMA database_list").fetchall()[0][2] or "")
    candidates = [db_file, Path(str(db_file) + "-wal")]
    newest = max((p.stat().st_mtime_ns for p in candidates if p.exists()), default=0)
    # Nanosecond mtimes let us use the actual ordering. A one-second grace period
    # hid the common case: mark a role applied immediately after rendering and
    # the checker still blessed the stale report because both writes were close.
    if newest and path.stat().st_mtime_ns < newest:
        return (f"the report is older than the database "
                f"({path.name}); regenerate with `report` before trusting a diff")
    return None


def check_report(con: sqlite3.Connection, report_path: str | Path) -> list[str]:
    """Return a list of disagreements between a rendered report and the database.

    Empty list means the report tells the truth about every row it shows.
    """
    path = Path(report_path)
    if not path.exists():
        return [f"report not found: {path}"]
    problems: list[str] = []

    text = path.read_text(encoding="utf-8")
    for block in _blocks(text):
        uid = None
        url = None
        for line in block["lines"]:
            m = _UID.match(line.strip())
            if m:
                uid = m.group("uid")
            m = _URL.match(line.strip())
            if m:
                url = m.group("url")
        if uid:
            row = con.execute("SELECT * FROM jobs WHERE uid = ?", (uid,)).fetchone()
            if row is None:
                problems.append(
                    f"{block['label']}: report shows uid {uid!r} "
                    "that the database does not have")
                continue
        elif not url:
            continue                      # a block with no identity is not a posting
        # A URL does not identify a row. uid deliberately splits two distinct
        # requisitions at one employer, and boards do serve one page for several
        # of them: 27 of 468 rows in a real database shared a URL with another,
        # across four ATS platforms. Assuming otherwise made the checker report
        # a contradiction whenever the report described one requisition and the
        # lookup happened to return its sibling.
        if not uid:
            candidates = con.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchall()
            if not candidates:
                problems.append(f"{block['label']}: report shows a "
                                f"posting the database does not have ({url})")
                continue
            row = candidates[0]
        else:
            candidates = [row]
        if not uid and len(candidates) > 1:
            # Judge against the sibling the report is actually describing, and
            # only complain if none of them match.
            shown = {}
            for line in block["lines"]:
                m = _SCORE.match(line)
                if m:
                    shown = {"gate": m.group("gate"), "score": int(m.group("score"))}
                    break
            match = [c for c in candidates
                     if c["gate"] == shown.get("gate") and c["score"] == shown.get("score")]
            if match:
                row = match[0]
            else:
                have = ", ".join(f"{c['gate']}/{c['score']}" for c in candidates)
                problems.append(
                    f"{block['label']}: report shows "
                    f"{shown.get('gate')}/{shown.get('score')} but none of the "
                    f"{len(candidates)} rows at this URL say that ({have})")
                continue

        where = f"{row['company']} / {row['title']}"
        want_label = (f"{sanitize_external(row['title'], 120)} - "
                      f"{sanitize_external(row['company'], 60) or 'employer not named'}")
        if block["label"] != want_label:
            problems.append(f"{where}: report heading says {block['label']!r}, "
                            f"database implies {want_label!r}")
        if url and url != _expected_url(row):
            problems.append(f"{where}: report shows URL {url!r}, database says "
                            f"{_expected_url(row)!r}")
        if uid and not url:
            problems.append(f"{where}: report block is missing its URL field")
        seen = set()
        for line in block["lines"]:
            m = _SCORE.match(line)
            if m:
                seen.add("score")
                if int(m.group("score")) != row["score"]:
                    problems.append(f"{where}: report says score {m.group('score')}, "
                                    f"database says {row['score']}")
                if m.group("gate") != row["gate"]:
                    problems.append(f"{where}: report says gate {m.group('gate')}, "
                                    f"database says {row['gate']}")
            m = _LOCCOMP.match(line)
            if m:
                seen.add("location")
                want_loc = sanitize_external(row["location"], 80) or "not stated"
                if uid and m.group("loc") != want_loc:
                    problems.append(f"{where}: report shows location "
                                    f"{m.group('loc')!r}, database says {want_loc!r}")
                want = _expected_comp(row)
                if m.group("comp") != want:
                    problems.append(f"{where}: report shows comp {m.group('comp')!r}, "
                                    f"database implies {want!r}")
                # The bug that motivated this file: a header saying the comp is
                # unknown while the reasons line quotes a band.
                if m.group("comp") == "not stated":
                    if re.search(r"comp \$[\d,]+", row["reasons"] or ""):
                        problems.append(
                            f"{where}: report says comp not stated, but the stored "
                            f"reasons quote a band: {row['reasons'][:80]!r}")
            m = _SOURCE.match(line)
            if m:
                seen.add("source")
                want_source = _field(row["source"], 80)
                want_lane = _field(row["lane"], 80) if row["lane"] else None
                if m.group("source") != want_source:
                    problems.append(f"{where}: report shows source "
                                    f"{m.group('source')!r}, database implies "
                                    f"{want_source!r}")
                if m.group("lane") != want_lane:
                    problems.append(f"{where}: report shows lane {m.group('lane')!r}, "
                                    f"database implies {want_lane!r}")
            m = _WHY.match(line)
            if m:
                seen.add("why")
                want_why = sanitize_external(row["reasons"], 300)
                if m.group("why") != want_why:
                    problems.append(f"{where}: report shows reason {m.group('why')!r}, "
                                    f"database implies {want_why!r}")
        # UID was added with the complete contract below. Preserve useful checks
        # for archived pre-UID reports, whose older layout did not render Source.
        if uid:
            for missing in sorted({"score", "location", "source", "why"} - seen):
                problems.append(f"{where}: report block is missing its {missing} field")
    # The compact tail is part of the report too. It used to be invisible to the
    # checker, so changing 40 -> 99 or replacing its URL still returned "agree".
    for entry in _tail_entries(text):
        row = con.execute("SELECT * FROM jobs WHERE uid=?", (entry["uid"],)).fetchone()
        if row is None:
            problems.append(f"VERIFY tail: report shows uid {entry['uid']!r} "
                            "that the database does not have")
            continue
        where = f"{row['company']} / {row['title']}"
        if entry["score"] != row["score"]:
            problems.append(f"{where}: report says score {entry['score']}, "
                            f"database says {row['score']}")
        if row["gate"] != "VERIFY":
            problems.append(f"{where}: report places the role in the VERIFY tail, "
                            f"database says {row['gate']}")
        want_title = sanitize_external(row["title"], 70)
        want_company = sanitize_external(row["company"], 40) or "employer not named"
        want_reason = _tail_reason(row)
        if entry["title"] != want_title:
            problems.append(f"{where}: VERIFY tail shows title {entry['title']!r}, "
                            f"database implies {want_title!r}")
        if entry["company"] != want_company:
            problems.append(f"{where}: VERIFY tail shows company {entry['company']!r}, "
                            f"database implies {want_company!r}")
        if entry["reason"] != want_reason:
            problems.append(f"{where}: VERIFY tail shows reason {entry['reason']!r}, "
                            f"database implies {want_reason!r}")
        if not entry["url"]:
            problems.append(f"{where}: VERIFY tail is missing its URL field")
        elif entry["url"] != _expected_url(row):
            problems.append(f"{where}: report shows URL {entry['url']!r}, database says "
                            f"{_expected_url(row)!r}")
    return problems


def check_db(con: sqlite3.Connection) -> list[str]:
    """Disagreements inside the database itself, independent of any report."""
    problems: list[str] = []
    rows = con.execute("SELECT uid, company, title, comp_min, comp_max, comp_source, reasons, "
                       "gate, score FROM jobs").fetchall()
    for r in rows:
        where = f"{r['company']} / {r['title']}"
        # Same defect class, caught one layer earlier: the scorer knew a number
        # the row does not carry.
        if r["comp_min"] is None and re.search(r"comp \$[\d,]{4,}", r["reasons"] or ""):
            problems.append(f"{where}: reasons quote a comp band but comp_min is NULL")
        if (r["comp_min"] is not None and r["comp_max"] is not None
                and r["comp_max"] < r["comp_min"]):
            problems.append(f"{where}: comp_max {r['comp_max']} below comp_min {r['comp_min']}")
        # A band wider than 10x is not a band, it is two different numbers.
        if (r["comp_min"] is not None and r["comp_max"] is not None
                and r["comp_max"] > r["comp_min"] * 10):
            problems.append(f"{where}: implausible comp spread "
                            f"${r['comp_min']:,} to ${r['comp_max']:,}")
        if ((r["comp_min"] is not None or r["comp_max"] is not None)
                and (r["comp_source"] or "") not in (
                     "board", "body", "unknown")):
            problems.append(f"{where}: comp has no recorded provenance")
        if (r["comp_min"] is None and r["comp_max"] is None
                and (r["comp_source"] or "") not in (
                     "", "absent")):
            problems.append(f"{where}: comp provenance {r['comp_source']!r} has no band")
        if r["gate"] not in ("QUALIFIED", "VERIFY", "EXCLUDED", "SLOT-BLOCKED", ""):
            problems.append(f"{where}: unknown gate {r['gate']!r}")
    return problems


def repair_comp(con: sqlite3.Connection, *, apply: bool = False) -> list[str]:
    """Clear compensation that cannot be true, so it can be re-derived.

    A guard stops the next bad parse; it cannot heal the last one. When an Indeed
    band of "$1.00 - $250,000.00 per year" was annualised to $2,080-$520,000,000,
    the fix that prevents it also stops recognising it: 2,080 is a plausible
    salary floor, so extract_comp now returns the corrupted pair unchanged and
    the row is stuck with it forever.

    Nulling the pair is safe because comp is derived data. The next rescore
    re-parses it from the stored description under the corrected rules, and if
    the posting really has no band the row is honestly blank instead of absurd.
    """
    fixed: list[str] = []
    rows = con.execute("SELECT uid, company, title, comp_min, comp_max FROM jobs "
                       "WHERE comp_min IS NOT NULL AND comp_max IS NOT NULL").fetchall()
    for r in rows:
        if r["comp_max"] > r["comp_min"] * 10 or r["comp_max"] > 2_000_000:
            fixed.append(f"{r['company']} / {r['title'][:50]}: "
                         f"cleared ${r['comp_min']:,} to ${r['comp_max']:,}")
            if apply:
                con.execute("UPDATE jobs SET comp_min=NULL, comp_max=NULL, comp_source='' "
                            "WHERE uid=?",
                            (r["uid"],))
    if apply and fixed:
        con.commit()
    return fixed
