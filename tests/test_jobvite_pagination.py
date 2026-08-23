"""Offline completeness regressions for bounded source pagination."""
from __future__ import annotations

import math
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from engine import adapters, aggregators


JOBVITE_CFG = {"ats": "jobvite", "slug": "acme", "name": "Acme"}


def _jobvite_page(
    ids: list[int] | range,
    *,
    first: int,
    last: int,
    total: int,
) -> str:
    links = "\n".join(
        f'<a href="/acme/job/id{job_id}">Role {job_id}</a>'
        for job_id in ids
    )
    return (
        '<body class="jv-page-search">'
        f"{links}"
        f'<div class="jv-pagination-text">{first}-{last} of {total}</div>'
        "</body>"
    )


def _stub_jobvite(monkeypatch, responses: dict[int, tuple[int, str]]):
    calls: list[int] = []

    def fake_fetch(url: str, *_args, **_kwargs):
        query = parse_qs(urlsplit(url).query)
        assert "p" in query, url
        page = int(query["p"][0])
        calls.append(page)
        if page not in responses:
            raise AssertionError(f"unexpected Jobvite page {page}")
        return responses[page]

    monkeypatch.setattr(adapters, "fetch", fake_fetch)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))
    return calls


def _three_jobvite_pages() -> dict[int, tuple[int, str]]:
    return {
        0: (200, _jobvite_page(range(1, 51), first=1, last=50, total=128)),
        1: (200, _jobvite_page(range(51, 101), first=51, last=100, total=128)),
        2: (200, _jobvite_page(range(101, 129), first=101, last=128, total=128)),
    }


def test_jobvite_default_fetches_all_three_pages_and_128_unique_roles(monkeypatch):
    calls = _stub_jobvite(monkeypatch, _three_jobvite_pages())

    jobs, error = adapters.run_adapter(JOBVITE_CFG)

    assert error is None
    assert calls == [0, 1, 2]
    assert len(jobs) == 128
    assert len({job.external_id for job in jobs}) == 128
    assert len({job.uid for job in jobs}) == 128
    assert jobs[0].external_id == "id1"
    assert jobs[-1].external_id == "id128"


def test_jobvite_page_failure_retains_prior_rows_and_reports_partial(monkeypatch):
    responses = _three_jobvite_pages()
    responses[1] = (503, "temporarily unavailable")
    calls = _stub_jobvite(monkeypatch, responses)

    jobs, error = adapters.run_adapter(JOBVITE_CFG)

    assert calls == [0, 1]
    assert len(jobs) == 50
    assert error and error.startswith("partial:")
    assert "listing page 2 returned HTTP 503" in error
    assert "50 of 128 advertised postings" in error


def test_jobvite_exact_page_replay_stops_without_duplicate_rows(monkeypatch):
    first = _three_jobvite_pages()[0][1]
    calls = _stub_jobvite(monkeypatch, {0: (200, first), 1: (200, first)})

    jobs, error = adapters.run_adapter(JOBVITE_CFG)

    assert calls == [0, 1]
    assert len(jobs) == 50
    assert error and error.startswith("partial:")
    assert "listing page 2 replayed an earlier page" in error
    assert "50 of 128 advertised postings" in error


def test_jobvite_reordered_replay_stops_on_no_unique_progress(monkeypatch):
    responses = {
        0: (200, _jobvite_page(range(1, 51), first=1, last=50, total=128)),
        1: (200, _jobvite_page(
            list(reversed(range(1, 51))), first=51, last=100, total=128
        )),
    }
    calls = _stub_jobvite(monkeypatch, responses)

    jobs, error = adapters.run_adapter(JOBVITE_CFG)

    assert calls == [0, 1]
    assert len(jobs) == 50
    assert error and error.startswith("partial:")
    assert "repeated 50 identities" in error
    assert "made no unique progress" in error


def test_jobvite_global_dedupe_retains_new_rows_from_overlapping_page(monkeypatch):
    responses = {
        0: (200, _jobvite_page(range(1, 51), first=1, last=50, total=75)),
        1: (200, _jobvite_page(range(50, 76), first=51, last=75, total=75)),
    }
    calls = _stub_jobvite(monkeypatch, responses)

    jobs, error = adapters.run_adapter(JOBVITE_CFG)

    assert calls == [0, 1]
    assert len(jobs) == 75
    assert len({job.external_id for job in jobs}) == 75
    assert error and error.startswith("partial:")
    assert "repeated 1 identities from earlier pages" in error


