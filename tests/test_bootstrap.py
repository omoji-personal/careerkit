"""bootstrap.sh installs software, so its refusals matter more than its installs.

Every test here runs the script with a controlled environment: no `claude` on
PATH, an isolated HOME, and the desktop-app probe pinned. Without that pinning
the results depend on whether the machine running the suite happens to have
Claude Code or Homebrew installed, which is how a green laptop and a red CI run
disagree about the same commit.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

# Git Bash on Windows cannot drive Homebrew or an interactive sudo prompt, so
# bootstrap.sh deliberately prints what to install by hand and exits nonzero
# instead of offering a plan it could not carry out. The plan-shaped tests below
# describe the macOS/Linux path; the Windows path has its own test.
WINDOWS = sys.platform.startswith("win")
not_windows = pytest.mark.skipif(WINDOWS, reason="Windows takes the manual-instructions path")


def _checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "bootstrap.sh", checkout / "bootstrap.sh")
    for helper in ("select-python.sh", "detect-claude.sh"):
        shutil.copy2(ROOT / "scripts" / helper, checkout / "scripts" / helper)
    (checkout / "bootstrap.sh").chmod(0o755)
    return checkout


def _env(tmp_path: Path, *, desktop: str = " ") -> dict[str, str]:
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
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        **os.environ,
        "HOME": str(home),
        "BASH_ENV": str(hide),
        "CAREERKIT_CLAUDE_DESKTOP_PATHS": desktop,
    }


def _run(checkout: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(checkout / "bootstrap.sh"), *args],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )


@not_windows
def test_dry_run_changes_nothing_and_says_so(tmp_path):
    checkout = _checkout(tmp_path)
    result = _run(checkout, _env(tmp_path), "--dry-run")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "Nothing has been changed yet." in combined
    assert "Dry run: stopping here." in combined


@not_windows
def test_non_interactive_without_yes_refuses_to_install(tmp_path):
    """stdin is not a terminal here, so nobody could have answered the prompt.
    Installing anyway would be an unattended install the user never approved."""
    checkout = _checkout(tmp_path)
    result = _run(checkout, _env(tmp_path))

    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "nothing was installed" in combined
    assert "--yes" in combined


@not_windows
def test_the_plan_prints_every_command_before_running_any(tmp_path):
    checkout = _checkout(tmp_path)
    result = _run(checkout, _env(tmp_path), "--dry-run")

    combined = result.stdout + result.stderr
    assert "This will install:" in combined
    # Claude Code is missing in this environment, so its exact command must be
    # visible in the plan rather than summarized.
    assert "https://claude.ai/install.sh" in combined


def test_claude_is_not_installed_through_npm(tmp_path):
    """The native installer ships a self-contained binary. Routing through npm
    would make Node.js a prerequisite for something that does not use it."""
    checkout = _checkout(tmp_path)
    result = _run(checkout, _env(tmp_path), "--dry-run")

    combined = result.stdout + result.stderr
    assert "https://claude.ai/install.sh" in combined
    assert "npm install -g" not in combined
    assert "brew install node" not in combined
    # Node.js must not appear as a planned step at all. Checking the plan lines
    # rather than the whole transcript keeps this honest without depending on
    # prose elsewhere in the output.
    plan_lines = [
        line for line in combined.splitlines() if line.strip().startswith(("1.", "2.", "3.", "4."))
    ]
    assert not any("node" in line.lower() for line in plan_lines), plan_lines


def test_desktop_app_counts_as_installed(tmp_path):
    checkout = _checkout(tmp_path)
    desktop = tmp_path / "Claude.app"
    desktop.mkdir()
    result = _run(checkout, _env(tmp_path, desktop=str(desktop)), "--dry-run")

    combined = result.stdout + result.stderr
    assert "desktop app" in combined
    assert "claude.ai/install.sh" not in combined


def test_an_off_path_launcher_is_reported_as_a_path_problem(tmp_path):
    """Reinstalling cannot fix a PATH entry, so the advice must differ."""
    checkout = _checkout(tmp_path)
    env = _env(tmp_path)
    launcher_dir = Path(env["HOME"]) / ".local" / "bin"
    launcher_dir.mkdir(parents=True)
    launcher = launcher_dir / "claude"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)

    result = _run(checkout, env, "--dry-run")

    combined = result.stdout + result.stderr
    assert "not on this shell's PATH" in combined
    assert "$HOME/.local/bin" in combined


def test_help_exits_cleanly_without_touching_anything(tmp_path):
    checkout = _checkout(tmp_path)
    result = _run(checkout, _env(tmp_path), "--help")

    assert result.returncode == 0
    assert "prerequisite installer" in result.stdout
    assert "This will install:" not in result.stdout


def test_unknown_option_is_rejected(tmp_path):
    checkout = _checkout(tmp_path)
    result = _run(checkout, _env(tmp_path), "--install-everything-now")

    assert result.returncode == 2
    assert "unknown option" in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="the Homebrew step is macOS-only")
def test_yes_cannot_force_a_step_that_needs_a_password_prompt(tmp_path):
    """Reported from a real first install: Homebrew's installer stops with
    "Running in non-interactive mode because stdin is not a TTY ... Need sudo
    access on macOS". --yes cannot answer a password prompt, so a plan that
    includes Homebrew must refuse rather than fail halfway through."""
    checkout = _checkout(tmp_path)
    env = _env(tmp_path)
    # No Homebrew and only the system python 3.9.6 on PATH, so Homebrew is
    # planned. The interpreter probe also checks absolute install prefixes, so
    # it must be pinned too or a real Homebrew python answers for the machine.
    env["PATH"] = "/usr/bin:/bin"
    env["CAREERKIT_PYTHON_SEARCH_PATHS"] = " "

    result = _run(checkout, env, "--yes")

    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "needs a real terminal window" in combined or "cannot run without a terminal" in combined
    # Nothing may have been attempted: install steps announce themselves with "==>".
    assert "==>" not in combined


@pytest.mark.skipif(not WINDOWS, reason="describes the Git Bash path specifically")
def test_windows_git_bash_is_told_what_to_install_by_hand(tmp_path):
    """bootstrap.sh cannot install packages from Git Bash, and pretending
    otherwise would strand a Windows user midway. It must name every missing
    prerequisite with a command that actually works on Windows."""
    checkout = _checkout(tmp_path)
    result = _run(checkout, _env(tmp_path), "--dry-run")

    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "cannot install packages on this system automatically" in combined
    assert "install.ps1" in combined or "winget" in combined
    assert "brew" not in combined
