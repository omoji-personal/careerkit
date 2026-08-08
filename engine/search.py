"""Search-driven discovery: the flywheel's entry point.

ATS platforms do not federate with each other, but search engines federate all
of them. So instead of guessing company names, we search the ATS domains
themselves, then resolve every hit back into an employer + platform + slug and
push it into the registry. Unknown employers become known ones, permanently.

Two backends:
  * bing_rss - key-free, works, but shallow (~10 hits) and under load it
    silently DROPS the site: operator and returns generic web results. So every
    hit is host-validated against the operator; a degraded response yields zero
    rather than junk.
  * the query matrix is also dumped to out/search-queries.txt so a higher
    quality search tool can be run over it and the results fed back via
    `careerkit.py ingest-urls`.
"""
from __future__ import annotations

import re
import urllib.parse as up
from dataclasses import dataclass

from .http import fetch

# --------------------------------------------------------------------------
# Title ontology. The whole point: the target job is frequently NOT titled
# "Salesforce Solution Architect". In-house postings hide it under CRM,
# business applications, constituent systems, advancement systems, etc.
# --------------------------------------------------------------------------

def set_core_terms(terms):
    """Discovery queries, from the user's profile.

    CORE_TERMS below was a hardcoded list of one person's Salesforce
    searches until 2026-08-05, so `careerkit.py search` found nothing for anyone
    whose field was different. Empty input clears the list: keeping the previous
    profile's terms leaks one person's search into another instance when the
    module is reused in a long-lived process."""
    global CORE_TERMS
    clean = [f'"{t}"' if not str(t).startswith('"') else str(t)
             for t in (terms or []) if t and str(t).strip()]
    CORE_TERMS = clean


# Discovery queries. EMPTY by default and filled from the user's profile by
# set_core_terms(). Held ~30 hardcoded Salesforce/CRM/nonprofit
# searches until 2026-08-06, in a repo whose README promises nothing personal
# lives here; anyone in another field also got zero discovery hits from them.
CORE_TERMS: list[str] = []

ATS_DOMAINS = [
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "jobs.smartrecruiters.com",
    "apply.workable.com",
    "myworkdayjobs.com",
    "jobs.jobvite.com",
    "icims.com",
    "recruiting.paylocity.com",
    "bamboohr.com/careers",
    "hrmdirect.com/employment",
    "recruitee.com",
    "applytojob.com",
    "breezy.hr",
    "ats.rippling.com",
    "governmentjobs.com",
    "taleo.net",
    "eightfold.ai",
    "avature.net",
    "dayforcehcm.com",
]

# Geography for discovery queries. Held one person's home metro and state
# until 2026-08-06; set_geo_terms() now takes them from the profile's metros.
GEO_TERMS = ["remote", '"united states"']


def set_geo_terms(metros):
    global GEO_TERMS
    clean = []
    for metro in metros or []:
        value = str(metro or "").strip()
        # Scoring accepts /raw regex/ metros, but sending regex syntax to a web
        # search engine produces junk queries such as '"/,\\s?ga\\b/"'. The
        # profile's ordinary city/state entries already carry the useful intent.
        if not value or (value.startswith("/") and value.endswith("/")):
            continue
        clean.append(value)
    GEO_TERMS = ["remote", '"united states"'] + [f'"{m}"' for m in clean]


def build_query_matrix(*, full: bool = False) -> list[str]:
    """Cross ATS domains with the title ontology."""
    terms = CORE_TERMS if full else CORE_TERMS[:16]
    domains = ATS_DOMAINS if full else ATS_DOMAINS[:10]
    out = []
    for d in domains:
        for t in terms:
            out.append(f"site:{d} {t}")
    for t in CORE_TERMS[:10]:
        for g in GEO_TERMS:
            out.append(f"{t} {g} jobs")
    return out


# --------------------------------------------------------------------------
# Result -> employer resolution. This is what makes discovery durable.
# --------------------------------------------------------------------------

@dataclass
class Hit:
    url: str
    title: str
    ats: str = ""
    slug: str = ""
    company_guess: str = ""


