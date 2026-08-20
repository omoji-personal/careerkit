"""Coverage and regression tests for search-driven source discovery."""
from __future__ import annotations

from argparse import Namespace


def test_automatic_search_domains_have_pollable_adapter_parity():
    from engine import adapters, search

    searched = {family for family, _ in search.ATS_DOMAIN_FAMILIES}
    assert searched == set(adapters.REGISTRY)
    unsupported = {"jazzhr", "breezy", "taleo", "avature", "dayforce", "adp"}
    assert searched.isdisjoint(unsupported)


def test_default_matrix_interleaves_every_supported_family():
    from engine import search

    before = list(search.CORE_TERMS)
    try:
        search.set_core_terms(["first lane", "second lane"])
        queries = search.build_query_matrix()
        first_round = queries[:len(search.ATS_DOMAINS)]
        assert first_round == [f"site:{domain} \"first lane\""
                               for domain in search.ATS_DOMAINS]

        attempted = queries[:60]
        coverage = search.query_coverage(queries, attempted)
        assert coverage["queries_attempted"] == min(60, len(queries))
        assert coverage["families_attempted"] == coverage["families_planned"]
    finally:
        search.CORE_TERMS = before


def test_composite_resolvers_return_exact_pollable_addresses(monkeypatch):
    from engine import search

    oracle = search.resolve(
        "https://fa-etqd-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/"
        "CandidateExperience/en/sites/CX_1/requisitions/preview/2026001760"
    )
    assert oracle.ats == "oracle_orc"
    assert oracle.address == {
        "host": "fa-etqd-saasfaprod1.fa.ocs.oraclecloud.com",
        "site": "CX_1",
    }

    eightfold = search.resolve(
        "https://acme.eightfold.ai/careers/job/12345?domain=acme.com"
    )
    assert eightfold.ats == "eightfold"
    assert eightfold.address == {
        "domain": "acme.com", "host": "https://acme.eightfold.ai",
    }

    guarded = []

    def guarded_fetch(_url, **kwargs):
        guarded.append(kwargs)
        return (200,
                '<div data-config="{&quot;domain&quot;: &quot;example.org&quot;}"></div>')

    monkeypatch.setattr(search, "fetch", guarded_fetch)
    subdomain = search.resolve("https://example.eightfold.ai/careers/job/999")
    assert subdomain.address == {
        "domain": "example.org", "host": "https://example.eightfold.ai",
    }
    assert guarded == [{"safe_external": True}]

    phenom = search.resolve("https://acme.phenompeople.com/us/en/job/456")
    assert phenom.ats == "phenom"
    assert phenom.address == {"host": "https://acme.phenompeople.com"}

    paylocity = search.resolve(
        "https://recruiting.paylocity.com/Recruiting/Jobs/All/"
        "211e692d-c45a-4e3a-ae01-3e497af97929"
    )
    assert paylocity.ats == "paylocity"
    assert paylocity.address == {
        "guid": "211e692d-c45a-4e3a-ae01-3e497af97929",
    }


def test_paylocity_details_is_recognised_but_not_mislabelled_as_board_guid():
    from engine.search import resolve

    hit = resolve("https://recruiting.paylocity.com/Recruiting/Jobs/Details/4141169")
    assert hit.ats == "paylocity"
    assert hit.address == {}
    assert hit.slug == ""


def test_legacy_app_jobvite_resolves_redirect_page_to_board(monkeypatch):
    from engine import search

    guarded = []

    def guarded_fetch(_url, **kwargs):
        guarded.append(kwargs)
        return 200, '<a href="/earthjustice/jobs">All jobs</a>'

    monkeypatch.setattr(search, "fetch", guarded_fetch)
    hit = search.resolve(
        "https://app.jobvite.com/CompanyJobs/Job.aspx?j=otYCAfwn&loc=C5FAYfw2"
    )
    assert hit.ats == "jobvite"
    assert hit.address == {"slug": "earthjustice"}
    assert guarded == [{"safe_external": True}]


def test_workable_short_job_url_uses_canonical_board_not_job_id(monkeypatch):
    from engine import search

    guarded = []

    def guarded_fetch(_url, **kwargs):
        guarded.append(kwargs)
        return (200,
                '<link rel="canonical" '
                'href="https://apply.workable.com/zaelab/j/1ECDD4128C">')

    monkeypatch.setattr(search, "fetch", guarded_fetch)
    hit = search.resolve("https://apply.workable.com/j/1ECDD4128C")
    assert hit.ats == "workable"
    assert hit.address == {"slug": "zaelab"}
    assert hit.slug != "1ECDD4128C"
    assert guarded == [{"safe_external": True}]


