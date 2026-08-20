"""Cross-employer aggregator feeds.

Low precision by design: these ponds are stocked with startup SaaS and developer
roles. A prior 380-posting pull yielded zero usable hits. They are kept because
they are nearly free to poll and they occasionally surface an employer we did
not know about, which then gets promoted into the registry permanently. Treat
them as DISCOVERY, not as a shortlist.

Adapters needing a free key are included but stay dormant until the key exists
in keys.yaml, so wiring one up later is a one-line change, not a build.
"""
from __future__ import annotations

# Search terms are injected from the user profile by engine.cli.
TERMS: tuple = ("program manager",)  # placeholder; set_search_terms() overrides

def set_search_terms(terms):
    """Feeds search for these. An empty list used to silently keep the
    placeholder below, so a user with no search_terms in their profile
    unknowingly searched for someone else's job title."""
    global TERMS
    clean = tuple(t for t in (terms or []) if t and str(t).strip())
    if not clean:
        # Was: keep the placeholder and warn. The warning scrolls past and the
        # user then searches every keyword feed for a stranger's job title. An
        # empty tuple makes the term-driven feeds poll nothing and say so.
        print("  ! profile has no search_terms: keyword feeds will return nothing. "
              "Add search_terms to profile/profile.yaml.")
        TERMS = ()
        return
    TERMS = clean

import json
import math
import re
import xml.etree.ElementTree as ET
from importlib import metadata
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlsplit
from typing import Callable

from . import http, jd as _jd
from .http import fetch, fetch_json
from .models import Job, strip_html

FEEDS: dict[str, Callable[[dict], list[Job]]] = {}


def feed(name: str):
    def deco(fn):
        FEEDS[name] = fn
        return fn
    return deco


def _j(**kw) -> Job:
    kw.setdefault("lane", "aggregator")
    kw.setdefault("employer_tier", "D")
    return Job(**kw)


def _description_sections(details: dict) -> str:
    """Join the decision-bearing sections a feed publishes separately."""
    fields = (
        ("Summary", "JobSummary"),
        ("Duties", "MajorDuties"),
        ("Requirements", "Requirements"),
        ("Qualifications", "Qualifications"),
        ("Education", "Education"),
        ("Evaluation", "Evaluations"),
        ("Required documents", "RequiredDocuments"),
        ("Benefits", "Benefits"),
        ("Other information", "OtherInformation"),
    )
    sections = []
    for heading, key in fields:
        value = details.get(key)
        if isinstance(value, list):
            value = "\n".join(str(item) for item in value if item)
        elif isinstance(value, dict):
            value = "\n".join(str(item) for item in value.values() if item)
        text = strip_html(str(value or ""))
        if text:
            sections.append(f"{heading}\n{text}")
    return "\n\n".join(sections)


