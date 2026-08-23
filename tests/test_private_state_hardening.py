"""Regressions for private state creation and non-mutating diagnostics."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from engine import http, privatefs, store
from engine.models import Job


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_writable_state_is_private_under_umask_022(tmp_path, monkeypatch):
    home = tmp_path / "new-careerkit-home"
    db_path = home / "data" / "jobs.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)

    previous_umask = os.umask(0o022)
    try:
        con = store.connect()
        con.execute(
            "INSERT INTO source_health(source,last_ok,last_count) VALUES (?,?,?)",
            ("fixture", "2026-08-23", 1),
        )
        con.commit()
        with store.RunLock():
            assert _mode(db_path.with_suffix(".lock")) == 0o600
        db_path.with_suffix(".lock").chmod(0o644)
        with store.RunLock():
            assert _mode(db_path.with_suffix(".lock")) == 0o600
    finally:
        os.umask(previous_umask)

    try:
        assert _mode(home) == 0o700
        assert _mode(home / "data") == 0o700
        assert _mode(db_path) == 0o600
        sidecars = [Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
        assert all(path.exists() for path in sidecars)
        assert {_mode(path) for path in sidecars} == {0o600}
    finally:
        con.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_writable_open_hardens_existing_database_and_state_directory(
    tmp_path, monkeypatch,
):
    data = tmp_path / "existing-home" / "data"
    data.mkdir(parents=True)
    db_path = data / "jobs.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)

    con = store.connect()
    con.execute(
        "INSERT INTO source_health(source,last_ok,last_count) VALUES (?,?,?)",
        ("fixture", "2026-08-23", 1),
    )
    con.commit()
    sidecars = [Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
    assert all(path.exists() for path in sidecars)
    data.chmod(0o755)
    for path in (db_path, *sidecars):
        path.chmod(0o644)

    second = store.connect()
    try:
        assert _mode(data) == 0o700
        assert _mode(db_path) == 0o600
        assert {_mode(path) for path in sidecars} == {0o600}
    finally:
        second.close()
        con.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_http_cache_replace_is_atomic_private_and_hardens_cache_hits(
    tmp_path, monkeypatch,
):
    cache_dir = tmp_path / "new-home" / "data" / "httpcache"
    monkeypatch.setattr(http, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(http, "_pruned", True)
    http.set_cache_enabled(True)
    url = "https://jobs.example.test/opening/1"

    response = requests.Response()
    response.status_code = 200
    response._content = b"private job description"
    response.url = url
    monkeypatch.setattr(
        http,
        "_request_following_redirects",
        lambda *_args, **_kwargs: (response, url),
    )

    real_replace = os.replace
    real_fsync = os.fsync
    replacements = []
    synced = []

    def checked_replace(source, destination):
        source, destination = Path(source), Path(destination)
        replacements.append((source.name, destination))
        assert source.parent == destination.parent == cache_dir
        assert json.loads(source.read_text(encoding="utf-8"))["text"] == (
            "private job description"
        )
        assert _mode(source) == 0o600
        real_replace(source, destination)

    monkeypatch.setattr(privatefs.os, "replace", checked_replace)

    def checked_fsync(fd):
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(privatefs.os, "fsync", checked_fsync)

    previous_umask = os.umask(0o022)
    try:
        assert http.fetch(url, tries=1) == (200, "private job description")
    finally:
        os.umask(previous_umask)

    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 1
    cache_file = cache_files[0]
    assert len(replacements) == 1 and replacements[0][1] == cache_file
    assert len(synced) == 1
    assert not list(cache_dir.glob(".*.tmp"))
    for directory in (tmp_path / "new-home", cache_dir.parent, cache_dir):
        assert _mode(directory) == 0o700
    assert _mode(cache_file) == 0o600

    # A cache created by an older build is tightened through its open file
    # descriptor before the cached response is returned.
    cache_file.chmod(0o644)
    monkeypatch.setattr(
        http,
        "_request_following_redirects",
        lambda *_args, **_kwargs: pytest.fail("fresh cache must avoid the network"),
    )
    assert http.fetch(url, tries=1) == (200, "private job description")
    assert _mode(cache_file) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_external_parent_mode_is_preserved_while_new_children_and_file_are_private(
    tmp_path,
):
    shared_parent = tmp_path / "owner-selected-shared-directory"
    shared_parent.mkdir(mode=0o755)
    shared_parent.chmod(0o755)
    nested = shared_parent / "new-private-child" / "profile"

    privatefs.ensure_private_directory(nested, harden_existing=False)
    privatefs.atomic_write_private_text(
        shared_parent / "configured-tracker.md",
        "private evidence\n",
        harden_parent=False,
    )

    assert _mode(shared_parent) == 0o755
    assert _mode(nested.parent) == 0o700
    assert _mode(nested) == 0o700
    assert _mode(shared_parent / "configured-tracker.md") == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_careerkit_state_writers_are_private_without_chmodding_external_parent(
    tmp_path, monkeypatch,
):
    import careerkit
    from engine import analytics, applied, report

    external_parent = tmp_path / "owner-selected-external-parent"
    external_parent.mkdir(mode=0o755)
    external_parent.chmod(0o755)
    monkeypatch.setattr(careerkit, "PROFILE", external_parent / "profile.yaml")
    monkeypatch.setattr(careerkit, "EMPLOYERS", external_parent / "employers.yaml")
    monkeypatch.setattr(careerkit, "TRACKER", external_parent / "tracker.md")

    careerkit.cmd_privacy(SimpleNamespace(accept=True))
    careerkit.save_employers({"employers": [], "feeds": []})
    careerkit._append_tracker_sync([{"line": "- 2026-08-23 | Example role"}])
    applications = external_parent / "applications.jsonl"
    applied.write_evidence(applications, [{
        "company": "Example Co",
        "title": "CRM Consultant",
        "status": "applied",
    }])

    assert _mode(external_parent) == 0o755
    for path in (
        external_parent / "setup-progress.md",
        careerkit.EMPLOYERS,
        careerkit.TRACKER,
        applications,
    ):
        assert _mode(path) == 0o600

    owned_home = tmp_path / "owned-careerkit-home"
    db_path = owned_home / "data" / "jobs.db"
    out_dir = owned_home / "out"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    monkeypatch.setattr(report, "OUT_DIR", out_dir)
    monkeypatch.setattr(analytics, "OUT_DIR", out_dir)
    con = store.connect()
    try:
        report_path = report.write_report(
            con, [], health=[], run_detail={"pulled": 0, "sources_ok": 0},
        )
        snapshot = analytics.build_snapshot(con, [])
        dashboard_path = analytics.write_dashboard(con, snapshot, [])

        assert _mode(owned_home) == 0o700
        assert _mode(db_path.parent) == 0o700
        assert _mode(out_dir) == 0o700
        for path in (report_path, out_dir / "latest.md", dashboard_path):
            assert _mode(path) == 0o600
    finally:
        con.close()


def test_setup_and_onboarding_contracts_reassert_private_modes():
    root = Path(__file__).resolve().parents[1]
    setup = (root / "setup.sh").read_text(encoding="utf-8")
    setup_skill = (root / ".claude/skills/setup/SKILL.md").read_text(encoding="utf-8")
    apply_skill = (root / ".claude/skills/apply/SKILL.md").read_text(encoding="utf-8")

    assert "(umask 077; mkdir -p profile data out)" in setup
    assert "find profile data out -type d -exec chmod 700" in setup
    assert "find profile data out -type f -exec chmod 600" in setup
    assert "find profile data out -type f -exec chmod 600" in setup_skill
    assert "chmod 600 profile/applications.jsonl profile/tracker.md" in apply_skill


def test_readonly_connect_preserves_database_bytes_mtime_and_live_wal(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "home" / "data" / "jobs.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    writer = store.connect()
    writer.execute(
        "INSERT INTO source_health(source,last_ok,last_count) VALUES (?,?,?)",
        ("fixture", "2026-08-23", 1),
    )
    writer.commit()
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")
    assert wal_path.exists(), "fixture must keep a live WAL beside the writer"
    assert shm_path.exists(), "fixture must exercise WAL shared-memory state"

    # SQLite readers may coordinate through the transient shared-memory index;
    # the durable database and WAL payloads are the non-mutation contract.
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (db_path, wal_path)
    }
    monkeypatch.setattr(
        store,
        "_migrate",
        lambda _con: pytest.fail("readonly diagnostics must not migrate"),
    )
    monkeypatch.setattr(
        store,
        "ensure_private_directory",
        lambda _path: pytest.fail("readonly diagnostics must not create state"),
    )
    monkeypatch.setattr(
        store,
        "prepare_private_file",
        lambda _path: pytest.fail("readonly diagnostics must not create files"),
    )

    reader = store.connect(readonly=True)
    try:
        assert reader.execute(
            "SELECT last_count FROM source_health WHERE source='fixture'"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM source_health")
    finally:
        reader.close()

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (db_path, wal_path)
    }
    assert after == before
    writer.close()


def test_readonly_connect_does_not_create_a_missing_database(tmp_path, monkeypatch):
    db_path = tmp_path / "absent" / "data" / "jobs.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)

    with pytest.raises(sqlite3.OperationalError):
        store.connect(readonly=True)
    assert not db_path.exists()
    assert not db_path.parent.exists()


def _workday_job(*, external_id: str, url: str, board: str = "workday:acme") -> Job:
    job = Job(
        company="Acme Corporation",
        title="Salesforce Technical Consultant",
        url=url,
        source="workday",
        board=board,
        external_id=external_id,
        location="London, United Kingdom",
        description=f"fresh detail for {external_id}",
    )
    job.score, job.gate, job.reasons = 72, "QUALIFIED", ["strong fit"]
    return job


def test_workday_path_identity_preserves_legacy_bullet_uid_and_history(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "data" / "jobs.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    con = store.connect()
    try:
        legacy = _workday_job(
            external_id="LONDON, UNITED KINGDOM",
            url=("https://Acme.wd1.myworkdayjobs.com:443/en-US/careers/"
                 "job/London/Salesforce-Technical-Consultant_R-100#listing"),
        )
        store.upsert(con, [legacy])
        legacy_uid = legacy.uid
        con.execute(
            "UPDATE jobs SET status='applied', notes='preserve user decision' "
            "WHERE uid=?",
            (legacy_uid,),
        )
        con.execute(
            "INSERT INTO events(uid,at,kind,detail) VALUES (?,?,?,?)",
            (legacy_uid, "2026-08-20T10:00:00", "application:applied", "evidence"),
        )
        con.commit()

        fresh = _workday_job(
            external_id="/en-US/careers/job/London/"
                        "Salesforce-Technical-Consultant_R-100",
            url=("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/"
                 "London/Salesforce-Technical-Consultant_R-100"),
        )
        fresh.description = "fresh provider-owned path detail"
        assert fresh.uid != legacy_uid

        new, again = store.upsert(con, [fresh])

        rows = con.execute("SELECT * FROM jobs").fetchall()
        assert len(rows) == 1
        assert (rows[0]["uid"], rows[0]["status"], rows[0]["notes"]) == (
            legacy_uid,
            "applied",
            "preserve user decision",
        )
        assert rows[0]["description"] == "fresh provider-owned path detail"
        assert fresh.uid == legacy_uid
        assert new == [] and again == [fresh]
        assert con.execute(
            "SELECT uid FROM events WHERE kind='application:applied'"
        ).fetchone()[0] == legacy_uid
        assert con.execute("SELECT uid FROM sightings").fetchone()[0] == legacy_uid
    finally:
        con.close()


@pytest.mark.parametrize(
    ("source", "board", "company"),
    [
        ("greenhouse", "greenhouse:acme", "Acme Corporation"),
        ("workday", "workday:other", "Acme Corporation"),
        ("workday", "workday:acme", "Different Employer"),
    ],
)
def test_workday_identity_bridge_fails_closed_outside_exact_native_scope(
    tmp_path, monkeypatch, source, board, company,
):
    db_path = tmp_path / f"{source}-{board.replace(':', '-')}" / "jobs.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    con = store.connect()
    try:
        url = "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Role_R-100"
        legacy = _workday_job(external_id="Swindon", url=url)
        store.upsert(con, [legacy])

        other = Job(
            company=company,
            title=legacy.title,
            url=url,
            source=source,
            board=board,
            external_id="/en-US/careers/job/Role_R-100",
            description="must stay separate",
        )
        other.score, other.gate, other.reasons = 70, "QUALIFIED", ["fit"]
        assert other.uid != legacy.uid

        store.upsert(con, [other])

        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert {row[0] for row in con.execute("SELECT uid FROM jobs")} == {
            legacy.uid,
            other.uid,
        }
    finally:
        con.close()
