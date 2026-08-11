"""The pull loop: poll every source, score, persist, reconcile, report.

This lives in the engine rather than in a CLI on purpose. Two front ends drive
it (this repo's `careerkit.py`, and the author's personal `sourcer.py`, which
imports this same engine), and while the loop was copied into both they drifted
silently and expensively:

  - one passed `run_id` to `upsert` and the other did not, so run-scoped
    freshness worked in one tool and fell back to a date comparison in the other
  - one defaulted a missing employer `tier` to "C" and the other dropped those
    employers entirely from any tier-filtered pull
  - the page-ceiling truncation warning existed in one and not the other

None of those announce themselves. A copied loop means every engine fix has to
be applied twice and being wrong the second time looks exactly like working.
"""
from __future__ import annotations

from collections import Counter

from . import adapters as _adapters
from . import store
from .adapters import run_adapter
from .aggregators import run_feed
from .report import write_report


def fetch_all(reg: dict, keys: dict, con=None, *, employers_only: bool = False,
              feeds_only: bool = False, tier=None, echo=print) -> dict:
    """Poll every active board and feed. Never raises for one bad source.

    Returns the jobs plus the health facts reconcile() needs: which boards and
    feeds answered well enough that their missing rows can be treated as
    genuinely closed."""
    prev_counts = {}
    if con is not None:
        prev_counts = {r["source"]: r["last_count"]
                       for r in con.execute("SELECT source, last_count FROM source_health")}
    all_jobs, ok, errors = [], 0, {}
    healthy_boards, healthy_feeds = set(), set()

    if not feeds_only:
        emps = [e for e in reg.get("employers", []) if e.get("active", True)]
        if tier:
            # A missing tier defaults to C rather than vanishing. Filtering on
            # `e.get("tier") in tier` silently excluded every employer that had
            # no tier key, so `--tier C` polled fewer boards than plain `pull`.
            emps = [e for e in emps if (e.get("tier") or "C") in tier]
        echo(f"Polling {len(emps)} employer boards...")
        for e in emps:
            jobs, err = run_adapter(e)
            label = f"{e.get('ats')}:{e.get('name')}"
            if con is not None:
                store.record_health(con, label, len(jobs), err)
            if err:
                errors[label] = err
            else:
                ok += 1
                # Board identity, not platform identity. And a board whose count
                # collapsed is treated as UNHEALTHY for retirement purposes: a
                # truncating board (broken pagination, an API that starts
                # capping) reports success with a short list, and its missing
                # rows would otherwise be retired as closed.
                prev = prev_counts.get(label)
                collapsed = prev and prev >= 10 and len(jobs) < prev * 0.5
                if not collapsed:
                    healthy_boards.add((e.get("ats"), e.get("name") or e.get("slug", ""),
                                        _adapters.board_id(e)))
                else:
                    echo(f"      (count fell {prev} -> {len(jobs)}; not retiring its rows)")
            all_jobs.extend(jobs)
            capped = _adapters.at_page_ceiling(e.get("ats"), len(jobs))
            echo(f"  {label:<46} {len(jobs):>4}"
                 + (f"  [{err}]" if err else "")
                 + ("  !! at page ceiling, likely truncated" if capped else ""))

    if not employers_only:
        feeds = [f for f in reg.get("feeds", []) if f.get("active", True)]
        echo(f"\nPolling {len(feeds)} aggregator feeds...")
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
                    echo(f"      (count fell {prev} -> {len(jobs)}; not retiring its rows)")
            all_jobs.extend(jobs)
            echo(f"  feed:{f['name']:<41} {len(jobs):>4}" + (f"  [{err}]" if err else ""))

    return {"jobs": all_jobs, "sources_ok": ok, "errors": errors,
            "healthy_boards": healthy_boards, "healthy_feeds": healthy_feeds}


#: Demotion gates, least severe first. SLOT-BLOCKED means the rails passed and
#: only the user's own preference stopped it; EXCLUDED means a rail killed it.
_DEMOTION_ORDER = {"EXCLUDED": 0, "SLOT-BLOCKED": 1}


def clip_description(job) -> None:
    """Cut the body to what will actually be stored, BEFORE it is scored.

    score() read the full fetched text while upsert kept only the first
    DESCRIPTION_LIMIT characters, so anything past that offset - a salary band, a
    signal phrase, a clearance requirement - counted on the pull and vanished on
    the rescore, flipping verdicts in both directions on rows nobody touched. A
    larger limit only moves the boundary; judging exactly what is kept removes it.
    """
    if job.description and len(job.description) > store.DESCRIPTION_LIMIT:
        job.description = job.description[:store.DESCRIPTION_LIMIT]


