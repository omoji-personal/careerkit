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
import re

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
    ("workable", {"ats": "workable", "slug": "acme", "name": "Acme"}),
    ("oracle_orc", {"ats": "oracle_orc", "host": "ejov.fa.ca2.oraclecloud.com",
                    "site": "CX", "name": "Acme"}),
    ("eightfold", {"ats": "eightfold", "domain": "albemarle.com",
                   "host": "https://albemarle.eightfold.ai", "name": "Acme"}),
    ("pinpoint", {"ats": "pinpoint", "slug": "acme", "name": "Acme"}),
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
        assert j.board == adapters.board_id(cfg)
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


# --------------------------------------------------------------------------
# Text-based adapters. These parse XML or HTML rather than JSON, so they need a
# different stub, but the contract is identical: a real payload in, usable Jobs
# out. Personio serves XML; a change to its tag names is exactly the silent
# failure a fixture exists to catch.
# --------------------------------------------------------------------------

TEXT_CASES = [
    ("personio", "personio.xml", {"ats": "personio", "slug": "acme", "name": "Acme"}),
    ("hrmdirect", "hrmdirect.html", {"ats": "hrmdirect", "slug": "acme", "name": "Acme"}),
    ("teamtailor", "teamtailor.json",
     {"ats": "teamtailor", "slug": "career", "name": "Acme"}),
    ("icims", "icims.html", {"ats": "icims", "slug": "wipfli", "name": "Acme"}),
    ("jobvite", "jobvite.html",
     {"ats": "jobvite", "slug": "feedingamerica", "name": "Acme"}),
    ("paylocity", "paylocity.html",
     {"ats": "paylocity", "guid": "211e692d-c45a-4e3a-ae01-3e497af97929",
      "name": "Acme"}),
    ("phenom", "phenom.html",
     {"ats": "phenom", "host": "https://careers.phenom.com/global/en",
      "name": "Acme"}),
    ("neogov", "neogov.xml",
     {"ats": "neogov", "slug": "exampleagency", "name": "Acme"}),
]


@pytest.fixture
def stub_text(monkeypatch):
    def make(body, status=200):
        monkeypatch.setattr(adapters, "fetch", lambda url, *a, **k: (status, body))
        # Phenom's current public board uses HTML fallback only after its old
        # widgets JSON endpoint is absent.  Keep this suite entirely offline.
        monkeypatch.setattr(adapters, "fetch_json", lambda url, *a, **k: None)
    return make


@pytest.mark.parametrize("name,fixture,cfg", TEXT_CASES)
def test_text_adapter_maps_its_real_payload(name, fixture, cfg, stub_text):
    path = FIXTURES / fixture
    if not path.exists():
        pytest.skip(f"no fixture for {name}")
    stub_text(path.read_text())

    jobs = getattr(adapters, name)(cfg)

    assert jobs, f"{name} mapped a real payload to zero jobs"
    for j in jobs:
        assert j.title.strip(), f"{name}: empty title"
        assert j.url.startswith("http"), f"{name}: bad url {j.url!r}"
        assert j.source == name
        expected_company = "Example Public Agency" if name == "neogov" else "Acme"
        assert j.company == expected_company
        assert j.external_id, f"{name}: no external_id, uid collapses siblings"
        assert "<" not in j.description or ">" not in j.description, \
            f"{name}: HTML survived into the description"


@pytest.mark.parametrize("name,fixture,cfg", TEXT_CASES)
def test_text_adapter_survives_junk(name, fixture, cfg, stub_text):
    for body, status in (("", 200), ("<html>nope</html>", 200), ("", 404), ("x" * 50, 500)):
        stub_text(body, status)
        assert getattr(adapters, name)(cfg) == []


