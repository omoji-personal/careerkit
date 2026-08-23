"""Owner-only creation contracts for CareerKit's non-database artifacts."""
from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

import pytest

import careerkit
from engine import applied, report, store


pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Windows does not expose meaningful POSIX mode bits",
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_configured_profile_artifacts_secure_files_not_shared_parent(
        tmp_path, monkeypatch):
    shared = tmp_path / "shared-config-root"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    monkeypatch.setattr(careerkit, "PROFILE", shared / "profile.yaml")
    monkeypatch.setattr(careerkit, "EMPLOYERS", shared / "employers.yaml")

    previous = os.umask(0o022)
    try:
        careerkit.cmd_privacy(argparse.Namespace(accept=True))
        careerkit.save_employers({"employers": [], "feeds": []})
        applied.write_evidence(
            shared / "applications.jsonl",
            [{"company": "Acme", "title": "CRM Lead", "status": "applied"}],
        )
    finally:
        os.umask(previous)

    assert _mode(shared) == 0o755
    for name in ("setup-progress.md", "employers.yaml", "applications.jsonl"):
        assert _mode(shared / name) == 0o600


def test_internal_reports_queue_and_tracker_are_owner_only(
        tmp_path, monkeypatch):
    out = tmp_path / "private-home" / "out"
    queue = tmp_path / "private-home" / "data" / "discovered-pending.json"
    tracker = tmp_path / "private-home" / "profile" / "tracker.md"
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "fixture" / "jobs.db")
    monkeypatch.setattr(report, "OUT_DIR", out)
    monkeypatch.setattr(careerkit, "DISCOVERED_QUEUE", queue)
    monkeypatch.setattr(careerkit, "TRACKER", tracker)

    previous = os.umask(0o022)
    con = store.connect()
    try:
        path = report.write_report(con, [], health=[], run_detail={})
        careerkit.queue_discovered([
            {"name": "Acme", "ats": "greenhouse", "slug": "acme"},
        ])
        careerkit._append_tracker_sync([{"line": "- [applied] Example"}])
    finally:
        con.close()
        os.umask(previous)

    for directory in (out, queue.parent, tracker.parent):
        assert _mode(directory) == 0o700
    for private_file in (path, out / "latest.md", queue, tracker):
        assert _mode(private_file) == 0o600


def test_setup_births_and_upgrades_only_declared_state_roots_privately():
    setup = (Path(__file__).resolve().parents[1] / "setup.sh").read_text(
        encoding="utf-8",
    )

    assert "(umask 077; mkdir -p profile data out)" in setup
    assert "find profile data out -type d -exec chmod 700" in setup
    assert "find profile data out -type f -exec chmod 600" in setup
    assert "chmod 600 profile/employers.yaml" in setup


def test_status_on_a_fresh_instance_does_not_create_a_database(
        tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "new-home" / "data" / "jobs.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    monkeypatch.setattr(
        careerkit, "load_registry",
        lambda **_kwargs: {"employers": [], "feeds": []},
    )

    careerkit.cmd_status(argparse.Namespace())

    assert "postings tracked : 0" in capsys.readouterr().out
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_coverage_opens_existing_state_readonly(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "home" / "data" / "jobs.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    writer = store.connect()
    writer.close()
    original_connect = store.connect
    calls = []

    def checked_connect(*, readonly=False):
        calls.append(readonly)
        return original_connect(readonly=readonly)

    monkeypatch.setattr(store, "connect", checked_connect)
    monkeypatch.setattr(
        careerkit, "load_registry",
        lambda **_kwargs: {"employers": [], "feeds": []},
    )
    monkeypatch.setattr(
        careerkit, "load_profile",
        lambda: argparse.Namespace(search_terms=["crm"]),
    )
    monkeypatch.setattr(careerkit, "load_yaml", lambda *_args, **_kwargs: {})

    careerkit.cmd_coverage(argparse.Namespace(json=False))

    assert calls == [True]
    assert "Source coverage" in capsys.readouterr().out


def test_manual_database_backup_is_owner_only(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "home" / "data" / "jobs.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    writer = store.connect()
    writer.close()

    careerkit.cmd_db(argparse.Namespace(action="backup"))

    backups = list(db_path.parent.glob("jobs.backup-*.db"))
    assert len(backups) == 1
    assert _mode(backups[0]) == 0o600
    assert "Backed up to" in capsys.readouterr().out
