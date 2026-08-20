"""Regression coverage for the 2026-08-20 release hardening.

Every test here reproduces a silent failure in the released engine.  Keep the
tests isolated from the user's profile and database; nothing in this module
uses the network.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERKIT_HOME", str(tmp_path))
    from engine import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    return store.connect()


def _profile():
    import re

    from engine.score import Profile

    profile = Profile()
    profile.lanes = [(50, re.compile(r"product manager", re.I), "pm")]
    profile.domain_terms = None
    return profile


@pytest.mark.parametrize(
    "fields, expected_band, expected_source",
    [
        ({"comp_min": 120_000, "comp_max": 150_000}, (120_000, 150_000), "board"),
        ({"comp_text": "Base salary range: $121,000 - $151,000"},
         (121_000, 151_000), "board"),
        ({"description": "The salary range for this role is $122,000 - $152,000."},
         (122_000, 152_000), "body"),
        ({"description": "Competitive pay and benefits."}, (None, None), "absent"),
    ],
)
def test_compensation_records_where_the_evidence_came_from(
        fields, expected_band, expected_source):
    """A displayed band did not say whether the board supplied it or the scorer
    inferred it from prose, even though those are materially different claims."""
    from engine.models import Job
    from engine.score import extract_comp

    job = Job(company="Acme", title="Product Manager", url="https://example.test/1",
              source="greenhouse", **fields)
    assert extract_comp(job) == expected_band
    assert job.comp_source == expected_source


def test_comp_provenance_survives_storage_rescore_and_the_report(
        db, tmp_path, monkeypatch):
    """Provenance is useful only if it survives every layer that carries comp."""
    from engine import consistency, pull, report, store
    from engine.models import Job
    from engine.score import score

    job = Job(
        company="Acme",
        title="Product Manager",
        url="https://example.test/body-band",
        source="greenhouse",
        location="Remote, US",
        description=("The salary range for this role is $130,000 - $160,000 per year. "
                     + "d" * 400),
        external_id="body-band",
    )
    score(job, _profile())
    store.upsert(db, [job])

    stored = db.execute("SELECT * FROM jobs WHERE uid=?", (job.uid,)).fetchone()
    assert stored["comp_source"] == "body"
    rebuilt = pull.job_from_row(stored)
    score(rebuilt, _profile())
    assert rebuilt.comp_source == "body"

    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    path = report.write_report(
        db, [stored], health=[], run_detail={"pulled": 1, "sources_ok": 1},
        filename="provenance.md",
    )
    assert "$130,000 - $160,000 (parsed from body)" in path.read_text()
    assert consistency.check_report(db, path) == []


def test_a_demoted_resighting_refreshes_comp_and_its_provenance(db):
    """Demoted sightings bypass upsert.  Their fresh salary values and source
    were discarded, so the stored row described an older version of the posting
    while carrying a verdict computed from the new one."""
    from engine import store
    from engine.models import Job
    from engine.score import extract_comp

    old = Job(company="Acme", title="Product Manager",
              url="https://example.test/demoted", source="greenhouse",
              external_id="demoted", comp_min=100_000, comp_max=120_000)
    extract_comp(old)
    old.gate, old.score, old.reasons = "QUALIFIED", 60, ["old"]
    store.upsert(db, [old])

    fresh = Job(company="Acme", title="Product Manager",
                url="https://example.test/demoted", source="greenhouse",
                external_id="demoted", comp_min=150_000, comp_max=180_000)
    extract_comp(fresh)
    fresh.gate, fresh.score, fresh.reasons = "SLOT-BLOCKED", 35, ["new floor"]
    store.reconcile(db, {fresh.uid: fresh}, {("greenhouse", "Acme")}, set())

    row = db.execute("SELECT comp_min, comp_max, comp_source FROM jobs WHERE uid=?",
                     (fresh.uid,)).fetchone()
    assert tuple(row) == (150_000, 180_000, "board")


def test_reports_and_exports_do_not_silently_stop_at_300_rows(
        db, tmp_path, monkeypatch):
    """The released report queried LIMIT 300 without disclosing the cap.  Row
    301 and everything below it disappeared from Markdown, JSON, CSV and HTML."""
    from engine import pull, report

    today = "2026-08-20"
    rows = [
        (
            f"uid-{i:03d}", f"group-{i:03d}", "greenhouse:acme", "Acme",
            f"Product Manager {i:03d}", f"https://example.test/role/{i:03d}",
            "Remote, US", "greenhouse", "pm", "", today, "", None, None,
            "", "absent", 40, "VERIFY", "NEEDS CHECK: comp unstated", "d" * 400,
            today, today, 1, 1, 1, None, 0, "", "", "",
        )
        for i in range(305)
    ]
    db.executemany(
        "INSERT INTO jobs (uid,group_key,board,company,title,url,location,source,lane,"
        "employer_tier,posted_at,department,comp_min,comp_max,comp_text,comp_source,"
        "score,gate,reasons,description,first_seen,last_seen,first_seen_run,last_seen_run,"
        "seen_count,remote_flag,rails_exempt,url_direct,company_site,registry_lane) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()
    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")

    path = pull.rebuild_report(db, echo=lambda *args: None)
    text = path.read_text()
    assert text.count("https://example.test/role/") == 305
    assert "https://example.test/role/304" in text


def test_engine_checkout_diagnostics_detect_dirty_and_nondefault_branches(tmp_path):
    """Instances execute whichever engine checkout happens to be on disk.  A
    branch switch or uncommitted edit changed the live tool without doctor
    saying so."""
    import careerkit

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "CareerKit Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"],
                   cwd=repo, check=True)
    tracked = repo / "engine.py"
    tracked.write_text("released = True\n")
    subprocess.run(["git", "add", "--", "engine.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=repo, check=True)

    assert careerkit.engine_checkout_notes(repo) == []

    subprocess.run(["git", "switch", "-q", "-c", "feature"], cwd=repo, check=True)
    tracked.write_text("released = False\n")
    notes = careerkit.engine_checkout_notes(repo)
    assert any("feature" in note and "default branch main" in note for note in notes)
    assert any("uncommitted" in note for note in notes)


def test_guide_command_inventory_matches_cli_help(tmp_path):
    """The PDF claimed to reference every command while silently omitting eight
    commands added after its last rebuild."""
    env = dict(os.environ, CAREERKIT_HOME=str(tmp_path))
    result = subprocess.run(
        [sys.executable, str(ROOT / "careerkit.py"), "--help"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    match = re.search(r"\{([^}]+)\}", result.stdout, re.S)
    assert match, result.stdout
    commands = {item.strip() for item in match.group(1).replace("\n", "").split(",")}
    guide = (ROOT / "guide" / "careerkit-guide.html").read_text()
    missing = sorted(command for command in commands
                     if f"./careerkit.py {command}" not in guide)
    assert missing == []


def test_guide_builder_uses_a_locked_playwright_browser():
    """A ranged library plus whichever system Chrome happened to be installed
    could not substantiate the guide's reproducibility claim."""
    package = (ROOT / "package.json").read_text()
    lock = (ROOT / "package-lock.json").read_text()
    builder = (ROOT / "guide" / "build-pdf.mjs").read_text()
    assert '"playwright": "1.62.1"' in package
    assert '"playwright": "1.62.1"' in lock
    assert "executablePath" not in builder
    assert "page.route(/^https?" in builder
    assert "guide builds must be offline" in builder
    assert "Guide build ${id}" in builder
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "committed-guide.txt" in workflow
    assert "Careerkit-Guide.pdf is stale" in workflow