def _nonnegative_count(value) -> int | None:
    """Parse provider totals without accepting bools, negatives, or fractions."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _optional_provider_count(data: dict, key: str, label: str) -> int:
    """Return an optional provider count, exposing malformed metadata."""
    raw = data.get(key)
    if raw in (None, ""):
        return 0
    parsed = _nonnegative_count(raw)
    if parsed is None:
        http.mark_partial(f"{label} had invalid {key}")
        return 0
    return parsed


def _unique_mapping_page(
    items: list,
    *,
    label: str,
    seen: set[str],
    prior_pages: set[tuple[str, ...]],
    identity: Callable[[dict], object],
) -> tuple[list[dict], bool]:
    """Retain unique JSON rows and expose pagination replay as partial health."""
    page_seen: set[str] = set()
    page_order: list[str] = []
    unique: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            http.mark_partial(f"{label} item {index + 1} was not an object")
            continue
        key = str(identity(item) or "").strip()
        if not key:
            http.mark_partial(f"{label} item {index + 1} had no stable identity")
            key = json.dumps(item, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"))
        if key in page_seen:
            http.mark_partial(f"{label} repeated a job within the page")
            continue
        page_seen.add(key)
        page_order.append(key)
        if key in seen:
            http.mark_partial(f"{label} repeated a job from an earlier page")
            continue
        seen.add(key)
        unique.append(item)
    fingerprint = tuple(page_order)
    replayed = bool(fingerprint and fingerprint in prior_pages)
    if replayed:
        http.mark_partial(f"{label} replayed a prior page")
    elif fingerprint:
        prior_pages.add(fingerprint)
    return unique, replayed


def _required_feed_objects(data, key: str, label: str) -> list[dict] | None:
    """Validate a documented feed envelope before declaring a healthy zero."""
    if not isinstance(data, dict):
        http.mark_partial(f"{label} had an unexpected response shape")
        return None
    items = data.get(key)
    if not isinstance(items, list):
        http.mark_partial(f"{label} did not contain {key!r} as a list")
        return None
    valid = []
    for index, item in enumerate(items):
        if isinstance(item, dict):
            valid.append(item)
        else:
            http.mark_partial(f"{label} {key}[{index}] was not an object")
    return valid


def _usable_feed_row(*, identity, title, company, label: str) -> bool:
    """Reject structurally valid objects that cannot identify a posting."""
    if (str(identity or "").strip() and str(title or "").strip()
            and str(company or "").strip()):
        return True
    http.mark_partial(
        f"{label} lacked a stable identity, title, or company")
    return False


@feed("remotive")
def remotive(_cfg: dict) -> list[Job]:
    out, emitted = [], set()
    for term_index, q in enumerate(TERMS, 1):
        d = fetch_json(f"https://remotive.com/api/remote-jobs?search={quote_plus(q)}&limit=100")
        items = _required_feed_objects(
            d, "jobs", f"remotive term {term_index}")
        if items is None:
            continue
        if len(items) >= 100:
            http.mark_capped(f"remotive term {term_index} reached its 100-result cap")
        for item_index, j in enumerate(items, 1):
            identity = str(j.get("id") or j.get("url") or "").strip()
            if not _usable_feed_row(
                    identity=identity, title=j.get("title"),
                    company=j.get("company_name"),
                    label=f"remotive term {term_index} item {item_index}"):
                continue
            if identity and identity in emitted:
                continue
            if identity:
                emitted.add(identity)
            out.append(_j(
                company=j.get("company_name", ""), title=j.get("title", ""),
                url=j.get("url", ""), location=j.get("candidate_required_location", ""),
                description=strip_html(j.get("description", "")),
                posted_at=(j.get("publication_date") or "")[:10],
                comp_text=j.get("salary", "") or "", external_id=str(j.get("id", "")),
                remote_flag=True, source="remotive", raw=j,
            ))
    return out


@feed("remoteok")
def remoteok(_cfg: dict) -> list[Job]:
    d = fetch_json("https://remoteok.com/api")
    if not isinstance(d, list):
        if http.last_status() == 200:
            http.mark_partial("remoteok listing response had an unexpected shape")
        return []
    out = []
    for index, j in enumerate((d or [])[1:], 1):
        if not isinstance(j, dict):
            http.mark_partial(f"remoteok item {index} was not an object")
            continue
        if not _usable_feed_row(
                identity=j.get("id") or j.get("url"), title=j.get("position"),
                company=j.get("company"), label=f"remoteok item {index}"):
            continue
        out.append(_j(
            company=j.get("company", ""), title=j.get("position", ""),
            url=j.get("url", ""), location=j.get("location", "") or "Remote",
            description=strip_html(j.get("description", "")),
            posted_at=(j.get("date") or "")[:10],
            comp_min=j.get("salary_min"), comp_max=j.get("salary_max"),
            external_id=str(j.get("id", "")), remote_flag=True,
            source="remoteok", raw=j,
        ))
    return out


@feed("arbeitnow")
def arbeitnow(_cfg: dict) -> list[Job]:
    out, seen, prior_pages = [], set(), set()
    for page in (1, 2, 3):
        d = fetch_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        items = _required_feed_objects(
            d, "data", f"arbeitnow listing page {page}")
        if items is None:
            continue
        links = d.get("links") if isinstance(d.get("links"), dict) else {}
        meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
        last_page = _optional_provider_count(
            meta, "last_page", f"arbeitnow listing page {page}")
        if page == 3 and (links.get("next") or d.get("next") or
                          last_page > page):
            http.mark_capped("arbeitnow stopped at page 3 while another page was advertised")
        unique, replayed = _unique_mapping_page(
            items, label=f"arbeitnow listing page {page}", seen=seen,
            prior_pages=prior_pages,
            identity=lambda item: item.get("slug") or item.get("url"),
        )
        for j in unique:
            out.append(_j(
                company=j.get("company_name", ""), title=j.get("title", ""),
                url=j.get("url", ""), location=j.get("location", ""),
                description=strip_html(j.get("description", "")),
                remote_flag=bool(j.get("remote")), external_id=str(j.get("slug", "")),
                source="arbeitnow", raw=j,
            ))
        if replayed or (page > 1 and items and not unique):
            break
    return out


@feed("himalayas")
def himalayas(_cfg: dict) -> list[Job]:
    out, seen, prior_pages = [], set(), set()
    prior_page_was_short = False
    for page in (1, 2):
        d = fetch_json(f"https://himalayas.app/jobs/api?limit=100&offset={(page-1)*100}")
        items = _required_feed_objects(
            d, "jobs", f"himalayas listing page {page}")
        if items is None:
            continue
        if page == 2 and len(items) >= 100:
            http.mark_capped("himalayas reached its 200-result safety cap")
        if page > 1 and prior_page_was_short and items:
            http.mark_partial(
                "himalayas returned more jobs after a short page; fixed offsets "
                "may have skipped postings")
        unique, replayed = _unique_mapping_page(
            items, label=f"himalayas listing page {page}", seen=seen,
            prior_pages=prior_pages,
            identity=lambda item: item.get("guid") or item.get("applicationLink"),
        )
        for j in unique:
            out.append(_j(
                company=j.get("companyName", ""), title=j.get("title", ""),
                url=j.get("applicationLink", "") or j.get("guid", ""),
                location=", ".join(j.get("locationRestrictions", []) or []) or "Remote",
                description=strip_html(j.get("description", "")),
                comp_min=j.get("minSalary"), comp_max=j.get("maxSalary"),
                posted_at=str(j.get("pubDate", ""))[:10], remote_flag=True,
                external_id=str(j.get("guid", "")), source="himalayas", raw=j,
            ))
        if replayed or (page > 1 and items and not unique):
            break
        prior_page_was_short = len(items) < 100
    return out


@feed("weworkremotely")
def weworkremotely(cfg: dict) -> list[Job]:
    out = []
    # Was four categories chosen for the original author. Configurable now, with
    # a broader default so a user in an unrelated field is not silently narrowed.
    for cat in (cfg.get("categories") or (
            "remote-customer-support-jobs", "remote-management-and-finance-jobs",
            "remote-product-jobs", "remote-programming-jobs",
            "remote-sales-and-marketing-jobs", "remote-design-jobs")):
        st, tx = fetch(f"https://weworkremotely.com/categories/{cat}.rss")
        if st != 200:
            http.mark_partial(
                f"weworkremotely category listing returned HTTP {st}")
            continue
        try:
            root = ET.fromstring(tx)
        except (ET.ParseError, ValueError):
            http.mark_partial("weworkremotely category returned unusable RSS")
            continue
        root_name = str(root.tag).rsplit("}", 1)[-1].casefold()
        channel = next((node for node in root if
                        str(node.tag).rsplit("}", 1)[-1].casefold() == "channel"), None)
        if root_name != "rss" or channel is None:
            http.mark_partial("weworkremotely category had an unexpected RSS shape")
            continue

        for item in (node for node in channel if
                     str(node.tag).rsplit("}", 1)[-1].casefold() == "item"):
            def g(tag):
                node = next((child for child in item if
                             str(child.tag).rsplit("}", 1)[-1].casefold()
                             == tag.casefold()), None)
                return str(node.text or "").strip() if node is not None else ""
            title = g("title")
            company, _, role = title.partition(":")
            link = g("link")
            if not title or not link:
                http.mark_partial(
                    "weworkremotely category contained an invalid RSS item")
                continue
            out.append(_j(
                company=company.strip(), title=(role or title).strip(),
                url=link, location=g("region") or "Remote",
                description=strip_html(g("description")), posted_at=g("pubDate")[:16],
                remote_flag=True, external_id=link, source="weworkremotely", raw={},
            ))
    return out


@feed("jobicy")
def jobicy(_cfg: dict) -> list[Job]:
    out, emitted = [], set()
    for term_index, q in enumerate(TERMS, 1):
        d = fetch_json(f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={quote_plus(q)}")
        items = _required_feed_objects(d, "jobs", f"jobicy term {term_index}")
        if items is None:
            continue
        if len(items) >= 50:
            http.mark_capped(f"jobicy term {term_index} reached its 50-result cap")
        for item_index, j in enumerate(items, 1):
            identity = str(j.get("id") or j.get("url") or "").strip()
            if not _usable_feed_row(
                    identity=identity, title=j.get("jobTitle"),
                    company=j.get("companyName"),
                    label=f"jobicy term {term_index} item {item_index}"):
                continue
            if identity and identity in emitted:
                continue
            if identity:
                emitted.add(identity)
            out.append(_j(
                company=j.get("companyName", ""), title=j.get("jobTitle", ""),
                url=j.get("url", ""), location=j.get("jobGeo", ""),
                description=strip_html(j.get("jobDescription", "")),
                comp_min=j.get("annualSalaryMin"), comp_max=j.get("annualSalaryMax"),
                posted_at=(j.get("pubDate") or "")[:10], remote_flag=True,
                external_id=str(j.get("id", "")), source="jobicy", raw=j,
            ))
    return out


@feed("themuse")
def themuse(cfg: dict) -> list[Job]:
    """Free, no key. No full-text search, so filter by category + location."""
    out = []
    # Categories and locations were hardcoded to one person's search until
    # 2026-08-05, including a specific home metro. The Muse
    # has no full-text search, so a category list is unavoidable; it is now
    # configurable per user via profile/employers.yaml, and the location follows
    # the profile rather than a stranger's hometown.
    cats = cfg.get("categories") or [
        "Project & Product Management", "IT", "Data and Analytics",
        "Business & Strategy", "Customer Service", "Marketing", "Sales",
        "Human Resources", "Accounting and Finance", "Operations"]
    locs = ["Flexible / Remote"] + [l for l in (cfg.get("locations") or []) if l]

    def muse_identity(item: dict) -> object:
        refs = item.get("refs") or {}
        refs = refs if isinstance(refs, dict) else {}
        return item.get("id") or refs.get("landing_page", "")

    emitted: set[str] = set()
    for cat in cats:
        for loc in locs:
            seen, prior_pages = set(), set()
            for page in (0, 1):
                url = ("https://www.themuse.com/api/public/jobs?"
                       f"category={cat.replace(' ', '%20').replace('&', '%26')}"
                       f"&location={loc.replace(' ', '%20').replace('/', '%2F')}"
                       f"&page={page}&descending=true")
                d = fetch_json(url)
                label = f"themuse category/location page {page + 1}"
                items = _required_feed_objects(d, "results", label)
                if items is None:
                    continue
                page_count = _optional_provider_count(
                    d, "page_count", label)
                if page == 1 and (page_count > 2 or (not page_count and len(items) >= 20)):
                    http.mark_capped(
                        "themuse stopped at page 2 while more results may remain")
                unique, replayed = _unique_mapping_page(
                    items,
                    label=f"themuse category/location page {page + 1}",
                    seen=seen, prior_pages=prior_pages,
                    identity=muse_identity,
                )
                for j in unique:
                    identity = str(muse_identity(j))
                    if identity in emitted:
                        continue
                    emitted.add(identity)
                    out.append(_j(
                        company=(j.get("company") or {}).get("name", ""),
                        title=j.get("name", ""),
                        url=(j.get("refs") or {}).get("landing_page", ""),
                        location=", ".join(l.get("name", "") for l in (j.get("locations") or [])),
                        description=strip_html(j.get("contents", "")),
                        posted_at=(j.get("publication_date") or "")[:10],
                        department=(j.get("categories") or [{}])[0].get("name", ""),
                        external_id=str(j.get("id", "")), source="themuse", raw=j,
                    ))
                if replayed or (page > 0 and items and not unique):
                    break
    return out


@feed("workingnomads")
def workingnomads(_cfg: dict) -> list[Job]:
    d = fetch_json("https://www.workingnomads.com/api/exposed_jobs/")
    if not isinstance(d, list):
        if http.last_status() == 200:
            http.mark_partial("workingnomads listing response had an unexpected shape")
        return []
    out = []
    for index, j in enumerate(d or [], 1):
        if not isinstance(j, dict):
            http.mark_partial(f"workingnomads item {index} was not an object")
            continue
        if not _usable_feed_row(
                identity=j.get("id") or j.get("url"), title=j.get("title"),
                company=j.get("company_name"),
                label=f"workingnomads item {index}"):
            continue
        out.append(_j(
            company=j.get("company_name", ""), title=j.get("title", ""),
            url=j.get("url", ""), location=j.get("location", "") or "Remote",
            description=strip_html(j.get("description", "")),
            posted_at=(j.get("pub_date") or "")[:10], remote_flag=True,
            external_id=str(j.get("id", "")), source="workingnomads", raw=j,
        ))
    return out


# --------------------------------------------------------------------------
# Explicitly opt-in, third-party discovery. Freehire normalizes postings from
# many upstream systems; it is useful for finding employers/boards CareerKit
# does not know yet, but it is never an authoritative replacement for polling
# the employer's own board. Only source + host pairs that identify a first-party
# ATS are accepted. This intentionally excludes LinkedIn and other aggregators
# even when Freehire happens to carry them.
# --------------------------------------------------------------------------

_FREEHIRE_BASE = "https://freehire.me"
_FREEHIRE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_FREEHIRE_TERM_WORD = re.compile(r"[a-z0-9+#.]+")
_FREEHIRE_TERM_FILLER = frozenset({"a", "an", "and", "for", "in", "of", "or", "the", "to"})

# Upstream source labels are normalized only for policy checks and the
# CareerKit source namespace. The original value remains in Job.raw.
_FREEHIRE_SOURCE_ALIASES = {
    "oracle_orc": "oracle",
    "oraclecloud": "oracle",
    "sap_successfactors": "successfactors",
    "ukg_pro": "ukg",
    "ultipro": "ukg",
    "isolved": "isolvedhire",
    "zohorecruit": "zoho",
    "zoho_recruit": "zoho",
}

# Conservative provider-host pairs. A bare custom employer careers domain is
# not enough evidence that a result came directly from the ATS named in
# ``source``; those rows stay excluded until a verified provider host is added.
_FREEHIRE_FIRST_PARTY_ATS_HOSTS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
    "smartrecruiters": ("smartrecruiters.com",),
    "workable": ("workable.com",),
    "recruitee": ("recruitee.com",),
    "bamboohr": ("bamboohr.com",),
    "rippling": ("rippling.com",),
    "teamtailor": ("teamtailor.com",),
    # myworkdaysite.com postings are not yet resolvable/pollable by CareerKit;
    # accepting them here would promise a promotion path that does not exist.
    "workday": ("myworkdayjobs.com",),
    "oracle": ("oraclecloud.com",),
    "eightfold": ("eightfold.ai",),
    "phenom": ("phenompeople.com",),
    "icims": ("icims.com",),
    "jobvite": ("jobvite.com",),
    "paylocity": ("paylocity.com",),
    "personio": ("personio.com", "personio.de"),
    "hrmdirect": ("hrmdirect.com",),
    "pinpoint": ("pinpointhq.com",),
    "neogov": ("governmentjobs.com",),
    "comeet": ("comeet.com",),
    "ukg": ("ultipro.com", "ukg.com", "ukg.net"),
    "paycom": ("paycomonline.net",),
    "jazzhr": ("applytojob.com", "jazz.co"),
    "breezy": ("breezy.hr",),
    "manatal": ("manatal.com", "careers-page.com"),
    "zoho": ("zohorecruit.com", "zohopublic.com"),
    "successfactors": ("successfactors.com", "successfactors.eu"),
    "taleo": ("taleo.net",),
    "gem": ("gem.com",),
    "radancy": ("radancy.com",),
    "isolvedhire": ("isolvedhire.com",),
}


def _freehire_source(value) -> str:
    source = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")
    return _FREEHIRE_SOURCE_ALIASES.get(source, source)


def _host_is(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


_FREEHIRE_JOB_PATHS: dict[str, tuple[re.Pattern, ...]] = {
    "greenhouse": (re.compile(r"^/[^/]+/jobs?/[^/?#]+", re.I),),
    "lever": (re.compile(r"^/[^/]+/[^/?#]+", re.I),),
    "ashby": (re.compile(r"^/[^/]+/[^/?#]+", re.I),),
    "smartrecruiters": (re.compile(r"^/[^/]+/[^/?#]+", re.I),),
    "workable": (re.compile(r"^/[^/]+/j/[^/?#]+", re.I),),
    "recruitee": (re.compile(r"^/o/[^/?#]+", re.I),),
    "bamboohr": (re.compile(r"^/careers/(?:[^/?#]+/)?[^/?#]+", re.I),),
    "rippling": (re.compile(r"^/[^/]+/jobs/[^/?#]+", re.I),),
    "teamtailor": (re.compile(r"^/jobs/[^/?#]+", re.I),),
    "workday": (re.compile(r"/job/[^/?#]+", re.I),),
    "oracle": (re.compile(
        r"/CandidateExperience/(?:[^/?#]+/)?sites/[^/?#]+/"
        r"(?:job|requisitions/preview)/[^/?#]+", re.I),),
    "eightfold": (re.compile(r"/careers/job/[^/?#]+", re.I),),
    "phenom": (re.compile(r"/job/[^/?#]+", re.I),),
    "icims": (re.compile(r"/jobs/[0-9]+(?:/|$)", re.I),),
    "jobvite": (
        re.compile(r"^/(?:careers/)?[^/]+/job/[^/?#]+", re.I),
        re.compile(r"/(?:CompanyJobs/)?Job\.aspx$", re.I),
    ),
    "paylocity": (re.compile(r"^/Recruiting/Jobs/Details/[^/?#]+", re.I),),
    "personio": (re.compile(r"/job/[^/?#]+", re.I),),
    "hrmdirect": (re.compile(r"/employment/job-opening\.php$", re.I),),
    "pinpoint": (re.compile(r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?postings/[^/?#]+", re.I),),
    "neogov": (re.compile(r"^/careers/[^/]+/jobs/[0-9]+(?:/|$)", re.I),),
    "comeet": (re.compile(r"^/jobs/[^/]+/[^/?#]+", re.I),),
    "ukg": (
        re.compile(r"/OpportunityDetail(?:/|$)", re.I),
        re.compile(r"/job-details(?:/|$)", re.I),
    ),
    "paycom": (re.compile(r"/jobs/ViewJobDetails(?:/|$)", re.I),),
    "jazzhr": (re.compile(r"/(?:apply|jobs?)/[^/?#]+", re.I),),
    "breezy": (re.compile(r"^/p/[^/?#]+", re.I),),
    "manatal": (re.compile(r"/[^/]+/job/[^/?#]+", re.I),),
    "zoho": (re.compile(r"/(?:jobs/)?Careers/[^/?#]+", re.I),),
    "successfactors": (
        re.compile(r"/career(?:/|$)", re.I),
        re.compile(r"/job/[^/?#]+", re.I),
    ),
    "taleo": (re.compile(r"/careersection/[^/]+/jobdetail\.ftl$", re.I),),
    "gem": (re.compile(r"^/[^/]+/[^/?#]+", re.I),),
    "radancy": (re.compile(r"/jobs?/[^/?#]+", re.I),),
    "isolvedhire": (re.compile(r"/jobs/[0-9]+(?:/|$)", re.I),),
}


def _freehire_job_url(source: str, parsed) -> bool:
    """Require a provider-specific posting path, not merely a vendor host."""
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path or "/"

    # The broad suffix list proves provider ownership. These tighter host rules
    # distinguish the public job surfaces from corporate sites such as
    # greenhouse.io/pricing or lever.co/blog.
    exact_hosts = {
        "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io"},
        "lever": {"jobs.lever.co"},
        "ashby": {"jobs.ashbyhq.com"},
        "smartrecruiters": {"jobs.smartrecruiters.com"},
        "workable": {"apply.workable.com"},
        "rippling": {"ats.rippling.com"},
        "paylocity": {"recruiting.paylocity.com"},
        "neogov": {"governmentjobs.com", "www.governmentjobs.com"},
        "paycom": {"www.paycomonline.net", "paycomonline.net"},
        "gem": {"jobs.gem.com"},
    }
    if source in exact_hosts and host not in exact_hosts[source]:
        return False
    if source == "jobvite" and host not in {"jobs.jobvite.com", "app.jobvite.com"}:
        return False
    if source == "manatal" and not _host_is(host, "careers-page.com"):
        return False

    patterns = _FREEHIRE_JOB_PATHS.get(source, ())
    if not any(pattern.search(path) for pattern in patterns):
        return False

    query = parse_qs(parsed.query)
    if source == "hrmdirect" and not (query.get("req") or query.get("reqid")):
        return False
    if source == "jobvite" and host == "app.jobvite.com" and not query.get("j"):
        return False
    if source == "successfactors" and path.rstrip("/").casefold() == "/career":
        if not (query.get("career_job_req_id") or query.get("job")):
            return False
    if source == "taleo" and not (query.get("job") or query.get("jobid")):
        return False
    return True


def _freehire_direct(item: dict) -> tuple[str, str] | None:
    """Return (normalized ATS source, direct URL) only for an allowed pair."""
    source = _freehire_source(item.get("source"))
    suffixes = _FREEHIRE_FIRST_PARTY_ATS_HOSTS.get(source)
    url = str(item.get("url") or "").strip()
    if not suffixes or not url:
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or not any(_host_is(host, suffix) for suffix in suffixes)
            or not _freehire_job_url(source, parsed)):
        return None
    return source, url


def _freehire_title_match(title: str, term: str) -> bool:
    """Require every meaningful configured term token in the preview title."""
    title_words = set(_FREEHIRE_TERM_WORD.findall(str(title or "").casefold()))
    wanted = [word for word in _FREEHIRE_TERM_WORD.findall(term.casefold())
              if word not in _FREEHIRE_TERM_FILLER]
    return bool(wanted) and all(word in title_words for word in wanted)


def _freehire_int(cfg: dict, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(cfg.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"freehire {name} must be an integer") from exc
    if not low <= value <= high:
        raise ValueError(f"freehire {name} must be between {low} and {high}")
    return value


def _freehire_countries(cfg: dict) -> tuple[str, ...]:
    raw = cfg.get("countries") or ()
    raw = [raw] if isinstance(raw, str) else raw
    if not isinstance(raw, (list, tuple)):
        raise ValueError("freehire countries must be a list of two-letter codes")
    out = []
    for value in raw:
        code = str(value or "").strip().casefold()
        if not re.fullmatch(r"[a-z]{2}", code):
            raise ValueError("freehire countries must contain two-letter codes")
        if code not in out:
            out.append(code)
    return tuple(out)


def _freehire_salary(enrichment: dict) -> tuple[int | None, int | None, str, str]:
    """Preserve model-enriched pay as a note, never as a scoring number."""
    if not isinstance(enrichment, dict):
        return None, None, "", ""

    def number(value):
        if isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if number > 0 else None

    lo = number(enrichment.get("salary_min"))
    hi = number(enrichment.get("salary_max"))
    if lo is None and hi is None:
        return None, None, "", ""
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    # Freehire's enrichment pipeline extracts pay with a language model. Even a
    # plausible annual USD range is not equivalent to a structured employer
    # field and must never drive CareerKit's compensation gate. Keep every field
    # in Job.raw for this pull, while the durable NONNUMERIC note is explicit
    # that Job.to_row does not persist raw provider values. Putting the model's
    # numbers in comp_text would let extract_comp later rediscover them as if
    # CareerKit had parsed an employer salary statement.
    note = ("Freehire reported model-derived pay metadata; values omitted from "
            "scoring and storage (not employer-published)")
    return None, None, note, ""


def _freehire_merge(preview: dict, detail: dict) -> dict:
    merged = dict(preview)
    for key, value in detail.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _freehire_identity_text(value) -> str:
    return " ".join(_FREEHIRE_TERM_WORD.findall(str(value or "").casefold()))


def _freehire_url_identity(value) -> tuple | None:
    """Canonical comparison key for preview/detail continuity only."""
    try:
        parsed = urlsplit(str(value or "").strip())
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or not host:
        return None
    if port in (None, 443):
        port = 443
    query = tuple(sorted(
        (key, tuple(sorted(values)))
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
    ))
    return host, port, parsed.path.rstrip("/") or "/", query


def _freehire_detail_continuous(preview: dict, detail: dict) -> bool:
    """Bind a hydrated record to the exact preview selected for hydration."""
    for field in ("company", "title"):
        before = _freehire_identity_text(preview.get(field))
        after = _freehire_identity_text(detail.get(field))
        if after and before != after:
            return False

    before_source = _freehire_source(preview.get("source"))
    after_source = _freehire_source(detail.get("source"))
    if after_source and before_source != after_source:
        return False

    matched_terms = preview.get("_matched_terms") or ()
    detail_title = str(detail.get("title") or preview.get("title") or "")
    if matched_terms and not any(
            _freehire_title_match(detail_title, str(term)) for term in matched_terms):
        return False

    detail_changes_identity = bool(
        str(detail.get("url") or "").strip()
        or str(detail.get("external_id") or "").strip())
    if not detail_changes_identity:
        return True
    same_url = (_freehire_url_identity(preview.get("url")) is not None
                and _freehire_url_identity(preview.get("url"))
                == _freehire_url_identity(detail.get("url")))
    before_external = str(preview.get("external_id") or "").strip()
    after_external = str(detail.get("external_id") or "").strip()
    same_external = bool(before_external and after_external
                         and before_external == after_external)
    return same_url or same_external


@feed("freehire")
def freehire(cfg: dict) -> list[Job]:
    """Opt-in Freehire discovery bridge using previews before details.

    Only read-only public search/detail endpoints are used. The exact configured
    search terms plus ordinary request metadata go to Freehire; no CareerKit
    credentials, resume, claims, or application data are sent.
    """
    if cfg.get("active") is not True:
        return []

    pages = _freehire_int(cfg, "pages", 10, 1, 100)
    page_size = _freehire_int(cfg, "results_per_page", 100, 1, 100)
    detail_cap = _freehire_int(cfg, "detail_cap", 100, 1, 500)
    posted_days = _freehire_int(cfg, "posted_within_days", 14, 0, 3650)
    countries = _freehire_countries(cfg)
    canonical_enrichment = cfg.get("canonical_enrichment") is True

    candidates: dict[str, dict] = {}
    for term_index, raw_term in enumerate(TERMS, 1):
        term = str(raw_term or "").strip().strip('"').strip()
        if not term:
            continue
        fetched, provider_total, filled_budget = 0, None, False
        terminated_partial = False
        page_fingerprints: set[tuple[str, ...]] = set()
        seen_term_jobs: set[str] = set()
        expected_total: int | None = None
        for page in range(pages):
            params: list[tuple[str, str]] = [
                ("q", f'"{term}"'),
                ("limit", str(page_size)),
                ("offset", str(page * page_size)),
            ]
            if posted_days:
                params.append(("posted_within_days", str(posted_days)))
            if countries:
                # The current hosted API documents multi-select facet values as
                # one comma-separated parameter. Keep this wire shape explicit;
                # repeated params were accepted by the reviewed client but are
                # not the hosted service's current public contract.
                params.append(("countries", ",".join(countries)))
            url = f"{_FREEHIRE_BASE}/api/v1/jobs/search?{urlencode(params)}"
            response = fetch_json(url, safe_external=True)
            if not isinstance(response, dict):
                http.mark_partial(
                    f"freehire term {term_index} page {page + 1} returned "
                    f"HTTP {http.last_status() or 0} or unusable JSON")
                terminated_partial = True
                break
            items = response.get("data")
            if not isinstance(items, list):
                http.mark_partial(
                    f"freehire term {term_index} page {page + 1} had invalid data")
                terminated_partial = True
                break
            meta = response.get("meta")
            meta_bad = False
            if not isinstance(meta, dict):
                http.mark_partial(f"freehire term {term_index} had invalid pagination metadata")
                meta = {}
                meta_bad = True
            page_total = _nonnegative_count(meta.get("total"))
            if page_total is None:
                http.mark_partial(f"freehire term {term_index} had an invalid total")
                meta_bad = True
            if page_total is not None:
                provider_total = max(provider_total or 0, page_total)
                if expected_total is None:
                    expected_total = page_total
                elif page_total != expected_total:
                    http.mark_partial(
                        f"freehire term {term_index} page {page + 1} changed total "
                        f"from {expected_total} to {page_total}")
                    meta_bad = True
                if page * page_size + len(items) > page_total:
                    http.mark_partial(
                        f"freehire term {term_index} page {page + 1} returned rows "
                        "beyond its advertised total")
                    meta_bad = True
            for field, expected in (("limit", page_size), ("offset", page * page_size)):
                actual = _nonnegative_count(meta.get(field))
                if actual != expected:
                    http.mark_partial(
                        f"freehire term {term_index} page {page + 1} had invalid "
                        f"pagination {field}")
                    meta_bad = True
            ignored = meta.get("ignored_params") or []
            if isinstance(ignored, str):
                ignored = [ignored]
            if isinstance(ignored, dict):
                ignored = [ignored]
            if not isinstance(ignored, list):
                http.mark_partial(
                    f"freehire term {term_index} had invalid ignored-parameter metadata")
                meta_bad = True
                ignored = []
            ignored_names = set()
            for value in ignored:
                if isinstance(value, dict):
                    value = value.get("param") or value.get("name") or ""
                value = str(value or "").strip()
                if value:
                    ignored_names.add(value)
            requested = {name for name, _value in params}
            relevant_ignored = sorted(ignored_names & requested)
            if relevant_ignored:
                http.mark_partial(
                    f"freehire term {term_index} ignored requested parameter(s): "
                    f"{', '.join(relevant_ignored)}")
                meta_bad = True

            fingerprint = tuple(
                str(item.get("public_slug") or item.get("external_id") or item.get("url") or "")
                if isinstance(item, dict) else "<invalid>"
                for item in items
            )
            if items and fingerprint in page_fingerprints:
                http.mark_partial(
                    f"freehire term {term_index} page {page + 1} repeated a prior page")
                meta_bad = True
            page_fingerprints.add(fingerprint)
            page_identity_list = [
                str(item.get("public_slug") or item.get("external_id") or
                    item.get("url") or "").strip()
                for item in items if isinstance(item, dict)
            ]
            page_identity_list = [value for value in page_identity_list if value]
            page_identities = set(page_identity_list)
            if len(page_identity_list) != len(page_identities):
                http.mark_partial(
                    f"freehire term {term_index} page {page + 1} contained "
                    "duplicate job identities")
                meta_bad = True
            page_identities.discard("")
            duplicates = sorted(page_identities & seen_term_jobs)
            if duplicates:
                http.mark_partial(
                    f"freehire term {term_index} page {page + 1} repeated "
                    f"{len(duplicates)} job(s) from an earlier page")
                meta_bad = True
            seen_term_jobs.update(page_identities)

            fetched += len(items)
            for item in items:
                if not isinstance(item, dict):
                    http.mark_partial(
                        f"freehire term {term_index} page {page + 1} contained an invalid job")
                    continue
                slug = str(item.get("public_slug") or "").strip()
                title = str(item.get("title") or "").strip()
                company = str(item.get("company") or "").strip()
                direct = _freehire_direct(item)
                if not slug or not _FREEHIRE_SLUG.fullmatch(slug) or not title or not company:
                    http.mark_partial(
                        f"freehire term {term_index} page {page + 1} contained an unusable job")
                    continue
                if item.get("closed_at"):
                    http.mark_partial(
                        f"freehire search returned closed job {slug}; omitted it")
                    continue
                # Intentional policy filtering is not a provider failure. Only
                # first-party ATS source+host pairs reach detail hydration.
                if direct is None or not _freehire_title_match(title, term):
                    continue
                existing = candidates.get(slug)
                if existing is None:
                    kept = dict(item)
                    kept["_matched_terms"] = [term]
                    candidates[slug] = kept
                elif term not in existing["_matched_terms"]:
                    existing["_matched_terms"].append(term)

            if meta_bad:
                terminated_partial = True
                break
            if not items:
                if provider_total is not None and fetched < provider_total:
                    http.mark_partial(
                        f"freehire term {term_index} ended after {fetched} of "
                        f"{provider_total} advertised results")
                    terminated_partial = True
                break
            if provider_total is not None and fetched >= provider_total:
                break
            if len(items) < page_size:
                if provider_total is not None and fetched < provider_total:
                    http.mark_partial(
                        f"freehire term {term_index} ended after {fetched} of "
                        f"{provider_total} advertised results")
                    terminated_partial = True
                break
            if page + 1 == pages:
                filled_budget = True
        if not terminated_partial and provider_total is not None and provider_total > fetched:
            http.mark_capped(
                f"freehire term {term_index} fetched {fetched} of {provider_total} "
                f"previews within its {pages}-page budget")
        elif not terminated_partial and provider_total is None and filled_budget:
            http.mark_capped(
                f"freehire term {term_index} filled its {pages * page_size}-preview "
                "budget without a provider total")

    selected = list(candidates.items())
    if len(selected) > detail_cap:
        http.mark_capped(
            f"freehire title prefilter matched {len(selected)} unique jobs; "
            f"hydrated the first {detail_cap}")
        selected = selected[:detail_cap]

    out, degraded = [], 0
    for slug, preview in selected:
        response = fetch_json(
            f"{_FREEHIRE_BASE}/api/v1/jobs/{quote(slug, safe='')}",
            safe_external=True,
        )
        detail = response.get("data") if isinstance(response, dict) else None
        if not isinstance(detail, dict):
            http.mark_partial(
                f"freehire detail {slug} returned HTTP {http.last_status() or 0} "
                "or unusable data; retained its preview")
            merged = dict(preview)
        elif str(detail.get("public_slug") or "").strip() != slug:
            http.mark_partial(
                f"freehire detail {slug} omitted or returned a different slug")
            merged = dict(preview)
        elif not _freehire_detail_continuous(preview, detail):
            http.mark_partial(
                f"freehire detail {slug} changed preview identity; retained its preview")
            merged = dict(preview)
        else:
            proposed = _freehire_merge(preview, detail)
            if _freehire_direct(proposed) is None:
                # A posting can change between the open-search index and the
                # detail row. Never let that switch an allowlisted preview to a
                # LinkedIn/aggregator redirect after the prefilter.
                http.mark_partial(
                    f"freehire detail {slug} changed to a non-allowlisted source or URL")
                merged = dict(preview)
            else:
                merged = proposed

        direct = _freehire_direct(merged) or _freehire_direct(preview)
        if direct is None:
            http.mark_partial(f"freehire detail {slug} lost its allowed direct ATS URL")
            continue
        source_key, direct_url = direct
        closed_at = str(merged.get("closed_at") or "").strip()
        if closed_at:
            # closed_at is Freehire's crawler lifecycle observation, not an
            # employer application deadline. Omit the stale row and retain the
            # provider observation in source health instead of fabricating a
            # deadline sentence that scoring would trust as employer prose.
            http.mark_partial(
                f"freehire detail {slug} was closed after search preview; omitted it")
            continue
        description = strip_html(str(merged.get("description") or ""))
        quality_bad = len(description) < 800 or not _jd.has_requirements(description)
        canonical_source = ""
        if quality_bad and canonical_enrichment:
            canonical, canonical_source = _jd.fetch_canonical(direct_url)
            if len(canonical) > len(description):
                description = canonical
            quality_bad = len(description) < 800 or not _jd.has_requirements(description)
        if quality_bad:
            degraded += 1

        enrichment = merged.get("enrichment")
        enrichment = enrichment if isinstance(enrichment, dict) else {}
        lo, hi, comp_text, comp_source = _freehire_salary(enrichment)
        posted_at = str(merged.get("posted_at") or "")[:10]
        raw = {
            "provider": "freehire",
            "upstream_source": str(merged.get("source") or preview.get("source") or ""),
            "normalized_upstream_source": source_key,
            "public_slug": slug,
            "upstream_external_id": str(merged.get("external_id") or ""),
            "direct_url": direct_url,
            "posted_at": merged.get("posted_at"),
            "created_at": merged.get("created_at"),
            "closed_at": merged.get("closed_at"),
            "work_mode": merged.get("work_mode"),
            "regions": merged.get("regions") if isinstance(merged.get("regions"), list) else [],
            "countries": merged.get("countries") if isinstance(merged.get("countries"), list) else [],
            "cities": merged.get("cities") if isinstance(merged.get("cities"), list) else [],
            "skills": merged.get("skills") if isinstance(merged.get("skills"), list) else [],
            "enrichment": enrichment,
            "matched_terms": list(preview.get("_matched_terms") or []),
            "canonical_jd_source": canonical_source,
        }
        out.append(_j(
            company=str(merged.get("company") or preview.get("company") or "").strip(),
            title=str(merged.get("title") or preview.get("title") or "").strip(),
            url=direct_url,
            url_direct=direct_url,
            location=str(merged.get("location") or preview.get("location") or "").strip(),
            description=description,
            posted_at=posted_at,
            # Freehire derives work_mode/category; neither may become an
            # employer-authoritative scoring input. Their exact provider values
            # remain in raw provenance above.
            department="",
            remote_flag=None,
            comp_min=lo,
            comp_max=hi,
            comp_text=comp_text,
            comp_source=comp_source,
            external_id=slug,
            source=f"freehire:{source_key}",
            raw=raw,
        ))
    if degraded:
        http.mark_partial(
            f"freehire retained {degraded} selected job(s) with short or "
            "requirement-free detail")
    return out


# --------------------------------------------------------------------------
# Key-gated feeds. Registration is free but must be done by the account owner;
# drop the key into keys.yaml and these light up with no code change.
# --------------------------------------------------------------------------

@feed("adzuna")
def adzuna(cfg: dict) -> list[Job]:
    app_id, app_key = cfg.get("app_id"), cfg.get("app_key")
    if not (app_id and app_key):
        return []
    try:
        pages = max(1, min(int(cfg.get("pages", 1)), 20))
        per_page = max(1, min(int(cfg.get("results_per_page", 50)), 100))
        max_days_old = int(cfg.get("max_days_old", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("adzuna pages/results_per_page/max_days_old must be integers")
    sort_by = str(cfg.get("sort_by") or "relevance").lower()
    if sort_by not in {"default", "hybrid", "date", "salary", "relevance"}:
        raise ValueError("adzuna sort_by must be default, hybrid, date, salary, or relevance")

    out, seen = [], set()
    for term_index, what in enumerate(TERMS, 1):
        fetched_for_term, provider_total = 0, None
        filled_budget = False
        term_seen: set[str] = set()
        prior_pages: set[tuple[str, ...]] = set()
        for page in range(1, pages + 1):
            age = f"&max_days_old={max_days_old}" if max_days_old > 0 else ""
            d = fetch_json(
                f"https://api.adzuna.com/v1/api/jobs/us/search/{page}"
                f"?app_id={quote_plus(str(app_id))}&app_key={quote_plus(str(app_key))}"
                f"&results_per_page={per_page}&what={quote_plus(str(what))}"
                f"&sort_by={sort_by}{age}&content-type=application/json"
            )
            if not isinstance(d, dict):
                http.mark_partial(f"adzuna term {term_index} page {page} failed")
                break
            items = d.get("results", []) or []
            if not isinstance(items, list):
                http.mark_partial(
                    f"adzuna term {term_index} page {page} had invalid jobs")
                break
            page_total = _nonnegative_count(d.get("count"))
            if page_total is None:
                http.mark_partial(f"adzuna term {term_index} had an invalid count")
            elif page_total > 0:
                provider_total = max(provider_total or 0, page_total)
            elif provider_total is None:
                provider_total = 0
                if items:
                    http.mark_partial(
                        f"adzuna term {term_index} returned rows beyond its "
                        "advertised total 0")
            page_seen: set[str] = set()
            page_order: list[str] = []
            page_unique = 0
            for item_index, j in enumerate(items):
                if not isinstance(j, dict):
                    http.mark_partial(
                        f"adzuna term {term_index} page {page} contained an invalid job")
                    continue
                external_id = str(j.get("id", ""))
                identity = external_id or str(j.get("redirect_url", ""))
                if not identity:
                    http.mark_partial(
                        f"adzuna term {term_index} page {page} job {item_index + 1} "
                        "had no stable identity")
                    identity = json.dumps(j, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":"))
                if identity in page_seen:
                    http.mark_partial(
                        f"adzuna term {term_index} page {page} repeated a job "
                        "within the page")
                    continue
                page_seen.add(identity)
                page_order.append(identity)
                if identity in term_seen:
                    http.mark_partial(
                        f"adzuna term {term_index} page {page} repeated a job "
                        "from an earlier page")
                    continue
                term_seen.add(identity)
                page_unique += 1
                if identity in seen:
                    continue
                seen.add(identity)
                lo = int(j["salary_min"]) if j.get("salary_min") else None
                hi = int(j["salary_max"]) if j.get("salary_max") else None
                # Adzuna PREDICTS salary when the employer did not publish one,
                # and signals it through salary_is_predicted or an identical
                # min/max. Never present that estimate as an employer claim.
                predicted = str(j.get("salary_is_predicted", "")) == "1" or (
                    lo is not None and lo == hi)
                comp_note = ""
                if predicted and lo:
                    comp_note = f"Adzuna ESTIMATE ~${lo:,} (employer published no band)"
                    lo = hi = None
                out.append(_j(
                    company=(j.get("company") or {}).get("display_name", ""),
                    title=j.get("title", ""), url=j.get("redirect_url", ""),
                    location=(j.get("location") or {}).get("display_name", ""),
                    description=strip_html(j.get("description", "")),
                    comp_min=lo, comp_max=hi, comp_text=comp_note,
                    posted_at=(j.get("created") or "")[:10],
                    external_id=external_id, source="adzuna", raw=j,
                ))
            fingerprint = tuple(page_order)
            if fingerprint and fingerprint in prior_pages:
                http.mark_partial(
                    f"adzuna term {term_index} page {page} replayed a prior page")
                fetched_for_term = len(term_seen)
                break
            if fingerprint:
                prior_pages.add(fingerprint)
            fetched_for_term = len(term_seen)
            if (provider_total is not None and
                    fetched_for_term > provider_total):
                http.mark_partial(
                    f"adzuna term {term_index} returned {fetched_for_term} unique "
                    f"jobs beyond its advertised total {provider_total}")
            if (not items or len(items) < per_page or
                    (provider_total is not None and
                     fetched_for_term >= provider_total)):
                break
            if page > 1 and page_unique == 0:
                http.mark_partial(
                    f"adzuna term {term_index} page {page} added no unique jobs")
                break
            if page == pages:
                filled_budget = True
        if provider_total is not None and provider_total > fetched_for_term:
            http.mark_capped(
                f"adzuna term {term_index} fetched {fetched_for_term} of "
                f"{provider_total} results within its {pages}-page budget")
        elif provider_total is None and filled_budget:
            http.mark_capped(
                f"adzuna term {term_index} filled its {pages * per_page}-result "
                "budget without a provider total")
    return out


@feed("usajobs")
def usajobs(cfg: dict) -> list[Job]:
    key, email = cfg.get("api_key"), cfg.get("email")
    if not (key and email):
        return []
    try:
        # USAJobs documents 500 rows/page and at most 10,000 rows/query.  The
        # previous single 100-row request silently omitted every later page.
        pages = max(1, min(int(cfg.get("pages", 20)), 20))
        per_page = max(1, min(int(cfg.get("results_per_page", 500)), 500))
    except (TypeError, ValueError):
        raise ValueError("usajobs pages/results_per_page must be integers")

    out, seen = [], set()
    for term_index, kw in enumerate(TERMS, 1):
        fetched_for_term, provider_total, provider_pages = 0, None, 0
        filled_budget = False
        term_seen: set[str] = set()
        prior_pages: set[tuple[str, ...]] = set()
        for page in range(1, pages + 1):
            st, tx = fetch(
                "https://data.usajobs.gov/api/search"
                f"?Keyword={quote_plus(str(kw))}&ResultsPerPage={per_page}&Page={page}",
                headers={"Host": "data.usajobs.gov", "User-Agent": email,
                         "Authorization-Key": key},
            )
            if st != 200:
                http.mark_partial(
                    f"usajobs term {term_index} page {page} returned HTTP {st}")
                break
            try:
                d = json.loads(tx)
            except Exception:
                http.mark_partial(
                    f"usajobs term {term_index} page {page} returned unusable JSON")
                break
            if not isinstance(d, dict) or not isinstance(d.get("SearchResult"), dict):
                http.mark_partial(
                    f"usajobs term {term_index} page {page} had an unexpected shape")
                break
            result = d["SearchResult"]
            items = result.get("SearchResultItems", []) or []
            if not isinstance(items, list):
                http.mark_partial(
                    f"usajobs term {term_index} page {page} had invalid jobs")
                break
            page_total = _nonnegative_count(result.get("SearchResultCountAll"))
            if page_total is None:
                http.mark_partial(f"usajobs term {term_index} had an invalid total")
            elif page_total > 0:
                provider_total = max(provider_total or 0, page_total)
            elif provider_total is None:
                provider_total = 0
                if items:
                    http.mark_partial(
                        f"usajobs term {term_index} returned rows beyond its "
                        "advertised total 0")
            page_seen: set[str] = set()
            page_order: list[str] = []
            page_unique = 0
            for item_index, item in enumerate(items):
                if not isinstance(item, dict):
                    http.mark_partial(
                        f"usajobs term {term_index} page {page} contained an invalid job")
                    continue
                j = item.get("MatchedObjectDescriptor", {})
                if not isinstance(j, dict):
                    http.mark_partial(
                        f"usajobs term {term_index} contained an invalid job")
                    continue
                external_id = str(j.get("PositionID", ""))
                identity = external_id or str(j.get("PositionURI", ""))
                if not identity:
                    http.mark_partial(
                        f"usajobs term {term_index} page {page} job {item_index + 1} "
                        "had no stable identity")
                    identity = json.dumps(j, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":"))
                if identity in page_seen:
                    http.mark_partial(
                        f"usajobs term {term_index} page {page} repeated a job "
                        "within the page")
                    continue
                page_seen.add(identity)
                page_order.append(identity)
                if identity in term_seen:
                    http.mark_partial(
                        f"usajobs term {term_index} page {page} repeated a job "
                        "from an earlier page")
                    continue
                term_seen.add(identity)
                page_unique += 1
                if identity in seen:
                    continue
                seen.add(identity)
                pay = (j.get("PositionRemuneration") or [{}])[0]
                pay = pay if isinstance(pay, dict) else {}
                rate = str(pay.get("RateIntervalCode") or pay.get("Description") or "")
                annual = bool(re.search(r"\b(?:year|annual|pa)\b", rate, re.I))
                try:
                    raw_min = int(float(pay.get("MinimumRange", 0) or 0)) or None
                    raw_max = int(float(pay.get("MaximumRange", 0) or 0)) or None
                except (TypeError, ValueError, OverflowError):
                    raw_min = raw_max = None
                    http.mark_partial(
                        f"usajobs term {term_index} job compensation was invalid")
                comp_text = " - ".join(
                    f"${value:,}" for value in (raw_min, raw_max) if value)
                if rate and comp_text:
                    comp_text += f" / {rate}"
                details = j.get("UserArea", {}).get("Details", {}) or {}
                details = details if isinstance(details, dict) else {}
                out.append(_j(
                    company=j.get("OrganizationName", ""),
                    title=j.get("PositionTitle", ""),
                    url=j.get("PositionURI", ""),
                    location=", ".join(
                        location.get("LocationName", "")
                        for location in (j.get("PositionLocation") or [])[:3]
                        if isinstance(location, dict)
                    ),
                    description=_description_sections(details),
                    comp_min=raw_min if annual else None,
                    comp_max=raw_max if annual else None,
                    comp_text=comp_text,
                    comp_source="board" if comp_text else "",
                    posted_at=(j.get("PublicationStartDate") or "")[:10],
                    external_id=external_id, source="usajobs", raw=j,
                ))

            fingerprint = tuple(page_order)
            if fingerprint and fingerprint in prior_pages:
                http.mark_partial(
                    f"usajobs term {term_index} page {page} replayed a prior page")
                fetched_for_term = len(term_seen)
                break
            if fingerprint:
                prior_pages.add(fingerprint)
            fetched_for_term = len(term_seen)
            if (provider_total is not None and
                    fetched_for_term > provider_total):
                http.mark_partial(
                    f"usajobs term {term_index} returned {fetched_for_term} unique "
                    f"jobs beyond its advertised total {provider_total}")

            user_area = result.get("UserArea") or {}
            raw_page_count = (user_area.get("NumberOfPages")
                              if isinstance(user_area, dict) else None)
            if raw_page_count in (None, ""):
                page_count = 0
            else:
                parsed_page_count = _nonnegative_count(raw_page_count)
                if parsed_page_count is None:
                    http.mark_partial(
                        f"usajobs term {term_index} had an invalid page count")
                    page_count = 0
                else:
                    page_count = parsed_page_count
            provider_pages = max(provider_pages, page_count)
            if (not items or
                    (provider_total is not None and
                     fetched_for_term >= provider_total) or
                    (provider_pages and page >= provider_pages) or
                    len(items) < per_page):
                break
            if page > 1 and page_unique == 0:
                http.mark_partial(
                    f"usajobs term {term_index} page {page} added no unique jobs")
                break
            if page == pages:
                filled_budget = True
        if provider_total is not None and provider_total > fetched_for_term:
            http.mark_capped(
                f"usajobs term {term_index} fetched {fetched_for_term} of "
                f"{provider_total} results within its {pages}-page budget")
        elif provider_total is None and filled_budget:
            http.mark_capped(
                f"usajobs term {term_index} filled its {pages * per_page}-result "
                "budget without a provider total")
    return out


@feed("findwork")
def findwork(cfg: dict) -> list[Job]:
    key = cfg.get("api_key")
    if not key:
        return []
    out, emitted = [], set()
    for term_index, q in enumerate(TERMS, 1):
        # quote_plus, like every other feed here. Interpolated raw, a term
        # containing "&" ("R&D Manager") ended the query string early and the
        # board answered a different search than the one asked for, while a
        # space produced a malformed URL.
        st, tx = fetch(f"https://findwork.dev/api/jobs/?search={quote_plus(q)}&location=usa",
                       headers={"Authorization": f"Token {key}"})
        if st != 200:
            http.mark_partial(f"findwork term {term_index} returned HTTP {st}")
            continue
        try:
            d = json.loads(tx)
        except Exception:
            http.mark_partial(f"findwork term {term_index} returned unusable JSON")
            continue
        items = _required_feed_objects(d, "results", f"findwork term {term_index}")
        if items is None:
            continue
        if d.get("next"):
            http.mark_capped(f"findwork term {term_index} stopped while a next page was advertised")
        for item_index, j in enumerate(items, 1):
            identity = str(j.get("id") or j.get("url") or "").strip()
            if not _usable_feed_row(
                    identity=identity, title=j.get("role"),
                    company=j.get("company_name"),
                    label=f"findwork term {term_index} item {item_index}"):
                continue
            if identity and identity in emitted:
                continue
            if identity:
                emitted.add(identity)
            out.append(_j(
                company=j.get("company_name", ""), title=j.get("role", ""),
                url=j.get("url", ""), location=j.get("location", ""),
                description=strip_html(j.get("text", "")),
                posted_at=(j.get("date_posted") or "")[:10],
                remote_flag=bool(j.get("remote")), external_id=str(j.get("id", "")),
                source="findwork", raw=j,
            ))
    return out


@feed("careerjet")
def careerjet(cfg: dict) -> list[Job]:
    affid = cfg.get("affid")
    if not affid:
        return []
    out, emitted = [], set()
    for term_index, kw in enumerate(TERMS, 1):
        d = fetch_json(
            f"https://public.api.careerjet.net/search?locale_code=en_US&affid={affid}"
            f"&keywords={quote_plus(kw)}&location=USA&pagesize=99&sort=date"
        )
        items = _required_feed_objects(d, "jobs", f"careerjet term {term_index}")
        if items is None:
            continue
        pages = _optional_provider_count(
            d, "pages", f"careerjet term {term_index}")
        if len(items) >= 99 or pages > 1:
            http.mark_capped(f"careerjet term {term_index} stopped at its first 99-result page")
        for item_index, j in enumerate(items, 1):
            identity = str(j.get("url") or j.get("id") or "").strip()
            if not _usable_feed_row(
                    identity=identity, title=j.get("title"),
                    company=j.get("company"),
                    label=f"careerjet term {term_index} item {item_index}"):
                continue
            if identity and identity in emitted:
                continue
            if identity:
                emitted.add(identity)
            out.append(_j(
                company=j.get("company", ""), title=j.get("title", ""),
                url=j.get("url", ""), location=j.get("locations", ""),
                description=strip_html(j.get("description", "")),
                posted_at=(j.get("date") or "")[:10],
                comp_text=j.get("salary", "") or "", external_id=j.get("url", ""),
                source="careerjet", raw=j,
            ))
    return out


# Feeds that cannot run without credentials, and the keys each one needs in
# profile/keys.yaml. Used to tell "you never configured this" apart from "this
# is broken", which the single conflated message could not.
FEED_KEYS = {
    "adzuna": ("app_id", "app_key"),
    "usajobs": ("api_key", "email"),
    "findwork": ("api_key",),
    "careerjet": ("affid",),
}

# These feeds execute one request sequence per configured search term.  With no
# terms, they make no request at all; that is dormant configuration, never a
# healthy zero-result observation that can authorize retirement of old rows.
TERM_FEEDS = frozenset({
    "remotive", "jobicy", "freehire", "adzuna", "usajobs", "findwork",
    "careerjet", "linkedin_guest", "jobspy",
})


def run_feed(name: str, cfg: dict) -> tuple[list[Job], str | None]:
    """Returns (jobs, error_or_None).

    An empty result used to report "0 postings (or dormant: needs key)"
    whatever the cause, so a feed that ran fine and simply had nothing in range
    looked identical to one that was never configured and to one that was
    broken. A run on 2026-08-05 showed adzuna succeeding and still being
    counted as a failure. Same defect as run_adapter had; same fix."""
    http.reset_status()          # never inherit the previous source's state
    fn = FEEDS.get(name)
    if not fn:
        return [], f"no feed named {name!r}"
    if name == "freehire" and cfg.get("active") is not True:
        return [], ("dormant: Freehire is explicit opt-in; review its privacy "
                    "disclosure and set active: true in profile/employers.yaml")
    if name in TERM_FEEDS and not TERMS:
        # set_search_terms already explains the missing profile value. No query
        # was attempted, so this result cannot be closure evidence.
        return [], "dormant: profile has no search_terms; feed was not queried"
    required = FEED_KEYS.get(name)
    if required and not all(cfg.get(k) for k in required):
        missing = ", ".join(k for k in required if not cfg.get(k))
        return [], f"dormant: add {missing} to profile/keys.yaml"
    try:
        jobs = fn(cfg)
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"
    integrity_error = http.source_integrity_error()
    if integrity_error:
        return jobs, integrity_error
    if jobs:
        return jobs, None
    st = http.last_status()
    if st == 200:
        if http.last_parse_ok() is False:
            return [], "HTTP 200 but the response was not usable JSON"
        return [], None                       # ran fine, nothing in range
    if st is None:
        return [], "no response (network/DNS/timeout)"
    return [], f"HTTP {st}"


@feed("linkedin_guest")
def linkedin_guest(cfg: dict) -> list[Job]:
    """LinkedIn public guest job search (unauthenticated HTML cards).

    Uses linkedin.com/jobs-guest endpoints - the same public pages a
    logged-out visitor sees. Deliberately polite: small page counts, a
    freshness window (default 7 days; set since_seconds), throttled requests,
    and detail fetches only for cards whose title matches a search term.
    LinkedIn throttles/blocks aggressive callers; if this feed starts
    returning 0 with prior success, back off - do not add retries.
    """
    import html as _html
    import time as _time
    from urllib.parse import quote as _q

    since = int(cfg.get("since_seconds", 604800))
    loc = cfg.get("location", "United States")
    pages = int(cfg.get("pages", 2))
    detail_cap = int(cfg.get("detail_cap", 40))

    # No card_re here on purpose. A combined link+title pattern was tried and
    # abandoned: LinkedIn's guest markup puts the two in separate elements often
    # enough that a single regex matched a fraction of the cards and silently
    # dropped the rest. The per-field patterns below are what actually parse.
    title_re = re.compile(r'base-search-card__title">\s*([^<]+)')
    comp_re = re.compile(r'base-search-card__subtitle[^>]*>\s*(?:<a[^>]*>)?\s*([^<]+)')
    loc_re = re.compile(r'job-search-card__location">\s*([^<]+)')
    time_re = re.compile(r'datetime="([^"]+)"')
    desc_re = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</div>', re.S)

    out, details = [], 0
    for term_index, term in enumerate(TERMS, 1):
        for p in range(pages):
            url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/"
                   f"search?keywords={_q(term)}&location={_q(loc)}"
                   f"&f_TPR=r{since}&start={p * 25}")
            status, text = fetch(url)
            if status != 200 or not text:
                http.mark_partial(
                    f"linkedin_guest term {term_index} page {p + 1} "
                    f"returned HTTP {status or 0}")
                break
            cards = text.split('<div class="base-card')
            got = 0
            for c in cards[1:]:
                href = re.search(r'href="(https://www\.linkedin\.com/jobs/view/[^"]+)"', c)
                t = title_re.search(c)
                if not href or not t:
                    continue
                got += 1
                jurl = href.group(1).split("?")[0]
                jid = re.search(r"(\d{6,})/?$", jurl)
                title = _html.unescape(t.group(1).strip())
                comp = comp_re.search(c)
                lc = loc_re.search(c)
                dt = time_re.search(c)
                desc = ""
                relevant = any(w.lower() in title.lower() for w in term.split())
                if jid and relevant:
                    if details >= detail_cap:
                        http.mark_capped(
                            f"linkedin_guest reached its {detail_cap}-detail safety cap")
                    else:
                        _time.sleep(1.0)
                        ds, dtext = fetch("https://www.linkedin.com/jobs-guest/jobs/api/"
                                          f"jobPosting/{jid.group(1)}")
                        if ds == 200 and dtext:
                            m = desc_re.search(dtext)
                            if m:
                                desc = strip_html(m.group(1))
                                details += 1
                            else:
                                http.mark_partial(
                                    f"linkedin_guest detail {jid.group(1)} was unusable")
                        else:
                            http.mark_partial(
                                f"linkedin_guest detail {jid.group(1)} returned HTTP {ds}")
                out.append(_j(
                    company=_html.unescape(comp.group(1).strip()) if comp else "",
                    title=title, url=jurl,
                    location=_html.unescape(lc.group(1).strip()) if lc else "",
                    description=desc,
                    posted_at=(dt.group(1) if dt else "")[:10],
                    external_id=jid.group(1) if jid else jurl,
                    source="linkedin_guest", raw={},
                ))
            _time.sleep(1.2)
            if got < 5:
                break
            if p + 1 == pages and got >= 25:
                http.mark_capped(
                    f"linkedin_guest term {term_index} reached its {pages * 25}-card cap")
    return out


@feed("jobspy")
def jobspy_feed(cfg: dict) -> list[Job]:
    """Optional multi-portal feed via the python-jobspy library (Indeed,
    ZipRecruiter, Glassdoor, Google).

    There is currently no supported installation path: python-jobspy 1.1.82
    requires markdownify<0.14.0 while CVE-2025-46656 is fixed in 0.14.1. The
    adapter remains for a future compatible upstream release, and the runtime
    guard below refuses today's vulnerable combination before importing it."""
    _require_safe_jobspy_runtime()
    try:
        from jobspy import scrape_jobs
    except ImportError as exc:
        raise RuntimeError("python-jobspy not installed (optional dependency)") from exc
    sites = cfg.get("sites") or ["indeed", "zip_recruiter"]
    hours = int(cfg.get("hours_old", 72))
    want = int(cfg.get("results_per_term", 25))
    loc = cfg.get("location", "United States")
    out = []
    for term_index, term in enumerate(TERMS, 1):
        try:
            df = scrape_jobs(site_name=sites, search_term=term, location=loc,
                             results_wanted=want, hours_old=hours,
                             country_indeed=cfg.get("country_indeed", "USA"))
        except Exception as exc:
            http.mark_partial(
                f"jobspy term {term_index} failed: {type(exc).__name__}")
            continue
        if len(df) >= want:
            http.mark_capped(
                f"jobspy term {term_index} reached its {want}-result request cap")
        for _, r in df.iterrows():
            def g(k):
                v = r.get(k)
                return "" if v is None or (isinstance(v, float) and v != v) else v
            comp_lo, comp_hi = r.get("min_amount"), r.get("max_amount")
            def num(v):
                try:
                    return int(v) if v == v and v is not None else None
                except Exception:
                    return None
            out.append(_j(
                company=str(g("company")), title=str(g("title")),
                url=str(g("job_url")), location=str(g("location")),
                description=str(g("description"))[:6000],
                posted_at=str(g("date_posted"))[:10],
                comp_min=num(comp_lo), comp_max=num(comp_hi),
                remote_flag=bool(r.get("is_remote") is True),
                external_id=str(g("id")) or str(g("job_url")),
                # str(None) is the string "None", which would look like evidence.
                url_direct=str(g("job_url_direct") or ""),
                company_site=str(g("company_url_direct") or ""),
                source=f"jobspy:{g('site')}", raw={},
            ))
    return out