def test_jobvite_total_change_is_partial_but_all_rows_survive(monkeypatch):
    responses = _three_jobvite_pages()
    responses[1] = (200, _jobvite_page(
        range(51, 101), first=51, last=100, total=127
    ))
    calls = _stub_jobvite(monkeypatch, responses)

    jobs, error = adapters.run_adapter(JOBVITE_CFG)

    assert calls == [0, 1, 2]
    assert len(jobs) == 128
    assert error and error.startswith("partial:")
    assert "advertised total changed from 128 to 127" in error


def test_jobvite_impossible_total_retains_rows_and_reports_contradiction(monkeypatch):
    calls = _stub_jobvite(monkeypatch, {
        0: (200, _jobvite_page([1, 2], first=1, last=2, total=1)),
    })

    jobs, error = adapters.run_adapter(JOBVITE_CFG)

    assert calls == [0]
    assert [job.external_id for job in jobs] == ["id1", "id2"]
    assert error and error.startswith("partial:")
    assert "pagination text was internally inconsistent" in error
    assert "above advertised total 1" in error


def test_jobvite_configured_cap_is_explicit_and_preserves_covered_rows(monkeypatch):
    calls = _stub_jobvite(monkeypatch, _three_jobvite_pages())

    jobs, error = adapters.run_adapter({**JOBVITE_CFG, "pages": 2})

    assert calls == [0, 1]
    assert len(jobs) == 100
    assert error and error.startswith("capped:")
    assert "2-page safety cap" in error
    assert "100 of 128 advertised postings" in error


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        0,
        101,
        1.5,
        1.9,
        math.nan,
        math.inf,
        -math.inf,
        "1.5",
        "1.9",
        "NaN",
        "inf",
        "2e0",
        "bad",
    ],
)
def test_jobvite_page_budget_rejects_lossy_or_nonfinite_values(monkeypatch, value):
    monkeypatch.setattr(
        adapters,
        "fetch",
        lambda *_args, **_kwargs: pytest.fail("invalid config made a request"),
    )

    jobs, error = adapters.run_adapter({**JOBVITE_CFG, "pages": value})

    assert jobs == []
    assert error == "ValueError: jobvite pages must be a whole number from 1 to 100"


@pytest.mark.parametrize("value", [3, 3.0, "3", "3.0", "+3", "3."])
def test_jobvite_page_budget_accepts_whole_numeric_values(monkeypatch, value):
    calls = _stub_jobvite(monkeypatch, {
        0: (200, _jobvite_page([1], first=1, last=1, total=1)),
    })

    jobs, error = adapters.run_adapter({**JOBVITE_CFG, "pages": value})

    assert calls == [0]
    assert [job.external_id for job in jobs] == ["id1"]
    assert error is None


@pytest.mark.parametrize(
    "value",
    [True, 1.5, 1.9, math.nan, math.inf, "1.5", "1.9", "NaN", "inf"],
)
def test_shared_feed_page_budget_rejects_lossy_values(value):
    with pytest.raises(ValueError, match="must be a whole number"):
        aggregators._bounded_pages(
            {"pages": value}, feed_name="fixture", default=2, maximum=20
        )


def test_arbeitnow_stops_at_one_based_provider_last_page(monkeypatch):
    calls: list[int] = []

    def fake_fetch_json(url: str, **_kwargs):
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        calls.append(page)
        if page > 2:
            raise AssertionError(f"requested out-of-range Arbeitnow page {page}")
        return {
            "data": [{
                "slug": f"role-{page}",
                "title": f"Role {page}",
                "company_name": "Acme",
                "url": f"https://example.test/arbeitnow/{page}",
            }],
            "meta": {"last_page": 2},
            # A stale link must not override the provider's numeric endpoint.
            "links": {"next": "https://example.test/stale-next"},
        }

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)

    jobs, error = aggregators.run_feed("arbeitnow", {"pages": 5})

    assert calls == [1, 2]
    assert [job.external_id for job in jobs] == ["role-1", "role-2"]
    assert error is None


def test_themuse_stops_at_zero_based_provider_page_count(monkeypatch):
    calls: list[int] = []

    def fake_fetch_json(url: str, **_kwargs):
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        calls.append(page)
        if page > 1:
            raise AssertionError(f"requested out-of-range The Muse page {page}")
        return {
            "results": [{
                "id": page + 1,
                "name": f"Role {page + 1}",
                "company": {"name": "Acme"},
                "refs": {"landing_page": f"https://example.test/muse/{page + 1}"},
                "locations": [{"name": "Remote"}],
                "contents": "Role description",
                "categories": [{"name": "IT"}],
            }],
            "page_count": 2,
        }

    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)

    jobs, error = aggregators.run_feed(
        "themuse",
        {"pages": 5, "categories": ["IT"], "locations": []},
    )

    assert calls == [0, 1]
    assert [job.external_id for job in jobs] == ["1", "2"]
    assert error is None
