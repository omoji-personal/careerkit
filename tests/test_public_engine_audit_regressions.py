"""Regressions from the 2026-08-23 public-engine audit.

All cases are isolated: no network and no user instance data.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERKIT_HOME", str(tmp_path))
    from engine import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    con = store.connect()
    yield con
    con.close()


def _job(*, external_id: str, gate: str = "QUALIFIED", score: int = 70):
    from engine.models import Job

    job = Job(
        company="Acme",
        title="Salesforce Technical Consultant",
        url=f"https://jobs.example.test/{external_id}",
        source="greenhouse",
        external_id=external_id,
        location=f"Location {external_id}",
        description="A sufficiently detailed posting body. " * 20,
    )
    job.gate, job.score, job.reasons = gate, score, ["audit regression"]
    return job


def test_workday_location_bullets_do_not_collapse_distinct_paths(monkeypatch):
    from engine import adapters

    payload = {
        "total": 2,
        "jobPostings": [
            {
                "title": "Salesforce Technical Consultant",
                "externalPath": "/job/Swindon/consultant_R-100",
                "bulletFields": ["Swindon"],
                "locationsText": "Swindon",
            },
            {
                "title": "Salesforce Technical Consultant",
                "externalPath": "/job/London/consultant_R-200",
                "bulletFields": ["Swindon"],
                "locationsText": "London",
            },
        ],
    }
    monkeypatch.setattr(
        adapters,
        "fetch",
        lambda *_args, **_kwargs: (200, json.dumps(payload)),
    )
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(r"a^"))

    jobs, error = adapters.run_adapter({
        "ats": "workday",
        "name": "Acme",
        "tenant": "acme",
        "dc": "wd1",
        "site": "External",
    })

    assert error is None
    assert len(jobs) == 2
    assert len({job.url for job in jobs}) == 2
    assert len({job.uid for job in jobs}) == 2
    assert {job.external_id for job in jobs} == {
        "/job/Swindon/consultant_R-100",
        "/job/London/consultant_R-200",
    }


@pytest.mark.parametrize(
    "location,description",
    [
        ("Remote - Canada; not available in the United States", "Remote role."),
        ("Remote - United States", "This role is not available in the United States."),
        ("Remote - United States", "This role is open everywhere except the US."),
        ("Remote - United States", "This opening excludes U.S. applicants."),
        ("Remote - United States", "U.S. applicants cannot apply."),
        ("Remote - United States", "Candidates in the United States are not eligible for this role."),
        ("Remote - United States", "We cannot hire candidates from the US."),
        (
            "Remote - Canada",
            "This job is available in Canada, but not in the United States.",
        ),
        ("Remote - United States", "This role isn't available in the United States."),
        ("Remote - United States", "Not open to US-based applicants."),
        (
            "Remote - United States",
            "We do not employ people located within the United States.",
        ),
    ],
)
def test_explicit_us_exclusions_never_become_positive_evidence(
    location, description,
):
    from engine.models import Job
    from engine.score import Profile, location_verdict

    job = Job(
        company="Acme",
        title="Salesforce Administrator",
        url="https://example.test/job",
        source="greenhouse",
        location=location,
        description=description,
    )
    verdict, why = location_verdict(job, Profile())
    assert verdict == "fail", why
    assert "eligibility excluded" in why


@pytest.mark.parametrize(
    "description",
    [
        "This benefit is not available in the United States.",
        "Our product is not available in the United States.",
        "PTO is not available in the United States.",
        "Our markets exclude the United States.",
        "Company calendars are configured excluding US holidays.",
        "This team serves every segment except the United States federal market.",
        "U.S. applicants cannot apply for relocation benefits.",
    ],
)
def test_non_job_us_exclusions_do_not_reject_a_us_remote_role(description):
    from engine.models import Job
    from engine.score import Profile, location_verdict

    job = Job(
        company="Acme",
        title="Salesforce Administrator",
        url="https://example.test/job",
        source="greenhouse",
        location="Remote - United States",
        description=description,
    )
    verdict, why = location_verdict(job, Profile())
    assert verdict == "pass", why


def test_us_and_canada_remains_valid_multi_region_evidence():
    from engine.models import Job
    from engine.score import Profile, location_verdict

    job = Job(
        company="Acme",
        title="Salesforce Administrator",
        url="https://example.test/job",
        source="greenhouse",
        location="Remote - US, Canada",
        description="Remote role open to candidates in the US or Canada.",
    )
    assert location_verdict(job, Profile())[0] == "pass"


@pytest.mark.parametrize("region", ("South America", "Central America", "Latin America"))
def test_non_us_america_regions_are_residency_restrictions(region):
    from engine.models import Job
    from engine.score import Profile, location_verdict, residency_restriction

    profile = Profile()
    sentence = f"Candidates must be based in {region} to be considered."
    assert residency_restriction(sentence, profile) is not None

    job = Job(
        company="Acme",
        title="Salesforce Administrator",
        url="https://example.test/job",
        source="greenhouse",
        location="Remote - United States",
        description=sentence,
    )
    assert location_verdict(job, profile)[0] == "unknown"


def test_united_states_of_america_remains_country_level_eligibility():
    from engine.score import Profile, residency_restriction

    assert residency_restriction(
        "Candidates must be based in the United States of America.",
        Profile(),
    ) is None


def test_report_renders_and_checks_every_qualified_sibling(
    db, tmp_path, monkeypatch,
):
    from engine import consistency, report, store

    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    jobs = [_job(external_id=f"req-{index}") for index in range(13)]
    store.upsert(db, jobs)
    path = report.write_report(
        db,
        store.query(db),
        health=[],
        run_detail={"pulled": 13, "sources_ok": 1},
        filename="siblings.md",
    )

    text = path.read_text()
    for job in jobs:
        assert f"**UID** `{job.uid}`" in text
    assert "...and 1 more" not in text
    assert consistency.check_report(db, path) == []

    missing = jobs[-1].uid
    lines = text.splitlines()
    line_index = next(i for i, line in enumerate(lines) if missing in line)
    del lines[line_index:line_index + 2]
    path.write_text("\n".join(lines))
    problems = consistency.check_report(db, path)
    assert any(missing in problem and "omits active uid" in problem
               for problem in problems), problems


def test_compact_verify_tail_renders_and_checks_every_sibling(
    db, tmp_path, monkeypatch,
):
    from engine import consistency, report, store

    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    jobs = [
        _job(external_id=f"verify-{index}", gate="VERIFY", score=40)
        for index in range(3)
    ]
    store.upsert(db, jobs)
    path = report.write_report(
        db,
        store.query(db),
        health=[],
        run_detail={"pulled": 3, "sources_ok": 1},
        filename="tail-siblings.md",
    )

    text = path.read_text()
    for job in jobs:
        assert f"**UID** `{job.uid}`" in text
    assert consistency.check_report(db, path) == []


def test_discovery_persists_only_finite_verification_states(monkeypatch):
    from engine import discover

    monkeypatch.setattr(discover, "slug_candidates", lambda *_args, **_kwargs: ["acme"])
    monkeypatch.setitem(discover.PROBES, "lever", lambda _slug: 3)
    monkeypatch.setattr(
        discover,
        "verify_board",
        lambda *_args, **_kwargs: (
            True,
            "platform does not publish a board name; not verifiable",
        ),
    )
    calls = []

    def richer_verify(entry):
        calls.append(entry.copy())
        entry["verified"] = "review"
        entry["verify_note"] = "board states no company name"
        return entry

    monkeypatch.setattr(discover, "verify_entry", richer_verify)
    monkeypatch.setattr(discover, "discover_workday", lambda _name: None)

    entry = discover.discover_company(
        "Acme", platforms=["lever"], max_slugs=1, workers=1)

    assert calls
    assert entry is not None
    assert entry["verified"] == "review"
    assert entry["verify_note"] == "board states no company name"
    assert entry["verified"] in {"ok", "review", "bad"}


def test_lightweight_name_match_uses_ok_state_and_keeps_evidence(monkeypatch):
    from engine import discover

    monkeypatch.setattr(discover, "slug_candidates", lambda *_args, **_kwargs: ["acme"])
    monkeypatch.setitem(discover.PROBES, "greenhouse", lambda _slug: 2)
    monkeypatch.setattr(
        discover,
        "verify_board",
        lambda *_args, **_kwargs: (True, "board reports itself as 'Acme'"),
    )

    entry = discover.discover_company(
        "Acme", platforms=["greenhouse"], max_slugs=1, workers=1)

    assert entry is not None
    assert entry["verified"] == "ok"
    assert entry["verify_note"] == "board reports itself as 'Acme'"
