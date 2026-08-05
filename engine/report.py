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

from .models import ATS_SOURCES, sanitize_external
OUT_DIR = Path(os.environ.get("CAREERKIT_HOME") or Path(__file__).resolve().parent.parent) / "out"


def is_new(r) -> bool:
    """First sighting was THIS run, not merely today.

    "new" used to mean first_seen == last_seen, which is a date comparison. Two
    pulls on the same day therefore both reported the same roles as new, and the
    user re-read a list they had already worked through. The run ids answer the
    question that was actually being asked. Rows written before the run columns
    existed have no ids, so they fall back to the date test."""
    keys = r.keys() if hasattr(r, "keys") else []
    if "first_seen_run" in keys and r["first_seen_run"] is not None:
        return r["first_seen_run"] == r["last_seen_run"]
    return r["first_seen"] == r["last_seen"]


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
    return (
        -(r["score"] or 0),
        0 if src in ATS_SOURCES else 1,
        0 if r["comp_min"] else 1,
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


def _row_block(rows: list[sqlite3.Row], idx: int) -> str:
    r = rows[0]
    comp = ""
    if r["comp_min"]:
        comp = f"${r['comp_min']:,}" + (f" - ${r['comp_max']:,}" if r["comp_max"] else "+")
    else:
        comp = "not stated"
    age = "NEW" if is_new(r) else f"first seen {r['first_seen']}"
    lines = [
        f"### {idx}. {sanitize_external(r['title'], 120)} - {sanitize_external(r['company'], 60)}"
        + (f"  ({len(rows)} open reqs)" if len(rows) > 1 else "")
        + (" \u26a0 STALE (not sighted in 2+ days - verify live before acting)"
           if str(r["last_seen"] or "")[:10] < (_dt.date.today() - _dt.timedelta(days=2)).isoformat() else ""),
        f"- **Score** {r['score']} | **{r['gate']}** | {age}",
        f"- **Location** {sanitize_external(r['location'], 80) or 'not stated'} | **Comp** {comp}",
        f"- **Source** {r['source']}" + (f" | lane: {r['lane']}" if r["lane"] else ""),
        f"- **Why** {sanitize_external(r['reasons'], 300)}",
        f"- {r['url']}",
    ]
    if len(rows) > 1:
        # Every sibling carries its own uid and URL. Listing location and score
        # alone made the grouping destructive in practice: the user could see
        # that other requisitions existed but could not open or mark any of them.
        lines.append(f"- **Also open ({len(rows) - 1} more):**")
        for o in rows[1:12]:
            lines.append(
                f"  - {sanitize_external(o['location'], 40) or 'location not stated'} "
                f"| {o['gate'].lower()} {o['score']} | `{o['uid']}`\n"
                f"    {o['url']}")
        if len(rows) > 12:
            lines.append(f"  - ...and {len(rows) - 12} more")
    return "\n".join(lines)


def write_report(con: sqlite3.Connection, rows: list[sqlite3.Row], *,
                 health: list[sqlite3.Row], run_detail: dict,
                 filename: str | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    path = OUT_DIR / (filename or f"sourcing-{today}.md")

    new_rows = [r for r in rows if is_new(r)]
    qual = _group([r for r in rows if r["gate"] == "QUALIFIED"])
    verify = _group([r for r in rows if r["gate"] == "VERIFY"])

    L: list[str] = [
        f"# Sourcing run - {today}",
        "",
        f"**Pulled** {run_detail.get('pulled', 0)} postings from "
        f"{run_detail.get('sources_ok', 0)} live sources | "
        f"**{len(new_rows)} new** | **{len(qual)} qualified roles** "
        f"({sum(len(g) for g in qual)} reqs) | {len(verify)} need a check",
        "",
    ]

    if run_detail.get("excluded_breakdown"):
        eb = run_detail["excluded_breakdown"]
        L += ["**Screened out:** " + ", ".join(f"{k} {v}" for k, v in
              sorted(eb.items(), key=lambda x: -x[1])[:8]), ""]

    L += ["---", "", "## Qualified", ""]
    if qual:
        L += [_row_block(r, i) + "\n" for i, r in enumerate(qual, 1)]
    else:
        L += ["_Nothing cleared every screenable rail this run._", ""]

    L += ["---", "", "## Needs a manual check", "",
          "_Passes the role and location rails but one rail could not be evidenced "
          "from the posting text._", ""]
    if verify:
        L += [_row_block(r, i) + "\n" for i, r in enumerate(verify, 1)]
    else:
        L += ["_None._", ""]

    L += ["---", "", "## Source health", "",
          "| source | last ok | count | consecutive failures | last error |",
          "|---|---|---|---|---|"]
    for h in health:
        L.append(
            f"| {h['source']} | {h['last_ok'] or '-'} | {h['last_count'] or 0} | "
            f"{h['consecutive_failures']} | {(h['last_error'] or '')[:70]} |"
        )
    L += ["",
          "_A source at 0 with no error is live but had nothing in family. A source "
          "with rising consecutive failures has broken and is silently costing coverage._",
          ""]

    if run_detail.get("discovered"):
        L += ["---", "", "## Newly discovered employers", "",
              "_Found by search, resolved to a pollable board, and added to the "
              "registry. These now get polled every run._", ""]
        for d in run_detail["discovered"][:40]:
            L.append(f"- **{d.get('name')}** - {d.get('ats')}:`{d.get('slug', d.get('tenant',''))}`"
                     f" ({d.get('open_roles', '?')} open)")
        L.append("")

    path.write_text("\n".join(L))
    return path
