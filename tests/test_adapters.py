"""Adapter contract tests against REAL cached payloads.

Every other test exercises the scorer with hand-built Job objects, so an
adapter could stop mapping a field entirely (a renamed key, a moved comp block)
and the whole suite stayed green while the live search quietly lost data. These
run the real adapter over a saved payload from that platform with the network
stubbed, so a board changing its JSON shape fails here instead of in a run.

Fixtures are trimmed to two postings and hold only public board data.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from engine import adapters

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

CASES = [
    ("greenhouse", {"ats": "greenhouse", "slug": "acme", "name": "Acme"}),
    ("lever", {"ats": "lever", "slug": "acme", "name": "Acme"}),
    ("ashby", {"ats": "ashby", "slug": "acme", "name": "Acme"}),
    ("smartrecruiters", {"ats": "smartrecruiters", "slug": "acme", "name": "Acme"}),
    ("bamboohr", {"ats": "bamboohr", "slug": "acme", "name": "Acme"}),
    ("rippling", {"ats": "rippling", "slug": "acme", "name": "Acme"}),
    ("recruitee", {"ats": "recruitee", "slug": "acme", "name": "Acme"}),
]


@pytest.fixture
def stub_fetch(monkeypatch):
    """Serve the fixture once, then an empty page so paginating adapters stop."""
    def make(payload):
        seen = {"n": 0}

        def fake(url, *a, **k):
            seen["n"] += 1
            if seen["n"] > 1:
                return {"content": [], "jobs": []}
            return payload
        monkeypatch.setattr(adapters, "fetch_json", fake)
    return make


@pytest.mark.parametrize("name,cfg", CASES)
def test_adapter_maps_its_real_payload(name, cfg, stub_fetch):
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        pytest.skip(f"no fixture for {name}")
    stub_fetch(json.loads(path.read_text()))

    jobs = getattr(adapters, name)(cfg)

    assert jobs, f"{name} mapped a real payload to zero jobs"
    for j in jobs:
        assert j.title.strip(), f"{name}: empty title"
        assert j.url.startswith("http"), f"{name}: bad url {j.url!r}"
        assert j.source == name
        assert j.company == "Acme"
        assert j.board == f"{name}:acme"
        # external_id is what splits two distinct requisitions at the same
        # employer into two rows. Losing it silently re-collapses them.
        assert j.external_id, f"{name}: no external_id, uid collapses siblings"
        assert j.uid and j.group_key
        assert "<" not in j.description or ">" not in j.description, \
            f"{name}: HTML survived into the description"


@pytest.mark.parametrize("name,cfg", CASES)
def test_adapter_survives_an_empty_or_wrong_shaped_payload(name, cfg, stub_fetch):
    """A board returning 200 with an unexpected body must yield [], not raise.
    A raising adapter aborts the whole pull and every later board goes unpolled."""
    for payload in ({}, [], {"jobs": None}, {"content": None}, "not json at all"):
        stub_fetch(payload)
        assert getattr(adapters, name)(cfg) == []
