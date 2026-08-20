"""Markdown run report.

Written to be read top-down in two minutes: what is new and qualified first,
then what needs a manual check, then an honest source-health table so a silently
dead adapter never masquerades as "nothing new this week".
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import os
import datetime as _dt

from .models import ATS_SOURCES, sanitize_external, sanitize_external_url
OUT_DIR = Path(os.environ.get("CAREERKIT_HOME") or Path(__file__).resolve().parent.parent) / "out"

_URL_OMITTED = "URL omitted (invalid or unsafe external target)"


def _display_url(value: str) -> str:
    """A URL safe to place in Markdown, or an explicit non-link placeholder."""
    return sanitize_external_url(value) or _URL_OMITTED


def _field(value, limit: int = 120) -> str:
    """Sanitize an inline metadata value, including structural pipe syntax."""
    text = "" if value is None else str(value)
    return sanitize_external(text, limit).replace("|", "&#124;")


def _table_cell(value, limit: int = 120) -> str:
    """Flatten untrusted text and prevent it from opening a Markdown table cell."""
    return _field(value, limit)


def is_new(r, current_run: int | None = None) -> bool:
    """First sighting was THIS run, not merely today.

    "new" used to mean first_seen == last_seen, which is a date comparison. Two
    pulls on the same day therefore both reported the same roles as new, and the
    user re-read a list they had already worked through. The run ids answer the
    question that was actually being asked. Rows written before the run columns
    existed have no ids, so they fall back to the date test."""
    keys = r.keys() if hasattr(r, "keys") else []
    if "first_seen_run" in keys:
        if r["first_seen_run"] is not None:
            # Compare against the run being reported, when it is known.
            # first_seen_run == last_seen_run says "seen in exactly one run",
            # which is true FOREVER for a posting sighted once and never again,
            # so seven rows from a previous run were being announced as new
            # again today. Same mistake as the date test it replaced.
            if current_run is not None:
                return r["first_seen_run"] == current_run
            return r["first_seen_run"] == r["last_seen_run"]
        if r["last_seen_run"] is not None:
            # Written before run stamping existed, and re-sighted since. It
            # cannot be new: this run only UPDATED it. Falling through to the
            # date test called 41 pre-existing rows new on the first real run
            # after the change, because they were first seen earlier the same
            # day, and the report disagreed with the pull's own count.
            return False
    # The legacy signal. first_seen == last_seen means "sighted exactly once",
    # NOT "sighted today": a posting seen once a week ago and never again
    # satisfied it forever and was announced as new in every later report. The
    # date bound is what makes it mean what it was always read as meaning.
    return (r["first_seen"] == r["last_seen"]
            and str(r["last_seen"] or "")[:10] == date.today().isoformat())


def _sighting_rank(r) -> tuple:
    """Which sighting of one role represents it in the report.

    The same opening arrives from the employer's own ATS and from two or three
    aggregators, all scoring identically. Whichever the database happened to
    return first used to win, so the URL the user clicked was arbitrary between
    runs. It should never be arbitrary: the employer's ATS link is the canonical
    application, while an aggregator link is a redirect that can be stale,
    tracking-wrapped, or dead while the role is still open.

    Order: highest score, then employer ATS over aggregator, then the sighting
    carrying the most usable detail, then uid so the result is total and two
    runs over unchanged data produce byte-identical reports."""
    src = (r["source"] or "").lower()
    keys = set(r.keys()) if hasattr(r, "keys") else set()
    comp_min = r["comp_min"] if "comp_min" in keys else None
    comp_max = r["comp_max"] if "comp_max" in keys else None
    return (
        -(r["score"] or 0),
        0 if src in ATS_SOURCES else 1,
        0 if comp_min is not None or comp_max is not None else 1,
        0 if (r["location"] or "").strip() else 1,
        -len(r["description"] or "") if "description" in r.keys() else 0,
        r["uid"] or "",
    )


def _group(rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Collapse one role's requisitions into a single entry, best score first.

    Distinct reqs are separate database rows on purpose (a company running two
    real openings under one title must not lose one of them). But a large
    employer also posts ONE role across six cities, and six near-identical
    entries is noise the reader has to de-duplicate by hand. group_key is what
    tells those apart from genuinely different jobs, so grouping happens here,
    at the point of reading, rather than by throwing rows away at write time."""
    by: dict = {}
    for r in rows:
        by.setdefault(r["group_key"] or r["uid"], []).append(r)
    out = [sorted(v, key=_sighting_rank) for v in by.values()]
    # Total order: score, then company, then group key. Without the last term
    # two groups tying on both sorted by insertion order, so the report's
    # numbering shuffled between runs on unchanged data and diffing two reports
    # was useless.
    out.sort(key=lambda g: (-g[0]["score"], g[0]["company"].lower(),
                            g[0]["group_key"] or g[0]["uid"]))
    return out


