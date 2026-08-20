"""HTTP-200 response-shape regressions for employer-board adapters.

These tests are deliberately offline.  A successful status code is not enough
to call a source healthy: the body must still match the vendor's documented
listing envelope (or an explicit, vendor-specific empty-board marker).
"""
from __future__ import annotations

import json
import re

import pytest

from engine import adapters, pull


JSON_CASES = [
    (
        "greenhouse", "jobs",
        {"ats": "greenhouse", "slug": "acme", "name": "Acme"},
        {"id": 1, "title": "Program Manager",
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"},
    ),
    (
        "ashby", "jobs",
        {"ats": "ashby", "slug": "acme", "name": "Acme"},
        {"id": "one", "title": "Program Manager",
         "jobUrl": "https://jobs.ashbyhq.com/acme/one"},
    ),
    (
        "recruitee", "offers",
        {"ats": "recruitee", "slug": "acme", "name": "Acme"},
        {"id": 1, "title": "Program Manager",
         "careers_url": "https://acme.recruitee.com/o/one"},
    ),
    (
        "bamboohr", "result",
        {"ats": "bamboohr", "slug": "acme", "name": "Acme"},
        {"id": 1, "jobOpeningName": "Program Manager"},
    ),
]

SMART_CFG = {
    "ats": "smartrecruiters", "slug": "acme", "name": "Acme",
}
SMART_ROW = {
    "id": "one",
    "name": "Program Manager",
    "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/one",
    "location": {"city": "Atlanta", "country": "US"},
}
MISSING = object()


def _stub_json(monkeypatch, *payloads):
    responses = iter(payloads)
    monkeypatch.setattr(adapters, "fetch_json", lambda *_a, **_k: next(responses))
    # The production fetch_json records these facts itself.  The offline stub
    # supplies the same status/parse contract so run_adapter can classify zero.
    monkeypatch.setattr(adapters.http, "last_status", lambda: 200)
    monkeypatch.setattr(adapters.http, "last_parse_ok", lambda: True)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))


def _stub_text(monkeypatch, body: str):
    monkeypatch.setattr(adapters, "fetch", lambda *_a, **_k: (200, body))
    monkeypatch.setattr(adapters.http, "last_status", lambda: 200)
    monkeypatch.setattr(adapters.http, "last_parse_ok", lambda: None)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))


@pytest.mark.parametrize("name,key,cfg,_row", JSON_CASES)
@pytest.mark.parametrize("bad_payload", ([], {}, {"required": None}))
def test_http_200_wrong_json_envelope_is_partial(
        name, key, cfg, _row, bad_payload, monkeypatch):
    if isinstance(bad_payload, dict) and bad_payload:
        bad_payload = {key: None}
    _stub_json(monkeypatch, bad_payload)

    jobs, error = adapters.run_adapter(cfg)

    assert jobs == []
    assert error and error.startswith("partial:")


@pytest.mark.parametrize("name,key,cfg,_row", JSON_CASES)
def test_documented_empty_json_envelope_is_healthy(
        name, key, cfg, _row, monkeypatch):
    _stub_json(monkeypatch, {key: []})

    jobs, error = adapters.run_adapter(cfg)

    assert jobs == []
    assert error is None


