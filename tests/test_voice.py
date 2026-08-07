"""The voice lint is calibrated against a real labelled pair, not intuition.

BATCH_KILLED is five LinkedIn notes that an adversarial review scored at 43
tells and that their author stopped mid-send: "this would be perceived as bulk
spam and would be terrible."

BATCH_SENT is what replaced them, written from scratch against those findings.
Two of the four were accepted within hours and one drew a warm reply quoting the
exact line the reviewer had called the bravest thing in the note.

A lint that cannot separate those two sets is worthless, so that separation is
the test. Everything else here defends a specific way the checks could go wrong:
flagging honest repetition, or firing on ordinary formal prose.
"""
from __future__ import annotations

import pytest

from engine.voice import check_batch, check_draft, lint, sentences


# The batch that was killed. Note 1 and 2 open with the same clause and one
# swapped noun, and close with the same sentence. All three carry "no ask".
BATCH_KILLED = {
    "snowden": (
        "Seven years building Salesforce for legal aid and nonprofit orgs, and "
        "lately a lot of applied Agentforce work. I keep circling the Success "
        "Architecture side of the house. No ask here, just want my corner of "
        "the ecosystem connected to the people leading that org."),
    "parnell": (
        "Seven years building Salesforce for legal aid and education orgs, now "
        "in process with your CSG PubSec team. No ask here, just want my corner "
        "of the ecosystem connected to the people leading it."),
    "kramer": (
        "I run most of my working life on Claude Code and last week open sourced "
        "an operations layer built on its hook system. Just applied to the CS "
        "Industries team. Wanted to be connected to the people doing customer "
        "success at the frontier."),
}

# What actually went out. Note these DO repeat a fact across notes ("seven
# years ... legal aid"), in much the same words. That is a person repeating
# themselves and must not be flagged.
BATCH_SENT = {
    "snowden": (
        "Saw \"Supportability + AI nerd\" in your headline and figured I'd "
        "connect. I've spent the last year putting Agentforce and Prompt Builder "
        "into legal aid orgs, which is cloud readiness from the other side of "
        "the table. Seven years of Salesforce delivery before that."),
    "parnell": (
        "I'm somewhere in the process for the Global Public Sector CSM role on "
        "your team, which makes this a slightly awkward connection request. "
        "Sending it anyway. I've spent seven years building Salesforce for legal "
        "aid and education nonprofits."),
    "domnescu": (
        "I applied to the CS Industries role last week, so partly this is that. "
        "But also: I built a thing called Torque that sits between Claude Code "
        "and Salesforce and reads each command before it goes out, because "
        "agents kept reporting deploys that hadn't worked."),
    "ottolini": (
        "I've had two applications sitting with NeuraFlash since early July, "
        "which is either bad timing or a clear answer. Either way, figured I'd "
        "introduce myself directly. Seven years of Salesforce delivery for "
        "nonprofits, mostly legal aid, plus a lot of Agentforce work this year."),
}


# Text the user demonstrably wrote himself, which is the hardest bar: the lint
# must stay quiet on it. Provenance matters here and was checked rather than
# assumed. His published Torque post is his own rewrite, bearing no resemblance
# to the draft prepared for him; the Landmark note is logged as "owner drafted
# this himself"; the recruiter question is his unaided reply in the thread.
#
# Deliberately NOT in this set: the four connection notes that were accepted,
# and the CareerKit announcement, which published verbatim from a prepared file.
# Those were written FOR him. Calibrating his voice against them would make the
# whole tool circular.
OWN_WRITING = {
    "torque-post": (
        "I've been using Claude to do real work in Salesforce orgs for a while "
        "now, and it's genuinely good at it. Faster than me for most things.\n\n"
        "What kept catching me out was verification. An agent deploys something, "
        "gets a success response, and tells you it's done. Salesforce agrees. "
        "And it can still be wrong in ways nothing in that response would show "
        "you. Permissions that were never granted. A bulk job reporting success "
        "with failed rows inside it.\n\n"
        "None of that is the agent being careless. The API tells the truth. It's "
        "just that \"the call succeeded\" and \"the work is done\" are two "
        "different things, and nothing was checking the second one.\n\n"
        "So I built Torque. It sits between the agent and Salesforce and reads "
        "each command before it goes out.\n\n"
        "It's MIT and public. Fair warning: the repo is about a week old, and "
        "most of it was written by Claude with Torque's own guardrails pointed "
        "back at it the whole time. That felt like the only way to build this "
        "without being a hypocrite about it."),
    "landmark-note": (
        "Hey Michael. I was looking at Landmark, looks like an exciting venture "
        "with some quality ex-Jabian talent. Was wondering if you think there is "
        "any Salesforce work I could get involved with, either in-house or as an "
        "offering to clients. Happy to chat or meet if you think there is "
        "something worth exploring."),
    "recruiter-ask": (
        "Hi! This sounds like an exciting opportunity! Can you please share more "
        "information and a compensation range? Thank you."),
}


def _checks(findings):
    return {f.check for f in findings}


