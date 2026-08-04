"""Markdown run report.

Written to be read top-down in two minutes: what is new and qualified first,
then what needs a manual check, then an honest source-health table so a silently
dead adapter never masquerades as "nothing new this week".
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "out"


def _row_block(r: sqlite3.Row, idx: int) -> str:
    comp = ""
    if r["comp_min"]:
        comp = f"${r['comp_min']:,}" + (f" - ${r['comp_max']:,}" if r["comp_max"] else "+")
    else:
        comp = "not stated"
    age = "NEW" if r["first_seen"] == r["last_seen"] else f"first seen {r['first_seen']}"
    lines = [
        f"### {idx}. {r['title']} - {r['company']}",
        f"- **Score** {r['score']} | **{r['gate']}** | {age}",
        f"- **Location** {r['location'] or 'not stated'} | **Comp** {comp}",
        f"- **Source** {r['source']}" + (f" | lane: {r['lane']}" if r["lane"] else ""),
        f"- **Why** {r['reasons']}",
        f"- {r['url']}",
    ]
    return "\n".join(lines)


def write_report(con: sqlite3.Connection, rows: list[sqlite3.Row], *,
                 health: list[sqlite3.Row], run_detail: dict,
                 filename: str | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    path = OUT_DIR / (filename or f"sourcing-{today}.md")

    new_rows = [r for r in rows if r["first_seen"] == r["last_seen"]]
    qual = [r for r in rows if r["gate"] == "QUALIFIED"]
    verify = [r for r in rows if r["gate"] == "VERIFY"]

    L: list[str] = [
        f"# Sourcing run - {today}",
        "",
        f"**Pulled** {run_detail.get('pulled', 0)} postings from "
        f"{run_detail.get('sources_ok', 0)} live sources | "
        f"**{len(new_rows)} new** | **{len(qual)} qualified** | {len(verify)} need a check",
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
