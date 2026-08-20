"""Regression tests for CLI, install, and instance-boundary failures."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 CI
    import tomli as tomllib

import pytest
import yaml

import careerkit


REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    command = list(args)
    if os.name == "nt" and command[0].endswith(".sh"):
        command.insert(0, BASH)
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run("git", *args, cwd=cwd)


def test_source_checkout_guard_rejects_partial_python_install(tmp_path):
    with pytest.raises(SystemExit, match="complete cloned checkout"):
        careerkit.require_source_checkout(tmp_path)

    (tmp_path / ".claude/skills/setup").mkdir(parents=True)
    for relative in ("CLAUDE.md", "setup.sh", ".claude/skills/setup/SKILL.md"):
        (tmp_path / relative).touch()
    careerkit.require_source_checkout(tmp_path)


def test_engine_checkout_notes_ignore_an_ancestor_repository(tmp_path):
    assert _git(tmp_path, "init", "-q").returncode == 0
    child = tmp_path / "installed-package"
    child.mkdir()
    assert careerkit.engine_checkout_notes(child) == []


def test_engine_checkout_notes_report_unknown_git_status(tmp_path, monkeypatch):
    assert _git(tmp_path, "init", "-q").returncode == 0
    original = subprocess.run

    def fail_status(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "-C", str(tmp_path)] and "status" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "simulated status failure")
        return original(cmd, *args, **kwargs)

    monkeypatch.setattr(careerkit.subprocess, "run", fail_status)
    notes = careerkit.engine_checkout_notes(tmp_path)
    assert any("status could not be inspected" in note for note in notes)


def test_registry_accepts_known_adapter_addresses_and_feed(tmp_path, monkeypatch):
    registry = tmp_path / "employers.yaml"
    registry.write_text(yaml.safe_dump({
        "employers": [
            {"name": "Acme", "ats": "greenhouse", "slug": "acme"},
            {"name": "Big Co", "ats": "workday", "tenant": "bigco",
             "dc": "wd5", "site": "External"},
        ],
        "feeds": [{"name": "remotive", "active": True}],
    }))
    monkeypatch.setattr(careerkit, "EMPLOYERS", registry)
    loaded = careerkit.load_registry()
    assert len(loaded["employers"]) == 2
    assert loaded["feeds"][0]["name"] == "remotive"


def test_rescore_refuses_a_missing_registry_before_opening_the_database(
        tmp_path, monkeypatch):
    missing = tmp_path / "employers.yaml"
    monkeypatch.setattr(careerkit, "EMPLOYERS", missing)

    def unexpected_connect():
        raise AssertionError("rescore opened the database before validating its registry")

    monkeypatch.setattr(careerkit.store, "connect", unexpected_connect)
    args = argparse.Namespace(min_score=0)
    with pytest.raises(careerkit.RegistryError, match="registry file is missing"):
        careerkit.cmd_rescore(args)


def test_registry_address_schema_covers_every_adapter():
    assert set(careerkit.ATS_ADDRESS_FIELDS) == set(careerkit._adapters.REGISTRY)


@pytest.mark.parametrize(("document", "message"), [
    (None, "expected a YAML mapping"),
    ({"employers": [], "feeds": None}, "feeds must be a YAML list"),
    ({"employers": []}, "missing required 'feeds' list"),
    ({"employers": [{"name": "Acme", "ats": "greenhouse"}], "feeds": []},
     "employers[0].slug"),
    ({"employers": [{"name": "Acme", "ats": "mystery", "slug": "acme"}],
      "feeds": []}, "employers[0].ats names unknown adapter"),
    ({"employers": [{"name": "Big Co", "ats": "workday", "tenant": "bigco",
                      "site": "External"}], "feeds": []}, "employers[0].dc"),
    ({"employers": [], "feeds": [{"name": ""}]}, "feeds[0].name"),
    ({"employers": [], "feeds": [{"name": "not-a-feed"}]},
     "feeds[0].name names unknown feed"),
    ({"employers": [], "feeds": [{"name": "remotive", "active": "yes"}]},
     "feeds[0].active must be true or false"),
    ({"employers": [{"name": "Acme", "ats": "greenhouse", "slug": "acme",
                      "rails_exempt": "false"}], "feeds": []},
     "employers[0].rails_exempt must be true or false"),
])
def test_registry_errors_name_the_exact_invalid_field(
        tmp_path, monkeypatch, document, message):
    registry = tmp_path / "employers.yaml"
    registry.write_text(yaml.safe_dump(document))
    monkeypatch.setattr(careerkit, "EMPLOYERS", registry)
    with pytest.raises(careerkit.RegistryError, match=r".*") as exc:
        careerkit.load_registry()
    assert str(registry) in str(exc.value)
    assert message in str(exc.value)


def test_audit_rejects_invalid_display_regex_before_profile_or_network(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("audit did work before validating --grep")

    monkeypatch.setattr(careerkit, "load_profile", unexpected)
    monkeypatch.setattr(careerkit._pull, "fetch_all", unexpected)
    args = argparse.Namespace(grep="[", no_cache=False, employers=False, samples=1)
    with pytest.raises(SystemExit, match="Invalid --grep regex"):
        careerkit.cmd_audit(args)


def test_pull_source_flags_are_exclusive_and_tier_needs_a_value():
    parser = careerkit.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["pull", "--employers", "--feeds"])
    with pytest.raises(SystemExit):
        parser.parse_args(["pull", "--tier"])
    with pytest.raises(SystemExit):
        careerkit.main(["pull", "--feeds", "--tier", "A"])


def test_min_score_belongs_to_rescore_not_profile_lint():
    parser = careerkit.build_parser()
    assert parser.parse_args(["rescore", "--min-score", "41"]).min_score == 41
    with pytest.raises(SystemExit):
        parser.parse_args(["profile-lint", "--min-score", "41"])


def test_profile_lint_missing_profile_uses_first_run_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(careerkit, "PROFILE", tmp_path / "profile.yaml")
    with pytest.raises(SystemExit, match=r"Run /setup in Claude Code first"):
        careerkit.cmd_profile_lint(argparse.Namespace())


@pytest.mark.parametrize(("args", "writes"), [
    (argparse.Namespace(cmd="status"), False),
    (argparse.Namespace(cmd="report"), True),
    (argparse.Namespace(cmd="rescore"), True),
    (argparse.Namespace(cmd="applied", apply=False), False),
    (argparse.Namespace(cmd="applied", apply=True), True),
    (argparse.Namespace(cmd="consistency", repair=False), False),
    (argparse.Namespace(cmd="consistency", repair=True), True),
    (argparse.Namespace(cmd="tracker-sync", apply=False), False),
    (argparse.Namespace(cmd="tracker-sync", apply=True), True),
    (argparse.Namespace(cmd="relationship", relationship_action="list"), False),
    (argparse.Namespace(cmd="relationship", relationship_action="add"), True),
    (argparse.Namespace(cmd="analytics", output=None), False),
    (argparse.Namespace(cmd="analytics", output="analytics.json"), True),
    (argparse.Namespace(cmd="db", action="check"), False),
    (argparse.Namespace(cmd="db", action="backup"), True),
])
def test_command_write_classification(args, writes):
    assert careerkit.command_writes(args) is writes


def test_analytics_output_implies_json_and_writes_a_parseable_artifact(
        tmp_path, monkeypatch):
    monkeypatch.setattr(careerkit.store, "DB_PATH", tmp_path / "data/jobs.db")
    monkeypatch.setattr(careerkit, "APPLICATIONS", tmp_path / "applications.jsonl")
    output = tmp_path / "nested/analytics.json"

    careerkit.cmd_analytics(argparse.Namespace(
        as_of=None,
        follow_up_days=14,
        weeks=8,
        format="text",
        output=str(output),
    ))

    assert output.is_file()
    payload = json.loads(output.read_text())
    assert payload["summary"]["submitted"] == 0


def test_top_level_help_explains_commands():
    help_text = careerkit.build_parser().format_help()
    for phrase in ("poll sources", "re-judge stored postings", "validate profile",
                   "database integrity", "pipeline conversion",
                   "set a posting status"):
        assert phrase in help_text


def test_new_instance_rejects_path_traversal(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    script = source / "new-instance.sh"
    shutil.copy2(REPO / "new-instance.sh", script)
    result = _run(str(script), "../escaped", "--local", cwd=source)
    assert result.returncode != 0
    assert "Invalid instance name" in result.stdout
    assert not (tmp_path / "escaped").exists()


def test_new_instance_requires_explicit_local_mode_for_non_http_origin(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(REPO / "new-instance.sh", source / "new-instance.sh")
    assert _git(source, "init", "-q").returncode == 0
    assert _git(source, "remote", "add", "origin", str(tmp_path / "private.git")).returncode == 0

    result = _run(str(source / "new-instance.sh"), "safe", cwd=source)

    assert result.returncode != 0
    assert "Origin is not a portable HTTP(S) URL" in result.stdout
    assert not (tmp_path / "careerkit-instances/safe").exists()


def test_new_instance_never_prints_or_clones_embedded_origin_credentials(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(REPO / "new-instance.sh", source / "new-instance.sh")
    assert _git(source, "init", "-q").returncode == 0
    secret = "do-not-print-this-token"
    origin = f"https://user:{secret}@example.invalid/private.git"
    assert _git(source, "remote", "add", "origin", origin).returncode == 0

    result = _run(str(source / "new-instance.sh"), "safe", cwd=source)

    assert result.returncode != 0
    assert "embedded credentials" in result.stdout
    assert secret not in result.stdout + result.stderr
    assert not (tmp_path / "careerkit-instances/safe").exists()


def test_new_instance_local_copies_dirty_nonignored_snapshot(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(REPO / "new-instance.sh", source / "new-instance.sh")
    (source / "setup.sh").write_text(
        "#!/bin/bash\nset -e\nprintf 'yes\\n' > setup-ran.txt\n")
    (source / "setup.sh").chmod(0o755)
    (source / ".gitignore").write_text("private/\n")
    (source / "tracked.txt").write_text("committed\n")
    assert _git(source, "init", "-q").returncode == 0
    assert _git(source, "config", "user.email", "test@example.invalid").returncode == 0
    assert _git(source, "config", "user.name", "CareerKit Test").returncode == 0
    assert _git(source, "add", ".").returncode == 0
    assert _git(source, "commit", "-qm", "fixture").returncode == 0

    (source / "tracked.txt").write_text("working tree\n")
    (source / "untracked.txt").write_text("also included\n")
    (source / "private").mkdir()
    (source / "private/secret.txt").write_text("must stay private\n")

    result = _run(str(source / "new-instance.sh"), "safe", "--local", cwd=source)
    assert result.returncode == 0, result.stdout + result.stderr
    instance = tmp_path / "careerkit-instances" / "safe"
    assert (instance / "tracked.txt").read_text() == "working tree\n"
    assert (instance / "untracked.txt").read_text() == "also included\n"
    assert (instance / "setup-ran.txt").read_text() == "yes\n"
    assert not (instance / "private/secret.txt").exists()


def test_test_runner_uses_windows_venv_layout(tmp_path):
    shutil.copy2(REPO / "run-tests.sh", tmp_path / "run-tests.sh")
    (tmp_path / "tests").mkdir()
    scripts = tmp_path / ".venv/Scripts"
    scripts.mkdir(parents=True)
    python = scripts / "python.exe"
    python.write_text(
        "#!/bin/sh\nprintf '%s|%s\\n' \"$PYTHONUTF8\" \"$*\" >> invoked.txt\n"
    )
    python.chmod(0o755)

    result = _run(str(tmp_path / "run-tests.sh"), cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "invoked.txt").read_text().splitlines()
    assert calls == ["1|-c import pytest", "1|-m pytest tests/ -q"]


def test_main_launcher_uses_windows_venv_without_a_python3_command(tmp_path):
    shutil.copy2(REPO / "careerkit.py", tmp_path / "careerkit.py")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(REPO / "scripts/run-careerkit.sh", scripts / "run-careerkit.sh")
    venv = tmp_path / ".venv/Scripts"
    venv.mkdir(parents=True)
    python = venv / "python.exe"
    python.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$PYTHONUTF8" "$*"\n')
    python.chmod(0o755)

    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    command = [str(tmp_path / "careerkit.py"), "--help"]
    if os.name == "nt":
        command.insert(0, BASH)
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env={**os.environ, "PATH": str(empty_path)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().startswith("1|")
    assert result.stdout.strip().endswith("careerkit.py --help")


def test_executable_sources_are_forced_to_lf_in_windows_checkouts():
    attributes = (REPO / ".gitattributes").read_text()
    assert "*.sh text eol=lf" in attributes
    assert "*.py text eol=lf" in attributes
    for path in [REPO / "careerkit.py", REPO / "setup.sh",
                 REPO / "new-instance.sh", REPO / "run-tests.sh",
                 *sorted((REPO / "scripts").glob("*.sh"))]:
        assert b"\r\n" not in path.read_bytes(), f"CRLF in executable {path}"


def test_distribution_has_no_console_script_and_docs_are_clone_only():
    with (REPO / "pyproject.toml").open("rb") as fh:
        project = tomllib.load(fh)["project"]
    assert "scripts" not in project
    readme = (REPO / "README.md").read_text()
    assert "intentionally clone-only" in readme
    assert "pip install -e ." not in readme


def test_application_contract_has_no_batch_submission_mode():
    required = {
        ".claude/skills/apply/SKILL.md": (
            "Every specific application requires fresh approval",
            "Approval for another application never carries over",
            "completes captcha and OTP steps directly",
        ),
        "CLAUDE.md": (
            "fresh approval for that specific completed application",
            "no batch, session-wide, or standing permission is valid",
            "completes captcha and OTP steps directly",
        ),
        "README.md": (
            "fresh, explicit approval for that one application",
            "no batch or standing submission permission",
            "you handle those directly and never relay codes",
        ),
        "SECURITY.md": (
            "fresh, explicit approval for that one completed application",
            "no batch or standing submission permission",
            "handles certification, captcha, and OTP steps directly",
        ),
        "guide/careerkit-guide.html": (
            "any agent click requires fresh approval for",
            "that one completed form",
            "captcha or OTP checks; you complete those directly and never relay codes",
        ),
        "profile.example/profile.yaml": (
            "submit: ask_each",
            "every specific application requires fresh approval",
        ),
    }
    documents = {
        name: " ".join((REPO / name).read_text().split())
        for name in required
    }
    for name, phrases in required.items():
        for phrase in phrases:
            assert phrase in documents[name], f"{name} is missing {phrase!r}"

    combined = "\n".join(documents.values())
    assert "per_batch" not in combined
    assert "relays OTP codes" not in combined


def test_criteria_and_setup_workflows_run_the_safety_sequence():
    criteria = (REPO / ".claude/skills/criteria/SKILL.md").read_text()
    assert (criteria.index("profile-lint") < criteria.index("rescore")
            < criteria.index("pull") < criteria.index("audit"))
    setup = (REPO / ".claude/skills/setup/SKILL.md").read_text()
    assert "employer boards, feeds, and pasted URLs" in setup
    assert "cannot retract data" in setup
    assert setup.index("Privacy disclosure") < setup.index("Ask for their resume")
    assert "`profile-lint`, `rescore`, `pull`, and `audit`" in setup


def test_search_workflow_bypasses_cache_for_verified_live_results():
    workflow = (REPO / ".claude/skills/search/SKILL.md").read_text()
    assert "./careerkit.py pull --no-cache" in workflow
    assert workflow.index("pull --no-cache") < workflow.index("VERIFY LIVE")


def test_monthly_audit_is_fresh_and_does_not_claim_exhaustive_titles():
    workflow = (REPO / ".claude/skills/audit/SKILL.md").read_text()
    assert "./careerkit.py audit --no-cache" in workflow
    assert "representative kills from every reason group" in workflow
    guide = (REPO / "guide/careerkit-guide.html").read_text()
    assert "sample exclusions by reason" in guide
    assert "review every exclusion" not in guide


def test_apply_records_only_confirmed_submissions_in_machine_and_human_state():
    workflow = " ".join(
        (REPO / ".claude/skills/apply/SKILL.md").read_text().split()
    )
    confirmation = workflow.index("verifying the confirmation page")
    assert confirmation < workflow.index("./careerkit.py progress UID applied")
    assert confirmation < workflow.index("profile/applications.jsonl")
    assert confirmation < workflow.index("log APPLIED in tracker.md")
    assert "A prepared or filled form is never recorded as submitted" in workflow


def test_privacy_contract_names_keyed_credentials_and_local_deletion_limits():
    expected = {
        "README.md": ("Keyed feeds, if you enable them", "USAJobs additionally",
                      "cannot retract requests already sent"),
        "SECURITY.md": ("Enabled keyed feeds transmit", "USAJobs additionally",
                        "cannot retract provider requests"),
        "guide/careerkit-guide.html": ("To keyed feeds you enable",
                                       "USAJobs additionally",
                                       "cannot retract provider requests"),
        ".claude/skills/setup/SKILL.md": ("optional keyed feeds transmit",
                                         "USAJobs also includes",
                                         "cannot retract data already sent"),
    }
    for name, phrases in expected.items():
        text = " ".join((REPO / name).read_text().split())
        for phrase in phrases:
            assert phrase in text, f"{name} is missing {phrase!r}"
