"""Employer discovery: turn a company NAME into a pollable ATS board.

This is the piece that makes the tool exhaustive rather than merely broad.
Keyword job search cannot reach most target employers because ATS platforms
deliberately do not federate. But every one of those boards is publicly
readable IF you know the slug. So: generate slug candidates from the company
name, probe each platform's cheapest endpoint, keep what answers.

Result is written back into employers.yaml so the next pull just works.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

from .http import fetch, fetch_json

_STOP = {
    "inc", "inc.", "llc", "l.l.c.", "ltd", "limited", "corp", "corporation", "co",
    "company", "the", "group", "holdings", "partners", "technologies", "technology",
    "solutions", "consulting", "consultants", "services", "usa", "us", "global",
    "international", "systems", "software", "labs", "cloud",
}


def slug_candidates(name: str, extra: Iterable[str] = ()) -> list[str]:
    """Plausible board slugs for a company name, most likely first."""
    clean = re.sub(r"[^\w\s&-]", " ", name.lower())
    words = [w for w in clean.split() if w]
    core = [w for w in words if w not in _STOP] or words

    joined = "".join(core)
    hyph = "-".join(core)
    full = "".join(words)
    fullhyph = "-".join(words)
    first = core[0] if core else ""
    initials = "".join(w[0] for w in core) if len(core) > 1 else ""

    cands = [joined, hyph, full, fullhyph, first, initials]
    cands += [c + "careers" for c in (joined, hyph) if c]
    cands += [c.replace("&", "and") for c in (joined, hyph) if "&" in c]
    cands = [c for c in cands if c and len(c) >= 2]
    cands = list(dict.fromkeys(list(extra) + cands))
    return cands


# Each probe returns posting count, or None if the board does not exist.
def _p_greenhouse(s: str):
    d = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs")
    return len(d.get("jobs", [])) if isinstance(d, dict) and "jobs" in d else None


def _p_lever(s: str):
    d = fetch_json(f"https://api.lever.co/v0/postings/{s}?mode=json")
    return len(d) if isinstance(d, list) else None


def _p_ashby(s: str):
    d = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
    return len(d.get("jobs", [])) if isinstance(d, dict) and "jobs" in d else None


def _p_smartrecruiters(s: str):
    d = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1")
    return d.get("totalFound") if isinstance(d, dict) and "totalFound" in d else None


def _p_workable(s: str):
    d = fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{s}?details=false")
    if isinstance(d, dict) and "jobs" in d:
        return len(d["jobs"])
    return None


def _p_recruitee(s: str):
    d = fetch_json(f"https://{s}.recruitee.com/api/offers/")
    return len(d.get("offers", [])) if isinstance(d, dict) and "offers" in d else None


def _p_bamboohr(s: str):
    d = fetch_json(f"https://{s}.bamboohr.com/careers/list")
    return len(d.get("result", [])) if isinstance(d, dict) and "result" in d else None


def _p_rippling(s: str):
    d = fetch_json(f"https://api.rippling.com/platform/api/ats/v1/board/{s}/jobs")
    return len(d) if isinstance(d, list) else None


def _p_jobvite(s: str):
    st, tx = fetch(f"https://jobs.jobvite.com/{s}/search")
    if st != 200:
        return None
    n = len(set(re.findall(r"/" + re.escape(s) + r"/job/([A-Za-z0-9]+)", tx)))
    return n or None


def _p_icims(s: str):
    st, tx = fetch(f"https://careers-{s}.icims.com/jobs/search?ss=1&in_iframe=1")
    if st != 200:
        return None
    n = len(set(re.findall(r"/jobs/(\d+)/", tx)))
    return n or None


def _p_personio(s: str):
    st, tx = fetch(f"https://{s}.jobs.personio.com/xml")
    return tx.count("<position>") if st == 200 and "<position>" in tx else None


def _p_teamtailor(s: str):
    st, tx = fetch(f"https://{s}.teamtailor.com/jobs.json")
    if st != 200:
        return None
    try:
        d = json.loads(tx)
    except Exception:
        return None
    return len(d) if isinstance(d, list) else None


PROBES = {
    "greenhouse": _p_greenhouse,
    "lever": _p_lever,
    "ashby": _p_ashby,
    "smartrecruiters": _p_smartrecruiters,
    "workable": _p_workable,
    "recruitee": _p_recruitee,
    "bamboohr": _p_bamboohr,
    "rippling": _p_rippling,
    "jobvite": _p_jobvite,
    "icims": _p_icims,
    "personio": _p_personio,
    "teamtailor": _p_teamtailor,
}

# Order matters: cheapest + most common first, so most companies resolve in
# a handful of requests.
PROBE_ORDER = [
    "greenhouse", "lever", "ashby", "smartrecruiters", "workable",
    "bamboohr", "rippling", "recruitee", "jobvite", "icims",
    "teamtailor", "personio",
]

# Probed platforms, plus Workday, which is searched after these miss.
PROBEABLE = len(PROBE_ORDER) + 1

# Adapters exist for these, but discovery cannot reach them: each addresses a
# board by an opaque tenant id or GUID with no convention that maps a company
# NAME onto it, so there is nothing to guess. They enter the registry by pasting
# a posting URL (`ingest-urls`), which contains the id. Documented rather than
# silently absent: "discover found nothing" otherwise reads as "this employer
# has no public board", which for these four is wrong.
UNPROBEABLE = ("oracle_orc", "eightfold", "phenom", "paylocity")


def discover_company(
    name: str, *, hints: Iterable[str] = (), platforms: Iterable[str] | None = None,
    max_slugs: int = 4, workers: int = 12,
) -> dict | None:
    """Probe platforms x slug candidates. Returns a registry entry or None.

    Parallel across PLATFORMS only. Each platform is a distinct host, so the
    per-host throttle in http.py is still respected - we are not hammering
    anyone, just no longer waiting on greenhouse before asking lever.
    """
    cands = slug_candidates(name, hints)[:max_slugs]
    order = list(platforms) if platforms else PROBE_ORDER

    def probe(plat: str, slug: str):
        try:
            return plat, slug, PROBES[plat](slug)
        except Exception:
            return plat, slug, None

    for slug in cands:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(probe, p, slug) for p in order]
            results = []
            for f in as_completed(futures):
                plat, s, n = f.result()
                # `if n:` dropped any employer with zero openings on the day
                # discovery ran, permanently, even though the board was real and
                # correctly identified. Keep it; n == 0 just sorts last.
                if n is not None:
                    results.append((PROBE_ORDER.index(plat) if plat in PROBE_ORDER else 99,
                                    -1 if n else 0,
                                    plat, s, n))
        if results:
            _, _, plat, s, n = sorted(results)[0]   # reliable platform, roles first
            return {"name": name, "ats": plat, "slug": s, "open_roles": n}
    # Workday is the biggest enterprise ATS and had no probe here at all:
    # discover_workday was fully implemented and never called. It walks tenant x
    # datacenter x site so it is slow, which is why it runs only after the fast
    # probes miss rather than alongside them.
    wd = discover_workday(name)
    if wd:
        wd.setdefault("name", name)
        return wd
    return None


def discover_many(names: list[str], *, lane: str = "discovered", tier: str = "C",
                  workers: int = 6, on_result=None) -> list[dict]:
    """Probe many companies concurrently."""
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(discover_company, n): n for n in names}
        for f in as_completed(futs):
            name = futs[f]
            try:
                entry = f.result()
            except Exception:
                entry = None
            if entry:
                entry.update(lane=lane, tier=tier, active=True)
                out.append(entry)
            if on_result:
                on_result(name, entry)
    return out


def discover_workday(name: str, hints: Iterable[str] = ()) -> dict | None:
    """Workday tenants are guessable too, but the datacenter shard is not, so
    walk the common shards."""
    for slug in slug_candidates(name, hints)[:3]:
        for dc in ("wd1", "wd5", "wd3", "wd12", "wd103", "wd2"):
            for site in ("External", "Careers", f"{slug}_Careers", "External_Career_Site", "en-US"):
                url = f"https://{slug}.{dc}.myworkdayjobs.com/wday/cxs/{slug}/{site}/jobs"
                st, tx = fetch(url, method="POST",
                               json_body={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                               headers={"Content-Type": "application/json"}, tries=1, timeout=12)
                if st == 200 and '"jobPostings"' in tx:
                    try:
                        total = json.loads(tx).get("total", 0)
                    except Exception:
                        total = 0
                    if total:
                        return {"name": name, "ats": "workday", "tenant": slug,
                                "dc": dc, "site": site, "open_roles": total}
    return None