def test_his_own_writing_is_not_flagged():
    """The bar that matters most. Every finding here is a false positive by
    definition, because a human wrote it and it worked.

    Two real ones were caught this way and fixed. The 53-word Landmark note has
    no contractions and was flagged HIGH until the threshold moved to 120 words.
    "Can you please share more information and a compensation range?" was flagged
    as evasive register because "information" and "compensation" end in -tion.
    """
    noisy = [f for f in lint(OWN_WRITING) if f.severity in ("high", "medium")]
    assert not noisy, "flagged the user's own writing:\n" + "\n".join(
        f"  {f.check} ({f.where}): {f.message}" for f in noisy)


def test_the_killed_batch_is_flagged():
    """If this stops failing, the lint has stopped working."""
    findings = lint(BATCH_KILLED)
    assert findings, "the batch a human called bulk spam came back clean"
    assert any(f.severity == "high" for f in findings)


def test_the_killed_batch_is_caught_as_a_batch_not_just_line_by_line():
    """Each note was defensible alone. Together they were one template, and that
    is the failure the reviewer actually named."""
    assert "merge-field" in _checks(check_batch(BATCH_KILLED))


def test_the_sent_batch_passes():
    """The notes that got accepted must come back quiet, or nobody will use it.

    Deliberately stricter than "no high": nothing above LOW is allowed. A first
    draft of this module included an appositive check that fired on three of
    these four, one of them the line the reviewer singled out as the best in the
    batch. A lint that flags most of your good writing gets ignored, so the bar
    is that known-good text stays silent and any new check must clear it.
    """
    noisy = [f for f in lint(BATCH_SENT) if f.severity in ("high", "medium")]
    assert not noisy, "flagged known-good notes:\n" + "\n".join(
        f"  {f.check} ({f.where}): {f.evidence[:80]}" for f in noisy)


def test_repeating_the_same_fact_in_the_same_words_is_not_a_tell():
    """The distinction the whole module rests on. Two notes that state one true
    fact using the same words are a person; two that thesaurus it are a machine."""
    honest = {
        "a": "I've spent seven years building Salesforce for legal aid. The org "
             "I work at is small and the work is hands on every day.",
        "b": "Different opening entirely, since this person needs another hook. "
             "I've spent seven years building Salesforce for legal aid.",
    }
    assert "thesaurused" not in _checks(check_batch(honest))
    assert "near-duplicate" not in _checks(check_batch(honest))


def test_the_thesaurus_tell_is_caught():
    """Same skeleton, swapped content words: what a generator does when it is
    told to make each output unique."""
    varied = {
        "a": "I am focused on the delivery side of the platform at the moment.",
        "b": "I am concentrating on the implementation side of the product right now.",
    }
    assert "thesaurused" in _checks(check_batch(varied))


@pytest.mark.parametrize("phrase", [
    "No ask here, just saying hello to you today.",
    "Just wanted to connect with you about the work.",
    "I hope this message finds you well this morning.",
])
def test_dead_phrases_are_flagged(phrase):
    assert "dead-phrase" in _checks(check_draft(phrase))


def test_absent_contractions_are_flagged_only_on_enough_text():
    short = "Thanks for the note. I will take a look at it."
    assert "contractions" not in _checks(check_draft(short))
    long = ("Thank you for reaching out about the position. " * 6 +
            "I would be glad to discuss it further at your convenience.")
    assert "contractions" in _checks(check_draft(long))


def test_a_draft_with_contractions_is_not_flagged_for_them():
    text = ("I've read the description and I don't think I'm the right fit. "
            "It isn't the product depth you need, and I'd rather say so now "
            "than waste a screening call on it. I'll keep an eye out anyway.")
    assert "contractions" not in _checks(check_draft(text))


def test_register_without_mechanism_is_flagged():
    assert "register-not-mechanism" in _checks(check_draft(
        "I built an operations layer leveraging its framework capabilities to "
        "deliver seamless value-add alignment across the ecosystem."))


def test_the_mechanism_version_of_the_same_claim_is_clean():
    """The author's own rewrite, which is the canonical good example."""
    assert "register-not-mechanism" not in _checks(check_draft(
        "I built a thing called Torque that sits between Claude Code and "
        "Salesforce and reads each command before it goes out, because agents "
        "kept reporting deploys that hadn't worked."))


def test_em_dash_is_flagged():
    assert "em-dash" in _checks(check_draft("I looked at the role — it is not a fit."))


def test_a_single_draft_never_produces_batch_findings():
    """Cross-draft checks need two documents; one must not compare with itself."""
    assert check_batch({"only": BATCH_KILLED["snowden"]}) == []


def test_sentences_ignores_markdown_furniture():
    text = "# Heading here\n\n- a bullet that is long enough to count\n```\ncode\n```"
    got = sentences(text)
    assert "```" not in " ".join(got)
    assert any("bullet" in s for s in got)
    assert not any(s.startswith("#") for s in got)


def test_empty_input_is_safe():
    assert check_draft("") == []
    assert lint({}) == []
