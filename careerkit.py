#!/usr/bin/env python3
"""CareerKit sourcing CLI. Personal state lives in profile/ (gitignored).

    ./careerkit.py pull                poll every registered employer + feed, score, report
    ./careerkit.py pull --employers    employer ATS boards only
    ./careerkit.py pull --feeds        aggregator feeds only
    ./careerkit.py discover NAME [...] probe a company across 12 ATS platforms, register hits
    ./careerkit.py discover --file f   one company name per line
    ./careerkit.py verify              confirm each board belongs to the right company
    ./careerkit.py ingest-urls FILE    resolve pasted job URLs -> employers, register
    ./careerkit.py audit [--grep RX]   re-fetch + re-score, show every kill and why
    ./careerkit.py report | status | mark UID STATUS
"""
from __future__ import annotations

import argparse
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
from engine.adapters import run_adapter  # noqa: E402
from engine.aggregators import run_feed  # noqa: E402
from engine.discover import discover_many  # noqa: E402
from engine.report import write_report  # noqa: E402
from engine.score import Profile, ProfileError, score, score_all  # noqa: E402
from engine import search as _search
from engine.search import resolve, workday_parts  # noqa: E402
from engine.verify import verify_entry  # noqa: E402

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "profile" / "profile.yaml"
EMPLOYERS = ROOT / "profile" / "employers.yaml"
KEYS = ROOT / "profile" / "keys.yaml"


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


def _fetch_all(reg, keys, con=None, employers_only=False, feeds_only=False, tier=None):
    prev_counts = {}
    if con is not None:
        prev_counts = {r["source"]: r["last_count"]
                       for r in con.execute("SELECT source, last_count FROM source_health")}
    all_jobs, ok, errors = [], 0, {}
    healthy_boards, healthy_feeds = set(), set()
    if not feeds_only:
        emps = [e for e in reg.get("employers", []) if e.get("active", True)]
        if tier:
            emps = [e for e in emps if (e.get("tier") or "C") in tier]   # missing tier defaulted to C rather than vanishing
        print(f"Polling {len(emps)} employer boards...")
        for e in emps:
            jobs, err = run_adapter(e)
            label = f"{e.get('ats')}:{e.get('name')}"
            if con is not None:
                store.record_health(con, label, len(jobs), err)
            if err:
                errors[label] = err
            else:
                ok += 1
                # Board identity, not platform identity. And a board whose
                # count collapsed is treated as UNHEALTHY for retirement
                # purposes: a truncating board (broken pagination, an API that
                # starts capping) reports success with a short list, and its
                # missing rows would otherwise be retired as closed.
                prev = prev_counts.get(label)
                collapsed = prev and prev >= 10 and len(jobs) < prev * 0.5
                if not collapsed:
                    healthy_boards.add((e.get("ats"), e.get("name") or e.get("slug", ""),
                                        _adapters.board_id(e)))
                else:
                    print(f"      (count fell {prev} -> {len(jobs)}; not retiring its rows)")
            all_jobs.extend(jobs)
            capped = _adapters.at_page_ceiling(e.get("ats"), len(jobs))
            print(f"  {label:<46} {len(jobs):>4}"
                  + (f"  [{err}]" if err else "")
                  + ("  !! at page ceiling, likely truncated" if capped else ""))
    if not employers_only:
        feeds = [f for f in reg.get("feeds", []) if f.get("active", True)]
        print(f"\nPolling {len(feeds)} aggregator feeds...")
        for f in feeds:
            cfg = dict(f)
            cfg.update(keys.get(f["name"], {}) or {})
            jobs, err = run_feed(f["name"], cfg)
            if con is not None:
                store.record_health(con, f"feed:{f['name']}", len(jobs), err)
            if err:
                errors[f["name"]] = err
            else:
                ok += 1
                # Same collapse guard as employer boards: a feed that loops over
                # several search terms can have most of them throttled, succeed
                # on one, and report success with a fraction of its usual rows.
                prev = prev_counts.get(f"feed:{f['name']}")
                if not (prev and prev >= 10 and len(jobs) < prev * 0.5):
                    healthy_feeds.add(f["name"])
                else:
                    print(f"      (count fell {prev} -> {len(jobs)}; not retiring its rows)")
            all_jobs.extend(jobs)
            print(f"  feed:{f['name']:<41} {len(jobs):>4}" + (f"  [{err}]" if err else ""))
    return all_jobs, ok, errors, healthy_boards, healthy_feeds