def pick_demoted(scored: list, kept_uids: set) -> dict:
    """One demotion per uid, best gate winning.

    Aggregator sightings of one role share a uid, so a copy listed under a
    foreign location scores EXCLUDED while the clean US copy scores
    SLOT-BLOCKED. "Best gate wins" was already implemented between a SURFACED
    and a demoted sighting; between two demoted ones this was a dict
    comprehension, which is plain last-write-wins. Iteration order then decided
    the stored verdict, and a rescore of the stored text disagreed with the pull
    that wrote it.

    Rare until the company floor arrived: the floor turns the formerly-surfaced
    clean copy into a demotion, which is exactly the case that exposes it.
    """
    best: dict = {}
    for j in scored:
        if j.gate not in _DEMOTION_ORDER or j.uid in kept_uids:
            continue
        current = best.get(j.uid)
        if current is None or (
                (_DEMOTION_ORDER[j.gate], j.score) >
                (_DEMOTION_ORDER[current.gate], current.score)):
            best[j.uid] = j
    return best


def run_pull(con, reg: dict, keys: dict, profile, *, employers_only: bool = False,
             feeds_only: bool = False, tier=None, min_score: int = 0,
             discovered=(), echo=print) -> dict:
    """One complete pull. Returns the numbers the caller prints."""
    from .score import score_all

    run_id = store.start_run(con)
    fetched = fetch_all(reg, keys, con, employers_only=employers_only,
                        feeds_only=feeds_only, tier=tier, echo=echo)
    all_jobs = fetched["jobs"]

    for _j in all_jobs:
        clip_description(_j)
    echo(f"\nScoring {len(all_jobs)} postings...")
    scored = score_all(all_jobs, profile)
    excluded: Counter = Counter()
    for j in scored:
        if j.gate in ("EXCLUDED", "SLOT-BLOCKED") and j.reasons:
            # split(":") already drops the variable payload ("non-US: Berlin" ->
            # "non-US"), so what is left is a small set of fixed phrases. A hard
            # 38-char clip did nothing but mangle the longest one, and the first
            # run a new user sees reported "domain terms never mentioned in
            # postin". Give the canned phrases room, and mark any real overflow
            # with an ellipsis so a truncation is visible as one.
            label = j.reasons[0].split(":")[0].strip()
            excluded[label if len(label) <= 60 else label[:59] + "…"] += 1

    keep = [j for j in scored if j.gate in ("QUALIFIED", "VERIFY")]
    # run_id is what makes "new" mean "first seen THIS run" rather than "first
    # seen today". Omitting it is silent: rows land with no run stamp and the
    # report falls back to the date comparison for the rest of their life.
    new, _again = store.upsert(con, keep, run_id=run_id)

    # Close the loop: a posting that STOPPED qualifying is written back, and a
    # posting that vanished from a healthy board is marked delisted. Without
    # this, both kept surfacing as live qualified roles indefinitely.
    # Best gate wins. Aggregator sightings of one role all share a uid, so a
    # copy listed under a foreign location scores EXCLUDED while the clean copy
    # scores QUALIFIED. Writing the demotion back blindly overwrote the row
    # upsert had just created and deleted the role on the run it was found.
    kept_uids = {j.uid for j in keep}
    demoted = pick_demoted(scored, kept_uids)
    n_delisted, n_demoted = store.reconcile(
        con, demoted, fetched["healthy_boards"], fetched["healthy_feeds"],
        # Deactivated boards are deliberately no longer known/live. Keeping them
        # here stranded every row they had ever produced: the board was no longer
        # polled, its non-empty stable board id matched no healthy source, and the
        # orphan rule refused to retire it because the inactive registry entry
        # still counted as known.
        known_boards={(e.get("ats"), e.get("name") or e.get("slug", ""))
                      for e in reg.get("employers", []) if e.get("active", True)})

    rows = store.query(con, min_score=min_score, limit=300)
    health = list(con.execute(
        "SELECT * FROM source_health ORDER BY consecutive_failures DESC, source"))
    detail = {"pulled": len(all_jobs), "sources_ok": fetched["sources_ok"],
              "excluded_breakdown": dict(excluded.most_common(12)),
              "errors": fetched["errors"], "discovered": list(discovered)}
    qualified = sum(1 for r in rows if r["gate"] == "QUALIFIED")
    store.finish_run(con, run_id, len(all_jobs), len(new), qualified, detail)
    path = write_report(con, rows, health=health, run_detail=detail, run_id=run_id)

    return {"run_id": run_id, "path": path, "pulled": len(all_jobs),
            "kept": len(keep), "new": len(new), "qualified": qualified,
            "delisted": n_delisted, "demoted": n_demoted,
            "excluded": excluded, "errors": fetched["errors"], "rows": rows}


def broken_sources(con, reg: dict, threshold: int = 2) -> list:
    """Repeated failures for sources that are active in the current registry.

    A deactivated, removed, or renamed board keeps its failure history in
    source_health forever. Showing those orphaned rows under "failing
    repeatedly" makes a repaired source look permanently broken. A warning
    list containing things already handled is one users learn to skip, which is
    how a real outage goes unnoticed.

    Lives here rather than in a CLI for the same reason the pull loop does:
    both front ends print this and one of them had already drifted."""
    active = {f"{e.get('ats')}:{e.get('name')}"
              for e in reg.get("employers", []) if e.get("active", True)}
    active |= {f"feed:{f.get('name')}"
               for f in reg.get("feeds", []) if f.get("active", True)}
    return [b for b in con.execute(
        "SELECT * FROM source_health WHERE consecutive_failures >= ? "
        "ORDER BY consecutive_failures DESC", (threshold,))
        if b["source"] in active]


