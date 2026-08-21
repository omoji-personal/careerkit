"""First-run readiness contracts discovered by the onboarding TAA."""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import careerkit  # noqa: E402
from engine import store  # noqa: E402


def _bind_home(monkeypatch, tmp_path: Path) -> Path:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    monkeypatch.setattr(careerkit, "PROFILE", profile_dir / "profile.yaml")
    monkeypatch.setattr(careerkit, "EMPLOYERS", profile_dir / "employers.yaml")
    monkeypatch.setattr(careerkit, "KEYS", profile_dir / "keys.yaml")
    monkeypatch.setattr(careerkit, "TRACKER", profile_dir / "tracker.md")
    monkeypatch.setattr(careerkit, "APPLICATIONS", profile_dir / "applications.jsonl")
    monkeypatch.setattr(careerkit, "CLAIMS", profile_dir / "claims.md")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    return profile_dir


def _write_checkpoint(
        profile_dir: Path, *, privacy: str = "complete",
        search_core: str = "complete") -> None:
    (profile_dir / "setup-progress.md").write_text(
        f"Privacy: {privacy}\nSearch Core: {search_core}\nFirst Win: skipped\n"
        "Source Expansion: deferred\nCareer Pack: deferred\n"
        "Final Checks: complete\n",
        encoding="utf-8",
    )


def _write_complete_checkpoint(profile_dir: Path) -> None:
    _write_checkpoint(profile_dir)


