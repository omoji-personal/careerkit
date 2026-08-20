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
from copy import copy
from datetime import date

from . import adapters as _adapters
from . import aggregators as _aggregators
from . import store
from .adapters import run_adapter
from .aggregators import run_feed
from .models import ATS_SOURCES
from .report import write_report


def _feed_active(feed: dict) -> bool:
    """One activation rule shared by polling, health, and retirement.

    Most legacy feeds default to enabled when ``active`` is omitted. Freehire is
    intentionally opt-in and its runner requires the literal boolean ``true``;
    classifying an omitted flag as active here would skip both polling and
    inactive-feed retirement, stranding every prior ``freehire:*`` row.
    """
    name = str(feed.get("name") or "").strip().casefold()
    if name == "freehire":
        return feed.get("active") is True
    return bool(feed.get("active", True))


def _health_identity_counts(employers: list[dict]) -> tuple[Counter, Counter]:
    """Counts needed to prove a legacy health-key migration is unambiguous."""
    legacy = Counter(f"{e.get('ats')}:{e.get('name')}" for e in employers)
    stable = Counter(_adapters.board_id(e) for e in employers)
    return legacy, stable


def _migrate_source_health_keys(con, employers: list[dict]) -> int:
    """Rename legacy ``ats:Display Name`` health rows to stable board ids.

    The row contains the previous count used by the truncation guard, so merely
    starting a second stable-key row loses exactly the baseline needed on the
    first hardened pull. Rename the existing row in place before counts are
    loaded. Both sides must be unique: two sites with one display name or two
    registry entries for one endpoint make attribution unknowable and are left
    untouched rather than guessed.
    """
    legacy_counts, stable_counts = _health_identity_counts(employers)
    existing = {r["source"] for r in con.execute("SELECT source FROM source_health")}
    migrated = 0
    with con:
        for entry in employers:
            legacy = f"{entry.get('ats')}:{entry.get('name')}"
            stable = _adapters.board_id(entry)
            if (legacy == stable or legacy not in existing or
                    legacy_counts[legacy] != 1 or stable_counts[stable] != 1):
                continue
            if stable in existing:
                # A prior hardened run already owns the current state. The old
                # display-key row is now only duplicate report noise.
                con.execute("DELETE FROM source_health WHERE source=?", (legacy,))
            else:
                con.execute("UPDATE source_health SET source=? WHERE source=?",
                            (stable, legacy))
                existing.add(stable)
            existing.discard(legacy)
            migrated += 1
    return migrated


