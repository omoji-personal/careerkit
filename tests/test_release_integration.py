"""Cross-cutting release regressions owned by the integration layer."""
from __future__ import annotations

import builtins
import csv
import io
import os
from pathlib import Path
import re
import shutil
import subprocess

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 CI
    import tomli as tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "/bin/bash"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERKIT_HOME", str(tmp_path))
    from engine import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    return store.connect()


def _non_us_profile():
    from engine.score import Profile

    profile = Profile()
    profile.lanes = [(50, re.compile(r"product manager", re.I), "pm")]
    profile.domain_terms = None
    return profile


def test_run_lock_contender_does_not_erase_active_owner(tmp_path):
    """Opening a lock with ``w`` truncated its PID before the contender's
    nonblocking flock was refused, destroying the evidence operators needed."""
    from engine.store import RunLock

    path = tmp_path / "jobs.lock"
    with RunLock(path):
        owner = path.read_text()
        assert owner.isdigit()
        with pytest.raises(RuntimeError, match="already writing"):
            with RunLock(path):
                pass
        assert path.read_text() == owner


def test_run_lock_fails_closed_when_lock_file_cannot_open(tmp_path, monkeypatch):
    """A filesystem error used to disable serialization and run the write
    anyway, exactly when mutual exclusion could not be guaranteed."""
    from engine.store import RunLock

    real_open = builtins.open

    def denied(path, *args, **kwargs):
        if str(path).endswith("jobs.lock"):
            raise PermissionError("simulated read-only filesystem")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    with pytest.raises(RuntimeError, match="cannot acquire.*refusing to write"):
        with RunLock(tmp_path / "jobs.lock"):
            pass


@pytest.mark.parametrize(
    "value",
    [
        "=HYPERLINK(\"https://tracker.invalid\")",
        "+cmd|' /C calc'!A0",
        "-1+1",
        "@SUM(1,1)",
        "  =WEBSERVICE(\"https://tracker.invalid\")",
        "\t=IMPORTDATA(\"https://tracker.invalid\")",
    ],
)
def test_csv_export_cells_neutralize_spreadsheet_formulas(value):
    import careerkit

    safe = careerkit.csv_safe_cell(value)
    assert safe.startswith("'")
    stream = io.StringIO()
    csv.writer(stream).writerow([safe])
    stream.seek(0)
    assert next(csv.reader(stream))[0].startswith("'")


def test_rescore_repairs_legacy_dream_exemptions_from_registry(db):
    """Rows written by the released scorer could carry a profile-derived
    exemption forever. Rescore must restore the registry as source of truth."""
    from engine import pull, store
    from engine.models import Job

    polluted = Job(
        company="Former Dream", title="Product Manager",
        url="https://example.test/former", location="London, UK",
        source="greenhouse", board="greenhouse:former", external_id="former",
        rails_exempt=True, description="Product operations ownership. " * 20,
    )
    legitimate = Job(
        company="Registry Exception", title="Product Manager",
        url="https://example.test/registry", location="London, UK",
        source="greenhouse", board="greenhouse:registry", external_id="registry",
        rails_exempt=True, description="Product operations ownership. " * 20,
    )
    for job in (polluted, legitimate):
        job.gate, job.score, job.reasons = "QUALIFIED", 50, ["historical"]
    store.upsert(db, [polluted, legitimate])

    pull.rescore(
        db,
        _non_us_profile(),
        registry_exempt_boards={"greenhouse:registry"},
        echo=lambda *_args: None,
    )

    rows = {
        row["board"]: row
        for row in db.execute(
            "SELECT board, gate, rails_exempt FROM jobs ORDER BY board"
        )
    }
    assert (rows["greenhouse:former"]["gate"],
            rows["greenhouse:former"]["rails_exempt"]) == ("EXCLUDED", 0)
    assert (rows["greenhouse:registry"]["gate"],
            rows["greenhouse:registry"]["rails_exempt"]) == ("QUALIFIED", 1)


