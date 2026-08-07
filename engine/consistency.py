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

import re
import sqlite3
from pathlib import Path

# "### 3. Senior Lifecycle Marketing Manager - Chime  (2 open reqs)"
_HEAD = re.compile(r"^### \d+\. (?P<title>.+?) - (?P<company>[^-]+?)"
                   r"(?:  \(\d+ open reqs\))?(?: ⚠ STALE.*)?$")
_SCORE = re.compile(r"^- \*\*Score\*\* (?P<score>\d+) \| \*\*(?P<gate>[A-Z-]+)\*\*")
_LOCCOMP = re.compile(r"^- \*\*Location\*\* (?P<loc>.*?) \| \*\*Comp\*\* (?P<comp>.*)$")
_URL = re.compile(r"^- (?P<url>https?://\S+)$")


def _expected_comp(row: sqlite3.Row) -> str:
    """What the renderer should print for this row's compensation."""
    if row["comp_min"]:
        return (f"${row['comp_min']:,}"
                + (f" - ${row['comp_max']:,}" if row["comp_max"] else "+"))
    return "not stated"


def _blocks(text: str) -> list[dict]:
    """Split a report into per-posting blocks, keeping the raw lines."""
    out, cur = [], None
    for line in text.splitlines():
        m = _HEAD.match(line)
        if m:
            if cur:
                out.append(cur)
            cur = {"title": m.group("title").strip(),
                   "company": m.group("company").strip(), "lines": []}
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
    row = con.execute("SELECT MAX(finished) AS t FROM runs").fetchone()
    db_file = Path(con.execute("PRAGMA database_list").fetchall()[0][2] or "")
    newest = db_file.stat().st_mtime if db_file.exists() else 0
    if newest and path.stat().st_mtime < newest - 1:
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

    for block in _blocks(path.read_text()):
        url = None
        for line in block["lines"]:
            m = _URL.match(line.strip())
            if m:
                url = m.group("url")
                break
        if not url:
            continue                      # a block with no URL is not a posting
        row = con.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        if row is None:
            problems.append(f"{block['company']} / {block['title']}: report shows a "
                            f"posting the database does not have ({url})")
            continue

        where = f"{row['company']} / {row['title']}"
        for line in block["lines"]:
            m = _SCORE.match(line)
            if m:
                if int(m.group("score")) != row["score"]:
                    problems.append(f"{where}: report says score {m.group('score')}, "
                                    f"database says {row['score']}")
                if m.group("gate") != row["gate"]:
                    problems.append(f"{where}: report says gate {m.group('gate')}, "
                                    f"database says {row['gate']}")
            m = _LOCCOMP.match(line)
            if m:
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
    return problems


def check_db(con: sqlite3.Connection) -> list[str]:
    """Disagreements inside the database itself, independent of any report."""
    problems: list[str] = []
    rows = con.execute("SELECT uid, company, title, comp_min, comp_max, reasons, "
                       "gate, score FROM jobs").fetchall()
    for r in rows:
        where = f"{r['company']} / {r['title']}"
        # Same defect class, caught one layer earlier: the scorer knew a number
        # the row does not carry.
        if r["comp_min"] is None and re.search(r"comp \$[\d,]{4,}", r["reasons"] or ""):
            problems.append(f"{where}: reasons quote a comp band but comp_min is NULL")
        if r["comp_min"] and r["comp_max"] and r["comp_max"] < r["comp_min"]:
            problems.append(f"{where}: comp_max {r['comp_max']} below comp_min {r['comp_min']}")
        # A band wider than 10x is not a band, it is two different numbers.
        if r["comp_min"] and r["comp_max"] and r["comp_max"] > r["comp_min"] * 10:
            problems.append(f"{where}: implausible comp spread "
                            f"${r['comp_min']:,} to ${r['comp_max']:,}")
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
                con.execute("UPDATE jobs SET comp_min=NULL, comp_max=NULL WHERE uid=?",
                            (r["uid"],))
    if apply and fixed:
        con.commit()
    return fixed