def fetch_all(reg: dict, keys: dict, con=None, *, employers_only: bool = False,
              feeds_only: bool = False, tier=None, echo=print) -> dict:
    """Poll every active board and feed. Never raises for one bad source.

    Returns the jobs plus the health facts reconcile() needs: which boards and
    feeds answered well enough that their missing rows can be treated as
    genuinely closed."""
    active_employer_rows = [
        e for e in reg.get("employers", []) if e.get("active", True)]
    if con is not None:
        _migrate_source_health_keys(con, active_employer_rows)
    prev_counts = {}
    if con is not None:
        # An incomplete attempt keeps its usable rows in last_count, but that
        # number is not a completeness baseline. record_health() freezes the
        # last complete count in prev_count until a genuinely healthy response.
        prev_counts = {
            r["source"]: (r["last_count"] if r["last_error"] is None
                          else r["prev_count"])
            for r in con.execute(
                "SELECT source,last_count,last_error,prev_count FROM source_health")
        }
    all_jobs, ok, errors = [], 0, {}
    healthy_boards, healthy_feeds = set(), set()

    if not feeds_only:
        emps = list(active_employer_rows)
        if tier:
            # A missing tier defaults to C rather than vanishing. Filtering on
            # `e.get("tier") in tier` silently excluded every employer that had
            # no tier key, so `--tier C` polled fewer boards than plain `pull`.
            emps = [e for e in emps if (e.get("tier") or "C") in tier]
        # A duplicated endpoint is a configuration problem, but polling it
        # twice is worse: one success followed by one partial response used to
        # leave the same board both healthy and failed. Keep the first registry
        # row deterministically; coverage/doctor tells the user to remove the
        # duplicate.
        unique_emps, seen_boards = [], set()
        for employer in emps:
            board = _adapters.board_id(employer).casefold()
            if board in seen_boards:
                continue
            seen_boards.add(board)
            unique_emps.append(employer)
        emps = unique_emps
        echo(f"Polling {len(emps)} employer boards...")
        for e in emps:
            jobs, err = run_adapter(e)
            label = f"{e.get('ats')}:{e.get('name')}"
            source_key = _adapters.board_id(e)
            prev = prev_counts.get(source_key)
            collapsed = (not err and prev is not None and prev >= 10
                         and len(jobs) < prev * 0.5)
            if collapsed:
                err = (f"partial: count fell {prev} -> {len(jobs)}; below 50% "
                       "of the last complete baseline")
            if con is not None:
                store.record_health(con, source_key, len(jobs), err)
            if err:
                errors[source_key] = err
            else:
                ok += 1
                # Board identity, not platform identity. And a board whose count
                # collapsed is treated as UNHEALTHY for retirement purposes: a
                # truncating board (broken pagination, an API that starts
                # capping) reports success with a short list, and its missing
                # rows would otherwise be retired as closed.
                healthy_boards.add((e.get("ats"), e.get("name") or e.get("slug", ""),
                                    _adapters.board_id(e)))
            if collapsed:
                echo(f"      (count fell {prev} -> {len(jobs)}; not retiring its rows)")
            all_jobs.extend(jobs)
            capped = _adapters.at_page_ceiling(e.get("ats"), len(jobs))
            echo(f"  {label:<46} {len(jobs):>4}"
                 + (f"  [{err}]" if err else "")
                 + ("  !! at page ceiling, likely truncated" if capped else ""))

    if not employers_only:
        feeds, seen_feeds = [], set()
        for feed_cfg in reg.get("feeds", []):
            if not _feed_active(feed_cfg):
                continue
            feed_name = str(feed_cfg.get("name") or "").strip().casefold()
            if feed_name in seen_feeds:
                continue
            seen_feeds.add(feed_name)
            feeds.append(feed_cfg)
        echo(f"\nPolling {len(feeds)} aggregator feeds...")
        for f in feeds:
            feed_name = str(f["name"]).strip().casefold()
            cfg = dict(f)
            cfg["name"] = feed_name
            cfg.update(keys.get(feed_name, {}) or {})
            jobs, err = run_feed(feed_name, cfg)
            health_key = f"feed:{feed_name}"
            prev = prev_counts.get(health_key)
            collapsed = (not err and prev is not None and prev >= 10
                         and len(jobs) < prev * 0.5)
            if collapsed:
                err = (f"partial: count fell {prev} -> {len(jobs)}; below 50% "
                       "of the last complete baseline")
            if con is not None:
                store.record_health(con, health_key, len(jobs), err)
            if err:
                errors[feed_name] = err
            else:
                ok += 1
                # Same collapse guard as employer boards: a feed that loops over
                # several search terms can have most of them throttled, succeed
                # on one, and report success with a fraction of its usual rows.
                healthy_feeds.add(feed_name)
            if collapsed:
                echo(f"      (count fell {prev} -> {len(jobs)}; not retiring its rows)")
            all_jobs.extend(jobs)
            echo(f"  feed:{feed_name:<41} {len(jobs):>4}" + (f"  [{err}]" if err else ""))

    return {"jobs": all_jobs, "sources_ok": ok, "errors": errors,
            "healthy_boards": healthy_boards, "healthy_feeds": healthy_feeds}