def test_vulnerable_jobspy_graph_is_not_an_installable_extra():
    """JobSpy 1.1.82 pins markdownify below the security-fixed release, so an
    advertised extra or copy-paste requirement would reinstall the known-bad
    graph even though the runtime guard correctly refuses it."""
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    assert "scrape" not in project.get("optional-dependencies", {})
    assert "python-jobspy" not in (ROOT / "requirements.txt").read_text()
    assert "requests>=2.33.0" in project["dependencies"]
    assert "requests>=2.33.0" in (ROOT / "requirements.txt").read_text()
    setup = (ROOT / "setup.sh").read_text()
    assert '"$VENV_PY" scripts/remove_unsafe_jobspy.py' in setup
    assert '"$VENV_PY" -m pip check' in setup


def test_setup_migrates_the_vulnerable_jobspy_pair_and_is_idempotent(monkeypatch):
    from scripts import remove_unsafe_jobspy as migration

    installed = {"python-jobspy": "1.1.82", "markdownify": "0.13.1"}

    def version(name):
        if name not in installed:
            raise migration.metadata.PackageNotFoundError(name)
        return installed[name]

    class Distribution:
        requires = ["markdownify>=0.13.1,<0.14.0"]

    def distribution(name):
        if name not in installed:
            raise migration.metadata.PackageNotFoundError(name)
        return Distribution()

    calls = []
    messages = []
    monkeypatch.setattr(migration.metadata, "version", version)
    monkeypatch.setattr(migration.metadata, "distribution", distribution)
    assert migration.cleanup(runner=lambda args, **kw: calls.append((args, kw)),
                             emit=messages.append) is True
    assert calls == [([
        migration.sys.executable, "-m", "pip", "uninstall", "--quiet", "--yes",
        "python-jobspy", "markdownify",
    ], {"check": True})]
    assert any("set active: false" in message for message in messages)

    installed.clear()
    assert migration.cleanup(runner=lambda *_args, **_kw: calls.append("unexpected"),
                             emit=lambda _message: None) is False
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("markdownify", "requirement", "unsafe"),
    [
        ("1.1.0", "markdownify>=0.13.1,<0.14.0", True),
        ("0.14.1", "markdownify>=0.14.1", False),
        ("1.1.0", "markdownify>=0.14.1", False),
    ],
)
def test_jobspy_migration_checks_declared_compatibility(
        monkeypatch, markdownify, requirement, unsafe):
    from scripts import remove_unsafe_jobspy as migration

    class Distribution:
        requires = [requirement]

    monkeypatch.setattr(migration.metadata, "distribution",
                        lambda _name: Distribution())
    monkeypatch.setattr(migration.metadata, "version",
                        lambda _name: markdownify)
    assert migration.has_unsafe_jobspy_pair() is unsafe


def test_setup_migration_removes_orphan_vulnerable_markdownify(monkeypatch):
    from scripts import remove_unsafe_jobspy as migration

    def version(name):
        if name == "markdownify":
            return "0.13.1"
        raise migration.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(migration.metadata, "version", version)
    monkeypatch.setattr(
        migration.metadata,
        "distribution",
        lambda name: (_ for _ in ()).throw(
            migration.metadata.PackageNotFoundError(name)
        ),
    )
    calls = []
    assert migration.cleanup(
        runner=lambda args, **kwargs: calls.append((args, kwargs)),
        emit=lambda _message: None,
    ) is True
    assert calls == [([
        migration.sys.executable, "-m", "pip", "uninstall", "--quiet", "--yes",
        "markdownify",
    ], {"check": True})]


