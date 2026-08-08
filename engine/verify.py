"""Verify that a discovered board actually belongs to the company we asked for.

Slug guessing has a real precision problem: generic slugs are owned by unrelated
companies. Observed in the first discovery run - `greenhouse:community` is
"Rome Community Partners", not Community Brands; `recruitee:classy` redirects to
Huuuge Games; `icims:exponent` is Exponent Inc, an engineering consultancy, not
Exponent Partners.

So every discovered board is asked to state its own name, and that name is
matched against what we searched for. Three outcomes, because automated name
matching genuinely cannot separate "Exponent Partners" from "Exponent Inc":

  ok     - strong match, poll it
  review - plausible but ambiguous, poll it and show it to a human once
  bad    - first distinctive token disagrees, deactivate

Better to flag ambiguity than to quietly poll the wrong company forever.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .http import fetch, fetch_json

_NOISE = {
    "inc", "inc.", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "the", "group", "holdings", "a", "of", "and", "&", "careers", "career",
    "opportunities", "jobs", "job", "listings", "at", "us", "usa", "part",
}


def _norm(s: str) -> list[str]:
    s = re.sub(r"[^\w\s&]", " ", (s or "").lower())
    return [w for w in s.split() if w and w not in _NOISE]


def _flat(tokens: list[str]) -> str:
    return "".join(tokens)


# --- per-platform "what is your name?" -------------------------------------

def _name_greenhouse(slug: str) -> str | None:
    d = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    return (d or {}).get("name")


def _name_recruitee(slug: str) -> str | None:
    d = fetch_json(f"https://{slug}.recruitee.com/api/offers/")
    offers = (d or {}).get("offers") or []
    if not offers:
        return None
    # A redirected board reveals itself in the careers_url host.
    url = offers[0].get("careers_url", "")
    m = re.search(r"https?://([a-z0-9_-]+)\.recruitee\.com", url)
    if m and m.group(1) != slug:
        return f"__REDIRECT__{m.group(1)}"
    return offers[0].get("company_name")


def _name_workable(slug: str) -> str | None:
    d = fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=false")
    return (d or {}).get("name") or ((d or {}).get("account") or {}).get("name")


def _name_smartrecruiters(slug: str) -> str | None:
    d = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
    c = ((d or {}).get("content") or [{}])[0].get("company") or {}
    return c.get("name") or slug


def _name_ashby(slug: str) -> str | None:
    d = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = (d or {}).get("jobs") or []
    return jobs[0].get("organizationName") if jobs else None


def _name_from_title(url: str) -> str | None:
    st, tx = fetch(url)
    if st != 200:
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", tx, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def _name_icims(slug: str) -> str | None:
    return _name_from_title(f"https://careers-{slug}.icims.com/jobs/search?ss=1&in_iframe=1")


def _name_jobvite(slug: str) -> str | None:
    return _name_from_title(f"https://jobs.jobvite.com/{slug}/search")


def _name_teamtailor(slug: str) -> str | None:
    return _name_from_title(f"https://{slug}.teamtailor.com/jobs")


def _name_hrmdirect(slug: str) -> str | None:
    title = _name_from_title(
        f"https://{slug}.hrmdirect.com/employment/job-openings.php?search=true")
    return re.sub(r"^Careers At\s+", "", title or "", flags=re.I) or None


def _name_lever(slug: str) -> str | None:
    d = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(d, list) or not d:
        return None
    return (d[0].get("categories") or {}).get("company") or None


NAMERS = {
    "greenhouse": _name_greenhouse,
    "recruitee": _name_recruitee,
    "workable": _name_workable,
    "smartrecruiters": _name_smartrecruiters,
    "ashby": _name_ashby,
    "icims": _name_icims,
    "jobvite": _name_jobvite,
    "teamtailor": _name_teamtailor,
    "lever": _name_lever,
    "hrmdirect": _name_hrmdirect,
}
# bamboohr, rippling, personio expose no company name field -> always "review".


def compare(asked: str, declared: str | None) -> tuple[str, str]:
    """Returns (ok|review|bad, evidence)."""
    if declared and declared.startswith("__REDIRECT__"):
        return "bad", f"board redirects to '{declared[12:]}'"
    if not declared:
        return "review", "board states no company name"

    a, d = _norm(asked), _norm(declared)
    if not a or not d:
        return "review", f"declared '{declared[:60]}'"

    fa, fd = _flat(a), _flat(d)
    if fa == fd:
        return "ok", f"declared '{declared[:60]}'"

    if a[0] != d[0] and a[0] not in d and d[0] not in a:
        return "bad", f"declared '{declared[:60]}' - different company"

    # "Exponent Partners" vs a board declaring only "Exponent": the qualifier we
    # searched for is exactly what would distinguish it from "Exponent Inc",
    # and the board dropped it. Genuinely undecidable from the name - flag it.
    if len(a) >= 2 and len(d) == 1 and d[0] == a[0]:
        return "review", (f"declared only '{declared[:50]}' - could be a different "
                          f"'{a[0]}' company, confirm once")

    # Substring containment alone waved impostors through: "Par" is inside
    # "Parachute Health", "Acme" inside "Acme Plumbing", "Community" inside
    # "Rome Community Partners". Require the containment to start at a token
    # boundary AND the shorter name to be substantial, so a three-letter slug
    # cannot claim an unrelated company.
    # Bare containment waved impostors through: "Par" sits inside "Parachute
    # Health", "Acme" inside "Acme Plumbing", "Community" inside "Rome Community
    # Partners". Require the shorter name to be most of the longer one, so a
    # short slug cannot claim an unrelated company that merely contains it.
    shorter, longer = (fa, fd) if len(fa) <= len(fd) else (fd, fa)
    if shorter and shorter in longer and len(shorter) / max(1, len(longer)) >= 0.6:
        return "ok", f"declared '{declared[:60]}'"

    ratio = SequenceMatcher(None, fa, fd).ratio()
    # Jaccard, not intersection-over-asked: the latter gave "Acme" vs "Acme
    # Plumbing" a perfect 1.0 because every token of the asked name appeared.
    overlap = len(set(a) & set(d)) / max(1, len(set(a) | set(d)))
    if ratio >= 0.85 or overlap >= 0.75:
        return "ok", f"declared '{declared[:60]}'"
    return "review", f"declared '{declared[:60]}' - ambiguous, confirm once"


def verify_entry(entry: dict) -> dict:
    """Annotate a registry entry with verified + verify_note."""
    ats = entry.get("ats")
    slug = entry.get("slug") or entry.get("tenant") or ""
    namer = NAMERS.get(ats)
    if not namer:
        entry["verified"] = "review"
        entry["verify_note"] = f"{ats} exposes no company name; confirm once by eye"
        return entry
    try:
        declared = namer(slug)
    except Exception as e:
        entry["verified"] = "review"
        entry["verify_note"] = f"name lookup failed: {type(e).__name__}"
        return entry
    verdict, note = compare(entry.get("name", ""), declared)
    entry["verified"] = verdict
    entry["verify_note"] = note
    if verdict == "bad":
        entry["active"] = False
    return entry
