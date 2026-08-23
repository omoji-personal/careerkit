"""Release-facing contracts that should stay true for a stranger's first run.

These tests intentionally inspect the public examples and execute only
``bootstrap.sh --dry-run``.  They must never install a package, contact a
service, or depend on the maintainer's local Claude installation.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")


def _bootstrap_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "bootstrap.sh", checkout / "bootstrap.sh")
    shutil.copy2(ROOT / "scripts" / "select-python.sh", scripts / "select-python.sh")
    shutil.copy2(ROOT / "scripts" / "detect-claude.sh", scripts / "detect-claude.sh")
    (checkout / "bootstrap.sh").chmod(0o755)
    return checkout


def _bootstrap_env(
    tmp_path: Path,
    *,
    platform: str,
    hide_curl: bool,
    hide_claude: bool = True,
    desktop: str = " ",
) -> dict[str, str]:
    """Pin platform/tool detection without removing real Python and git."""
    bash_env = tmp_path / "controlled-shell.sh"
    hidden_curl = "curl" if hide_curl else "never-hide-curl"
    hidden_claude = "claude" if hide_claude else "never-hide-claude"
    bash_env.write_text(
        f'''command() {{
  if [ "${{1-}}" = -v ] && [ "${{2-}}" = {hidden_claude} ]; then
    return 1
  fi
  if [ "${{1-}}" = -v ] && [ "${{2-}}" = {hidden_curl} ]; then
    return 1
  fi
  builtin command "$@"
}}
uname() {{
  case "${{1-}}" in
    -s) printf '%s\\n' {platform} ;;
    -m) printf '%s\\n' x86_64 ;;
    *) printf '%s\\n' {platform} ;;
  esac
}}
''',
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        **os.environ,
        "HOME": str(home),
        "BASH_ENV": str(bash_env),
        "CAREERKIT_CLAUDE_DESKTOP_PATHS": desktop,
    }


def _run_bootstrap(
    checkout: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(checkout / "bootstrap.sh"), "--dry-run"],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def test_public_profile_and_plan_use_reserved_email_domains():
    email_pattern = re.compile(r"[A-Z0-9._%+-]+@([A-Z0-9.-]+)", re.IGNORECASE)
    public_examples = (
        ROOT / "profile.example" / "profile.yaml",
        ROOT / "docs" / "IMPROVEMENT-PLAN.md",
    )

    found: list[tuple[str, str]] = []
    for path in public_examples:
        for domain in email_pattern.findall(path.read_text(encoding="utf-8")):
            found.append((str(path.relative_to(ROOT)), domain.lower()))

    assert found, "the fixtures should keep at least one realistic email example"
    assert all(
        domain.endswith((".invalid", ".example", ".test")) for _, domain in found
    ), found


def test_tracked_text_has_no_private_search_fixture_residue():
    """Public authorship is intentional; private client/search fixtures are not."""
    listed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, text=True, capture_output=True, check=True,
    ).stdout.splitlines()
    text_suffixes = {
        ".md", ".py", ".sh", ".yaml", ".yml", ".toml", ".json", ".html",
        ".txt",
    }
    patterns = [
        re.compile("profile-" + "omid", re.IGNORECASE),
        re.compile(r"\b" + "delta air" + r"\s+lines\b", re.IGNORECASE),
        re.compile(r"\b" + "nelson " + r"mullins\b", re.IGNORECASE),
        re.compile(r"\b" + "tech" + r"bridge\b", re.IGNORECASE),
        re.compile(r"\b" + "jac" + r"oby\b", re.IGNORECASE),
        re.compile(r"\b" + "ki" + r"pp\b", re.IGNORECASE),
    ]
    findings = []
    for relative in listed:
        path = ROOT / relative
        if path.suffix.lower() not in text_suffixes:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            if pattern.search(text):
                findings.append((relative, pattern.pattern))
    assert findings == []


def test_claude_install_guidance_uses_the_current_supported_paths():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")

    assert "curl -fsSL https://claude.ai/install.sh | bash" in readme
    assert "Node.js 22" in readme
    assert "Node 18" not in readme
    assert "npm install -g --allow-scripts=" not in readme
    assert "npm install -g @anthropic-ai/claude-code" not in setup
    assert "npm install -g @anthropic-ai/claude-code" not in bootstrap
    assert "https://claude.ai/install.sh" in setup
    assert "https://claude.ai/install.sh" in bootstrap


def test_readme_explains_the_gitless_bootstrap_boundary():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "git: command not found" in readme
    assert "bootstrap.sh` cannot create for itself" in readme


@pytest.mark.parametrize(
    ("manager", "install_command"),
    (
        ("apt-get", "sudo apt-get install -y curl"),
        ("dnf", "sudo dnf install -y curl"),
    ),
)
def test_linux_dry_run_installs_missing_curl_before_any_download(
    tmp_path, manager, install_command
):
    checkout = _bootstrap_checkout(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    attempted = tmp_path / "package-manager-ran"
    package_manager = fake_bin / manager
    package_manager.write_text(
        f"#!/bin/sh\nprintf attempted > {attempted!s}\nexit 99\n", encoding="utf-8"
    )
    package_manager.chmod(0o755)

    env = _bootstrap_env(tmp_path, platform="Linux", hide_curl=True)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = _run_bootstrap(checkout, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    curl_step = combined.index(install_command)
    download_step = combined.index("curl -fsSL https://claude.ai/install.sh | bash")
    assert curl_step < download_step
    assert "Nothing has been changed yet." in combined
    assert "Dry run: stopping here." in combined
    assert not attempted.exists(), "dry-run must not invoke the package manager"


def test_missing_curl_does_not_block_a_desktop_only_install(tmp_path):
    checkout = _bootstrap_checkout(tmp_path)
    desktop = tmp_path / "Claude.app"
    desktop.mkdir()
    env = _bootstrap_env(
        tmp_path,
        platform="Linux",
        hide_curl=True,
        desktop=str(desktop),
    )

    result = _run_bootstrap(checkout, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "desktop app" in combined
    assert "Everything CareerKit needs is already installed." in combined
    assert "install -y curl" not in combined


def test_unusable_claude_is_offered_a_native_repair(tmp_path):
    checkout = _bootstrap_checkout(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    claude = fake_bin / "claude"
    claude.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    claude.chmod(0o755)
    apt_get = fake_bin / "apt-get"
    apt_get.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    apt_get.chmod(0o755)
    env = _bootstrap_env(
        tmp_path,
        platform="Linux",
        hide_curl=False,
        hide_claude=False,
    )
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    result = _run_bootstrap(checkout, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "present but unusable; repair required" in combined
    assert "curl -fsSL https://claude.ai/install.sh | bash" in combined
    assert "Dry run: stopping here." in combined


def test_macos_without_system_curl_stops_before_planning_a_download(tmp_path):
    checkout = _bootstrap_checkout(tmp_path)
    env = _bootstrap_env(tmp_path, platform="Darwin", hide_curl=True)

    result = _run_bootstrap(checkout, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "MISSING  curl" in combined
    assert "https://curl.se/download.html" in combined
    assert "This will install:" not in combined
    assert "Nothing has been changed yet." not in combined