def rebuild_report(con, *, min_score: int = 0, echo=print):
    """Regenerate the report from the database, as of the last COMPLETED run.

    Both front ends had their own copy of this, and one of them kept calling
    write_report without a run id, so "new" silently reverted to the old
    heuristic there and announced week-old postings as new. Third time this
    class of duplication has cost something, so it lives here now."""
    import json as _json
    rows = store.query(con, min_score=min_score, limit=300)
    health = list(con.execute(
        "SELECT * FROM source_health ORDER BY consecutive_failures DESC, source"))
    last = con.execute("SELECT run_id, detail FROM runs WHERE finished IS NOT NULL "
                       "ORDER BY run_id DESC LIMIT 1").fetchone()
    detail = _json.loads(last["detail"]) if last and last["detail"] else {}
    path = write_report(con, rows, health=health, run_detail=detail,
                        run_id=last["run_id"] if last else None)
    echo(f"Report: {path}")
    return path


def job_from_row(r):
    """Rebuild the Job a stored row represents.

    Extracted so that rescore and the equivalence test cannot drift apart. The
    bug this guards against was exactly a drift: remote_flag was set by twelve
    adapters, trusted by location_verdict, and never stored, so rescore silently
    re-judged remote postings as if the board had never said they were remote.
    Any field the scorer reads and this function cannot restore is a missing
    column, and test_pull_and_rescore_agree turns that into a failing test.
    """
    from .models import Job
    keys = r.keys()
    rf = r["remote_flag"] if "remote_flag" in keys else None
    rx = r["rails_exempt"] if "rails_exempt" in keys else None
    return Job(company=r["company"], title=r["title"], url=r["url"],
               source=r["source"], location=r["location"] or "",
               description=r["description"] or "", posted_at=r["posted_at"] or "",
               department=r["department"] or "", comp_min=r["comp_min"],
               comp_max=r["comp_max"], comp_text=r["comp_text"] or "",
               lane=r["lane"] or "", employer_tier=r["employer_tier"] or "",
               registry_lane=(r["registry_lane"] or "") if "registry_lane" in keys else "",
               board=r["board"] if "board" in keys else "",
               remote_flag=None if rf is None else bool(rf),
               rails_exempt=bool(rx),
               url_direct=r["url_direct"] if "url_direct" in keys else "",
               company_site=r["company_site"] if "company_site" in keys else "")


def rescore(con, profile, *, echo=print) -> dict:
    """Re-judge every stored posting against the CURRENT profile.

    Changing your criteria only affected postings that happened to be re-sighted
    afterwards. Everything already in the database kept the verdict it was given
    under the old rules, potentially forever, because a posting the new search
    terms no longer surface is never re-scored. For a tool whose premise is that
    your rules are the only rules, that left the report showing roles the rules
    no longer accept.

    Scores from stored text, so it makes no network requests."""
    from .score import score

    rows = list(con.execute("SELECT * FROM jobs"))
    changed, dropped = 0, 0
    for r in rows:
        j = job_from_row(r)
        score(j, profile)
        # score() resolves comp (parsing the body, annualising hourly figures), so
        # persist it here too. Leaving it out of the UPDATE meant a rescore could
        # never repair a row whose comp had been dropped, and the report kept
        # printing "Comp not stated" above a reasons line quoting the band.
        comp_changed = (j.comp_min, j.comp_max) != (r["comp_min"], r["comp_max"])
        # Reasons are part of the verdict, not decoration. A row whose gate and
        # score are unchanged can still be explained differently under new rules,
        # and skipping the write left the database asserting a reason the current
        # rules would never give. That is the same drift as a report contradicting
        # the row it was rendered from, one layer earlier.
        reasons_changed = " | ".join(j.reasons) != (r["reasons"] or "")
        if (j.gate == r["gate"] and j.score == r["score"]
                and not comp_changed and not reasons_changed):
            continue
        if j.gate != r["gate"] or j.score != r["score"]:
            changed += 1
            if r["gate"] in ("QUALIFIED", "VERIFY") and j.gate not in ("QUALIFIED", "VERIFY"):
                dropped += 1
        con.execute("UPDATE jobs SET gate=?, score=?, reasons=?, lane=?, comp_min=?, comp_max=? "
                    "WHERE uid=?",
                    (j.gate, j.score, " | ".join(j.reasons), j.lane or r["lane"],
                     j.comp_min, j.comp_max, r["uid"]))
    con.commit()
    echo(f"  re-scored {len(rows)} stored postings against the current profile")
    echo(f"  {changed} changed verdict, {dropped} no longer surface")
    return {"total": len(rows), "changed": changed, "dropped": dropped}