def test_jobspy_policy_is_explicitly_unsupported_until_dependency_is_safe():
    from engine import aggregators

    assert aggregators.policy("jobspy")["supported"] is False


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake-interpreter fixture; native Git Bash setup runs in CI",
)
def test_setup_rebuilds_old_venv_and_targets_only_project_python(tmp_path):
    """A runnable 3.9 venv and a no-op activation file must not make setup
    retain old Python or run pip/uninstall against a PATH interpreter."""
    shutil.copy2(ROOT / "setup.sh", tmp_path / "setup.sh")
    shutil.copy2(ROOT / "requirements-dev.txt", tmp_path / "requirements-dev.txt")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts/select-python.sh", scripts / "select-python.sh")
    shutil.copy2(ROOT / "scripts/remove_unsafe_jobspy.py",
                 scripts / "remove_unsafe_jobspy.py")

    old_venv = tmp_path / ".venv/bin"
    old_venv.mkdir(parents=True)
    old_marker = tmp_path / ".venv/old-marker"
    old_marker.touch()
    old_python = old_venv / "python"
    old_python.write_text(
        "#!/bin/sh\n"
        "# Runnable Python 3.9 stand-in: the released import-only predicate\n"
        "# accepted it, while the new version-floor probe rejects it.\n"
        "case \"$*\" in *sys.version_info*) exit 1 ;; esac\n"
        "case \"$*\" in *'import sys'*) exit 0 ;; esac\n"
        "exit 0\n"
    )
    old_python.chmod(0o755)
    # This deliberately does nothing; setup must not rely on activation for
    # selecting the interpreter it mutates.
    (old_venv / "activate").write_text("")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "venv-calls.txt"
    trap = tmp_path / "path-python-was-used.txt"
    venv_template = tmp_path / "new-venv-python"
    venv_template.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$VENV_CALLS"\nexit 0\n'
    )
    venv_template.chmod(0o755)
    bootstrap = fake_bin / "python3"
    bootstrap.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'print(\"%d.%d\"'*) printf '3.12\\n'; exit 0 ;;\n"
        "  *sys.version_info*) exit 0 ;;\n"
        "esac\n"
        "if [ \"${1-}\" = -m ] && [ \"${2-}\" = venv ]; then\n"
        "  /bin/mkdir -p .venv/bin\n"
        "  /bin/cp \"$FAKE_VENV_PY\" .venv/bin/python\n"
        "  /bin/chmod +x .venv/bin/python\n"
        "  : > .venv/rebuilt\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    bootstrap.chmod(0o755)
    path_python = fake_bin / "python"
    path_python.write_text(
        '#!/bin/sh\nprintf used > "$PATH_PYTHON_TRAP"\nexit 91\n'
    )
    path_python.chmod(0o755)

    result = subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(tmp_path / "setup.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_VENV_PY": str(venv_template),
            "VENV_CALLS": str(calls),
            "PATH_PYTHON_TRAP": str(trap),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / ".venv/rebuilt").is_file()
    assert not old_marker.exists()
    assert not trap.exists(), "setup used PATH python instead of the project venv"
    invoked = calls.read_text()
    assert "-m pip install --quiet --upgrade pip" in invoked
    assert "scripts/remove_unsafe_jobspy.py" in invoked
    assert "-m pip check" in invoked


@pytest.mark.parametrize(
    ("available", "selected"),
    [("python", "python"), ("py", "py -3")],
)
def test_setup_selects_native_windows_python_names(tmp_path, available, selected):
    """Git Bash often has python.exe or py.exe but no `python3` command."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    executable = fake_bin / available
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    helper = ROOT / "scripts" / "select-python.sh"
    command = (
        f'. "{helper}"; careerkit_select_bootstrap_python; '
        'printf "%s\\n" "${CAREERKIT_BOOTSTRAP_PYTHON[*]}"'
    )
    result = subprocess.run(
        [BASH, "-c", command],
        env={**os.environ, "PATH": str(fake_bin)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == selected
    assert ". ./scripts/select-python.sh" in (ROOT / "setup.sh").read_text()


def test_setup_skips_an_old_python3_for_a_supported_python(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    old = fake_bin / "python3"
    old.write_text("#!/bin/sh\nexit 1\n")
    old.chmod(0o755)
    current = fake_bin / "python"
    current.write_text("#!/bin/sh\nexit 0\n")
    current.chmod(0o755)
    helper = ROOT / "scripts" / "select-python.sh"
    command = (
        f'. "{helper}"; careerkit_select_bootstrap_python; '
        'printf "%s\\n" "${CAREERKIT_BOOTSTRAP_PYTHON[*]}"'
    )
    result = subprocess.run(
        [BASH, "-c", command],
        env={**os.environ, "PATH": str(fake_bin)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "python"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"search_terms": [None]}, "search_terms[0]"),
        ({"exclusions": {"companies": [None]}}, "exclusions.companies[0]"),
        ({"autonomy": {"submit": "per_batch"}}, "must be 'ask_each'"),
    ],
)
def test_profile_rejects_nested_values_that_break_operating_contracts(
        tmp_path, extra, message):
    from engine.score import Profile, ProfileError

    config = {
        "lanes": [{"key": "pm", "titles": ["product manager"], "weight": 50}],
        **extra,
    }
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ProfileError, match=re.escape(message)):
        Profile.load(path)
