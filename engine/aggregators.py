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
import re
from urllib.parse import quote_plus
from typing import Callable

from . import http
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


@feed("remotive")
def remotive(_cfg: dict) -> list[Job]:
    out = []
    for q in TERMS:
        d = fetch_json(f"https://remotive.com/api/remote-jobs?search={quote_plus(q)}&limit=100")
        for j in (d or {}).get("jobs", []) or []:
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
    out = []
    for j in (d or [])[1:] if isinstance(d, list) else []:
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
    out = []
    for page in (1, 2, 3):
        d = fetch_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        for j in (d or {}).get("data", []) or []:
            out.append(_j(
                company=j.get("company_name", ""), title=j.get("title", ""),
                url=j.get("url", ""), location=j.get("location", ""),
                description=strip_html(j.get("description", "")),
                remote_flag=bool(j.get("remote")), external_id=str(j.get("slug", "")),
                source="arbeitnow", raw=j,
            ))
    return out


@feed("himalayas")
def himalayas(_cfg: dict) -> list[Job]:
    out = []
    for page in (1, 2):
        d = fetch_json(f"https://himalayas.app/jobs/api?limit=100&offset={(page-1)*100}")
        for j in (d or {}).get("jobs", []) or []:
            out.append(_j(
                company=j.get("companyName", ""), title=j.get("title", ""),
                url=j.get("applicationLink", "") or j.get("guid", ""),
                location=", ".join(j.get("locationRestrictions", []) or []) or "Remote",
                description=strip_html(j.get("description", "")),
                comp_min=j.get("minSalary"), comp_max=j.get("maxSalary"),
                posted_at=str(j.get("pubDate", ""))[:10], remote_flag=True,
                external_id=str(j.get("guid", "")), source="himalayas", raw=j,
            ))
    return out


@feed("weworkremotely")
def weworkremotely(cfg: dict) -> list[Job]:
    out = []
    # Was four categories chosen for the original author. Configurable now, with
    # a broader default so a user in an unrelated field is not silently narrowed.
    for cat in (cfg.get("categories") or (
            "remote-customer-support-jobs", "remote-management-and-finance-jobs",
            "remote-product-jobs", "remote-programming-jobs", "remote-marketing-jobs",
            "remote-sales-jobs", "remote-design-jobs")):
        st, tx = fetch(f"https://weworkremotely.com/categories/{cat}.rss")
        if st != 200:
            continue
        for item in re.findall(r"<item>(.*?)</item>", tx, re.S):
            def g(tag):
                m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", item, re.S)
                return m.group(1).strip() if m else ""
            title = g("title")
            company, _, role = title.partition(":")
            out.append(_j(
                company=company.strip(), title=(role or title).strip(),
                url=g("link"), location=g("region") or "Remote",
                description=strip_html(g("description")), posted_at=g("pubDate")[:16],
                remote_flag=True, external_id=g("link"), source="weworkremotely", raw={},
            ))
    return out


@feed("jobicy")
def jobicy(_cfg: dict) -> list[Job]:
    out = []
    for q in TERMS:
        d = fetch_json(f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={quote_plus(q)}")
        for j in (d or {}).get("jobs", []) or []:
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
    for cat in cats:
        for loc in locs:
            for page in (0, 1):
                url = ("https://www.themuse.com/api/public/jobs?"
                       f"category={cat.replace(' ', '%20').replace('&', '%26')}"
                       f"&location={loc.replace(' ', '%20').replace('/', '%2F')}"
                       f"&page={page}&descending=true")
                d = fetch_json(url)
                for j in (d or {}).get("results", []) or []:
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
    return out


@feed("workingnomads")
def workingnomads(_cfg: dict) -> list[Job]:
    d = fetch_json("https://www.workingnomads.com/api/exposed_jobs/")
    out = []
    for j in d or []:
        out.append(_j(
            company=j.get("company_name", ""), title=j.get("title", ""),
            url=j.get("url", ""), location=j.get("location", "") or "Remote",
            description=strip_html(j.get("description", "")),
            posted_at=(j.get("pub_date") or "")[:10], remote_flag=True,
            external_id=str(j.get("id", "")), source="workingnomads", raw=j,
        ))
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
    out = []
    for what in TERMS:
        d = fetch_json(
            f"https://api.adzuna.com/v1/api/jobs/us/search/1?app_id={app_id}&app_key={app_key}"
            f"&results_per_page=50&what={what.replace(' ', '%20')}&content-type=application/json"
        )
        for j in (d or {}).get("results", []) or []:
            lo = int(j["salary_min"]) if j.get("salary_min") else None
            hi = int(j["salary_max"]) if j.get("salary_max") else None
            # Adzuna PREDICTS salary when the employer did not publish one, and
            # signals it two ways: salary_is_predicted, or an identical min/max.
            # Measured: 150 of 195 results came back single-point. Treating those
            # as a published band would manufacture false confidence, so they are
            # demoted to a note and the role scores as comp-unknown.
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
                external_id=str(j.get("id", "")), source="adzuna", raw=j,
            ))
    return out


