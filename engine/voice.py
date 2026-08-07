"""Voice lint: find the things that make career writing read as machine-written.

This exists because of a measured failure. Five LinkedIn notes were drafted for
one user, an adversarial review found 43 separate tells, and three targeted
rewrites all scored 6/10. The verdict was not "the prose is weak", it was: "you
asked whether they read as written individually by one person, and the answer is
no." Rewriting harder did not fix it. Knowing which specific things to stop doing
did.

**This is a LINT, not a rewriter, and that is the whole design.**

The commercial category called "AI humanizers" paraphrases text and swaps
synonyms to defeat a detector. Against the failure this module addresses, that
makes things WORSE, and the reason is worth stating because it is unintuitive:

    The single most damaging tell in the reviewed batch was the same fact
    restated in different words across notes ("focused" here, "concentrating"
    there). Real people repeat themselves with the SAME words. Deliberate
    lexical variation is exactly what a generator produces when instructed to
    make each output unique. Synonym substitution is the tell, not the cure.

So nothing here rewrites anything. It reports, the human writes. That also keeps
it compatible with the rule the same failure produced: brief the writer, never
ghostwrite.

What it CANNOT do, stated plainly because a lint trusted too far is worse than
none:

  - It cannot tell whether a sentence is TRUE. Use `claims-lint` for that.
  - It cannot judge whether the writing is good, only whether it carries
    patterns that read as generated.
  - A clean result is not a certificate. The batch that scored 6/10 would pass
    several of these checks individually; it failed on the combination.
  - The mechanism-versus-register check is a heuristic over abstract nouns. It
    will flag some honest abstraction and miss some genuine vagueness.

Calibration: the checks below were selected because they separate a real
labelled pair. The killed batch trips merge-field, dead-phrase and contraction
checks. The rewritten notes that replaced it, two of which were accepted and one
of which drew a warm reply, come back clean. `tests/test_voice.py` holds both
sets and fails if that separation ever stops holding.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# ---------------------------------------------------------------- finding type


class Finding:
    __slots__ = ("check", "severity", "message", "evidence", "where")

    def __init__(self, check, severity, message, evidence="", where=""):
        self.check = check
        self.severity = severity          # "high" | "medium" | "low"
        self.message = message
        self.evidence = evidence
        self.where = where

    def __repr__(self):                                    # pragma: no cover
        return f"<Finding {self.check} {self.severity}>"


_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


# ------------------------------------------------------------------- utilities

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


def sentences(text: str) -> list[str]:
    """Sentences, with the bullet and heading furniture stripped off."""
    out = []
    for block in (text or "").split("\n"):
        line = block.strip().lstrip("-*#>").strip()
        if not line or line.startswith("```"):
            continue
        for s in _SENT_SPLIT.split(line):
            s = s.strip()
            if len(s.split()) >= 3:
                out.append(s)
    return out


_WORD = re.compile(r"[a-z][a-z'-]*")


def _tokens(s: str) -> list[str]:
    return _WORD.findall(s.lower())


# Function words carry the shape of a sentence. Two sentences with the same
# skeleton and different content words are the thesaurus tell; two with the same
# content words are a person repeating themselves, which is fine.
_FUNCTION = {
    "a", "an", "the", "and", "or", "but", "so", "if", "as", "at", "by", "for",
    "from", "in", "into", "of", "on", "to", "with", "that", "which", "who",
    "i", "my", "me", "you", "your", "we", "our", "they", "their", "it", "its",
    "is", "am", "are", "was", "were", "be", "been", "have", "has", "had",
    "do", "does", "did", "will", "would", "can", "could", "just", "now", "not",
    "this", "these", "those", "there", "here", "than", "then", "about", "over",
}


def _skeleton(s: str) -> tuple[str, ...]:
    return tuple(t for t in _tokens(s) if t in _FUNCTION)


def _content(s: str) -> set[str]:
    return {t for t in _tokens(s) if t not in _FUNCTION and len(t) > 2}


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(1, len(a | b))


# ------------------------------------------------------------ per-draft checks

# "No ask" draws attention to the ask and contradicts itself the moment the
# writer is in fact live in a role at that person's employer.
_DEAD_PHRASE = re.compile(
    r"\b(no ask(?:\s+(?:here|attached|intended))?|not asking for anything|"
    r"nothing to ask|just wanted to (?:connect|reach out)|"
    r"reaching out to connect|wanted to (?:be connected|connect) (?:to|with) the people|"
    r"i hope this (?:message |email )?finds you well|"
    r"i wanted to take a moment|in today's (?:fast[- ]paced|ever[- ]changing))\b", re.I)

_CONTRACTION = re.compile(
    r"\b\w+(?:'|’)(?:s|t|re|ve|ll|d|m)\b|\bcan(?:'|’)t\b|\bwon(?:'|’)t\b", re.I)

# Abstract head nouns that stand in for a mechanism. The user's own correction
# is the canonical example: "an operations layer built on its hook system"
# (register) versus "it sits between the agent and Salesforce and reads each
# command before it goes out" (mechanism).
_ABSTRACT = re.compile(
    r"\b(ecosystem|landscape|space|layer|framework|paradigm|synerg\w+|leverag\w+|"
    r"holistic|robust|seamless|cutting[- ]edge|best[- ]in[- ]class|value[- ]add|"
    r"solutions?|offerings?|capabilit\w+|touchpoints?|bandwidth|alignment)\b", re.I)

_NOMINAL = re.compile(r"\b\w{4,}(?:tion|ment|ance|ence|ility|ization|isation)s?\b", re.I)

# NOT IMPLEMENTED, DELIBERATELY: the ", which is X" appositive.
#
# It was in the first version, because the review that started this module
# listed "balanced two-clause reflections" among the tells. Run against the
# labelled data it fired on three of the four notes that WERE accepted,
# including the exact sentence the reviewer had praised as the bravest line in
# the batch ("...on your team, which makes this a slightly awkward connection
# request"). Roughly a 75% false-positive rate on known-good text.
#
# Narrowing it did not rescue it either: requiring a coordinated pair still
# flagged "which is either bad timing or a clear answer", and requiring an
# abstract head noun still flagged "connection request".
#
# A check that fires on most of your good writing trains you to ignore the
# output, which costs more than the tell it catches. `test_voice.py` asserts the
# accepted batch stays free of high and medium findings, so re-adding this
# breaks the suite on purpose.
_PEOPLE_CLOSER = re.compile(r"\bthe people\s+\w+ing\b", re.I)
_COLON_PIVOT = re.compile(r"^[^:\n]{15,80}:\s+[a-z]", re.M)


def check_draft(text: str, name: str = "draft") -> list[Finding]:
    """Checks that need only one document."""
    out = []
    sents = sentences(text)
    words = _tokens(text)

    for m in _DEAD_PHRASE.finditer(text):
        out.append(Finding(
            "dead-phrase", "high",
            "A phrase that reads as filler in every message it appears in.",
            m.group(0), name))

    if len(words) >= 40:
        n = len(_CONTRACTION.findall(text))
        if n == 0:
            out.append(Finding(
                "contractions", "high",
                f"No contractions in {len(words)} words. Most people write with "
                f"them constantly; their total absence is one of the most "
                f"consistent machine tells.", "", name))
        elif n / (len(words) / 100) < 1.0:
            out.append(Finding(
                "contractions", "low",
                f"{n} contraction(s) in {len(words)} words. Low, though not "
                f"necessarily wrong for a formal cover letter.", "", name))

    if "—" in text:
        out.append(Finding("em-dash", "medium",
                           "Em-dash present. House rule is none in outward writing.",
                           "—", name))

    for m in _PEOPLE_CLOSER.finditer(text):
        out.append(Finding("stock-closer", "high",
                           "'the people [do]ing [thing]' closer.",
                           m.group(0), name))

    for s in sents:
        toks = _tokens(s)
        if len(toks) < 8:
            continue
        abstract = len(_ABSTRACT.findall(s)) + len(_NOMINAL.findall(s))
        if abstract >= 3 or (abstract >= 2 and len(toks) < 18):
            out.append(Finding(
                "register-not-mechanism", "medium",
                "Dense abstract nouns where a mechanism would be more "
                "convincing. Say what the thing does, concretely.",
                s[:140], name))

    return out


# ------------------------------------------------------------- cross-draft check

def check_batch(drafts: dict[str, str]) -> list[Finding]:
    """Checks that only exist once there is more than one document.

    This is where the batch that got killed actually failed. Each note was
    defensible alone; together they were obviously one template.
    """
    out = []
    names = list(drafts)
    if len(names) < 2:
        return out

    parsed = {n: sentences(drafts[n]) for n in names}

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            sa, sb = parsed[a], parsed[b]
            if not sa or not sb:
                continue

            # Openings and closings carry the most weight: they are what a
            # reader compares when two notes arrive from one person, and two
            # recipients who share a mutual connection can literally compare.
            for label, x, y in (("opening", sa[0], sb[0]),
                                ("closing", sa[-1], sb[-1])):
                r = _ratio(x, y)
                if r >= 0.60:
                    out.append(Finding(
                        "merge-field", "high",
                        f"The {label} sentences of {a} and {b} are {r:.0%} "
                        f"identical. That is a merge field, not a sentence.",
                        f"{a}: {x[:90]}\n    {b}: {y[:90]}", f"{a} + {b}"))

            for x in sa:
                for y in sb:
                    if len(_tokens(x)) < 6 or len(_tokens(y)) < 6:
                        continue
                    r = _ratio(x, y)
                    # A near-copy with small substitutions. Identical sentences
                    # are deliberately NOT flagged here: reusing your own words
                    # verbatim is what people do.
                    if 0.72 <= r < 0.985:
                        out.append(Finding(
                            "near-duplicate", "high",
                            f"Near-identical sentences ({r:.0%}) differing by a "
                            f"few substituted words.",
                            f"{a}: {x[:90]}\n    {b}: {y[:90]}", f"{a} + {b}"))
                        continue
                    # The thesaurus tell: same shape, different content words.
                    # Skeletons are compared by similarity rather than equality,
                    # because a genuine reword rarely preserves the function
                    # words exactly ("at the moment" becomes "right now") and
                    # demanding an exact match caught nothing real.
                    skel_x, skel_y = _skeleton(x), _skeleton(y)
                    if min(len(skel_x), len(skel_y)) >= 4 and \
                            _ratio(" ".join(skel_x), " ".join(skel_y)) >= 0.70:
                        overlap = _jaccard(_content(x), _content(y))
                        if overlap < 0.40:
                            out.append(Finding(
                                "thesaurused", "high",
                                "Same sentence skeleton, different content words. "
                                "Real repetition reuses the same words; varying "
                                "them is what a generator does when told to make "
                                "each one unique.",
                                f"{a}: {x[:90]}\n    {b}: {y[:90]}", f"{a} + {b}"))
    return out


# ------------------------------------------------------------------- reporting

def lint(drafts: dict[str, str]) -> list[Finding]:
    out = []
    for name, text in drafts.items():
        out.extend(check_draft(text, name))
    out.extend(check_batch(drafts))
    out.sort(key=lambda f: (_SEV_ORDER[f.severity], f.check))
    return out


def format_report(findings: list[Finding], count: int) -> str:
    if not findings:
        return (f"voice-lint: {count} draft(s), nothing flagged.\n"
                "This is not a certificate. It means none of the known tells "
                "fired; read it aloud before you send it.")
    lines = [f"voice-lint: {len(findings)} finding(s) across {count} draft(s)", ""]
    for f in findings:
        lines.append(f"[{f.severity.upper():<6}] {f.check}  ({f.where})")
        lines.append(f"         {f.message}")
        if f.evidence:
            for ev in f.evidence.split("\n"):
                lines.append(f"         > {ev.strip()}")
        lines.append("")
    high = sum(1 for f in findings if f.severity == "high")
    if high:
        lines.append(f"{high} high-severity. Rewrite from scratch rather than "
                     f"editing; edited generated prose keeps reading as generated.")
    return "\n".join(lines)
