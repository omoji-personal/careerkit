"""End-to-end invariants for the opt-in Freehire discovery bridge."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from engine import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    con = store.connect()
    yield con
    con.close()


def _profile():
    from engine.score import Profile

    profile = Profile()
    profile.lanes = [
        (80, re.compile(r"solution architect", re.I), "architecture"),
        (50, re.compile(r"software developer", re.I), "engineering"),
    ]
    profile.domain_terms = None
    return profile


def _native_and_freehire():
    from engine.models import Job

    direct = "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123"
    native = Job(
        company="Acme, Inc.", title="Solution Architect", url=direct,
        url_direct=direct, source="greenhouse", external_id="123",
        board="greenhouse:acme", location="Remote, US",
        description="Employer-published role description. " + "d" * 400,
    )
    freehire = Job(
        company="Acme", title="Solution Architect",
        url="HTTPS://boards.greenhouse.io:443/acme/jobs/123?gh_jid=123#apply",
        url_direct="HTTPS://boards.greenhouse.io:443/acme/jobs/123?gh_jid=123#apply",
        source="freehire:greenhouse", external_id="public-freehire-slug",
        location="Remote, US",
        description="Freehire-hydrated role description. " + "d" * 500,
    )
    native.gate, native.score, native.reasons = "QUALIFIED", 82, ["native verdict"]
    freehire.gate, freehire.score, freehire.reasons = "VERIFY", 82, ["bridge verdict"]
    return native, freehire


def test_every_surfaced_freehire_row_requires_source_verification():
    from engine.models import Job
    from engine.score import score

    profile = _profile()
    direct = "https://boards.greenhouse.io/acme/jobs/123"
    job = Job(
        company="Acme", title="Solution Architect", url=direct,
        url_direct=direct, source="freehire:greenhouse",
        location="Remote, US", description="complete description " + "d" * 400,
    )
    result = score(job, profile)

    assert result.gate == "VERIFY"
    assert any("source verification required" in reason.casefold()
               for reason in result.reasons)


@pytest.mark.parametrize(
    ("title", "configure", "expected"),
    [
        ("Unrelated Role", lambda profile: None, "EXCLUDED"),
        ("Software Developer",
         lambda profile: setattr(
             profile, "slot_block_always", re.compile(r"software developer", re.I)),
         "SLOT-BLOCKED"),
    ],
)
def test_freehire_verification_rule_never_promotes_a_screened_posting(
        title, configure, expected):
    from engine.models import Job
    from engine.score import score

    profile = _profile()
    configure(profile)
    result = score(Job(
        company="Acme", title=title,
        url="https://boards.greenhouse.io/acme/jobs/123",
        url_direct="https://boards.greenhouse.io/acme/jobs/123",
        source="freehire:greenhouse", location="Remote, US",
        description="complete description " + "d" * 400,
    ), profile)

    assert result.gate == expected
    assert not any("source verification required" in reason.casefold()
                   for reason in result.reasons)


def test_disabling_configured_freehire_retires_namespaced_rows_only_after_guard(db):
    from engine import store
    from engine.models import Job

    freehire = Job(
        company="Acme", title="Solution Architect",
        url="https://boards.greenhouse.io/acme/jobs/123",
        url_direct="https://boards.greenhouse.io/acme/jobs/123",
        source="freehire:greenhouse", external_id="freehire-123",
        gate="VERIFY", score=70,
    )
    unconfigured = Job(
        company="Beta", title="Solution Architect",
        url="https://unknown.example/jobs/456",
        source="unknownbridge:greenhouse", external_id="unknown-456",
        gate="VERIFY", score=70,
    )
    store.upsert(db, [freehire, unconfigured])
    db.execute("UPDATE jobs SET last_seen='2020-01-01'")
    db.commit()

    # Active-but-unpolled feeds are not closure evidence (for example during an
    # employers-only run).
    store.reconcile(
        db, {}, set(), set(), known_feeds={"freehire"},
        active_feeds={"freehire"},
    )
    assert db.execute(
        "SELECT misses FROM jobs WHERE uid=?", (freehire.uid,)).fetchone()[0] == 0

    # Explicit disable begins the normal miss clock, but never retires on the
    # first day and never touches a source absent from configuration.
    store.reconcile(
        db, {}, set(), set(), known_feeds={"freehire"}, active_feeds=set(),
    )
    first = db.execute(
        "SELECT misses,delisted_on FROM jobs WHERE uid=?", (freehire.uid,)).fetchone()
    assert (first["misses"], first["delisted_on"]) == (1, None)
    assert db.execute(
        "SELECT misses FROM jobs WHERE uid=?", (unconfigured.uid,)).fetchone()[0] == 0

    # Simulate the following calendar day's eligible miss without changing the
    # production clock implementation.
    db.execute("UPDATE jobs SET miss_on='2020-01-01' WHERE uid=?", (freehire.uid,))
    db.commit()
    delisted, _ = store.reconcile(
        db, {}, set(), set(), known_feeds={"freehire"}, active_feeds=set(),
    )
    assert delisted == 1
    assert db.execute(
        "SELECT delisted_on FROM jobs WHERE uid=?", (freehire.uid,)).fetchone()[0]
    assert db.execute(
        "SELECT delisted_on FROM jobs WHERE uid=?", (unconfigured.uid,)).fetchone()[0] is None


def test_omitted_freehire_active_flag_is_inactive_in_pull_and_reconciliation(
        db, tmp_path, monkeypatch):
    from engine import pull, store
    from engine.models import Job
    from engine.score import Profile

    job = Job(
        company="Acme", title="Solution Architect",
        url="https://boards.greenhouse.io/acme/jobs/123",
        url_direct="https://boards.greenhouse.io/acme/jobs/123",
        source="freehire:greenhouse", external_id="freehire-123",
        gate="VERIFY", score=70,
    )
    store.upsert(db, [job])
    db.execute("UPDATE jobs SET last_seen='2020-01-01'")
    db.commit()

    attempted = []

    def forbidden_feed(name, cfg):
        attempted.append((name, cfg))
        raise AssertionError("opt-in Freehire was polled without active: true")

    monkeypatch.setattr(pull, "run_feed", forbidden_feed)
    monkeypatch.setattr(
        pull, "write_report", lambda *args, **kwargs: tmp_path / "latest.md")
    registry = {"employers": [], "feeds": [{"name": "freehire"}]}

    pull.run_pull(db, registry, {}, Profile(), echo=lambda *args: None)
    first = db.execute(
        "SELECT misses,delisted_on FROM jobs WHERE uid=?", (job.uid,)).fetchone()
    assert attempted == []
    assert (first["misses"], first["delisted_on"]) == (1, None)

    db.execute("UPDATE jobs SET miss_on='2020-01-01' WHERE uid=?", (job.uid,))
    db.commit()
    result = pull.run_pull(db, registry, {}, Profile(), echo=lambda *args: None)
    assert attempted == []
    assert result["delisted"] == 1
    assert db.execute(
        "SELECT delisted_on FROM jobs WHERE uid=?", (job.uid,)).fetchone()[0]


@pytest.mark.parametrize("first_source", ["native", "freehire"])
@pytest.mark.parametrize("batch_order", ["native-first", "freehire-first"])
def test_native_and_freehire_exact_direct_url_are_one_opening_in_both_orders(
        db, first_source, batch_order):
    from engine import pull, store

    native, freehire = _native_and_freehire()
    first = native if first_source == "native" else freehire
    store.upsert(db, [first])
    first_uid = first.uid
    store.set_status(db, first_uid, "applied", "confirmation retained")

    batch = ([native, freehire] if batch_order == "native-first"
             else [freehire, native])
    store.coalesce_exact_direct_url_identities(db, batch)
    keep = pull.pick_surfaced(batch)
    assert len(keep) == 1
    store.upsert(db, keep)
    pull.record_surfaced_sightings(db, batch, {keep[0].uid})

    rows = list(db.execute("SELECT * FROM jobs"))
    assert len(rows) == 1
    row = rows[0]
    assert row["uid"] == native.uid
    assert row["source"] == "greenhouse"
    assert row["status"] == "applied"
    assert row["notes"] == "confirmation retained"
    assert {s["source"] for s in db.execute(
        "SELECT source FROM sightings WHERE uid=?", (row["uid"],))} == {
            "greenhouse", "freehire:greenhouse",
        }
    assert {event["uid"] for event in db.execute("SELECT uid FROM events")} == {
        row["uid"],
    }


def test_nearby_but_nonidentical_direct_urls_remain_distinct(db):
    from engine import pull, store

    native, freehire = _native_and_freehire()
    freehire.url = freehire.url_direct = (
        "https://boards.greenhouse.io/acme/jobs/123?gh_jid=DIFFERENT"
    )
    batch = [native, freehire]
    store.coalesce_exact_direct_url_identities(db, batch)
    keep = pull.pick_surfaced(batch)
    store.upsert(db, keep)
    pull.record_surfaced_sightings(db, batch, {job.uid for job in keep})

    assert len(list(db.execute("SELECT uid FROM jobs"))) == 2


def test_freehire_oracle_alias_coalesces_with_native_oracle_orc(db):
    from engine import pull, store
    from engine.models import Job

    direct = (
        "https://example.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/"
        "en/sites/CX/job/REQ-1"
    )
    native = Job(
        company="Acme", title="Solution Architect", url=direct,
        url_direct=direct, source="oracle_orc", external_id="REQ-1",
        board="oracle_orc:example.fa.ocs.oraclecloud.com/cx",
        gate="QUALIFIED", score=82,
    )
    bridge = Job(
        company="Acme", title="Solution Architect", url=direct,
        url_direct=direct, source="freehire:oracle", external_id="freehire-1",
        gate="VERIFY", score=82,
    )
    batch = [bridge, native]

    store.coalesce_exact_direct_url_identities(db, batch)
    keep = pull.pick_surfaced(batch)
    store.upsert(db, keep)
    pull.record_surfaced_sightings(db, batch, {job.uid for job in keep})

    rows = list(db.execute("SELECT uid,source FROM jobs"))
    assert len(rows) == 1
    assert rows[0]["source"] == "oracle_orc"
    assert {row["source"] for row in db.execute(
        "SELECT source FROM sightings WHERE uid=?", (rows[0]["uid"],)
    )} == {"oracle_orc", "freehire:oracle"}


@pytest.mark.parametrize(
    ("native_gate", "freehire_gate"),
    [
        ("EXCLUDED", "VERIFY"),
        ("SLOT-BLOCKED", "VERIFY"),
        ("EXCLUDED", "SLOT-BLOCKED"),
    ],
)
def test_native_ats_verdict_overrides_freehire_for_exact_opening(
        db, native_gate, freehire_gate):
    from engine import pull, store

    native, freehire = _native_and_freehire()
    native.gate, native.score, native.reasons = native_gate, 0, ["hard rail"]
    freehire.gate, freehire.score = freehire_gate, 99
    batch = [freehire, native]

    store.coalesce_exact_direct_url_identities(db, batch)
    keep = pull.pick_surfaced(batch)
    demoted = pull.pick_demoted(batch, {job.uid for job in keep})

    assert keep == []
    assert list(demoted) == [native.uid]
    assert demoted[native.uid].source == "greenhouse"
    assert demoted[native.uid].gate == native_gate
    assert demoted[native.uid].description.startswith("Employer-published")


def test_inactive_feed_name_cannot_collide_with_native_ats_namespace(db):
    from engine import store

    native, _freehire = _native_and_freehire()
    store.upsert(db, [native])
    db.execute("UPDATE jobs SET last_seen='2020-01-01'")
    db.commit()

    store.reconcile(
        db, {}, set(), set(), known_feeds={"greenhouse"}, active_feeds=set())

    row = db.execute(
        "SELECT misses,delisted_on FROM jobs WHERE uid=?", (native.uid,)
    ).fetchone()
    assert (row["misses"], row["delisted_on"]) == (0, None)
