"""Execution-level regressions for the stranger-facing shell onboarding path."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"


def _run_bash(script: Path, *args: str, cwd: Path, env=None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(script), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _instance_source(tmp_path: Path, setup_body: str, *, spaced_parent: bool = False) -> Path:
    parent = tmp_path / "parent with space" if spaced_parent else tmp_path
    source = parent / "source"
    source.mkdir(parents=True)
    shutil.copy2(ROOT / "new-instance.sh", source / "new-instance.sh")
    setup = source / "setup.sh"
    setup.write_text("#!/bin/bash\nset -eu\n" + setup_body, encoding="utf-8")
    setup.chmod(0o755)
    (source / "README.md").write_text("# clean fixture\n", encoding="utf-8")
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "CareerKit Test")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "add", "README.md", "new-instance.sh", "setup.sh")
    _git(source, "commit", "-qm", "fixture")
    return source


def test_clean_local_instance_skips_an_empty_tracked_patch(tmp_path):
    source = _instance_source(
        tmp_path,
        "printf 'setup complete\\n' > setup-ran.txt\n",
    )

    result = _run_bash(source / "new-instance.sh", "stranger", "--local", cwd=source)

    instance = tmp_path / "careerkit-instances" / "stranger"
    assert result.returncode == 0, result.stdout + result.stderr
    assert (instance / "setup-ran.txt").read_text() == "setup complete\n"
    assert "No valid patches in input" not in result.stdout + result.stderr
    assert "Instance ready:" in result.stdout


def test_failed_instance_setup_is_retained_with_an_executable_recovery_command(tmp_path):
    setup = """
if [ ! -f .first-setup-failed ]; then
  : > .first-setup-failed
  echo "simulated dependency failure" >&2
  exit 42