_VERSION_CORE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$")


def _safe_markdownify_version(value: str) -> bool:
    """Whether a markdownify release includes the heading-memory fix.

    markdownify before 0.14.1 can allocate attacker-chosen amounts of memory
    while converting a huge HTML heading tag. JobSpy processes third-party job
    HTML through this library, so importing an installed-but-vulnerable stack as
    if it were usable turns untrusted board markup into a local DoS primitive.
    """
    match = _VERSION_CORE.fullmatch((value or "").strip().lower())
    if not match:
        return False
    core = tuple(int(part or 0) for part in match.groups()[:3])
    suffix = match.group(4).lstrip(".-+")
    prerelease = bool(re.match(r"(?:a|alpha|b|beta|rc|pre|preview|dev)", suffix))
    return core >= (0, 14, 1) and not prerelease


def _require_safe_jobspy_runtime() -> None:
    """Fail closed before JobSpy imports a vulnerable markdownify runtime."""
    try:
        metadata.version("python-jobspy")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError("python-jobspy not installed (optional dependency)") from exc
    try:
        markdownify_version = metadata.version("markdownify")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            "jobspy disabled: markdownify is missing from its runtime"
        ) from exc
    if not _safe_markdownify_version(markdownify_version):
        raise RuntimeError(
            "jobspy disabled: installed markdownify "
            f"{markdownify_version} is affected by CVE-2025-46656; require "
            "markdownify>=0.14.1 with a compatible python-jobspy release"
        )
    try:
        parsed = Version(markdownify_version)
        requirements = metadata.distribution("python-jobspy").requires or []
    except (InvalidVersion, metadata.PackageNotFoundError) as exc:
        raise RuntimeError(
            "jobspy disabled: its installed dependency metadata cannot prove "
            "compatibility with security-fixed markdownify"
        ) from exc
    compatible = False
    for raw in requirements:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            continue
        if canonicalize_name(requirement.name) != "markdownify":
            continue
        if requirement.marker and not requirement.marker.evaluate():
            continue
        if parsed in requirement.specifier:
            compatible = True
            break
    if not compatible:
        raise RuntimeError(
            "jobspy disabled: installed python-jobspy does not declare "
            f"compatibility with secure markdownify {markdownify_version}"
        )


