"""Fetch the posting the feed only summarised, and find its requirements.

Aggregator rows carry a title, a company, a location and often a paragraph. The
part that decides whether you can actually do the job lives in a Qualifications
block that the row frequently does not include. Four roles were recommended on
their titles in a single day and every one died in that block:

  KIPP            required Application Architect plus Platform Developer II
  Optomi          was Microsoft Dynamics 365, with Salesforce nowhere in it
  Nelson Mullins  required Platform Developer II and expert Apex
  Axiom           required ten years plus direct reports

In a real database, 62 of 170 live rows had no requirements section stored at
all. The rails cannot screen text that is not there, so they passed, and the
postings presented as clean matches.

This does two separate jobs, and keeping them separate matters:

  1. `fetch_canonical` gets the full posting text for a row that lacks it.
  2. `hard_requirements` reads a requirements section and reports gates the user
     cannot clear.

The second works on any description, whether or not the first ran. A row that
already has its requirements does not need a network request.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from .http import UnsafeExternalURL, fetch

# Headings that introduce what a candidate must have, as opposed to what they
# will do. The distinction is the whole point: "you will design Apex triggers"
# is a responsibility, "5+ years writing Apex" is a gate.
_REQ_HEADING = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?\s*(?:"
    r"(?:minimum|basic|required|preferred|desired|key)\s+)?"
    r"(?:qualifications?|requirements?|what you(?:'| a)?ll (?:need|bring)|"
    r"what we(?:'| a)?re looking for|who you are|about you|skills? (?:and|&) "
    r"experience|experience required|you have|must have)"
    r"\s*:?\s*(?:\*\*)?\s*$", re.I | re.M)

_NONREQ_HEADING = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?\s*(?:"
    r"responsibilities|what you(?:'| wi)?ll do|the role|about (?:us|the (?:role|team|company))|"
    r"benefits?|perks?|compensation|equal opportunity|our (?:team|company|mission)|"
    r"why (?:join|work)|day (?:in the life|to day)"
    r")\s*:?\s*(?:\*\*)?\s*$", re.I | re.M)


def has_requirements(description: str) -> bool:
    """Does this text contain a section stating what the candidate must have?"""
    return bool(_REQ_HEADING.search(description or ""))


def requirements_section(description: str) -> str:
    """The requirements text, or the whole description when no heading is found.

    Falling back to the whole description is deliberate. A posting that states
    its gates in prose, with no heading, still states them, and returning
    nothing would silently disable every check built on this.
    """
    text = description or ""
    m = _REQ_HEADING.search(text)
    if not m:
        return text
    start = m.end()
    nxt = _NONREQ_HEADING.search(text, start)
    return text[start:nxt.start()] if nxt else text[start:]


def _from_jsonld(raw: str) -> str:
    """The posting's own description, from schema.org JobPosting markup.

    Far better than stripping a page. Most boards emit this block so search
    engines can index the role, and it contains the description and nothing
    else. Stripping the surrounding HTML instead produced 20,000 characters of
    navigation, cookie banner and footer for a 5,000 character posting, and
    writing that over a good description made the row less screenable, not more.
    """
    import json as _json
    for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            raw, re.S | re.I):
        try:
            data = _json.loads(m.group(1).strip())
        except Exception:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            if "JobPosting" in str(node.get("@type", "")):
                desc = node.get("description") or ""
                if desc:
                    return _strip_html(desc)
    return ""


def _strip_html(raw: str) -> str:
    s = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", raw)
    s = re.sub(r"<li[^>]*>", "  - ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


_LINKEDIN_ID = re.compile(r"/jobs/view/(?:[^/]*-)?(\d{6,})(?:/|$)", re.I)


def fetch_canonical(url: str) -> tuple[str, str]:
    """Return (text, source) for a posting URL, or ("", reason) on failure.

    Only LinkedIn is special-cased, because its guest endpoint returns the full
    description without authentication and a large share of thin rows come from
    there. Everything else is fetched as-is and stripped, which works for the
    ATS boards that render server-side and fails harmlessly for the ones that do
    not: an empty result leaves the row exactly as it was.
    """
    try:
        parsed = urlsplit(url or "")
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        hostname, parsed = "", None
    m = (_LINKEDIN_ID.search(parsed.path) if parsed is not None
         and (hostname == "linkedin.com" or hostname.endswith(".linkedin.com"))
         else None)
    if m:
        api = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}"
        try:
            st, raw = fetch(api, tries=2, safe_external=True)
        except UnsafeExternalURL as exc:
            return "", f"refused unsafe URL: {exc}"
        if st == 200 and raw:
            block = re.search(r"description__text.*?</section>", raw, re.S)
            if block:
                text = _strip_html(block.group(0))
                if len(text) > 300:
                    return text, "linkedin-guest"
            return "", "linkedin returned no description block"
        return "", f"linkedin guest endpoint returned {st}"

    try:
        st, raw = fetch(url, tries=2, safe_external=True)
    except UnsafeExternalURL as exc:
        return "", f"refused unsafe URL: {exc}"
    if st != 200 or not raw:
        return "", f"fetch returned {st}"
    text = _from_jsonld(raw)
    if len(text) > 500:
        return text, "schema.org JobPosting"
    # Deliberately no HTML-stripping fallback. It returns the whole page, which
    # is mostly chrome, and a longer string that is mostly navigation is worse
    # than the summary it would replace.
    return "", "no JobPosting markup on the page"


def hard_requirements(description: str, patterns: dict) -> list[tuple[str, str]]:
    """Gates the candidate cannot clear, found in the requirements section.

    `patterns` maps a label to a compiled regex, from the user's profile. Returns
    (label, quoted sentence) so the report can show WHY rather than only that
    something failed, which is the difference between a decision and a shrug.

    Only the requirements section is searched. "You will collaborate with our
    architects" in a responsibilities block is not a demand that you be one, and
    matching it there is how a good posting gets discarded.
    """
    section = requirements_section(description)
    if not section:
        return []
    out = []
    for label, pat in (patterns or {}).items():
        m = pat.search(section)
        if not m:
            continue
        start = max(section.rfind(".", 0, m.start()) + 1,
                    section.rfind("\n", 0, m.start()) + 1)
        end = section.find("\n", m.end())
        end = len(section) if end == -1 else end
        sentence = " ".join(section[start:end].split())[:180]
        # A posting can name a gate in order to say it is optional.
        if re.search(r"\b(preferred|nice[ -]to[ -]have|a plus|bonus|not required|"
                     r"desirable|ideally)\b", sentence, re.I):
            continue
        out.append((label, sentence))
    return out


def enrich_row(row) -> dict:
    """Decide whether a stored row needs its canonical posting fetched."""
    desc = row["description"] or ""
    if has_requirements(desc):
        return {"needed": False, "why": "already has a requirements section"}
    if len(desc) > 6000:
        return {"needed": False, "why": "long description, requirements likely in prose"}
    return {"needed": True, "why": f"{len(desc)} chars and no requirements section",
            "url": row["url"]}