def _comp_display(r: sqlite3.Row) -> str:
    """Render both the band and the confidence of its provenance."""
    lo, hi = r["comp_min"], r["comp_max"]
    if lo is None and hi is None:
        return "not stated"
    if lo is None:
        comp = f"up to ${hi:,}"
    elif hi is None:
        comp = f"${lo:,}+"
    else:
        comp = f"${lo:,} - ${hi:,}"
    keys = r.keys() if hasattr(r, "keys") else ()
    source = (r["comp_source"] or "unknown") if "comp_source" in keys else "unknown"
    label = {
        "board": "board field",
        "body": "parsed from body",
        "unknown": "source unknown",
    }.get(source, "source unknown")
    return f"{comp} ({label})"


def _row_block(rows: list[sqlite3.Row], idx: int, new_uids: set | None = None) -> str:
    r = rows[0]
    comp = _comp_display(r)
    age = ("NEW" if (r["uid"] in new_uids if new_uids is not None else is_new(r))
           else f"first seen {r['first_seen']}")
    lines = [
        # A blank company must never render as a bare trailing dash. Aggregators
        # do serve rows with no employer at all, and the heading shape
        # "Title - Company" is what this report's OWN parser splits blocks on:
        # one such row on 2026-08-12 was folded into the previous posting's
        # block, and `consistency` then compared a different employer's entry
        # against it and reported three disagreements that did not exist. Saying
        # the employer is unnamed is also the more useful thing to tell a reader,
        # since a posting with no employer cannot be verified or applied to.
        f"### {idx}. {sanitize_external(r['title'], 120)} - "
        f"{sanitize_external(r['company'], 60) or 'employer not named'}"
        + (f"  ({len(rows)} open reqs)" if len(rows) > 1 else "")
        + (" \u26a0 STALE (not sighted in 2+ days - verify live before acting)"
           if str(r["last_seen"] or "")[:10] < (_dt.date.today() - _dt.timedelta(days=2)).isoformat() else ""),
        f"- **Score** {r['score']} | **{r['gate']}** | {age}",
        f"- **UID** `{r['uid']}`",
        f"- **Location** {sanitize_external(r['location'], 80) or 'not stated'} | **Comp** {comp}",
        f"- **Source** {_field(r['source'], 80)}"
        + (f" | lane: {_field(r['lane'], 80)}" if r["lane"] else ""),
        f"- **Why** {sanitize_external(r['reasons'], 300)}",
        f"- {_display_url(r['url'])}",
    ]
    # Surfaced, never used to drop a row: a ghost listing and a small employer
    # with a thin web presence look the same from here, and silently discarding
    # the second to catch the first is the failure this tool exists to prevent.
    try:
        from .ghost import flag as _ghost_flag
        g = _ghost_flag(r)
        if g:
            lines.insert(1, f"- \u26a0 {g}")
    except Exception:
        pass
    if len(rows) > 1:
        # Every sibling carries its own uid and URL. Listing location and score
        # alone made the grouping destructive in practice: the user could see
        # that other requisitions existed but could not open or mark any of them.
        lines.append(f"- **Also open ({len(rows) - 1} more):**")
        for o in rows[1:12]:
            lines.append(
                f"  - {sanitize_external(o['location'], 40) or 'location not stated'} "
                f"| {o['gate'].lower()} {o['score']} | `{o['uid']}`\n"
                f"    {_display_url(o['url'])}")
        if len(rows) > 12:
            lines.append(f"  - ...and {len(rows) - 12} more")
    return "\n".join(lines)


