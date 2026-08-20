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
import math
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote, urljoin, urlsplit

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


def board_id(cfg: dict) -> str:
    """Stable identity for a board: platform + the slug/tenant that addresses it.

    Health used to key on the registry DISPLAY NAME, so renaming an employer in
    employers.yaml stranded every row written under the old name; they matched
    no healthy board and could never be retired."""
    ats = cfg.get("ats", "")
    if ats == "workday":
        key = "/".join(str(cfg.get(k) or "") for k in ("tenant", "dc", "site"))
    elif ats == "oracle_orc":
        key = "/".join(str(cfg.get(k) or "") for k in ("host", "site"))
    elif ats == "phenom":
        key = str(cfg.get("host") or "")
    else:
        key = cfg.get("slug") or cfg.get("tenant") or cfg.get("domain") or cfg.get("guid") or ""
    return f"{cfg.get('ats', '')}:{key}".lower()


def _base(cfg: dict) -> dict:
    return {
        "board": board_id(cfg),
        "company": cfg.get("name") or cfg.get("slug", ""),
        "lane": cfg.get("lane", ""),
        "registry_lane": cfg.get("lane", ""),
        "employer_tier": cfg.get("tier", ""),
        "rails_exempt": bool(cfg.get("rails_exempt", False)),
    }


def _positive_int(value) -> int | None:
    """Return a finite positive whole-unit amount, never an inferred salary."""
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number)


def _sections(*parts: tuple[str, object]) -> str:
    """Join separately-published JD sections without losing their headings."""
    rendered = []
    for heading, body in parts:
        text = strip_html(str(body or ""))
        if not text:
            continue
        rendered.append(f"{heading}\n{text}" if heading else text)
    return "\n\n".join(rendered)


def _required_object_list(data, key: str, source: str) -> list[dict] | None:
    """Validate a documented ``{key: [objects...]}`` listing envelope.

    An HTTP 200 only proves that a server answered.  Login pages serialized as
    JSON, vendor error envelopes, and changed API contracts must not be treated
    as a healthy board with zero openings.  Bad siblings are skipped so useful
    rows from an otherwise-partial response can still be retained.
    """
    if not isinstance(data, dict):
        if http.last_status() == 200:
            http.mark_partial(f"{source} listing response had an unexpected shape")
        return None
    items = data.get(key)
    if not isinstance(items, list):
        http.mark_partial(
            f"{source} listing response did not contain {key!r} as a list")
        return None
    valid = []
    for index, item in enumerate(items):
        if isinstance(item, dict):
            valid.append(item)
        else:
            http.mark_partial(
                f"{source} listing {key}[{index}] was not an object")
    return valid


def _nonnegative_count(value) -> int | None:
    """Validate a JSON count without accepting bools, NaN, or fractions."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _stable_provider_id(value) -> str:
    """Normalize provider identities without manufacturing one from junk."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def _nonblank_title(value) -> str:
    """A title is user-facing text, not an arbitrary JSON scalar."""
    return value.strip() if isinstance(value, str) else ""


def _actionable_job_url(value) -> str:
    """Return a direct HTTP(S) job URL, never a blank/relative/script target."""
    if not isinstance(value, str):
        return ""
    target = value.strip()
    if not target or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in target):
        return ""
    try:
        parsed = urlsplit(target)
        _ = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None):
        return ""
    return target


def _required_listing_fields(
    source: str,
    index: int,
    identity_value,
    title_value,
    *url_values,
    url_builder: Callable[[str], str] | None = None,
) -> tuple[str, str, str] | None:
    """Validate the minimum evidence required to emit an actionable posting.

    A provider-owned ID keeps sibling requisitions distinct, a title makes the
    row screenable, and a direct URL makes it usable. Some providers omit a URL
    because their public detail route is defined entirely by that ID; callers
    may supply that vendor-specific construction explicitly.
    """
    identity = _stable_provider_id(identity_value)
    title = _nonblank_title(title_value)
    url = next((candidate for candidate in
                (_actionable_job_url(value) for value in url_values)
                if candidate), "")
    if not url and identity and url_builder is not None:
        url = _actionable_job_url(url_builder(identity))
    if identity and title and url:
        return identity, title, url
    missing = []
    if not identity:
        missing.append("stable provider id")
    if not title:
        missing.append("nonblank title")
    if not url:
        missing.append("actionable URL")
    http.mark_partial(
        f"{source} listing row {index} lacked " + ", ".join(missing))
    return None


# --------------------------------------------------------------------------
# Clean public JSON boards
# --------------------------------------------------------------------------