@pytest.mark.parametrize(
    "rows",
    [
        [{"title": "Program Manager",
          "absolute_url": "https://boards.greenhouse.io/acme/jobs/one"}],
        [
            {"id": 1, "title": "Program Manager",
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/one"},
            {"id": 1, "title": "Program Manager II",
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/two"},
        ],
    ],
)
def test_greenhouse_requires_unique_provider_job_ids(monkeypatch, rows):
    _stub_json(monkeypatch, {"jobs": rows})

    jobs, error = adapters.run_adapter(
        {"ats": "greenhouse", "slug": "acme", "name": "Acme"})

    assert len(jobs) <= 1
    assert error and error.startswith("partial:")
    assert len({job.external_id for job in jobs}) == len(jobs)


@pytest.mark.parametrize("name,key,cfg,row", JSON_CASES)
def test_non_object_json_sibling_is_partial_but_valid_row_survives(
        name, key, cfg, row, monkeypatch):
    _stub_json(monkeypatch, {key: [row, "changed vendor row"]})

    jobs, error = adapters.run_adapter(cfg)

    assert [job.external_id for job in jobs] == [str(row["id"])]
    assert error and error.startswith("partial:")
    assert f"{key}[1]" in error


@pytest.mark.parametrize("total", (None, -1, "0", True, float("nan"), 0.5))
def test_smartrecruiters_requires_nonnegative_numeric_total_on_first_page(
        total, monkeypatch):
    payload = {"content": []}
    if total is not None:
        payload["totalFound"] = total
    _stub_json(monkeypatch, payload)

    jobs, error = adapters.run_adapter(SMART_CFG)

    assert jobs == []
    assert error and error.startswith("partial:")
    assert "totalFound" in error


def test_smartrecruiters_documented_empty_shape_is_healthy(monkeypatch):
    _stub_json(monkeypatch, {"content": [], "totalFound": 0})

    jobs, error = adapters.run_adapter(SMART_CFG)

    assert jobs == []
    assert error is None


def test_smartrecruiters_invalid_later_page_retains_earlier_rows(monkeypatch):
    _stub_json(
        monkeypatch,
        {"content": [SMART_ROW], "totalFound": 101},
        {"content": [], "totalFound": "101"},
    )

    jobs, error = adapters.run_adapter(SMART_CFG)

    assert [job.external_id for job in jobs] == ["one"]
    assert error and error.startswith("partial:")
    assert "offset 100" in error and "totalFound" in error


def test_smartrecruiters_bad_sibling_retains_valid_row(monkeypatch):
    _stub_json(
        monkeypatch,
        {"content": [SMART_ROW, "changed vendor row"], "totalFound": 2},
    )

    jobs, error = adapters.run_adapter(SMART_CFG)

    assert [job.external_id for job in jobs] == ["one"]
    assert error and error.startswith("partial:")
    assert "content[1]" in error


def test_jobvite_junk_200_is_partial(monkeypatch):
    _stub_text(monkeypatch, "<html><body>Sign in to continue</body></html>")

    jobs, error = adapters.run_adapter(
        {"ats": "jobvite", "slug": "acme", "name": "Acme"})

    assert jobs == []
    assert error and error.startswith("partial:")


def test_jobvite_vendor_zero_count_is_healthy(monkeypatch):
    _stub_text(monkeypatch, """
      <body class="jv-desktop jv-page-search">
        <table class="jv-job-list"><tbody></tbody></table>
        <div class="jv-pagination-text">0-0 of 0</div>
      </body>
    """)

    jobs, error = adapters.run_adapter(
        {"ats": "jobvite", "slug": "acme", "name": "Acme"})

    assert jobs == []
    assert error is None


def test_jobvite_generic_empty_prose_is_not_proof(monkeypatch):
    _stub_text(monkeypatch, "<html><p>No jobs are currently available.</p></html>")

    _jobs, error = adapters.run_adapter(
        {"ats": "jobvite", "slug": "acme", "name": "Acme"})

    assert error and error.startswith("partial:")


def test_jobvite_retains_parsed_sibling_when_another_link_changes(monkeypatch):
    _stub_text(monkeypatch, """
      <body class="jv-page-search">
        <a href="/acme/job/one">Program Manager</a>
        <a href="/acme/job/two">x</a>
      </body>
    """)

    jobs, error = adapters.run_adapter(
        {"ats": "jobvite", "slug": "acme", "name": "Acme"})

    assert [job.external_id for job in jobs] == ["one"]
    assert error and error.startswith("partial:")
    assert "1 of 2" in error


def test_hrmdirect_junk_200_is_partial(monkeypatch):
    _stub_text(monkeypatch, "<html><body>Sign in to continue</body></html>")

    jobs, error = adapters.run_adapter(
        {"ats": "hrmdirect", "slug": "acme", "name": "Acme"})

    assert jobs == []
    assert error and error.startswith("partial:")


def test_hrmdirect_explicit_vendor_empty_page_is_healthy(monkeypatch):
    _stub_text(monkeypatch, """
      <html><body>
        <p>Current job opportunities are posted here as they become available.</p>
        <p>There are currently no open positions.</p>
      </body></html>
    """)

    jobs, error = adapters.run_adapter(
        {"ats": "hrmdirect", "slug": "acme", "name": "Acme"})

    assert jobs == []
    assert error is None


def test_hrmdirect_retains_valid_sibling_when_another_row_changes(monkeypatch):
    _stub_text(monkeypatch, """
      <table>
        <tr data-req-id="one">
          <td id="posTitle0"><a href="job-opening.php?req=one">Program Manager</a></td>
        </tr>
        <tr data-req-id="two"><td id="posTitle1">Changed row without link</td></tr>
      </table>
    """)

    jobs, error = adapters.run_adapter(
        {"ats": "hrmdirect", "slug": "acme", "name": "Acme"})

    assert [job.external_id for job in jobs] == ["one"]
    assert error and error.startswith("partial:")
    assert "requisition two" in error


@pytest.mark.parametrize("body", (
    "<html><body>Sign in</body></html>",
    "<workzag-jobs><position></workzag-jobs>",
))
def test_personio_wrong_or_malformed_xml_is_partial(body, monkeypatch):
    _stub_text(monkeypatch, body)

    jobs, error = adapters.run_adapter(
        {"ats": "personio", "slug": "acme", "name": "Acme"})

    assert jobs == []
    assert error and error.startswith("partial:")


def test_personio_empty_workzag_root_is_healthy(monkeypatch):
    _stub_text(monkeypatch, "<?xml version='1.0'?><workzag-jobs></workzag-jobs>")

    jobs, error = adapters.run_adapter(
        {"ats": "personio", "slug": "acme", "name": "Acme"})

    assert jobs == []
    assert error is None


def test_fetch_all_does_not_count_partial_shape_as_healthy(monkeypatch):
    cfg = {"ats": "greenhouse", "slug": "acme", "name": "Acme"}
    _stub_json(monkeypatch, {
        "jobs": [JSON_CASES[0][3], "changed vendor row"],
    })

    result = pull.fetch_all(
        {"employers": [cfg], "feeds": []}, {}, echo=lambda *_a: None)

    assert len(result["jobs"]) == 1
    assert result["sources_ok"] == 0
    assert result["healthy_boards"] == set()
    assert result["errors"][adapters.board_id(cfg)].startswith("partial:")


def _workday_row(index: int) -> dict:
    return {
        "title": f"Role {index}",
        "externalPath": f"/job/role-{index}",
        "bulletFields": [f"REQ-{index}"],
    }


def test_workday_replayed_second_page_counts_only_unique_rows(monkeypatch):
    calls = []
    page = [_workday_row(index) for index in range(20)]

    def fake_fetch(_url, *, json_body=None, **_kwargs):
        calls.append(json_body["offset"])
        return 200, json.dumps({"total": 40, "jobPostings": page})

    monkeypatch.setattr(adapters, "fetch", fake_fetch)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External", "pages": 4,
    })

    assert calls == [0, 20]
    assert len(jobs) == 20
    assert len({job.external_id for job in jobs}) == 20
    assert error and error.startswith("partial:")
    assert "repeated a listing page" in error