def cmd_pull(args) -> None:
    _http.set_cache_enabled(not getattr(args, "no_cache", False))
    profile = load_profile()
    aggregators.set_search_terms(profile.search_terms)
    _adapters.set_relevance_terms(profile.relevance_terms)
    _search.set_core_terms(profile.search_terms)
    reg = load_yaml(EMPLOYERS, {"employers": [], "feeds": []})
    keys = load_yaml(KEYS, {})
    con = store.connect()
    run_id = store.start_run(con)

    all_jobs, ok_sources, errors, healthy_boards, healthy_feeds = _fetch_all(
        reg, keys, con, employers_only=args.employers, feeds_only=args.feeds,
        tier=args.tier)

    print(f"\nScoring {len(all_jobs)} postings...")
    scored = score_all(all_jobs, profile)
    excluded = Counter()
    for j in scored:
        if j.gate in ("EXCLUDED", "SLOT-BLOCKED") and j.reasons:
            excluded[j.reasons[0].split(":")[0][:38]] += 1

    keep = [j for j in scored if j.gate in ("QUALIFIED", "VERIFY")]
    new, again = store.upsert(con, keep)

    # Close the loop: a posting that STOPPED qualifying is written back, and a
    # posting that vanished from a healthy board is marked delisted. Without
    # this, both kept surfacing as live qualified roles indefinitely.
    # Best gate wins. Aggregator sightings of one role all share a uid, so a
    # copy listed under a foreign location scores EXCLUDED while the clean copy
    # scores QUALIFIED. Writing the demotion back blindly overwrote the row
    # upsert had just created and deleted the role on the run it was found.
    kept_uids = {j.uid for j in keep}
    demoted = {j.uid: (j.score, j.gate, " | ".join(j.reasons))
               for j in scored
               if j.gate in ("EXCLUDED", "SLOT-BLOCKED") and j.uid not in kept_uids}
    n_delisted, n_demoted = store.reconcile(
        con, demoted, healthy_boards, healthy_feeds,
        known_boards={(e.get("ats"), e.get("name") or e.get("slug", ""))
                      for e in reg.get("employers", [])})

    rows = store.query(con, min_score=args.min_score, limit=300)
    health = list(con.execute(
        "SELECT * FROM source_health ORDER BY consecutive_failures DESC, source"))
    detail = {"pulled": len(all_jobs), "sources_ok": ok_sources,
              "excluded_breakdown": dict(excluded.most_common(12)), "errors": errors}
    store.finish_run(con, run_id, len(all_jobs), len(new),
                     sum(1 for r in rows if r["gate"] == "QUALIFIED"), detail)
    path = write_report(con, rows, health=health, run_detail=detail)

    screened = len(all_jobs) - len(keep)
    print(f"\n  pulled      {len(all_jobs)}")
    print(f"  in-family   {len(keep)}")
    print(f"  screened    {screened}   ("
          f"{', '.join(f'{k} {v}' for k, v in excluded.most_common(5))})")
    print(f"  NEW         {len(new)}")
    print(f"  qualified   {sum(1 for r in rows if r['gate'] == 'QUALIFIED')}")
    if n_delisted or n_demoted:
        print(f"  closed out  {n_delisted} delisted, {n_demoted} no longer qualify")
    dropped = store.dropped_to_zero(con)
    if dropped:
        print(f"\n  !! {len(dropped)} source(s) returned 0 but had postings last run "
              f"(possible schema change, not an outage):")
        for d in dropped[:10]:
            print(f"     {d['source']:<44} was {d['prev_count']}")
    print(f"\nReport: {path}")


def cmd_audit(args) -> None:
    _http.set_cache_enabled(not getattr(args, "no_cache", False))
    """The calibration loop: re-fetch live boards, re-score, and show what got
    KILLED and why - so silent false negatives get caught, not discovered
    months later. Use --grep to focus (e.g. --grep 'product manager')."""
    profile = load_profile()
    aggregators.set_search_terms(profile.search_terms)
    _adapters.set_relevance_terms(profile.relevance_terms)
    _search.set_core_terms(profile.search_terms)
    reg = load_yaml(EMPLOYERS, {"employers": [], "feeds": []})
    keys = load_yaml(KEYS, {})
    all_jobs, _, _, _, _ = _fetch_all(reg, keys, None, employers_only=args.employers,
                                feeds_only=False)
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
    print(f"Probing {len(todo)} companies across 12 ATS platforms...", flush=True)
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
    print(f"\n{len(found)} registered, {len(missed)} not found. "
          f"Registry now holds {len(reg['employers'])} employers.")
    if missed:
        print("Not found (may post only on LinkedIn, or use an unguessable slug):")
        for m in missed:
            print(f"  {m}")


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
    health = list(con.execute(
        "SELECT * FROM source_health ORDER BY consecutive_failures DESC"))
    last = con.execute("SELECT detail FROM runs WHERE finished IS NOT NULL "
                       "ORDER BY run_id DESC LIMIT 1").fetchone()
    import json
    detail = json.loads(last["detail"]) if last and last["detail"] else {}
    print("Report:", write_report(con, rows, health=health, run_detail=detail))


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
    broken = list(con.execute(
        "SELECT * FROM source_health WHERE consecutive_failures >= 2 "
        "ORDER BY consecutive_failures DESC"))
    if broken:
        print(f"\n{len(broken)} sources failing repeatedly (silent coverage loss):")
        for b in broken[:20]:
            print(f"  {b['source']:<46} x{b['consecutive_failures']}  {(b['last_error'] or '')[:50]}")


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
    sp.add_argument("--tier", nargs="*")
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
    sp.add_argument("--tier")

    sp = sub.add_parser("ingest-urls"); sp.set_defaults(fn=cmd_ingest_urls)
    sp.add_argument("file")

    sp = sub.add_parser("ingest-url"); sp.set_defaults(fn=cmd_ingest_url)
    sp.add_argument("url", help="one job URL; pass after -- if it starts with a dash")

    sp = sub.add_parser("verify"); sp.set_defaults(fn=cmd_verify)
    sp.add_argument("--all", action="store_true")

    sp = sub.add_parser("report"); sp.set_defaults(fn=cmd_report)
    sp.add_argument("--min-score", type=int, default=0)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    sp = sub.add_parser("mark"); sp.set_defaults(fn=cmd_mark)
    sp.add_argument("uid"); sp.add_argument("status"); sp.add_argument("--notes")

    args = p.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
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
