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
from dataclasses import dataclass, field
from html import unescape

from .http import UnsafeExternalURL, fetch

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
    clean = []
    for term in terms or []:
        if not term or not str(term).strip():
            continue
        value = str(term) if str(term).startswith('"') else f'"{term}"'
        if value not in clean:
            clean.append(value)
    CORE_TERMS = clean


# Discovery queries. EMPTY by default and filled from the user's profile by
# set_core_terms(). Held ~30 hardcoded Salesforce/CRM/nonprofit
# searches until 2026-08-06, in a repo whose README promises nothing personal
# lives here; anyone in another field also got zero discovery hits from them.
CORE_TERMS: list[str] = []

# Every automatic domain must resolve to an adapter CareerKit can actually
# poll.  The old list mixed supported boards with six aspirational families
# (JazzHR, Breezy, GovernmentJobs, Taleo, Avature and Dayforce), so a search hit
# looked like coverage even though ingest could never register it.  Keep the
# family beside the domain so the CLI can report family coverage, not merely a
# flattering count of queries.
ATS_DOMAIN_FAMILIES: list[tuple[str, str]] = [
    ("greenhouse", "boards.greenhouse.io"),
    ("greenhouse", "job-boards.greenhouse.io"),
    ("lever", "jobs.lever.co"),
    ("ashby", "jobs.ashbyhq.com"),
    ("smartrecruiters", "jobs.smartrecruiters.com"),
    ("workable", "apply.workable.com"),
    ("recruitee", "recruitee.com"),
    ("bamboohr", "bamboohr.com/careers"),
    ("rippling", "ats.rippling.com"),
    ("teamtailor", "teamtailor.com"),
    ("pinpoint", "pinpointhq.com"),
    ("neogov", "governmentjobs.com/careers"),
    ("workday", "myworkdayjobs.com"),
    ("oracle_orc", "oraclecloud.com"),
    ("eightfold", "eightfold.ai"),
    ("phenom", "phenompeople.com"),
    ("icims", "icims.com"),
    ("jobvite", "jobs.jobvite.com"),
    ("jobvite", "app.jobvite.com"),
    ("hrmdirect", "hrmdirect.com/employment"),
    ("paylocity", "recruiting.paylocity.com"),
    ("personio", "jobs.personio.com"),
    ("personio", "jobs.personio.de"),
]
ATS_DOMAINS = [domain for _, domain in ATS_DOMAIN_FAMILIES]

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
    """Cross ATS domains with the title ontology, term first.

    Domain-first ordering made the default 60-query budget spend every request
    on the first few platforms.  Term-first ordering gives every supported ATS
    family a probe before returning to the second title term.
    """
    terms = CORE_TERMS if full else CORE_TERMS[:16]
    out = []
    for t in terms:
        for d in ATS_DOMAINS:
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
    # The complete registry address. A single slug cannot represent Workday's
    # tenant/datacenter/site or Oracle's host/site, and pretending it could made
    # those URLs resolve successfully but then get skipped by ingest.
    address: dict[str, str] = field(default_factory=dict)


