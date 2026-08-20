"""What the user has already applied to, and what already said no.

The defect this exists for: on 2026-08-06 the tool recommended a role as a fresh
find. The owner had applied to it a week earlier and been rejected the previous
day. A wider check found roughly twenty applications in forty five days against
six the database knew about, and five with no trace anywhere at all.

A job search tool that cannot see this is not merely incomplete, it is
confidently wrong in the most expensive direction: it spends the user's
attention on doors that are already shut, and if they act on it, their
credibility with an employer who has already declined them.

Design note. This deliberately does NOT talk to a mail provider. Ingesting a
mailbox means credentials, scopes, refresh tokens and a privacy surface in a
tool that otherwise keeps everything local. Instead the engine consumes an
evidence file that anything can write:

    profile/applications.jsonl

one JSON object per line:

    {"company": "Delta Air Lines", "title": "Business Technology Product Owner",
     "status": "rejected", "on": "2026-08-05", "source": "gmail",
     "evidence": "Thank you for your interest in the ... (#32177)"}

`title` may be null when the evidence does not name the role, which is common:
plenty of confirmations say only "thanks for applying to Acme". Those become
review candidates rather than automatic marks, because company-only matching
would mark the wrong requisition at any employer with more than one opening.

Claude populates this from whatever mail access the operator already has. A
script, a manual note, or an export works identically.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

VALID = ("applied", "rejected", "interviewing", "offer", "withdrawn")
PREFLIGHT = ("prepared",)
# SQLite's status is a report-suppression state, not the full application
# pipeline. `store.record_application_stage` owns that mapping so manual
# progress and evidence reconciliation cannot drift apart.
DB_STATUS = {
    "applied": "applied", "rejected": "rejected", "interviewing": "applied",
    "offer": "applied", "withdrawn": "applied",
}


def _norm(s: str) -> str:
    """Company names collapse: 'Georgia-Pacific LLC' and 'Georgia Pacific'."""
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|the|group|holdings|"
               r"international|technologies|technology|solutions|services)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_overlap(a: str, b: str) -> float:
    """Crude but legible. Shared words as a fraction of all words used, ignoring
    filler.

    Deliberately NOT "fraction of the SHORTER title", which is what this was.
    That inflates any short title against a long one: "Senior Manager, Success
    Architecture" is three words after stopwords, so sharing the two commonest
    words in a Salesforce search - "success" and "manager" - with "Customer
    Success Manager - Global Public Sector (State, Local Government and
    Education)" scored 2/3 = 67% and tripped the "you were rejected for X at
    this employer" warning on the highest-scoring role in the pipeline
    (2026-08-11). This check errs toward talking the user out of a role, so it
    has to be the harder direction to trip, not the easier one.

    Jaccard costs nothing real: a genuine repost shares nearly all its words and
    still clears the threshold, while two roles that merely sound alike do not.
    """
    stop = {"the", "a", "an", "of", "and", "for", "in", "at", "to", "senior", "sr",
            "junior", "jr", "staff", "lead", "principal", "ii", "iii", "i"}
    wa = {w for w in re.findall(r"[a-z0-9]+", (a or "").lower()) if w not in stop}
    wb = {w for w in re.findall(r"[a-z0-9]+", (b or "").lower()) if w not in stop}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def load_evidence(path: str | Path) -> list[dict]:
    """Read applications.jsonl, skipping malformed lines rather than dying.

    A single bad line in an evidence file must not stop a search. Bad lines are
    returned as problems by `check` instead."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out.append({"_bad": True, "_line": i, "_raw": line[:120]})
            continue
        # JSONL evidence is one object per line.  Valid JSON scalars and arrays
        # used to reach the assignment below and raw-crash with TypeError (or,
        # for null, "NoneType does not support item assignment").  Treat a
        # wrong-shaped JSON value exactly like malformed JSON so `applied` can
        # report the bad line and continue reconciling the rest of the file.
        if not isinstance(rec, dict):
            out.append({"_bad": True, "_line": i, "_raw": line[:120]})
            continue
        rec["_line"] = i
        out.append(rec)
    return out