#: Demotion gates, least severe first. SLOT-BLOCKED means the rails passed and
#: only the user's own preference stopped it; EXCLUDED means a rail killed it.
_DEMOTION_ORDER = {"EXCLUDED": 0, "SLOT-BLOCKED": 1}

# Surfaced verdicts, weakest first.  A QUALIFIED sighting is the stronger
# decision even when a VERIFY copy happens to carry a numerically higher score.
_SURFACE_ORDER = {"VERIFY": 0, "QUALIFIED": 1}


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

    Repeated sightings that resolve to one opening uid can disagree: a copy
    listed under a foreign location scores EXCLUDED while a richer copy scores
    SLOT-BLOCKED. "Best gate wins" was already implemented between a SURFACED
    and a demoted sighting; between two demoted ones this was a dict
    comprehension, which is plain last-write-wins. Iteration order then decided
    the stored verdict, and a rescore of the stored text disagreed with the pull
    that wrote it.

    Rare until the company floor arrived: the floor turns the formerly-surfaced
    clean copy into a demotion, which is exactly the case that exposes it.
    """
    grouped: dict[str, list] = {}
    for j in scored:
        if j.uid in kept_uids:
            continue
        grouped.setdefault(j.uid, []).append(j)

    best: dict = {}
    for uid, sightings in grouped.items():
        authoritative = [job for job in sightings
                         if _authoritative_sighting(job)]
        candidates = [job for job in (authoritative or sightings)
                      if job.gate in _DEMOTION_ORDER]
        for j in candidates:
            current = best.get(uid)
            if current is None or (
                    (_DEMOTION_ORDER[j.gate], j.score) >
                    (_DEMOTION_ORDER[current.gate], current.score)):
                best[uid] = j
    return best


def _surface_rank(job) -> tuple:
    """Deterministic preference for two surfaced copies of one posting.

    Gate and score are the verdict.  Everything after them chooses the richest
    whole sighting without allowing fetch order to decide which location,
    description, compensation claim, or source reaches the database.  The
    final textual tuple is only a stable tie-breaker; no two unequal records
    are left to Python's stable (and therefore input-order-sensitive) ``max``.
    """
    source = (job.source or "").lower().split(":", 1)[0]
    authoritative = source in ATS_SOURCES and bool((job.board or "").strip())
    comp_known = job.comp_min is not None or job.comp_max is not None
    evidence_fields = (
        job.location, job.description, job.posted_at, job.department,
        job.comp_text, job.url_direct, job.company_site, job.board,
        job.registry_lane, job.employer_tier,
    )
    richness = sum(value not in (None, "") for value in evidence_fields)
    stable = tuple(str(value or "") for value in (
        job.source, job.url, job.company, job.title, job.location,
        job.description, job.posted_at, job.department, job.remote_flag,
        job.comp_min, job.comp_max, job.comp_text, job.comp_source,
        job.external_id, job.url_direct, job.company_site, job.lane,
        job.registry_lane, job.rails_exempt, job.employer_tier, job.board,
        " | ".join(job.reasons or []),
    ))
    return (
        _SURFACE_ORDER[job.gate], job.score,
        int(authoritative), richness, int(comp_known),
        int(job.remote_flag is not None), len(job.description or ""), stable,
    )


def _authoritative_sighting(job) -> bool:
    """Employer ATS evidence outranks a third-party copy of the same URL."""
    source = str(job.source or "").strip().casefold()
    return source in ATS_SOURCES


def pick_surfaced(scored: list) -> list:
    """Collapse surfaced sightings to one deterministic record per uid.

    ``store.upsert`` updates a duplicate uid once per list item, so its former
    input was effectively last-write-wins.  Feed response order then decided
    whether the stored row was QUALIFIED or VERIFY and which copy supplied its
    evidence.  Select the strongest verdict first and the richest whole
    sighting within that verdict.  Direct-apply and company-site URLs are
    provenance rather than scorer inputs, so complementary values may safely be
    filled from another copy without creating a synthetic scoring verdict.

    Copies are shallow-cloned so enriching the winner cannot mutate an adapter's
    object (or make a second call with the reverse order observe different data).
    """
    grouped: dict[str, list] = {}
    for job in scored:
        grouped.setdefault(job.uid, []).append(job)

    collapsed = []
    for uid in sorted(grouped):
        all_sightings = grouped[uid]
        authoritative = [job for job in all_sightings
                         if _authoritative_sighting(job)]
        # Exact direct-URL coalescing can put a stale/thin Freehire copy beside
        # an employer-published sighting. The employer ATS owns the verdict,
        # including EXCLUDED and SLOT-BLOCKED. Otherwise a third-party VERIFY
        # copy could keep an opening surfaced after the canonical body failed a
        # hard requirement.
        decision_sightings = authoritative or all_sightings
        sightings = [job for job in decision_sightings
                     if job.gate in _SURFACE_ORDER]
        if not sightings:
            continue
        winner = max(sightings, key=_surface_rank)
        merged = copy(winner)
        merged.reasons = list(winner.reasons or [])
        merged.raw = dict(winner.raw or {})

        # These are tri-state evidence fields: None means not checked, empty
        # means checked without a result, and a URL is positive evidence.  A
        # positive value wins; otherwise preserve an explicit checked-empty
        # result rather than degrading it back to unknown.
        for field in ("url_direct", "company_site"):
            candidates = [j for j in sightings if getattr(j, field, None)]
            if candidates:
                setattr(merged, field,
                        getattr(max(candidates, key=_surface_rank), field))
            elif any(getattr(j, field, None) == "" for j in sightings):
                setattr(merged, field, "")

        collapsed.append(merged)
    return collapsed


def record_surfaced_sightings(con, scored: list, kept_uids: set[str]) -> None:
    """Keep every source's provenance after duplicate UIDs are collapsed.

    The jobs table needs one canonical row per UID, but the sightings table is
    explicitly source provenance.  Passing only the canonical record to upsert
    must not make corroborating feeds disappear.  Its schema stores one URL per
    ``(uid, source)``, so repeated copies from the same source are reduced with
    a stable key while distinct sources all survive.
    """
    per_source: dict[tuple[str, str], object] = {}

    def rank(job) -> tuple:
        surfaced = job.gate in _SURFACE_ORDER
        return (
            int(surfaced), _SURFACE_ORDER.get(job.gate, -1), job.score,
            int(bool(job.url_direct)), int(bool(job.company_site)),
            len(job.description or ""), job.url or "",
        )

    for job in scored:
        if job.uid not in kept_uids:
            continue
        key = (job.uid, job.source or "")
        current = per_source.get(key)
        if current is None or rank(job) > rank(current):
            per_source[key] = job

    today = date.today().isoformat()
    con.executemany(
        "INSERT INTO sightings (uid,source,url,seen_on) VALUES (?,?,?,?) "
        "ON CONFLICT(uid,source) DO UPDATE SET url=excluded.url, "
        "seen_on=MAX(sightings.seen_on,excluded.seen_on)",
        [(uid, source, per_source[(uid, source)].url, today)
         for uid, source in sorted(per_source)],
    )
    con.commit()


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
    # Freehire and a configured employer adapter can sight the same opening
    # under different source-local IDs.  Resolve only an exact canonical direct
    # URL match before surfaced/demoted selection, so both verdicts and both
    # provenance rows operate on one opening regardless of fetch order.
    store.coalesce_exact_direct_url_identities(con, scored)
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

    # A UID can have repeated sightings with different verdicts and different
    # amounts of evidence. Collapse them before upsert; otherwise the final
    # fetched copy silently wins and the database changes when an API returns
    # the same records in a different order.
    keep = pick_surfaced(scored)
    # run_id is what makes "new" mean "first seen THIS run" rather than "first
    # seen today". Omitting it is silent: rows land with no run stamp and the
    # report falls back to the date comparison for the rest of their life.
    new, _again = store.upsert(con, keep, run_id=run_id)
    kept_uids = {j.uid for j in keep}
    record_surfaced_sightings(con, scored, kept_uids)

    # Close the loop: a posting that STOPPED qualifying is written back, and a
    # posting that vanished from a healthy board is marked delisted. Without
    # this, both kept surfacing as live qualified roles indefinitely.
    # Best gate wins for repeated sightings of the same opening. Writing a
    # weaker demotion back blindly overwrote the surfaced row upsert had just
    # created and deleted the role on the run it was found.
    demoted = pick_demoted(scored, kept_uids)
    n_delisted, n_demoted = store.reconcile(
        con, demoted, fetched["healthy_boards"], fetched["healthy_feeds"],
        # Deactivated boards are deliberately no longer known/live. Keeping them
        # here stranded every row they had ever produced: the board was no longer
        # polled, its non-empty stable board id matched no healthy source, and the
        # orphan rule refused to retire it because the inactive registry entry
        # still counted as known.
        known_boards={(e.get("ats"), e.get("name") or e.get("slug", ""),
                       _adapters.board_id(e))
                      for e in reg.get("employers", []) if e.get("active", True)},
        # Keep configured and active feed identities distinct.  An explicitly
        # disabled feed is orphan-eligible after the ordinary two-day guard;
        # an absent/unconfigured source is not evidence of closure, and an
        # active source that this filtered run did not poll must stay live.
        known_feeds={str(f.get("name") or "").strip().casefold()
                     for f in reg.get("feeds", [])
                     if (str(f.get("name") or "").strip().casefold()
                         in _aggregators.FEEDS)},
        active_feeds={str(f.get("name") or "").strip().casefold()
                      for f in reg.get("feeds", [])
                      if (str(f.get("name") or "").strip().casefold()
                          in _aggregators.FEEDS) and _feed_active(f)})

    # Reports and exports are the user's complete decision queue.  A fixed LIMIT
    # silently dropped every active row below 300, precisely the low-scoring tail
    # the audit workflow needs in order to find false negatives.
    rows = store.query(con, min_score=min_score)
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
    employers = [e for e in reg.get("employers", []) if e.get("active", True)]
    existing = {r["source"] for r in con.execute("SELECT source FROM source_health")}
    legacy_counts, stable_counts = _health_identity_counts(employers)
    active = {_adapters.board_id(e) for e in employers}
    # Before stable board keys shipped, health used the display label. Continue
    # reporting an unambiguous legacy row only until this board has produced its
    # first stable-key health record. Same-name boards never share that fallback.
    active |= {
        label for e in employers
        if (label := f"{e.get('ats')}:{e.get('name')}")
        and legacy_counts[label] == 1
        and stable_counts[_adapters.board_id(e)] == 1
        and _adapters.board_id(e) not in existing
    }
    active |= {f"feed:{f.get('name')}"
               for f in reg.get("feeds", []) if _feed_active(f)}
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
    rows = store.query(con, min_score=min_score)
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
               # A migrated row with figures but no recorded provenance must
               # remain honest: rescore may normalize it, but cannot retroactively
               # claim the board supplied a value that may have come from prose.
               comp_source=((r["comp_source"] or "unknown")
                            if "comp_source" in keys and (r["comp_min"] or r["comp_max"])
                            else ((r["comp_source"] or "") if "comp_source" in keys else
                                  ("unknown" if (r["comp_min"] or r["comp_max"]) else ""))),
               lane=r["lane"] or "", employer_tier=r["employer_tier"] or "",
               registry_lane=(r["registry_lane"] or "") if "registry_lane" in keys else "",
               board=r["board"] if "board" in keys else "",
               remote_flag=None if rf is None else bool(rf),
               rails_exempt=bool(rx),
               url_direct=r["url_direct"] if "url_direct" in keys else "",
               company_site=r["company_site"] if "company_site" in keys else "")


def rescore(con, profile, *, registry_exempt_boards: set[str] | None = None,
            echo=print) -> dict:
    """Re-judge every stored posting against the CURRENT profile.

    Changing your criteria only affected postings that happened to be re-sighted
    afterwards. Everything already in the database kept the verdict it was given
    under the old rules, potentially forever, because a posting the new search
    terms no longer surface is never re-scored. For a tool whose premise is that
    your rules are the only rules, that left the report showing roles the rules
    no longer accept.

    Scores from stored text, so it makes no network requests."""
    from .score import score

    # The same guard reconcile's demotion loop carries, and for the same
    # reason: you do not rewrite the record of a posting the user acted on.
    # This is the OTHER path a criteria change flows through - the README
    # tells the user to run rescore after changing criteria - and it was left
    # unguarded when b0d3c69 guarded reconcile. Found live on 2026-08-14: the
    # same two submitted Anthropic applications that motivated that fix read
    # EXCLUDED score 0 again, re-corrupted by ordinary rescore runs. The
    # verdict a role carried when the user acted on it is history, not state,
    # and history is not subject to the current profile.
    rows = list(con.execute(
        "SELECT * FROM jobs WHERE status NOT IN ('applied','rejected','ignored')"))
    authoritative_exemptions = (
        {str(board).lower() for board in registry_exempt_boards}
        if registry_exempt_boards is not None else None
    )
    changed, dropped = 0, 0
    for r in rows:
        j = job_from_row(r)
        # Released builds persisted a profile-derived dream-company exemption
        # into this registry field. When the caller supplies the current registry,
        # repair that ambiguity before scoring: only an explicit board carve-out
        # may survive a criteria change. ``None`` preserves the library API's
        # historical behavior for callers that do not have registry context.
        if authoritative_exemptions is not None:
            j.rails_exempt = (j.board or "").lower() in authoritative_exemptions
        exempt_changed = int(bool(j.rails_exempt)) != int(bool(r["rails_exempt"]))
        score(j, profile)
        # score() resolves comp (parsing the body, annualising hourly figures), so
        # persist it here too. Leaving it out of the UPDATE meant a rescore could
        # never repair a row whose comp had been dropped, and the report kept
        # printing "Comp not stated" above a reasons line quoting the band.
        comp_changed = ((j.comp_min, j.comp_max, j.comp_source)
                        != (r["comp_min"], r["comp_max"], r["comp_source"] or ""))
        # Reasons are part of the verdict, not decoration. A row whose gate and
        # score are unchanged can still be explained differently under new rules,
        # and skipping the write left the database asserting a reason the current
        # rules would never give. That is the same drift as a report contradicting
        # the row it was rendered from, one layer earlier.
        reasons_changed = " | ".join(j.reasons) != (r["reasons"] or "")
        if (j.gate == r["gate"] and j.score == r["score"]
                and not comp_changed and not reasons_changed and not exempt_changed):
            continue
        if j.gate != r["gate"] or j.score != r["score"]:
            changed += 1
            if r["gate"] in ("QUALIFIED", "VERIFY") and j.gate not in ("QUALIFIED", "VERIFY"):
                dropped += 1
        con.execute("UPDATE jobs SET gate=?, score=?, reasons=?, lane=?, comp_min=?, comp_max=?, "
                    "comp_source=?, rails_exempt=? "
                    "WHERE uid=?",
                    (j.gate, j.score, " | ".join(j.reasons), j.lane or r["lane"],
                     j.comp_min, j.comp_max, j.comp_source,
                     int(bool(j.rails_exempt)), r["uid"]))
    con.commit()
    echo(f"  re-scored {len(rows)} stored postings against the current profile")
    echo(f"  {changed} changed verdict, {dropped} no longer surface")
    return {"total": len(rows), "changed": changed, "dropped": dropped}
