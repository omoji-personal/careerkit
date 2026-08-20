"""Focused tests for the privacy-safe source coverage ledger."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sqlite3
from argparse import Namespace


def test_employer_rows_are_deduplicated_by_casefolded_board_identity(monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.adapters, "REGISTRY", {
        "smartrecruiters": object(), "workday": object(), "oracle_orc": object(),
    })
    registry = {
        "employers": [
            {"name": "Kipp", "ats": "smartrecruiters", "slug": "KIPP"},
            {"name": "KIPP Foundation", "ats": "smartrecruiters", "slug": "kipp"},
            {"name": "Acme", "ats": "workday", "tenant": "ACME", "dc": "WD1",
             "site": "External"},
            {"name": "Acme Careers", "ats": "workday", "tenant": "acme",
             "dc": "wd1", "site": "external"},
            {"name": "Acme Internal", "ats": "workday", "tenant": "acme",
             "dc": "wd1", "site": "Internal"},
            {"name": "Parked", "ats": "smartrecruiters", "slug": "KIPP",
             "active": False},
        ],
        "feeds": [],
    }

    result = coverage.build_coverage_ledger(registry)

    assert result["employers"] == {
        "active_rows": 5,
        "unique_board_ids": 3,
        "duplicate_rows": 2,
        "duplicate_boards": [
            {
                "board_id": "smartrecruiters:kipp",
                "row_count": 2,
                "duplicate_rows": 1,
                "labels": ["smartrecruiters:Kipp", "smartrecruiters:KIPP Foundation"],
            },
            {
                "board_id": "workday:acme/wd1/external",
                "row_count": 2,
                "duplicate_rows": 1,
                "labels": ["workday:Acme", "workday:Acme Careers"],
            },
        ],
    }
    assert result["ats_families"] == {
        "supported": ["oracle_orc", "smartrecruiters", "workday"],
        "represented": ["smartrecruiters", "workday"],
        "supported_unrepresented": ["oracle_orc"],
        "represented_unsupported": [],
    }


def test_unknown_represented_ats_is_reported_without_crashing(monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.adapters, "REGISTRY", {"greenhouse": object()})
    registry = {
        "employers": [{"name": "Acme", "ats": "future_ats", "slug": "acme"}],
        "feeds": [],
    }

    result = coverage.build_coverage_ledger(registry)

    assert result["ats_families"]["supported_unrepresented"] == ["greenhouse"]
    assert result["ats_families"]["represented_unsupported"] == ["future_ats"]


def test_active_feeds_split_by_policy_and_key_presence_without_leaking_values(
        monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.aggregators, "FEEDS", {
        "official": object(), "keyed-ready": object(), "keyed-missing": object(),
        "blocked": object(),
    })
    monkeypatch.setattr(coverage.aggregators, "FEED_KEYS", {
        "keyed-ready": ("api_key",),
        "keyed-missing": ("email", "api_key"),
    })
    policies = {
        "official": {"kind": "official"},
        "keyed-ready": {"kind": "keyed"},
        "keyed-missing": {"kind": "keyed"},
        "blocked": {"kind": "scraping", "supported": False},
    }
    monkeypatch.setattr(coverage.aggregators, "policy",
                        lambda name: policies.get(name, {"kind": "unknown"}))
    secret = "do-not-copy-this-secret"
    registry = {
        "employers": [],
        "feeds": [
            {"name": "keyed-missing"},
            {"name": "official"},
            {"name": "blocked"},
            {"name": "keyed-ready"},
            {"name": "unknown"},
            {"name": "inactive", "active": False},
        ],
    }
    keys = {
        "keyed-ready": {"api_key": secret},
        "keyed-missing": {"email": ""},
    }
    registry_before, keys_before = deepcopy(registry), deepcopy(keys)

    result = coverage.build_coverage_ledger(registry, keys=keys)

    assert result["feeds"]["active_rows"] == 5
    assert result["feeds"]["unique_count"] == 5
    assert result["feeds"]["duplicate_rows"] == 0
    assert result["feeds"]["duplicate_feeds"] == []
    assert result["feeds"]["operational_count"] == 2
    assert result["feeds"]["operational"] == [
        {"label": "feed:keyed-ready", "name": "keyed-ready", "kind": "keyed"},
        {"label": "feed:official", "name": "official", "kind": "official"},
    ]
    assert result["feeds"]["dormant"] == [{
        "label": "feed:keyed-missing",
        "name": "keyed-missing",
        "kind": "keyed",
        "reason": "missing_required_keys",
        "missing_fields": ["api_key", "email"],
    }]
    assert result["feeds"]["dormant_count"] == 1
    assert result["feeds"]["unsupported"] == [
        {"label": "feed:blocked", "name": "blocked", "kind": "scraping",
         "reason": "unsupported_feed"},
        {"label": "feed:unknown", "name": "unknown", "kind": "unknown",
         "reason": "unsupported_feed"},
    ]
    assert result["feeds"]["unsupported_count"] == 2
    assert secret not in json.dumps(result, sort_keys=True)
    assert registry == registry_before
    assert keys == keys_before


def test_active_feeds_are_deduplicated_case_insensitively(monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.aggregators, "FEEDS", {"official": object()})
    monkeypatch.setattr(coverage.aggregators, "FEED_KEYS", {})
    monkeypatch.setattr(coverage.aggregators, "policy",
                        lambda name: {"kind": "official"} if name == "official" else {})
    registry = {
        "employers": [],
        "feeds": [
            {"name": "Official"},
            {"name": "official"},
            {"name": "OFFICIAL"},
            {"name": "parked", "active": False},
        ],
    }

    result = coverage.build_coverage_ledger(registry)

    assert result["feeds"] == {
        "active_rows": 3,
        "unique_count": 1,
        "duplicate_rows": 2,
        "duplicate_feeds": [{
            "feed_id": "feed:official",
            "row_count": 3,
            "duplicate_rows": 2,
            "labels": ["feed:OFFICIAL", "feed:Official", "feed:official"],
        }],
        "operational_count": 1,
        "operational": [
            {"label": "feed:official", "name": "official", "kind": "official"},
        ],
        "dormant_count": 0,
        "dormant": [],
        "unsupported_count": 0,
        "unsupported": [],
    }


def test_freehire_without_literal_opt_in_is_dormant_not_operational(monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.aggregators, "FEEDS", {"freehire": object()})
    monkeypatch.setattr(coverage.aggregators, "FEED_KEYS", {})
    monkeypatch.setattr(
        coverage.aggregators, "policy",
        lambda name: {"kind": "official", "opt_in": True} if name == "freehire" else {},
    )
    registry = {
        "employers": [],
        "feeds": [
            {"name": "freehire"},
            {"name": "FreeHire", "active": False},
        ],
    }

    result = coverage.build_coverage_ledger(registry)

    assert result["feeds"]["active_rows"] == 0
    assert result["feeds"]["unique_count"] == 0
    assert result["feeds"]["operational_count"] == 0
    assert result["feeds"]["dormant"] == [{
        "label": "feed:freehire",
        "name": "freehire",
        "kind": "official",
        "reason": "explicit_opt_in_disabled",
        "missing_fields": ["active: true (explicit opt-in)"],
    }]

    cli = (Path(__file__).resolve().parents[1] / "careerkit.py").read_text(
        encoding="utf-8")
    assert "dormant or opt-in feeds" in cli
    assert "active but dormant keyed feeds" not in cli


def test_falsey_active_values_never_overstate_employer_or_feed_reach(monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.adapters, "REGISTRY", {"greenhouse": object()})
    monkeypatch.setattr(coverage.aggregators, "FEEDS", {"remotive": object()})
    monkeypatch.setattr(coverage.aggregators, "FEED_KEYS", {})
    monkeypatch.setattr(coverage.aggregators, "policy",
                        lambda _name: {"kind": "official"})
    registry = {
        "employers": [
            {"name": "Null", "ats": "greenhouse", "slug": "null", "active": None},
            {"name": "Zero", "ats": "greenhouse", "slug": "zero", "active": 0},
            {"name": "Live", "ats": "greenhouse", "slug": "live"},
        ],
        "feeds": [
            {"name": "remotive", "active": None},
            {"name": "remotive", "active": 0},
        ],
    }

    result = coverage.build_coverage_ledger(registry)

    assert result["employers"]["active_rows"] == 1
    assert result["employers"]["unique_board_ids"] == 1
    assert result["feeds"]["active_rows"] == 0
    assert result["feeds"]["unique_count"] == 0


def test_active_term_feed_without_search_terms_is_dormant(monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.aggregators, "FEEDS", {"remotive": object()})
    monkeypatch.setattr(coverage.aggregators, "TERM_FEEDS", frozenset({"remotive"}))
    monkeypatch.setattr(coverage.aggregators, "FEED_KEYS", {})
    monkeypatch.setattr(coverage.aggregators, "policy",
                        lambda _name: {"kind": "official"})
    registry = {"employers": [], "feeds": [{"name": "remotive"}]}

    result = coverage.build_coverage_ledger(registry, search_term_count=0)

    assert result["feeds"]["operational_count"] == 0
    assert result["feeds"]["dormant"] == [{
        "label": "feed:remotive",
        "name": "remotive",
        "kind": "official",
        "reason": "missing_search_terms",
        "missing_fields": ["profile.search_terms"],
    }]


def test_page_ceiling_sources_use_active_labels_and_accept_sqlite_rows(
        monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.adapters, "PAGE_CEILING", {
        "icims": 1500, "workable": 100,
    })
    registry = {
        "employers": [
            {"name": "Piedmont", "ats": "icims", "slug": "piedmont"},
            {"name": "Northside", "ats": "icims", "slug": "northside"},
            {"name": "Over", "ats": "workable", "slug": "over"},
            {"name": "Inactive", "ats": "icims", "slug": "inactive",
             "active": False},
        ],
        "feeds": [],
    }
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE health(source TEXT, last_count INTEGER)")
    con.executemany("INSERT INTO health VALUES (?, ?)", [
        ("ICIMS:Piedmont", 1500),
        ("icims:northside", 1499),
        ("workable:over", 120),
        ("icims:inactive", 9999),
        ("icims:orphan", 9999),
    ])
    rows = list(con.execute("SELECT * FROM health ORDER BY source DESC"))

    result = coverage.build_coverage_ledger(registry, rows)

    assert result["page_ceiling_sources"] == [
        {
            "label": "icims:Piedmont",
            "board_id": "icims:piedmont",
            "ats": "icims",
            "last_count": 1500,
            "ceiling": 1500,
        },
        {
            "label": "workable:Over",
            "board_id": "workable:over",
            "ats": "workable",
            "last_count": 120,
            "ceiling": 100,
        },
    ]
    assert result["page_ceiling_count"] == 2
    json.dumps(result, sort_keys=True)


def test_board_id_health_fallback_and_missing_health_are_safe(monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.adapters, "PAGE_CEILING", {"workday": 120})
    registry = {
        "employers": [
            {"name": "Display Name", "ats": "workday", "tenant": "acme",
             "dc": "wd1", "site": "External"},
        ],
        "feeds": [],
    }

    without_health = coverage.build_coverage_ledger(registry)
    with_board_health = coverage.build_coverage_ledger(
        registry, [{"source": "WORKDAY:ACME/WD1/EXTERNAL", "last_count": "120"}],
    )

    assert without_health["page_ceiling_sources"] == []
    assert without_health["page_ceiling_count"] == 0
    assert without_health["health_gap_sources"] == [{
        "label": "workday:Display Name",
        "source_id": "workday:acme/wd1/external",
        "state": "unpolled",
        "last_count": 0,
    }]
    assert with_board_health["page_ceiling_sources"][0]["label"] == (
        "workday:Display Name")


def test_current_partial_capped_and_failed_health_are_privacy_safe(monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.adapters, "REGISTRY", {"workday": object()})
    monkeypatch.setattr(coverage.adapters, "PAGE_CEILING", {})
    monkeypatch.setattr(coverage.aggregators, "FEEDS", {"official": object()})
    monkeypatch.setattr(coverage.aggregators, "policy",
                        lambda _name: {"kind": "official"})
    registry = {
        "employers": [{
            "name": "Acme", "ats": "workday", "tenant": "acme",
            "dc": "wd1", "site": "External",
        }],
        "feeds": [{"name": "official"}],
    }
    secret = "private-provider-detail"
    rows = [
        {"source": "workday:Acme", "last_count": 999,
         "last_error": "failed: stale legacy row"},
        {"source": "workday:acme/wd1/external", "last_count": 120,
         "last_error": f"capped: {secret}"},
        {"source": "feed:official", "last_count": 7,
         "last_error": f"partial: {secret}"},
        {"source": "feed:inactive", "last_count": 0,
         "last_error": f"HTTP 500 {secret}"},
    ]

    result = coverage.build_coverage_ledger(registry, rows)

    assert result["health_gap_sources"] == [
        {"label": "workday:Acme", "source_id": "workday:acme/wd1/external",
         "state": "capped", "last_count": 120},
        {"label": "feed:official", "source_id": "feed:official",
         "state": "partial", "last_count": 7},
    ]
    assert result["health_gap_count"] == 2
    assert secret not in json.dumps(result, sort_keys=True)


def test_old_success_is_a_stale_gap_even_after_another_source_runs(monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.adapters, "REGISTRY", {"greenhouse": object()})
    monkeypatch.setattr(coverage.adapters, "PAGE_CEILING", {})
    monkeypatch.setattr(coverage.aggregators, "FEEDS", {"remotive": object()})
    monkeypatch.setattr(coverage.aggregators, "policy",
                        lambda _name: {"kind": "official"})
    registry = {
        "employers": [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}],
        "feeds": [{"name": "remotive"}],
    }
    health = [
        {"source": "greenhouse:acme", "last_count": 10,
         "last_ok": "2000-01-01T00:00:00", "last_error": None},
        {"source": "feed:remotive", "last_count": 20,
         "last_ok": "2026-08-20T10:00:00", "last_error": None},
    ]

    result = coverage.build_coverage_ledger(
        registry, health, as_of=date(2026, 8, 20))

    assert result["health_gap_sources"] == [{
        "label": "greenhouse:Acme", "source_id": "greenhouse:acme",
        "state": "stale", "last_count": 10,
    }]


def test_exact_stable_health_key_wins_over_casefold_colliding_legacy_row(
        monkeypatch):
    from engine import coverage

    monkeypatch.setattr(coverage.adapters, "REGISTRY", {"greenhouse": object()})
    monkeypatch.setattr(coverage.adapters, "PAGE_CEILING", {})
    registry = {
        "employers": [{
            "name": "Acme", "ats": "greenhouse", "slug": "acme",
        }],
        "feeds": [],
    }
    secret = "private-provider-detail"
    rows = [
        {"source": "greenhouse:Acme", "last_count": 100, "last_error": None},
        {"source": "greenhouse:acme", "last_count": 10,
         "last_error": f"partial: {secret}"},
    ]

    result = coverage.build_coverage_ledger(registry, rows)

    assert result["health_gap_sources"] == [{
        "label": "greenhouse:Acme",
        "source_id": "greenhouse:acme",
        "state": "partial",
        "last_count": 10,
    }]
    assert result["health_gap_count"] == 1
    assert secret not in json.dumps(result, sort_keys=True)


def test_coverage_cli_reports_real_unique_and_operational_counts(monkeypatch, capsys):
    import careerkit

    registry = {
        "employers": [
            {"name": "Acme", "ats": "greenhouse", "slug": "ACME"},
            {"name": "Acme Duplicate", "ats": "greenhouse", "slug": "acme"},
        ],
        "feeds": [
            {"name": "remotive"},
            {"name": "adzuna"},
            {"name": "jobspy"},
        ],
    }
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE source_health (source,last_count)")
    monkeypatch.setattr(careerkit, "load_registry", lambda **_kw: registry)
    monkeypatch.setattr(
        careerkit, "load_profile", lambda: Namespace(search_terms=["crm"]))
    monkeypatch.setattr(careerkit, "load_yaml", lambda *_a, **_kw: {})
    monkeypatch.setattr(careerkit.store, "connect", lambda: con)

    careerkit.cmd_coverage(Namespace(json=False))
    text = capsys.readouterr().out
    assert "employer rows       : 2" in text
    assert "unique boards       : 1" in text
    assert "operational feeds   : 1/3 unique active (3 rows)" in text
    assert "inferred source set : 2 unique configured endpoints" in text
    assert "greenhouse:acme" in text
    assert "adzuna (missing: app_id, app_key)" in text
    assert "jobspy" in text

    careerkit.cmd_coverage(Namespace(json=True))
    ledger = json.loads(capsys.readouterr().out)
    assert ledger["employers"]["unique_board_ids"] == 1
    assert ledger["feeds"]["operational_count"] == 1
    assert "secret" not in json.dumps(ledger).lower()


def test_coverage_parser_is_read_only_and_has_json_mode():
    import careerkit

    args = careerkit.build_parser().parse_args(["coverage", "--json"])
    assert args.fn is careerkit.cmd_coverage
    assert args.json is True
    assert careerkit.command_writes(args) is False


def test_coverage_cli_prints_single_run_partial_feed_as_a_blocking_gap(
        monkeypatch, capsys):
    import careerkit

    registry = {"employers": [], "feeds": [{"name": "remotive"}]}
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE source_health (source,last_count,last_error)"
    )
    con.execute(
        "INSERT INTO source_health VALUES (?,?,?)",
        ("feed:remotive", 42, "partial: provider page two timed out"),
    )
    monkeypatch.setattr(careerkit, "load_registry", lambda **_kw: registry)
    monkeypatch.setattr(
        careerkit, "load_profile", lambda: Namespace(search_terms=["crm"]))
    monkeypatch.setattr(careerkit, "load_yaml", lambda *_a, **_kw: {})
    monkeypatch.setattr(careerkit.store, "connect", lambda: con)

    careerkit.cmd_coverage(Namespace(json=False))

    text = capsys.readouterr().out
    assert "Current source health gaps (results are not complete)" in text
    assert "feed:remotive: partial (42 usable postings retained)" in text
    assert "1 blocking coverage gap" in text
    assert "timed out" not in text, "raw provider errors must stay out of the ledger"