def test_hrmdirect_fetches_detail_for_a_relevant_title(monkeypatch):
    listing = (FIXTURES / "hrmdirect.html").read_text()
    detail = """
      <td class="viewFieldName"><b>Location:</b></td>
      <td class="viewFieldValue">Remote, United States<br></td>
      <div class="jobDesc"><p>Minimum Qualifications</p>
      <p>Five years of CRM delivery. Salary range $140,000 - $170,000.</p></div>
    """
    monkeypatch.setattr(adapters, "_looks_relevant", lambda title: "CRM" in title)
    monkeypatch.setattr(adapters, "fetch", lambda url, *a, **k:
                        (200, detail if "job-opening.php" in url else listing))
    jobs = adapters.hrmdirect({"ats": "hrmdirect", "slug": "acme", "name": "Acme"})
    crm = next(j for j in jobs if "CRM" in j.title)
    assert crm.location == "Remote, United States"
    assert "Minimum Qualifications" in crm.description
    assert crm.posted_at == "2026-07-15"


def test_workday_maps_its_real_payload(monkeypatch):
    payload = (FIXTURES / "workday.json").read_text()
    calls = {"n": 0}

    def fake(url, *a, **k):
        calls["n"] += 1
        return (200, payload) if calls["n"] == 1 else (200, '{"total":0,"jobPostings":[]}')

    monkeypatch.setattr(adapters, "fetch", fake)
    monkeypatch.setattr(adapters, "_looks_relevant", lambda title: False)
    cfg = {"ats": "workday", "tenant": "salesforce", "dc": "wd12",
           "site": "External_Career_Site", "name": "Acme"}
    jobs = adapters.workday(cfg)

    assert len(jobs) == 2
    assert {j.external_id for j in jobs} == {"JR354232", "JR332446"}
    assert all(j.board == "workday:salesforce/wd12/external_career_site" for j in jobs)
    assert all(j.url.startswith("https://salesforce.wd12.myworkdayjobs.com/") for j in jobs)


def test_workday_survives_junk(monkeypatch):
    cfg = {"ats": "workday", "tenant": "acme", "dc": "wd1",
           "site": "External", "name": "Acme"}
    for status, body in ((200, ""), (200, "not json"), (404, ""), (500, "x")):
        monkeypatch.setattr(adapters, "fetch", lambda *a, _s=status, _b=body, **k: (_s, _b))
        assert adapters.workday(cfg) == []


def test_workable_merges_current_multi_country_rows(stub_fetch):
    stub_fetch(json.loads((FIXTURES / "workable.json").read_text()))
    jobs = adapters.workable({"ats": "workable", "slug": "acme", "name": "Acme"})
    assert len(jobs) == 1
    assert jobs[0].location == "Spain; Portugal"


def test_teamtailor_maps_json_feed_location_and_date(stub_text):
    stub_text((FIXTURES / "teamtailor.json").read_text())
    jobs = adapters.teamtailor({"ats": "teamtailor", "slug": "career", "name": "Acme"})
    assert jobs[0].location == "Madrid, Europe, ES"
    assert jobs[0].posted_at == "2026-02-04"


def test_paylocity_maps_current_public_page_model(stub_text, monkeypatch):
    stub_text((FIXTURES / "paylocity.html").read_text())
    monkeypatch.setattr(adapters, "_looks_relevant", lambda title: False)
    jobs = adapters.paylocity({
        "ats": "paylocity", "guid": "211e692d-c45a-4e3a-ae01-3e497af97929",
        "name": "Acme",
    })
    assert len(jobs) == 2
    assert jobs[0].location == "Raleigh, NC, USA"
    assert jobs[0].url.endswith("/Details/4141169")


def test_pinpoint_maps_documented_full_posting_and_only_annual_usd(stub_fetch):
    stub_fetch(json.loads((FIXTURES / "pinpoint.json").read_text()))
    jobs = adapters.pinpoint({"ats": "pinpoint", "slug": "acme", "name": "Acme"})

    assert len(jobs) == 2
    annual, hourly = jobs
    assert annual.company == "Acme"
    assert annual.external_id == "post-001"
    assert annual.url == "https://acme.pinpointhq.com/en/postings/post-001"
    assert annual.location == "United States"
    assert annual.department == "Technology"
    assert annual.remote_flag is True
    assert (annual.comp_min, annual.comp_max, annual.comp_source) == (
        135000, 198000, "board",
    )
    for section in ("Lead the enterprise applications roadmap",
                    "Own architecture decisions", "Experience with CRM platforms",
                    "Medical and retirement plans", "Application deadline",
                    "2026-08-28"):
        assert section in annual.description

    assert hourly.department == "Consulting", "legacy top-level department was lost"
    assert hourly.remote_flag is None, "hybrid was overstated as fully remote"
    assert hourly.comp_min is None and hourly.comp_max is None
    assert "/ hour" in hourly.comp_text