@adapter("greenhouse")
def greenhouse(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    jobs = _required_object_list(data, "jobs", "greenhouse")
    if jobs is None:
        return []
    out, seen = [], set()
    for index, j in enumerate(jobs, 1):
        external_id = str(j.get("id") or "").strip()
        title = str(j.get("title") or "").strip()
        absolute_url = str(j.get("absolute_url") or "").strip()
        if not (external_id and title and absolute_url):
            http.mark_partial(
                f"greenhouse listing job {index} lacked id, title, or URL")
            continue
        if external_id in seen:
            http.mark_partial(
                f"greenhouse listing repeated job id {external_id}")
            continue
        seen.add(external_id)
        offices = ", ".join(o.get("name", "") for o in (j.get("offices") or []))
        meta = {m.get("name"): m.get("value") for m in (j.get("metadata") or [])}
        out.append(Job(
            title=title,
            url=absolute_url,
            location=(j.get("location") or {}).get("name", "") or offices,
            description=strip_html(j.get("content", "")),
            posted_at=(j.get("updated_at") or j.get("first_published") or "")[:10],
            department=", ".join(d.get("name", "") for d in (j.get("departments") or [])),
            external_id=external_id,
            comp_text=str(meta.get("Salary Range") or meta.get("Compensation") or ""),
            source="greenhouse", raw=j, **_base(cfg),
        ))
    meta = data.get("meta")
    if meta is not None:
        total = (_nonnegative_count(meta.get("total"))
                 if isinstance(meta, dict) else None)
        if total is None:
            http.mark_partial("greenhouse listing metadata had an invalid total")
        elif len(out) < total:
            http.mark_capped(
                f"greenhouse returned {len(out)} of {total} advertised postings")
        elif len(out) > total:
            http.mark_partial(
                f"greenhouse returned {len(out)} postings above advertised total {total}")
    return out


@adapter("lever")
def lever(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        if http.last_status() == 200:
            http.mark_partial("lever listing response had an unexpected shape")
        return []
    out = []
    for index, j in enumerate(data, 1):
        if not isinstance(j, dict):
            http.mark_partial(f"lever listing row {index} was not an object")
            continue
        required = _required_listing_fields(
            "lever", index, j.get("id"), j.get("text"),
            j.get("hostedUrl"), j.get("applyUrl"),
            url_builder=lambda identity:
                f"https://jobs.lever.co/{quote(slug, safe='')}/{quote(identity, safe='')}",
        )
        if required is None:
            continue
        external_id, title, direct_url = required
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
            title=title,
            url=direct_url,
            location=cats.get("location", "") or "",
            description=body.strip(),
            posted_at="",
            department=" / ".join(x for x in [cats.get("team"), cats.get("department")] if x),
            comp_text=str(cats.get("commitment") or ""),
            external_id=external_id,
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
    jobs = _required_object_list(data, "jobs", "ashby")
    if jobs is None:
        return []
    out = []
    for index, j in enumerate(jobs, 1):
        required = _required_listing_fields(
            "ashby", index, j.get("id"), j.get("title"),
            j.get("jobUrl"), j.get("applyUrl"),
            url_builder=lambda identity:
                f"https://jobs.ashbyhq.com/{quote(slug, safe='')}/{quote(identity, safe='')}",
        )
        if required is None:
            continue
        external_id, title, direct_url = required
        comp = j.get("compensation") or {}
        summary = comp.get("compensationTierSummary") or ""
        lo = hi = None
        for tier in comp.get("compensationTiers") or []:
            for comp_part in tier.get("components") or []:
                if comp_part.get("compensationType") == "Salary":
                    lo = comp_part.get("minValue") or lo
                    hi = comp_part.get("maxValue") or hi
        out.append(Job(
            title=title,
            url=direct_url,
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
            external_id=external_id,
            source="ashby", raw=j, **_base(cfg),
        ))
    return out


@adapter("smartrecruiters")
def smartrecruiters(cfg: dict) -> list[Job]:
    """Per-company postings API. The CROSS-company search endpoint is dead, this
    one is not."""
    slug = cfg["slug"]
    out, offset = [], 0
    seen_ids: set[str] = set()
    page_fingerprints: set[tuple[str, ...]] = set()
    reported_total = 0
    while True:
        data = fetch_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
            f"?limit=100&offset={offset}"
        )
        content = _required_object_list(data, "content", "smartrecruiters")
        if content is None:
            http.mark_partial(
                f"smartrecruiters listing page offset {offset} failed validation")
            break
        raw_page_size = len(data["content"])
        total = _nonnegative_count(data.get("totalFound"))
        if total is None:
            http.mark_partial(
                f"smartrecruiters listing page offset {offset} had invalid totalFound")
            break
        reported_total = max(reported_total, total)
        if not content:
            if len(seen_ids) < reported_total:
                http.mark_partial(
                    f"smartrecruiters listing ended at {len(seen_ids)} of "
                    f"{reported_total} unique postings")
            break
        if total == 0:
            http.mark_partial(
                f"smartrecruiters listing page offset {offset} reported "
                "totalFound 0 for a nonempty page")

        page_ids: list[str] = []
        page_seen: set[str] = set()
        page_rows: list[tuple[str, Job]] = []
        for j in content:
            identity = str(j.get("id") or "").strip()
            if not identity:
                http.mark_partial(
                    f"smartrecruiters listing page offset {offset} contained "
                    "a posting without an identity")
                continue
            page_ids.append(identity)
            if identity in page_seen:
                http.mark_partial(
                    f"smartrecruiters listing page offset {offset} repeated "
                    f"identity {identity}")
                continue
            page_seen.add(identity)
            loc = j.get("location") or {}
            if not isinstance(loc, dict):
                http.mark_partial(
                    f"smartrecruiters listing content row {j.get('id') or '?'} "
                    "had an invalid location")
                loc = {}
            loc_s = ", ".join(
                x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x
            )
            if loc.get("remote"):
                loc_s = (loc_s + " (Remote)").strip()
            department = j.get("department") or {}
            function = j.get("function") or {}
            if not isinstance(department, dict):
                department = {}
                http.mark_partial(
                    f"smartrecruiters listing content row {j.get('id') or '?'} "
                    "had an invalid department")
            if not isinstance(function, dict):
                function = {}
                http.mark_partial(
                    f"smartrecruiters listing content row {j.get('id') or '?'} "
                    "had an invalid function")
            page_rows.append((identity, Job(
                title=j.get("name", ""),
                url=(j.get("ref") or "").replace("api.smartrecruiters.com/v1", "jobs.smartrecruiters.com")
                    or f"https://jobs.smartrecruiters.com/{slug}/{j.get('id','')}",
                location=loc_s,
                posted_at=(j.get("releasedDate") or "")[:10],
                department=department.get("label", "") or function.get("label", ""),
                remote_flag=bool(loc.get("remote")),
                external_id=str(j.get("id", "")),
                source="smartrecruiters", raw=j, **_base(cfg),
            )))

        fingerprint = tuple(page_ids)
        if fingerprint and fingerprint in page_fingerprints:
            http.mark_partial(
                f"smartrecruiters repeated a listing page at offset {offset}")
            break
        if fingerprint:
            page_fingerprints.add(fingerprint)

        new_on_page = 0
        for identity, job in page_rows:
            if identity in seen_ids:
                http.mark_partial(
                    f"smartrecruiters listing page offset {offset} replayed "
                    f"identity {identity} from an earlier page")
                continue
            seen_ids.add(identity)
            new_on_page += 1
            out.append(job)
        if content and not new_on_page:
            http.mark_partial(
                f"smartrecruiters listing page offset {offset} made no unique progress")
            break

        page_end = offset + raw_page_size
        offset += 100
        if len(seen_ids) > reported_total:
            http.mark_partial(
                f"smartrecruiters returned {len(seen_ids)} unique postings "
                f"above totalFound {reported_total}")
            break
        if len(seen_ids) >= reported_total:
            break
        if page_end >= reported_total:
            http.mark_partial(
                f"smartrecruiters listing ended at {len(seen_ids)} of "
                f"{reported_total} unique postings")
            break
        if raw_page_size < 100:
            http.mark_partial(
                f"smartrecruiters listing returned a short page at "
                f"{len(seen_ids)} of {reported_total} unique postings")
        if offset > 5000:
            break
    # SmartRecruiters list view has no body text; pull detail for in-family titles only.
    for job in out:
        if _looks_relevant(job.title):
            d = fetch_json(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job.external_id}"
            )
            job_ad = d.get("jobAd") if isinstance(d, dict) else None
            sections = job_ad.get("sections") if isinstance(job_ad, dict) else None
            if not isinstance(sections, dict):
                http.mark_partial(
                    f"smartrecruiters detail {job.external_id or job.url} "
                    "had an unexpected shape")
                continue
            parts = []
            for key in ("companyDescription", "jobDescription", "qualifications",
                        "additionalInformation"):
                section = sections.get(key) or {}
                if not isinstance(section, dict):
                    http.mark_partial(
                        f"smartrecruiters detail {job.external_id or job.url} "
                        f"had an invalid {key} section")
                    continue
                parts.append(str(section.get("text") or ""))
            description = strip_html(" \n".join(parts))
            if description:
                job.description = description
            else:
                http.mark_partial(
                    f"smartrecruiters detail {job.external_id or job.url} had no JD text")
    return out


