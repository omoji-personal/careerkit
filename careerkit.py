#!/usr/bin/env python3
"""CareerKit sourcing CLI. Personal state lives in profile/ (gitignored).

    ./careerkit.py pull                poll every registered employer + feed, score, report
    ./careerkit.py pull --employers    employer ATS boards only
    ./careerkit.py pull --feeds        aggregator feeds only
    ./careerkit.py discover NAME [...] probe a company across every guessable ATS, register hits
    ./careerkit.py discover --file f   one company name per line
    ./careerkit.py verify              confirm each board belongs to the right company
    ./careerkit.py ingest-urls FILE    resolve pasted job URLs -> employers, register
    ./careerkit.py audit [--grep RX]   re-fetch + re-score, show every kill and why
    ./careerkit.py report [--format json|csv]   rebuild the report, or export rows
    ./careerkit.py rescore             re-judge stored postings after a criteria change
    ./careerkit.py doctor              one check: profile, sources, freshness, drift
    ./careerkit.py status | mark UID STATUS
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _use_venv() -> None:
    """Re-exec inside .venv when one exists and we are not already in it.

    setup.sh installs dependencies into .venv (system Python refuses under PEP
    668). Without this, running ./careerkit.py directly picks the system
    interpreter and dies on `import yaml`, which reads as "the tool is broken"
    rather than "wrong interpreter"."""
    import os
    if os.environ.get("CAREERKIT_VENV") == "1":
        return
    root = Path(__file__).resolve().parent
    venv = root / ".venv"
    exe = venv / ("Scripts" if os.name == "nt" else "bin") / \
        ("python.exe" if os.name == "nt" else "python")
    if exe.exists() and Path(sys.prefix).resolve() != venv.resolve():
        os.environ["CAREERKIT_VENV"] = "1"
        try:
            os.execv(str(exe), [str(exe), str(Path(__file__).resolve()), *sys.argv[1:]])
        except OSError as e:
            sys.exit(f"Could not start the virtual environment at {venv}: {e}\n"
                     "Delete the .venv folder and run ./setup.sh again.")


# Only when run AS A PROGRAM. At import time this re-exec'd the interpreter with
# the importing script's argv, so `import careerkit` from any other script died
# with "the following arguments are required: cmd". Tests never caught it because
# pytest already runs inside .venv, where the re-exec is a no-op.
if __name__ == "__main__":
    _use_venv()

try:
    import yaml  # noqa: E402
except ModuleNotFoundError as _e:  # pragma: no cover - environment failure
    sys.exit(f"Missing dependency: {_e.name}.\n"
             "Run ./setup.sh (it builds .venv and installs what CareerKit needs).\n"
             "If you already did, delete the .venv folder and run it again.")

from engine import http as _http
from engine import store  # noqa: E402
from engine import aggregators  # noqa: E402
from engine import adapters as _adapters
from engine import discover as _discover
from engine.discover import discover_many  # noqa: E402
from engine import pull as _pull
from engine.score import Profile, ProfileError, score  # noqa: E402
from engine import score as _score_mod
from engine import search as _search
from engine.search import resolve, workday_parts  # noqa: E402
from engine.verify import verify_entry  # noqa: E402

# CAREERKIT_HOME is what makes `new-instance.sh` work: several instances share
# this one script and each keeps its own profile, database and reports. store.py
# and report.py already honoured it; this file did not, so pointing the CLI at
# another instance read that instance's DATABASE while scoring it against the
# REPO's profile. Silent, and exactly the wrong direction: the numbers looked
# real because the postings were.
ROOT = Path(os.environ.get("CAREERKIT_HOME") or Path(__file__).resolve().parent)
PROFILE = ROOT / "profile" / "profile.yaml"
EMPLOYERS = ROOT / "profile" / "employers.yaml"
KEYS = ROOT / "profile" / "keys.yaml"
TRACKER = ROOT / "profile" / "tracker.md"


def load_yaml(p: Path, default):
    if not p.exists():
        return default
    return yaml.safe_load(p.read_text()) or default


def load_profile() -> Profile:
    if not PROFILE.exists():
        sys.exit("No profile yet. Run /setup in Claude Code first "
                 "(it interviews you and writes profile/profile.yaml).")
    return Profile.load(PROFILE)


def save_employers(data: dict) -> None:
    EMPLOYERS.parent.mkdir(parents=True, exist_ok=True)
    EMPLOYERS.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))




def cmd_pull(args) -> None:
    """Thin wrapper. The loop itself lives in engine/pull.py because a second
    front end (the author's personal sourcer.py) drives the same engine, and
    while the loop was copied into both they drifted without a symptom."""
    _http.set_cache_enabled(not getattr(args, "no_cache", False))
    profile = load_profile()
    aggregators.set_search_terms(profile.search_terms)
    _adapters.set_relevance_terms(profile.relevance_terms)
    _search.set_core_terms(profile.search_terms)
    reg = load_yaml(EMPLOYERS, {"employers": [], "feeds": []})
    keys = load_yaml(KEYS, {})
    con = store.connect()

    r = _pull.run_pull(con, reg, keys, profile,
                       employers_only=args.employers, feeds_only=args.feeds,
                       tier=args.tier, min_score=args.min_score,
                       discovered=take_discovered())
    path = r["path"]

    # Keep an immutable artifact per run. The dated filename is overwritten by a
    # second run on the same day, which loses the evidence of what the first one
    # actually surfaced.
    try:
        import shutil
        runs_dir = path.parent / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        shutil.copy2(path, runs_dir / f"{stamp}-run-{r['run_id']:04d}.md")
        shutil.copy2(path, path.parent / "latest.md")
    except Exception as e:
        print(f"  (could not write the run artifact: {e})")

    print(f"\n  pulled      {r['pulled']}")
    print(f"  in-family   {r['kept']}")
    print(f"  screened    {r['pulled'] - r['kept']}   ("
          f"{', '.join(f'{k} {v}' for k, v in r['excluded'].most_common(5))})")
    print(f"  NEW         {r['new']}")
    print(f"  qualified   {r['qualified']}")
    if r["delisted"] or r["demoted"]:
        print(f"  closed out  {r['delisted']} delisted, {r['demoted']} no longer qualify")
    dropped = store.dropped_to_zero(con)
    if dropped:
        print(f"\n  !! {len(dropped)} source(s) returned 0 but had postings last run "
              f"(possible schema change, not an outage):")
        for d in dropped[:10]:
            print(f"     {d['source']:<44} was {d['prev_count']}")
    print(f"\nReport: {path}")


def cmd_audit(args) -> None:
    """The calibration loop: re-poll every board, re-score, and show what got
    KILLED and why - so silent false negatives get caught, not discovered
    months later. Use --grep to focus (e.g. --grep 'product manager').

    Boards answered within the last 6 hours come from the HTTP cache, so this
    audits the scoring rules against the data a pull would see. Pass --no-cache
    to really re-fetch, which is what you want when auditing whether a posting
    is still live rather than whether the gates judged it correctly."""
    # This assignment used to sit ABOVE the string, which made the string an
    # expression statement rather than a docstring: --help showed nothing.
    _http.set_cache_enabled(not getattr(args, "no_cache", False))
    profile = load_profile()
    aggregators.set_search_terms(profile.search_terms)
    _adapters.set_relevance_terms(profile.relevance_terms)
    _search.set_core_terms(profile.search_terms)
    reg = load_yaml(EMPLOYERS, {"employers": [], "feeds": []})
    keys = load_yaml(KEYS, {})
    all_jobs = _pull.fetch_all(reg, keys, None, employers_only=args.employers,
                               feeds_only=False)["jobs"]
    import re as _re
    flt = _re.compile(args.grep, _re.I) if args.grep else None
    kills = Counter()
    samples: dict[str, list] = {}
    for j in all_jobs:
        score(j, profile)
        if j.gate not in ("EXCLUDED", "SLOT-BLOCKED"):
            continue
        if flt and not flt.search(j.title):
            continue
        key = (j.reasons or ["?"])[0].split(":")[0][:44]
        kills[key] += 1
        samples.setdefault(key, [])
        if len(samples[key]) < args.samples:
            samples[key].append(j)
    print(f"\n=== AUDIT: {sum(kills.values())} kills"
          + (f" matching /{args.grep}/" if args.grep else "") + " ===")
    for reason, n in kills.most_common():
        print(f"\n[{n}] {reason}")
        for j in samples[reason]:
            print(f"    {j.company[:24]:<25} {j.title[:56]:<57} {(j.location or '')[:28]}")
    print("\nIf a kill looks wrong, fix profile/profile.yaml (lanes, exclusions,"
          "\nmetros, floors) and re-run. The profile is the only place rules live.")


def cmd_discover(args) -> None:
    reg = load_yaml(EMPLOYERS, {"employers": [], "feeds": []})
    known = {(e.get("slug") or e.get("tenant"), e.get("ats")) for e in reg["employers"]}
    known_names = {e["name"].lower() for e in reg["employers"]}
    names = list(args.names)
    if args.file:
        names += [l.strip() for l in Path(args.file).read_text().splitlines()
                  if l.strip() and not l.startswith("#")]
    todo = [n for n in names if n.lower() not in known_names]
    print(f"Probing {len(todo)} companies across {_discover.PROBEABLE} ATS platforms...",
          flush=True)
    found, missed = [], []

    def on_result(name, entry):
        if entry:
            print(f"  + {name:<42} {entry['ats']}:"
                  f"{entry.get('slug') or entry.get('tenant')} ({entry['open_roles']} open)", flush=True)
        else:
            missed.append(name)
            print(f"  - {name:<42} no public board found", flush=True)

    for entry in discover_many(todo, lane=args.lane, tier=args.tier or "C",
                               on_result=on_result):
        if (entry.get("slug") or entry.get("tenant"), entry["ats"]) not in known:
            known.add((entry.get("slug") or entry.get("tenant"), entry["ats"]))
            reg["employers"].append(entry)
            found.append(entry)
    save_employers(reg)
    queue_discovered(found)
    print(f"\n{len(found)} registered, {len(missed)} not found. "
          f"Registry now holds {len(reg['employers'])} employers.")
    if missed:
        print("Not found (may post only on LinkedIn, or use an unguessable slug):")
        for m in missed:
            print(f"  {m}")
        # "Not found" reads as "this employer has no public board", which is
        # wrong for the four platforms addressed by an opaque tenant id. There
        # is nothing to guess from a company name, but a posting URL contains
        # the id, so the path forward exists and should be said out loud.
        print(f"\n  {', '.join(_discover.UNPROBEABLE)} cannot be probed from a name "
              f"(they use opaque tenant ids).\n"
              f"  If one of these is the employer's board, paste any posting URL:\n"
              f"    ./careerkit.py ingest-urls FILE")


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _safe_url(u: str) -> tuple[str, str]:
    """Return (url, "") if usable, else ("", reason).

    URLs arrive from job boards, recruiter emails and whatever the user pasted,
    so they are untrusted input. This never shells out, but validating here
    means a malformed or hostile string is refused with a reason instead of
    flowing onward as a half-parsed registry entry."""
    u = (u or "").strip()
    if not u:
        return "", "empty"
    if _CTRL.search(u):
        return "", "contains control characters"
    if u.startswith("-"):
        return "", "starts with '-' (would read as a flag)"
    from urllib.parse import urlparse
    try:
        parts = urlparse(u)
    except Exception as e:
        return "", f"unparseable ({type(e).__name__})"
    if parts.scheme not in ("https", "http"):
        return "", f"scheme {parts.scheme or 'missing'!r}, expected https"
    if not parts.netloc:
        return "", "no host"
    return u, ""


def cmd_ingest_url(args) -> None:
    """Register ONE url passed as an argument. No temp file, no shell string."""
    url, why = _safe_url(args.url)
    if not url:
        sys.exit(f"Refused: {why}\n  {args.url[:120]!r}")
    _ingest([url])


def cmd_ingest_urls(args) -> None:
    path = Path(args.file)
    if not path.exists():
        sys.exit(f"File not found: {path}")
    _ingest([l.strip() for l in path.read_text().splitlines() if l.strip()])


def _ingest(raw_urls: list[str]) -> None:
    reg = load_yaml(EMPLOYERS, {"employers": [], "feeds": []})
    known = {(e.get("slug") or e.get("tenant"), e.get("ats")) for e in reg["employers"]}
    added, skipped = 0, []
    for raw in raw_urls:
        u, why = _safe_url(raw)
        if not u:
            skipped.append((raw, why))
            continue
        h = resolve(u)
        if not h.ats or not h.slug:
            # Was a silent `continue`, so the documented LinkedIn/Indeed coverage
            # path dropped exactly the platforms resolve() cannot parse and the
            # user believed the employer had been registered.
            skipped.append((u, "no adapter recognises this URL shape"))
            continue
        if (h.slug, h.ats) in known:
            skipped.append((u, f"already registered ({h.ats}:{h.slug})"))
            continue
        entry = {"name": h.company_guess or h.slug, "ats": h.ats, "slug": h.slug,
                 "lane": "url-ingested", "tier": "C", "active": True}
        if h.ats == "workday":
            parts = workday_parts(u)
            if not parts:
                skipped.append((u, "workday URL missing tenant/datacenter/site"))
                continue
            entry.update(parts)
            entry.pop("slug", None)
        else:
            # Some platforms need config a job URL does not carry. Writing a
            # partial entry produces a KeyError at poll time, so say what is
            # missing and let the user add it rather than registering a board
            # that cannot be polled.
            need = _search.EXTRA_CONFIG.get(h.ats, ())
            missing = [k for k in need if k not in entry]
            if missing:
                skipped.append((u, f"{h.ats} board found (slug {h.slug}) but needs "
                                   f"{', '.join(missing)} added by hand in "
                                   f"profile/employers.yaml"))
                continue
        known.add((h.slug, h.ats))
        reg["employers"].append(entry)
        added += 1
        print(f"  + {entry['name']:<34} {h.ats}:{h.slug}")
    save_employers(reg)
    for u, why in skipped:
        print(f"  - skipped: {why}\n      {u[:110]}")
    print(f"\n{added} new employer(s) from {len(raw_urls)} URL(s); "
          f"{len(skipped)} skipped. Registry: {len(reg['employers'])}.")


def cmd_verify(args) -> None:
    from concurrent.futures import ThreadPoolExecutor
    reg = load_yaml(EMPLOYERS, {"employers": [], "feeds": []})
    emps = [e for e in reg["employers"] if args.all or e.get("verified") is None]
    print(f"Verifying {len(emps)} employer boards...", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(verify_entry, emps))
    counts = Counter(e.get("verified") for e in reg["employers"])
    save_employers(reg)
    for v, sym in (("bad", "X"), ("review", "?"), ("ok", "ok")):
        for e in reg["employers"]:
            if e.get("verified") == v:
                print(f"  {sym:<3} {e['name']:<38} {e['ats']}:"
                      f"{e.get('slug') or e.get('tenant')}  {e.get('verify_note','')}")
    print(f"\n{counts.get('ok',0)} verified, {counts.get('review',0)} need one look, "
          f"{counts.get('bad',0)} deactivated as wrong company.")


def cmd_report(args) -> None:
    con = store.connect()
    rows = store.query(con, min_score=args.min_score, limit=300)

    fmt = getattr(args, "format", "md")
    if fmt in ("json", "csv"):
        # The Markdown report is for reading. This is for everything else:
        # a spreadsheet, a diff between two runs, or your own analysis. The
        # database is the only place this data existed and SQL is not a
        # reasonable thing to ask a non-programmer for.
        out = OUT_DIR_FOR_EXPORT()
        out.mkdir(parents=True, exist_ok=True)
        cols = ["uid", "company", "title", "location", "score", "gate", "status",
                "source", "comp_min", "comp_max", "first_seen", "last_seen", "url"]
        path = out / f"export-{date.today().isoformat()}.{fmt}"
        if fmt == "json":
            path.write_text(json.dumps([{c: r[c] for c in cols} for r in rows], indent=1))
        else:
            import csv
            with path.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                for r in rows:
                    w.writerow([r[c] for c in cols])
        print(f"Exported {len(rows)} rows: {path}")
        return

    _pull.rebuild_report(con, min_score=args.min_score)


def OUT_DIR_FOR_EXPORT() -> Path:
    from engine.report import OUT_DIR
    return OUT_DIR


def cmd_claims_lint(args) -> None:
    """Mechanical second pass over a draft: numbers and names the register does
    not back. A cheap backstop for the truth rule, NOT a substitute for it."""
    from engine.claims import lint, format_report
    draft_p = Path(args.draft)
    reg_p = Path(args.register) if args.register else PROFILE.parent / "claims.md"
    if not draft_p.exists():
        sys.exit(f"No such draft: {draft_p}")
    if not reg_p.exists():
        sys.exit(f"No claims register at {reg_p}. It is written by /setup, and "
                 f"linting a draft against nothing would flag every fact in it.")
    findings = lint(draft_p.read_text(), reg_p.read_text(),
                    extra_allowed=Path(args.allow).read_text() if args.allow else "")
    print(format_report(findings, draft_p.name))
    sys.exit(1 if findings else 0)


def cmd_history(args) -> None:
    """What has actually happened, in order.

    The jobs row holds only the CURRENT status, so applying and then being
    rejected left no trace of the first event and no way to ask how long
    anything took."""
    con = store.connect()
    rows = store.history(con, uid=getattr(args, "uid", None))
    if not rows:
        print("No events recorded yet. They accumulate as you mark postings.")
        return
    for e in rows:
        who = f"{e['company']} - {e['title']}" if e["company"] else e["uid"]
        print(f"{e['at'][:16]}  {e['kind']:<18} {who[:56]}"
              + (f"  ({e['detail'][:40]})" if e["detail"] else ""))


def cmd_rescore(args) -> None:
    """Apply changed criteria to postings already in the database.

    Without this, editing your profile only affects postings the boards happen
    to show you again afterwards."""
    con = store.connect()
    _pull.rescore(con, load_profile())
    _pull.rebuild_report(con, min_score=getattr(args, "min_score", 0))


def cmd_consistency(args) -> None:
    """Does the report tell the truth about the database it came from?

    Added after a row shipped reading "Comp not stated" in its header and
    "comp $150,000-$208,000" in the line beneath. Both were rendered from the
    same posting. The value reached the reader by one path and the row by
    another, so they disagreed silently, and the CSV export dropped the band for
    a third of the postings that had one."""
    from engine import consistency as _cons
    con = store.connect()

    db_problems = _cons.check_db(con)
    rpt = args.report or _latest_report()
    stale = _cons.report_is_stale(con, rpt) if rpt else None
    if stale:
        print(f"  !  {stale}")
        print("     Checking the database only.\n")
        rpt_problems = []
    else:
        rpt_problems = _cons.check_report(con, rpt) if rpt else ["no report found to check"]

    if args.repair:
        fixed = _cons.repair_comp(con, apply=True)
        for m in fixed:
            print(f"  repaired  {m}")
        print(f"{len(fixed)} row(s) cleared; run `rescore` to re-derive them.\n"
              if fixed else "Nothing to repair.\n")
        db_problems = _cons.check_db(con)

    if not db_problems and not rpt_problems:
        print(f"Consistent. Database and {Path(rpt).name if rpt else 'report'} agree.")
        return
    if db_problems:
        print(f"{len(db_problems)} problem(s) inside the database:")
        for m in db_problems[:40]:
            print(f"  x  {m}")
    if rpt_problems:
        print(f"\n{len(rpt_problems)} disagreement(s) between the report and the database:")
        for m in rpt_problems[:40]:
            print(f"  x  {m}")
    raise SystemExit(1)


def _latest_report():
    from engine.report import OUT_DIR
    reports = sorted(OUT_DIR.glob("sourcing-*.md")) if OUT_DIR.exists() else []
    return str(reports[-1]) if reports else None


def cmd_applied(args) -> None:
    """Reconcile profile/applications.jsonl against the database.

    Exists because the tool recommended a role the owner had been rejected from
    the previous day, and a wider check found roughly twenty applications in six
    weeks against six the database knew about. Evidence that names a company but
    not a role is never written automatically: at an employer with several
    openings that marks the wrong one."""
    from engine import applied as _applied
    con = store.connect()
    path = Path(args.file) if args.file else (PROFILE.parent / "applications.jsonl")
    ev = _applied.load_evidence(path)
    if not ev:
        print(f"No evidence file at {path}.")
        print("Write one line of JSON per application, e.g.")
        print('  {"company": "Acme", "title": "Salesforce Admin", "status": "rejected", "on": "2026-08-05"}')
        return

    res = _applied.reconcile(con, ev, apply=args.apply)
    for m in res["matched"]:
        verb = "marked" if args.apply else "would mark"
        print(f"  {verb} {m['status']:<13} {m['company'][:28]:<28} {m['matched_title'][:44]}")
        print(f"      ({m['confidence']})")
    for a in res["ambiguous"]:
        print(f"  ?  {a['company'][:28]:<28} {a.get('title') or '(role not named)'}")
        print(f"      {a['why']}; not written. Candidates:")
        for c in a["candidates"][:6]:
            print(f"        {c['uid'][:12]}  {c['title'][:60]}")
    for u in res["unmatched"]:
        print(f"  -  {u['company'][:28]:<28} {u.get('title') or ''}")
        print(f"      {u['why']}")
    for pr in res["problems"]:
        print(f"  x  {pr}")

    print(f"\n{len(res['matched'])} matched, {len(res['ambiguous'])} ambiguous, "
          f"{len(res['unmatched'])} unmatched.")
    if res["matched"] and not args.apply:
        print("Nothing written. Re-run with --apply to record the matched ones.")

    doors = _applied.surfacing_a_closed_door(con)
    if doors:
        print(f"\n{len(doors)} live row(s) at an employer that already declined you:")
        for d in doors[:20]:
            print(f"  !  {d}")


def cmd_doctor(args) -> None:
    """One place that answers "is this thing working?".

    The signals existed but were scattered across status, the report footer and
    the tail of a pull, so a broken feed or a stale database was only noticed by
    someone already looking for it."""
    con = store.connect()
    reg = load_yaml(EMPLOYERS, {"employers": [], "feeds": []})
    problems, notes = [], []

    if not PROFILE.exists():
        problems.append("no profile/profile.yaml - run /setup in Claude Code")
    else:
        try:
            for w in _score_mod.validate_profile(
                    yaml.safe_load(PROFILE.read_text()) or {}):
                notes.append(f"profile: {w}")
        except ProfileError as e:
            problems.append(f"profile: {e}")

    n_emp = len([e for e in reg.get("employers", []) if e.get("active", True)])
    if not n_emp:
        problems.append("no active employers registered - run discover or ingest-urls")

    try:
        from engine import applied as _applied
        for d in _applied.surfacing_a_closed_door(con)[:10]:
            problems.append(f"already declined: {d}")
    except Exception:
        pass

    broken = _pull.broken_sources(con, reg)
    for b in broken:
        problems.append(f"source failing x{b['consecutive_failures']}: {b['source']} "
                        f"({(b['last_error'] or '')[:40]})")
    for d in store.dropped_to_zero(con):
        problems.append(f"{d['source']} returned 0 but had {d['prev_count']} last run")

    last = con.execute("SELECT started, finished FROM runs "
                       "ORDER BY run_id DESC LIMIT 1").fetchone()
    if last is None:
        notes.append("no completed run yet - try: ./careerkit.py pull")
    else:
        age = (datetime.now() - datetime.fromisoformat(last["started"])).days
        if age >= 7:
            problems.append(f"last run was {age} days ago - postings may be stale")
        if not last["finished"]:
            problems.append("the last run did not finish (interrupted?)")

    drift = tracker_drift(con)
    for url, sect, uid in drift["missing_from_db"]:
        problems.append(f"tracked as {sect} but still open in the database: "
                        f"{url[:60]} (mark {uid} applied)")
    for r in drift["missing_from_tracker"]:
        notes.append(f"applied with no tracker entry: {r['company']} - {r['title']}")

    scrapers = [f["name"] for f in reg.get("feeds", [])
                if f.get("active", True)
                and aggregators.policy(f["name"])["kind"] == "scraping"]
    if scrapers:
        notes.append(f"enabled scrapers (read public HTML, can be blocked): "
                     f"{', '.join(scrapers)}")

    ok = con.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    print(f"CareerKit check: {ok} postings, {n_emp} active employers, "
          f"{len(reg.get('feeds', []))} feeds")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  x  {p}")
    else:
        print("\n  no problems found")
    if notes:
        print(f"\n{len(notes)} note(s):")
        for n in notes:
            print(f"  -  {n}")
    sys.exit(1 if problems else 0)


def cmd_status(args) -> None:
    con = store.connect()
    s = store.stats(con)
    print(f"Database: {store.DB_PATH}")
    print(f"  postings tracked : {s['total']}")
    print(f"  completed runs   : {s['runs']}")
    print("  by gate          :", ", ".join(f"{k} {v}" for k, v in s["by_gate"].items()))
    print("  top sources      :", ", ".join(f"{k} {v}" for k, v in list(s["by_source"].items())[:8]))
    reg = load_yaml(EMPLOYERS, {"employers": [], "feeds": []})
    print(f"\nRegistry: {len(reg.get('employers', []))} employers, "
          f"{len(reg.get('feeds', []))} feeds")
    dropped = store.dropped_to_zero(con)
    if dropped:
        print(f"\n{len(dropped)} source(s) returned 0 but had postings last run "
              f"(possible schema change):")
        for d in dropped[:20]:
            print(f"  {d['source']:<46} was {d['prev_count']}")
    drift = tracker_drift(con)
    if drift["missing_from_db"]:
        print(f"\n{len(drift['missing_from_db'])} role(s) tracked as applied in "
              f"tracker.md but not in the database - these will resurface as new:")
        for url, sect, uid in drift["missing_from_db"][:15]:
            print(f"  [{sect}] {url}\n        ./careerkit.py mark {uid} applied")
    if drift.get("untracked_history"):
        print(f"\n  ({drift['untracked_history']} tracker application(s) have no database "
              f"row at all: applied before the database existed, or the posting has "
              f"since closed. Nothing to act on.)")
    if drift["missing_from_tracker"]:
        print(f"\n{len(drift['missing_from_tracker'])} application(s) in the database "
              f"with no tracker.md entry (no date, contact, or resume version recorded):")
        for r in drift["missing_from_tracker"][:15]:
            print(f"  {r['company']} - {r['title']}  ({r['status']})")

    broken = _pull.broken_sources(con, reg)
    if broken:
        print(f"\n{len(broken)} sources failing repeatedly (silent coverage loss):")
        for b in broken[:20]:
            print(f"  {b['source']:<46} x{b['consecutive_failures']}  {(b['last_error'] or '')[:50]}")
        print("  (deactivate one in employers.yaml to stop polling and silence it)")


DISCOVERED_QUEUE = ROOT / "data" / "discovered-pending.json"


def queue_discovered(entries: list[dict]) -> None:
    """Remember employers `discover` just registered, for the next pull report.

    discover and pull are separate commands, so the newly registered employers
    were announced only on the terminal of whoever ran discover, and never
    appeared in the report the user actually reads."""
    if not entries:
        return
    try:
        DISCOVERED_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        prev = json.loads(DISCOVERED_QUEUE.read_text()) if DISCOVERED_QUEUE.exists() else []
        seen = {(e.get("ats"), e.get("slug") or e.get("tenant")) for e in prev}
        prev += [e for e in entries
                 if (e.get("ats"), e.get("slug") or e.get("tenant")) not in seen]
        DISCOVERED_QUEUE.write_text(json.dumps(prev[-200:]))
    except OSError:
        pass          # a report nicety must never fail a discovery run


def take_discovered() -> list[dict]:
    """Read and clear the queue, so each employer is announced exactly once."""
    if not DISCOVERED_QUEUE.exists():
        return []
    try:
        entries = json.loads(DISCOVERED_QUEUE.read_text())
        DISCOVERED_QUEUE.unlink()
        return entries if isinstance(entries, list) else []
    except (OSError, ValueError):
        return []


def tracker_drift(con) -> dict:
    """Where the database and profile/tracker.md disagree.

    Two things record the pipeline and neither knows about the other. The
    database is written by `mark` and is what suppresses a role from future
    reports; tracker.md is written by the skills and is what the user and the
    agent actually read. They drift in both directions and both hurt:

      - applied in the database, missing from tracker.md: the human record of
        an application the user made has no date, no contact, no resume version.
      - in tracker.md under APPLIED, not applied in the database: the role is
        still 'new' to the engine, so it resurfaces in the next report as an
        opportunity the user already took.

    Matching is by URL, which is the only identifier both sides carry."""
    import re as _re

    def norm(u: str) -> tuple[str, str]:
        """(host, path) with scheme, www, query and trailing slash removed.

        Exact string matching was useless in practice: the same opening is
        written as a bare host in one place and a full https URL with tracking
        query in the other, so every row looked like drift."""
        u = _re.sub(r"^https?://", "", (u or "").strip().rstrip(".,;)"))
        u = _re.sub(r"^www\.", "", u).split("?")[0].split("#")[0].rstrip("/")
        host, _, path = u.partition("/")
        return host.lower(), path.lower()

    def same(a: tuple[str, str], b: tuple[str, str]) -> bool:
        """Same host, and one path is a prefix of the other. A tracker entry
        naming only the careers site still matches the full requisition URL."""
        if a[0] != b[0]:
            return False
        return a[1].startswith(b[1]) or b[1].startswith(a[1])

    # A line the user has already marked dead or rejected is not an open
    # application, so it must not demand a matching 'applied' row.
    CLOSED = _re.compile(r"\b(dead|rejected|closed|withdrawn|declined)\b", _re.I)

    text = TRACKER.read_text() if TRACKER.exists() else ""
    section, in_tracker = "", {}
    for line in text.splitlines():
        # Real headings carry a parenthetical ("## APPLIED (do not resurface)"),
        # so anchoring the caps run to end-of-line matched nothing and every URL
        # fell into an unnamed section - the check reported zero drift forever.
        h = _re.match(r"^#{1,4}\s*([A-Z][A-Z /-]*[A-Z])\b", line.strip())
        if h:
            section = h.group(1).strip()
            continue
        # Bare hosts count too: the tracker often names a careers site rather
        # than pasting a 200-character requisition URL.
        # A scheme or a path is required. A bare host is too weak a signal:
        # "Apollo.io" is a company name in prose and matched as a hostname.
        for url in _re.findall(
                r"https?://[^\s)\]>,]+|\b[a-z0-9.-]+\.(?:com|org|net|io|co|gov|us|ai)/[^\s)\]>,]*",
                line, _re.I):
            key = norm(url)
            if key[0] and key[1] and key not in in_tracker:
                in_tracker[key] = (section, bool(CLOSED.search(line)), url)

    db_applied, missing_from_tracker = [], []
    for r in con.execute("SELECT uid, url, company, title, status FROM jobs "
                         "WHERE status IN ('applied','rejected')"):
        key = norm(r["url"])
        db_applied.append(key)
        if not any(same(key, t) for t in in_tracker):
            missing_from_tracker.append(r)

    # The actionable case is narrow: a row EXISTS for a role the tracker calls
    # applied, and that row is still open. Those resurface in the next report as
    # fresh finds the user already acted on.
    #
    # A tracker entry with NO row at all is not that. It predates the database,
    # or the posting has since closed, and nothing can resurface from a row that
    # does not exist. Reporting those as drift produced six permanent warnings
    # that no action could ever clear, which is how a check trains you to ignore
    # it. They are counted, not listed.
    missing_from_db, untracked_history = [], 0
    for key, (sect, closed, raw) in in_tracker.items():
        if sect not in ("APPLIED", "INTERVIEWING") or closed:
            continue
        if any(same(key, d) for d in db_applied):
            continue
        row = next((r for r in con.execute("SELECT uid, url, status FROM jobs")
                    if same(key, norm(r["url"]))), None)
        if row is None:
            untracked_history += 1
        else:
            missing_from_db.append((raw, sect, row["uid"]))
    return {"missing_from_tracker": missing_from_tracker,
            "missing_from_db": missing_from_db,
            "untracked_history": untracked_history,
            "tracker_exists": TRACKER.exists()}


def cmd_db(args) -> None:
    """Integrity check and manual backup.

    The database holds months of first_seen dates and application status that
    cannot be re-derived from any job board, so it deserves the same care as the
    profile."""
    con = store.connect()
    if args.action == "check":
        row = con.execute("PRAGMA quick_check").fetchone()
        verdict = str(row[0]) if row else "unknown"
        n = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        print(f"{store.DB_PATH}\n  integrity : {verdict}\n  postings  : {n}")
        if verdict.lower() != "ok":
            sys.exit("Database reports a problem. Restore the newest "
                     "jobs.pre-migration-*.db beside it.")
    else:
        dest = store.DB_PATH.with_suffix(
            f".backup-{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}.db")
        import sqlite3 as _s
        with _s.connect(dest) as b:
            con.backup(b)
        print(f"Backed up to {dest}")


def cmd_mark(args) -> None:
    con = store.connect()
    try:
        store.set_status(con, args.uid, args.status, args.notes or "")
    except (ValueError, KeyError) as e:
        sys.exit(f"{e.args[0]}")
    print(f"{args.uid} -> {args.status}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pull"); sp.set_defaults(fn=cmd_pull)
    sp.add_argument("--no-cache", action="store_true",
                    help="bypass the 6h HTTP cache and really re-fetch")
    sp.add_argument("--employers", action="store_true")
    sp.add_argument("--feeds", action="store_true")
    sp.add_argument("--tier", nargs="*", choices=["A", "B", "C"],
                    help="poll only employers in these tiers")
    sp.add_argument("--min-score", type=int, default=0)

    sp = sub.add_parser("audit"); sp.set_defaults(fn=cmd_audit)
    sp.add_argument("--no-cache", action="store_true",
                    help="bypass the 6h HTTP cache and really re-fetch")
    sp.add_argument("--grep", help="only show kills whose title matches this regex")
    sp.add_argument("--samples", type=int, default=6)
    sp.add_argument("--employers", action="store_true", help="skip feeds (faster)")

    sp = sub.add_parser("discover"); sp.set_defaults(fn=cmd_discover)
    sp.add_argument("names", nargs="*")
    sp.add_argument("--file")
    sp.add_argument("--lane", default="discovered")
    # NOT the same flag as `pull --tier`, which FILTERS which tiers to poll.
    # This one ASSIGNS a tier to the employers being registered, so it takes a
    # single value. One name for two operations read as an inconsistency and
    # invited "fixing" one of them into the other's shape.
    sp.add_argument("--assign-tier", dest="tier", default="C",
                    choices=["A", "B", "C"],
                    help="tier to record for newly registered employers")

    sp = sub.add_parser("ingest-urls"); sp.set_defaults(fn=cmd_ingest_urls)
    sp.add_argument("file")

    sp = sub.add_parser("ingest-url"); sp.set_defaults(fn=cmd_ingest_url)
    sp.add_argument("url", help="one job URL; pass after -- if it starts with a dash")

    sp = sub.add_parser("verify"); sp.set_defaults(fn=cmd_verify)
    sp.add_argument("--all", action="store_true")

    sp = sub.add_parser("report"); sp.set_defaults(fn=cmd_report)
    sp.add_argument("--min-score", type=int, default=0)
    sp.add_argument("--format", choices=["md", "json", "csv"], default="md",
                    help="md rebuilds the readable report; json/csv export the rows")

    sub.add_parser("status").set_defaults(fn=cmd_status)

    sp = sub.add_parser("db", help="database integrity and backup")
    sp.set_defaults(fn=cmd_db)
    sp.add_argument("action", choices=["check", "backup"])

    sp = sub.add_parser("rescore"); sp.set_defaults(fn=cmd_rescore)
    sp.add_argument("--min-score", type=int, default=0)

    sp = sub.add_parser("doctor"); sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("applied"); sp.set_defaults(fn=cmd_applied)
    sp.add_argument("--file", help="default: profile/applications.jsonl")
    sp.add_argument("--apply", action="store_true", help="write the unambiguous matches")

    sp = sub.add_parser("consistency"); sp.set_defaults(fn=cmd_consistency)
    sp.add_argument("--report", help="default: the newest report in out/")
    sp.add_argument("--repair", action="store_true",
                    help="clear impossible comp values so rescore can re-derive them")

    sp = sub.add_parser("claims-lint"); sp.set_defaults(fn=cmd_claims_lint)
    sp.add_argument("draft", help="the resume, cover letter or answer to check")
    sp.add_argument("--register", help="default: profile/claims.md")
    sp.add_argument("--allow", help="extra authoritative text, e.g. the posting")

    sp = sub.add_parser("history"); sp.set_defaults(fn=cmd_history)
    sp.add_argument("uid", nargs="?", help="one posting, or omit for everything")

    sp = sub.add_parser("mark"); sp.set_defaults(fn=cmd_mark)
    # choices, not a free string: argparse rejects a typo before the database
    # is touched and prints the valid set, instead of failing later or silently.
    sp.add_argument("uid"); sp.add_argument("status", choices=store.VALID_STATUS)
    sp.add_argument("--notes")

    args = p.parse_args()
    # Commands that write are serialized against each other; read-only ones stay
    # usable while a long pull is running.
    MUTATING = {"pull", "audit", "discover", "ingest-url", "ingest-urls",
                "verify", "mark", "applied"}
    try:
        if args.cmd in MUTATING:
            with store.RunLock():
                args.fn(args)
        else:
            args.fn(args)
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
    except RuntimeError as e:
        sys.exit(str(e))
    except FileNotFoundError as e:
        sys.exit(f"File not found: {e.filename}")
    except yaml.YAMLError as e:
        # The single most likely failure for a non-technical user: they hand
        # edited profile.yaml. A raw parser traceback tells them nothing.
        sys.exit(f"Could not read your YAML.\n{e}\n\n"
                 "Fix the file it names, or ask Claude: 'my profile.yaml is broken'.")
    except re.error as e:
        sys.exit(f"A pattern in your profile is not a valid regex: {e}\n"
                 "Check the /raw regex/ entries in profile/profile.yaml.")
    except ProfileError as e:
        sys.exit(str(e))
    except sqlite3.DatabaseError as e:
        sys.exit(f"The database looks damaged: {e}\n"
                 f"Move {store.DB_PATH} aside and run a fresh pull to rebuild it. "
                 "Your profile/ folder is untouched.")
    except (AttributeError, TypeError) as e:
        # Almost always a YAML file with the right syntax but the wrong SHAPE:
        # a list where a mapping belongs, a string where a number belongs.
        sys.exit(f"A config file has an unexpected shape: {e}\n"
                 "Check profile/profile.yaml and profile/employers.yaml, or ask "
                 "Claude: 'my careerkit config looks wrong'.")
    except PermissionError as e:
        sys.exit(f"No permission to read or write {e.filename}.")


if __name__ == "__main__":
    main()
