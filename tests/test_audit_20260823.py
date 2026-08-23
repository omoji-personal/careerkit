"""Regressions from the 2026-08-23 public/private CareerKit audit."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

import careerkit
from engine import aggregators, coverage, ghost, http, store
from engine.models import Job


def _bind_home(monkeypatch, tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(careerkit, "PROFILE", profile / "profile.yaml")
    monkeypatch.setattr(careerkit, "EMPLOYERS", profile / "employers.yaml")
    monkeypatch.setattr(careerkit, "KEYS", profile / "keys.yaml")
    monkeypatch.setattr(careerkit, "TRACKER", profile / "tracker.md")
    monkeypatch.setattr(careerkit, "APPLICATIONS", profile / "applications.jsonl")
    monkeypatch.setattr(careerkit, "CLAIMS", profile / "claims.md")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    return profile


def test_fresh_privacy_acceptance_does_not_invent_optional_phase_decisions(
        tmp_path, monkeypatch, capsys):
    profile = _bind_home(monkeypatch, tmp_path)

    careerkit.cmd_privacy(argparse.Namespace(accept=True))

    capsys.readouterr()
    progress, issues = careerkit.onboarding_progress(profile / "setup-progress.md")
    assert not issues
    assert progress == {
        "Privacy": "complete",
        "Search Core": "pending",
        "First Win": "pending",
        "Source Expansion": "pending",
        "Career Pack": "pending",
        "Final Checks": "pending",
    }


def test_audit_scores_only_the_description_bytes_a_pull_can_store(monkeypatch):
    job = Job(
        company="Acme", title="CRM Lead", url="https://example.test/job/1",
        description="x" * (store.DESCRIPTION_LIMIT + 100), source="greenhouse",
    )
    observed = []
    monkeypatch.setattr(careerkit, "load_profile", lambda: object())
    monkeypatch.setattr(careerkit, "load_registry", lambda **_kwargs: {})
    monkeypatch.setattr(careerkit, "load_yaml", lambda *_args: {})
    monkeypatch.setattr(
        careerkit._pull, "fetch_all",
        lambda *_args, **_kwargs: {"jobs": [job]},
    )

    def fake_score(candidate, _profile):
        observed.append(len(candidate.description))
        candidate.gate = "EXCLUDED"
        candidate.reasons = ["fixture"]
        return candidate

    monkeypatch.setattr(careerkit, "score", fake_score)
    careerkit.cmd_audit(argparse.Namespace(
        grep=None, no_cache=False, employers=False, samples=1,
    ))

    assert observed == [store.DESCRIPTION_LIMIT]


def test_coverage_gap_carries_failure_count_without_copying_provider_error(
        monkeypatch):
    monkeypatch.setattr(coverage.adapters, "REGISTRY", {"greenhouse": object()})
    monkeypatch.setattr(coverage.adapters, "PAGE_CEILING", {})
    registry = {
        "employers": [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}],
        "feeds": [],
    }
    result = coverage.build_coverage_ledger(registry, [{
        "source": "greenhouse:acme", "last_count": 4,
        "last_error": "partial: private upstream text", "consecutive_failures": 7,
    }])

    gap = result["health_gap_sources"][0]
    assert gap["consecutive_failures"] == 7
    assert "private upstream text" not in str(result)


def test_stale_unfinished_run_is_found_even_after_a_later_run_finishes(tmp_path):
    previous = store.DB_PATH
    try:
        store.DB_PATH = tmp_path / "jobs.db"
        con = store.connect()
        old = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        con.execute(
            "INSERT INTO runs(started, state) VALUES (?, 'running:999999')", (old,)
        )
        later = store.start_run(con)
        store.finish_run(con, later, 0, 0, 0, {})

        stale = careerkit._stale_unfinished_runs(con)

        assert len(stale) == 1
        assert stale[0]["state"] == "running:999999"
    finally:
        store.DB_PATH = previous


def test_career_pack_checkpoint_requires_every_named_destination(
        tmp_path, monkeypatch):
    profile = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        "identity: {name: Example}\nwork_auth: {authorized_us: true}\n"
        "eeo: {policy: decline}\nautonomy: {submit: ask_each}\n",
        encoding="utf-8",
    )
    careerkit.CLAIMS.write_text("verified claim\n", encoding="utf-8")
    careerkit.TRACKER.write_text("# APPLIED\n", encoding="utf-8")
    (profile / "person.md").write_text("context\n", encoding="utf-8")
    raw = careerkit.load_yaml(careerkit.PROFILE, {})

    assert careerkit._career_pack_missing(raw) == ["style.md"]


def test_delisted_rows_are_not_described_as_live_ghosts(tmp_path):
    previous = store.DB_PATH
    try:
        store.DB_PATH = tmp_path / "jobs.db"
        con = store.connect()
        job = Job(
            company="Acme", title="CRM Lead", url="https://feed.test/job/1",
            source="feed:example", external_id="one",
        )
        job.gate = "VERIFY"
        job.url_direct = ""
        job.company_site = ""
        store.upsert(con, [job])
        con.execute(
            "UPDATE jobs SET delisted_on='2026-08-20' WHERE uid=?", (job.uid,)
        )
        con.commit()

        assert ghost.review(con) == []
    finally:
        store.DB_PATH = previous


def test_verify_rechecks_legacy_free_text_states_but_not_terminal_states(
        monkeypatch, capsys):
    registry = {
        "employers": [
            {"name": "Legacy", "ats": "lever", "slug": "legacy",
             "verified": "board responded; ownership not verifiable"},
            {"name": "Reviewed", "ats": "lever", "slug": "reviewed",
             "verified": "review"},
        ],
        "feeds": [],
    }
    checked = []
    monkeypatch.setattr(careerkit, "load_registry", lambda **_kwargs: registry)
    monkeypatch.setattr(careerkit, "save_employers", lambda _registry: None)

    def verify(entry):
        checked.append(entry["name"])
        entry["verified"] = "review"

    monkeypatch.setattr(careerkit, "verify_entry", verify)
    careerkit.cmd_verify(argparse.Namespace(all=False))

    capsys.readouterr()
    assert checked == ["Legacy"]


def test_doctor_reports_application_ledger_drift_as_privacy_safe_counts(
        tmp_path, monkeypatch, capsys):
    profile = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        (Path(__file__).resolve().parent.parent / "profile.example" / "profile.yaml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    careerkit.EMPLOYERS.write_text(
        yaml.safe_dump({
            "employers": [{
                "name": "Acme", "ats": "greenhouse", "slug": "acme",
                "active": True,
            }],
            "feeds": [{"name": "freehire", "active": False}],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (profile / "setup-progress.md").write_text(
        "Privacy: complete\nSearch Core: complete\nFirst Win: skipped\n"
        "Source Expansion: deferred\nCareer Pack: deferred\nFinal Checks: complete\n",
        encoding="utf-8",
    )
    careerkit.APPLICATIONS.write_text(
        '{"company":"Private Employer","title":"Private Role",'
        '"status":"declined"}\n',
        encoding="utf-8",
    )
    con = store.connect()
    store.record_health(con, "greenhouse:acme", 0, None)
    run_id = store.start_run(con)
    store.finish_run(con, run_id, 0, 0, 0, {})

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_doctor(argparse.Namespace())

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "application evidence needs review" in output
    assert "1 invalid" in output
    assert "Private Employer" not in output
    assert "Private Role" not in output


def test_doctor_does_not_report_already_synchronized_application_evidence(
        tmp_path, monkeypatch, capsys):
    profile = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        (Path(__file__).resolve().parent.parent / "profile.example" / "profile.yaml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    careerkit.EMPLOYERS.write_text(
        yaml.safe_dump({
            "employers": [{
                "name": "Acme", "ats": "greenhouse", "slug": "acme",
                "active": True,
            }],
            "feeds": [{"name": "freehire", "active": False}],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (profile / "setup-progress.md").write_text(
        "Privacy: complete\nSearch Core: complete\nFirst Win: skipped\n"
        "Source Expansion: deferred\nCareer Pack: deferred\nFinal Checks: complete\n",
        encoding="utf-8",
    )
    careerkit.APPLICATIONS.write_text(
        '{"company":"Acme","title":"CRM Lead","status":"applied",'
        '"url":"https://example.test/acme/1","on":"2026-08-20"}\n',
        encoding="utf-8",
    )
    con = store.connect()
    job = Job(
        company="Acme", title="CRM Lead", url="https://example.test/acme/1",
        source="greenhouse", external_id="one",
    )
    job.gate = "QUALIFIED"
    store.upsert(con, [job])
    store.record_application_stage(
        con, job.uid, "applied", on="2026-08-20",
        detail="2026-08-20 from applications.jsonl",
    )
    store.record_health(con, "greenhouse:acme", 1, None)
    run_id = store.start_run(con)
    store.finish_run(con, run_id, 1, 0, 1, {})

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_doctor(argparse.Namespace())

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "application evidence needs review" not in output
    assert "no blocking problems found" in output


def test_doctor_requires_the_exact_application_stage_event(
        tmp_path, monkeypatch, capsys):
    profile = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        (Path(__file__).resolve().parent.parent / "profile.example" / "profile.yaml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    careerkit.EMPLOYERS.write_text(
        yaml.safe_dump({
            "employers": [{
                "name": "Acme", "ats": "greenhouse", "slug": "acme",
                "active": True,
            }],
            "feeds": [{"name": "freehire", "active": False}],
        }, sort_keys=False),
        encoding="utf-8",
    )
    (profile / "setup-progress.md").write_text(
        "Privacy: complete\nSearch Core: complete\nFirst Win: skipped\n"
        "Source Expansion: deferred\nCareer Pack: deferred\nFinal Checks: complete\n",
        encoding="utf-8",
    )
    careerkit.APPLICATIONS.write_text(
        '{"company":"Acme","title":"CRM Lead","status":"interviewing",'
        '"url":"https://example.test/acme/2","on":"2026-08-21"}\n',
        encoding="utf-8",
    )
    con = store.connect()
    job = Job(
        company="Acme", title="CRM Lead", url="https://example.test/acme/2",
        source="greenhouse", external_id="two",
    )
    job.gate = "QUALIFIED"
    store.upsert(con, [job])
    # The coarse row status is already right, but the decision-bearing stage
    # event is missing. `applied --apply` would still make a real change.
    store.set_status(con, job.uid, "applied")
    store.record_health(con, "greenhouse:acme", 1, None)
    run_id = store.start_run(con)
    store.finish_run(con, run_id, 1, 0, 1, {})

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_doctor(argparse.Namespace())

    assert exc.value.code == 1
    assert "1 unsynchronized safe match" in capsys.readouterr().out

    store.record_application_stage(
        con, job.uid, "interviewing", on="2026-08-21",
        detail="2026-08-21 from applications.jsonl",
    )
    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_doctor(argparse.Namespace())
    assert exc.value.code == 0
    assert "application evidence needs review" not in capsys.readouterr().out


def test_feed_page_budgets_are_configurable_and_current(monkeypatch):
    urls = []

    def fake_fetch_json(url):
        urls.append(url)
        http._local.last_status = 200
        http._local.last_parse_ok = True
        if "himalayas" in url:
            return {"jobs": [], "totalCount": 0}
        if "arbeitnow" in url:
            return {"data": [], "meta": {"last_page": 2}}
        if "jobicy" in url:
            return {"jobs": []}
        return {"results": [], "page_count": 0}

    monkeypatch.setattr(aggregators, "TERMS", ("crm",))
    monkeypatch.setattr(aggregators, "fetch_json", fake_fetch_json)

    assert aggregators.run_feed("himalayas", {"pages": 3}) == ([], None)
    assert aggregators.run_feed("arbeitnow", {"pages": 2}) == ([], None)
    assert aggregators.run_feed("jobicy", {}) == ([], None)
    assert aggregators.run_feed(
        "themuse", {"pages": 3, "categories": ["IT"], "locations": []}
    ) == ([], None)
    assert any("jobs/api/search?q=crm&page=1" in url for url in urls)
    assert not any("offset=" in url for url in urls if "himalayas" in url)
    assert any("jobicy.com/api/v2/remote-jobs?count=100&tag=crm" in url
               for url in urls)


@pytest.mark.parametrize("value", [0, 21, True, "bad"])
def test_feed_page_budgets_reject_invalid_values(value):
    with pytest.raises(ValueError):
        aggregators._bounded_pages(
            {"pages": value}, feed_name="fixture", default=2, maximum=20
        )


def test_quiet_repositories_still_rerun_security_and_dependency_updates():
    root = Path(__file__).resolve().parent.parent
    workflow = (root / ".github" / "workflows" / "test.yml").read_text(
        encoding="utf-8"
    )
    dependabot = (root / ".github" / "dependabot.yml").read_text(
        encoding="utf-8"
    )

    assert "schedule:" in workflow and "cron:" in workflow
    for ecosystem in ("pip", "npm", "github-actions"):
        assert f"package-ecosystem: {ecosystem}" in dependabot
