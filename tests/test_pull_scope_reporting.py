"""Execution-level regressions for honest filtered-pull reporting."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _run_without_network(tmp_path, monkeypatch, **scope):
    from engine import pull, report, store
    from engine.score import Profile

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(
        pull,
        "fetch_all",
        lambda *_args, **_kwargs: {
            "jobs": [],
            "sources_ok": 0,
            "errors": {},
            "healthy_boards": set(),
            "healthy_feeds": set(),
        },
    )
    con = store.connect()
    result = pull.run_pull(
        con,
        {
            "employers": [
                {
                    "name": "Acme",
                    "ats": "greenhouse",
                    "slug": "acme",
                    "tier": "A",
                    "active": True,
                }
            ],
            "feeds": [{"name": "remotive", "active": True}],
        },
        {},
        Profile(),
        echo=lambda *_args: None,
        **scope,
    )
    detail = json.loads(
        con.execute(
            "SELECT detail FROM runs WHERE run_id=?", (result["run_id"],)
        ).fetchone()["detail"]
    )
    return con, result, detail


def test_feed_only_run_prominently_discloses_scope_and_retained_queue(
        tmp_path, monkeypatch):
    from engine import pull, report, store
    from engine.models import Job

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(
        pull,
        "fetch_all",
        lambda *_args, **_kwargs: {
            "jobs": [],
            "sources_ok": 0,
            "errors": {},
            "healthy_boards": set(),
            "healthy_feeds": set(),
        },
    )
    con = store.connect()
    retained = Job(
        company="Acme",
        title="Retained Architect",
        url="https://boards.greenhouse.io/acme/jobs/123",
        source="greenhouse",
        board="greenhouse:acme",
        external_id="123",
        gate="QUALIFIED",
        score=80,
        reasons=["qualified"],
    )
    store.upsert(con, [retained])
    con.execute(
        "UPDATE jobs SET first_seen='2020-01-01', last_seen='2020-01-01' "
        "WHERE uid=?",
        (retained.uid,),
    )
    con.commit()

    from engine.score import Profile

    result = pull.run_pull(
        con,
        {
            "employers": [
                {
                    "name": "Acme",
                    "ats": "greenhouse",
                    "slug": "acme",
                    "active": True,
                }
            ],
            "feeds": [{"name": "remotive", "active": True}],
        },
        {},
        Profile(),
        feeds_only=True,
        cache_mode="  cache bypassed; fresh fetch requested  ",
        echo=lambda *_args: None,
    )
    detail = json.loads(
        con.execute(
            "SELECT detail FROM runs WHERE run_id=?", (result["run_id"],)
        ).fetchone()["detail"]
    )
    assert detail["scope"] == {
        "mode": "feeds-only",
        "label": "aggregator feeds only",
        "filtered": True,
        "employer_boards": "skipped",
        "feeds": "included",
        "tiers": [],
        "cache_mode": "cache bypassed; fresh fetch requested",
        "report_rows": "active-retained-decision-queue",
    }

    body = result["path"].read_text()
    prominent = "\n".join(body.splitlines()[:10])
    assert "Employer boards were intentionally skipped." in prominent
    assert "This is not a comprehensive sourcing run." in prominent
    assert "may include retained rows" in prominent
    assert "Cache mode: cache bypassed; fresh fetch requested." in prominent
    assert "Retained Architect" in body

    # A later report rebuild reads the durable run metadata and must not erase
    # the warning just because no pull flags are present at render time.
    rebuilt = pull.rebuild_report(con, echo=lambda *_args: None).read_text()
    assert "Employer boards were intentionally skipped." in rebuilt
    assert "may include retained rows" in rebuilt
    con.close()


@pytest.mark.parametrize(
    ("scope", "expected_mode", "expected_fragments"),
    [
        (
            {"employers_only": True},
            "employers-only",
            ("Aggregator feeds were intentionally skipped.",),
        ),
        (
            {"tier": ["C", "A", "C"]},
            "employer-tiers-and-feeds",
            ("outside tiers A, C were intentionally skipped.",),
        ),
        (
            {"employers_only": True, "tier": ["B"]},
            "employer-tiers-only",
            (
                "outside tiers B were intentionally skipped.",
                "Aggregator feeds were intentionally skipped.",
            ),
        ),
    ],
)
def test_other_filtered_run_scopes_are_deterministic_and_prominent(
        tmp_path, monkeypatch, scope, expected_mode, expected_fragments):
    con, result, detail = _run_without_network(
        tmp_path, monkeypatch, cache_mode="normal 6-hour cache", **scope
    )
    assert detail["scope"]["mode"] == expected_mode
    assert detail["scope"]["filtered"] is True
    assert detail["scope"]["tiers"] == sorted(set(scope.get("tier", [])))
    body = "\n".join(result["path"].read_text().splitlines()[:10])
    assert "This is not a comprehensive sourcing run." in body
    assert "may include retained rows" in body
    assert "Cache mode: normal 6-hour cache." in body
    for fragment in expected_fragments:
        assert fragment in body
    con.close()


def test_default_run_keeps_compatibility_without_a_false_scope_warning(
        tmp_path, monkeypatch):
    con, result, detail = _run_without_network(tmp_path, monkeypatch)
    assert detail["scope"] == {
        "mode": "all-configured-sources",
        "label": "all configured active sources",
        "filtered": False,
        "employer_boards": "included",
        "feeds": "included",
        "tiers": [],
        "cache_mode": None,
        "report_rows": "active-retained-decision-queue",
    }
    body = result["path"].read_text()
    assert "Limited run scope" not in body
    assert "not a comprehensive sourcing run" not in body
    assert "may include retained rows" not in body
    assert "Fetch mode:" not in body
    con.close()
