"""What a stranger sees, and what happens to a database that predates a change.

Three defects surfaced in one afternoon by cloning the public repository and
running it cold, none of which any unit test caught:

  - a qualified role whose header said "Comp not stated" directly above a line
    quoting its salary band
  - a screening summary that told the user "domain terms never mentioned in
    postin", clipped mid-word
  - a migration announcing it had backed up a database that did not yet exist

They were all first-run behaviour, which nothing exercised because every other
test starts from a database the test itself built. These start from nothing, the
way a new user does, and from an old schema, the way an existing user does.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest
import yaml


def _fresh(tmp_path, monkeypatch):
    """A CareerKit home with nothing in it, and modules bound to it."""
    monkeypatch.setenv("CAREERKIT_HOME", str(tmp_path))
    for name in [m for m in list(sys.modules) if m.startswith("engine")]:
        importlib.reload(sys.modules[name])
    from engine import store
    return store


def test_one_cli_supports_a_legacy_instance_layout(tmp_path):
    """The personal app predates profile/. It used to copy the CLI, then miss
    every new command and safety fix. Explicit path overrides let a tiny launcher
    use the canonical CLI without moving private state."""
    root = pathlib.Path(__file__).parent.parent
    paths = {
        "CAREERKIT_HOME": tmp_path / "state",
        "CAREERKIT_PROFILE": tmp_path / "profile-omid.yaml",
        "CAREERKIT_EMPLOYERS": tmp_path / "employers.yaml",
        "CAREERKIT_KEYS": tmp_path / "keys.yaml",
        "CAREERKIT_TRACKER": tmp_path / "job-search-tracker.md",
        "CAREERKIT_APPLICATIONS": tmp_path / "applications.jsonl",
        "CAREERKIT_CLAIMS": tmp_path / "VERIFIED_CLAIMS.md",
    }
    env = dict(os.environ, **{k: str(v) for k, v in paths.items()})
    code = ("import careerkit; print('\\n'.join(str(getattr(careerkit, n)) for n in "
            "('ROOT','PROFILE','EMPLOYERS','KEYS','TRACKER','APPLICATIONS','CLAIMS')))" )
    got = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                         capture_output=True, text=True, check=True).stdout.splitlines()
    assert got == [str(p) for p in paths.values()]


def test_tracker_sync_cli_honors_the_legacy_tracker_override(tmp_path):
    """Importing the override is insufficient: the real command must preview
    against that path and leave it untouched without --apply."""
    root = pathlib.Path(__file__).parent.parent
    home = tmp_path / "state"
    tracker = tmp_path / "legacy" / "job-search-tracker.md"
    tracker.parent.mkdir()
    original = "# Existing narrative\nThis must not be rewritten.\n"
    tracker.write_text(original)
    env = dict(os.environ, CAREERKIT_HOME=str(home), CAREERKIT_TRACKER=str(tracker),
               CAREERKIT_VENV="1")
    setup = (
        "from engine import store; from engine.models import Job; "
        "j=Job(company='Acme',title='Architect',url='https://jobs.example.test/1',"
        "source='greenhouse'); j.gate='QUALIFIED'; j.score=70; "
        "store.upsert(store.connect(),[j]); "
        "c=store.connect(); store.set_status(c,j.uid,'applied')"
    )
    subprocess.run([sys.executable, "-c", setup], cwd=root, env=env,
                   capture_output=True, text=True, check=True)
    got = subprocess.run([sys.executable, str(root / "careerkit.py"), "tracker-sync"],
                         cwd=root, env=env, capture_output=True, text=True, check=True)
    assert f"Tracker: {tracker}" in got.stdout
    assert "Canonical-link tracker additions (1)" in got.stdout
    assert "DRY RUN: nothing written" in got.stdout
    assert tracker.read_text() == original


def test_registry_save_is_atomic(tmp_path, monkeypatch):
    import careerkit
    target = tmp_path / "employers.yaml"
    monkeypatch.setattr(careerkit, "EMPLOYERS", target)
    careerkit.save_employers({"employers": [{"name": "Acme"}], "feeds": []})
    assert yaml.safe_load(target.read_text())["employers"][0]["name"] == "Acme"
    assert not list(tmp_path.glob(".*.tmp"))


def test_a_brand_new_database_says_nothing_alarming(tmp_path, monkeypatch, capsys):
    """The first command a new user runs announced that it had backed up their
    database before migrating. There was no database and nothing to lose, and
    the sentence reads like something went wrong."""
    store = _fresh(tmp_path, monkeypatch)
    store.connect()
    out = capsys.readouterr().out
    assert "backed up" not in out.lower(), out
    assert "migrat" not in out.lower(), out


def test_every_command_survives_an_empty_database(tmp_path, monkeypatch):
    """A new user pokes at the tool before running setup. Nothing may raise, and
    nothing may claim a result it does not have."""
    store = _fresh(tmp_path, monkeypatch)
    from engine import applied, consistency, ghost
    con = store.connect()

    assert consistency.check_db(con) == []
    assert ghost.review(con) == []
    assert applied.surfacing_a_closed_door(con) == []
    assert applied.load_evidence(tmp_path / "profile" / "applications.jsonl") == []
    assert store.dropped_to_zero(con) == []


def test_a_database_from_before_the_new_columns_still_opens(tmp_path, monkeypatch):
    """Existing users have rows that predate every column added since. A
    migration that fails, or that strands their applied status, destroys months
    of first_seen dates and application state that cannot be re-derived from any
    board."""
    monkeypatch.setenv("CAREERKIT_HOME", str(tmp_path))
    (tmp_path / "data").mkdir(parents=True)
    db = tmp_path / "data" / "jobs.db"

    # The v1 shape: no group_key, no run stamps, none of today's evidence columns.
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE jobs (
        uid TEXT PRIMARY KEY, company TEXT, title TEXT, url TEXT, location TEXT,
        source TEXT, lane TEXT, employer_tier TEXT, posted_at TEXT, department TEXT,
        comp_min INTEGER, comp_max INTEGER, comp_text TEXT,
        score INTEGER, gate TEXT, reasons TEXT, description TEXT,
        first_seen TEXT, last_seen TEXT, seen_count INTEGER DEFAULT 1,
        status TEXT DEFAULT 'new', notes TEXT DEFAULT '')""")
    con.execute("INSERT INTO jobs (uid, company, title, url, source, gate, score, "
                "status, first_seen, last_seen) VALUES "
                "('legacyuid1','Acme','Salesforce Administrator','https://x/1',"
                "'greenhouse','QUALIFIED',60,'applied','2026-01-02','2026-01-02')")
    con.commit()
    con.close()

    for name in [m for m in list(sys.modules) if m.startswith("engine")]:
        importlib.reload(sys.modules[name])
    from engine import store
    con = store.connect()

    row = con.execute("SELECT * FROM jobs WHERE company='Acme'").fetchone()
    assert row is not None, "the migration lost the user's only row"
    assert row["status"] == "applied", "the migration lost an application record"
    assert row["first_seen"] == "2026-01-02", "the migration rewrote history"
    # every column added since must exist and be readable
    for col in ("group_key", "remote_flag", "rails_exempt", "url_direct",
                "company_site", "first_seen_run", "board"):
        assert col in row.keys(), f"{col} missing after migration"


