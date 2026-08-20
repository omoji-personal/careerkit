"""Application-pipeline analytics and a private, self-contained dashboard.

CareerKit already remembers every posting and status transition.  This module
turns that history into decisions: where applications are converting, which
threads have gone quiet, and whether the search is producing interviews rather
than merely producing rows.  It deliberately stays deterministic and local.
No ATS-specific "match score" is invented and the HTML loads no remote assets.
"""
from __future__ import annotations

import html
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

from . import applied
from .models import sanitize_external_url
from .report import OUT_DIR


STAGES = ("applied", "interviewing", "offer", "rejected", "withdrawn")
ACTIVE_STAGES = {"applied", "interviewing", "offer"}
_STAGE_ORDER = {s: i for i, s in enumerate(STAGES)}


def _day(value) -> date | None:
    """Parse an ISO date/datetime without turning bad evidence into today."""
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _pct(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def _synthetic_key(rec: dict) -> str:
    """Stable identity for evidence that cannot safely map to a database row.

    Requisition or URL wins when supplied.  Company/title is the conservative
    fallback: it may merge two applications to the exact same role over time,
    but it never fabricates two applications from two updates to one pipeline.
    """
    identifying = (rec.get("url") or rec.get("job_id") or
                   rec.get("requisition") or rec.get("req_id") or "")
    return "evidence:" + "|".join((
        applied._norm(rec.get("company") or ""),
        str(rec.get("title") or "").strip().lower(),
        str(identifying).strip().lower(),
    ))


def build_snapshot(con: sqlite3.Connection, evidence: list[dict] | None = None,
                   *, as_of: date | None = None, follow_up_days: int = 14,
                   weeks: int = 8) -> dict:
    """Return a JSON-serialisable, evidence-aware view of the search pipeline.

    `applications.jsonl` is richer than jobs.status, while events preserve
    manual changes.  Both are used.  Evidence is matched with the same cautious
    reconciler as `careerkit applied`; ambiguous company-only evidence still
    counts as a real application but is never attached to an arbitrary job.
    """
    today = as_of or date.today()
    # `load_evidence` supplies line numbers, but callers of this public helper
    # may pass dictionaries directly. A unique synthetic line prevents several
    # otherwise-valid records from all inheriting the UID matched for the last
    # record whose missing line number became the shared `None` key.
    evidence = [{**rec, "_line": rec.get("_line", -(i + 1))}
                for i, rec in enumerate(evidence or [])]
    match = applied.reconcile(con, evidence, apply=False)
    matched_line = {m.get("_line"): m["uid"] for m in match["matched"]}

    jobs = {r["uid"]: dict(r) for r in con.execute(
        "SELECT uid, company, title, lane, source, status, first_seen, last_seen, "
        "url, gate, score "
        "FROM jobs")}
    apps: dict[str, dict] = {}
    sequence = 0

    def ensure(key: str, *, uid: str | None = None, company: str = "",
               title: str = "") -> dict:
        row = jobs.get(uid or "", {})
        return apps.setdefault(key, {
            "uid": uid,
            "company": company or row.get("company") or "Unknown employer",
            "title": title or row.get("title") or "Role not identified",
            "lane": row.get("lane") or "(unmapped)",
            "source": row.get("source") or "evidence",
            "url": row.get("url") or "",
            "gate": row.get("gate"),
            "score": row.get("score"),
            "database_status": row.get("status"),
            "timeline": [],
            "last_activity": None,
            "mapped": bool(uid),
        })

    def add_stage(app: dict, stage: str, when, origin: str, detail: str = "") -> None:
        nonlocal sequence
        if stage not in STAGES:
            return
        sequence += 1
        d = _day(when)
        event = {"stage": stage, "on": d.isoformat() if d else None,
                 "origin": origin, "detail": detail or "", "_sequence": sequence}
        signature = (event["stage"], event["on"], event["origin"], event["detail"])
        if not any((e["stage"], e["on"], e["origin"], e["detail"]) == signature
                   for e in app["timeline"]):
            app["timeline"].append(event)
        if d and (app["last_activity"] is None or d > app["last_activity"]):
            app["last_activity"] = d

    # The evidence file can describe roles the sourcing engine never saw.  That
    # is a coverage fact, not a reason to erase the application from metrics.
    for rec in evidence:
        status = str(rec.get("status") or "").lower()
        if rec.get("_bad") or status not in STAGES:
            continue
        uid = matched_line.get(rec.get("_line"))
        key = uid or _synthetic_key(rec)
        app = ensure(key, uid=uid, company=rec.get("company") or "",
                     title=rec.get("title") or "")
        if rec.get("applied_on"):
            add_stage(app, "applied", rec.get("applied_on"), "evidence",
                      "explicit applied_on")
        add_stage(app, status, rec.get("on") or rec.get("status_on"), "evidence",
                  str(rec.get("source") or "applications.jsonl"))

    # Application events are authoritative pipeline stages.  Plain status
    # events remain useful for manual marks and for databases predating the
    # richer evidence file.
    db_events = [dict(r) for r in con.execute(
        "SELECT e.uid, e.at, e.kind, e.detail, j.company, j.title "
        "FROM events e LEFT JOIN jobs j ON j.uid=e.uid ORDER BY e.at, e.event_id")]
    app_event_stages: dict[str, set[str]] = defaultdict(set)
    app_event_days: dict[str, list[date]] = defaultdict(list)
    for e in db_events:
        if e["kind"].startswith("application:"):
            stage = e["kind"].split(":", 1)[1]
            if stage in STAGES:
                app_event_stages[e["uid"]].add(stage)
                if _day(e["at"]):
                    app_event_days[e["uid"]].append(_day(e["at"]))

    for e in db_events:
        uid = e["uid"]
        if not uid:
            continue
        kind = e["kind"]
        stage = kind.split(":", 1)[1] if ":" in kind else ""
        if stage not in STAGES:
            # A note, recruiter contact, or screen still resets the silence
            # clock even when it is not a pipeline transition.
            app = apps.get(uid)
            d = _day(e["at"])
            if app and d and (app["last_activity"] is None or d > app["last_activity"]):
                app["last_activity"] = d
            continue
        app = ensure(uid, uid=uid, company=e.get("company") or "",
                     title=e.get("title") or "")
        if kind.startswith("application:"):
            add_stage(app, stage, e["at"], "application-event", e.get("detail") or "")
            continue
        if not kind.startswith("status:"):
            continue
        # Reconciliation in older builds wrote status:applied *today* while
        # preserving an older application:interviewing date.  Letting that
        # bookkeeping event win made an interview look like a fresh application.
        # Keep status:applied only when it supplies a genuinely earlier apply
        # date or there is no richer application stage at all.
        richer_days = app_event_days.get(uid, [])
        if stage == "applied" and richer_days:
            d = _day(e["at"])
            if not d or d >= min(richer_days):
                continue
        # A later manual rejection remains meaningful even if the application
        # originally came from evidence.
        if stage in app_event_stages.get(uid, set()):
            continue
        add_stage(app, stage, e["at"], "status-event", e.get("detail") or "")

    # Legacy applied/rejected rows may predate events entirely.  Count them,
    # but leave their date unknown rather than inventing an application date
    # from a board sighting.
    for uid, row in jobs.items():
        if row["status"] not in ("applied", "rejected"):
            continue
        app = ensure(uid, uid=uid)
        if not app["timeline"]:
            add_stage(app, row["status"], None, "legacy-status")

    output = []
    for app in apps.values():
        timeline = sorted(app["timeline"], key=lambda e: (
            e["on"] or "0000-00-00", _STAGE_ORDER[e["stage"]], e["_sequence"]))
        if not timeline:
            continue
        latest = timeline[-1]["stage"]
        # The row's closed state is stronger than a partial event history.
        if app["database_status"] == "rejected":
            latest = "rejected"
        stages_seen = {e["stage"] for e in timeline}
        submitted = bool(stages_seen)
        interviewed = bool(stages_seen & {"interviewing", "offer"})
        offered = "offer" in stages_seen
        last = app["last_activity"]
        days_silent = (today - last).days if last and last <= today else None
        follow_up_due = (latest in {"applied", "interviewing"} and
                         days_silent is not None and days_silent >= follow_up_days)

        def first_day(names: set[str]) -> date | None:
            ds = [_day(e["on"]) for e in timeline if e["stage"] in names and e["on"]]
            return min(ds) if ds else None

        applied_on = first_day({"applied"})
        interview_on = first_day({"interviewing", "offer"})
        outcome_on = first_day({"offer", "rejected", "withdrawn"})
        item = {k: v for k, v in app.items() if k not in {"timeline", "last_activity"}}
        item.update({
            "current_stage": latest,
            "submitted": submitted,
            "interviewed": interviewed,
            "offered": offered,
            "applied_on": applied_on.isoformat() if applied_on else None,
            "last_activity": last.isoformat() if last else None,
            "days_silent": days_silent,
            "follow_up_due": follow_up_due,
            "days_to_interview": ((interview_on - applied_on).days
                                  if applied_on and interview_on and interview_on >= applied_on
                                  else None),
            "days_to_outcome": ((outcome_on - applied_on).days
                                if applied_on and outcome_on and outcome_on >= applied_on
                                else None),
            "timeline": [{k: v for k, v in e.items() if not k.startswith("_")}
                         for e in timeline],
        })
        output.append(item)

    output.sort(key=lambda a: (not a["follow_up_due"],
                               -(a["days_silent"] if a["days_silent"] is not None else -1),
                               a["company"].lower()))
    submitted = len(output)
    interviewed = sum(a["interviewed"] for a in output)
    offered = sum(a["offered"] for a in output)
    current = Counter(a["current_stage"] for a in output)
    known_response = sum(a["current_stage"] in {"interviewing", "offer", "rejected"}
                         or a["interviewed"] for a in output)
    interview_days = [a["days_to_interview"] for a in output
                      if a["days_to_interview"] is not None]
    outcome_days = [a["days_to_outcome"] for a in output
                    if a["days_to_outcome"] is not None]

    by_lane = []
    for lane in sorted({a["lane"] for a in output}):
        lane_apps = [a for a in output if a["lane"] == lane]
        lane_interviews = sum(a["interviewed"] for a in lane_apps)
        by_lane.append({"lane": lane, "submitted": len(lane_apps),
                        "interviewed": lane_interviews,
                        "interview_rate": _pct(lane_interviews, len(lane_apps)),
                        "offers": sum(a["offered"] for a in lane_apps)})
    by_lane.sort(key=lambda x: (-x["submitted"], x["lane"]))

    week_rows = []
    monday = today - timedelta(days=today.weekday())
    for offset in range(weeks - 1, -1, -1):
        start = monday - timedelta(weeks=offset)
        end = start + timedelta(days=6)
        n = sum(1 for a in output if a["applied_on"] and
                start <= date.fromisoformat(a["applied_on"]) <= end)
        week_rows.append({"week": start.isoformat(), "applications": n})

    return {
        "as_of": today.isoformat(),
        "follow_up_days": follow_up_days,
        "summary": {
            "submitted": submitted,
            "active": sum(current[s] for s in ACTIVE_STAGES),
            "interviewed": interviewed,
            "offers": offered,
            "rejected": current["rejected"],
            "withdrawn": current["withdrawn"],
            "known_response_rate": _pct(known_response, submitted),
            "interview_rate": _pct(interviewed, submitted),
            "offer_rate": _pct(offered, submitted),
            "follow_up_due": sum(a["follow_up_due"] for a in output),
            "median_days_to_interview": (round(median(interview_days), 1)
                                         if interview_days else None),
            "median_days_to_outcome": (round(median(outcome_days), 1)
                                       if outcome_days else None),
        },
        "current_stages": {s: current[s] for s in STAGES},
        "by_lane": by_lane,
        "weekly": week_rows,
        "follow_ups": [a for a in output if a["follow_up_due"]],
        "applications": output,
        "data_quality": {
            "unmapped_evidence": len(match["ambiguous"]) + len(match["unmatched"]),
            "ambiguous_evidence": len(match["ambiguous"]),
            "invalid_evidence": len(match["problems"]),
            "undated_applications": sum(not a["last_activity"] for a in output),
        },
    }


def format_text(snapshot: dict) -> str:
    """A compact weekly read that remains useful in a terminal or notification."""
    s = snapshot["summary"]
    pct = lambda v: "n/a" if v is None else f"{v:.1f}%"
    days = lambda v: "n/a" if v is None else f"{v:g}d"
    lines = [
        f"Pipeline as of {snapshot['as_of']}",
        (f"  submitted {s['submitted']} | active {s['active']} | "
         f"interviewed {s['interviewed']} | offers {s['offers']} | "
         f"rejected {s['rejected']}"),
        (f"  known response {pct(s['known_response_rate'])} | "
         f"interview {pct(s['interview_rate'])} | offer {pct(s['offer_rate'])}"),
        (f"  median apply-to-interview {days(s['median_days_to_interview'])} | "
         f"apply-to-outcome {days(s['median_days_to_outcome'])}"),
        "",
        f"Follow-up queue ({s['follow_up_due']} at {snapshot['follow_up_days']}+ silent days)",
    ]
    if snapshot["follow_ups"]:
        for a in snapshot["follow_ups"][:20]:
            lines.append(f"  {a['days_silent']:>3}d  {a['company']} - {a['title']} "
                         f"[{a['current_stage']}]")
    else:
        lines.append("  none")
    lines += ["", "Conversion by lane"]
    if snapshot["by_lane"]:
        for lane in snapshot["by_lane"]:
            rate = pct(lane["interview_rate"])
            lines.append(f"  {lane['lane']:<22} {lane['submitted']:>3} submitted | "
                         f"{lane['interviewed']:>2} interviews ({rate}) | "
                         f"{lane['offers']} offers")
    else:
        lines.append("  no application history yet")
    q = snapshot["data_quality"]
    if any(q.values()):
        lines += ["", (f"Data notes: {q['unmapped_evidence']} evidence row(s) not safely mapped; "
                        f"{q['undated_applications']} application(s) lack a usable date; "
                        f"{q['invalid_evidence']} invalid evidence row(s).")]
    return "\n".join(lines)


def _safe_url(value: str) -> str:
    """Use the same untrusted-link policy as the Markdown report."""
    return sanitize_external_url(value)


def write_dashboard(con: sqlite3.Connection, snapshot: dict,
                    opportunities: list[sqlite3.Row], *, filename: str | None = None) -> Path:
    """Write an app-like command center with no telemetry or remote resources."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / (filename or f"dashboard-{snapshot['as_of']}.html")
    esc = lambda v: html.escape("" if v is None else str(v), quote=True)
    s = snapshot["summary"]

    last_run = con.execute(
        "SELECT run_id, started, pulled, new, qualified FROM runs "
        "WHERE finished IS NOT NULL ORDER BY run_id DESC LIMIT 1").fetchone()
    new_last_run = 0
    if last_run:
        new_last_run = con.execute(
            "SELECT COUNT(*) n FROM jobs WHERE first_seen_run=? "
            "AND gate IN ('QUALIFIED','VERIFY') AND status='new' "
            "AND delisted_on IS NULL", (last_run["run_id"],)).fetchone()["n"]

    health = [dict(r) for r in con.execute(
        "SELECT source,last_ok,last_count,last_error,consecutive_failures "
        "FROM source_health ORDER BY consecutive_failures DESC, source")]
    dormant = [h for h in health if str(h["last_error"] or "").lower().startswith("dormant:")]
    failing = [h for h in health if (h["consecutive_failures"] or 0) > 0 and h not in dormant]
    healthy = len(health) - len(dormant) - len(failing)

    relationships = [dict(r) for r in con.execute(
        "SELECT * FROM employer_history ORDER BY at DESC, history_id DESC")]
    history_by_company: dict[str, list[dict]] = defaultdict(list)
    for rel in relationships:
        history_by_company[rel["company_key"]].append(rel)

    def display_pct(value) -> str:
        return "n/a" if value is None else f"{value:.1f}%"

    def display_days(value) -> str:
        return "n/a" if value is None else f"{value:g} days"

    def plural(count: int, singular: str, plural_form: str | None = None) -> str:
        return singular if count == 1 else (plural_form or singular + "s")

    def kpi(label: str, value, note: str, tone: str, testid: str = "") -> str:
        attr = f' data-testid="{esc(testid)}"' if testid else ""
        return (f'<article class="kpi tone-{tone}"{attr}><div class="kpi-top">'
                f'<span>{esc(label)}</span><i aria-hidden="true"></i></div>'
                f'<strong>{esc(value)}</strong><small>{esc(note)}</small></article>')

    kpis = "".join((
        kpi("New matches", new_last_run, "from the latest completed run", "blue"),
        kpi("Active pipeline", s["active"], f'{s["submitted"]} submitted overall', "violet"),
        kpi("Interview rate", display_pct(s["interview_rate"]),
            f'{s["interviewed"]} {plural(s["interviewed"], "application")} reached interview',
            "green"),
        kpi("Follow-ups", s["follow_up_due"],
            f'due after {snapshot["follow_up_days"]} silent days', "amber"),
        kpi("Source failures", len(failing), f'{healthy} healthy · {len(dormant)} dormant',
            "red" if failing else "green", "source-failures"),
    ))

    stage_cells = "".join(
        f'<div class="stage stage-{esc(stage)}"><span>{esc(stage)}</span>'
        f'<strong>{snapshot["current_stages"][stage]}</strong></div>' for stage in STAGES)
    weekly_max = max((w["applications"] for w in snapshot["weekly"]), default=0) or 1
    weekly_cells = "".join(
        f'<div class="week"><span class="bar-shell"><i style="height:'
        f'{max(3, round(100 * w["applications"] / weekly_max))}%"></i></span>'
        f'<b>{w["applications"]}</b><small>{esc(w["week"][5:])}</small></div>'
        for w in snapshot["weekly"])

    lane_max = max((x["submitted"] for x in snapshot["by_lane"]), default=0) or 1
    lane_rows = "".join(
        f'<div class="lane-row"><div><b>{esc(x["lane"])}</b>'
        f'<span>{x["submitted"]} submitted · {x["interviewed"]} '
        f'{plural(x["interviewed"], "interview")}</span></div>'
        f'<div class="lane-meter"><i style="width:{round(100*x["submitted"]/lane_max)}%"></i></div>'
        f'<strong>{"—" if x["interview_rate"] is None else str(x["interview_rate"]) + "%"}</strong></div>'
        for x in snapshot["by_lane"])
    if not lane_rows:
        lane_rows = '<div class="empty-state compact"><b>No lane data yet</b><span>Record an application to start measuring conversion.</span></div>'

    follow_cards = []
    for a in snapshot["follow_ups"]:
        url = _safe_url(a.get("url") or "")
        open_link = (f'<a class="text-link" href="{esc(url)}" rel="noreferrer noopener">'
                     'Open posting <span aria-hidden="true">↗</span></a>') if url else ""
        follow_cards.append(
            f'<article class="follow-card"><div class="age"><strong>{a["days_silent"]}</strong>'
            f'<span>days quiet</span></div><div class="follow-main"><div class="eyebrow">'
            f'{esc(a["current_stage"])} · last activity {esc(a["last_activity"])}</div>'
            f'<h3>{esc(a["title"])}</h3><p>{esc(a["company"])}</p></div>{open_link}</article>')
    if not follow_cards:
        follow_cards.append('<div class="empty-state"><div class="empty-icon">✓</div><b>Nothing is stale</b><span>No active thread has crossed the follow-up threshold.</span></div>')

    relationship_cards = []
    for rel in relationships[:12]:
        person = rel["contact"] or rel["contact_email"] or rel["kind"]
        initial = next((c.upper() for c in str(person) if c.isalnum()), "•")
        relationship_cards.append(
            f'<article class="relationship-card"><div class="avatar">{esc(initial)}</div>'
            f'<div><div class="relationship-meta"><span>{esc(rel["kind"])}</span>'
            f'<time>{esc(rel["at"][:10])}</time></div><h3>{esc(rel["company"])}</h3>'
            f'<p>{esc(person)}</p><small>{esc(rel["detail"] or "Context recorded")}</small></div></article>')
    if not relationship_cards:
        relationship_cards.append('<div class="empty-state"><div class="empty-icon">＋</div><b>No relationship memory yet</b><span>Add a recruiter, referral, or prior employer interaction.</span></div>')

    job_rows = []
    for r in opportunities:
        score = int(r["score"] or 0)
        url = _safe_url(r["url"])
        title = esc(r["title"])
        title_cell = (f'<a href="{esc(url)}" rel="noreferrer noopener">{title}'
                      '<span class="external" aria-hidden="true">↗</span></a>' if url else title)
        lo, hi = r["comp_min"], r["comp_max"]
        if lo is not None or hi is not None:
            if lo is None:
                comp = f'Up to ${hi:,}'
            elif hi is None:
                comp = f'${lo:,}+'
            else:
                comp = f'${lo:,}-${hi:,}'
            comp_source = ((r["comp_source"] or "unknown")
                           if "comp_source" in r.keys() else "unknown")
            comp += {"board": " · board field", "body": " · parsed from body",
                     "unknown": " · source unknown"}.get(
                         comp_source, " · source unknown")
        else:
            comp = "Not stated"
        related = history_by_company.get(applied._norm(r["company"]), [])
        history_badge = ""
        if related:
            latest = related[0]
            summary = f'{latest["at"][:10]} {latest["kind"]}'
            if latest["contact"]:
                summary += f' with {latest["contact"]}'
            if latest["detail"]:
                summary += f': {latest["detail"]}'
            history_badge = (f'<span class="history" title="{esc(summary)}">'
                             f'{len(related)} relationship note'
                             f'{"s" if len(related) != 1 else ""}</span>')
        is_new = bool(last_run and r["first_seen_run"] == last_run["run_id"])
        new_badge = '<span class="new-badge">New</span>' if is_new else ""
        search = " ".join(str(r[k] or "") for k in
                          ("company", "title", "location", "lane", "gate", "source")).lower()
        job_rows.append(
            f'<tr data-gate="{esc(r["gate"])}" data-search="{esc(search)}" '
            f'data-score="{score}" data-company="{esc(str(r["company"]).lower())}" '
            f'data-first="{esc(r["first_seen"] or "")}"><td class="score-cell">'
            f'<strong>{score}</strong><span><i style="width:{score}%"></i></span></td>'
            f'<td class="role-cell"><div>{new_badge}{title_cell}</div><b>{esc(r["company"])}</b>'
            f'{history_badge}</td><td><span class="gate {esc(r["gate"].lower())}">'
            f'{esc(r["gate"])}</span><small class="lane">{esc(r["lane"] or "unmapped")}</small></td>'
            f'<td><span class="primary-detail">{esc(r["location"] or "Not stated")}</span>'
            f'<small>{esc(comp)}</small></td><td><span class="primary-detail">{esc(r["source"])}</span>'
            f'<small>First seen {esc(r["first_seen"] or "unknown")}</small></td></tr>')
    if not job_rows:
        job_rows.append('<tr class="no-jobs"><td colspan="5"><div class="empty-state"><b>No active roles clear the current rails</b><span>Run a search or review the criteria audit.</span></div></td></tr>')

    issue_cards = "".join(
        f'<article class="issue-card"><div><span class="status-dot"></span>'
        f'<b>{esc(h["source"])}</b></div><strong>{h["consecutive_failures"] or 0} consecutive</strong>'
        f'<p>{esc((h["last_error"] or "Unknown source error")[:180])}</p>'
        f'<small>Last successful run: {esc((h["last_ok"] or "never")[:16])}</small></article>'
        for h in failing[:12])
    if not issue_cards:
        issue_cards = '<div class="empty-state compact"><div class="empty-icon">✓</div><b>All enabled sources are healthy</b><span>Dormant integrations are not counted as failures.</span></div>'

    health_rows = []
    for h in health:
        is_dormant = h in dormant
        is_failing = h in failing
        label = "Dormant" if is_dormant else ("Failing" if is_failing else "Healthy")
        cls = label.lower()
        health_rows.append(
            f'<tr><td><b>{esc(h["source"])}</b></td><td><span class="health {cls}">{label}</span></td>'
            f'<td>{esc((h["last_ok"] or "—")[:16])}</td><td>{h["last_count"] or 0}</td>'
            f'<td>{esc((h["last_error"] or "")[:140])}</td></tr>')
    if not health_rows:
        health_rows.append('<tr><td colspan="5">No source run recorded yet.</td></tr>')

    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    last_run_text = esc(last_run["started"][:16].replace("T", " ") if last_run else "Not run yet")
    style = """
:root{--navy:#0c1828;--navy-2:#13263c;--ink:#152238;--muted:#66758c;--paper:#f4f6fa;--card:#fff;--line:#e1e6ee;--blue:#2f6fed;--blue-soft:#eaf0ff;--green:#138a6a;--green-soft:#e6f6f1;--amber:#b66808;--amber-soft:#fff2dc;--red:#c44444;--red-soft:#fdebec;--violet:#7455c6;--shadow:0 1px 2px rgba(16,30,54,.04),0 8px 24px rgba(16,30,54,.06)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.skip{position:fixed;left:-999px;top:8px;z-index:20;background:#fff;padding:8px}.skip:focus{left:8px}.app{min-height:100vh;display:grid;grid-template-columns:248px minmax(0,1fr)}.sidebar{position:sticky;top:0;height:100vh;background:linear-gradient(180deg,var(--navy),#0a1421);color:#dbe6f5;padding:28px 18px;display:flex;flex-direction:column}.brand{display:flex;align-items:center;gap:11px;padding:0 8px 28px}.brand-mark{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;background:linear-gradient(145deg,#4f87ff,#7e63de);font-weight:850;letter-spacing:-.05em;color:#fff;box-shadow:0 8px 20px #0004}.brand strong{display:block;color:#fff;font-size:17px}.brand span{display:block;color:#8fa2ba;font-size:11px}.nav{display:grid;gap:4px}.nav a{display:flex;align-items:center;justify-content:space-between;gap:12px;color:#aebed2;text-decoration:none;padding:10px 12px;border-radius:9px;font-weight:600}.nav a:hover,.nav a:focus-visible{background:#ffffff0d;color:#fff}.nav a span:first-child{display:flex;gap:10px;align-items:center}.nav svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:1.8}.nav-badge{min-width:22px;text-align:center;padding:1px 6px;border-radius:99px;background:#ffffff12;color:#d7e5f7;font-size:10px}.nav-badge.alert{background:#bd732a33;color:#ffd18c}.privacy{margin-top:auto;background:#ffffff09;border:1px solid #ffffff0b;border-radius:12px;padding:13px}.privacy b{display:block;color:#eef5ff;font-size:12px}.privacy span{color:#8fa2ba;font-size:10px}.content{min-width:0}.topbar{height:72px;padding:0 34px;display:flex;align-items:center;justify-content:space-between;background:#ffffffc9;border-bottom:1px solid var(--line);backdrop-filter:blur(12px);position:sticky;top:0;z-index:8}.topbar b{font-size:13px}.topbar span{display:block;color:var(--muted);font-size:11px}.run-status{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:12px}.run-status i{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 4px var(--green-soft)}main{max-width:1500px;margin:auto;padding:30px 34px 60px}section{scroll-margin-top:92px}.hero{position:relative;overflow:hidden;background:linear-gradient(118deg,#142b48,#1c3a61 58%,#245275);color:#fff;border-radius:20px;padding:30px 34px;box-shadow:var(--shadow)}.hero:after{content:"";position:absolute;width:330px;height:330px;right:-80px;top:-160px;border:70px solid #ffffff09;border-radius:50%}.hero .eyebrow{color:#9bc3ff}.hero h1{font-size:32px;letter-spacing:-.035em;margin:6px 0 7px}.hero p{max-width:680px;color:#c4d3e7;margin:0}.hero-actions{display:flex;gap:10px;margin-top:20px}.button{display:inline-flex;align-items:center;gap:7px;border-radius:9px;padding:9px 13px;text-decoration:none;font-weight:700;font-size:12px}.button.primary{background:#fff;color:#18385c}.button.secondary{background:#ffffff10;color:#fff;border:1px solid #ffffff24}.eyebrow{text-transform:uppercase;letter-spacing:.11em;font-size:10px;font-weight:800;color:var(--muted)}.kpis{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:12px;margin:16px 0}.kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:17px 18px;box-shadow:0 1px 2px #15223808}.kpi-top{display:flex;align-items:center;justify-content:space-between;color:var(--muted);font-size:11px;font-weight:700}.kpi-top i{width:9px;height:9px;border-radius:50%;background:var(--tone)}.kpi strong{display:block;font-size:27px;line-height:1.1;margin:9px 0 5px;letter-spacing:-.03em}.kpi small{color:var(--muted);font-size:10px}.tone-blue{--tone:var(--blue)}.tone-violet{--tone:var(--violet)}.tone-green{--tone:var(--green)}.tone-amber{--tone:var(--amber)}.tone-red{--tone:var(--red)}.section-grid{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(320px,.8fr);gap:16px;margin-top:16px}.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);overflow:hidden}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;padding:20px 22px 14px}.panel-head h2{font-size:18px;letter-spacing:-.02em;margin:2px 0}.panel-head p{color:var(--muted);font-size:11px;margin:0}.panel-body{padding:0 22px 22px}.pipeline{display:grid;grid-template-columns:repeat(5,1fr);gap:7px}.stage{position:relative;background:#f7f8fb;border:1px solid #edf0f4;border-radius:11px;padding:12px}.stage span{text-transform:capitalize;color:var(--muted);font-size:10px}.stage strong{display:block;font-size:23px;margin-top:3px}.stage-interviewing{background:#edf2ff}.stage-offer{background:var(--green-soft)}.stage-rejected,.stage-withdrawn{background:#faf5f5}.microstats{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--line);border-radius:11px;margin-top:10px;overflow:hidden}.microstats div{padding:11px 12px;border-right:1px solid var(--line)}.microstats div:last-child{border:0}.microstats span{display:block;color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.06em}.microstats strong{font-size:15px}.chart-title{display:flex;align-items:center;justify-content:space-between;margin:18px 0 6px}.chart-title b{font-size:11px}.chart-title span{color:var(--muted);font-size:9px}.weeks{height:126px;display:flex;align-items:end;gap:7px;border-bottom:1px solid var(--line);padding:4px 4px 0}.week{height:100%;flex:1;min-width:28px;display:flex;flex-direction:column;align-items:center;justify-content:end}.bar-shell{width:72%;height:78px;display:flex;align-items:end;justify-content:center}.bar-shell i{display:block;width:100%;max-width:36px;min-height:3px;background:linear-gradient(180deg,#5b8ff7,var(--blue));border-radius:5px 5px 1px 1px}.week b{font-size:10px;margin-top:2px}.week small{font-size:8px;color:var(--muted)}.lane-list{display:grid;gap:12px;padding:0 22px 20px}.lane-row{display:grid;grid-template-columns:minmax(130px,1fr) minmax(70px,.7fr) 44px;gap:10px;align-items:center}.lane-row div:first-child b,.lane-row div:first-child span{display:block}.lane-row div:first-child b{font-size:11px}.lane-row div:first-child span{font-size:9px;color:var(--muted)}.lane-row>strong{font-size:10px;text-align:right}.lane-meter{height:5px;background:#edf0f4;border-radius:99px;overflow:hidden}.lane-meter i{display:block;height:100%;background:#8aa7df;border-radius:99px}.full{margin-top:16px}.follow-list{display:grid;gap:10px;padding:0 22px 22px}.follow-card{display:grid;grid-template-columns:70px minmax(0,1fr) auto;align-items:center;gap:15px;border:1px solid var(--line);border-radius:12px;padding:13px;background:#fffaf2}.age{width:58px;height:58px;border-radius:12px;background:var(--amber-soft);color:#8b4f07;display:grid;place-content:center;text-align:center}.age strong{font-size:20px;line-height:1}.age span{font-size:8px;margin-top:3px}.follow-main h3{font-size:14px;margin:2px 0}.follow-main p{color:var(--muted);font-size:11px;margin:0}.text-link{color:var(--blue);text-decoration:none;font-size:11px;font-weight:700;white-space:nowrap}.relationship-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;padding:0 22px 22px}.relationship-card{display:grid;grid-template-columns:38px minmax(0,1fr);gap:11px;border:1px solid var(--line);border-radius:12px;padding:13px}.avatar{width:36px;height:36px;border-radius:11px;background:var(--blue-soft);color:var(--blue);display:grid;place-items:center;font-weight:800}.relationship-meta{display:flex;justify-content:space-between;gap:10px;color:var(--muted);font-size:8px;text-transform:uppercase;letter-spacing:.06em}.relationship-card h3{font-size:12px;margin:3px 0 0}.relationship-card p{font-size:10px;color:var(--muted);margin:1px 0}.relationship-card small{display:block;color:#495a71;font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.opportunity-tools{display:flex;flex-wrap:wrap;align-items:center;gap:9px;padding:0 22px 15px}.search{position:relative;flex:1;min-width:220px}.search input{width:100%;height:38px;border:1px solid var(--line);border-radius:9px;padding:0 11px 0 34px;background:#fafbfc;color:var(--ink)}.search svg{position:absolute;left:11px;top:11px;width:16px;height:16px;stroke:var(--muted);fill:none}.chips{display:flex;gap:5px}.chip{border:1px solid var(--line);background:#fff;color:var(--muted);border-radius:8px;padding:8px 10px;font-size:10px;font-weight:700;cursor:pointer}.chip.active{background:var(--navy-2);border-color:var(--navy-2);color:#fff}select{height:38px;border:1px solid var(--line);border-radius:9px;background:#fff;color:var(--ink);padding:0 28px 0 10px;font-size:10px}.results{color:var(--muted);font-size:10px;margin-left:auto}.table-wrap{max-height:760px;overflow:auto;border-top:1px solid var(--line)}table{width:100%;border-collapse:collapse}thead{position:sticky;top:0;z-index:2;background:#f9fafc}th{text-align:left;color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.07em;padding:10px 14px;border-bottom:1px solid var(--line)}td{padding:13px 14px;border-bottom:1px solid #edf0f4;vertical-align:middle}tbody tr:hover{background:#fbfcff}.score-cell{width:88px}.score-cell>strong{font-size:15px}.score-cell>span{display:block;width:52px;height:4px;background:#e8ebf1;border-radius:99px;margin-top:5px}.score-cell i{display:block;height:100%;background:var(--blue);border-radius:99px}.role-cell{min-width:300px}.role-cell>div{display:flex;align-items:center;gap:6px}.role-cell a{color:var(--ink);text-decoration:none;font-weight:750}.role-cell a:hover{color:var(--blue)}.external{color:var(--muted);font-size:10px;margin-left:4px}.role-cell>b{display:inline-block;color:var(--muted);font-size:10px;margin-top:3px}.new-badge{background:var(--blue-soft);color:var(--blue);font-size:8px;text-transform:uppercase;font-weight:800;padding:2px 5px;border-radius:4px}.history{display:block;width:max-content;background:var(--amber-soft);color:#87510c;font-size:8px;padding:2px 6px;border-radius:4px;margin-top:4px}.gate,.health{display:inline-block;border-radius:99px;padding:3px 7px;font-size:8px;font-weight:800;text-transform:uppercase;letter-spacing:.04em}.gate.qualified,.health.healthy{background:var(--green-soft);color:#087055}.gate.verify,.health.dormant{background:var(--amber-soft);color:#88520d}.health.failing{background:var(--red-soft);color:#a62f38}.lane{display:block;color:var(--muted);font-size:9px;margin-top:4px}.primary-detail{display:block;font-size:10px}.primary-detail+small{display:block;color:var(--muted);font-size:9px;margin-top:3px}.source-summary{display:flex;gap:7px}.source-summary span{border:1px solid var(--line);border-radius:8px;padding:5px 8px;font-size:9px;color:var(--muted)}.issue-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;padding:0 22px 22px}.issue-card{border:1px solid #f0d8da;background:#fffafb;border-radius:11px;padding:12px}.issue-card>div{display:flex;align-items:center;gap:7px}.issue-card b{font-size:10px}.status-dot{width:7px;height:7px;border-radius:50%;background:var(--red)}.issue-card>strong{display:block;color:var(--red);font-size:9px;margin-top:8px}.issue-card p{color:#5d6878;font-size:9px;min-height:26px;margin:4px 0}.issue-card small{color:var(--muted);font-size:8px}.source-details{border-top:1px solid var(--line)}details>summary{cursor:pointer;padding:14px 22px;font-weight:700;font-size:11px;list-style:none}details>summary::-webkit-details-marker{display:none}details>summary:after{content:"＋";float:right;color:var(--muted)}details[open]>summary:after{content:"−"}.source-table{max-height:520px;overflow:auto;border-top:1px solid var(--line)}.source-table td{font-size:9px}.empty-state{grid-column:1/-1;min-height:140px;display:grid;place-items:center;align-content:center;text-align:center;color:var(--muted);padding:24px}.empty-state.compact{min-height:100px}.empty-state b,.empty-state span{display:block}.empty-state b{color:var(--ink);font-size:12px}.empty-state span{font-size:10px;margin-top:3px}.empty-icon{width:32px;height:32px;border-radius:50%;display:grid;place-items:center;background:var(--green-soft);color:var(--green);margin-bottom:8px;font-weight:800}.footer{display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:9px;padding:24px 3px}.footer b{color:var(--ink)}button,input,select,a{outline:none}button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible{box-shadow:0 0 0 3px #2f6fed33;border-radius:5px}
@media(max-width:1120px){.app{grid-template-columns:210px minmax(0,1fr)}.kpis{grid-template-columns:repeat(3,1fr)}.relationship-grid,.issue-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:820px){.app{display:block}.sidebar{position:static;height:auto;padding:14px 16px}.brand{padding:0 4px 13px}.brand-mark{width:32px;height:32px}.nav{display:flex;overflow:auto;padding-bottom:2px}.nav a{white-space:nowrap}.nav a span:first-child{gap:6px}.nav svg{display:none}.privacy{display:none}.topbar{height:58px;padding:0 18px}.topbar>div:first-child{display:none}main{padding:18px}.hero{padding:24px}.hero h1{font-size:26px}.kpis{grid-template-columns:repeat(2,1fr)}.section-grid{grid-template-columns:1fr}.relationship-grid,.issue-grid{grid-template-columns:repeat(2,1fr)}.role-cell{min-width:250px}.footer{display:block}.footer span{display:block;margin-top:5px}}
@media(max-width:520px){.nav a{padding:8px 9px;font-size:11px}.nav-badge{display:none}.hero{border-radius:14px;padding:20px}.hero h1{font-size:24px}.hero-actions{display:grid}.kpis{grid-template-columns:1fr 1fr}.kpi{padding:13px}.kpi strong{font-size:23px}.kpi:last-child{grid-column:1/-1}.pipeline{grid-template-columns:repeat(2,1fr)}.stage:last-child{grid-column:1/-1}.microstats{grid-template-columns:1fr}.microstats div{border-right:0;border-bottom:1px solid var(--line)}.weeks{overflow-x:auto}.week{min-width:34px}.follow-card{grid-template-columns:58px minmax(0,1fr)}.follow-card .text-link{grid-column:2}.relationship-grid,.issue-grid{grid-template-columns:1fr}.opportunity-tools{align-items:stretch}.chips{order:2}.results{order:3;margin:0}.search{min-width:100%}.panel-head{padding:17px}.panel-body,.lane-list,.follow-list,.relationship-grid,.issue-grid{padding-left:17px;padding-right:17px}.topbar .run-status span{display:none}.source-summary{display:none}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""
    script = """
const rows=[...document.querySelectorAll('#job-rows tr[data-search]')];
const query=document.getElementById('job-search');
const result=document.getElementById('result-count');
const sort=document.getElementById('job-sort');
const chips=[...document.querySelectorAll('[data-filter-gate]')];
let gate='';
function renderJobs(){
  const needle=query.value.trim().toLowerCase();
  let visible=0;
  rows.forEach(row=>{const show=(!gate||row.dataset.gate===gate)&&(!needle||row.dataset.search.includes(needle));row.hidden=!show;if(show)visible++;});
  result.textContent=`${visible} of ${rows.length} roles`;
}
function sortJobs(){
  const mode=sort.value;
  rows.sort((a,b)=>mode==='company'?a.dataset.company.localeCompare(b.dataset.company):mode==='newest'?b.dataset.first.localeCompare(a.dataset.first):Number(b.dataset.score)-Number(a.dataset.score));
  const body=document.getElementById('job-rows');rows.forEach(row=>body.appendChild(row));
}
query.addEventListener('input',renderJobs);
sort.addEventListener('change',()=>{sortJobs();renderJobs();});
chips.forEach(chip=>chip.addEventListener('click',()=>{gate=chip.dataset.filterGate;chips.forEach(x=>x.classList.toggle('active',x===chip));renderJobs();}));
sortJobs();renderJobs();
"""
    icons = {
        "overview": '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
        "pipeline": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/></svg>',
        "followups": '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
        "relationships": '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="3"/><path d="M3 20c0-4 2-7 6-7s6 3 6 7M17 11c2.5.3 4 2.3 4 5"/></svg>',
        "opportunities": '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="6" width="18" height="14" rx="2"/><path d="M8 6V4h8v2M3 11h18M10 11v2h4v-2"/></svg>',
        "sources": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-5 4 10 2-5h6"/><circle cx="12" cy="12" r="9"/></svg>',
    }
    nav = "".join((
        f'<a href="#overview"><span>{icons["overview"]}Overview</span></a>',
        f'<a href="#pipeline"><span>{icons["pipeline"]}Pipeline</span><span class="nav-badge">{s["active"]}</span></a>',
        f'<a href="#follow-ups"><span>{icons["followups"]}Follow-ups</span><span class="nav-badge alert">{s["follow_up_due"]}</span></a>',
        f'<a href="#relationships"><span>{icons["relationships"]}Relationships</span><span class="nav-badge">{len(relationships)}</span></a>',
        f'<a href="#opportunities"><span>{icons["opportunities"]}Opportunities</span><span class="nav-badge">{len(opportunities)}</span></a>',
        f'<a href="#sources"><span>{icons["sources"]}Source health</span><span class="nav-badge alert">{len(failing)}</span></a>',
    ))
    thread_word = plural(s["follow_up_due"], "thread")
    thread_verb = "needs" if s["follow_up_due"] == 1 else "need"
    match_word = plural(new_last_run, "match", "matches")
    note_word = plural(len(relationships), "note")
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; object-src 'none'">
<title>CareerKit command center — {esc(snapshot['as_of'])}</title><style>{style}</style></head><body>
<a class="skip" href="#main">Skip to content</a><div class="app"><aside class="sidebar"><div class="brand"><div class="brand-mark">CK</div><div><strong>CareerKit</strong><span>Command center</span></div></div><nav class="nav" aria-label="Dashboard sections">{nav}</nav><div class="privacy"><b>Private by design</b><span>One local file. No telemetry, remote assets, or hosted account.</span></div></aside>
<div class="content"><header class="topbar"><div><b>Career intelligence</b><span>Last engine run: {last_run_text}</span></div><div class="run-status"><i></i><div><b>Local data</b><span>Updated {esc(generated[:16].replace('T',' '))}</span></div></div></header>
<main id="main"><section class="hero" id="overview"><div class="eyebrow">Search brief · {esc(snapshot['as_of'])}</div><h1>Your job search, in focus.</h1><p>{len(opportunities)} roles are ready for review, {new_last_run} arrived in the latest run, and {s['follow_up_due']} active {thread_word} {thread_verb} attention.</p><div class="hero-actions"><a class="button primary" href="#opportunities">Review {new_last_run} new {match_word}</a><a class="button secondary" href="#follow-ups">Open follow-up queue</a></div></section>
<section class="kpis" aria-label="Search metrics">{kpis}</section>
<section class="section-grid" id="pipeline"><article class="panel"><div class="panel-head"><div><div class="eyebrow">Application funnel</div><h2>Current pipeline</h2><p>Current state, response quality, and eight-week cadence.</p></div></div><div class="panel-body"><div class="pipeline">{stage_cells}</div><div class="microstats"><div><span>Known response</span><strong>{display_pct(s['known_response_rate'])}</strong></div><div><span>Median to interview</span><strong>{display_days(s['median_days_to_interview'])}</strong></div><div><span>Median to outcome</span><strong>{display_days(s['median_days_to_outcome'])}</strong></div></div><div class="chart-title"><b>Applications by week</b><span>week beginning</span></div><div class="weeks">{weekly_cells}</div></div></article>
<article class="panel"><div class="panel-head"><div><div class="eyebrow">Signal quality</div><h2>Conversion by lane</h2><p>Use sustained differences—not one result—to tune criteria.</p></div></div><div class="lane-list">{lane_rows}</div></article></section>
<section class="panel full" id="follow-ups"><div class="panel-head"><div><div class="eyebrow">Next actions</div><h2>Follow-up queue</h2><p>Active threads silent for {snapshot['follow_up_days']} days or longer.</p></div><span class="gate verify">{s['follow_up_due']} due</span></div><div class="follow-list">{''.join(follow_cards)}</div></section>
<section class="panel full" id="relationships"><div class="panel-head"><div><div class="eyebrow">Long-term memory</div><h2>Relationship memory</h2><p>Recruiters, referrals, invitations, and context that outlive one requisition.</p></div><span class="source-summary"><span>{len(relationships)} {note_word}</span></span></div><div class="relationship-grid">{''.join(relationship_cards)}</div></section>
<section class="panel full" id="opportunities"><div class="panel-head"><div><div class="eyebrow">Decision queue</div><h2>Actionable opportunities</h2><p>Ranked by the transparent rules in your profile. VERIFY means evidence is incomplete.</p></div></div><div class="opportunity-tools"><label class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input id="job-search" aria-label="Search opportunities" placeholder="Search role, employer, location, lane…"></label><div class="chips" aria-label="Filter by gate"><button class="chip active" data-filter-gate="">All</button><button class="chip" data-filter-gate="QUALIFIED">Qualified</button><button class="chip" data-filter-gate="VERIFY">Verify</button></div><label><span class="skip">Sort opportunities</span><select id="job-sort"><option value="score">Highest score</option><option value="newest">Newest first</option><option value="company">Company A–Z</option></select></label><span class="results" id="result-count" aria-live="polite"></span></div><div class="table-wrap"><table><thead><tr><th>Score</th><th>Role</th><th>Decision</th><th>Location & comp</th><th>Provenance</th></tr></thead><tbody id="job-rows">{''.join(job_rows)}</tbody></table></div></section>
<section class="panel full" id="sources"><div class="panel-head"><div><div class="eyebrow">Coverage integrity</div><h2>Source health</h2><p>Issues first. Dormant optional integrations are not failures.</p></div><div class="source-summary"><span>{healthy} healthy</span><span>{len(dormant)} dormant</span><span>{len(failing)} failing</span></div></div><div class="issue-grid">{issue_cards}</div><details class="source-details"><summary>Inspect all {len(health)} sources</summary><div class="source-table"><table><thead><tr><th>Source</th><th>Status</th><th>Last OK</th><th>Count</th><th>Last error</th></tr></thead><tbody>{''.join(health_rows)}</tbody></table></div></details></section>
<footer class="footer"><b>CareerKit · local command center</b><span>Generated {esc(generated)} · no telemetry, remote fonts, analytics, or network-loaded scripts.</span></footer></main></div></div><script>{script}</script></body></html>'''
    path.write_text(page, encoding="utf-8")
    return path


def write_json(snapshot: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return p