_URL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_app\?for=)?([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?:j/)?([a-z0-9_-]+)", re.I)),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([a-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"https?://([a-z0-9_-]+)\.recruitee\.com", re.I)),
    ("bamboohr", re.compile(r"https?://([a-z0-9_-]+)\.bamboohr\.com", re.I)),
    ("hrmdirect", re.compile(r"https?://([a-z0-9_-]+)\.hrmdirect\.com", re.I)),
    ("jazzhr", re.compile(r"https?://([a-z0-9_-]+)\.applytojob\.com", re.I)),
    ("breezy", re.compile(r"https?://([a-z0-9_-]+)\.breezy\.hr", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/([a-z0-9_-]+)", re.I)),
    ("teamtailor", re.compile(r"https?://([a-z0-9_-]+)\.teamtailor\.com", re.I)),
    ("icims", re.compile(r"https?://careers-([a-z0-9_-]+)\.icims\.com", re.I)),
    # Added 2026-08-06. Without these five, ingest-urls dropped exactly the
    # enterprise boards they host, silently, while the user believed the
    # employer had been registered.
    ("personio", re.compile(r"https?://([a-z0-9_-]+)\.jobs\.personio\.(?:com|de)", re.I)),
    ("phenom", re.compile(r"https?://([a-z0-9_-]+)\.phenompeople\.com", re.I)),
    ("paylocity", re.compile(r"recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/[A-Za-z]+/([A-Za-z0-9-]+)", re.I)),
    ("eightfold", re.compile(r"jobs\.eightfold\.ai/(?:careers/?)?(?:job/\d+)?[^?]*\?[^#]*domain=([a-z0-9_.-]+)", re.I)),
    ("oracle_orc", re.compile(r"https?://([a-z0-9_-]+)\.fa\.[a-z0-9]+\.oraclecloud\.com", re.I)),
]

# Platforms whose registry entry needs more than a slug. resolve() returns the
# slug it could see; these tell the caller what is still missing rather than
# letting a half-formed entry into the registry.
EXTRA_CONFIG = {
    "oracle_orc": ("host", "site"),
    "phenom": ("host",),
    "eightfold": ("domain",),
    "paylocity": ("guid",),
}
_WORKDAY_RE = re.compile(r"https?://([a-z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z-]+/)?([A-Za-z0-9_-]+)", re.I)


def resolve(url: str, title: str = "") -> Hit:
    """Map a job URL back to (platform, slug) so it can be polled directly."""
    h = Hit(url=url, title=title)
    m = _WORKDAY_RE.search(url)
    if m:
        h.ats, h.slug = "workday", m.group(1)
        h.company_guess = m.group(1)
        return h
    for ats, pat in _URL_PATTERNS:
        m = pat.search(url)
        if m:
            h.ats, h.slug = ats, m.group(1)
            h.company_guess = m.group(1).replace("-", " ").replace("_", " ").title()
            return h
    return h


def workday_parts(url: str) -> dict | None:
    m = _WORKDAY_RE.search(url)
    if not m:
        return None
    return {"tenant": m.group(1), "dc": m.group(2), "site": m.group(3)}


# --------------------------------------------------------------------------
# Bing RSS backend
# --------------------------------------------------------------------------

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_LINK = re.compile(r"<link>(.*?)</link>", re.S)


def bing_rss(query: str, count: int = 50) -> list[Hit]:
    """Run one query. Host-validate against any site: operator so a degraded
    response (Bing silently ignoring site:) contributes nothing instead of
    polluting the registry with google.com and wikipedia."""
    st, tx = fetch(f"https://www.bing.com/search?format=rss&q={up.quote_plus(query)}&count={count}")
    if st != 200:
        return []
    site_m = re.search(r"site:(\S+)", query)
    required = site_m.group(1).lower() if site_m else None

    hits = []
    for block in _ITEM.findall(tx):
        t, l = _TITLE.search(block), _LINK.search(block)
        if not l:
            continue
        url = l.group(1).strip()
        if required and required not in url.lower():
            continue          # degraded response, or an unrelated result
        hits.append(resolve(url, (t.group(1).strip() if t else "")))
    return hits


def run_matrix(queries: list[str], *, limit: int | None = None) -> tuple[list[Hit], dict]:
    """Execute queries, return hits plus a per-query yield table for honesty
    about what actually worked."""
    hits, stats = [], {}
    for q in queries[:limit] if limit else queries:
        got = bing_rss(q)
        stats[q] = len(got)
        hits.extend(got)
    seen, uniq = set(), []
    for h in hits:
        if h.url in seen:
            continue
        seen.add(h.url)
        uniq.append(h)
    return uniq, stats