#: VERIFY rows at or above this score get a full block; below it, one line.
#: The 2026-08-14 report was 1,178 lines against this module's own two-minute
#: promise: 144 full manual-check blocks, so a 71-score strong fit with one
#: unproven rail rendered BELOW a 38-score marginal qualified role and was
#: read by nobody. The bar sits just under the lane weights (52-55), where a
#: manual check has a real chance of changing the answer; the tail keeps every
#: fact needed to act (score, missing rail, uid, url) in one line per role.
STRONG_VERIFY = 55


def _tail_line(rows: list[sqlite3.Row]) -> str:
    """One line for a manual-check role below the strong bar."""
    r = rows[0]
    miss = ""
    m = (r["reasons"] or "")
    i = m.find("NEEDS CHECK: ")
    if i != -1:
        miss = m[i + len("NEEDS CHECK: "):].split(" | ")[0]
    else:
        miss = m.split(" | ")[0]
    sib = f" (+{len(rows) - 1} reqs)" if len(rows) > 1 else ""
    return (f"- {r['score']} · **Title** {sanitize_external(r['title'], 70)} · "
            f"**Company** {sanitize_external(r['company'], 40) or 'employer not named'}{sib} · "
            f"**Check** {sanitize_external(miss, 70)} · **UID** `{r['uid']}`\n"
            f"  {_display_url(r['url'])}")