fi
printf 'recovered\\n' > setup-recovered.txt
"""
    source = _instance_source(tmp_path, setup, spaced_parent=True)
    result = _run_bash(source / "new-instance.sh", "stranger", "--local", cwd=source)

    instance = source.resolve().parent / "careerkit-instances" / "stranger"
    combined = result.stdout + result.stderr
    assert result.returncode == 42, combined
    assert instance.is_dir()
    prefix = "  Retry setup with: "
    recovery_lines = [line for line in result.stdout.splitlines() if line.startswith(prefix)]
    assert len(recovery_lines) == 1, combined
    recovery = recovery_lines[0][len(prefix):]
    assert recovery.startswith("cd ") and recovery.endswith(" && ./setup.sh")
    assert r"\ " in recovery, "the checkout path contains spaces and must be shell-quoted"
    assert "Do not rerun new-instance.sh; that command creates only new destinations." in combined

    wrong_retry = _run_bash(source / "new-instance.sh", "stranger", "--local", cwd=source)
    assert wrong_retry.returncode != 0
    assert "exists:" in wrong_retry.stdout
    assert not (instance / "setup-recovered.txt").exists()

    recovered = subprocess.run(
        [BASH, "-c", recovery], capture_output=True, text=True, check=False
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert (instance / "setup-recovered.txt").read_text() == "recovered\n"


def _setup_fixture(
    tmp_path: Path, venv_python_body: str, claude_body: str | None
) -> tuple[Path, dict[str, str]]:
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "setup.sh", checkout / "setup.sh")
    shutil.copy2(ROOT / "scripts/select-python.sh", scripts / "select-python.sh")
    shutil.copy2(ROOT / "scripts/detect-claude.sh", scripts / "detect-claude.sh")
    shutil.copy2(ROOT / "requirements-dev.txt", checkout / "requirements-dev.txt")

    venv_dir = checkout / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    venv_dir.mkdir(parents=True)
    venv_python = venv_dir / ("python.exe" if os.name == "nt" else "python")
    venv_python.write_text("#!/bin/sh\nset -eu\n" + venv_python_body, encoding="utf-8")
    venv_python.chmod(0o755)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    if claude_body is not None:
        claude = fake_bin / "claude"
        claude.write_text("#!/bin/sh\n" + claude_body, encoding="utf-8")
        claude.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        # A single space means "no desktop app". Without this the result depends
        # on whether the machine running the tests has Claude.app installed.
        "CAREERKIT_CLAUDE_DESKTOP_PATHS": " ",
    }
    return checkout, env


def _isolate_claude(tmp_path: Path, env: dict[str, str]) -> dict[str, str]:
    """Make Claude Code detection depend only on what a test sets up.

    Three things leak in otherwise: `claude` on the real PATH, the developer's
    own ~/.local/bin/claude launcher, and /Applications/Claude.app. A test that
    controls none of them asserts different behavior on a laptop than on CI.
    """
    hide = tmp_path / "hide-claude.sh"
    hide.write_text(
        """command() {
  if [ "${1-}" = -v ] && [ "${2-}" = claude ]; then
    return 1
  fi
  builtin command "$@"
}
""",
        encoding="utf-8",
    )
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    return {
        **env,
        "BASH_ENV": str(hide),
        "HOME": str(home),
        "CAREERKIT_CLAUDE_DESKTOP_PATHS": " ",
    }


@pytest.mark.parametrize("claude_body", ["exit 23\n", "exit 0\n"])
def test_setup_warns_when_claude_is_present_but_unusable(tmp_path, claude_body):
    checkout, env = _setup_fixture(tmp_path, "exit 0\n", claude_body)

    result = _run_bash(checkout / "setup.sh", cwd=checkout, env=env)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "! claude is present but unusable" in combined
    assert "Fix or reinstall it:" in combined
    assert "CareerKit's Python environment is ready, but Claude Code is not usable yet." in combined
    assert "Next:  claude doctor" in combined
    assert "./setup.sh      # safe recheck" in combined
    assert "Next:  claude          # open Claude Code" not in combined
    assert "Done." not in combined


def test_setup_without_claude_points_to_install_and_safe_recheck(tmp_path):
    checkout, env = _setup_fixture(tmp_path, "exit 0\n", None)
    env = _isolate_claude(tmp_path, env)

    result = _run_bash(checkout / "setup.sh", cwd=checkout, env=env)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "! claude not found" in combined
    assert "CareerKit's Python environment is ready, but Claude Code is not installed yet." in combined
    assert "Next:  ./bootstrap.sh" in combined
    assert "https://claude.ai/download" in combined
    assert "npm install -g" not in combined
    assert "./setup.sh      # safe recheck" in combined
    assert "Next:  claude          # open Claude Code" not in combined
    assert "Done." not in combined


@pytest.mark.parametrize(
    ("failure", "exit_code", "progress", "failure_text"),
    [
        ("upgrade", 71, "upgrading pip in .venv", "could not upgrade pip in .venv"),
        (
            "requirements",
            72,
            "installing CareerKit runtime and development dependencies",
            "could not install CareerKit runtime and development dependencies",
        ),
    ],
)
def test_dependency_install_failure_names_the_step_and_safe_rerun(
    tmp_path, failure, exit_code, progress, failure_text
):
    venv_python = r'''
case "${FAKE_PIP_FAILURE-}:$*" in
  "upgrade:-m pip install --quiet --upgrade pip") exit 71 ;;
  "requirements:-m pip install --quiet -r requirements-dev.txt") exit 72 ;;
esac
exit 0
'''
    checkout, env = _setup_fixture(tmp_path, venv_python, "echo '2.1.236 (Claude Code)'\n")
    failing_env = {**env, "FAKE_PIP_FAILURE": failure}

    failed = _run_bash(checkout / "setup.sh", cwd=checkout, env=failing_env)

    combined = failed.stdout + failed.stderr
    assert failed.returncode == exit_code, combined
    assert progress in combined
    assert failure_text in combined
    assert "Setup is incomplete, but it is safe to rerun." in combined
    assert "Check your network or proxy, then run: ./setup.sh" in combined
    assert "CareerKit will reuse .venv and retry the dependency installation." in combined
    assert "Done." not in combined

    recovered = _run_bash(checkout / "setup.sh", cwd=checkout, env=env)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert "Done." in recovered.stdout
    assert "Next:  claude          # open Claude Code" in recovered.stdout
    assert "claude doctor" in recovered.stdout


def test_setup_accepts_the_desktop_app_instead_of_the_terminal_command(tmp_path):
    """A desktop-app user has a working Claude Code and must not be told to
    install a second copy: CareerKit's /setup is a skill in this checkout, and
    whichever Claude opens the folder can run it."""
    checkout, env = _setup_fixture(tmp_path, "exit 0\n", None)
    desktop = tmp_path / "Claude.app"
    desktop.mkdir()
    env = {**_isolate_claude(tmp_path, env), "CAREERKIT_CLAUDE_DESKTOP_PATHS": str(desktop)}

    result = _run_bash(checkout / "setup.sh", cwd=checkout, env=env)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "claude Code desktop app" in combined
    assert "open this folder in the Claude Code desktop app" in combined
    assert "! claude not found" not in combined
    assert "not installed yet" not in combined


def test_setup_distinguishes_an_off_path_install_from_a_missing_one(tmp_path):
    """The native installer writes ~/.local/bin/claude, which a fresh shell may
    not have on PATH. Reinstalling cannot fix that, so setup must name the PATH
    problem rather than repeat the install instructions."""
    checkout, env = _setup_fixture(tmp_path, "exit 0\n", None)
    env = _isolate_claude(tmp_path, env)
    launcher_dir = Path(env["HOME"]) / ".local" / "bin"
    launcher_dir.mkdir(parents=True)
    launcher = launcher_dir / "claude"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)

    result = _run_bash(checkout / "setup.sh", cwd=checkout, env=env)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "not on this shell's PATH" in combined
    assert "$HOME/.local/bin" in combined
    assert "! claude not found" not in combined