# --------------------------------------------------------------------------
# Source policy (CK-020)
# --------------------------------------------------------------------------
# What each feed actually is, so a user can decide before enabling it rather
# than discovering it from a rate-limit or a blocked request. Three kinds:
#
#   official   a documented public API, no key needed
#   keyed      a documented API that requires the user's own registration
#   scraping   reads public HTML search pages; no API contract, can be
#              rate-limited or blocked, and the terms of use are the operator's
#              to read. These are OFF by default in the shipped registry.
#
# `identifies_user` flags the one case where a request carries something
# personal: USAJobs requires the registered email in a request header.
SOURCE_POLICY = {
    "remotive":       {"kind": "official", "note": "public JSON API"},
    "remoteok":       {"kind": "official", "note": "public JSON API"},
    "arbeitnow":      {"kind": "official", "note": "public job-board API"},
    "himalayas":      {"kind": "official", "note": "public JSON API"},
    "weworkremotely": {"kind": "official", "note": "public RSS"},
    "jobicy":         {"kind": "official", "note": "public JSON API"},
    "themuse":        {"kind": "official", "note": "public API, key optional"},
    "workingnomads":  {"kind": "official", "note": "public JSON API"},
    "freehire":       {"kind": "official", "opt_in": True,
                       "sends_search_terms": True,
                       "note": "third-party discovery API; sends quoted search "
                               "terms and ordinary request metadata to Freehire, "
                               "never credentials/resume/claims. Off by default."},
    "adzuna":         {"kind": "keyed", "note": "needs app id + key"},
    "usajobs":        {"kind": "keyed", "note": "needs API key AND sends your "
                                                "registered email in a header",
                       "identifies_user": True},
    "findwork":       {"kind": "keyed", "note": "needs API token"},
    "careerjet":      {"kind": "keyed", "note": "needs affiliate id"},
    "linkedin_guest": {"kind": "scraping",
                       "note": "reads the public guest search pages; no API "
                               "contract, can be blocked. Off by default."},
    "jobspy":         {"kind": "scraping", "supported": False,
                       "note": "third-party scraper for Indeed / ZipRecruiter / "
                               "Glassdoor / Google. Off by default."},
}


def policy(name: str) -> dict:
    return SOURCE_POLICY.get(name, {"kind": "unknown", "note": ""})


def scraping_feeds() -> list[str]:
    return sorted(n for n, p in SOURCE_POLICY.items() if p["kind"] == "scraping")