_SLUG_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_app\?for=)?([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?!j(?:/|$))([a-z0-9_-]+)", re.I)),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/(?:careers/)?([a-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"https?://([a-z0-9_-]+)\.recruitee\.com", re.I)),
    ("bamboohr", re.compile(r"https?://([a-z0-9_-]+)\.bamboohr\.com", re.I)),
    ("hrmdirect", re.compile(r"https?://([a-z0-9_-]+)\.hrmdirect\.com", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/([a-z0-9_-]+)", re.I)),
    ("teamtailor", re.compile(r"https?://([a-z0-9_-]+)\.teamtailor\.com", re.I)),
    ("icims", re.compile(r"https?://careers-([a-z0-9_-]+)\.icims\.com", re.I)),
    ("personio", re.compile(r"https?://([a-z0-9_-]+)\.jobs\.personio\.(?:com|de)", re.I)),
]

# Recognise a few common but unsupported boards so ingest can say "no adapter"
# instead of claiming their URL shape is unknown. They are deliberately absent
# from ATS_DOMAIN_FAMILIES and therefore never searched automatically.
_UNSUPPORTED_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("jazzhr", re.compile(r"https?://([a-z0-9_-]+)\.applytojob\.com", re.I)),
    ("breezy", re.compile(r"https?://([a-z0-9_-]+)\.breezy\.hr", re.I)),
]
_WORKDAY_RE = re.compile(r"https?://([a-z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z-]+/)?([A-Za-z0-9_-]+)", re.I)
_PAYLOCITY_ALL_RE = re.compile(
    r"/recruiting/jobs/all/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)",
    re.I,
)
_PAYLOCITY_DETAILS_RE = re.compile(r"/recruiting/jobs/details/[^/?#]+", re.I)
_ORACLE_SITE_RE = re.compile(
    r"/CandidateExperience/(?:[^/?#]+/)?sites/([^/?#]+)/"
    r"(?:job/[^/?#]+|requisitions/preview/[^/?#]+)",
    re.I,
)


def _pattern_host_matches(ats: str, host: str) -> bool:
    """Prove a regex match belongs to the ATS host, not a lookalike host.

    The path regexes predate URL parsing and deliberately remain small, but a
    substring such as ``notjobs.lever.co`` must never satisfy the real
    ``jobs.lever.co`` pattern.  Check the parsed hostname independently before
    allowing any of those patterns to identify a board.
    """
    exact = {
        "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io"},
        "lever": {"jobs.lever.co"},
        "ashby": {"jobs.ashbyhq.com"},
        "smartrecruiters": {"jobs.smartrecruiters.com"},
        "workable": {"apply.workable.com"},
        "jobvite": {"jobs.jobvite.com"},
        "rippling": {"ats.rippling.com"},
    }
    if ats in exact:
        return host in exact[ats]
    suffix = {
        "recruitee": "recruitee.com",
        "bamboohr": "bamboohr.com",
        "hrmdirect": "hrmdirect.com",
        "teamtailor": "teamtailor.com",
        "icims": "icims.com",
        "personio": "jobs.personio.com",
        "jazzhr": "applytojob.com",
        "breezy": "breezy.hr",
    }.get(ats)
    if not suffix:
        return False
    if ats == "personio" and host.endswith(".jobs.personio.de"):
        return True
    return host.endswith("." + suffix) and host != suffix


def _hit(url: str, title: str, ats: str, address: dict[str, str],
         company_guess: str = "") -> Hit:
    """Build a backwards-compatible Hit while keeping its real address."""
    slug = (address.get("slug") or address.get("tenant") or
            address.get("domain") or address.get("guid") or "")
    guess = company_guess or slug
    return Hit(url=url, title=title, ats=ats, slug=slug,
               company_guess=guess.replace("-", " ").replace("_", " ").title(),
               address=address)