def write_report(con: sqlite3.Connection, rows: list[sqlite3.Row], *,
                 health: list[sqlite3.Row], run_detail: dict,
                 filename: str | None = None, run_id: int | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    path = OUT_DIR / (filename or f"sourcing-{today}.md")

    new_rows = [r for r in rows if is_new(r, run_id)]
    _new_uids = {r["uid"] for r in new_rows}
    qual = _group([r for r in rows if r["gate"] == "QUALIFIED"])
    verify = _group([r for r in rows if r["gate"] == "VERIFY"])
    strong = [g for g in verify if (g[0]["score"] or 0) >= STRONG_VERIFY]
    tail = [g for g in verify if (g[0]["score"] or 0) < STRONG_VERIFY]

    L: list[str] = [
        f"# Sourcing run - {today}",
        "",
        f"**Pulled** {run_detail.get('pulled', 0)} postings from "
        f"{run_detail.get('sources_ok', 0)} complete sources | "
        f"**{len(new_rows)} new** | **{len(qual)} qualified roles** "
        f"({sum(len(g) for g in qual)} reqs) | {len(verify)} need a check",
        "",
    ]
    errors = run_detail.get("errors") or {}
    if errors:
        L += [f"**Coverage gaps:** {len(errors)} source(s) were partial, capped, "
              "dormant, or failed; their retained jobs remain usable, but they "
              "are not counted as complete. See Source health below.", ""]
    # A strong fit missing one rail can outscore everything in Qualified, and
    # the section order buries it. Name the best one where it cannot be missed.
    if strong:
        b = strong[0][0]
        L += [f"**Strongest unverified:** {sanitize_external(b['title'], 70)} - "
              f"{sanitize_external(b['company'], 40) or 'employer not named'} "
              f"({b['score']}) - see the manual-check section", ""]

    if run_detail.get("excluded_breakdown"):
        eb = run_detail["excluded_breakdown"]
        L += ["**Screened out:** " + ", ".join(
              f"{_field(k, 80)} {_field(v, 20)}" for k, v in
              sorted(eb.items(), key=lambda x: -x[1])[:8]), ""]

    L += ["---", "", "## Qualified", ""]
    if qual:
        L += [_row_block(r, i, _new_uids) + "\n" for i, r in enumerate(qual, 1)]
    else:
        L += ["_Nothing cleared every screenable rail this run._", ""]

    L += ["---", "", "## Needs a manual check", "",
          "_Passes the role and location rails but one rail could not be evidenced "
          "from the posting text._", ""]
    if strong:
        L += [_row_block(r, i, _new_uids) + "\n" for i, r in enumerate(strong, 1)]
    if tail:
        L += [f"### ...and {len(tail)} more below {STRONG_VERIFY}, one line each", "",
              "_Everything needed to act is here: score, the unproven rail, the "
              "uid to mark, the link. Full detail lives in the database._", ""]
        L += [_tail_line(g) for g in tail]
        L += [""]
    if not verify:
        L += ["_None._", ""]

    L += ["---", "", "## Source health", "",
          "| source | last ok | count | consecutive failures | last error |",
          "|---|---|---|---|---|"]
    for h in health:
        L.append(
            f"| {_table_cell(h['source'], 80)} | {_table_cell(h['last_ok'] or '-', 40)} | "
            f"{_table_cell(h['last_count'] or 0, 20)} | "
            f"{_table_cell(h['consecutive_failures'], 20)} | "
            f"{_table_cell(h['last_error'] or '', 70)} |"
        )
    L += ["",
          "_A source at 0 with no error completed but had nothing in family. "
          "Partial or capped sources may still contribute jobs, but cannot prove "
          "that missing postings closed. Rising failures are coverage loss._",
          ""]

    # Which of the feeds that ran are scrapers rather than APIs. Worth stating in
    # the artifact the user keeps, not only in the README they read once.
    from .aggregators import policy as _policy
    scraped = sorted({h["source"][5:] for h in health if str(h["source"]).startswith("feed:")
                      and _policy(h["source"][5:])["kind"] == "scraping"
                      and (h["last_count"] or 0) > 0})
    if scraped:
        L += [f"_Results above include {', '.join(_field(s, 60) for s in scraped)}, "
              "which read public "
              "search pages rather than an official API._", ""]

    if run_detail.get("discovered"):
        L += ["---", "", "## Newly discovered employers", "",
              "_Found by search, resolved to a pollable board, and added to the "
              "registry. These now get polled every run._", ""]
        for d in run_detail["discovered"][:40]:
            name = _field(d.get("name") or "", 100)
            ats = _field(d.get("ats") or "", 40)
            slug = _field(d.get("slug", d.get("tenant", "")) or "", 80)
            count = _field(d.get("open_roles", "?"), 20)
            L.append(f"- **{name}** - {ats}:`{slug}` ({count} open)")
        L.append("")

    # A hidden logical-state marker lets consistency distinguish a real
    # post-report database change from SQLite checkpointing identical WAL data
    # into jobs.db after this file is written.
    from .consistency import report_state_fingerprint
    try:
        state = report_state_fingerprint(con)
    except sqlite3.OperationalError:
        # write_report is also a small reusable renderer and its unit fixtures
        # intentionally pass rows without constructing CareerKit's full schema.
        # Real CLI connections have already run store.connect() migrations.
        state = None
    if state:
        L += [f"<!-- careerkit-report-state-v1: {state} -->", ""]

    path.write_text("\n".join(L), encoding="utf-8")
    # latest.md follows the report EVERY time one is written, not just on a pull.
    # It used to be copied by the pull command only, so after a `rescore` the
    # dated report held the new verdicts while latest.md still showed the old
    # ones -- and `consistency` compares the DATED file, so it passed and the
    # stale copy survived. Seen live 2026-08-10: latest.md advertised five
    # postings as QUALIFIED that the database had already excluded.
    if filename is None:
        try:
            (OUT_DIR / "latest.md").write_text("\n".join(L), encoding="utf-8")
        except OSError as e:
            print(f"  (could not refresh latest.md: {e})")
    return path
