"""Per-employer ATS adapters.

Each adapter takes one registry entry and returns normalized Jobs. These are the
high-precision lane: you get every open posting a company has, with full text,
straight from the system of record. The tradeoff is that you must already know
the company and its board slug, which is what discover.py is for.

Every endpoint here is the board's own public, unauthenticated feed - the same
data the careers page renders for any visitor.
"""
from __future__ import annotations

import json
import re
from typing import Callable

from . import http
from .http import fetch, fetch_json
from .models import Job, strip_html

Adapter = Callable[[dict], list[Job]]
REGISTRY: dict[str, Adapter] = {}


def adapter(name: str):
    def deco(fn: Adapter) -> Adapter:
        REGISTRY[name] = fn
        fn.ats_name = name  # type: ignore[attr-defined]
        return fn
    return deco


def _base(cfg: dict) -> dict:
    return {
        "company": cfg.get("name") or cfg.get("slug", ""),
        "lane": cfg.get("lane", ""),
        "employer_tier": cfg.get("tier", ""),
        "rails_exempt": bool(cfg.get("rails_exempt", False)),
    }


# --------------------------------------------------------------------------
# Clean public JSON boards
# --------------------------------------------------------------------------

@adapter("greenhouse")
def greenhouse(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs", []) or []:
        offices = ", ".join(o.get("name", "") for o in (j.get("offices") or []))
        meta = {m.get("name"): m.get("value") for m in (j.get("metadata") or [])}
        out.append(Job(
            title=j.get("title", ""),
            url=j.get("absolute_url", ""),
            location=(j.get("location") or {}).get("name", "") or offices,
            description=strip_html(j.get("content", "")),
            posted_at=(j.get("updated_at") or j.get("first_published") or "")[:10],
            department=", ".join(d.get("name", "") for d in (j.get("departments") or [])),
            external_id=str(j.get("id", "")),
            comp_text=str(meta.get("Salary Range") or meta.get("Compensation") or ""),
            source="greenhouse", raw=j, **_base(cfg),
        ))
    return out


@adapter("lever")
def lever(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        cats = j.get("categories") or {}
        body = strip_html(j.get("description", "")) + "\n" + strip_html(
            "\n".join(s.get("text", "") for s in (j.get("lists") or []))
        )
        for s in j.get("lists") or []:
            body += "\n" + strip_html(s.get("content", ""))
        # Lever postings carry a top-level workplaceType (remote/hybrid/onsite).
        # Not mapping it is how six remote CrossCountry roles with location
        # "United States" were excluded as onsite (2026-08-03).
        wp = j.get("workplaceType") or ""
        out.append(Job(
            title=j.get("text", ""),
            url=j.get("hostedUrl", "") or j.get("applyUrl", ""),
            location=cats.get("location", "") or "",
            description=body.strip(),
            posted_at="",
            department=" / ".join(x for x in [cats.get("team"), cats.get("department")] if x),
            comp_text=str(cats.get("commitment") or ""),
            external_id=str(j.get("id", "")),
            remote_flag=(wp == "remote") if wp else None,
            source="lever", raw=j, **_base(cfg),
        ))
    return out


@adapter("ashby")
def ashby(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    )
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs", []) or []:
        comp = j.get("compensation") or {}
        summary = comp.get("compensationTierSummary") or ""
        lo = hi = None
        for tier in comp.get("compensationTiers") or []:
            for comp_part in tier.get("components") or []:
                if comp_part.get("compensationType") == "Salary":
                    lo = comp_part.get("minValue") or lo
                    hi = comp_part.get("maxValue") or hi
        out.append(Job(
            title=j.get("title", ""),
            url=j.get("jobUrl", "") or j.get("applyUrl", ""),
            location=j.get("location", "") or ", ".join(
                a.get("location", "") for a in (j.get("secondaryLocations") or [])
            ),
            description=strip_html(j.get("descriptionHtml") or j.get("descriptionPlain") or ""),
            posted_at=(j.get("publishedAt") or "")[:10],
            department=j.get("department", "") or j.get("team", ""),
            remote_flag=j.get("isRemote"),
            comp_min=int(lo) if lo else None,
            comp_max=int(hi) if hi else None,
            comp_text=str(summary),
            external_id=str(j.get("id", "")),
            source="ashby", raw=j, **_base(cfg),
        ))
    return out


@adapter("smartrecruiters")
def smartrecruiters(cfg: dict) -> list[Job]:
    """Per-company postings API. The CROSS-company search endpoint is dead, this
    one is not."""
    slug = cfg["slug"]
    out, offset = [], 0
    while True:
        data = fetch_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
            f"?limit=100&offset={offset}"
        )
        if not isinstance(data, dict):
            break
        content = data.get("content") or []
        for j in content:
            loc = j.get("location") or {}
            loc_s = ", ".join(
                x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x
            )
            if loc.get("remote"):
                loc_s = (loc_s + " (Remote)").strip()
            out.append(Job(
                title=j.get("name", ""),
                url=(j.get("ref") or "").replace("api.smartrecruiters.com/v1", "jobs.smartrecruiters.com")
                    or f"https://jobs.smartrecruiters.com/{slug}/{j.get('id','')}",
                location=loc_s,
                posted_at=(j.get("releasedDate") or "")[:10],
                department=(j.get("department") or {}).get("label", "")
                           or (j.get("function") or {}).get("label", ""),
                remote_flag=bool(loc.get("remote")),
                external_id=str(j.get("id", "")),
                source="smartrecruiters", raw=j, **_base(cfg),
            ))
        total = data.get("totalFound", 0)
        offset += 100
        if offset >= total or not content or offset > 5000:
            break
    # SmartRecruiters list view has no body text; pull detail for in-family titles only.
    for job in out:
        if _looks_relevant(job.title):
            d = fetch_json(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job.external_id}"
            )
            if isinstance(d, dict):
                ad = (d.get("jobAd") or {}).get("sections") or {}
                job.description = strip_html(" \n".join(
                    (ad.get(k) or {}).get("text", "") for k in
                    ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
                ))
    return out