def test_workday_same_page_duplicate_is_partial(monkeypatch):
    calls = []
    first_page = [_workday_row(index) for index in range(19)]
    first_page.append(_workday_row(0))

    def fake_fetch(_url, *, json_body=None, **_kwargs):
        offset = json_body["offset"]
        calls.append(offset)
        posts = first_page if offset == 0 else []
        return 200, json.dumps({"total": 20, "jobPostings": posts})

    monkeypatch.setattr(adapters, "fetch", fake_fetch)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External", "pages": 4,
    })

    assert calls == [0, 20]
    assert len(jobs) == 19
    assert error and error.startswith("partial:")
    assert "repeated identity" in error
    assert "19 unique postings of 20" in error


def test_workday_cross_page_duplicate_preserves_new_siblings(monkeypatch):
    calls = []

    def fake_fetch(_url, *, json_body=None, **_kwargs):
        offset = json_body["offset"]
        calls.append(offset)
        if offset == 0:
            posts = [_workday_row(index) for index in range(20)]
        elif offset == 20:
            posts = [_workday_row(index) for index in range(19, 39)]
        else:
            posts = []
        return 200, json.dumps({"total": 40, "jobPostings": posts})

    monkeypatch.setattr(adapters, "fetch", fake_fetch)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External", "pages": 4,
    })

    assert calls == [0, 20, 40]
    assert len(jobs) == 39
    assert len({job.external_id for job in jobs}) == 39
    assert error and error.startswith("partial:")
    assert "from an earlier page" in error


