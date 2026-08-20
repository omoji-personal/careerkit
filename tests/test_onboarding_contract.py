"""Static contract for the resumable, search-first onboarding workflow.

The conversational setup is product behavior. These checks keep its ordering,
privacy boundary, checkpoints, and completion handoff from drifting even though
the workflow itself is Markdown rather than Python.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _assert_ordered(text: str, *needles: str) -> None:
    positions = [text.index(needle) for needle in needles]
    assert positions == sorted(positions), list(zip(needles, positions))


def test_setup_gates_privacy_then_inspects_and_resumes_safely():
    setup = _read(".claude/skills/setup/SKILL.md")

    _assert_ordered(
        setup,
        "## Phase 0 — privacy gate",
        "## Privacy disclosure and acknowledgment",
        "### After acknowledgment — inspect and resume",
        "## Phase 1 — Search Core",
        "## Phase 2 — First Win",
        "## Phase 3 — Source Expansion and calibration",
        "## Phase 4 — Career Pack",
        "## Phase 5 — Final Checks and handoff",
    )
    assert "profile/setup-progress.md" in setup
    phase_zero = setup[setup.index("## Phase 0"):setup.index("## Privacy disclosure")]
    assert "./careerkit.py doctor" in phase_zero
    assert "Do not open the checkpoint with a file-reading tool" in phase_zero
    assert "without sending the checkpoint contents to Claude" in _flat(phase_zero)
    assert "Resume the first `pending`" in setup
    assert "do not automatically resume" in setup
    assert "explicit confirmation before restarting or overwriting" in setup
    assert "content-free checklist" in setup
    assert "Update the checklist after every phase" in setup
    assert "sibling temporary file" in setup
    assert "atomically replace `profile/setup-progress.md`" in _flat(setup)
    for phase in ("Privacy", "Search Core", "First Win", "Source Expansion",
                  "Career Pack", "Final Checks"):
        assert f"{phase}:" in setup


def test_search_core_precedes_optional_first_win_and_application_data():
    setup = _read(".claude/skills/setup/SKILL.md")
    core = setup[setup.index("## Phase 1"):setup.index("## Phase 2")]
    first_win = setup[setup.index("## Phase 2"):setup.index("## Phase 3")]
    expansion = setup[setup.index("## Phase 3"):setup.index("## Phase 4")]
    flat_core = _flat(core)

    assert "source document is optional" in core
    assert "profile/profile.yaml" in core
    assert "autonomy.submit: ask_each" in core
    assert core.index("profile/profile.yaml") < core.index("./careerkit.py profile-lint")
    assert "EEO" not in core and "story bank" not in core
    assert "`exclusions.titles`" in flat_core
    assert "dream company may waive" in flat_core
    assert "`exclusions.titles_always`" in flat_core
    assert "no dream company ever waives" in flat_core
    assert "generic industry, company-size, or stage preferences" in flat_core
    assert "dedicated scoring field" in flat_core
    assert "`hard_requirements`" in flat_core
    assert "`exclusions.body_patterns`" in flat_core
    assert "target metros/states" not in flat_core
    assert "target metros" in flat_core

    assert "./careerkit.py pull --feeds --no-cache" in first_win
    assert "optional and bounded" in first_win
    assert "not a complete search" in _flat(first_win)
    assert "does not activate Freehire" in first_win
    assert "Show the active feed names" in first_win
    assert "If Freehire is already `active: true`" in first_win
    assert "First Win consent alone is insufficient" in first_win

    assert "normal cache" in expansion
    _assert_ordered(expansion, "./careerkit.py pull", "./careerkit.py audit")
    assert "`profile-lint`, `rescore`, `pull`, and `audit`" in expansion


def test_privacy_precedes_documents_and_freehire_needs_separate_activation():
    setup = _read(".claude/skills/setup/SKILL.md")
    flat = _flat(setup)
    privacy = setup[setup.index("## Privacy disclosure"):setup.index("## Phase 1")]

    _assert_ordered(setup, "Privacy disclosure", "After acknowledgment",
                    "./careerkit.py profile-lint", "Ask for their resume")
    for phrase in (
        "employer boards, feeds, and pasted URLs",
        "optional keyed feeds transmit",
        "USAJobs also includes",
        "cannot retract data already sent",
        "configured `search_terms` phrase",
        "separate, explicit activation confirmation",
        "general setup or sourcing consent is not activation consent",
    ):
        assert phrase in flat
    _assert_ordered(privacy, "After acknowledgment",
                    "create or update `profile/setup-progress.md`")
    assert setup.index("Privacy disclosure") < setup.index(
        "existing `profile/profile.yaml`"
    )


def test_career_pack_is_deferable_without_weakening_truth_or_submission_rules():
    setup = _read(".claude/skills/setup/SKILL.md")
    pack = setup[setup.index("## Phase 4"):setup.index("## Phase 5")]
    flat_pack = _flat(pack)

    assert "Searching, ingesting, evaluating, changing criteria, and auditing remain" in flat_pack
    assert "verified facts only" in flat_pack
    assert "unconfirmed claim" in flat_pack
    assert "preserve `autonomy.submit: ask_each`" in flat_pack
    assert "`/compose` needs confirmed claims and voice samples" in flat_pack
    assert "`/prep` needs the story bank" in flat_pack
    assert "`/apply` needs confirmed claims plus identity/work-auth/EEO choices" in flat_pack
    assert "umbrella, not evidence that every artifact is complete" in flat_pack
    assert "leave it `deferred` until" in flat_pack
    assert "Never re-ask a completed destination" in pack
    assert "after `/compose` has completed claims and voice" in pack
    assert "a later `/apply` asks only for missing application fields" in flat_pack
    for destination in ("`profile/claims.md`", "`profile/style.md`",
                        "`profile/person.md`", "`profile.yaml.identity`",
                        "`profile.yaml.work_auth`", "`profile.yaml.eeo`",
                        "`profile/tracker.md`"):
        assert destination in flat_pack
    assert "do not score jobs" in flat_pack
    assert "not scorer inputs" in flat_pack


def test_setup_finishes_with_readiness_checks_and_an_explicit_handoff():
    setup = _read(".claude/skills/setup/SKILL.md")
    final = setup[setup.index("## Phase 5"):]

    _assert_ordered(
        final,
        "./careerkit.py db check",
        "./careerkit.py consistency",
        "./careerkit.py coverage",
        "./careerkit.py doctor",
    )
    for heading in ("**First shortlist:**", "**Coverage:**", "**Deferred work:**",
                    "**Local state:**", "**Exact next action:**"):
        assert heading in final
    assert "never call deferred work complete" in final
    assert "should not be repeated immediately" in final


def test_public_docs_and_operating_rules_describe_the_same_two_milestones():
    readme = _flat(_read("README.md"))
    claude = _flat(_read("CLAUDE.md"))
    guide_md = _flat(_read("guide/CAREERKIT-GUIDE.md"))
    guide_html = _flat(_read("guide/careerkit-guide.html"))

    for phrase in ("two milestones", "Search Core", "profile/setup-progress.md",
                   "First Win", "about 40 minutes", "Workspace Trust",
                   ".claude/skills/setup/SKILL.md", "/skills", "claude doctor",
                   "https://code.claude.com/docs/en/setup"):
        assert phrase in readme
    for phrase in ("resumes the first pending phase", "its `Search Core` checkpoint",
                   "resume `/setup`", "later optional phases may remain `deferred`",
                   "must not block", "content-free phase checklist"):
        assert phrase in claude
    assert "Ordinary `/search` never resumes a deferred optional phase" in claude
    assert "bypass unrelated deferred phases" in claude
    for guide in (guide_md, guide_html):
        assert "Search Core" in guide
        assert "profile/setup-progress.md" in guide
        assert "First Win" in guide
        assert "separate activation confirmation" in guide
        assert "40 minutes" in guide
        assert "/skills" in guide
        assert "claude doctor" in guide
        assert "code.claude.com/docs/en/setup" in guide
        assert "do not score jobs" in guide
    for public_doc in (readme, guide_md, guide_html):
        assert "exclusions.titles" in public_doc
        assert "exclusions.titles_always" in public_doc
        assert "waive" in public_doc