def reconcile(con: sqlite3.Connection, evidence: list[dict], *,
              apply: bool = False, min_title_overlap: float = 0.6) -> dict:
    """Match evidence to stored postings.

    Returns three buckets. `matched` is safe to write. `ambiguous` is evidence
    that identifies a company but not which requisition, which is exactly the
    case that must never be auto-applied. `unmatched` is evidence for a company
    that is not in the database at all, which is itself a finding: it means the
    user applied somewhere the tool never surfaced.
    """
    matched, ambiguous, unmatched, pending, problems = [], [], [], [], []
    rows = con.execute("SELECT uid, company, title, status FROM jobs").fetchall()
    by_company: dict[str, list] = {}
    for r in rows:
        by_company.setdefault(_norm(r["company"]), []).append(r)

    for rec in evidence:
        if rec.get("_bad"):
            problems.append(f"line {rec['_line']}: not valid JSON: {rec['_raw']}")
            continue
        status = (rec.get("status") or "").lower()
        if status in PREFLIGHT:
            pending.append({**rec, "why": "prepared is pre-submission; database unchanged"})
            continue
        if status not in VALID:
            problems.append(f"line {rec.get('_line')}: unknown status {status!r}")
            continue
        company = rec.get("company") or ""
        candidates = by_company.get(_norm(company), [])
        if not candidates:
            unmatched.append({**rec, "why": "no posting from this employer in the database"})
            continue

        title = rec.get("title")
        if not title:
            if len(candidates) == 1:
                matched.append({**rec, "uid": candidates[0]["uid"],
                                "matched_title": candidates[0]["title"],
                                "confidence": "company only, single posting"})
            else:
                ambiguous.append({**rec, "candidates": [
                    {"uid": c["uid"], "title": c["title"]} for c in candidates],
                    "why": f"evidence names no role and {len(candidates)} are stored"})
            continue

        scored = sorted(((_title_overlap(title, c["title"]), c)
                         for c in candidates), key=lambda t: t[0])
        best_score, best = scored[-1]
        runner = scored[-2][0] if len(scored) > 1 else 0.0
        if best_score >= min_title_overlap and best_score - runner >= 0.15:
            matched.append({**rec, "uid": best["uid"], "matched_title": best["title"],
                            "confidence": f"title overlap {best_score:.0%}"})
        elif best_score >= min_title_overlap:
            ambiguous.append({**rec, "candidates": [
                {"uid": c["uid"], "title": c["title"]} for s, c in scored if s >= min_title_overlap],
                "why": "two stored postings match the title about equally well"})
        else:
            unmatched.append({**rec, "why": f"best stored title only {best_score:.0%} similar "
                                            f"({best['title'][:60]!r})"})

    for item in matched:
        item["db_status"] = DB_STATUS[item["status"]]

    if apply and matched:
        from engine import store
        for m in matched:
            # Empty notes are deliberate: store.set_status preserves whatever
            # free-form note is already on the row. The former direct UPDATE
            # replaced it with generated evidence text.
            detail = (f"{m.get('on') or ''} from "
                      f"{m.get('source') or 'applications.jsonl'}").strip()
            try:
                store.record_application_stage(
                    con, m["uid"], m["status"], on=m.get("on"), detail=detail)
            except ValueError as e:
                problems.append(f"line {m.get('_line')}: {e}")

    return {"matched": matched, "ambiguous": ambiguous,
            "unmatched": unmatched, "pending": pending, "problems": problems}


def surfacing_a_closed_door(con: sqlite3.Connection) -> list[str]:
    """Rows the report would show that the user has already been rejected for.

    The Delta case, generalised: same employer, similar title, already declined.
    Worth warning about even when the requisition id differs, because employers
    repost, and a repost of a role you were declined for is not a fresh find.
    """
    out = []
    rejected = con.execute(
        "SELECT company, title FROM jobs WHERE status='rejected'").fetchall()
    if not rejected:
        return out
    live = con.execute(
        "SELECT uid, company, title, gate FROM jobs "
        "WHERE gate IN ('QUALIFIED','VERIFY') AND status='new'").fetchall()
    for l in live:
        for rj in rejected:
            if _norm(l["company"]) != _norm(rj["company"]):
                continue
            overlap = _title_overlap(l["title"], rj["title"])
            if overlap >= 0.6:
                # A likely repost of the role itself. Worth stopping for.
                out.append(("problem",
                            f"{l['company']} / {l['title'][:55]}: you were rejected for "
                            f"{rj['title'][:55]!r} at this employer "
                            f"({overlap:.0%} title overlap)"))
            elif overlap > 0:
                # Merely the same employer. True, and useful context, but it is
                # not a defect in the row and doctor listing it as a PROBLEM
                # reads as "do not bother" - which is how the highest-scoring
                # role in the pipeline came to be flagged (2026-08-11).
                out.append(("note",
                            f"{l['company']} / {l['title'][:55]}: previously rejected by "
                            f"this employer for a different role"))
            break
    return out


def write_evidence(path: str | Path, records: list[dict]) -> int:
    """Append records, skipping ones already present. Returns the number written."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    for rec in load_evidence(p):
        if not rec.get("_bad"):
            seen.add((_norm(rec.get("company", "")), (rec.get("title") or "").lower(),
                      rec.get("status")))
    n = 0
    with p.open("a", encoding="utf-8") as fh:
        for rec in records:
            key = (_norm(rec.get("company", "")), (rec.get("title") or "").lower(),
                   rec.get("status"))
            if key in seen:
                continue
            seen.add(key)
            rec.setdefault("on", date.today().isoformat())
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            n += 1
    return n