@adapter("workable")
def workable(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    jobs = (data or {}).get("jobs") if isinstance(data, dict) else None
    if not jobs:
        status, text = fetch(
            f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
            method="POST", json_body={"query": "", "location": [], "department": [],
                                      "worktype": [], "limit": 100},
        )
        if status == 200:
            try:
                jobs = json.loads(text).get("results", [])
            except Exception:
                jobs = []
    out = []
    for j in jobs or []:
        loc = j.get("location") or {}
        if isinstance(loc, dict):
            loc_s = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
        else:
            loc_s = str(loc)
        out.append(Job(
            title=j.get("title", ""),
            url=j.get("url") or j.get("shortlink") or
                f"https://apply.workable.com/{slug}/j/{j.get('shortcode','')}/",
            location=loc_s or ("Remote" if j.get("remote") else ""),
            description=strip_html(j.get("description", "") or "") + "\n" +
                        strip_html(j.get("requirements", "") or ""),
            posted_at=(j.get("published_on") or j.get("created_at") or "")[:10],
            department=j.get("department", "") or "",
            remote_flag=bool(j.get("remote") or j.get("telecommuting")),
            external_id=str(j.get("shortcode") or j.get("id") or ""),
            source="workable", raw=j, **_base(cfg),
        ))
    return out


@adapter("recruitee")
def recruitee(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://{slug}.recruitee.com/api/offers/")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("offers", []) or []:
        out.append(Job(
            title=j.get("title", ""),
            url=j.get("careers_url") or j.get("careers_apply_url", ""),
            location=", ".join(x for x in [j.get("city"), j.get("state_name"), j.get("country")] if x),
            description=strip_html(j.get("description", "")) + "\n" + strip_html(j.get("requirements", "")),
            posted_at=(j.get("published_at") or "")[:10],
            department=j.get("department", "") or "",
            remote_flag=str(j.get("remote", "")).lower() in ("true", "1", "remote"),
            external_id=str(j.get("id", "")),
            source="recruitee", raw=j, **_base(cfg),
        ))
    return out


@adapter("bamboohr")
def bamboohr(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://{slug}.bamboohr.com/careers/list")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("result", []) or []:
        loc = j.get("location") or {}
        out.append(Job(
            title=(j.get("jobOpeningName") or ""),
            url=f"https://{slug}.bamboohr.com/careers/{j.get('id','')}",
            location=", ".join(x for x in [loc.get("city"), loc.get("state"), loc.get("country")] if x)
                     or ("Remote" if j.get("isRemote") else ""),
            posted_at=(j.get("datePosted") or "")[:10],
            department=j.get("departmentLabel", "") or "",
            remote_flag=bool(j.get("isRemote")),
            external_id=str(j.get("id", "")),
            source="bamboohr", raw=j, **_base(cfg),
        ))
    for job in out:
        if _looks_relevant(job.title):
            d = fetch_json(f"https://{slug}.bamboohr.com/careers/{job.external_id}/detail")
            if isinstance(d, dict):
                job.description = strip_html((d.get("result") or {}).get("jobOpening", {}).get("description", ""))
    return out


@adapter("rippling")
def rippling(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        out.append(Job(
            title=j.get("name", ""),
            url=j.get("url", "") or f"https://ats.rippling.com/{slug}/jobs/{j.get('uuid','')}",
            location=j.get("workLocation", {}).get("label", "") if isinstance(j.get("workLocation"), dict) else "",
            description=strip_html(j.get("descriptionHtml", "")),
            department=(j.get("department") or {}).get("label", "") if isinstance(j.get("department"), dict) else "",
            external_id=str(j.get("uuid", "")),
            source="rippling", raw=j, **_base(cfg),
        ))
    return out


@adapter("teamtailor")
def teamtailor(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    status, text = fetch(f"https://{slug}.teamtailor.com/jobs.json")
    if status != 200:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    out = []
    for j in data if isinstance(data, list) else data.get("jobs", []):
        out.append(Job(
            title=j.get("title", ""), url=j.get("careersite-job-url", "") or j.get("url", ""),
            location=j.get("location", "") or "", description=strip_html(j.get("body", "")),
            external_id=str(j.get("id", "")), source="teamtailor", raw=j, **_base(cfg),
        ))
    return out


# --------------------------------------------------------------------------
# Enterprise platforms
# --------------------------------------------------------------------------

@adapter("workday")
def workday(cfg: dict) -> list[Job]:
    """Workday's own 'cxs' JSON API - the one the careers SPA itself calls.

    cfg needs: tenant, site, dc (e.g. wd1/wd5/wd12). Optionally `search` to
    narrow, since big tenants have thousands of reqs.
    """
    tenant, site = cfg["tenant"], cfg["site"]
    dc = cfg.get("dc", "wd1")
    host = f"https://{tenant}.{dc}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    terms = cfg.get("search") or [""]
    # Default 6 pages x 20 = 120 postings per term. Big tenants blow past
    # that: seven boards sat pinned at exactly 120, and salesforce.com's
    # "Customer Success" term alone matches 1,483 reqs (2026-08-03). Registry
    # entries can widen with `pages:` or narrow with sharper `search` terms.
    pages_cap = int(cfg.get("pages", 6))
    seen, out = set(), []
    for term in terms:
        offset, pages = 0, 0
        while pages < pages_cap:
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term}
            status, text = fetch(api, method="POST", json_body=body,
                                 headers={"Content-Type": "application/json"})
            if status != 200:
                break
            try:
                data = json.loads(text)
            except Exception:
                break
            posts = data.get("jobPostings") or []
            if not posts:
                break
            for j in posts:
                path = j.get("externalPath", "")
                if path in seen:
                    continue
                seen.add(path)
                out.append(Job(
                    title=j.get("title", ""),
                    url=f"{host}/{site}{path}",
                    location=j.get("locationsText", "") or "",
                    posted_at=j.get("postedOn", "") or "",
                    external_id=(j.get("bulletFields") or [""])[0] or path,
                    source="workday", raw=j, **_base(cfg),
                ))
            offset += 20
            pages += 1
            if offset >= data.get("total", 0):
                break
    # Detail fetch only for plausible titles - tenants can be huge.
    for job in out:
        if _looks_relevant(job.title):
            path = job.raw.get("externalPath", "")
            d = fetch_json(f"{host}/wday/cxs/{tenant}/{site}{path}")
            if not isinstance(d, dict):
                continue
            info = d.get("jobPostingInfo") or {}
            job.description = strip_html(info.get("jobDescription", ""))
            # remoteType is a label, not a boolean. Salesforce returns
            # 'Office - Flexible' and 'Office Tech-Flexible' for office-based
            # roles; treating it as truthy scored an onsite SF VP role remote.
            rt = str(info.get("remoteType") or "")
            job.remote_flag = bool(re.search(r"remote", rt, re.I)) if rt else None
            # The list view collapses multi-site reqs to "5 Locations"; the real
            # cities (including Atlanta) are only in the detail payload.
            locs = [info.get("location")] + list(info.get("additionalLocations") or [])
            locs = [x for x in locs if x]
            if locs:
                job.location = ", ".join(locs)
            if info.get("startDate"):
                job.posted_at = info["startDate"][:10]
    return out


@adapter("oracle_orc")
def oracle_orc(cfg: dict) -> list[Job]:
    """Oracle Recruiting Cloud / the modern Taleo replacement. Public REST."""
    host, site = cfg["host"], cfg["site"]
    out = []
    # Was a single request capped at limit=200, so employer 201 onward simply did
    # not exist as far as this tool was concerned, with nothing said about it.
    for off in range(0, 2000, 200):
        url = (
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber={site},limit=200,offset={off},sortBy=POSTING_DATES_DESC"
        )
        data = fetch_json(url)
        if not isinstance(data, dict):
            break
        before = len(out)
        for item in data.get("items", []) or []:
            for j in item.get("requisitionList", []) or []:
                out.append(Job(
                    title=j.get("Title", ""),
                    url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j.get('Id','')}",
                    location=j.get("PrimaryLocation", "") or "",
                    posted_at=(j.get("PostedDate") or "")[:10],
                    description=strip_html(j.get("ShortDescriptionStr", "") or ""),
                    external_id=str(j.get("Id", "")),
                    source="oracle_orc", raw=j, **_base(cfg),
                ))
        if len(out) - before < 200:      # short page means the last page
            break
    return out


@adapter("eightfold")
def eightfold(cfg: dict) -> list[Job]:
    """Eightfold.ai career sites (Bayer, Vodafone, many F500)."""
    domain = cfg["domain"]
    host = cfg.get("host", "https://jobs.eightfold.ai")
    out = []
    for start in range(0, 1000, 100):   # was capped at 300
        data = fetch_json(
            f"{host}/api/apply/v2/jobs?domain={domain}&start={start}&num=100"
            f"&sort_by=timestamp&triggerGoButton=false"
        )
        if not isinstance(data, dict):
            break
        positions = data.get("positions") or []
        if not positions:
            break
        for j in positions:
            out.append(Job(
                title=j.get("name", ""),
                url=j.get("canonicalPositionUrl", "") or
                    f"{host}/careers/job/{j.get('id','')}",
                location=j.get("location", "") or ", ".join(j.get("locations", []) or []),
                description=strip_html(j.get("job_description", "")),
                posted_at=str(j.get("t_create", ""))[:10],
                department=j.get("department", "") or "",
                external_id=str(j.get("id", "")),
                source="eightfold", raw=j, **_base(cfg),
            ))
    return out


@adapter("phenom")
def phenom(cfg: dict) -> list[Job]:
    """Phenom People career sites - very common at large US enterprises."""
    host = cfg["host"].rstrip("/")
    out = []
    for page in range(20):   # was capped at 150 postings
        data = fetch_json(
            f"{host}/widgets?ddoKey=refineSearch&sortBy=&subsearch=&from={page*50}&size=50"
            f"&jobs=true&counts=true&all_fields=true"
        )
        if not isinstance(data, dict):
            break
        ref = (data.get("refineSearch") or {}).get("data", {}).get("jobs") or []
        if not ref:
            break
        for j in ref:
            out.append(Job(
                title=j.get("title", ""),
                url=j.get("applyUrl", "") or f"{host}/job/{j.get('jobSeqNo','')}",
                location=", ".join(x for x in [j.get("cityStateCountry") or j.get("location")] if x),
                description=strip_html(j.get("descriptionTeaser", "")),
                posted_at=(j.get("postedDate") or "")[:10],
                department=j.get("category", "") or "",
                external_id=str(j.get("jobId") or j.get("jobSeqNo") or ""),
                source="phenom", raw=j, **_base(cfg),
            ))
    return out


_ICIMS_CARD = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.S)
_ICIMS_LINK = re.compile(r'<a href="([^"]*?/jobs/(\d+)/[^"]*?)"[^>]*class="iCIMS_Anchor"', re.S)
_ICIMS_TITLE = re.compile(r'<h3[^>]*>\s*(.*?)\s*</h3>', re.S)
_ICIMS_LOC = re.compile(r'Job Locations?</span>\s*<span[^>]*>\s*(.*?)\s*</span>', re.S)
_ICIMS_DESC = re.compile(r'class="col-xs-12 description">\s*(.*?)\s*</div>', re.S)


@adapter("icims")
def icims(cfg: dict) -> list[Job]:
    """iCIMS has no public JSON, but its search page is server-rendered into
    job cards that already carry title, location and a description snippet, so
    no per-job detail request is needed for screening."""
    slug = cfg["slug"]
    base = cfg.get("host") or f"https://careers-{slug}.icims.com"
    out, seen = [], set()
    for page in range(0, 30):   # was capped at 6 pages
        status, text = fetch(f"{base}/jobs/search?ss=1&in_iframe=1&pr={page}")
        if status != 200 or not text:
            break
        cards = _ICIMS_CARD.findall(text)
        if not cards:
            break
        added = 0
        for card in cards:
            link = _ICIMS_LINK.search(card)
            title = _ICIMS_TITLE.search(card)
            if not (link and title):
                continue
            jid = link.group(2)
            if jid in seen:
                continue
            seen.add(jid)
            added += 1
            loc = _ICIMS_LOC.search(card)
            desc = _ICIMS_DESC.search(card)
            out.append(Job(
                title=strip_html(title.group(1)),
                url=link.group(1).split("?")[0],
                location=strip_html(loc.group(1)).replace("US-", "").replace("-", ", ")
                         if loc else "",
                description=strip_html(desc.group(1)) if desc else "",
                external_id=jid, source="icims", raw={}, **_base(cfg),
            ))
        if not added:
            break
    # Full body only where the title is plausible.
    for job in out:
        if _looks_relevant(job.title):
            st, tx = fetch(job.url + "?in_iframe=1")
            if st == 200:
                body = re.search(r'<div[^>]*class="[^"]*iCIMS_JobContent[^"]*"(.*?)(?:</body>|iCIMS_JobFooter)',
                                 tx, re.S)
                job.description = strip_html(body.group(1))[:12000] if body else job.description
    return out


@adapter("jobvite")
def jobvite(cfg: dict) -> list[Job]:
    """Jobvite embeds its job list as JSON inside the careers page."""
    slug = cfg["slug"]
    status, text = fetch(f"https://jobs.jobvite.com/{slug}/search")
    if status != 200:
        return []
    out = []
    for m in re.finditer(
        r'href="(/' + re.escape(slug) + r'/job/([A-Za-z0-9]+))"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{3,120})',
        text,
    ):
        out.append(Job(
            title=strip_html(m.group(3)),
            url=f"https://jobs.jobvite.com{m.group(1)}",
            external_id=m.group(2), source="jobvite", raw={}, **_base(cfg),
        ))
    seen, uniq = set(), []
    for j in out:
        if j.external_id in seen:
            continue
        seen.add(j.external_id)
        uniq.append(j)
    for job in uniq:
        if _looks_relevant(job.title):
            st, tx = fetch(job.url)
            if st == 200:
                job.description = strip_html(tx)[:9000]
                loc = re.search(r'jv-job-detail-meta[^>]*>(.*?)</div>', tx, re.S)
                if loc:
                    job.location = strip_html(loc.group(1))[:120]
    return uniq


@adapter("paylocity")
def paylocity(cfg: dict) -> list[Job]:
    """Paylocity Recruiting - common at US mid-market and nonprofits."""
    guid = cfg["guid"]
    data = fetch_json(f"https://recruiting.paylocity.com/recruiting/v2/api/jobs?companyId={guid}")
    if not isinstance(data, (list, dict)):
        return []
    items = data if isinstance(data, list) else data.get("data", [])
    out = []
    for j in items or []:
        out.append(Job(
            title=j.get("jobTitle") or j.get("title", ""),
            url=j.get("jobUrl") or f"https://recruiting.paylocity.com/recruiting/jobs/Details/{j.get('jobId','')}",
            location=", ".join(x for x in [j.get("city"), j.get("state")] if x),
            description=strip_html(j.get("jobDescription", "") or j.get("description", "")),
            posted_at=(j.get("publishedDate") or "")[:10],
            external_id=str(j.get("jobId") or j.get("id") or ""),
            source="paylocity", raw=j, **_base(cfg),
        ))
    return out


@adapter("personio")
def personio(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    status, text = fetch(f"https://{slug}.jobs.personio.com/xml")
    if status != 200:
        return []
    out = []
    for block in re.findall(r"<position>(.*?)</position>", text, re.S):
        def g(tag):
            m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block, re.S)
            return strip_html(m.group(1)) if m else ""
        out.append(Job(
            title=g("name"), url=f"https://{slug}.jobs.personio.com/job/{g('id')}",
            location=g("office"), description=g("jobDescriptions"),
            department=g("department"), external_id=g("id"),
            source="personio", raw={}, **_base(cfg),
        ))
    return out


# --------------------------------------------------------------------------

# Set from the user's own profile by set_relevance_terms(). It starts as None on
# purpose: until somebody says what they are looking for, every posting is worth
# a detail request.
#
# This was a hardcoded regex of the original author's job search
# (salesforce|crm|solution architect|...) until 2026-08-05. Five adapters gate
# their detail fetch on it, so for anyone whose work was not Salesforce it
# returned False for every title, no description was ever fetched, and their
# postings were scored on the title alone: body exclusions never fired, comp was
# never parsed, and everything landed in VERIFY with no explanation. Every title
# in this repo's own example profile failed it. Three audits missed it because
# they reviewed diffs and nobody read this file.
_RELEVANT_HINT: re.Pattern | None = None


def set_relevance_terms(terms) -> None:
    """Teach the detail pre-filter what this user is looking for.

    Accepts plain terms and /raw regex/ entries, matching the profile syntax.
    Anything unusable is skipped rather than allowed to make the pattern match
    everything or nothing."""
    global _RELEVANT_HINT
    parts = []
    for t in terms or []:
        t = str(t or "").strip()
        if not t:
            continue
        if t.startswith("/") and t.endswith("/") and len(t) > 2:
            try:
                re.compile(t[1:-1])
            except re.error:
                continue
            parts.append(f"(?:{t[1:-1]})")
        else:
            parts.append(re.escape(t))
    _RELEVANT_HINT = re.compile("|".join(parts), re.I) if parts else None


def _looks_relevant(title: str) -> bool:
    """Cheap pre-filter so detail requests are spent on plausible titles.

    FAILS OPEN. With no profile terms set, everything is relevant. The cost of a
    wrong True is one wasted HTTP request; the cost of a wrong False is a job the
    user never sees and cannot know they missed."""
    if _RELEVANT_HINT is None:
        return True
    return bool(_RELEVANT_HINT.search(title or ""))


# Hard ceilings each adapter can reach. A board sitting exactly on one is very
# likely truncated rather than exactly that size, and because the number is
# stable the drop-to-zero guard never notices.
PAGE_CEILING = {
    "oracle_orc": 2000, "eightfold": 1000, "phenom": 1000,
    "icims": 30 * 50, "smartrecruiters": 5000, "workable": 100,
}


def at_page_ceiling(ats: str, n: int) -> bool:
    c = PAGE_CEILING.get(ats)
    return bool(c and n >= c)


def run_adapter(cfg: dict) -> tuple[list[Job], str | None]:
    """Dispatch one registry entry. Returns (jobs, error_or_None)."""
    ats = cfg.get("ats")
    fn = REGISTRY.get(ats)
    if not fn:
        return [], f"no adapter for ats={ats!r}"
    http.reset_status()          # never inherit the previous board's status
    try:
        jobs = fn(cfg)
    except Exception as e:  # a single bad board must never kill the run
        return [], f"{type(e).__name__}: {e}"
    if not jobs:
        # An empty list means one of two very different things. Report which.
        st = http.last_status()
        if st == 200:
            return [], None                       # board is fine, nothing open
        if st is None:
            return [], "no response (network/DNS/timeout)"
        return [], f"HTTP {st}"
    return jobs, None