def test_neogov_maps_official_rss_and_only_annual_usd(stub_text):
    # GovernmentJobs' live response currently exposes its UTF-8 BOM as these
    # three mojibake characters through the shared HTTP decoder.
    stub_text("ï»¿" + (FIXTURES / "neogov.xml").read_text())
    jobs = adapters.neogov({
        "ats": "neogov", "slug": "exampleagency", "name": "Acme",
    })

    assert len(jobs) == 2
    annual, hourly = jobs
    assert annual.company == "Example Public Agency"
    assert annual.external_id == "410001"
    assert annual.url.endswith("/careers/exampleagency/jobs/410001")
    assert annual.location == "Atlanta, Georgia"
    assert annual.department == "Information Technology"
    assert annual.posted_at == "2026-08-19"
    assert (annual.comp_min, annual.comp_max, annual.comp_source) == (
        135000, 175000, "board",
    )
    for section in ("Lead the agency applications portfolio",
                    "Own architecture and delivery governance",
                    "Five years of enterprise systems leadership",
                    "A background check is required", "Application deadline",
                    "2026-09-20"):
        assert section in annual.description

    assert hourly.department == "Project Management"
    assert hourly.comp_min is None and hourly.comp_max is None
    assert hourly.comp_text.endswith("/ Hour")


def test_neogov_uses_non_utc_deadline_fallback(stub_text):
    payload = (FIXTURES / "neogov.xml").read_text().replace(
        "advertiseToDateTimeUTC", "advertiseToDateTime")
    stub_text(payload)

    jobs = adapters.neogov({
        "ats": "neogov", "slug": "exampleagency", "name": "Acme",
    })

    assert "Application deadline\n2026-09-20" in jobs[0].description