def test_guarded_legacy_resolution_refusal_skips_one_url_without_aborting(monkeypatch):
    from engine import http, search

    def refused(_url, **_kwargs):
        raise http.UnsafeExternalURL("private destination")

    monkeypatch.setattr(search, "fetch", refused)

    hit = search.resolve("https://apply.workable.com/j/1ECDD4128C")

    assert hit.ats == "workable"
    assert hit.address == {}


def test_resolver_rejects_ats_urls_embedded_in_unrelated_query_strings():
    from engine import search

    for url in (
        "https://unrelated.example/?next=https://jobs.lever.co/acme/123",
        "https://unrelated.example/?next=https://tenant.wd5.myworkdayjobs.com/Site/123",
        "https://unrelated.example/?url=https://acme.breezy.hr/p/123-role",
    ):
        hit = search.resolve(url)
        assert hit.ats == ""
        assert hit.address == {}

    embedded = search.resolve(
        "https://boards.greenhouse.io/embed/job_app?for=real_board"
    )
    assert embedded.ats == "greenhouse"
    assert embedded.address == {"slug": "real_board"}


def test_resolver_rejects_lookalike_ats_hostnames():
    from engine import search

    for url in (
        "https://notjobs.lever.co/acme/123",
        "https://evilboards.greenhouse.io/acme/jobs/123",
        "https://notapply.workable.com/acme/j/123",
        "https://careers-acme.noticims.com/jobs/123",
        "https://recruitee.com/o/fake-root-host",
    ):
        hit = search.resolve(url)
        assert hit.ats == ""
        assert hit.address == {}


def test_bing_site_filter_matches_hostname_not_query_text(monkeypatch):
    from engine import search

    rss = """<rss><channel>
      <item><title>Fake</title><link>https://unrelated.example/?next=https://jobs.lever.co/acme</link></item>
      <item><title>Real</title><link>https://jobs.lever.co/acme/123</link></item>
    </channel></rss>"""
    monkeypatch.setattr(search, "fetch", lambda _url: (200, rss))

    hits = search.bing_rss('site:jobs.lever.co "role"')

    assert len(hits) == 1
    assert hits[0].ats == "lever"
    assert hits[0].address == {"slug": "acme"}


def test_bing_site_filter_honors_path_bearing_domains(monkeypatch):
    from engine import search

    rss = """<rss><channel>
      <item><title>Wrong path</title><link>https://acme.bamboohr.com/about/careers</link></item>
      <item><title>Real</title><link>https://acme.bamboohr.com/careers/123</link></item>
    </channel></rss>"""
    monkeypatch.setattr(search, "fetch", lambda _url: (200, rss))

    query = 'site:bamboohr.com/careers "role"'
    hits = search.bing_rss(query)
    coverage = search.query_coverage([query], [query])

    assert len(hits) == 1
    assert hits[0].ats == "bamboohr"
    assert hits[0].address == {"slug": "acme"}
    assert coverage["families_attempted"] == 1


def test_pinpoint_and_neogov_urls_resolve_to_exact_public_feed_addresses():
    from engine import search

    pinpoint = search.resolve(
        "https://sensiba.pinpointhq.com/en/postings/"
        "b1dd8157-3f5d-41fe-a846-215f1f9d9b41"
    )
    assert pinpoint.ats == "pinpoint"
    assert pinpoint.address == {"slug": "sensiba"}

    neogov = search.resolve(
        "https://www.governmentjobs.com/careers/fulton/jobs/3828944/example"
    )
    assert neogov.ats == "neogov"
    assert neogov.address == {"slug": "fulton"}

    feed = search.resolve(
        "https://www.governmentjobs.com/SearchEngine/JobsFeed?agency=gwinnett"
    )
    assert feed.ats == "neogov"
    assert feed.address == {"slug": "gwinnett"}


def test_pinpoint_and_neogov_ingest_as_pollable_boards(monkeypatch):
    import careerkit

    registry = {"employers": [], "feeds": []}
    saved = []
    monkeypatch.setattr(careerkit, "load_registry", lambda: registry)
    monkeypatch.setattr(careerkit, "save_employers",
                        lambda reg: saved.append([dict(e) for e in reg["employers"]]))
    monkeypatch.setattr(careerkit, "queue_discovered", lambda _entries: None)

    added = careerkit._ingest([
        "https://sensiba.pinpointhq.com/postings/"
        "b1dd8157-3f5d-41fe-a846-215f1f9d9b41",
        "https://www.governmentjobs.com/careers/fulton/jobs/3828944/example",
    ])

    assert [(entry["ats"], entry["slug"]) for entry in added] == [
        ("pinpoint", "sensiba"), ("neogov", "fulton"),
    ]
    assert all(entry["active"] is True for entry in added)
    assert len(saved) == 1


