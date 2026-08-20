"""Deterministic Freehire discovery-feed contract tests.

Every provider response is a local fixture or in-memory stub. Nothing in this
module may contact Freehire, an employer ATS, or a private CareerKit instance.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from engine import aggregators, http, score as score_engine
from engine.models import Job


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _rich_detail(slug: str, *, source="greenhouse", url=None):
    detail = _fixture("freehire_detail_greenhouse.json")
    row = detail["data"]
    suffix = slug.rsplit("-", 1)[-1]
    row["public_slug"] = slug
    row["external_id"] = f"gh-{suffix}"
    row["source"] = source
    row["url"] = url or f"https://job-boards.greenhouse.io/acme/jobs/{suffix}"
    row["title"] = "Salesforce Consultant"
    row["company"] = "Acme"
    return detail


def test_freehire_is_dormant_without_an_explicit_true_and_makes_no_request(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("an inactive Freehire feed made a network request")

    monkeypatch.setattr(aggregators, "fetch_json", forbidden)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {})

    assert jobs == []
    assert error and "explicit opt-in" in error
    assert aggregators.freehire({"active": False}) == []

    monkeypatch.setattr(aggregators, "TERMS", ())
    jobs, error = aggregators.run_feed("freehire", {"active": True})
    assert jobs == []
    assert error and "no search_terms" in error


def test_freehire_pages_previews_then_hydrates_only_allowlisted_title_matches(monkeypatch):
    page1 = _fixture("freehire_search_page1.json")
    page2 = _fixture("freehire_search_page2.json")
    details = {
        "salesforce-consultant-acme-gh1": _fixture("freehire_detail_greenhouse.json"),
        "salesforce-consultant-example-wd1": _fixture("freehire_detail_workday.json"),
    }
    calls = []

    def fake_fetch_json(url, **kwargs):
        calls.append((url, kwargs))
        parsed = urlsplit(url)
        if parsed.path == "/api/v1/jobs/search":
            offset = int(parse_qs(parsed.query)["offset"][0])
            return page1 if offset == 0 else page2
        return details[parsed.path.rsplit("/", 1)[-1]]

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {
        "active": True,
        "pages": 4,
        "results_per_page": 3,
        "detail_cap": 10,
        "posted_within_days": 30,
        "countries": ["US", "ca", "us"],
    })

    assert error is None
    assert len(jobs) == 2
    search_calls = [call for call in calls if "/jobs/search?" in call[0]]
    detail_calls = [call for call in calls if "/jobs/search?" not in call[0]]
    assert len(search_calls) == 2
    assert len(detail_calls) == 2, "aggregator and irrelevant previews were hydrated"
    assert all(call[1] == {"safe_external": True} for call in calls)

    first_query = parse_qs(urlsplit(search_calls[0][0]).query)
    assert first_query == {
        "q": ['"salesforce consultant"'],
        "limit": ["3"],
        "offset": ["0"],
        "posted_within_days": ["30"],
        "countries": ["us,ca"],
    }
    assert parse_qs(urlsplit(search_calls[1][0]).query)["offset"] == ["3"]
    assert all("include_description" not in call[0] for call in search_calls)

    greenhouse, workday = jobs
    assert greenhouse.source == "freehire:greenhouse"
    assert greenhouse.external_id == "salesforce-consultant-acme-gh1"
    assert greenhouse.url == greenhouse.url_direct
    assert greenhouse.url.startswith("https://job-boards.greenhouse.io/")
    assert greenhouse.posted_at == "2026-08-19"
    assert greenhouse.remote_flag is None
    assert greenhouse.raw["work_mode"] == "remote"
    assert greenhouse.department == ""
    assert greenhouse.raw["enrichment"]["category"] == "consulting"
    assert greenhouse.comp_min is None and greenhouse.comp_max is None
    assert greenhouse.comp_source == ""
    assert "model-derived pay metadata" in greenhouse.comp_text
    assert "values omitted from scoring and storage" in greenhouse.comp_text
    assert "not employer-published" in greenhouse.comp_text
    assert greenhouse.raw["enrichment"]["salary_min"] == 135000
    assert greenhouse.raw["enrichment"]["salary_max"] == 175000
    assert greenhouse.raw["upstream_source"] == "greenhouse"
    assert greenhouse.raw["upstream_external_id"] == "acme-41001"
    assert greenhouse.raw["public_slug"] == greenhouse.external_id
    assert greenhouse.raw["matched_terms"] == ["salesforce consultant"]

    assert workday.source == "freehire:workday"
    assert workday.remote_flag is None, "derived hybrid must not become a scoring field"
    assert workday.raw["work_mode"] == "hybrid"
    assert workday.comp_min is None and workday.comp_max is None
    assert not any(char.isdigit() for char in workday.comp_text)
    assert workday.raw["enrichment"]["salary_currency"] == "EUR"
    assert workday.raw["closed_at"] is None

    native = Job(
        company=greenhouse.company,
        title=greenhouse.title,
        url=greenhouse.url,
        source="greenhouse",
        external_id="acme-41001",
    )
    assert greenhouse.group_key == native.group_key
    assert greenhouse.uid != native.uid, "provenance-specific sightings must remain auditable"


def test_freehire_reports_both_preview_and_detail_caps(monkeypatch):
    previews = []
    for index in range(2):
        previews.append({
            "public_slug": f"salesforce-consultant-{index}",
            "source": "greenhouse",
            "external_id": f"gh-{index}",
            "url": f"https://job-boards.greenhouse.io/acme/jobs/{index}",
            "title": "Salesforce Consultant",
            "company": "Acme",
            "description": "preview",
            "posted_at": "2026-08-19T00:00:00Z",
            "enrichment": {},
        })
    calls = []

    def fake_fetch_json(url, **_kwargs):
        calls.append(url)
        if "/jobs/search?" in url:
            return {"data": previews, "meta": {"total": 9, "limit": 2, "offset": 0}}
        return _rich_detail(url.rsplit("/", 1)[-1])

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {
        "active": True, "pages": 1, "results_per_page": 2, "detail_cap": 1,
    })

    assert len(jobs) == 1
    assert error and error.startswith("capped:")
    assert "fetched 2 of 9" in error
    assert "matched 2 unique jobs" in error
    assert sum("/api/v1/jobs/" in call and "/search?" not in call for call in calls) == 1


def test_freehire_429_is_a_coverage_gap_not_a_clean_zero(monkeypatch):
    def rate_limited(_url, **_kwargs):
        http._local.last_status = 429
        return None

    monkeypatch.setattr(aggregators, "fetch_json", rate_limited)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {"active": True})

    assert jobs == []
    assert error and error.startswith("partial:")
    assert "HTTP 429" in error


def test_freehire_malformed_detail_retains_preview_but_marks_it_degraded(monkeypatch):
    preview = _fixture("freehire_search_page1.json")["data"][0]

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" in url:
            return {"data": [preview, None],
                    "meta": {"total": 2, "limit": 100, "offset": 0}}
        return {"data": "not-a-job"}

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {
        "active": True, "results_per_page": 100,
    })

    assert len(jobs) == 1
    assert jobs[0].description == "Short search preview for the Acme role."
    assert error and error.startswith("partial:")
    assert "invalid job" in error
    assert "retained its preview" in error
    assert "short or requirement-free detail" in error


def test_freehire_optional_canonical_enrichment_uses_existing_guarded_path(monkeypatch):
    preview = _fixture("freehire_search_page1.json")["data"][0]
    rich = _fixture("freehire_detail_greenhouse.json")["data"]["description"]
    canonical_calls = []

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" in url:
            return {"data": [preview],
                    "meta": {"total": 1, "limit": 100, "offset": 0}}
        return {"data": {**preview, "description": "tiny detail"}}

    def fake_canonical(url):
        canonical_calls.append(url)
        return rich, "schema.org JobPosting"

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators._jd, "fetch_canonical", fake_canonical)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {
        "active": True, "canonical_enrichment": True,
    })

    assert error is None
    assert jobs[0].description == rich
    assert canonical_calls == [preview["url"]]
    assert jobs[0].raw["canonical_jd_source"] == "schema.org JobPosting"


def test_freehire_conservative_source_and_host_policy_excludes_redirects():
    assert aggregators._freehire_direct({
        "source": "linkedin",
        "url": "https://www.linkedin.com/jobs/view/1234567",
    }) is None
    assert aggregators._freehire_direct({
        "source": "adzuna",
        "url": "https://www.adzuna.com/details/123",
    }) is None
    assert aggregators._freehire_direct({
        "source": "greenhouse",
        "url": "https://greenhouse.io.attacker.invalid/acme/jobs/1",
    }) is None
    assert aggregators._freehire_direct({
        "source": "greenhouse",
        "url": "https://job-boards.greenhouse.io/acme/jobs/1",
    }) == ("greenhouse", "https://job-boards.greenhouse.io/acme/jobs/1")
    for source in ("zohorecruit", "zoho-recruit"):
        assert aggregators._freehire_direct({
            "source": source,
            "url": "https://acme.zohorecruit.com/jobs/Careers/1",
        }) == ("zoho", "https://acme.zohorecruit.com/jobs/Careers/1")


def test_freehire_requires_a_provider_specific_job_path_not_just_vendor_host():
    rejected = (
        ("greenhouse", "https://greenhouse.io/pricing"),
        ("greenhouse", "https://job-boards.greenhouse.io/acme"),
        ("lever", "https://lever.co/blog"),
        ("lever", "https://jobs.lever.co/acme"),
        ("neogov", "https://www.governmentjobs.com/"),
        ("workday", "https://example.wd5.myworkdayjobs.com/en-US/External"),
        ("workday", "https://example.myworkdaysite.com/recruiting/acme/job/R-1"),
        ("taleo", "https://example.taleo.net/careersection/external/jobsearch.ftl"),
    )
    for source, url in rejected:
        assert aggregators._freehire_direct({"source": source, "url": url}) is None

    accepted = (
        ("greenhouse", "https://job-boards.greenhouse.io/acme/jobs/41001"),
        ("lever", "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555"),
        ("workday", "https://example.wd5.myworkdayjobs.com/en-US/External/job/R-771"),
        ("neogov", "https://www.governmentjobs.com/careers/fulton/jobs/3828944/example"),
        ("taleo", "https://example.taleo.net/careersection/external/jobdetail.ftl?job=123"),
    )
    for source, url in accepted:
        assert aggregators._freehire_direct({"source": source, "url": url}) == (source, url)


def test_freehire_never_treats_nonannual_or_untyped_usd_pay_as_annual_salary():
    annual = aggregators._freehire_salary({
        "salary_min": 120000, "salary_max": 150000,
        "salary_currency": "USD", "salary_period": "year",
    })
    monthly = aggregators._freehire_salary({
        "salary_min": 10000, "salary_max": 12500,
        "salary_currency": "USD", "salary_period": "month",
    })
    untyped = aggregators._freehire_salary({
        "salary_min": 120000, "salary_max": 150000,
        "salary_currency": "USD",
    })

    assert annual[:2] == (None, None)
    assert "model-derived pay metadata" in annual[2]
    assert "not employer-published" in annual[2]
    assert "values omitted from scoring and storage" in annual[2]
    assert monthly[:2] == (None, None)
    assert not any(char.isdigit() for char in monthly[2])
    assert untyped[:2] == (None, None)
    assert not any(char.isdigit() for char in untyped[2])

    job = Job(company="Acme", title="Consultant", url="https://example.invalid",
              source="freehire:greenhouse", comp_text=annual[2])
    assert score_engine.extract_comp(job) == (None, None)
    assert job.comp_source == "absent"


def test_freehire_closed_detail_is_omitted_without_fabricating_a_deadline(monkeypatch):
    preview = _fixture("freehire_search_page1.json")["data"][0]
    detail = _fixture("freehire_detail_greenhouse.json")
    detail["data"]["closed_at"] = "2026-08-20T12:30:00Z"

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" in url:
            return {"data": [preview],
                    "meta": {"total": 1, "limit": 100, "offset": 0}}
        return detail

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {"active": True})

    assert jobs == []
    assert error and error.startswith("partial:")
    assert "closed after search preview; omitted" in error
    assert "Position closes on" not in error


def test_freehire_page_two_429_is_partial_only_not_misstated_as_a_cap(monkeypatch):
    preview = _fixture("freehire_search_page1.json")["data"][0]
    calls = {"search": 0}

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" not in url:
            return _fixture("freehire_detail_greenhouse.json")
        calls["search"] += 1
        if calls["search"] == 1:
            return {"data": [preview],
                    "meta": {"total": 2, "limit": 1, "offset": 0}}
        http._local.last_status = 429
        return None

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {
        "active": True, "results_per_page": 1, "pages": 2,
    })

    assert len(jobs) == 1
    assert error and error.startswith("partial:")
    assert "HTTP 429" in error
    assert "capped:" not in error


def test_freehire_rejects_ignored_or_wrong_pagination_metadata(monkeypatch):
    preview = _fixture("freehire_search_page1.json")["data"][0]

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" in url:
            return {"data": [preview], "meta": {
                "total": 50,
                "limit": 99,
                "offset": 25,
                "ignored_params": [{"param": "countries", "reason": "unsupported"}],
            }}
        return _fixture("freehire_detail_greenhouse.json")

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {
        "active": True, "countries": ["us"],
    })

    assert len(jobs) == 1
    assert error and error.startswith("partial:")
    assert "pagination limit" in error
    assert "pagination offset" in error
    assert "ignored requested parameter(s): countries" in error


@pytest.mark.parametrize("invalid_total", [True, 1.5])
def test_freehire_rejects_boolean_and_fractional_provider_totals(
        monkeypatch, invalid_total):
    preview = _fixture("freehire_search_page1.json")["data"][0]

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" in url:
            return {"data": [preview], "meta": {
                "total": invalid_total, "limit": 100, "offset": 0,
            }}
        return _fixture("freehire_detail_greenhouse.json")

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {"active": True})

    assert len(jobs) == 1
    assert error and error.startswith("partial:")
    assert "invalid total" in error
    assert "capped:" not in error


def test_freehire_cross_page_duplicate_is_partial_and_hydrated_once(monkeypatch):
    preview = _fixture("freehire_search_page1.json")["data"][0]
    search_calls = {"count": 0}
    detail_calls = []

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" not in url:
            detail_calls.append(url)
            return _fixture("freehire_detail_greenhouse.json")
        page = search_calls["count"]
        search_calls["count"] += 1
        return {"data": [preview],
                "meta": {"total": 2, "limit": 1, "offset": page}}

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {
        "active": True, "results_per_page": 1, "pages": 2,
    })

    assert len(jobs) == 1
    assert len(detail_calls) == 1
    assert error and error.startswith("partial:")
    assert "repeated 1 job(s) from an earlier page" in error
    assert "capped:" not in error


def test_freehire_missing_detail_slug_cannot_launder_another_allowed_job(monkeypatch):
    preview = _fixture("freehire_search_page1.json")["data"][0]
    switched = _fixture("freehire_detail_greenhouse.json")["data"]
    switched.pop("public_slug")
    switched["company"] = "Different Company"
    switched["external_id"] = "different-upstream-id"
    switched["url"] = "https://job-boards.greenhouse.io/different/jobs/999"

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" in url:
            return {"data": [preview],
                    "meta": {"total": 1, "limit": 100, "offset": 0}}
        return {"data": switched}

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {"active": True})

    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_id == preview["public_slug"]
    assert job.company == preview["company"]
    assert job.url == preview["url"]
    assert job.raw["upstream_external_id"] == preview["external_id"]
    assert error and "omitted or returned a different slug" in error


def test_freehire_detail_cannot_switch_preview_identity(monkeypatch):
    preview = _fixture("freehire_search_page1.json")["data"][0]
    switched = _fixture("freehire_detail_greenhouse.json")["data"]
    switched.update({
        "public_slug": preview["public_slug"],
        "company": "Different Company",
        "title": "Unrelated Engineering Director",
        "external_id": "different-999",
        "url": "https://job-boards.greenhouse.io/different/jobs/999",
    })

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" in url:
            return {"data": [preview], "meta": {
                "total": 1, "limit": 100, "offset": 0,
            }}
        return {"data": switched}

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {"active": True})

    assert len(jobs) == 1
    assert (jobs[0].company, jobs[0].title, jobs[0].url) == (
        preview["company"], preview["title"], preview["url"])
    assert error and "changed preview identity" in error


def test_freehire_total_underflow_is_partial_not_healthy(monkeypatch):
    preview = _fixture("freehire_search_page1.json")["data"][0]

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" in url:
            return {"data": [preview],
                    "meta": {"total": 0, "limit": 100, "offset": 0}}
        return _fixture("freehire_detail_greenhouse.json")

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {"active": True})

    assert len(jobs) == 1
    assert error and error.startswith("partial:")
    assert "rows beyond its advertised total" in error


def test_freehire_same_page_duplicate_is_partial_and_hydrated_once(monkeypatch):
    preview = _fixture("freehire_search_page1.json")["data"][0]
    detail_calls = []

    def fake_fetch_json(url, **_kwargs):
        if "/jobs/search?" in url:
            return {"data": [preview, dict(preview)],
                    "meta": {"total": 2, "limit": 100, "offset": 0}}
        detail_calls.append(url)
        return _fixture("freehire_detail_greenhouse.json")

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce consultant",))

    jobs, error = aggregators.run_feed("freehire", {"active": True})

    assert len(jobs) == 1
    assert len(detail_calls) == 1
    assert error and error.startswith("partial:")
    assert "duplicate job identities" in error


def test_freehire_shipped_config_and_privacy_disclosure_are_explicit():
    setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
    assert "{name: freehire, active: false" in setup
    assert "posted_within_days: 14" in setup
    assert "canonical_enrichment: false" in setup

    policy = aggregators.policy("freehire")
    assert policy["opt_in"] is True
    assert policy["sends_search_terms"] is True
    assert "never credentials/resume/claims" in policy["note"]

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    disclosure = readme[readme.index("**Freehire, only if you explicitly enable it:**"):]
    assert "search_terms" in disclosure
    assert "request metadata" in disclosure
    assert "no secret, API key, resume, claims" in disclosure
    assert "LinkedIn and aggregator-of-aggregator results are" in disclosure
    assert "Every Freehire row" in disclosure
    assert "forced to `VERIFY`" in disclosure

    guide_html = (ROOT / "guide" / "careerkit-guide.html").read_text(encoding="utf-8")
    assert "<span>14 + 1 dormant</span>" in guide_html
    assert "To Freehire, only if you enable it:" in guide_html
    assert "quoted search terms" in guide_html
    assert "ordinary request metadata" in guide_html
    assert "No API key, resume, claims register or application data is sent" in guide_html
    assert "Every\n    Freehire row is forced to <code>VERIFY</code>" in guide_html