def test_a_migration_backs_up_a_database_that_has_rows(tmp_path, monkeypatch, capsys):
    """The inverse of the empty case. When there IS something to lose, say so."""
    monkeypatch.setenv("CAREERKIT_HOME", str(tmp_path))
    (tmp_path / "data").mkdir(parents=True)
    db = tmp_path / "data" / "jobs.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE jobs (uid TEXT PRIMARY KEY, company TEXT, title TEXT, "
                "url TEXT, location TEXT, source TEXT, lane TEXT, employer_tier TEXT, "
                "posted_at TEXT, department TEXT, comp_min INTEGER, comp_max INTEGER, "
                "comp_text TEXT, score INTEGER, gate TEXT, reasons TEXT, description TEXT, "
                "first_seen TEXT, last_seen TEXT, seen_count INTEGER DEFAULT 1, "
                "status TEXT DEFAULT 'new', notes TEXT DEFAULT '')")
    con.execute("INSERT INTO jobs (uid, company, title) VALUES ('u','Acme','Admin')")
    con.commit()
    con.close()

    for name in [m for m in list(sys.modules) if m.startswith("engine")]:
        importlib.reload(sys.modules[name])
    from engine import store
    store.connect()
    assert "backed up" in capsys.readouterr().out.lower()
    assert list((tmp_path / "data").glob("*pre-migration*.db")), "no backup written"


def test_a_first_report_is_internally_consistent(tmp_path, monkeypatch):
    """The clean-clone run produced a row that contradicted itself. Whatever the
    first report says, it must agree with the database that produced it."""
    store = _fresh(tmp_path, monkeypatch)
    from engine import consistency
    from engine.models import Job
    from engine.report import write_report
    from engine.score import Profile, score

    prof = tmp_path / "profile.yaml"
    prof.write_text(yaml.safe_dump({
        "lanes": [{"key": "sf", "titles": ["/salesforce/"]}],
        "location": {"remote_us": True},
    }))
    profile = Profile.load(prof)

    j = Job(company="Chime", title="Salesforce Administrator", url="https://x/1",
            source="greenhouse", external_id="1", location="Remote, United States",
            description=("Minimum Qualifications\n- 3 years of Salesforce.\n"
                         "The salary range for this role is $150,000 - $208,000 per year. "
                         + "d" * 300))
    score(j, profile)
    con = store.connect()
    run_id = store.start_run(con)
    store.upsert(con, [j], run_id=run_id)
    rows = list(con.execute("SELECT * FROM jobs"))
    path = write_report(con, rows, health=[], run_detail={}, run_id=run_id)

    assert consistency.check_report(con, path) == [], consistency.check_report(con, path)
    text = path.read_text()
    assert "$150,000" in text, "the band the scorer resolved never reached the reader"
    # nothing clipped mid-word, which is how "domain terms never mentioned in
    # postin" reached a new user's screen
    assert "postin " not in text and "…" not in text.split("Source health")[0]


@pytest.mark.parametrize("bad", [
    {"lanes": [{"key": "x", "titles": ["/.+/"]}]},
    {"exclusions": {"titles": [""]}},
    {"exclusions": {"titles": [None]}},
])
def test_a_rule_that_would_hide_everything_stops_the_run(tmp_path, monkeypatch, bad):
    """An empty entry in an exclusion list compiled to a regex matching every
    posting ever written, so the report showed nothing, which is indistinguishable
    from a quiet week. Failing open is the one failure the documentation promises
    cannot happen."""
    _fresh(tmp_path, monkeypatch)
    from engine.score import Profile, ProfileError
    prof = tmp_path / "profile.yaml"
    cfg = {"lanes": [{"key": "sf", "titles": ["/salesforce/"]}],
           "location": {"remote_us": True}}
    cfg.update(bad)
    prof.write_text(yaml.safe_dump(cfg))
    try:
        p = Profile.load(prof)
    except ProfileError:
        return                      # refused loudly, which is correct
    # or it loaded, in which case the dangerous rule must have been dropped
    if p.slot_block or p.slot_block_always:
        assert not (p.slot_block and p.slot_block.search("Warehouse Associate II")
                    and p.slot_block.search("Chief Financial Officer")), \
            "an exclusion that matches everything survived profile validation"