def test_oracle_replayed_second_page_counts_only_unique_rows(monkeypatch):
    calls = []
    page = [{
        "Id": str(index),
        "Title": f"Role {index}",
        "PrimaryLocation": "Atlanta, GA",
    } for index in range(200)]

    def fake_fetch_json(url, **_kwargs):
        calls.append(url)
        return {"items": [{"TotalJobsCount": 400, "requisitionList": page}]}

    monkeypatch.setattr(adapters, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(adapters.http, "last_status", lambda: 200)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter({
        "ats": "oracle_orc", "name": "Acme",
        "host": "example.fa.example.oraclecloud.com", "site": "CX",
    })

    assert len(calls) == 2
    assert "offset=0" in calls[0] and "offset=200" in calls[1]
    assert len(jobs) == 200
    assert len({job.external_id for job in jobs}) == 200
    assert error and error.startswith("partial:")
    assert "repeated a listing page" in error


def _smart_row(index: int) -> dict:
    return {
        "id": str(index),
        "name": f"Program Manager {index}",
        "ref": ("https://api.smartrecruiters.com/v1/companies/"
                f"acme/postings/{index}"),
        "location": {"country": "US"},
    }


def test_smartrecruiters_replayed_page_retains_100_unique_rows(monkeypatch):
    page = [_smart_row(index) for index in range(100)]
    payload = {"content": page, "totalFound": 200}
    _stub_json(monkeypatch, payload, payload)

    jobs, error = adapters.run_adapter(SMART_CFG)

    assert len(jobs) == 100
    assert len({job.external_id for job in jobs}) == 100
    assert error and error.startswith("partial:")
    assert "repeated a listing page at offset 100" in error


def _eightfold_row(index: int) -> dict:
    return {
        "id": str(index), "name": f"Program Manager {index}",
        "canonicalPositionUrl": f"https://jobs.example/{index}",
    }


def test_eightfold_replayed_page_retains_unique_rows(monkeypatch):
    payload = {"positions": [_eightfold_row(1), _eightfold_row(2)]}
    _stub_json(monkeypatch, payload, payload)

    jobs, error = adapters.run_adapter({
        "ats": "eightfold", "name": "Acme", "domain": "acme.example",
        "host": "https://acme.eightfold.ai",
    })

    assert [job.external_id for job in jobs] == ["1", "2"]
    assert error and error.startswith("partial:")
    assert "repeated a listing page" in error


def _phenom_row(index: int) -> dict:
    return {
        "jobId": str(index), "jobSeqNo": f"REQ-{index}",
        "title": f"Program Manager {index}",
        "applyUrl": f"https://phenom.example/job/{index}",
    }


def _phenom_payload(rows: list[dict], total: int = 4) -> dict:
    return {"refineSearch": {"totalHits": total, "data": {"jobs": rows}}}


def test_phenom_replayed_page_retains_unique_rows(monkeypatch):
    rows = [_phenom_row(1), _phenom_row(2)]
    payload = _phenom_payload(rows)
    _stub_json(monkeypatch, payload, payload)

    jobs, error = adapters.run_adapter({
        "ats": "phenom", "name": "Acme", "host": "https://phenom.example",
    })

    assert [job.external_id for job in jobs] == ["1", "2"]
    assert error and error.startswith("partial:")
    assert "repeated listing page" in error


def test_phenom_reordered_replay_is_no_progress(monkeypatch):
    rows = [_phenom_row(1), _phenom_row(2)]
    _stub_json(
        monkeypatch,
        _phenom_payload(rows),
        _phenom_payload(list(reversed(rows))),
    )

    jobs, error = adapters.run_adapter({
        "ats": "phenom", "name": "Acme", "host": "https://phenom.example",
    })

    assert [job.external_id for job in jobs] == ["1", "2"]
    assert error and error.startswith("partial:")
    assert "made no unique progress" in error


def test_phenom_provider_total_prevents_false_short_completion(monkeypatch):
    rows = [_phenom_row(1), _phenom_row(2)]
    _stub_json(
        monkeypatch,
        _phenom_payload(rows, total=52),
        _phenom_payload([], total=52),
    )

    jobs, error = adapters.run_adapter({
        "ats": "phenom", "name": "Acme", "host": "https://phenom.example",
    })

    assert [job.external_id for job in jobs] == ["1", "2"]
    assert error and error.startswith("partial:")
    assert "2 of 52 unique postings" in error


def _icims_page(*identities: int) -> str:
    return "".join(
        '<li class="iCIMS_JobCardItem">'
        f'<a href="https://careers-acme.icims.com/jobs/{identity}/role/job" '
        'class="iCIMS_Anchor">'
        f'<h3>Program Manager {identity}</h3></a></li>'
        for identity in identities
    )


def test_icims_replayed_page_retains_unique_rows(monkeypatch):
    _stub_text(monkeypatch, _icims_page(1, 2))

    jobs, error = adapters.run_adapter({
        "ats": "icims", "slug": "acme", "name": "Acme", "pages": 4,
    })

    assert [job.external_id for job in jobs] == ["1", "2"]
    assert error and error.startswith("partial:")
    assert "repeated listing page" in error


def test_icims_reordered_replay_is_no_progress(monkeypatch):
    pages = iter((_icims_page(1, 2), _icims_page(2, 1)))
    monkeypatch.setattr(adapters, "fetch", lambda *_a, **_k: (200, next(pages)))
    monkeypatch.setattr(adapters.http, "last_status", lambda: 200)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter({
        "ats": "icims", "slug": "acme", "name": "Acme", "pages": 4,
    })

    assert [job.external_id for job in jobs] == ["1", "2"]
    assert error and error.startswith("partial:")
    assert "made no unique progress" in error


def test_icims_login_page_is_partial(monkeypatch):
    _stub_text(monkeypatch, "<html><body>Sign in to continue</body></html>")

    jobs, error = adapters.run_adapter({
        "ats": "icims", "slug": "acme", "name": "Acme", "pages": 4,
    })

    assert jobs == []
    assert error and error.startswith("partial:")
    assert "login/challenge" in error


@pytest.mark.parametrize("total", (MISSING, -1, True, float("nan"),
                                    float("inf"), 1.5, "1"))
def test_workday_short_nonempty_page_requires_valid_numeric_total(
        total, monkeypatch):
    payload = {"jobPostings": [_workday_row(1)]}
    if total is not MISSING:
        payload["total"] = total
    monkeypatch.setattr(
        adapters, "fetch", lambda *_a, **_k: (200, json.dumps(payload)))
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External",
    })

    assert [job.external_id for job in jobs] == ["REQ-1"]
    assert error and error.startswith("partial:")
    assert "missing or invalid total" in error


def test_workday_missing_postings_key_is_partial(monkeypatch):
    monkeypatch.setattr(
        adapters, "fetch", lambda *_a, **_k: (200, json.dumps({"total": 0})))

    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External",
    })

    assert jobs == []
    assert error and error.startswith("partial:")
    assert "invalid postings" in error