@adapter("workable")
def workable(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        status, text = fetch(
            f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
            method="POST", json_body={"query": "", "location": [], "department": [],
                                      "worktype": [], "limit": 100},
        )
        if status != 200:
            http.mark_partial(f"workable fallback returned HTTP {status}")
            jobs = []
        else:
            try:
                fallback = json.loads(text)
            except Exception:
                http.mark_partial("workable fallback returned unusable JSON")
                fallback = None
            if not isinstance(fallback, dict) or not isinstance(
                    fallback.get("results"), list):
                http.mark_partial("workable fallback had an unexpected shape")
                jobs = []
            else:
                jobs = fallback["results"]
    out = []
    by_id: dict[str, Job] = {}
    for index, j in enumerate(jobs or [], 1):
        if not isinstance(j, dict):
            http.mark_partial(
                f"workable listing row {index} was not an object")
            continue
        shortcode = _stable_provider_id(j.get("shortcode"))
        required = _required_listing_fields(
            "workable", index, shortcode or j.get("id"), j.get("title"),
            j.get("url"), j.get("shortlink"),
            url_builder=(lambda _identity:
                         f"https://apply.workable.com/{quote(slug, safe='')}/j/"
                         f"{quote(shortcode, safe='')}/") if shortcode else None,
        )
        if required is None:
            continue
        ext, title, direct_url = required
        loc = j.get("location") or {}
        if isinstance(loc, dict):
            loc_s = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
        else:
            loc_s = str(loc)
        # The current widget returns one row per advertised country and puts
        # the place in `locations` (plus top-level city/state/country), not in
        # the older singular `location`.  Ignoring both silently reduced every
        # multi-country opening to a generic "Remote" row.  Rows for those
        # countries share a shortcode, so merge them into the one requisition
        # rather than sending duplicates downstream.
        if not loc_s:
            places = j.get("locations") or []
            rendered = []
            for place in places if isinstance(places, list) else []:
                if not isinstance(place, dict):
                    continue
                value = ", ".join(x for x in
                                  [place.get("city"), place.get("region"), place.get("country")]
                                  if x)
                if value and value not in rendered:
                    rendered.append(value)
            loc_s = "; ".join(rendered)
        if not loc_s:
            loc_s = ", ".join(x for x in
                              [j.get("city"), j.get("state"), j.get("country")] if x)
        job = Job(
            title=title,
            url=direct_url,
            location=loc_s or ("Remote" if j.get("remote") else ""),
            description=strip_html(j.get("description", "") or "") + "\n" +
                        strip_html(j.get("requirements", "") or ""),
            posted_at=(j.get("published_on") or j.get("created_at") or "")[:10],
            department=j.get("department", "") or "",
            remote_flag=bool(j.get("remote") or j.get("telecommuting")),
            external_id=ext,
            source="workable", raw=j, **_base(cfg),
        )
        if ext and ext in by_id:
            prior = by_id[ext]
            locations = [x for x in (prior.location, job.location) if x]
            prior.location = "; ".join(dict.fromkeys(
                part for value in locations for part in value.split("; ") if part
            ))
            continue
        out.append(job)
        if ext:
            by_id[ext] = job
    return out


@adapter("recruitee")
def recruitee(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://{slug}.recruitee.com/api/offers/")
    offers = _required_object_list(data, "offers", "recruitee")
    if offers is None:
        return []
    out = []
    for index, j in enumerate(offers, 1):
        required = _required_listing_fields(
            "recruitee", index, j.get("id"), j.get("title"),
            j.get("careers_url"), j.get("careers_apply_url"),
        )
        if required is None:
            continue
        external_id, title, direct_url = required
        out.append(Job(
            title=title,
            url=direct_url,
            location=", ".join(x for x in [j.get("city"), j.get("state_name"), j.get("country")] if x),
            description=strip_html(j.get("description", "")) + "\n" + strip_html(j.get("requirements", "")),
            posted_at=(j.get("published_at") or "")[:10],
            department=j.get("department", "") or "",
            remote_flag=str(j.get("remote", "")).lower() in ("true", "1", "remote"),
            external_id=external_id,
            source="recruitee", raw=j, **_base(cfg),
        ))
    return out


@adapter("bamboohr")
def bamboohr(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://{slug}.bamboohr.com/careers/list")
    results = _required_object_list(data, "result", "bamboohr")
    if results is None:
        return []
    out = []
    for index, j in enumerate(results, 1):
        required = _required_listing_fields(
            "bamboohr", index, j.get("id"), j.get("jobOpeningName"),
            url_builder=lambda identity:
                f"https://{slug}.bamboohr.com/careers/{quote(identity, safe='')}",
        )
        if required is None:
            continue
        external_id, title, direct_url = required
        loc = j.get("location") or {}
        if not isinstance(loc, dict):
            http.mark_partial(
                f"bamboohr listing result row {j.get('id') or '?'} had an invalid location")
            loc = {}
        out.append(Job(
            title=title,
            url=direct_url,
            location=", ".join(x for x in [loc.get("city"), loc.get("state"), loc.get("country")] if x)
                     or ("Remote" if j.get("isRemote") else ""),
            posted_at=(j.get("datePosted") or "")[:10],
            department=j.get("departmentLabel", "") or "",
            remote_flag=bool(j.get("isRemote")),
            external_id=external_id,
            source="bamboohr", raw=j, **_base(cfg),
        ))
    meta = data.get("meta")
    if meta is not None:
        total = (_nonnegative_count(meta.get("totalCount"))
                 if isinstance(meta, dict) else None)
        if total is None:
            http.mark_partial("bamboohr listing metadata had an invalid totalCount")
        elif len(out) < total:
            http.mark_capped(
                f"bamboohr returned {len(out)} of {total} advertised postings")
        elif len(out) > total:
            http.mark_partial(
                f"bamboohr returned {len(out)} postings above advertised total {total}")
    for job in out:
        if _looks_relevant(job.title):
            d = fetch_json(f"https://{slug}.bamboohr.com/careers/{job.external_id}/detail")
            result = d.get("result") if isinstance(d, dict) else None
            opening = result.get("jobOpening") if isinstance(result, dict) else None
            if not isinstance(opening, dict):
                http.mark_partial(
                    f"bamboohr detail {job.external_id or job.url} "
                    "had an unexpected shape")
                continue
            description = strip_html(opening.get("description", ""))
            if description:
                job.description = description
            else:
                http.mark_partial(
                    f"bamboohr detail {job.external_id or job.url} had no JD text")
    return out


@adapter("rippling")
def rippling(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    data = fetch_json(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
    if not isinstance(data, list):
        if http.last_status() == 200:
            http.mark_partial("rippling listing response had an unexpected shape")
        return []
    out = []
    for index, j in enumerate(data, 1):
        if not isinstance(j, dict):
            http.mark_partial(f"rippling listing row {index} was not an object")
            continue
        required = _required_listing_fields(
            "rippling", index, j.get("uuid"), j.get("name"), j.get("url"),
            url_builder=lambda identity:
                f"https://ats.rippling.com/{quote(slug, safe='')}/jobs/"
                f"{quote(identity, safe='')}",
        )
        if required is None:
            continue
        external_id, title, direct_url = required
        out.append(Job(
            title=title,
            url=direct_url,
            location=j.get("workLocation", {}).get("label", "") if isinstance(j.get("workLocation"), dict) else "",
            description=strip_html(j.get("descriptionHtml", "")),
            department=(j.get("department") or {}).get("label", "") if isinstance(j.get("department"), dict) else "",
            external_id=external_id,
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
        http.mark_partial("teamtailor jobs.json returned unusable JSON")
        return []
    next_url = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Teamtailor's public jobs.json changed from a private `jobs` shape to
        # JSON Feed 1.1.  The schema.org JobPosting nested in each feed item is
        # the current public contract and carries the useful location/body.
        legacy_jobs = data.get("jobs")
        feed_items = data.get("items")
        if isinstance(legacy_jobs, list) and legacy_jobs:
            items = legacy_jobs
        elif isinstance(feed_items, list):
            items = feed_items
        elif isinstance(legacy_jobs, list):
            items = legacy_jobs
        else:
            http.mark_partial(
                "teamtailor jobs.json did not contain jobs/items as a list")
            return []
        next_url = data.get("next_url")
        if next_url is not None and not isinstance(next_url, str):
            http.mark_partial("teamtailor jobs.json had an invalid next_url")
            next_url = None
    else:
        http.mark_partial("teamtailor jobs.json had an unexpected shape")
        return []
    out = []
    for index, j in enumerate(items, 1):
        if not isinstance(j, dict):
            http.mark_partial(
                f"teamtailor listing row {index} was not an object")
            continue
        posting = j.get("_jobposting") or {}
        if not isinstance(posting, dict):
            http.mark_partial("teamtailor job had invalid _jobposting data")
            posting = {}
        locs = posting.get("jobLocation") or []
        if isinstance(locs, dict):
            locs = [locs]
        rendered = []
        for place in locs:
            if not isinstance(place, dict):
                http.mark_partial("teamtailor job had an invalid location")
                continue
            address = place.get("address") or {}
            if not isinstance(address, dict):
                http.mark_partial("teamtailor job had an invalid address")
                continue
            value = ", ".join(x for x in
                              [address.get("addressLocality"), address.get("addressRegion"),
                               address.get("addressCountry")] if x)
            if value and value not in rendered:
                rendered.append(value)
        identifier = posting.get("identifier") or ""
        if isinstance(identifier, dict):
            identifier = identifier.get("value") or identifier.get("name") or ""
        required = _required_listing_fields(
            "teamtailor", index, j.get("id") or identifier,
            j.get("title") or posting.get("title"),
            j.get("careersite-job-url"), j.get("url"),
        )
        if required is None:
            continue
        external_id, title, direct_url = required
        out.append(Job(
            title=title,
            url=direct_url,
            location=j.get("location", "") or "; ".join(rendered),
            description=strip_html(j.get("body", "") or j.get("content_html", "")
                                   or posting.get("description", "")),
            posted_at=(j.get("date_published") or posting.get("datePosted") or "")[:10],
            remote_flag=True if posting.get("jobLocationType") == "TELECOMMUTE" else None,
            external_id=external_id, source="teamtailor", raw=j, **_base(cfg),
        ))
    if isinstance(next_url, str) and next_url.strip():
        http.mark_capped(
            f"teamtailor advertised another JSON Feed page at {next_url.strip()[:100]}")
    return out


@adapter("pinpoint")
def pinpoint(cfg: dict) -> list[Job]:
    """Pinpoint's documented public postings.json career-site feed."""
    slug = cfg["slug"]
    base = f"https://{slug}.pinpointhq.com/"
    data = fetch_json(urljoin(base, "postings.json"))
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        if http.last_status() == 200:
            http.mark_partial("pinpoint postings.json had an unexpected shape")
        return []

    out = []
    for index, posting in enumerate(data["data"], 1):
        if not isinstance(posting, dict):
            http.mark_partial(
                f"pinpoint listing row {index} was not an object")
            continue
        job = posting.get("job") if isinstance(posting.get("job"), dict) else {}
        raw_target = posting.get("url") or posting.get("path")
        resolved_target = (urljoin(base, raw_target.strip())
                           if isinstance(raw_target, str) and raw_target.strip()
                           else "")
        required = _required_listing_fields(
            "pinpoint", index, posting.get("id"), posting.get("title"),
            resolved_target,
            url_builder=lambda identity:
                urljoin(base, f"postings/{quote(identity, safe='')}"),
        )
        if required is None:
            continue
        posting_id, title, direct_url = required
        department = job.get("department") or posting.get("department") or {}
        if isinstance(department, dict):
            department = department.get("name") or ""
        location = posting.get("location") or {}
        if isinstance(location, dict):
            location = location.get("name") or ""

        visible = posting.get("compensation_visible") is True
        currency = str(posting.get("compensation_currency") or "").upper()
        frequency = str(posting.get("compensation_frequency") or "").casefold()
        annual_usd = visible and currency == "USD" and frequency in {
            "year", "annual", "annually",
        }
        comp_min = (_positive_int(posting.get("compensation_minimum"))
                    if annual_usd else None)
        comp_max = (_positive_int(posting.get("compensation_maximum"))
                    if annual_usd else None)
        comp_text = str(posting.get("compensation") or "") if visible else ""

        workplace = str(posting.get("workplace_type") or "").casefold()
        remote = (True if workplace in {"remote", "fully_remote"}
                  else False if workplace in {"onsite", "on_site"}
                  else None)
        out.append(Job(
            title=title,
            url=direct_url,
            location=str(location),
            description=_sections(
                ("", posting.get("description")),
                (strip_html(posting.get("key_responsibilities_header"))
                 or "Key responsibilities", posting.get("key_responsibilities")),
                (strip_html(posting.get("skills_knowledge_expertise_header"))
                 or "Skills, knowledge and expertise",
                 posting.get("skills_knowledge_expertise")),
                (strip_html(posting.get("benefits_header")) or "Benefits",
                 posting.get("benefits")),
                ("Employment type", posting.get("employment_type_text")
                 or posting.get("employment_type")),
                ("Application deadline", posting.get("deadline_at")),
            ),
            department=str(department),
            remote_flag=remote,
            comp_min=comp_min,
            comp_max=comp_max,
            comp_text=comp_text,
            comp_source="board" if visible and (comp_text or comp_min or comp_max) else "",
            external_id=posting_id,
            source="pinpoint", raw=posting, **_base(cfg),
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
    for term_index, term in enumerate(terms, 1):
        offset, pages = 0, 0
        unique_count = 0
        term_seen: set[str] = set()
        page_fingerprints: set[tuple[str, ...]] = set()
        reported_total = None
        zero_total_with_rows = False
        while pages < pages_cap:
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term}
            status, text = fetch(api, method="POST", json_body=body,
                                 headers={"Content-Type": "application/json"})
            if status != 200:
                http.mark_partial(
                    f"workday term {term_index} listing page offset {offset} returned HTTP {status}")
                break
            try:
                data = json.loads(text)
            except Exception:
                http.mark_partial(
                    f"workday term {term_index} listing page offset {offset} returned unusable JSON")
                break
            if not isinstance(data, dict):
                http.mark_partial(
                    f"workday term {term_index} listing page offset {offset} had an unexpected shape")
                break
            page_total = _nonnegative_count(data.get("total"))
            if page_total is None:
                http.mark_partial(
                    f"workday term {term_index} listing page offset {offset} "
                    "had a missing or invalid total")
            posts = data.get("jobPostings")
            if not isinstance(posts, list):
                http.mark_partial(
                    f"workday term {term_index} listing page offset {offset} had invalid postings")
                break
            # Some live Workday tenants publish the total only on offset zero
            # and send 0 on every later page. Keep the strongest total seen;
            # replacing 157 with 0 made the loop stop healthy after 40 rows.
            if page_total is not None and page_total > 0:
                reported_total = max(reported_total or 0, page_total)
            elif page_total == 0 and reported_total is None:
                if posts:
                    zero_total_with_rows = True
                else:
                    reported_total = 0
            if not posts:
                if reported_total is not None and unique_count < reported_total:
                    http.mark_partial(
                        f"workday term {term_index} ended at {unique_count} unique "
                        f"postings of {reported_total}")
                break

            page_rows: list[tuple[str, dict]] = []
            page_ids: list[str] = []
            page_seen: set[str] = set()
            for j in posts:
                if not isinstance(j, dict):
                    http.mark_partial(
                        f"workday term {term_index} listing page offset {offset} "
                        "contained an invalid posting")
                    continue

                path = str(j.get("externalPath") or "").strip()
                bullets = j.get("bulletFields") or []
                if not isinstance(bullets, list):
                    http.mark_partial(
                        f"workday term {term_index} listing page offset {offset} "
                        "contained invalid bulletFields")
                    bullets = []
                external_id = str(bullets[0] or "").strip() if bullets else ""
                # Requisition id is stable even when a localized Workday path
                # changes; the path remains the fallback for boards that omit
                # bulletFields.
                identity = f"id:{external_id}" if external_id else (
                    f"path:{path}" if path else "")
                if not identity:
                    http.mark_partial(
                        f"workday term {term_index} listing page offset {offset} "
                        "contained a posting without an identity")
                    continue

                page_ids.append(identity)
                if identity in page_seen:
                    http.mark_partial(
                        f"workday term {term_index} listing page offset {offset} "
                        f"repeated identity {identity}")
                    continue
                page_seen.add(identity)
                page_rows.append((identity, j))

            fingerprint = tuple(page_ids)
            if fingerprint and fingerprint in page_fingerprints:
                http.mark_partial(
                    f"workday term {term_index} repeated a listing page at offset {offset}")
                break
            if fingerprint:
                page_fingerprints.add(fingerprint)

            new_on_page = 0
            for identity, j in page_rows:
                if identity in term_seen:
                    http.mark_partial(
                        f"workday term {term_index} listing page offset {offset} "
                        f"replayed identity {identity} from an earlier page")
                    continue
                term_seen.add(identity)
                unique_count += 1
                new_on_page += 1
                if identity in seen:
                    continue
                seen.add(identity)
                path = str(j.get("externalPath") or "").strip()
                bullets = j.get("bulletFields") or []
                external_id = str(bullets[0] or "").strip() if bullets else ""
                out.append(Job(
                    title=j.get("title", ""),
                    url=f"{host}/{site}{path}",
                    location=j.get("locationsText", "") or "",
                    posted_at=j.get("postedOn", "") or "",
                    external_id=external_id or path,
                    source="workday", raw=j, **_base(cfg),
                ))

            page_size = len(posts)
            if page_size and not new_on_page:
                http.mark_partial(
                    f"workday term {term_index} listing page offset {offset} "
                    "made no unique progress")
                break
            offset += page_size
            pages += 1
            if reported_total is not None and unique_count > reported_total:
                http.mark_partial(
                    f"workday term {term_index} returned {unique_count} unique "
                    f"postings above provider total {reported_total}")
                break
            if reported_total is not None and unique_count >= reported_total:
                break
            if page_size < 20:
                if reported_total is not None and unique_count < reported_total:
                    http.mark_partial(
                        f"workday term {term_index} returned a short page at "
                        f"{unique_count} unique postings of {reported_total}")
                break
        if pages >= pages_cap:
            if reported_total is None:
                http.mark_capped(
                    f"workday term {term_index} filled its {pages_cap * 20}-posting "
                    "budget without a provider total")
            elif unique_count < reported_total:
                http.mark_capped(
                    f"workday term {term_index} safety cap {pages_cap * 20} "
                    f"below provider total {reported_total}; fetched "
                    f"{unique_count} unique postings")
        elif zero_total_with_rows:
            http.mark_partial(
                f"workday term {term_index} reported total 0 for a nonempty page")
    # Detail fetch only for plausible titles - tenants can be huge.
    for job in out:
        if _looks_relevant(job.title):
            path = job.raw.get("externalPath", "")
            d = fetch_json(f"{host}/wday/cxs/{tenant}/{site}{path}")
            info = d.get("jobPostingInfo") if isinstance(d, dict) else None
            if not isinstance(info, dict):
                http.mark_partial(
                    f"workday detail {job.external_id or job.url} "
                    "had an unexpected shape")
                continue
            description = strip_html(info.get("jobDescription", ""))
            if description:
                job.description = description
            else:
                http.mark_partial(
                    f"workday detail {job.external_id or job.url} had no JD text")
            # remoteType is a label, not a boolean. Salesforce returns
            # 'Office - Flexible' and 'Office Tech-Flexible' for office-based
            # roles; treating it as truthy scored an onsite SF VP role remote.
            rt = str(info.get("remoteType") or "")
            job.remote_flag = bool(re.search(r"remote", rt, re.I)) if rt else None
            # The list view collapses multi-site reqs to "5 Locations"; the real
            # specific cities are only in the detail payload.
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
    seen_ids: set[str] = set()
    page_fingerprints: set[tuple[str, ...]] = set()
    unique_count = 0
    reported_total = None
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
            http.mark_partial(f"oracle_orc listing page offset {off} failed")
            break
        items = data.get("items")
        if not isinstance(items, list):
            http.mark_partial(
                f"oracle_orc listing page offset {off} had an unexpected shape")
            break
        has_more = data.get("hasMore")
        if has_more is not None and not isinstance(has_more, bool):
            http.mark_partial(
                f"oracle_orc listing page offset {off} had invalid hasMore")
            has_more = None
        page_size = 0
        page_rows: list[tuple[str, dict]] = []
        page_ids: list[str] = []
        page_seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                http.mark_partial(
                    f"oracle_orc listing page offset {off} contained an invalid wrapper")
                continue
            item_total = _nonnegative_count(item.get("TotalJobsCount"))
            if item_total is None:
                http.mark_partial(
                    f"oracle_orc listing page offset {off} had a missing or invalid total")
            else:
                reported_total = max(reported_total or 0, item_total)
            requisitions = item.get("requisitionList")
            if not isinstance(requisitions, list):
                http.mark_partial(
                    f"oracle_orc listing page offset {off} had invalid requisitions")
                continue
            if item_total == 0 and requisitions:
                http.mark_partial(
                    f"oracle_orc listing page offset {off} reported total 0 "
                    "for a nonempty page")
            page_size += len(requisitions)
            for j in requisitions:
                if not isinstance(j, dict):
                    http.mark_partial(
                        f"oracle_orc listing page offset {off} contained an invalid job")
                    continue
                identity = str(j.get("Id") or "").strip()
                if not identity:
                    http.mark_partial(
                        f"oracle_orc listing page offset {off} contained a job "
                        "without an identity")
                    continue
                page_ids.append(identity)
                if identity in page_seen:
                    http.mark_partial(
                        f"oracle_orc listing page offset {off} repeated identity {identity}")
                    continue
                page_seen.add(identity)
                page_rows.append((identity, j))

        fingerprint = tuple(page_ids)
        if fingerprint and fingerprint in page_fingerprints:
            http.mark_partial(
                f"oracle_orc repeated a listing page at offset {off}")
            break
        if fingerprint:
            page_fingerprints.add(fingerprint)

        new_on_page = 0
        for identity, j in page_rows:
            if identity in seen_ids:
                http.mark_partial(
                    f"oracle_orc listing page offset {off} replayed identity "
                    f"{identity} from an earlier page")
                continue
            seen_ids.add(identity)
            unique_count += 1
            new_on_page += 1
            out.append(Job(
                title=j.get("Title", ""),
                url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j.get('Id','')}",
                location=j.get("PrimaryLocation", "") or "",
                posted_at=(j.get("PostedDate") or "")[:10],
                description=strip_html(j.get("ShortDescriptionStr", "") or ""),
                external_id=identity,
                source="oracle_orc", raw=j, **_base(cfg),
            ))

        if page_size and not new_on_page:
            http.mark_partial(
                f"oracle_orc listing page offset {off} made no unique progress")
            break
        if has_more is True and (
                page_size < 200
                or (reported_total is not None and unique_count >= reported_total)):
            http.mark_partial(
                f"oracle_orc listing page offset {off} advertised hasMore=true "
                f"after {unique_count} unique postings")
            break
        if (has_more is False and reported_total is not None
                and unique_count < reported_total):
            http.mark_partial(
                f"oracle_orc advertised hasMore=false at {unique_count} of "
                f"{reported_total} unique postings")
            break
        if reported_total is not None and unique_count > reported_total:
            http.mark_partial(
                f"oracle_orc returned {unique_count} unique postings above "
                f"provider total {reported_total}")
            break
        if reported_total is not None and unique_count >= reported_total:
            break
        if page_size < 200:      # short page normally means the last page
            if reported_total is not None and unique_count < reported_total:
                http.mark_partial(
                    f"oracle_orc ended at {unique_count} of {reported_total} "
                    "postings (unique identities)")
            break
    else:
        http.mark_capped("oracle_orc reached its 2000-posting safety cap")

    # The list API frequently contains only a short summary (or an empty one),
    # while the public Candidate Experience detail resource carries the actual
    # responsibilities and qualifications used by the scorer. Spend those
    # requests only on plausible titles, as with other large enterprise boards.
    for job in out:
        if not _looks_relevant(job.title):
            continue
        detail = fetch_json(
            f"https://{host}/hcmRestApi/resources/latest/"
            f"recruitingCEJobRequisitionDetails/{job.external_id}?onlyData=true"
        )
        detail_fields = (
            "ExternalDescriptionStr", "ExternalResponsibilitiesStr",
            "ExternalQualificationsStr",
        )
        if (not isinstance(detail, dict)
                or not any(field in detail for field in detail_fields)):
            http.mark_partial(
                f"oracle_orc detail {job.external_id or job.url} "
                "had an unexpected shape")
            continue
        body = _sections(
            ("Description", detail.get("ExternalDescriptionStr")),
            ("Responsibilities", detail.get("ExternalResponsibilitiesStr")),
            ("Qualifications", detail.get("ExternalQualificationsStr")),
        )
        if body:
            job.description = body
        else:
            http.mark_partial(
                f"oracle_orc detail {job.external_id or job.url} had no JD text")
        location = str(detail.get("PrimaryLocation") or "").strip()
        if location:
            job.location = location
        workplace = str(detail.get("WorkplaceType") or "").strip()
        if workplace:
            job.remote_flag = bool(re.search(r"\bremote\b", workplace, re.I))
        posted = str(detail.get("ExternalPostedStartDate") or "")
        if posted:
            job.posted_at = posted[:10]
    return out


@adapter("eightfold")
def eightfold(cfg: dict) -> list[Job]:
    """Eightfold.ai career sites (Bayer, Vodafone, many F500)."""
    domain = cfg["domain"]
    host = cfg.get("host", "https://jobs.eightfold.ai")
    out = []
    seen_ids: set[str] = set()
    page_fingerprints: set[tuple[str, ...]] = set()
    prior_short_page = None
    for start in range(0, 1000, 100):   # was capped at 300
        data = fetch_json(
            f"{host}/api/apply/v2/jobs?domain={domain}&start={start}&num=100"
            f"&sort_by=timestamp&triggerGoButton=false"
        )
        if not isinstance(data, dict):
            http.mark_partial(f"eightfold listing page offset {start} failed")
            break
        positions = data.get("positions")
        if not isinstance(positions, list):
            http.mark_partial(
                f"eightfold listing page offset {start} had invalid positions")
            break
        if not positions:
            break
        if prior_short_page is not None:
            http.mark_partial(
                f"eightfold returned nonempty offset {start} after short page "
                f"at offset {prior_short_page}; fixed offsets may have skipped rows")
        prior_short_page = start if len(positions) < 100 else None

        page_ids: list[str] = []
        page_seen: set[str] = set()
        page_rows: list[tuple[str, Job]] = []
        for j in positions:
            if not isinstance(j, dict):
                http.mark_partial(
                    f"eightfold listing page offset {start} contained an invalid job")
                continue
            identity = str(j.get("id") or "").strip()
            if not identity:
                http.mark_partial(
                    f"eightfold listing page offset {start} contained a job "
                    "without an identity")
                continue
            page_ids.append(identity)
            if identity in page_seen:
                http.mark_partial(
                    f"eightfold listing page offset {start} repeated identity {identity}")
                continue
            page_seen.add(identity)
            locations = j.get("locations") or []
            if not isinstance(locations, list):
                http.mark_partial(
                    f"eightfold listing job {identity} had invalid locations")
                locations = []
            page_rows.append((identity, Job(
                title=j.get("name", ""),
                url=j.get("canonicalPositionUrl", "") or
                    f"{host}/careers/job/{j.get('id','')}",
                location=j.get("location", "") or ", ".join(locations),
                description=strip_html(j.get("job_description", "")),
                posted_at=str(j.get("t_create", ""))[:10],
                department=j.get("department", "") or "",
                external_id=identity,
                source="eightfold", raw=j, **_base(cfg),
            )))

        fingerprint = tuple(page_ids)
        if fingerprint and fingerprint in page_fingerprints:
            http.mark_partial(f"eightfold repeated a listing page at offset {start}")
            break
        if fingerprint:
            page_fingerprints.add(fingerprint)

        new_on_page = 0
        for identity, job in page_rows:
            if identity in seen_ids:
                http.mark_partial(
                    f"eightfold listing page offset {start} replayed identity "
                    f"{identity} from an earlier page")
                continue
            seen_ids.add(identity)
            new_on_page += 1
            out.append(job)
        if positions and not new_on_page:
            http.mark_partial(
                f"eightfold listing page offset {start} made no unique progress")
            break
    else:
        http.mark_capped("eightfold reached its 1000-posting safety cap")
    return out


@adapter("phenom")
def phenom(cfg: dict) -> list[Job]:
    """Phenom People career sites - very common at large US enterprises."""
    host = cfg["host"].rstrip("/")
    out = []
    seen: set[str] = set()
    page_fingerprints: set[tuple[str, ...]] = set()
    reported_total = None
    html_fallback = False
    for page in range(20):   # was capped at 150 postings
        data = None
        if not html_fallback:
            data = fetch_json(
                f"{host}/widgets?ddoKey=refineSearch&sortBy=&subsearch=&from={page*50}&size=50"
                f"&jobs=true&counts=true&all_fields=true"
            )
            html_fallback = not isinstance(data, dict)
        if html_fallback:
            status, text = fetch(f"{host}/search-results?from={page*10}")
            if status != 200:
                http.mark_partial(
                    f"phenom fallback listing page {page + 1} returned HTTP {status}")
                break
            match = re.search(
                r'phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.experimentData', text, re.S
            )
            try:
                data = json.loads(match.group(1)) if match else None
            except (TypeError, ValueError, json.JSONDecodeError):
                data = None
        if not isinstance(data, dict):
            http.mark_partial(f"phenom listing page {page + 1} was unusable")
            break
        if isinstance(data.get("refineSearch"), dict):
            block = data["refineSearch"]
        elif isinstance(data.get("eagerLoadRefineSearch"), dict):
            block = data["eagerLoadRefineSearch"]
        else:
            http.mark_partial(
                f"phenom listing page {page + 1} had an unexpected envelope")
            break
        block_data = block.get("data")
        if not isinstance(block_data, dict) or not isinstance(block_data.get("jobs"), list):
            http.mark_partial(
                f"phenom listing page {page + 1} had invalid jobs")
            break
        ref = block_data["jobs"]
        page_total = _nonnegative_count(block.get("totalHits"))
        if page_total is None:
            http.mark_partial(
                f"phenom listing page {page + 1} had a missing or invalid totalHits")
        else:
            reported_total = max(reported_total or 0, page_total)
        if not ref:
            if reported_total is not None and len(seen) < reported_total:
                http.mark_partial(
                    f"phenom listing ended at {len(seen)} of {reported_total} "
                    "unique postings")
            break
        if page_total == 0:
            http.mark_partial(
                f"phenom listing page {page + 1} reported totalHits 0 "
                "for a nonempty page")

        page_ids: list[str] = []
        page_seen: set[str] = set()
        page_rows: list[tuple[str, Job]] = []
        for j in ref:
            if not isinstance(j, dict):
                http.mark_partial(
                    f"phenom listing page {page + 1} contained an invalid job")
                continue
            external_id = str(j.get("jobId") or j.get("jobSeqNo") or "")
            if not external_id:
                http.mark_partial(
                    f"phenom listing page {page + 1} contained a job "
                    "without an identity")
                continue
            page_ids.append(external_id)
            if external_id in page_seen:
                http.mark_partial(
                    f"phenom listing page {page + 1} repeated identity {external_id}")
                continue
            page_seen.add(external_id)
            page_rows.append((external_id, Job(
                title=j.get("title", ""),
                url=j.get("applyUrl", "") or f"{host}/job/{j.get('jobSeqNo','')}",
                location=", ".join(x for x in [j.get("cityStateCountry") or j.get("location")] if x),
                description=strip_html(j.get("descriptionTeaser", "")),
                posted_at=(j.get("postedDate") or "")[:10],
                department=j.get("category", "") or "",
                external_id=external_id,
                source="phenom", raw=j, **_base(cfg),
            )))

        fingerprint = tuple(page_ids)
        if fingerprint and fingerprint in page_fingerprints:
            http.mark_partial(f"phenom repeated listing page {page + 1}")
            break
        if fingerprint:
            page_fingerprints.add(fingerprint)

        added = 0
        for external_id, job in page_rows:
            if external_id in seen:
                http.mark_partial(
                    f"phenom listing page {page + 1} replayed identity "
                    f"{external_id} from an earlier page")
                continue
            seen.add(external_id)
            added += 1
            out.append(job)
        if ref and not added:
            http.mark_partial(
                f"phenom listing page {page + 1} made no unique progress")
            break
        if reported_total is not None and len(seen) > reported_total:
            http.mark_partial(
                f"phenom returned {len(seen)} unique postings above "
                f"totalHits {reported_total}")
            break
        if reported_total is not None and len(seen) >= reported_total:
            break
    else:
        http.mark_capped("phenom reached its 20-page safety cap")
    return out


_ICIMS_CARD = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.S)
_ICIMS_LINK = re.compile(r'<a href="([^"]*?/jobs/(\d+)/[^"]*?)"[^>]*class="iCIMS_Anchor"', re.S)
_ICIMS_TITLE = re.compile(r'<h3[^>]*>\s*(.*?)\s*</h3>', re.S)
_ICIMS_LOC = re.compile(r'Job Locations?</span>\s*<span[^>]*>\s*(.*?)\s*</span>', re.S)
_ICIMS_DESC = re.compile(r'class="col-xs-12 description">\s*(.*?)\s*</div>', re.S)
_ICIMS_BLOCK_PAGE = re.compile(
    r"\b(?:sign in|log in|login|access denied|authentication required|captcha|"
    r"verify (?:that )?you are human|just a moment|checking your browser|"
    r"enable javascript|cloudflare)\b",
    re.I,
)
_ICIMS_EMPTY_PAGE = re.compile(
    r"\biCIMS_(?:JobSearchNoResults|NoResults|EmptyResults)\b", re.I)
_ICIMS_EMPTY_TEXT = re.compile(
    r"\b(?:no (?:jobs?|results|positions)|"
    r"there are (?:currently )?no open positions)\b",
    re.I,
)


def _icims_proven_empty(text: str) -> bool:
    return bool(_ICIMS_EMPTY_PAGE.search(text)
                or (re.search(r"\biCIMS_", text, re.I)
                    and _ICIMS_EMPTY_TEXT.search(strip_html(text))))


@adapter("icims")
def icims(cfg: dict) -> list[Job]:
    """iCIMS has no public JSON, but its search page is server-rendered into
    job cards that already carry title, location and a description snippet, so
    no per-job detail request is needed for screening."""
    slug = cfg["slug"]
    base = cfg.get("host") or f"https://careers-{slug}.icims.com"
    try:
        # Piedmont had 1,777 live rows on 2026-08-20. The old fixed 30-page
        # budget stopped at 1,500 even though six more pages were available.
        # Fifty remains bounded, while large boards can opt up to 100 and any
        # exhausted budget is surfaced as capped rather than called healthy.
        pages_cap = max(1, min(int(cfg.get("pages", 50)), 100))
    except (TypeError, ValueError):
        raise ValueError("icims pages must be an integer")
    out, seen = [], set()
    page_fingerprints: set[tuple[str, ...]] = set()
    for page in range(pages_cap):
        status, text = fetch(f"{base}/jobs/search?ss=1&in_iframe=1&pr={page}")
        if status != 200 or not text:
            http.mark_partial(
                f"icims listing page {page + 1} returned HTTP {status or 0}")
            break
        cards = _ICIMS_CARD.findall(text)
        if not cards:
            if _ICIMS_BLOCK_PAGE.search(strip_html(text)):
                http.mark_partial(
                    f"icims listing page {page + 1} returned a login/challenge page")
            elif not _icims_proven_empty(text):
                http.mark_partial(
                    f"icims listing page {page + 1} had no jobs or proven empty marker")
            break

        page_ids: list[str] = []
        page_seen: set[str] = set()
        page_rows: list[tuple[str, Job]] = []
        for card in cards:
            link = _ICIMS_LINK.search(card)
            title = _ICIMS_TITLE.search(card)
            if not (link and title):
                http.mark_partial(
                    f"icims listing page {page + 1} contained an invalid job card")
                continue
            jid = link.group(2)
            page_ids.append(jid)
            if jid in page_seen:
                http.mark_partial(
                    f"icims listing page {page + 1} repeated identity {jid}")
                continue
            page_seen.add(jid)
            loc = _ICIMS_LOC.search(card)
            desc = _ICIMS_DESC.search(card)
            page_rows.append((jid, Job(
                title=strip_html(title.group(1)),
                url=link.group(1).split("?")[0],
                location=strip_html(loc.group(1)).replace("US-", "").replace("-", ", ")
                         if loc else "",
                description=strip_html(desc.group(1)) if desc else "",
                external_id=jid, source="icims", raw={}, **_base(cfg),
            )))

        fingerprint = tuple(page_ids)
        if fingerprint and fingerprint in page_fingerprints:
            http.mark_partial(f"icims repeated listing page {page + 1}")
            break
        if fingerprint:
            page_fingerprints.add(fingerprint)

        added = 0
        for jid, job in page_rows:
            if jid in seen:
                http.mark_partial(
                    f"icims listing page {page + 1} replayed identity {jid} "
                    "from an earlier page")
                continue
            seen.add(jid)
            added += 1
            out.append(job)
        if cards and not added:
            http.mark_partial(
                f"icims listing page {page + 1} made no unique progress")
            break
    else:
        http.mark_capped(
            f"icims reached its {pages_cap}-page / {pages_cap * 50}-posting safety cap")
    # Full body only where the title is plausible.
    for job in out:
        if _looks_relevant(job.title):
            st, tx = fetch(job.url + "?in_iframe=1")
            if st == 200:
                body = re.search(r'<div[^>]*class="[^"]*iCIMS_JobContent[^"]*"(.*?)(?:</body>|iCIMS_JobFooter)',
                                 tx, re.S)
                if body:
                    job.description = strip_html(body.group(1))[:12000]
                else:
                    http.mark_partial(
                        f"icims detail {job.external_id or job.url} was unusable")
            else:
                http.mark_partial(
                    f"icims detail {job.external_id or job.url} returned HTTP {st}")
    return out


_JOBVITE_SEARCH_PAGE = re.compile(r"\bjv-page-search\b", re.I)
_JOBVITE_DETAIL_PAGE = re.compile(r"\bjv-page-job\b", re.I)
_JOBVITE_DETAIL_BODY = re.compile(
    r'<div[^>]*class=["\'][^"\']*\bjv-job-detail-description\b[^"\']*["\'][^>]*>'
    r'(.*?)(?=<div[^>]*class=["\'][^"\']*\bjv-job-detail-bottom-actions\b|</article>)',
    re.S | re.I,
)
_JOBVITE_PAGE_COUNT = re.compile(
    r'class=["\'][^"\']*\bjv-pagination-text\b[^"\']*["\'][^>]*>'
    r'\s*(\d+)\s*-\s*(\d+)\s+of\s+(\d+)\b',
    re.S | re.I,
)
_JOBVITE_EMPTY_COUNT = re.compile(
    r'class=["\'][^"\']*\bjv-pagination-text\b[^"\']*["\'][^>]*>'
    r'\s*(?:0\s*-\s*0|0)\s+of\s+0\b',
    re.S | re.I,
)


def _jobvite_proven_empty(text: str) -> bool:
    """Recognize Jobvite's own zero-result pagination, not generic prose."""
    return bool(_JOBVITE_SEARCH_PAGE.search(text)
                and _JOBVITE_EMPTY_COUNT.search(text))


@adapter("jobvite")
def jobvite(cfg: dict) -> list[Job]:
    """Jobvite embeds its job list as JSON inside the careers page."""
    slug = cfg["slug"]
    status, text = fetch(f"https://jobs.jobvite.com/{slug}/search")
    if status != 200:
        return []
    matches = list(re.finditer(
        r'href=(["\'])(/' + re.escape(slug)
        + r'/job/([A-Za-z0-9]+))\1[^>]*>\s*(?:<[^>]+>\s*)*([^<]{3,120})',
        text, re.I,
    ))
    candidate_links = len(re.findall(
        r'href=["\']/' + re.escape(slug) + r'/job/[A-Za-z0-9]+', text, re.I))
    if not matches:
        if not _jobvite_proven_empty(text):
            http.mark_partial(
                "jobvite search page had no recognized rows or proven empty marker")
        return []
    if candidate_links > len(matches):
        http.mark_partial(
            f"jobvite search page could parse {len(matches)} of "
            f"{candidate_links} candidate job links")

    out = []
    for m in matches:
        out.append(Job(
            title=strip_html(m.group(4)),
            url=f"https://jobs.jobvite.com{m.group(2)}",
            external_id=m.group(3), source="jobvite", raw={}, **_base(cfg),
        ))
    seen, uniq = set(), []
    for j in out:
        if j.external_id in seen:
            continue
        seen.add(j.external_id)
        uniq.append(j)
    page_count = _JOBVITE_PAGE_COUNT.search(text)
    if page_count:
        first, last, total = (int(value) for value in page_count.groups())
        advertised_page_size = last - first + 1
        if first < 1 or last < first or total < last:
            http.mark_partial("jobvite pagination text was internally inconsistent")
        elif len(uniq) != advertised_page_size:
            http.mark_partial(
                f"jobvite parsed {len(uniq)} of {advertised_page_size} "
                "advertised rows on the current page")
        if len(uniq) < total:
            http.mark_capped(
                f"jobvite parsed {len(uniq)} of {total} advertised postings; "
                "additional pages were not fetched")
    for job in uniq:
        if _looks_relevant(job.title):
            st, tx = fetch(job.url)
            if st == 200:
                detail = _JOBVITE_DETAIL_BODY.search(tx)
                description = strip_html(detail.group(1)) if detail else ""
                if not (_JOBVITE_DETAIL_PAGE.search(tx) and description):
                    http.mark_partial(
                        f"jobvite detail {job.external_id or job.url} was unusable")
                    continue
                job.description = description[:9000]
                loc = re.search(r'jv-job-detail-meta[^>]*>(.*?)</div>', tx, re.S)
                if loc:
                    job.location = strip_html(loc.group(1))[:120]
            else:
                http.mark_partial(
                    f"jobvite detail {job.external_id or job.url} returned HTTP {st}")
    return uniq


_HRM_ROW = re.compile(r'<tr[^>]*\bdata-req-id=["\']([^"\']+)["\'][^>]*>(.*?)</tr>',
                      re.S | re.I)
_HRM_LINK = re.compile(r'href=["\']([^"\']*job-opening\.php\?[^"\']+)["\']', re.I)
_HRM_TITLE = re.compile(r'<td[^>]*\bid=["\']posTitle[^"\']*["\'][^>]*>\s*'
                        r'(?:<a[^>]*>)?\s*(.*?)(?:</a>)?\s*</td>', re.S | re.I)
_HRM_DEPT = re.compile(r'<td[^>]*\bid=["\']departments[^"\']*["\'][^>]*>(.*?)</td>',
                       re.S | re.I)
_HRM_DATE = re.compile(r'</td>\s*<td[^>]*class=["\'][^"\']*reqitem[^"\']*["\'][^>]*>'
                       r'\s*(\d{1,2}/\d{1,2}/\d{4})', re.S | re.I)
_HRM_TEMPLATE_MARKER = re.compile(
    r"current job opportunities are posted here as they become available", re.I)
_HRM_EMPTY_MARKER = re.compile(
    r"\b(?:there are\s+)?(?:currently\s+)?no\s+(?:open\s+)?"
    r"(?:jobs?|positions?|job openings?|openings?)"
    r"(?:\s+(?:are\s+)?(?:currently\s+)?available(?:\s+at\s+this\s+time)?"
    r"|(?=[.!?]|$))",
    re.I,
)


def _hrmdirect_proven_empty(text: str) -> bool:
    """Require the vendor template plus an explicit zero-openings statement."""
    plain = strip_html(text)
    return bool(_HRM_TEMPLATE_MARKER.search(plain)
                and _HRM_EMPTY_MARKER.search(plain))


def _hrm_date(value: str) -> str:
    try:
        from datetime import datetime
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except (TypeError, ValueError):
        return ""


@adapter("hrmdirect")
def hrmdirect(cfg: dict) -> list[Job]:
    """HRMDirect / ClearCompany legacy career pages.

    Some nonprofits still publish a server-rendered list here. The list carries
    stable requisition ids, titles, departments and dates; plausible titles get
    a detail fetch for location, compensation and requirements before scoring.
    """
    slug = cfg["slug"]
    base = (cfg.get("host") or f"https://{slug}.hrmdirect.com/employment/").rstrip("/") + "/"
    status, text = fetch(urljoin(base, "job-openings.php?search=true"))
    if status != 200:
        return []
    rows = _HRM_ROW.findall(text)
    if not rows:
        if not _hrmdirect_proven_empty(text):
            http.mark_partial(
                "hrmdirect listing page had no recognized rows or proven empty marker")
        return []
    out = []
    for req, row in rows:
        link, title = _HRM_LINK.search(row), _HRM_TITLE.search(row)
        if not (link and title):
            http.mark_partial(
                f"hrmdirect requisition {req or '?'} had an unexpected row shape")
            continue
        dept, posted = _HRM_DEPT.search(row), _HRM_DATE.search(row)
        out.append(Job(
            title=strip_html(title.group(1)),
            url=urljoin(base, link.group(1).replace("&amp;", "&")),
            department=strip_html(dept.group(1)) if dept else "",
            posted_at=_hrm_date(posted.group(1)) if posted else "",
            external_id=req, source="hrmdirect", raw={}, **_base(cfg),
        ))
    for job in out:
        if not _looks_relevant(job.title):
            continue
        st, tx = fetch(job.url)
        if st != 200:
            http.mark_partial(
                f"hrmdirect detail {job.external_id or job.url} returned HTTP {st}")
            continue
        loc = re.search(r'Location:</b>\s*</td>\s*<td[^>]*class=["\']viewFieldValue["\'][^>]*>'
                        r'(.*?)</td>', tx, re.S | re.I)
        desc = re.search(r'<div[^>]*class=["\']jobDesc["\'][^>]*>(.*?)</div>',
                         tx, re.S | re.I)
        if loc:
            job.location = strip_html(loc.group(1))
        if desc:
            job.description = strip_html(desc.group(1))[:20000]
        else:
            http.mark_partial(
                f"hrmdirect detail {job.external_id or job.url} was unusable")
    return out


@adapter("paylocity")
def paylocity(cfg: dict) -> list[Job]:
    """Paylocity Recruiting - common at US mid-market and nonprofits."""
    guid = cfg["guid"]
    # The former /recruiting/v2/api/jobs endpoint now routes to JobNotFound
    # HTML with HTTP 200.  The public All page contains the board's real JSON
    # model in window.pageData; use that stable requisition list instead.
    status, text = fetch(f"https://recruiting.paylocity.com/recruiting/jobs/All/{guid}")
    if status != 200:
        return []
    match = re.search(r'window\.pageData\s*=\s*(\{.*?\})\s*;', text, re.S)
    try:
        data = json.loads(match.group(1)) if match else None
    except (TypeError, ValueError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        http.mark_partial("paylocity listing pageData was unusable")
        return []
    jobs = data.get("Jobs")
    if not isinstance(jobs, list):
        http.mark_partial("paylocity listing pageData did not contain Jobs as a list")
        return []
    out = []
    for index, j in enumerate(jobs, 1):
        if not isinstance(j, dict):
            http.mark_partial(
                f"paylocity listing row {index} was not an object")
            continue
        required = _required_listing_fields(
            "paylocity", index, j.get("JobId"), j.get("JobTitle"),
            url_builder=lambda identity:
                "https://recruiting.paylocity.com/recruiting/jobs/Details/"
                + quote(identity, safe=""),
        )
        if required is None:
            continue
        jid, title, direct_url = required
        loc = j.get("JobLocation") or {}
        if not isinstance(loc, dict):
            http.mark_partial("paylocity listing job had an invalid JobLocation")
            loc = {}
        out.append(Job(
            title=title,
            url=direct_url,
            location=", ".join(x for x in [loc.get("City"), loc.get("State"),
                                            loc.get("Country")] if x)
                     or j.get("LocationName", ""),
            description=strip_html(j.get("Description", "")),
            posted_at=(j.get("PublishedDate") or "")[:10],
            remote_flag=bool(j.get("IsRemote")),
            external_id=jid,
            source="paylocity", raw=j, **_base(cfg),
        ))
    # Current list records intentionally omit the full description.  Fetch it
    # only for in-family titles, matching the traffic policy of other large
    # HTML boards.
    for job in out:
        if job.description or not _looks_relevant(job.title):
            continue
        st, tx = fetch(job.url)
        if st != 200:
            http.mark_partial(
                f"paylocity detail {job.external_id or job.url} returned HTTP {st}")
            continue
        body = re.search(
            r'<div[^>]*class=["\']job-listing-header["\'][^>]*>\s*Description\s*</div>'
            r'\s*<div>(.*?)</div>', tx, re.S | re.I
        )
        if body:
            job.description = strip_html(body.group(1))[:20000]
        else:
            http.mark_partial(
                f"paylocity detail {job.external_id or job.url} was unusable")
    return out


_NEOGOV = "{http://www.neogov.com/namespaces/JobListing}"


def _rss_date(value: str) -> str:
    # NEOGOV's extended fields sometimes append a non-RFC fractional marker
    # (`05:00:00:0`). The ordinary RSS pubDate is RFC-compliant, but prefer the
    # actual advertise-from date when this harmless suffix is present.
    value = re.sub(r"(?<=:\d{2}):\d$", "", str(value or "").strip())
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


@adapter("neogov")
def neogov(cfg: dict) -> list[Job]:
    """NEOGOV/GovernmentJobs' official public per-agency RSS feed."""
    slug = cfg["slug"]
    status, text = fetch(
        "https://www.governmentjobs.com/SearchEngine/JobsFeed?agency="
        + quote(slug, safe="")
    )
    if status != 200:
        return []
    payload = text.lstrip("\ufeff")
    # GovernmentJobs currently sends a UTF-8 BOM that some HTTP stacks decode
    # as the three visible Latin-1 characters below. Accept both representations
    # before handing the otherwise valid RSS to ElementTree.
    if payload.startswith("ï»¿"):
        payload = payload[3:]
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError):
        http.mark_partial("neogov JobsFeed returned unusable XML")
        return []
    channel = root.find("channel")
    if root.tag.casefold() != "rss" or channel is None:
        http.mark_partial("neogov JobsFeed had an unexpected RSS shape")
        return []
    agency_name = str(channel.findtext("title", default="") or "").strip()

    def plain(item: ET.Element, tag: str) -> str:
        return str(item.findtext(tag, default="") or "").strip()

    def ng(item: ET.Element, tag: str) -> str:
        return plain(item, _NEOGOV + tag)

    out = []
    for index, item in enumerate(channel.findall("item"), 1):
        guid = plain(item, "guid")
        link = plain(item, "link") or guid
        external_id = ng(item, "jobId")
        if not external_id:
            match = re.search(r"/jobs/(\d+)", link)
            external_id = match.group(1) if match else ""
        required = _required_listing_fields(
            "neogov", index, external_id, plain(item, "title"), link, guid,
            url_builder=lambda identity:
                "https://www.governmentjobs.com/careers/"
                f"{quote(slug, safe='')}/jobs/{quote(identity, safe='')}",
        )
        if required is None:
            continue
        external_id, title, direct_url = required

        categories = [str(node.text or "").strip() for node in
                      item.findall(f"{_NEOGOV}categories/{_NEOGOV}category")
                      if str(node.text or "").strip()]
        department = ng(item, "department") or ", ".join(categories)
        currency = ng(item, "salaryCurrency").upper()
        interval = ng(item, "salaryInterval").casefold()
        raw_min, raw_max = ng(item, "minimumSalary"), ng(item, "maximumSalary")
        annual_usd = currency == "USD" and interval in {"year", "annual", "annually"}
        comp_min = _positive_int(raw_min) if annual_usd else None
        comp_max = _positive_int(raw_max) if annual_usd else None
        shown = " - ".join(value for value in (raw_min, raw_max) if value)
        comp_text = (" ".join(value for value in (currency, shown) if value)
                     + (f" / {ng(item, 'salaryInterval')}" if interval else ""))
        comp_text = comp_text.strip()
        advertise_from = ng(item, "advertiseFromDateUTC") or ng(
            item, "advertiseFromDate")
        advertise_to = ng(item, "advertiseToDateTimeUTC") or ng(
            item, "advertiseToDateTime")
        pub_date = plain(item, "pubDate")

        base = _base(cfg)
        if agency_name:
            # The channel title is the provider's authoritative agency name;
            # URL ingest can only guess a slug such as "Fulton".
            base["company"] = agency_name
        out.append(Job(
            title=title,
            url=direct_url,
            location=ng(item, "location"),
            description=_sections(
                ("", plain(item, "description")),
                ("Examples of duties", ng(item, "examplesofduties")),
                ("Qualifications", ng(item, "qualifications")),
                ("Supplemental information", ng(item, "supplementalinformation")),
                ("Application deadline", _rss_date(advertise_to)),
            ),
            posted_at=_rss_date(advertise_from) or _rss_date(pub_date),
            department=department,
            comp_min=comp_min,
            comp_max=comp_max,
            comp_text=comp_text,
            comp_source="board" if comp_text else "",
            external_id=external_id,
            source="neogov", raw={
                "pubDate": pub_date,
                "advertiseFromDate": advertise_from,
                "advertiseToDateTime": advertise_to,
                "salaryCurrency": currency,
                "salaryInterval": ng(item, "salaryInterval"),
            }, **base,
        ))
    return out


@adapter("personio")
def personio(cfg: dict) -> list[Job]:
    slug = cfg["slug"]
    status, text = fetch(f"https://{slug}.jobs.personio.com/xml")
    if status != 200:
        return []
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        http.mark_partial("personio listing response returned unusable XML")
        return []
    root_name = str(root.tag).rsplit("}", 1)[-1].casefold()
    if root_name != "workzag-jobs":
        http.mark_partial(
            "personio listing response did not have a workzag-jobs root")
        return []

    out = []
    positions = [node for node in root
                 if str(node.tag).rsplit("}", 1)[-1].casefold() == "position"]
    for index, position in enumerate(positions, 1):
        def g(tag):
            node = next((child for child in position
                         if str(child.tag).rsplit("}", 1)[-1].casefold()
                         == tag.casefold()), None)
            return strip_html("".join(node.itertext())) if node is not None else ""
        required = _required_listing_fields(
            "personio", index, g("id"), g("name"),
            url_builder=lambda identity:
                f"https://{slug}.jobs.personio.com/job/{quote(identity, safe='')}",
        )
        if required is None:
            continue
        external_id, title, direct_url = required
        out.append(Job(
            title=title, url=direct_url,
            location=g("office"), description=g("jobDescriptions"),
            department=g("department"), external_id=external_id,
            source="personio", raw={}, **_base(cfg),
        ))
    return out


# --------------------------------------------------------------------------

# Set from the user's own profile by set_relevance_terms(). It starts as None on
# purpose: until somebody says what they are looking for, every posting is worth
# a detail request.
#
# This was a hardcoded regex of one person's job search
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
    "smartrecruiters": 5000, "workable": 100,
}


def at_page_ceiling(ats: str, n: int) -> bool:
    c = PAGE_CEILING.get(ats)
    return bool(c and n >= c)


def run_adapter(cfg: dict) -> tuple[list[Job], str | None]:
    """Dispatch one registry entry. Returns (jobs, error_or_None)."""
    http.reset_status()          # never inherit the previous source's state
    ats = cfg.get("ats")
    fn = REGISTRY.get(ats)
    if not fn:
        return [], f"no adapter for ats={ats!r}"
    try:
        jobs = fn(cfg)
    except Exception as e:  # a single bad board must never kill the run
        return [], f"{type(e).__name__}: {e}"
    if jobs and at_page_ceiling(str(ats or ""), len(jobs)):
        http.mark_capped(
            f"{ats} returned {len(jobs)} postings at its known hard ceiling")
    integrity_error = http.source_integrity_error()
    if integrity_error:
        return jobs, integrity_error
    if not jobs:
        # An empty list means one of two very different things. Report which.
        st = http.last_status()
        if st == 200:
            if http.last_parse_ok() is False:
                # 200 with a body we could not parse: a challenge page, an SSO
                # redirect, or the API changed shape. Reporting this as an empty
                # board is how an employer leaves coverage without a word.
                return [], "HTTP 200 but the response was not usable JSON"
            return [], None                       # board is fine, nothing open
        if st is None:
            return [], "no response (network/DNS/timeout)"
        return [], f"HTTP {st}"
    return jobs, None
