"""Source-completeness regressions.

All responses are deterministic stubs.  These tests must never contact a job
board or a user's private CareerKit instance.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from engine import adapters, aggregators, http, pull, store
from engine.models import Job


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    con = store.connect()
    yield con
    con.close()


def _job(*, source="greenhouse", company="Acme", external_id="1", board=""):
    return Job(
        company=company,
        title="Program Manager",
        url=f"https://jobs.example/{external_id}",
        source=source,
        external_id=external_id,
        board=board,
        description="Complete job description " * 20,
    )


def test_source_diagnostics_reset_between_dispatches():
    http.reset_status()
    http.mark_partial("page two failed")
    http.mark_capped("stopped at 100")
    assert http.source_integrity_error().startswith("partial:")

    http.reset_status()
    assert http.source_diagnostics() == {"partial": (), "capped": ()}
    assert http.source_integrity_error() is None


def test_smartrecruiters_page_two_failure_retains_page_one(monkeypatch):
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    def fake_fetch_json(url, **_kwargs):
        if "offset=0" in url:
            return {
                "totalFound": 101,
                "content": [{
                    "id": "one",
                    "name": "Program Manager",
                    "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/one",
                    "location": {"city": "Remote", "country": "US"},
                }],
            }
        return None

    monkeypatch.setattr(adapters, "fetch_json", fake_fetch_json)
    jobs, error = adapters.run_adapter(
        {"ats": "smartrecruiters", "slug": "acme", "name": "Acme"})

    assert [job.external_id for job in jobs] == ["one"]
    assert error and error.startswith("partial:")
    assert "offset 100" in error


def test_remotive_term_failure_retains_successful_term(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ("program manager", "delivery lead"))
    calls = {"n": 0}

    def fake_fetch_json(_url, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"jobs": [{
                "id": 7,
                "company_name": "Acme",
                "title": "Program Manager",
                "url": "https://remote.example/7",
                "description": "A complete posting",
            }]}
        return None

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    jobs, error = aggregators.run_feed("remotive", {})

    assert [job.external_id for job in jobs] == ["7"]
    assert error and error.startswith("partial:")
    assert "term 2" in error


def test_remotive_blank_object_is_partial_and_not_a_healthy_job(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ("crm",))
    monkeypatch.setattr(
        aggregators, "fetch_json", lambda *_a, **_k: {"jobs": [{}]},
    )

    jobs, error = aggregators.run_feed("remotive", {})

    assert jobs == []
    assert error and "stable identity, title, or company" in error


def test_adzuna_pages_recent_results_dedupes_and_reports_remaining_cap(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ("salesforce architect",))
    calls = []

    def fake_fetch_json(url, **_kwargs):
        calls.append(url)
        page = int(urlsplit(url).path.rsplit("/", 1)[-1])
        start = 1 if page == 1 else 50
        return {
            "count": 120,
            "results": [{
                "id": str(index),
                "company": {"display_name": "Acme"},
                "title": "Salesforce Architect",
                "redirect_url": f"https://adzuna.example/{index}",
                "location": {"display_name": "Remote, US"},
                "description": "Full role",
            } for index in range(start, start + 50)],
        }

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)
    jobs, error = aggregators.run_feed("adzuna", {
        "app_id": "id with space", "app_key": "key&value", "pages": 2,
        "results_per_page": 50, "max_days_old": 14, "sort_by": "date",
    })

    # ID 50 occurs on both pages and is counted once.
    assert len(jobs) == 99
    assert error and error.startswith("partial:")
    assert "repeated a job from an earlier page" in error
    assert "capped:" in error
    assert "fetched 99 of 120" in error
    assert [urlsplit(url).path.rsplit("/", 1)[-1] for url in calls] == ["1", "2"]
    query = parse_qs(urlsplit(calls[0]).query)
    assert query["what"] == ["salesforce architect"]
    assert query["app_id"] == ["id with space"]
    assert query["app_key"] == ["key&value"]
    assert query["max_days_old"] == ["14"]
    assert query["sort_by"] == ["date"]


def test_usajobs_pages_to_provider_end_and_dedupes_terms(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ("crm", "salesforce"))
    calls = []

    def result(position_id):
        return {"MatchedObjectDescriptor": {
            "PositionID": position_id,
            "PositionTitle": "CRM Program Manager",
            "PositionURI": f"https://www.usajobs.gov/job/{position_id}",
            "OrganizationName": "Example Agency",
            "PositionLocation": [{"LocationName": "Atlanta, Georgia"}],
            "PositionRemuneration": [{
                "MinimumRange": "120000", "MaximumRange": "160000",
                "RateIntervalCode": "Per Year",
            }],
            "UserArea": {"Details": {
                "JobSummary": "Lead the CRM program.",
                "MajorDuties": "Own cross-agency delivery.",
                "Requirements": "Public-trust review required.",
                "Qualifications": "Five years of CRM leadership.",
            }},
            "PublicationStartDate": "2026-08-19T00:00:00",
        }}

    def fake_fetch(url, **_kwargs):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        page = int(query["Page"][0])
        ids = ["1", "2"] if page == 1 else ["3"]
        return 200, json.dumps({"SearchResult": {
            "SearchResultCountAll": 3,
            "SearchResultItems": [result(value) for value in ids],
            "UserArea": {"NumberOfPages": "2"},
        }})

    monkeypatch.setattr(aggregators, "fetch", fake_fetch)
    jobs, error = aggregators.run_feed("usajobs", {
        "api_key": "private key", "email": "owner@example.invalid",
        "pages": 3, "results_per_page": 2,
    })

    assert [job.external_id for job in jobs] == ["1", "2", "3"]
    assert error is None
    assert jobs[0].comp_min == 120000 and jobs[0].comp_max == 160000
    assert jobs[0].comp_source == "board"
    for phrase in ("Lead the CRM program", "Own cross-agency delivery",
                   "Public-trust review required", "Five years of CRM leadership"):
        assert phrase in jobs[0].description
    assert len(calls) == 4, "each term should traverse its advertised two pages"
    assert all(query["ResultsPerPage"] == ["2"] for query in calls)
    assert [query["Page"][0] for query in calls] == ["1", "2", "1", "2"]


def test_usajobs_marks_configured_page_budget_as_incomplete(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ("crm",))

    def fake_fetch(_url, **_kwargs):
        return 200, json.dumps({"SearchResult": {
            "SearchResultCountAll": 900,
            "SearchResultItems": [{"MatchedObjectDescriptor": {
                "PositionID": "1", "PositionTitle": "CRM Lead",
                "PositionURI": "https://www.usajobs.gov/job/1",
            }}],
            "UserArea": {"NumberOfPages": "2"},
        }})

    monkeypatch.setattr(aggregators, "fetch", fake_fetch)
    jobs, error = aggregators.run_feed("usajobs", {
        "api_key": "private key", "email": "owner@example.invalid",
        "pages": 1, "results_per_page": 500,
    })

    assert [job.external_id for job in jobs] == ["1"]
    assert error and error.startswith("capped:")
    assert "fetched 1 of 900" in error


def test_usajobs_does_not_misstate_hourly_compensation_as_annual(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ("crm",))

    def fake_fetch(_url, **_kwargs):
        return 200, json.dumps({"SearchResult": {
            "SearchResultCountAll": 1,
            "SearchResultItems": [{"MatchedObjectDescriptor": {
                "PositionID": "1", "PositionTitle": "CRM Consultant",
                "PositionURI": "https://www.usajobs.gov/job/1",
                "PositionRemuneration": [{
                    "MinimumRange": "65", "MaximumRange": "85",
                    "RateIntervalCode": "Per Hour",
                }],
            }}],
            "UserArea": {"NumberOfPages": "1"},
        }})

    monkeypatch.setattr(aggregators, "fetch", fake_fetch)
    jobs, error = aggregators.run_feed("usajobs", {
        "api_key": "private key", "email": "owner@example.invalid",
    })

    assert error is None
    assert jobs[0].comp_min is None and jobs[0].comp_max is None
    assert jobs[0].comp_text == "$65 - $85 / Per Hour"


def test_workday_provider_total_exposes_configured_safety_cap(monkeypatch):
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    def fake_fetch(_url, *, json_body=None, **_kwargs):
        offset = json_body["offset"]
        postings = [{
            "title": f"Role {offset + index}",
            "externalPath": f"/job/{offset + index}",
            "bulletFields": [f"REQ-{offset + index}"],
        } for index in range(20)]
        return 200, json.dumps({"total": 1000, "jobPostings": postings})

    monkeypatch.setattr(adapters, "fetch", fake_fetch)
    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External",
    })

    assert len(jobs) == 120
    assert error and error.startswith("capped:")
    assert "provider total 1000" in error


def test_workday_keeps_first_page_total_when_later_pages_report_zero(monkeypatch):
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    def fake_fetch(_url, *, json_body=None, **_kwargs):
        offset = json_body["offset"]
        postings = [{
            "title": f"Role {offset + index}",
            "externalPath": f"/job/{offset + index}",
            "bulletFields": [f"REQ-{offset + index}"],
        } for index in range(20)]
        total = 157 if offset == 0 else 0
        return 200, json.dumps({"total": total, "jobPostings": postings})

    monkeypatch.setattr(adapters, "fetch", fake_fetch)
    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Arch Capital", "tenant": "archgroup",
        "dc": "wd1", "site": "Careers",
    })

    assert len(jobs) == 120
    assert error and error.startswith("capped:")
    assert "provider total 157" in error


def test_relevant_detail_failure_retains_list_job_but_marks_partial(monkeypatch):
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile("program", re.I))
    calls = {"n": 0}

    def fake_fetch_json(_url, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"result": [{
                "id": "42",
                "jobOpeningName": "Program Manager",
                "location": {"city": "Atlanta", "state": "GA"},
            }]}
        return None

    monkeypatch.setattr(adapters, "fetch_json", fake_fetch_json)
    jobs, error = adapters.run_adapter(
        {"ats": "bamboohr", "slug": "acme", "name": "Acme"})

    assert [job.external_id for job in jobs] == ["42"]
    assert error and error.startswith("partial:")
    assert "detail 42" in error


def test_known_hard_cap_is_persisted_as_a_durable_health_error(db, monkeypatch):
    payload = {"jobs": [{
        "title": f"Role {index}",
        "shortcode": f"REQ-{index}",
        "url": f"https://apply.workable.com/acme/j/REQ-{index}/",
    } for index in range(100)]}
    monkeypatch.setattr(adapters, "fetch_json", lambda *_a, **_k: payload)

    cfg = {"ats": "workable", "slug": "acme", "name": "Acme"}
    result = pull.fetch_all(
        {"employers": [cfg], "feeds": []}, {}, db, echo=lambda *_a: None)

    assert len(result["jobs"]) == 100
    assert result["sources_ok"] == 0
    error = result["errors"][adapters.board_id(cfg)]
    assert error.startswith("capped:") and "known hard ceiling" in error
    health = db.execute(
        "SELECT source,last_error FROM source_health").fetchone()
    assert tuple(health) == (adapters.board_id(cfg), error[:300])


def test_partial_jobs_are_retained_but_source_cannot_retire_rows(
        db, monkeypatch):
    cfg = {"ats": "greenhouse", "slug": "acme", "name": "Acme"}
    board = adapters.board_id(cfg)
    retained = _job(external_id="new", board=board)
    stale = _job(external_id="old", board=board)
    store.upsert(db, [stale])
    db.execute("UPDATE jobs SET last_seen='2000-01-01'")
    db.commit()

    monkeypatch.setattr(
        pull, "run_adapter",
        lambda _cfg: ([retained], "partial: listing page two returned HTTP 500"),
    )
    result = pull.fetch_all({"employers": [cfg], "feeds": []}, {}, db,
                            echo=lambda *_a: None)

    assert result["jobs"] == [retained]
    assert result["sources_ok"] == 0
    assert result["healthy_boards"] == set()
    health = db.execute(
        "SELECT source,last_count,last_error FROM source_health").fetchone()
    assert tuple(health) == (board, 1, "partial: listing page two returned HTTP 500")

    for _ in range(2):
        store.reconcile(db, {}, result["healthy_boards"], result["healthy_feeds"])
    row = db.execute(
        "SELECT misses,delisted_on FROM jobs WHERE uid=?", (stale.uid,)).fetchone()
    assert tuple(row) == (0, None)


def test_same_name_boards_have_independent_health_rows(db, monkeypatch):
    first = {
        "ats": "workday", "name": "Same Co", "tenant": "same",
        "dc": "wd1", "site": "External-A",
    }
    second = {
        "ats": "workday", "name": "Same Co", "tenant": "same",
        "dc": "wd1", "site": "External-B",
    }

    def fake_run_adapter(cfg):
        board = adapters.board_id(cfg)
        if cfg["site"] == "External-A":
            return [_job(source="workday", external_id="one", board=board)], None
        return [], "partial: listing failed"

    monkeypatch.setattr(pull, "run_adapter", fake_run_adapter)
    result = pull.fetch_all(
        {"employers": [first, second], "feeds": []}, {}, db,
        echo=lambda *_a: None,
    )

    rows = list(db.execute(
        "SELECT source,last_count,last_error FROM source_health ORDER BY source"))
    assert [tuple(row) for row in rows] == [
        (adapters.board_id(first), 1, None),
        (adapters.board_id(second), 0, "partial: listing failed"),
    ]
    assert result["sources_ok"] == 1
    assert set(result["errors"]) == {adapters.board_id(second)}
    assert {entry[2] for entry in result["healthy_boards"]} == {
        adapters.board_id(first)}


def test_legacy_health_key_migrates_before_collapse_comparison(db, monkeypatch):
    cfg = {"ats": "greenhouse", "slug": "acme", "name": "Acme Corp"}
    legacy = "greenhouse:Acme Corp"
    stable = adapters.board_id(cfg)
    store.record_health(db, legacy, 100, None)
    monkeypatch.setattr(
        pull, "run_adapter",
        lambda _cfg: ([_job(external_id="only", board=stable)], None),
    )

    result = pull.fetch_all(
        {"employers": [cfg], "feeds": []}, {}, db, echo=lambda *_a: None)

    rows = list(db.execute(
        "SELECT source,prev_count,last_count FROM source_health"))
    assert [tuple(row) for row in rows] == [(stable, 100, 1)]
    assert result["healthy_boards"] == set(), (
        "the first stable-key run lost the legacy 100-row collapse baseline")


def test_legacy_health_migration_does_not_guess_duplicate_endpoints(db):
    first = {"ats": "greenhouse", "slug": "shared", "name": "First Label"}
    second = {"ats": "greenhouse", "slug": "shared", "name": "Second Label"}
    store.record_health(db, "greenhouse:First Label", 20, None)
    store.record_health(db, "greenhouse:Second Label", 30, None)

    migrated = pull._migrate_source_health_keys(db, [first, second])

    assert migrated == 0
    assert [row["source"] for row in db.execute(
        "SELECT source FROM source_health ORDER BY source")] == [
            "greenhouse:First Label", "greenhouse:Second Label"]


def test_partial_attempt_cannot_poison_the_last_complete_count(db, monkeypatch):
    cfg = {"ats": "greenhouse", "slug": "acme", "name": "Acme"}
    board = adapters.board_id(cfg)
    store.record_health(db, board, 100, None)
    responses = [
        ([_job(external_id=f"partial-{i}", board=board) for i in range(10)],
         "partial: page two failed"),
        ([_job(external_id=f"clean-{i}", board=board) for i in range(10)], None),
    ]
    monkeypatch.setattr(pull, "run_adapter", lambda _cfg: responses.pop(0))

    first = pull.fetch_all(
        {"employers": [cfg], "feeds": []}, {}, db, echo=lambda *_a: None)
    second = pull.fetch_all(
        {"employers": [cfg], "feeds": []}, {}, db, echo=lambda *_a: None)

    row = db.execute(
        "SELECT prev_count,last_count,last_error FROM source_health WHERE source=?",
        (board,),
    ).fetchone()
    assert tuple(row) == (
        100, 10,
        "partial: count fell 100 -> 10; below 50% of the last complete baseline",
    )
    assert first["sources_ok"] == second["sources_ok"] == 0
    assert first["healthy_boards"] == second["healthy_boards"] == set()


def test_duplicate_active_feeds_are_polled_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pull, "run_feed",
        lambda name, _cfg: (calls.append(name), ([], None))[1],
    )

    result = pull.fetch_all(
        {"employers": [], "feeds": [
            {"name": "remotive"}, {"name": "remotive"},
        ]}, {}, echo=lambda *_a: None,
    )

    assert calls == ["remotive"]
    assert result["sources_ok"] == 1


def test_duplicate_active_employer_endpoint_is_polled_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pull, "run_adapter",
        lambda cfg: (calls.append(cfg["name"]), ([], None))[1],
    )
    first = {"ats": "greenhouse", "slug": "acme", "name": "First"}
    second = {"ats": "greenhouse", "slug": "ACME", "name": "Second"}

    result = pull.fetch_all(
        {"employers": [first, second], "feeds": []}, {},
        echo=lambda *_a: None,
    )

    assert calls == ["First"]
    assert result["sources_ok"] == 1
    assert {item[2] for item in result["healthy_boards"]} == {
        adapters.board_id(first),
    }


def test_provider_totals_cannot_disappear_to_make_full_budgets_healthy(monkeypatch):
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    def workday_fetch(_url, *, json_body=None, **_kwargs):
        offset = json_body["offset"]
        rows = [{
            "title": f"Role {offset + i}",
            "externalPath": f"/job/{offset + i}",
            "bulletFields": [f"REQ-{offset + i}"],
        } for i in range(20)]
        return 200, json.dumps({"total": 0, "jobPostings": rows})

    monkeypatch.setattr(adapters, "fetch", workday_fetch)
    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External", "pages": 2,
    })
    assert len(jobs) == 40
    assert error and error.startswith("capped:")

    monkeypatch.setattr(aggregators, "TERMS", ["crm"])
    adzuna_calls = {"n": 0}

    def adzuna_fetch(*_args, **_kwargs):
        adzuna_calls["n"] += 1
        start = (adzuna_calls["n"] - 1) * 2
        return {
            "count": 6 if adzuna_calls["n"] == 1 else 0,
            "results": [{
                "id": f"adz-{start + i}", "title": "CRM Manager",
                "redirect_url": f"https://example.test/adz-{start + i}",
                "company": {"display_name": "Acme"},
                "location": {"display_name": "Remote"},
            } for i in range(2)],
        }

    monkeypatch.setattr(aggregators, "fetch_json", adzuna_fetch)
    jobs, error = aggregators.run_feed("adzuna", {
        "app_id": "id", "app_key": "key", "pages": 2,
        "results_per_page": 2,
    })
    assert len(jobs) == 4
    assert error and error.startswith("capped:")
    assert "4 of 6" in error


def test_oracle_requires_listing_shape_and_retains_valid_siblings(monkeypatch):
    cfg = {
        "ats": "oracle_orc", "name": "Acme",
        "host": "example.fa.ocs.oraclecloud.com", "site": "CX",
    }
    monkeypatch.setattr(adapters, "fetch_json", lambda *_a, **_k: {})
    jobs, error = adapters.run_adapter(cfg)
    assert jobs == []
    assert error and error.startswith("partial:")

    valid = {
        "Id": "REQ-1", "Title": "Operations Manager",
        "PrimaryLocation": "Remote, US", "ShortDescriptionStr": "Full JD",
    }
    responses = iter([
        {"items": [{"TotalJobsCount": 2, "requisitionList": [valid, None]}]},
    ])
    monkeypatch.setattr(adapters, "fetch_json", lambda *_a, **_k: next(responses))
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter(cfg)

    assert [job.external_id for job in jobs] == ["REQ-1"]
    assert error and error.startswith("partial:")

    many = [{
        "Id": f"REQ-{index}", "Title": "Operations Manager",
        "PrimaryLocation": "Remote, US", "ShortDescriptionStr": "Full JD",
    } for index in range(100)]
    monkeypatch.setattr(
        adapters, "fetch_json",
        lambda *_a, **_k: {
            "items": [{"TotalJobsCount": 300, "requisitionList": many}],
        },
    )

    jobs, error = adapters.run_adapter(cfg)

    assert len(jobs) == 100
    assert error and error.startswith("partial:")
    assert "100 of 300" in error

def test_usajobs_preserves_the_strongest_provider_total(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ["crm"])
    usajobs_calls = {"n": 0}

    def usajobs_fetch(*_args, **_kwargs):
        usajobs_calls["n"] += 1
        start = (usajobs_calls["n"] - 1) * 2
        rows = [{"MatchedObjectDescriptor": {
            "PositionID": f"fed-{start + i}",
            "PositionTitle": "CRM Manager",
            "PositionURI": f"https://www.usajobs.gov/job/{start + i}",
            "OrganizationName": "Agency",
        }} for i in range(2)]
        total = 6 if usajobs_calls["n"] == 1 else 0
        return 200, json.dumps({"SearchResult": {
            "SearchResultCountAll": total,
            "SearchResultItems": rows,
            "UserArea": {"NumberOfPages": 3 if total else 0},
        }})

    monkeypatch.setattr(aggregators, "fetch", usajobs_fetch)
    jobs, error = aggregators.run_feed("usajobs", {
        "api_key": "key", "email": "owner@example.test", "pages": 2,
        "results_per_page": 2,
    })
    assert len(jobs) == 4
    assert error and error.startswith("capped:")
    assert "4 of 6" in error


def test_replayed_aggregator_pages_are_partial_and_do_not_satisfy_totals(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ["crm"])
    adzuna_rows = [{
        "id": f"adz-{index}", "title": "CRM Manager",
        "redirect_url": f"https://example.test/adz-{index}",
        "company": {"display_name": "Acme"},
        "location": {"display_name": "Remote"},
    } for index in range(2)]
    monkeypatch.setattr(
        aggregators, "fetch_json",
        lambda *_a, **_k: {"count": 4, "results": adzuna_rows},
    )

    jobs, error = aggregators.run_feed("adzuna", {
        "app_id": "id", "app_key": "key", "pages": 2,
        "results_per_page": 2,
    })

    assert [job.external_id for job in jobs] == ["adz-0", "adz-1"]
    assert error and error.startswith("partial:")
    assert "replayed a prior page" in error
    assert "fetched 2 of 4" in error

    def federal(position_id):
        return {"MatchedObjectDescriptor": {
            "PositionID": position_id,
            "PositionTitle": "CRM Manager",
            "PositionURI": f"https://www.usajobs.gov/job/{position_id}",
            "OrganizationName": "Agency",
        }}

    federal_rows = [federal("fed-1"), federal("fed-2")]
    monkeypatch.setattr(
        aggregators, "fetch",
        lambda *_a, **_k: (200, json.dumps({"SearchResult": {
            "SearchResultCountAll": 4,
            "SearchResultItems": federal_rows,
            "UserArea": {"NumberOfPages": 2},
        }})),
    )

    jobs, error = aggregators.run_feed("usajobs", {
        "api_key": "key", "email": "owner@example.test", "pages": 2,
        "results_per_page": 2,
    })

    assert [job.external_id for job in jobs] == ["fed-1", "fed-2"]
    assert error and error.startswith("partial:")
    assert "replayed a prior page" in error
    assert "fetched 2 of 4" in error


def test_term_driven_feeds_without_terms_are_not_retirement_authority(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a term-driven feed made a request without terms")

    monkeypatch.setattr(aggregators, "fetch", forbidden)
    monkeypatch.setattr(aggregators, "fetch_json", forbidden)
    registry = {"employers": [], "feeds": [
        {"name": "freehire", "active": True},
        {"name": "remotive", "active": True},
    ]}

    result = pull.fetch_all(registry, {}, feeds_only=True, echo=lambda *_a: None)

    assert result["jobs"] == []
    assert result["healthy_feeds"] == set()
    assert set(result["errors"]) == {"freehire", "remotive"}
    assert all("no search_terms" in error for error in result["errors"].values())


def test_authoritative_company_name_does_not_orphan_a_known_board(db):
    board = "neogov:fulton"
    job = _job(source="neogov", company="Fulton County, GA", board=board)
    store.upsert(db, [job])
    db.execute("UPDATE jobs SET last_seen='2000-01-01' WHERE uid=?", (job.uid,))
    db.commit()

    store.reconcile(
        db, {}, {("greenhouse", "Other", "greenhouse:other")}, set(),
        known_boards={("neogov", "Fulton", board)},
    )

    row = db.execute("SELECT misses,delisted_on FROM jobs WHERE uid=?", (job.uid,)).fetchone()
    assert tuple(row) == (0, None)


def test_mapping_feeds_retain_valid_rows_when_one_element_is_malformed(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ["crm"])

    adzuna_valid = {
        "id": "adz-1", "title": "CRM Manager",
        "redirect_url": "https://example.test/adz-1",
        "company": {"display_name": "Acme"},
        "location": {"display_name": "Remote"},
    }
    monkeypatch.setattr(
        aggregators, "fetch_json",
        lambda *_a, **_k: {"count": 2, "results": [adzuna_valid, None]},
    )
    adzuna_jobs, adzuna_error = aggregators.run_feed(
        "adzuna", {"app_id": "id", "app_key": "key"})

    usajobs_valid = {"MatchedObjectDescriptor": {
        "PositionID": "fed-1", "PositionTitle": "CRM Manager",
        "PositionURI": "https://www.usajobs.gov/job/1",
        "OrganizationName": "Agency",
    }}
    monkeypatch.setattr(
        aggregators, "fetch",
        lambda *_a, **_k: (200, json.dumps({"SearchResult": {
            "SearchResultCountAll": 2,
            "SearchResultItems": [usajobs_valid, None],
            "UserArea": {"NumberOfPages": 1},
        }})),
    )
    usajobs_jobs, usajobs_error = aggregators.run_feed(
        "usajobs", {"api_key": "key", "email": "owner@example.test"})

    assert [job.external_id for job in adzuna_jobs] == ["adz-1"]
    assert adzuna_error and adzuna_error.startswith("partial:")
    assert [job.external_id for job in usajobs_jobs] == ["fed-1"]
    assert usajobs_error and usajobs_error.startswith("partial:")


def test_weworkremotely_defaults_use_the_live_combined_sales_marketing_feed(
        monkeypatch):
    requested = []

    def fake_fetch(url, **_kwargs):
        requested.append(url)
        http._local.last_status = 200
        return 200, "<rss><channel/></rss>"

    monkeypatch.setattr(
        aggregators, "fetch", fake_fetch,
    )

    jobs, error = aggregators.run_feed("weworkremotely", {})

    assert jobs == [] and error is None
    assert any("remote-sales-and-marketing-jobs.rss" in url for url in requested)
    assert not any("remote-marketing-jobs.rss" in url for url in requested)
    assert not any("remote-sales-jobs.rss" in url for url in requested)


def test_weworkremotely_rejects_login_html_but_accepts_valid_empty_rss(monkeypatch):
    def response(body):
        def fake_fetch(*_args, **_kwargs):
            http._local.last_status = 200
            return 200, body
        return fake_fetch

    monkeypatch.setattr(
        aggregators, "fetch",
        response("<html><body>Sign in to continue</body></html>"),
    )
    jobs, error = aggregators.run_feed(
        "weworkremotely", {"categories": ["remote-product-jobs"]})
    assert jobs == []
    assert error and error.startswith("partial:")

    monkeypatch.setattr(
        aggregators, "fetch",
        response("<rss><channel/></rss>"),
    )
    jobs, error = aggregators.run_feed(
        "weworkremotely", {"categories": ["remote-product-jobs"]})
    assert jobs == [] and error is None


def test_unkeyed_feed_page_replays_are_partial_and_deduplicated(monkeypatch):
    arbeit = {
        "slug": "one", "company_name": "Acme", "title": "CRM Manager",
        "url": "https://arbeit.example/one", "location": "Remote",
    }
    monkeypatch.setattr(
        aggregators, "fetch_json",
        lambda *_a, **_k: {"data": [arbeit], "meta": {"last_page": 3}},
    )
    jobs, error = aggregators.run_feed("arbeitnow", {})
    assert [job.external_id for job in jobs] == ["one"]
    assert error and "replayed a prior page" in error

    himalaya = {
        "guid": "two", "companyName": "Acme", "title": "CRM Manager",
        "applicationLink": "https://himalayas.example/two",
    }
    monkeypatch.setattr(
        aggregators, "fetch_json", lambda *_a, **_k: {"jobs": [himalaya]},
    )
    jobs, error = aggregators.run_feed("himalayas", {})
    assert [job.external_id for job in jobs] == ["two"]
    assert error and "replayed a prior page" in error

    muse = {
        "id": "three", "name": "CRM Manager",
        "company": {"name": "Acme"},
        "refs": {"landing_page": "https://themuse.example/three"},
        "locations": [], "categories": [],
    }
    monkeypatch.setattr(
        aggregators, "fetch_json",
        lambda *_a, **_k: {"results": [muse], "page_count": 2},
    )
    jobs, error = aggregators.run_feed(
        "themuse", {"categories": ["IT"], "locations": []})
    assert [job.external_id for job in jobs] == ["three"]
    assert error and "replayed a prior page" in error


def test_negative_provider_counts_are_partial_not_healthy(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ["crm"])
    adzuna = {
        "id": "adz-1", "title": "CRM Manager",
        "redirect_url": "https://example.test/adz-1",
        "company": {"display_name": "Acme"},
        "location": {"display_name": "Remote"},
    }
    monkeypatch.setattr(
        aggregators, "fetch_json",
        lambda *_a, **_k: {"count": -1, "results": [adzuna]},
    )
    jobs, error = aggregators.run_feed(
        "adzuna", {"app_id": "id", "app_key": "key"})
    assert [job.external_id for job in jobs] == ["adz-1"]
    assert error and "invalid count" in error

    federal = {"MatchedObjectDescriptor": {
        "PositionID": "fed-1", "PositionTitle": "CRM Manager",
        "PositionURI": "https://www.usajobs.gov/job/fed-1",
        "OrganizationName": "Agency",
    }}
    monkeypatch.setattr(
        aggregators, "fetch",
        lambda *_a, **_k: (200, json.dumps({"SearchResult": {
            "SearchResultCountAll": -1,
            "SearchResultItems": [federal],
            "UserArea": {"NumberOfPages": -2},
        }})),
    )
    jobs, error = aggregators.run_feed(
        "usajobs", {"api_key": "key", "email": "owner@example.test"})
    assert [job.external_id for job in jobs] == ["fed-1"]
    assert error and "invalid total" in error
    assert "invalid page count" in error


@pytest.mark.parametrize(
    ("name", "cfg", "empty_payload"),
    [
        ("remotive", {}, {"jobs": []}),
        ("arbeitnow", {}, {"data": []}),
        ("himalayas", {}, {"jobs": []}),
        ("jobicy", {}, {"jobs": []}),
        ("themuse", {"categories": ["IT"]}, {"results": [], "page_count": 0}),
        ("findwork", {"api_key": "key"}, {"results": [], "next": None}),
        ("careerjet", {"affid": "affiliate"}, {"jobs": [], "pages": 1}),
    ],
)
def test_mapping_feed_envelopes_distinguish_clean_empty_from_missing_key(
        monkeypatch, name, cfg, empty_payload):
    monkeypatch.setattr(aggregators, "TERMS", ["crm"])

    def response(payload):
        def fake_fetch_json(*_args, **_kwargs):
            http._local.last_status = 200
            http._local.last_parse_ok = True
            return payload
        return fake_fetch_json

    def findwork_response(payload):
        def fake_fetch(*_args, **_kwargs):
            http._local.last_status = 200
            return 200, json.dumps(payload)
        return fake_fetch

    target = "fetch" if name == "findwork" else "fetch_json"
    factory = findwork_response if name == "findwork" else response
    monkeypatch.setattr(aggregators, target, factory({}))
    jobs, error = aggregators.run_feed(name, cfg)
    assert jobs == []
    assert error and error.startswith("partial:")

    monkeypatch.setattr(aggregators, target, factory(empty_payload))
    jobs, error = aggregators.run_feed(name, cfg)
    assert jobs == [] and error is None


@pytest.mark.parametrize(
    ("name", "cfg", "payload", "uses_text_fetch"),
    [
        ("remoteok", {}, [{}, {}], False),
        ("jobicy", {}, {"jobs": [{}]}, False),
        ("workingnomads", {}, [{}], False),
        ("findwork", {"api_key": "test-key"}, {"results": [{}]}, True),
        ("careerjet", {"affid": "test-affiliate"},
         {"jobs": [{}], "pages": 1}, False),
    ],
)
def test_feed_blank_objects_are_partial_not_healthy(
        monkeypatch, name, cfg, payload, uses_text_fetch):
    monkeypatch.setattr(aggregators, "TERMS", ["crm"])
    if uses_text_fetch:
        monkeypatch.setattr(
            aggregators, "fetch",
            lambda *_a, **_k: (200, json.dumps(payload)),
        )
    else:
        monkeypatch.setattr(
            aggregators, "fetch_json", lambda *_a, **_k: payload,
        )

    jobs, error = aggregators.run_feed(name, cfg)

    assert jobs == []
    assert error and "stable identity, title, or company" in error


@pytest.mark.parametrize("name", ["remotive", "jobicy", "findwork", "careerjet"])
def test_term_feed_overlap_counts_a_posting_once(monkeypatch, name):
    monkeypatch.setattr(aggregators, "TERMS", ["crm", "salesforce"])
    payloads = {
        "remotive": {"jobs": [{
            "id": 1, "company_name": "Acme", "title": "CRM Manager",
            "url": "https://jobs.example/one",
        }]},
        "jobicy": {"jobs": [{
            "id": 1, "companyName": "Acme", "jobTitle": "CRM Manager",
            "url": "https://jobs.example/one",
        }]},
        "findwork": {"results": [{
            "id": 1, "company_name": "Acme", "role": "CRM Manager",
            "url": "https://jobs.example/one",
        }]},
        "careerjet": {"jobs": [{
            "company": "Acme", "title": "CRM Manager",
            "url": "https://jobs.example/one",
        }], "pages": 1},
    }
    cfg = {
        "remotive": {}, "jobicy": {}, "findwork": {"api_key": "test-key"},
        "careerjet": {"affid": "test-affiliate"},
    }[name]
    if name == "findwork":
        monkeypatch.setattr(
            aggregators, "fetch",
            lambda *_a, **_k: (200, json.dumps(payloads[name])),
        )
    else:
        monkeypatch.setattr(
            aggregators, "fetch_json", lambda *_a, **_k: payloads[name],
        )

    jobs, error = aggregators.run_feed(name, cfg)

    assert len(jobs) == 1
    assert jobs[0].url == "https://jobs.example/one"
    assert error is None


def test_invalid_optional_feed_pagination_metadata_is_partial(monkeypatch):
    monkeypatch.setattr(aggregators, "TERMS", ["crm"])

    arbeit = {
        "slug": "one", "company_name": "Acme", "title": "CRM Manager",
        "url": "https://jobs.example/one",
    }
    monkeypatch.setattr(
        aggregators, "fetch_json",
        lambda *_a, **_k: {"data": [arbeit], "meta": {"last_page": "bogus"}},
    )
    jobs, error = aggregators.run_feed("arbeitnow", {})
    assert len(jobs) == 1
    assert error and "invalid last_page" in error

    muse = {
        "id": "two", "name": "CRM Manager", "company": {"name": "Acme"},
        "refs": {"landing_page": "https://jobs.example/two"},
        "locations": [], "categories": [],
    }
    monkeypatch.setattr(
        aggregators, "fetch_json",
        lambda *_a, **_k: {"results": [muse], "page_count": "bogus"},
    )
    jobs, error = aggregators.run_feed(
        "themuse", {"categories": ["IT"], "locations": []})
    assert len(jobs) == 1
    assert error and "invalid page_count" in error

    careerjet = {
        "jobs": [{
            "company": "Acme", "title": "CRM Manager",
            "url": "https://jobs.example/three",
        }],
        "pages": "bogus",
    }
    monkeypatch.setattr(
        aggregators, "fetch_json", lambda *_a, **_k: careerjet,
    )
    jobs, error = aggregators.run_feed(
        "careerjet", {"affid": "test-affiliate"})
    assert len(jobs) == 1
    assert error and "invalid pages" in error


def test_himalayas_nonempty_page_after_short_page_is_partial(monkeypatch):
    pages = [
        {"jobs": [{
            "guid": f"first-{index}", "companyName": "Acme",
            "title": "CRM Manager",
            "applicationLink": f"https://jobs.example/first-{index}",
        } for index in range(50)]},
        {"jobs": [{
            "guid": f"second-{index}", "companyName": "Acme",
            "title": "CRM Manager",
            "applicationLink": f"https://jobs.example/second-{index}",
        } for index in range(20)]},
    ]
    monkeypatch.setattr(
        aggregators, "fetch_json", lambda *_a, **_k: pages.pop(0),
    )

    jobs, error = aggregators.run_feed("himalayas", {})

    assert len(jobs) == 70
    assert error and "after a short page" in error


def test_workday_retains_valid_rows_when_one_element_is_malformed(monkeypatch):
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))
    payload = {
        "total": 2,
        "jobPostings": [{
            "title": "CRM Manager", "externalPath": "/job/one",
            "bulletFields": ["REQ-1"],
        }, None],
    }
    monkeypatch.setattr(
        adapters, "fetch", lambda *_a, **_k: (200, json.dumps(payload)))

    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External",
    })

    assert [job.external_id for job in jobs] == ["REQ-1"]
    assert error and error.startswith("partial:")


def test_icims_continues_past_the_old_thirty_page_limit(monkeypatch):
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))
    calls = []

    def fake_fetch(url, **_kwargs):
        page = int(parse_qs(urlsplit(url).query)["pr"][0])
        calls.append(page)
        if page >= 31:
            return 200, '<div class="iCIMS_NoResults">No jobs</div>'
        return 200, (
            '<li class="iCIMS_JobCardItem">'
            f'<a href="https://careers-acme.icims.com/jobs/{page}/role/job" '
            'class="iCIMS_Anchor"><h3>Operations Role</h3></a></li>'
        )

    monkeypatch.setattr(adapters, "fetch", fake_fetch)
    jobs, error = adapters.run_adapter({
        "ats": "icims", "slug": "acme", "name": "Acme",
    })

    assert len(jobs) == 31
    assert calls[-1] == 31
    assert error is None


def test_icims_configured_page_budget_is_reported_as_capped(monkeypatch):
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    def fake_fetch(url, **_kwargs):
        page = int(parse_qs(urlsplit(url).query)["pr"][0])
        return 200, (
            '<li class="iCIMS_JobCardItem">'
            f'<a href="https://careers-acme.icims.com/jobs/{page}/role/job" '
            'class="iCIMS_Anchor"><h3>Operations Role</h3></a></li>'
        )

    monkeypatch.setattr(adapters, "fetch", fake_fetch)
    jobs, error = adapters.run_adapter({
        "ats": "icims", "slug": "acme", "name": "Acme", "pages": 2,
    })

    assert len(jobs) == 2
    assert error and error.startswith("capped:")
    assert "2-page / 100-posting safety cap" in error