def test_oracle_fetches_public_detail_requirements_for_relevant_titles(monkeypatch):
    listing = json.loads((FIXTURES / "oracle_orc.json").read_text())
    detail = json.loads((FIXTURES / "oracle_orc_detail.json").read_text())
    calls = []

    def fake_fetch_json(url, **_kwargs):
        calls.append(url)
        if "recruitingCEJobRequisitionDetails/" in url:
            return detail
        return listing if len([u for u in calls if "findReqs" in u]) == 1 else {
            "items": [],
        }

    monkeypatch.setattr(adapters, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(adapters, "_RELEVANT_HINT", re.compile(
        "Technology and Communications", re.I))

    jobs = adapters.oracle_orc({
        "ats": "oracle_orc", "host": "example.fa.example.oraclecloud.com",
        "site": "CX", "name": "Acme",
    })

    relevant = next(job for job in jobs if "Technology" in job.title)
    assert "Lead enterprise systems delivery" in relevant.description
    assert "Own the implementation roadmap" in relevant.description
    assert "Five years of CRM delivery" in relevant.description
    assert relevant.location == "Atlanta, GA"
    assert relevant.posted_at == "2026-08-19"
    assert sum("recruitingCEJobRequisitionDetails/" in url for url in calls) == 1


# --------------------------------------------------------------------------
# End to end: fixtures in, report out
# --------------------------------------------------------------------------
# Every test above exercises one layer. Nothing ran the whole chain, which is
# how the report came to disagree with the database that produced it: each half
# was individually correct. This drives real payloads through the real adapters,
# the real scorer, real storage and the real renderer, and asserts the document
# a person would actually read.

def test_the_whole_chain_from_payload_to_report(tmp_path, monkeypatch):
    import json as _json
    import yaml as _yaml
    from engine import consistency, pull as _pull, store as store2
    from engine.report import write_report
    from engine.score import Profile

    monkeypatch.setenv("CAREERKIT_HOME", str(tmp_path))
    for mod in ("engine.store", "engine.report"):
        import importlib, sys
        importlib.reload(sys.modules[mod])

    payload = _json.loads((FIXTURES / "greenhouse.json").read_text())
    calls = {"n": 0}

    def fake(url, *a, **k):
        calls["n"] += 1
        return payload if calls["n"] == 1 else {"jobs": [], "content": []}
    monkeypatch.setattr(adapters, "fetch_json", fake)

    jobs = adapters.greenhouse({"ats": "greenhouse", "slug": "acme", "name": "Acme"})
    assert jobs, "fixture produced no jobs"

    # The fixture's postings state no salary, so on their own they never
    # exercise the path where the scorer resolves a band from the body. That is
    # the exact defect this chain was built to catch, and without a posting that
    # has one the test passes with the bug reintroduced. Add one.
    from engine.models import Job as _Job
    jobs.append(_Job(
        company="Acme", title="Senior Data Scientist", url="https://boards.test/acme/9",
        source="greenhouse", external_id="9", location="Remote, United States",
        description=("Minimum Qualifications\n- 5 years of experimentation work.\n"
                     "The salary range for this role is $150,000 - $208,000 per year. "
                     + "d" * 300)))

    prof = tmp_path / "profile.yaml"
    prof.write_text(_yaml.safe_dump({
        # Real lane patterns, matched to what the fixture actually contains.
        # A catch-all is rejected by the profile guard, correctly: a rule that
        # matches everything in an exclusion list hides every posting.
        "lanes": [{"key": "ds", "titles": ["/data scientist/"]},
                  {"key": "ae", "titles": ["/account executive/"]}],
        "location": {"remote_us": True, "metros": ["London", "Bangalore"],
                     "relocation": True},
    }))
    profile = Profile.load(prof)

    from engine.score import score_all
    scored = score_all(jobs, profile)
    keep = [j for j in scored if j.gate in ("QUALIFIED", "VERIFY")]

    con = store2.connect()
    run_id = store2.start_run(con)
    store2.upsert(con, keep, run_id=run_id)
    rows = list(con.execute(
        "SELECT * FROM jobs WHERE gate IN ('QUALIFIED','VERIFY') ORDER BY score DESC"))
    path = write_report(con, rows, health=[], run_detail={}, run_id=run_id)

    text = pathlib.Path(path).read_text()
    assert "# Sourcing run" in text

    # The claim this whole file exists to make: what a person reads matches what
    # the database holds.
    assert consistency.check_report(con, path) == [], consistency.check_report(con, path)
    assert consistency.check_db(con) == [], consistency.check_db(con)

    # And every stored row survives the round trip the scorer depends on.
    from engine.score import score as score_one
    for row in con.execute("SELECT * FROM jobs"):
        rebuilt = _pull.job_from_row(row)
        score_one(rebuilt, profile)
        assert (rebuilt.gate, rebuilt.score) == (row["gate"], row["score"]), \
            f"{row['company']} / {row['title']}: {row['gate']}/{row['score']} -> " \
            f"{rebuilt.gate}/{rebuilt.score}"


def test_adapter_fixture_coverage_does_not_regress():
    """Which adapters are covered by a real payload, and which are not.

    All twenty adapters now have a sanitized fixture using a real public board
    response shape. Writing a fixture from an assumed shape would be worse than
    none, because it would test the assumption rather than the board.

    Asserting the current count stops the gap being forgotten, and fails loudly
    if somebody adds an adapter without one.
    """
    covered = ({p.stem for p in FIXTURES.glob("*.json")} |
               {p.stem for p in FIXTURES.glob("*.xml")} |
               {p.stem for p in FIXTURES.glob("*.html")})
    known = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee",
             "bamboohr", "rippling", "teamtailor", "workday", "oracle_orc", "eightfold",
             "phenom", "icims", "jobvite", "paylocity", "personio", "hrmdirect",
             "pinpoint", "neogov"}
    have = covered & known
    missing = known - covered
    assert len(have) >= 20, (
        f"adapter fixture coverage regressed to {len(have)}/20: {sorted(have)}")
    assert missing == set(), (
        f"the uncovered set changed; update this list deliberately: {sorted(missing)}")