def test_ingest_names_inactive_existing_board_instead_of_hiding_gap(
        monkeypatch, capsys):
    import careerkit

    registry = {"employers": [{
        "name": "Anthropic", "ats": "greenhouse", "slug": "anthropic",
        "active": False,
    }], "feeds": []}
    monkeypatch.setattr(careerkit, "load_registry", lambda: registry)
    monkeypatch.setattr(
        careerkit, "save_employers",
        lambda _reg: (_ for _ in ()).throw(AssertionError("must not rewrite registry")),
    )

    added = careerkit._ingest([
        "https://job-boards.greenhouse.io/anthropic/jobs/4954098008",
    ])

    assert added == []
    output = capsys.readouterr().out
    assert "already registered but inactive (greenhouse:anthropic)" in output
    assert "review why it was disabled" in output


def test_new_official_sources_are_url_discovered_not_name_guessed():
    from engine import discover

    assert {"pinpoint", "neogov"} <= set(discover.UNPROBEABLE)
    assert {"pinpoint", "neogov"}.isdisjoint(discover.PROBE_ORDER)


def test_ingest_writes_complete_addresses_and_dedupes_boards(monkeypatch):
    import careerkit

    registry = {"employers": [], "feeds": []}
    saved = []
    queued = []
    monkeypatch.setattr(careerkit, "load_registry", lambda: registry)
    monkeypatch.setattr(careerkit, "save_employers",
                        lambda reg: saved.append([dict(e) for e in reg["employers"]]))
    monkeypatch.setattr(careerkit, "queue_discovered",
                        lambda entries: queued.extend(entries))

    oracle_base = (
        "https://fa-etqd-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/"
        "CandidateExperience/en/sites/CX_1/requisitions/preview/"
    )
    urls = [
        oracle_base + "2026001760",
        oracle_base + "2026001835",
        "https://jobs.eightfold.ai/careers/job/12345?domain=acme.com",
        "https://acme.phenompeople.com/job/456",
        "https://recruiting.paylocity.com/Recruiting/Jobs/All/"
        "211e692d-c45a-4e3a-ae01-3e497af97929",
    ]
    added = careerkit._ingest(urls)

    assert len(added) == 4
    by_ats = {entry["ats"]: entry for entry in added}
    assert by_ats["oracle_orc"]["host"] == (
        "fa-etqd-saasfaprod1.fa.ocs.oraclecloud.com"
    )
    assert by_ats["oracle_orc"]["site"] == "CX_1"
    assert by_ats["eightfold"]["domain"] == "acme.com"
    assert by_ats["eightfold"]["host"] == "https://jobs.eightfold.ai"
    assert by_ats["phenom"]["host"] == "https://acme.phenompeople.com"
    assert by_ats["paylocity"]["guid"].startswith("211e692d-")
    assert len(saved) == 1
    assert queued == added


def test_ingest_rejects_unpollable_and_unsupported_without_writing(monkeypatch, capsys):
    import careerkit

    registry = {"employers": [], "feeds": []}
    writes = []
    monkeypatch.setattr(careerkit, "load_registry", lambda: registry)
    monkeypatch.setattr(careerkit, "save_employers", lambda _reg: writes.append(True))
    monkeypatch.setattr(careerkit, "queue_discovered", lambda _entries: writes.append(True))

    added = careerkit._ingest([
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4141169",
        "https://acme.breezy.hr/p/123-role",
    ])

    assert added == []
    assert registry["employers"] == []
    assert writes == []
    output = capsys.readouterr().out
    assert "use the employer's /Recruiting/Jobs/All/<board-guid> URL" in output
    assert "no pollable adapter" in output


def test_search_full_is_unbounded_unless_limit_is_explicit(monkeypatch, tmp_path, capsys):
    import careerkit

    queries = [f"site:boards.greenhouse.io role-{i}" for i in range(75)]
    limits = []
    monkeypatch.setattr(careerkit, "ROOT", tmp_path)
    monkeypatch.setattr(careerkit, "load_profile", lambda: None)
    monkeypatch.setattr(careerkit._search, "build_query_matrix", lambda **_kw: queries)

    def fake_run_matrix(_queries, *, limit):
        limits.append(limit)
        attempted = _queries if limit is None else _queries[:limit]
        return [], {query: 0 for query in attempted}

    monkeypatch.setattr(careerkit._search, "run_matrix", fake_run_matrix)
    monkeypatch.setattr(careerkit, "_ingest", lambda _urls: [])

    careerkit.cmd_search(Namespace(full=True, limit=None))
    assert limits[-1] is None
    assert "attempted 75/75 queries" in capsys.readouterr().out

    careerkit.cmd_search(Namespace(full=True, limit=12))
    assert limits[-1] == 12
    assert "attempted 12/75 queries" in capsys.readouterr().out

    careerkit.cmd_search(Namespace(full=False, limit=None))
    assert limits[-1] == 60
    assert "attempted 60/75 queries" in capsys.readouterr().out