def test_guide_ci_uses_source_build_marker_as_its_freshness_contract():
    """PDF text layout is platform-specific, so CI must compare the stable
    source-derived marker instead of requiring two extractions to be identical."""
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert 'marker = re.compile(r"\\bGuide build ([0-9a-f]{12})\\b")' in workflow
    assert 'committed_id = inspect("committed-guide.txt")' in workflow
    assert 'generated_id = inspect("generated-guide.txt")' in workflow
    assert "if committed_id != generated_id:" in workflow
    assert workflow.count("pdfinfo guide/Careerkit-Guide.pdf") == 2
    for critical_text in (
        "stops at submit for your review",
        "never relay codes",
        "What leaves your machine",
        "git pull && ./setup.sh",
    ):
        assert critical_text in workflow
    assert 'committed = " ".join' not in workflow
    assert "if committed != generated:" not in workflow


def test_actions_are_sha_pinned_to_node24_generations():
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
        encoding="utf-8"
    )
    uses = re.findall(r"^\s*- uses:\s+(\S+)", workflow, re.MULTILINE)
    assert uses
    assert all(re.search(r"@[0-9a-f]{40}$", item) for item in uses)
    assert "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09" in uses
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in uses
    assert "actions/setup-node@a0853c24544627f65ddf259abe73b1d18a591444" in uses


def test_every_instance_update_path_reapplies_setup():
    command = "git pull && ./setup.sh"
    assert command in (ROOT / "README.md").read_text()
    assert "git pull &amp;&amp; ./setup.sh" in (
        ROOT / "guide" / "careerkit-guide.html"
    ).read_text()
    assert command in (ROOT / "new-instance.sh").read_text()