@feed("usajobs")
def usajobs(cfg: dict) -> list[Job]:
    key, email = cfg.get("api_key"), cfg.get("email")
    if not (key and email):
        return []
    out = []
    for kw in TERMS:
        st, tx = fetch(
            f"https://data.usajobs.gov/api/search?Keyword={quote_plus(kw)}&ResultsPerPage=100",
            headers={"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key},
        )
        if st != 200:
            continue
        try:
            d = json.loads(tx)
        except Exception:
            continue
        for item in d.get("SearchResult", {}).get("SearchResultItems", []) or []:
            j = item.get("MatchedObjectDescriptor", {})
            pay = (j.get("PositionRemuneration") or [{}])[0]
            out.append(_j(
                company=j.get("OrganizationName", ""), title=j.get("PositionTitle", ""),
                url=j.get("PositionURI", ""),
                location=", ".join(l.get("LocationName", "") for l in (j.get("PositionLocation") or [])[:3]),
                description=strip_html((j.get("UserArea", {}).get("Details", {}) or {}).get("JobSummary", "")),
                comp_min=int(float(pay.get("MinimumRange", 0) or 0)) or None,
                comp_max=int(float(pay.get("MaximumRange", 0) or 0)) or None,
                posted_at=(j.get("PublicationStartDate") or "")[:10],
                external_id=str(j.get("PositionID", "")), source="usajobs", raw=j,
            ))
    return out


@feed("findwork")
def findwork(cfg: dict) -> list[Job]:
    key = cfg.get("api_key")
    if not key:
        return []
    out = []
    for q in TERMS:
        # quote_plus, like every other feed here. Interpolated raw, a term
        # containing "&" ("R&D Manager") ended the query string early and the
        # board answered a different search than the one asked for, while a
        # space produced a malformed URL.
        st, tx = fetch(f"https://findwork.dev/api/jobs/?search={quote_plus(q)}&location=usa",
                       headers={"Authorization": f"Token {key}"})
        if st != 200:
            continue
        try:
            d = json.loads(tx)
        except Exception:
            continue
        for j in d.get("results", []) or []:
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
    out = []
    for kw in TERMS:
        d = fetch_json(
            f"https://public.api.careerjet.net/search?locale_code=en_US&affid={affid}"
            f"&keywords={quote_plus(kw)}&location=USA&pagesize=99&sort=date"
        )
        for j in (d or {}).get("jobs", []) or []:
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


def run_feed(name: str, cfg: dict) -> tuple[list[Job], str | None]:
    """Returns (jobs, error_or_None).

    An empty result used to report "0 postings (or dormant: needs key)"
    whatever the cause, so a feed that ran fine and simply had nothing in range
    looked identical to one that was never configured and to one that was
    broken. A run on 2026-08-05 showed adzuna succeeding and still being
    counted as a failure. Same defect as run_adapter had; same fix."""
    fn = FEEDS.get(name)
    if not fn:
        return [], f"no feed named {name!r}"
    required = FEED_KEYS.get(name)
    if required and not all(cfg.get(k) for k in required):
        missing = ", ".join(k for k in required if not cfg.get(k))
        return [], f"dormant: add {missing} to profile/keys.yaml"
    http.reset_status()          # never inherit the previous feed's status
    try:
        jobs = fn(cfg)
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"
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
    ua = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}

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
    for term in TERMS:
        for p in range(pages):
            url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/"
                   f"search?keywords={_q(term)}&location={_q(loc)}"
                   f"&f_TPR=r{since}&start={p * 25}")
            status, text = fetch(url, headers=ua)
            if status != 200 or not text:
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
                if jid and details < detail_cap and any(
                        w.lower() in title.lower() for w in term.split()):
                    _time.sleep(1.0)
                    ds, dtext = fetch("https://www.linkedin.com/jobs-guest/jobs/api/"
                                      f"jobPosting/{jid.group(1)}", headers=ua)
                    if ds == 200 and dtext:
                        m = desc_re.search(dtext)
                        if m:
                            desc = strip_html(m.group(1))
                            details += 1
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
    return out


@feed("jobspy")
def jobspy_feed(cfg: dict) -> list[Job]:
    """Optional multi-portal feed via the python-jobspy library (Indeed,
    ZipRecruiter, Glassdoor, Google). Install: pip install python-jobspy.
    Indeed is the workhorse (no rate limiting per their docs); we skip
    LinkedIn here because the polite linkedin_guest feed covers it."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        raise RuntimeError("python-jobspy not installed (optional dependency)")
    sites = cfg.get("sites") or ["indeed", "zip_recruiter"]
    hours = int(cfg.get("hours_old", 72))
    want = int(cfg.get("results_per_term", 25))
    loc = cfg.get("location", "United States")
    out = []
    for term in TERMS:
        try:
            df = scrape_jobs(site_name=sites, search_term=term, location=loc,
                             results_wanted=want, hours_old=hours,
                             country_indeed=cfg.get("country_indeed", "USA"))
        except Exception:
            continue
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
                source=f"jobspy:{g('site')}", raw={},
            ))
    return out


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
    "adzuna":         {"kind": "keyed", "note": "needs app id + key"},
    "usajobs":        {"kind": "keyed", "note": "needs API key AND sends your "
                                                "registered email in a header",
                       "identifies_user": True},
    "findwork":       {"kind": "keyed", "note": "needs API token"},
    "careerjet":      {"kind": "keyed", "note": "needs affiliate id"},
    "linkedin_guest": {"kind": "scraping",
                       "note": "reads the public guest search pages; no API "
                               "contract, can be blocked. Off by default."},
    "jobspy":         {"kind": "scraping",
                       "note": "third-party scraper for Indeed / ZipRecruiter / "
                               "Glassdoor / Google. Off by default."},
}


def policy(name: str) -> dict:
    return SOURCE_POLICY.get(name, {"kind": "unknown", "note": ""})


def scraping_feeds() -> list[str]:
    return sorted(n for n, p in SOURCE_POLICY.items() if p["kind"] == "scraping")
