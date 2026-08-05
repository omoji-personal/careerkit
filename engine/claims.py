"""Claims lint: flag checkable facts in a draft that the register does not back.

CareerKit's first rule is that nothing may be written about a person that is not
in `profile/claims.md`. That rule is enforced by the model, and a model can drift
across a long document. This is a cheap mechanical second pass over the specific
things that are both checkable and expensive to get wrong: numbers, percentages,
money, dates and year spans, and capitalised multi-word names that look like
employers, products or credentials.

What this is NOT, stated plainly because a lint that is trusted too far is worse
than no lint at all:

  - It does not understand meaning. "grew revenue 40%" and "grew revenue by
    about two fifths" say the same thing; it can only see the first.
  - It cannot catch a false statement built entirely from true words, which is
    the most common way a resume overclaims.
  - A clean result is NOT a certification that the document is truthful. It means
    the specific token classes above were all found in the register.

Use it to catch the careless cases. Keep reading the draft."""
from __future__ import annotations

import re

# Things worth checking because a reader can verify them and being wrong is
# costly. Deliberately narrow: broad matching produced so many hits that the
# real ones were invisible.
_NUM = re.compile(r"(?<![\w.])(?:\$\s?[\d,]+(?:\.\d+)?[kKmMbB]?|\d+(?:\.\d+)?\s?%|"
                  r"\d{1,3}(?:,\d{3})+|\b\d{4}\b|\b\d+\+?\s?(?:years?|yrs?|months?)\b)")
# Two or more capitalised words in a row: employers, products, certifications.
_PROPER = re.compile(r"\b([A-Z][a-zA-Z0-9&.+-]*(?:\s+[A-Z][a-zA-Z0-9&.+-]*){1,4})\b")

# Sentence starts, headings and common phrasing produce capitalised runs that
# are not claims about the person.
_STOP = {
    "i", "a", "an", "the", "and", "or", "but", "for", "with", "this", "that",
    "my", "our", "their", "his", "her", "its", "we", "they", "you",
    "dear", "hiring", "manager", "team", "sincerely", "regards", "best",
    "thank", "thanks", "please", "resume", "cover", "letter", "summary",
    "experience", "education", "skills", "objective", "references",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def lint(draft: str, register: str, *, extra_allowed: str = "") -> list[dict]:
    """Return unbacked claims. Each is {kind, text, line, context}.

    `register` is claims.md (plus anything else you consider authoritative, such
    as the posting itself for the employer's own name)."""
    hay = _norm(register + "\n" + extra_allowed)
    hay_compact = hay.replace(" ", "")
    out: list[dict] = []
    seen: set[str] = set()

    for i, line in enumerate(draft.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith(("#", ">", "|")):
            continue
        for m in _NUM.finditer(line):
            tok = m.group(0).strip()
            key = f"n:{_norm(tok)}"
            if key in seen:
                continue
            # compare without spaces or punctuation: "$120,000" vs "120000",
            # "40 %" vs "40%"
            probe = re.sub(r"[^a-z0-9]", "", tok.lower())
            if probe and probe not in hay_compact:
                seen.add(key)
                out.append({"kind": "number", "text": tok, "line": i,
                            "context": line.strip()[:100]})
        for m in _PROPER.finditer(line):
            words = m.group(1).strip().split()
            # A sentence-initial capital and a bare "I" get swept into the run:
            # "At TechBridge I" was reported as an unknown employer even though
            # TechBridge is in the register. Trim the edges before judging.
            while words and words[0].lower() in _STOP:
                words.pop(0)
            while words and words[-1].lower() in _STOP:
                words.pop()
            if len(words) < 2:
                continue
            phrase = " ".join(words)
            key = f"p:{_norm(phrase)}"
            if key in seen:
                continue
            if _norm(phrase) not in hay:
                seen.add(key)
                out.append({"kind": "name", "text": phrase, "line": i,
                            "context": line.strip()[:100]})
    return out


def format_report(findings: list[dict], draft_name: str = "draft") -> str:
    if not findings:
        return (f"{draft_name}: no unbacked numbers or names found.\n"
                "This checks tokens, not meaning. It is not a certification that "
                "the document is accurate: read it.")
    lines = [f"{draft_name}: {len(findings)} item(s) not found in the claims register.",
             "Each is either something to add to claims.md, or something to cut.", ""]
    for f in findings:
        lines.append(f"  line {f['line']:>3}  [{f['kind']}] {f['text']}")
        lines.append(f"            {f['context']}")
    lines += ["", "Tokens only. A false statement made of true words will pass this."]
    return "\n".join(lines)
