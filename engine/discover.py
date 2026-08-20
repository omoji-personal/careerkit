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

# Words too common to identify a company on their own. Any of these as a bare
# slug is far more likely to be somebody else's board than the employer meant.
_GENERIC_FIRST = {
    "national", "american", "united", "general", "first", "premier", "advanced",
    "allied", "atlantic", "pacific", "central", "northern", "southern", "eastern",
    "western", "capital", "summit", "pinnacle", "apex", "vertex", "delta", "alpha",
    "omega", "prime", "core", "next", "new", "modern", "future", "smart", "bright",
    "clear", "direct", "express", "elite", "select", "superior", "standard",
    "quality", "reliable", "trusted", "secure", "safe", "green", "blue", "red",
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
    # The first word alone, but only when it is doing the identifying. Probing
    # "National Public Radio" produced the candidate "national", which is a live
    # greenhouse board belonging to a public-affairs firm in Toronto. Its jobs
    # would have entered the report as NPR's. The first word is redundant when a
    # company is a single word (it equals `joined` already) and it is a weak
    # identifier the moment the name has several, so require it to be both
    # distinctive and most of the name.
    first = core[0] if len(core) == 2 and core[0] not in _GENERIC_FIRST else ""
    # Initials only when there are enough of them. A two-letter slug is almost
    # always somebody else: probing "Fisher Phillips" found lever:fp, a Polish IT
    # company in Gliwice, and "DLA Piper" found recruitee:dp, a Belgian firm in
    # West-Vlaanderen. Both would have been registered as the law firm and polled
    # forever, quietly feeding the wrong company's jobs into the report.
    initials = "".join(w[0] for w in core) if len(core) > 2 else ""

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
    """0 openings is indistinguishable from "no such company" here, so 0 means NOT FOUND.

    SmartRecruiters answers 200 with {"totalFound": 0} for ANY slug, including
    "asdfqwerzxcv". Returning 0 made every guessed slug look like a discovered
    board: one run over 30 company names registered 27 phantom employers, each of
    which would then be polled forever and always return nothing. There is no
    other existence check; api.smartrecruiters.com/v1/companies/<slug> 404s even
    for real companies, and careers.smartrecruiters.com/<anything> returns the
    same SPA shell. So a real board has to prove itself with at least one posting.
    The cost is a genuinely empty SmartRecruiters board going undiscovered, which
    is much cheaper than a registry full of employers that do not exist."""
    d = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1")
    if not isinstance(d, dict) or "totalFound" not in d:
        return None
    return d["totalFound"] or None


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


def _p_hrmdirect(s: str):
    st, tx = fetch(f"https://{s}.hrmdirect.com/employment/job-openings.php?search=true")
    if st != 200 or not re.search(r"<title[^>]*>\s*Careers At\b", tx, re.I):
        return None
    return len(set(re.findall(r'\bdata-req-id=["\']([^"\']+)', tx, re.I)))


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
    "hrmdirect": _p_hrmdirect,
    "teamtailor": _p_teamtailor,
}

# Order matters: cheapest + most common first, so most companies resolve in
# a handful of requests.
PROBE_ORDER = [
    "greenhouse", "lever", "ashby", "smartrecruiters", "workable",
    "bamboohr", "rippling", "recruitee", "jobvite", "icims",
    "teamtailor", "personio", "hrmdirect",
]

# Probed platforms, plus Workday, which is searched after these miss.
PROBEABLE = len(PROBE_ORDER) + 1

# Adapters exist for these, but name-based discovery cannot reach them safely.
# Some use opaque tenant identifiers; Pinpoint and NEOGOV use human-readable
# slugs that are still unsafe to guess because a valid collision can belong to
# another employer. They enter the registry by pasting a posting URL
# (`ingest-urls`), which carries an exact address. Documented rather than
# silently absent: "discover found nothing" otherwise reads as "this employer
# has no public board", which is wrong.
_REJECTED: list[dict] = []

UNPROBEABLE = (
    "oracle_orc", "eightfold", "phenom", "paylocity", "pinpoint", "neogov",
)


# --------------------------------------------------------------------------
# Does the board that answered actually belong to the company we asked for?
# --------------------------------------------------------------------------
# Every collision so far was the same mistake: a guessed slug resolved to a real
# board, the probe counted its postings, and nobody asked whose board it was.
# "Fisher Phillips" found a Polish IT company at lever:fp. "DLA Piper" found a
# Belgian firm at recruitee:dp. "National Public Radio" found a Toronto public
# affairs firm at greenhouse:national. Each was patched by narrowing the slug
# generator, which is guessing about guessing.
#
# Some platforms simply tell you. Greenhouse returns {"name": "Stripe"} from its
# board endpoint, and for the NPR case it returns "NATIONAL", which is the other
# company's actual name and settles the question outright. Where a name is
# available this is evidence rather than heuristic, so it is checked first and
# a mismatch is disqualifying.

def _board_name(ats: str, slug: str) -> str | None:
    """The company name the board reports for itself, when it reports one."""
    try:
        if ats == "greenhouse":
            d = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
            return (d or {}).get("name") or None
    except Exception:
        return None
    return None


def _name_matches(target: str, board_name: str) -> bool:
    """Does the board's own name account for the company we searched for?

    Deliberately asks what fraction of the TARGET the board name covers, not the
    other way round. "NATIONAL" contains all of itself and would pass any
    shorter-string test, while covering only one word of "National Public
    Radio", which is exactly the collision being caught.
    """
    def words(x):
        x = re.sub(r"[^\w\s]", " ", (x or "").lower())
        return {w for w in x.split() if w and w not in _STOP}

    want, got = words(target), words(board_name)
    if not want or not got:
        return True                    # nothing to judge on; leave it to the caller
    return len(want & got) / len(want) >= 0.6


def verify_board(name: str, entry: dict) -> tuple[bool, str]:
    """Is this discovered board really this employer's? Returns (ok, why)."""
    board_name = _board_name(entry.get("ats", ""), entry.get("slug", ""))
    if board_name is None:
        return True, "platform does not publish a board name; not verifiable"
    if _name_matches(name, board_name):
        return True, f"board reports itself as {board_name!r}"
    return False, (f"board {entry.get('ats')}:{entry.get('slug')} reports itself as "
                   f"{board_name!r}, which is not {name!r}")


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
            for _, _, plat, s_, n in sorted(results):
                entry = {"name": name, "ats": plat, "slug": s_, "open_roles": n}
                ok, why = verify_board(name, entry)
                if ok:
                    entry["verified"] = why
                    return entry
                entry["rejected"] = why
                _REJECTED.append(entry)
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