def test_workday_zero_total_with_short_nonempty_page_is_partial(monkeypatch):
    payload = {"total": 0, "jobPostings": [_workday_row(1)]}
    monkeypatch.setattr(
        adapters, "fetch", lambda *_a, **_k: (200, json.dumps(payload)))
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External",
    })

    assert [job.external_id for job in jobs] == ["REQ-1"]
    assert error and error.startswith("partial:")
    assert "total 0 for a nonempty page" in error


@pytest.mark.parametrize("total", (MISSING, -1, True, float("nan"),
                                    float("inf"), 1.5, "1"))
def test_oracle_short_nonempty_page_requires_valid_numeric_total(
        total, monkeypatch):
    wrapper = {"requisitionList": [{"Id": "one", "Title": "Program Manager"}]}
    if total is not MISSING:
        wrapper["TotalJobsCount"] = total
    _stub_json(monkeypatch, {"items": [wrapper]})

    jobs, error = adapters.run_adapter({
        "ats": "oracle_orc", "name": "Acme",
        "host": "example.fa.example.oraclecloud.com", "site": "CX",
    })

    assert [job.external_id for job in jobs] == ["one"]
    assert error and error.startswith("partial:")
    assert "missing or invalid total" in error


def test_oracle_zero_total_with_nonempty_page_is_partial(monkeypatch):
    _stub_json(monkeypatch, {"items": [{
        "TotalJobsCount": 0,
        "requisitionList": [{"Id": "one", "Title": "Program Manager"}],
    }]})

    jobs, error = adapters.run_adapter({
        "ats": "oracle_orc", "name": "Acme",
        "host": "example.fa.example.oraclecloud.com", "site": "CX",
    })

    assert [job.external_id for job in jobs] == ["one"]
    assert error and error.startswith("partial:")
    assert "total 0 for a nonempty page" in error


def test_smartrecruiters_empty_detail_retains_listing_row_as_partial(monkeypatch):
    _stub_json(monkeypatch, {"content": [SMART_ROW], "totalFound": 1}, {})
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile("program", re.I))

    jobs, error = adapters.run_adapter(SMART_CFG)

    assert [job.external_id for job in jobs] == ["one"]
    assert jobs[0].description == ""
    assert error and error.startswith("partial:")
    assert "detail one" in error and "unexpected shape" in error


def test_workday_empty_detail_retains_listing_row_as_partial(monkeypatch):
    listing = {"total": 1, "jobPostings": [_workday_row(1)]}
    monkeypatch.setattr(
        adapters, "fetch", lambda *_a, **_k: (200, json.dumps(listing)))
    monkeypatch.setattr(adapters, "fetch_json", lambda *_a, **_k: {})
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile("role", re.I))

    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External",
    })

    assert [job.external_id for job in jobs] == ["REQ-1"]
    assert jobs[0].description == ""
    assert error and error.startswith("partial:")
    assert "detail REQ-1" in error and "unexpected shape" in error


def test_jobvite_login_detail_retains_listing_row_as_partial(monkeypatch):
    listing = '<a href="/acme/job/one">Program Manager</a>'

    def fake_fetch(url, **_kwargs):
        return (200, "<html>Sign in to continue</html>") if "/job/" in url else (
            200, listing)

    monkeypatch.setattr(adapters, "fetch", fake_fetch)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile("program", re.I))

    jobs, error = adapters.run_adapter(
        {"ats": "jobvite", "slug": "acme", "name": "Acme"})

    assert [job.external_id for job in jobs] == ["one"]
    assert jobs[0].description == ""
    assert error and error.startswith("partial:")
    assert "detail one was unusable" in error


def test_workable_empty_objects_are_not_a_valid_empty_board(monkeypatch):
    monkeypatch.setattr(adapters, "fetch_json", lambda *_a, **_k: {})
    monkeypatch.setattr(adapters, "fetch", lambda *_a, **_k: (200, "{}"))
    monkeypatch.setattr(adapters.http, "last_status", lambda: 200)

    jobs, error = adapters.run_adapter(
        {"ats": "workable", "slug": "acme", "name": "Acme"})

    assert jobs == []
    assert error and error.startswith("partial:")
    assert "unexpected shape" in error


def test_teamtailor_empty_object_is_not_a_valid_empty_board(monkeypatch):
    _stub_text(monkeypatch, "{}")

    jobs, error = adapters.run_adapter(
        {"ats": "teamtailor", "slug": "acme", "name": "Acme"})

    assert jobs == []
    assert error and error.startswith("partial:")
    assert "jobs/items as a list" in error


def test_paylocity_empty_page_data_is_not_a_valid_empty_board(monkeypatch):
    _stub_text(monkeypatch, "<script>window.pageData = {};</script>")

    jobs, error = adapters.run_adapter({
        "ats": "paylocity", "guid": "00000000-0000-0000-0000-000000000000",
        "name": "Acme",
    })

    assert jobs == []
    assert error and error.startswith("partial:")
    assert "Jobs as a list" in error