def _page_value(url: str, patterns: tuple[re.Pattern, ...]) -> str:
    """Fetch a legacy redirect page only when its URL omits the board address."""
    # These URLs come from paste/search input, not from the trusted registry.
    # They can redirect, so they need the same DNS/IP/redirect validation as JD
    # enrichment; the ordinary cacheable fetch path is not an SSRF boundary.
    try:
        status, text = fetch(url, safe_external=True)
    except UnsafeExternalURL:
        # A refused destination is an unresolvable candidate, not a reason to
        # abort every other URL in the ingest batch.
        return ""
    if status != 200:
        return ""
    text = unescape(text)
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def resolve(url: str, title: str = "") -> Hit:
    """Map a job URL to a platform and its complete pollable board address."""
    try:
        parsed = up.urlsplit(url)
    except ValueError:
        return Hit(url=url, title=title)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme.casefold() not in {"http", "https"} or not host
            or parsed.username or parsed.password):
        return Hit(url=url, title=title)

    # Resolver patterns must never inspect the raw query/fragment. A pasted URL
    # such as ``unrelated.example/?next=https://jobs.lever.co/acme`` contains an
    # ATS-looking string but is not an ATS page; accepting it would permanently
    # register a board that the user never supplied. The handful of legitimate
    # query-addressed fields are parsed explicitly below.
    origin_path = f"https://{host}{parsed.path}"

    m = _WORKDAY_RE.search(origin_path)
    if m:
        return _hit(url, title, "workday", {
            "tenant": m.group(1), "dc": m.group(2), "site": m.group(3),
        }, m.group(1))

    # Oracle Candidate Experience URLs expose both fields the public adapter
    # needs. Preserve the complete DNS host; the old regex kept only its first
    # label and discarded the site entirely.
    if host.endswith(".oraclecloud.com") and ".fa." in host:
        site = _ORACLE_SITE_RE.search(parsed.path)
        if site:
            site_name = up.unquote(site.group(1))
            if not re.fullmatch(r"[A-Za-z0-9_-]+", site_name):
                return Hit(url=url, title=title, ats="oracle_orc")
            prefix = host.split(".fa.", 1)[0]
            return _hit(url, title, "oracle_orc", {
                "host": host, "site": site_name,
            }, prefix)

    # Eightfold's canonical host may be tenant-specific. The domain query is
    # sufficient when present; canonical job pages also publish it in their
    # HTML, so resolve that one missing value without guessing from a hostname.
    if host == "eightfold.ai" or host.endswith(".eightfold.ai"):
        values = up.parse_qs(parsed.query).get("domain", [])
        domain = values[0].strip() if values else ""
        if domain and not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
            domain = ""
        if not domain:
            domain = _page_value(url, (
                re.compile(r'[?&]domain=([a-z0-9.-]+)', re.I),
                re.compile(r'["\']domain["\']\s*:\s*["\']([a-z0-9.-]+)', re.I),
            ))
        address = {"domain": domain} if domain else {}
        if domain:
            address["host"] = f"https://{host}"
        guess = ((domain.split(".", 1)[0] if domain else "")
                 if host in ("eightfold.ai", "jobs.eightfold.ai")
                 else host.split(".", 1)[0])
        return _hit(url, title, "eightfold", address, guess)

    if host.endswith(".phenompeople.com"):
        origin = f"https://{host}"
        return _hit(url, title, "phenom", {"host": origin}, host.split(".", 1)[0])

    if host.endswith(".pinpointhq.com"):
        path = parsed.path
        pinpoint_path = re.match(
            r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:postings|jobs)(?:\.json|/|$)",
            path,
            re.I,
        )
        slug = host.removesuffix(".pinpointhq.com")
        if pinpoint_path and re.fullmatch(r"[a-z0-9-]+", slug):
            return _hit(url, title, "pinpoint", {"slug": slug})

    if host in {"www.governmentjobs.com", "governmentjobs.com"}:
        agency = re.match(r"^/careers/([a-z0-9_-]+)(?:/|$)", parsed.path, re.I)
        if not agency and parsed.path.casefold() == "/searchengine/jobsfeed":
            values = up.parse_qs(parsed.query).get("agency", [])
            value = values[0] if values else ""
            agency = (re.match(r"^([a-z0-9_-]+)$", value, re.I) if value else None)
        if agency:
            return _hit(url, title, "neogov", {"slug": agency.group(1)})
    if host == "agency.governmentjobs.com":
        agency = re.match(r"^/([a-z0-9_-]+)(?:/|$)", parsed.path, re.I)
        if agency:
            return _hit(url, title, "neogov", {"slug": agency.group(1)})

    if host == "recruiting.paylocity.com":
        board = _PAYLOCITY_ALL_RE.search(parsed.path)
        if board:
            guid = board.group(1)
            return _hit(url, title, "paylocity", {"guid": guid}, guid)
        if _PAYLOCITY_DETAILS_RE.search(parsed.path):
            # A Details URL exposes a job id, not the board GUID. Recognise the
            # platform but leave it intentionally unaddressed so ingest gives a
            # truthful instruction instead of registering a broken board.
            return Hit(url=url, title=title, ats="paylocity")

    # Legacy app.jobvite.com URLs redirect to a normal board, but the source URL
    # carries only a job id. The final HTML consistently links `/slug/jobs`.
    if host == "app.jobvite.com":
        slug = _page_value(url, (
            re.compile(r"jobs\.jobvite\.com/(?:careers/)?([a-z0-9_-]+)", re.I),
            re.compile(r"/([a-z0-9_-]+)/jobs(?:[/?#\"'])", re.I),
        ))
        return _hit(url, title, "jobvite", {"slug": slug} if slug else {})

    # Workable's shortened `/j/<job-id>` URL does not expose the account slug.
    # Its public page does publish a canonical account URL; resolve that rather
    # than registering the job id as if it were an employer board.
    if host == "apply.workable.com" and re.match(r"^/j/", parsed.path, re.I):
        slug = _page_value(url, (
            re.compile(r"apply\.workable\.com/([a-z0-9_-]+)/j/", re.I),
        ))
        return _hit(url, title, "workable", {"slug": slug} if slug else {})

    # Greenhouse's embedded application form is the one supported slug address
    # carried in a query parameter. Parse only its own host/path and validate
    # the value, rather than putting every query string back into regex scope.
    if (host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}
            and parsed.path.rstrip("/").casefold() == "/embed/job_app"):
        values = up.parse_qs(parsed.query).get("for", [])
        slug = str(values[0] if values else "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", slug):
            return _hit(url, title, "greenhouse", {"slug": slug})

    for ats, pat in _SLUG_PATTERNS:
        if not _pattern_host_matches(ats, host):
            continue
        m = pat.search(origin_path)
        if m:
            return _hit(url, title, ats, {"slug": m.group(1)})
    for ats, pat in _UNSUPPORTED_PATTERNS:
        if not _pattern_host_matches(ats, host):
            continue
        m = pat.search(origin_path)
        if m:
            return _hit(url, title, ats, {"slug": m.group(1)})
    return Hit(url=url, title=title)