def test_doctor_reports_all_first_run_gaps_without_creating_a_database(
        tmp_path, monkeypatch, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    (profile_dir / "employers.yaml").write_text(
        "employers: []\nfeeds:\n- {name: remotive, active: true}\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_doctor(argparse.Namespace())

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "CareerKit check: personal state not inspected" in output
    assert "no profile/profile.yaml" in output
    assert "no setup progress checklist or privacy acknowledgment" in output
    assert "contents were not inspected before privacy acknowledgment" in output
    assert "remotive" not in output
    assert not store.DB_PATH.exists(), "a read-only readiness check created state"


def test_an_explicitly_disabled_privacy_feed_is_a_note_not_a_failure(
        tmp_path, monkeypatch, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        (ROOT / "profile.example" / "profile.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    registry = {
        "employers": [{
            "name": "Acme", "ats": "greenhouse", "slug": "acme", "active": True,
        }],
        "feeds": [{"name": "freehire", "active": False}],
    }
    careerkit.EMPLOYERS.write_text(
        yaml.safe_dump(registry, sort_keys=False), encoding="utf-8"
    )
    _write_complete_checkpoint(profile_dir)
    con = store.connect()
    store.record_health(con, "greenhouse:acme", 1, None)
    run_id = store.start_run(con)
    store.finish_run(con, run_id, 1, 0, 0, {})

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_doctor(argparse.Namespace())

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "no blocking problems found" in output
    assert "optional feeds left disabled by choice: freehire" in output
    assert "dormant or opt-in feeds" not in output


def test_profile_lint_rejects_an_interrupted_empty_search_profile(
        tmp_path, monkeypatch, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text("{}\n", encoding="utf-8")
    _write_checkpoint(profile_dir, search_core="pending")

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_profile_lint(argparse.Namespace())

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "no lanes defined" in captured.out + captured.err
    assert "profile lint: clean" not in captured.out


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            {"lanes": [{"key": "target", "titles": []}],
             "search_terms": ["salesforce"]},
            "has no titles",
        ),
        (
            {"lanes": [{"key": "target", "titles": ["consultant"]}],
             "search_terms": []},
            "no search_terms defined",
        ),
        (
            {"lanes": [{"key": "target", "titles": ["/[invalid/"]}],
             "search_terms": ["salesforce"]},
            "no usable lane title patterns",
        ),
    ],
)
def test_profile_lint_rejects_an_unusable_search_core(
        profile, expected, tmp_path, monkeypatch, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        yaml.safe_dump(profile, sort_keys=False), encoding="utf-8"
    )
    _write_checkpoint(profile_dir, search_core="pending")

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_profile_lint(argparse.Namespace())

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert expected in output
    assert "profile lint: clean" not in output


def test_profile_lint_keeps_the_public_example_clean(tmp_path, monkeypatch, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        (ROOT / "profile.example" / "profile.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _write_checkpoint(profile_dir, search_core="pending")

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_profile_lint(argparse.Namespace())

    assert exc.value.code == 0
    assert "profile lint: clean" in capsys.readouterr().out


def test_public_example_separates_preferences_from_capability_boundaries():
    raw = yaml.safe_load(
        (ROOT / "profile.example" / "profile.yaml").read_text(encoding="utf-8")
    )
    exclusions = raw["exclusions"]

    assert exclusions["titles"]
    assert exclusions["titles_always"]
    assert all("software|data" not in str(rule) for rule in exclusions["titles"])
    assert any("software|data" in str(rule)
               for rule in exclusions["titles_always"])


def _healthy_first_run(tmp_path, monkeypatch) -> Path:
    profile_dir = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        (ROOT / "profile.example" / "profile.yaml").read_text(encoding="utf-8"),
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
    con = store.connect()
    store.record_health(con, "greenhouse:acme", 1, None)
    run_id = store.start_run(con)
    store.finish_run(con, run_id, 1, 0, 0, {})
    return profile_dir


def test_missing_privacy_checkpoint_blocks_sourcing_and_fails_doctor(
        tmp_path, monkeypatch, capsys):
    _bind_home(monkeypatch, tmp_path)
    # If this file were parsed, the YAML reader would fail. The privacy gate must
    # win before any personal profile content is opened.
    careerkit.PROFILE.write_text("lanes: [\n", encoding="utf-8")
    careerkit.EMPLOYERS.write_text(
        "employers: []\nfeeds:\n- {name: remotive, active: true}\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as blocked:
        careerkit.load_profile()
    # A profile with no checkpoint is an upgraded install, so the refusal must
    # offer the migration rather than send a working setup back to onboarding.
    assert "privacy --accept" in str(blocked.value)

    with pytest.raises(SystemExit) as diagnosed:
        careerkit.cmd_doctor(argparse.Namespace())
    assert diagnosed.value.code == 1
    output = capsys.readouterr().out
    assert "no setup progress checklist or privacy acknowledgment" in output
    assert "contents were not inspected before privacy acknowledgment" in output
    assert "remotive" not in output
    assert "no blocking problems found" not in output


_MALFORMED_CHECKPOINT_SECRET = "PrivateCheckpointValue"
_PRIVACY_CHECKPOINT_CASES = (
    pytest.param(None, "privacy --accept", id="absent"),
    pytest.param(
        "Privacy: complete\nSearch Core: pending\nFirst Win: pending\n"
        "Source Expansion: pending\nCareer Pack: pending\nFinal Checks: pending\n"
        f"{_MALFORMED_CHECKPOINT_SECRET}\n",
        "checkpoint is incomplete or malformed",
        id="malformed",
    ),
    pytest.param(
        "Privacy: pending\nSearch Core: pending\nFirst Win: pending\n"
        "Source Expansion: pending\nCareer Pack: pending\nFinal Checks: pending\n",
        "privacy acknowledgment is not complete",
        id="privacy-incomplete",
    ),
)

_PRIVACY_SENSITIVE_COMMANDS = (
    pytest.param(["discover", "Private Target Employer"], id="discover-network"),
    pytest.param(["verify"], id="verify-network"),
    pytest.param(["enrich"], id="enrich-network"),
    pytest.param(["profile-lint"], id="profile-read"),
    pytest.param(["status"], id="status-read"),
    pytest.param(["coverage"], id="coverage-read"),
    pytest.param(["db", "check"], id="database-read"),
    pytest.param(["voice-lint", "private-draft.txt"], id="voice-read"),
    pytest.param(["claims-lint", "private-draft.txt"], id="claims-read"),
)


@pytest.mark.parametrize(("checkpoint", "expected"), _PRIVACY_CHECKPOINT_CASES)
@pytest.mark.parametrize("argv", _PRIVACY_SENSITIVE_COMMANDS)
def test_dispatch_privacy_policy_blocks_sensitive_commands_before_dispatch(
        checkpoint, expected, argv, tmp_path, monkeypatch, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    private_value = "ConfidentialProfileValue"
    careerkit.PROFILE.write_text(
        f"comp:\n  screen_floor: {private_value}\n", encoding="utf-8"
    )
    if checkpoint is not None:
        (profile_dir / "setup-progress.md").write_text(
            checkpoint, encoding="utf-8"
        )

    def dispatched_too_early(*_args, **_kwargs):
        raise AssertionError("privacy-sensitive command reached dispatch")

    # main() classifies writes immediately before invoking a handler. Reaching
    # this sentinel therefore means registry/DB/profile or network work can run.
    monkeypatch.setattr(careerkit, "command_writes", dispatched_too_early)

    with pytest.raises(SystemExit) as blocked:
        careerkit.main(argv)

    diagnostic = str(blocked.value) + capsys.readouterr().out
    assert expected in diagnostic
    assert private_value not in diagnostic
    assert _MALFORMED_CHECKPOINT_SECRET not in diagnostic


@pytest.mark.parametrize(("checkpoint", "expected"), _PRIVACY_CHECKPOINT_CASES)
def test_profile_lint_search_core_exception_never_bypasses_privacy(
        checkpoint, expected, tmp_path, monkeypatch, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    private_value = "ConfidentialProfileValue"
    careerkit.PROFILE.write_text(
        f"comp:\n  screen_floor: {private_value}\n", encoding="utf-8"
    )
    if checkpoint is not None:
        (profile_dir / "setup-progress.md").write_text(
            checkpoint, encoding="utf-8"
        )

    with pytest.raises(SystemExit) as blocked:
        careerkit.cmd_profile_lint(argparse.Namespace())

    diagnostic = str(blocked.value) + capsys.readouterr().out
    assert expected in diagnostic
    assert private_value not in diagnostic
    assert _MALFORMED_CHECKPOINT_SECRET not in diagnostic


def test_pre_privacy_dispatch_allowlist_contains_only_the_limited_doctor(
        tmp_path, monkeypatch):
    _bind_home(monkeypatch, tmp_path)
    called = []

    def limited_doctor(args):
        called.append(args.cmd)

    # Only two commands may run before acknowledgment: the limited diagnostic,
    # and the command that presents the disclosure and records the answer.
    # Anything else joining this set is a hole in the gate.
    assert careerkit.PRE_PRIVACY_COMMANDS == frozenset({"doctor", "privacy"})
    monkeypatch.setattr(careerkit, "cmd_doctor", limited_doctor)
    monkeypatch.setattr(careerkit, "command_writes", lambda _args: False)

    careerkit.main(["doctor"])

    assert called == ["doctor"]


def test_help_exits_before_privacy_command_dispatch(tmp_path, monkeypatch, capsys):
    _bind_home(monkeypatch, tmp_path)

    def unexpected_gate():
        raise AssertionError("--help reached privacy command dispatch")

    monkeypatch.setattr(careerkit, "require_privacy_checkpoint", unexpected_gate)

    with pytest.raises(SystemExit) as helped:
        careerkit.main(["--help"])

    assert helped.value.code == 0
    assert "CareerKit sourcing CLI" in capsys.readouterr().out


def test_completed_checkpoint_cannot_override_an_unusable_profile(
        tmp_path, monkeypatch):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text("{}\n", encoding="utf-8")
    _write_complete_checkpoint(profile_dir)

    with pytest.raises(SystemExit) as blocked:
        careerkit.load_profile()

    assert "no lanes defined" in str(blocked.value)
    assert "no search_terms defined" in str(blocked.value)


def test_doctor_rejects_a_checkpoint_with_an_incomplete_search_core(
        tmp_path, monkeypatch, capsys):
    profile_dir = _healthy_first_run(tmp_path, monkeypatch)
    (profile_dir / "setup-progress.md").write_text(
        "Privacy: complete\nSearch Core: pending\nFirst Win: pending\n"
        "Source Expansion: pending\nCareer Pack: pending\nFinal Checks: pending\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_doctor(argparse.Namespace())

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "onboarding Search Core is incomplete" in output


def test_doctor_accepts_a_search_ready_checkpoint_with_optional_work_deferred(
        tmp_path, monkeypatch, capsys):
    profile_dir = _healthy_first_run(tmp_path, monkeypatch)
    (profile_dir / "setup-progress.md").write_text(
        "Privacy: complete\nSearch Core: complete\nFirst Win: skipped\n"
        "Source Expansion: deferred\nCareer Pack: deferred\nFinal Checks: complete\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_doctor(argparse.Namespace())

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "no blocking problems found" in output
    assert "optional onboarding phases not complete" in output


def test_doctor_accepts_an_explicitly_deferred_feed_only_search(
        tmp_path, monkeypatch, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        (ROOT / "profile.example" / "profile.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    careerkit.EMPLOYERS.write_text(
        "employers: []\nfeeds:\n- {name: remotive, active: true}\n",
        encoding="utf-8",
    )
    _write_complete_checkpoint(profile_dir)
    con = store.connect()
    store.record_health(con, "feed:remotive", 1, None)
    run_id = store.start_run(con)
    store.finish_run(con, run_id, 1, 0, 0, {})

    with pytest.raises(SystemExit) as exc:
        careerkit.cmd_doctor(argparse.Namespace())

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "search remains limited to operational feeds" in output
    assert "no active employers registered" not in output


def test_setup_checkpoint_parser_never_echoes_unrecognised_private_text(tmp_path):
    secret = "My employer is ExampleCorp and my salary is 123456"
    checkpoint = tmp_path / "setup-progress.md"
    checkpoint.write_text(
        "- Privacy: complete\n- Search Core: complete\n"
        "- First Win: skipped\n- Source Expansion: deferred\n"
        "- Career Pack: deferred\n- Final Checks: pending\n"
        f"{secret}\n",
        encoding="utf-8",
    )

    states, issues = careerkit.onboarding_progress(checkpoint)

    assert states["Search Core"] == "complete"
    assert issues == ["contains a line outside the phase checklist"]
    assert secret not in repr(issues)


def test_sourcing_stops_at_an_explicitly_incomplete_search_core_but_lint_can_repair_it(
        tmp_path, monkeypatch, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        (ROOT / "profile.example" / "profile.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (profile_dir / "setup-progress.md").write_text(
        "Privacy: complete\nSearch Core: pending\nFirst Win: pending\n"
        "Source Expansion: pending\nCareer Pack: pending\nFinal Checks: pending\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as blocked:
        careerkit.load_profile()
    assert "Search Core is not complete" in str(blocked.value)

    with pytest.raises(SystemExit) as linted:
        careerkit.cmd_profile_lint(argparse.Namespace())
    assert linted.value.code == 0
    assert "profile lint: clean" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("no_cache", "expected"),
    [
        (True, "cache bypassed; fresh fetch requested"),
        (False, "normal 6-hour cache"),
    ],
)
def test_pull_passes_its_cache_scope_into_the_durable_report_metadata(
        tmp_path, monkeypatch, no_cache, expected):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    careerkit.PROFILE.write_text(
        (ROOT / "profile.example" / "profile.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    careerkit.EMPLOYERS.write_text(
        "employers: []\nfeeds:\n- {name: freehire, active: false}\n",
        encoding="utf-8",
    )
    _write_complete_checkpoint(profile_dir)
    report_path = tmp_path / "out" / "sourcing.md"
    report_path.parent.mkdir()
    report_path.write_text("# scoped report\n", encoding="utf-8")
    captured = {}

    def fake_run_pull(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "path": report_path,
            "run_id": 1,
            "pulled": 0,
            "kept": 0,
            "excluded": Counter(),
            "new": 0,
            "qualified": 0,
            "delisted": 0,
            "demoted": 0,
        }

    monkeypatch.setattr(store, "connect", lambda: object())
    monkeypatch.setattr(store, "dropped_to_zero", lambda _con: [])
    monkeypatch.setattr(careerkit._pull, "run_pull", fake_run_pull)
    monkeypatch.setattr(careerkit, "take_discovered", lambda: [])

    careerkit.cmd_pull(argparse.Namespace(
        no_cache=no_cache,
        employers=False,
        feeds=True,
        tier=None,
        min_score=0,
    ))

    assert captured["cache_mode"] == expected
    assert captured["feeds_only"] is True


# --- upgrade path: installs that predate the checkpoint ----------------------
#
# The checkpoint became mandatory for every command after installs already
# existed. A user with a mature profile and no checkpoint was refused by the
# gate and pointed at /setup, which is the one thing they cannot reach and which
# sounds like it will redo the profile they already have.


def _accept_args(accept: bool) -> argparse.Namespace:
    return argparse.Namespace(cmd="privacy", accept=accept)


def test_an_install_that_predates_the_checkpoint_is_told_how_to_upgrade(
        monkeypatch, tmp_path, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    (profile_dir / "profile.yaml").write_text("lanes: []\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        careerkit.require_privacy_checkpoint()

    message = str(exit_info.value)
    assert "predates that" in message
    assert "privacy --accept" in message
    assert "has not been touched" in message


def test_a_genuinely_new_install_is_still_sent_to_setup(monkeypatch, tmp_path):
    """No profile means onboarding, not a migration: the original routing must
    survive, or /setup stops being the front door for new users."""
    _bind_home(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        careerkit.require_privacy_checkpoint()

    message = str(exit_info.value)
    assert "/setup" in message
    assert "privacy --accept" not in message


def test_privacy_shows_the_disclosure_without_recording_anything(
        monkeypatch, tmp_path, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)

    careerkit.cmd_privacy(_accept_args(False))

    out = capsys.readouterr().out
    for topic in ("Anthropic", "employer boards", "freehire.me", "telemetry"):
        assert topic in out
    assert not (profile_dir / "setup-progress.md").exists()


def test_accept_records_a_checkpoint_the_gate_accepts(monkeypatch, tmp_path, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    (profile_dir / "profile.yaml").write_text("lanes: []\n", encoding="utf-8")

    careerkit.cmd_privacy(_accept_args(True))

    out = capsys.readouterr().out
    assert "Anthropic" in out, "acceptance must still show what is being accepted"
    assert "not read or changed" in out
    progress = careerkit.require_privacy_checkpoint()
    assert progress["Privacy"] == "complete"
    # A migrated install already has its criteria; sending it back through the
    # Search Core interview would be the upgrade undoing working configuration.
    assert progress["Search Core"] == "complete"


def test_accept_leaves_search_core_pending_when_there_is_no_profile(
        monkeypatch, tmp_path, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)

    careerkit.cmd_privacy(_accept_args(True))

    capsys.readouterr()
    progress, issues = careerkit.onboarding_progress(profile_dir / "setup-progress.md")
    assert not issues
    assert progress["Privacy"] == "complete"
    assert progress["Search Core"] == "pending"


def test_accept_refuses_to_overwrite_a_checkpoint_it_did_not_create(
        monkeypatch, tmp_path, capsys):
    """A malformed checkpoint may encode deliberate state. Rewriting it would
    silently discard whatever /setup recorded there."""
    profile_dir = _bind_home(monkeypatch, tmp_path)
    existing = "Privacy: pending\nSearch Core: complete\n"
    (profile_dir / "setup-progress.md").write_text(existing, encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        careerkit.cmd_privacy(_accept_args(True))

    assert "will not rewrite" in str(exit_info.value)
    assert (profile_dir / "setup-progress.md").read_text(encoding="utf-8") == existing


def test_accept_is_idempotent(monkeypatch, tmp_path, capsys):
    profile_dir = _bind_home(monkeypatch, tmp_path)
    _write_complete_checkpoint(profile_dir)
    before = (profile_dir / "setup-progress.md").read_text(encoding="utf-8")

    careerkit.cmd_privacy(_accept_args(True))

    assert "Already acknowledged" in capsys.readouterr().out
    assert (profile_dir / "setup-progress.md").read_text(encoding="utf-8") == before


def test_privacy_runs_before_acknowledgment(monkeypatch, tmp_path):
    """The command that records the acknowledgment cannot itself require one."""
    _bind_home(monkeypatch, tmp_path)
    careerkit.enforce_privacy_command_policy(_accept_args(False))


def test_the_cli_disclosure_still_covers_what_the_setup_skill_promises():
    """Two copies of a disclosure drift. This pins the topics that must appear
    in both, so a change to one is a visible failure rather than a quiet gap."""
    skill = (ROOT / ".claude" / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
    cli = careerkit.PRIVACY_DISCLOSURE
    for topic in ("Anthropic", "USAJobs", "freehire.me", "telemetry"):
        assert topic in skill, f"{topic} missing from the setup skill"
        assert topic in cli, f"{topic} missing from the CLI disclosure"