@pytest.mark.parametrize("ats,body,cfg", (
    ("teamtailor", '{"items": []}',
     {"ats": "teamtailor", "slug": "acme", "name": "Acme"}),
    ("paylocity", '<script>window.pageData = {"Jobs": []};</script>',
     {"ats": "paylocity", "guid": "empty", "name": "Acme"}),
))
def test_documented_text_empty_shapes_remain_healthy(ats, body, cfg, monkeypatch):
    _stub_text(monkeypatch, body)

    jobs, error = adapters.run_adapter(cfg)

    assert jobs == []
    assert error is None


def test_workable_documented_empty_shape_remains_healthy(monkeypatch):
    _stub_json(monkeypatch, {"jobs": []})

    jobs, error = adapters.run_adapter(
        {"ats": "workable", "slug": "acme", "name": "Acme"})

    assert jobs == []
    assert error is None


def test_smartrecruiters_rows_cannot_exceed_total(monkeypatch):
    _stub_json(monkeypatch, {
        "content": [_smart_row(1), _smart_row(2)], "totalFound": 1,
    })

    jobs, error = adapters.run_adapter(SMART_CFG)

    assert len(jobs) == 2
    assert error and error.startswith("partial:")
    assert "above totalFound 1" in error


def test_workday_rows_cannot_exceed_total(monkeypatch):
    payload = {"total": 1, "jobPostings": [_workday_row(1), _workday_row(2)]}
    monkeypatch.setattr(
        adapters, "fetch", lambda *_a, **_k: (200, json.dumps(payload)))
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter({
        "ats": "workday", "name": "Acme", "tenant": "acme",
        "dc": "wd1", "site": "External",
    })

    assert len(jobs) == 2
    assert error and error.startswith("partial:")
    assert "above provider total 1" in error


def test_oracle_rows_cannot_exceed_total(monkeypatch):
    _stub_json(monkeypatch, {"items": [{
        "TotalJobsCount": 1,
        "requisitionList": [
            {"Id": "one", "Title": "Program Manager"},
            {"Id": "two", "Title": "Program Manager"},
        ],
    }]})

    jobs, error = adapters.run_adapter({
        "ats": "oracle_orc", "name": "Acme",
        "host": "example.fa.example.oraclecloud.com", "site": "CX",
    })

    assert len(jobs) == 2
    assert error and error.startswith("partial:")
    assert "above provider total 1" in error


def test_jobvite_advertised_additional_pages_are_not_called_complete(monkeypatch):
    _stub_text(monkeypatch, """
      <body class="jv-page-search">
        <a href="/acme/job/one">Program Manager</a>
        <div class="jv-pagination-text">1-20 of 75</div>
      </body>
    """)

    jobs, error = adapters.run_adapter(
        {"ats": "jobvite", "slug": "acme", "name": "Acme"})

    assert [job.external_id for job in jobs] == ["one"]
    assert error and error.startswith("partial:")
    assert "1 of 20 advertised rows" in error
    assert "1 of 75 advertised postings" in error


def test_phenom_rows_cannot_exceed_total(monkeypatch):
    _stub_json(monkeypatch, _phenom_payload(
        [_phenom_row(1), _phenom_row(2)], total=1))

    jobs, error = adapters.run_adapter({
        "ats": "phenom", "name": "Acme", "host": "https://phenom.example",
    })

    assert len(jobs) == 2
    assert error and error.startswith("partial:")
    assert "above totalHits 1" in error


def test_icims_unrecognized_first_page_is_partial(monkeypatch):
    _stub_text(monkeypatch, "<html><body>Welcome</body></html>")

    jobs, error = adapters.run_adapter({
        "ats": "icims", "slug": "acme", "name": "Acme", "pages": 4,
    })

    assert jobs == []
    assert error and error.startswith("partial:")
    assert "proven empty marker" in error


def test_greenhouse_advertised_total_cannot_silently_exceed_rows(monkeypatch):
    _stub_json(monkeypatch, {
        "jobs": [JSON_CASES[0][3]],
        "meta": {"total": 2},
    })

    jobs, error = adapters.run_adapter(JSON_CASES[0][2])

    assert [job.external_id for job in jobs] == ["1"]
    assert error
    assert "1 of 2 advertised postings" in error


def test_bamboohr_advertised_total_cannot_silently_exceed_rows(monkeypatch):
    _stub_json(monkeypatch, {
        "result": [JSON_CASES[3][3]],
        "meta": {"totalCount": 2},
    })

    jobs, error = adapters.run_adapter(JSON_CASES[3][2])

    assert [job.external_id for job in jobs] == ["1"]
    assert error
    assert "1 of 2 advertised postings" in error


def test_oracle_has_more_true_cannot_be_called_complete(monkeypatch):
    _stub_json(monkeypatch, {
        "items": [{
            "TotalJobsCount": 2,
            "requisitionList": [{"Id": "one", "Title": "Program Manager"}],
        }],
        "hasMore": True,
    })

    jobs, error = adapters.run_adapter({
        "ats": "oracle_orc", "name": "Acme",
        "host": "example.fa.example.oraclecloud.com", "site": "CX",
    })

    assert [job.external_id for job in jobs] == ["one"]
    assert error and error.startswith("partial:")
    assert "hasMore=true" in error


