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
import datetime as _dt
OUT_DIR = Path(os.environ.get("CAREERKIT_HOME") or Path(__file__).resolve().parent.parent) / "out"


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
    out = [sorted(v, key=lambda x: -x["score"]) for v in by.values()]
    out.sort(key=lambda g: (-g[0]["score"], g[0]["company"]))
    return out


def _row_block(rows: list[sqlite3.Row], idx: int) -> str:
    r = rows[0]
    comp = ""
    if r["comp_min"]:
        comp = f"${r['comp_min']:,}" + (f" - ${r['comp_max']:,}" if r["comp_max"] else "+")
    else:
        comp = "not stated"
    age = "NEW" if r["first_seen"] == r["last_seen"] else f"first seen {r['first_seen']}"
    lines = [
        f"### {idx}. {r['title']} - {r['company']}"
        + (f"  ({len(rows)} open reqs)" if len(rows) > 1 else "")
        + (" \u26a0 STALE (not sighted in 2+ days - verify live before acting)"
           if str(r["last_seen"] or "")[:10] < (_dt.date.today() - _dt.timedelta(days=2)).isoformat() else ""),
        f"- **Score** {r['score']} | **{r['gate']}** | {age}",
        f"- **Location** {r['location'] or 'not stated'} | **Comp** {comp}",
        f"- **Source** {r['source']}" + (f" | lane: {r['lane']}" if r["lane"] else ""),
        f"- **Why** {r['reasons']}",
        f"- {r['url']}",
    ]
    if len(rows) > 1:
        lines.append(f"- **Also open at:** " + " \u00b7 ".join(
            f"{(o['location'] or 'location not stated')[:34]} ({o['gate'].lower()}, {o['score']})"
            for o in rows[1:8]) + (" ..." if len(rows) > 8 else ""))
    return "\n".join(lines)


def write_report(con: sqlite3.Connection, rows: list[sqlite3.Row], *,
                 health: list[sqlite3.Row], run_detail: dict,
                 filename: str | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    path = OUT_DIR / (filename or f"sourcing-{today}.md")

    new_rows = [r for r in rows if r["first_seen"] == r["last_seen"]]
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