def workday_parts(url: str) -> dict | None:
    hit = resolve(url)
    return hit.address if hit.ats == "workday" else None


def query_coverage(planned: list[str], attempted: list[str]) -> dict[str, int]:
    """Describe query and ATS-family coverage without counting skipped work."""
    domain_family = {
        _site_requirement(domain): family
        for family, domain in ATS_DOMAIN_FAMILIES
    }

    def families(queries: list[str]) -> set[str]:
        found = set()
        for query in queries:
            match = re.search(r"\bsite:(\S+)", query, re.I)
            requirement = _site_requirement(match.group(1)) if match else None
            if requirement in domain_family:
                found.add(domain_family[requirement])
        return found

    planned_families, attempted_families = families(planned), families(attempted)
    return {
        "queries_planned": len(planned),
        "queries_attempted": len(attempted),
        "families_planned": len(planned_families),
        "families_attempted": len(attempted_families),
    }


# --------------------------------------------------------------------------
# Bing RSS backend
# --------------------------------------------------------------------------

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_LINK = re.compile(r"<link>(.*?)</link>", re.S)


def _site_requirement(value: str) -> tuple[str, str] | None:
    """Normalize a ``site:host/path`` operand for host and path validation."""
    raw = str(value or "").strip().casefold().rstrip("/")
    if not raw or "://" in raw:
        return None
    host, separator, remainder = raw.partition("/")
    host = host.rstrip(".")
    if not host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    path = "/" + remainder.strip("/") if separator and remainder.strip("/") else ""
    return host, path


def bing_rss(query: str, count: int = 50) -> list[Hit]:
    """Run one query. Host-validate against any site: operator so a degraded
    response (Bing silently ignoring site:) contributes nothing instead of
    polluting the registry with google.com and wikipedia."""
    st, tx = fetch(f"https://www.bing.com/search?format=rss&q={up.quote_plus(query)}&count={count}")
    if st != 200:
        return []
    site_m = re.search(r"site:(\S+)", query)
    required = _site_requirement(site_m.group(1)) if site_m else None

    hits = []
    for block in _ITEM.findall(tx):
        t, l = _TITLE.search(block), _LINK.search(block)
        if not l:
            continue
        url = unescape(l.group(1).strip())
        try:
            parsed = up.urlsplit(url)
            result_host = (parsed.hostname or "").casefold().rstrip(".")
        except ValueError:
            continue
        if (parsed.scheme.casefold() not in {"http", "https"} or not result_host
                or parsed.username or parsed.password):
            continue
        if required:
            required_host, required_path = required
            if not (result_host == required_host or
                    result_host.endswith("." + required_host)):
                continue      # degraded response, or an unrelated result
            result_path = up.unquote(parsed.path or "/").casefold().rstrip("/")
            if required_path and not (
                    result_path == required_path or
                    result_path.startswith(required_path + "/")):
                continue
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