def test_teamtailor_next_url_is_not_called_complete(monkeypatch):
    _stub_text(monkeypatch, json.dumps({
        "version": "https://jsonfeed.org/version/1.1",
        "items": [{
            "id": "one", "title": "Program Manager",
            "url": "https://acme.teamtailor.com/jobs/one",
        }],
        "next_url": "https://acme.teamtailor.com/jobs.json?page=2",
    }))

    jobs, error = adapters.run_adapter(
        {"ats": "teamtailor", "slug": "acme", "name": "Acme"})

    assert [job.external_id for job in jobs] == ["one"]
    assert error
    assert "another JSON Feed page" in error


def test_icims_unrecognized_later_page_retains_prior_rows(monkeypatch):
    pages = iter((_icims_page(1), "<html><body>Welcome</body></html>"))
    monkeypatch.setattr(
        adapters, "fetch", lambda *_a, **_k: (200, next(pages)))
    monkeypatch.setattr(adapters.http, "last_status", lambda: 200)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter({
        "ats": "icims", "slug": "acme", "name": "Acme", "pages": 4,
    })

    assert [job.external_id for job in jobs] == ["1"]
    assert error and error.startswith("partial:")
    assert "page 2" in error and "proven empty marker" in error


def test_icims_vendor_empty_marker_remains_healthy(monkeypatch):
    _stub_text(
        monkeypatch,
        '<div class="iCIMS_JobSearchNoResults">No jobs found</div>',
    )

    jobs, error = adapters.run_adapter({
        "ats": "icims", "slug": "acme", "name": "Acme", "pages": 4,
    })

    assert jobs == []
    assert error is None


def test_bamboohr_empty_detail_retains_listing_row_as_partial(monkeypatch):
    _stub_json(monkeypatch, {
        "result": [JSON_CASES[3][3]],
        "meta": {"totalCount": 1},
    }, {})
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile("program", re.I))

    jobs, error = adapters.run_adapter(JSON_CASES[3][2])

    assert [job.external_id for job in jobs] == ["1"]
    assert jobs[0].description == ""
    assert error and error.startswith("partial:")
    assert "detail 1" in error and "unexpected shape" in error


def test_oracle_empty_detail_does_not_validate_listing_teaser(monkeypatch):
    _stub_json(monkeypatch, {
        "items": [{
            "TotalJobsCount": 1,
            "requisitionList": [{
                "Id": "one", "Title": "Program Manager",
                "ShortDescriptionStr": "Teaser",
            }],
        }],
        "hasMore": False,
    }, {})
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile("program", re.I))

    jobs, error = adapters.run_adapter({
        "ats": "oracle_orc", "name": "Acme",
        "host": "example.fa.example.oraclecloud.com", "site": "CX",
    })

    assert [job.external_id for job in jobs] == ["one"]
    assert jobs[0].description == "Teaser"
    assert error and error.startswith("partial:")
    assert "detail one" in error and "unexpected shape" in error


def test_eightfold_nonempty_page_after_short_page_is_partial(monkeypatch):
    _stub_json(
        monkeypatch,
        {"positions": [_eightfold_row(1), _eightfold_row(2)]},
        {"positions": [_eightfold_row(3), _eightfold_row(4)]},
        {"positions": []},
    )

    jobs, error = adapters.run_adapter({
        "ats": "eightfold", "name": "Acme", "domain": "acme.example",
        "host": "https://acme.eightfold.ai",
    })

    assert [job.external_id for job in jobs] == ["1", "2", "3", "4"]
    assert error and error.startswith("partial:")
    assert "fixed offsets may have skipped rows" in error


MINIMUM_ROW_CFG = {
    "lever": {"ats": "lever", "slug": "acme", "name": "Acme"},
    "ashby": {"ats": "ashby", "slug": "acme", "name": "Acme"},
    "workable": {"ats": "workable", "slug": "acme", "name": "Acme"},
    "recruitee": {"ats": "recruitee", "slug": "acme", "name": "Acme"},
    "bamboohr": {"ats": "bamboohr", "slug": "acme", "name": "Acme"},
    "rippling": {"ats": "rippling", "slug": "acme", "name": "Acme"},
    "teamtailor": {"ats": "teamtailor", "slug": "acme", "name": "Acme"},
    "pinpoint": {"ats": "pinpoint", "slug": "acme", "name": "Acme"},
    "paylocity": {"ats": "paylocity", "guid": "board-guid", "name": "Acme"},
    "personio": {"ats": "personio", "slug": "acme", "name": "Acme"},
    "neogov": {"ats": "neogov", "slug": "acme", "name": "Acme"},
}


def _mapped_row(row: dict, **fields: str) -> dict:
    return {vendor: row[canonical] for vendor, canonical in fields.items()
            if canonical in row}


def _minimum_row_payload(ats: str, rows: list[dict]):
    if ats == "lever":
        return [_mapped_row(row, id="id", text="title", hostedUrl="url")
                for row in rows]
    if ats == "ashby":
        return {"jobs": [
            _mapped_row(row, id="id", title="title", jobUrl="url")
            for row in rows
        ]}
    if ats == "workable":
        return {"jobs": [
            _mapped_row(row, shortcode="id", title="title", url="url")
            for row in rows
        ]}
    if ats == "recruitee":
        return {"offers": [
            _mapped_row(row, id="id", title="title", careers_url="url")
            for row in rows
        ]}
    if ats == "bamboohr":
        return {"result": [
            _mapped_row(row, id="id", jobOpeningName="title")
            for row in rows
        ]}
    if ats == "rippling":
        return [_mapped_row(row, uuid="id", name="title", url="url")
                for row in rows]
    if ats == "teamtailor":
        return json.dumps({"items": [
            _mapped_row(row, id="id", title="title", url="url")
            for row in rows
        ]})
    if ats == "pinpoint":
        return {"data": [
            _mapped_row(row, id="id", title="title", url="url")
            for row in rows
        ]}
    if ats == "paylocity":
        data = {"Jobs": [
            _mapped_row(row, JobId="id", JobTitle="title")
            for row in rows
        ]}
        return f"<script>window.pageData = {json.dumps(data)};</script>"

    def tag(name: str, value) -> str:
        return f"<{name}>{value}</{name}>" if value is not None else ""

    if ats == "personio":
        positions = "".join(
            "<position>"
            + tag("id", row.get("id"))
            + tag("name", row.get("title"))
            + "</position>"
            for row in rows
        )
        return f"<workzag-jobs>{positions}</workzag-jobs>"
    if ats == "neogov":
        items = "".join(
            "<item>"
            + tag("joblisting:jobId", row.get("id"))
            + tag("title", row.get("title"))
            + tag("link", row.get("url"))
            + tag("guid", row.get("url"))
            + "</item>"
            for row in rows
        )
        return (
            '<rss xmlns:joblisting="http://www.neogov.com/namespaces/JobListing">'
            f"<channel><title>Acme</title>{items}</channel></rss>"
        )
    raise AssertionError(f"unhandled adapter {ats}")


def _stub_minimum_rows(monkeypatch, ats: str, rows: list[dict]):
    payload = _minimum_row_payload(ats, rows)
    if ats in {"teamtailor", "paylocity", "personio", "neogov"}:
        _stub_text(monkeypatch, payload)
    else:
        _stub_json(monkeypatch, payload)


@pytest.mark.parametrize("ats", tuple(MINIMUM_ROW_CFG))
def test_blank_fallback_listing_row_is_partial_and_skipped(ats, monkeypatch):
    _stub_minimum_rows(monkeypatch, ats, [{}])

    jobs, error = adapters.run_adapter(MINIMUM_ROW_CFG[ats])

    assert jobs == []
    assert error and error.startswith("partial:")
    assert ats in error


@pytest.mark.parametrize("bad_field", ("id", "title"))
@pytest.mark.parametrize("ats", tuple(MINIMUM_ROW_CFG))
def test_malformed_required_field_retains_valid_sibling(
        ats, bad_field, monkeypatch):
    good = {
        "id": "good-1", "title": "Program Manager",
        "url": "https://jobs.example/good-1",
    }
    bad = {
        "id": "bad-2", "title": "Program Manager II",
        "url": "https://jobs.example/bad-2",
    }
    bad[bad_field] = "   "
    _stub_minimum_rows(monkeypatch, ats, [good, bad])

    jobs, error = adapters.run_adapter(MINIMUM_ROW_CFG[ats])

    assert [job.external_id for job in jobs] == ["good-1"]
    assert jobs[0].title == "Program Manager"
    assert jobs[0].url.startswith("https://")
    assert error and error.startswith("partial:")
    assert f"{ats} listing row 2" in error


@pytest.mark.parametrize("ats", ("recruitee", "teamtailor"))
def test_provider_without_id_route_rejects_unactionable_url(ats, monkeypatch):
    _stub_minimum_rows(monkeypatch, ats, [{
        "id": "bad-1", "title": "Program Manager",
        "url": "javascript:alert(1)",
    }])

    jobs, error = adapters.run_adapter(MINIMUM_ROW_CFG[ats])

    assert jobs == []
    assert error and error.startswith("partial:")
    assert "actionable URL" in error


@pytest.mark.parametrize(
    "ats",
    tuple(name for name in MINIMUM_ROW_CFG
          if name not in {"recruitee", "teamtailor"}),
)
def test_provider_id_can_construct_a_safe_direct_url(ats, monkeypatch):
    _stub_minimum_rows(monkeypatch, ats, [{
        "id": "good-1", "title": "Program Manager",
    }])

    jobs, error = adapters.run_adapter(MINIMUM_ROW_CFG[ats])

    assert [job.external_id for job in jobs] == ["good-1"]
    assert jobs[0].url.startswith("https://")
    assert error is None
