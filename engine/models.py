"""Normalized posting record shared by every adapter."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field, asdict
from typing import Any

# Sources that are an employer's OWN applicant tracking system. Their
# external_id is the employer's requisition id: stable across runs, and it
# distinguishes two genuinely different openings. Aggregator feeds are excluded
# on purpose, because each mints its own id for the same underlying role.
ATS_SOURCES = frozenset({
    "greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee",
    "bamboohr", "rippling", "teamtailor", "workday", "oracle_orc", "eightfold",
    "phenom", "icims", "jobvite", "paylocity", "personio",
})

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    # Unescape BEFORE stripping. Greenhouse ships `content` HTML-escaped
    # (&lt;p&gt;...), so tag-stripping the raw payload sees no tags, and the
    # later &lt;-> unescape resurrected them as literal text - scorer regexes
    # were matching across "<strong>" fragments (caught 2026-08-03). Two
    # passes cover the occasional double-escaped board.
    s = html.unescape(html.unescape(s))
    s = s.replace("</p>", "\n").replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = s.replace("</li>", "\n").replace("<li>", " - ")
    s = _TAG_RE.sub(" ", s)
    s = s.replace("\xa0", " ")
    s = _WS_RE.sub(" ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


@dataclass
class Job:
    """One posting, normalized. `uid` is the stable dedupe key."""

    company: str
    title: str
    url: str
    source: str                      # adapter name, e.g. "greenhouse"
    location: str = ""
    description: str = ""
    posted_at: str = ""              # ISO date if the board gives one
    department: str = ""
    remote_flag: bool | None = None   # board's own claim, if any
    comp_min: int | None = None
    comp_max: int | None = None
    comp_text: str = ""
    external_id: str = ""
    # Evidence, never identity. JobSpy already resolves the employer's own apply
    # URL and corporate website out of an aggregator row and CareerKit was
    # discarding both. They are what tells a real posting from a listing that
    # exists only on scraper sites, which is how a consultancy that did not
    # exist reached the top of a report with the highest band on the board.
    # Deliberately NOT folded into uid or source: relabelling a scraped row as
    # an ATS row would launder its provenance and break retirement.
    url_direct: str = ""
    company_site: str = ""
    lane: str = ""                   # which employer lane this company sits in
    rails_exempt: bool = False       # owner carve-out: judge on fit, not mechanical rails
    employer_tier: str = ""          # A / B / C from the registry
    board: str = ""                  # stable board id, e.g. 'greenhouse:acme'
    raw: dict[str, Any] = field(default_factory=dict)

    # populated by the scorer
    score: int = 0
    gate: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def _basis(self) -> str:
        return f"{_norm_company(self.company)}|{_norm_title(self.title)}"

    @property
    def group_key(self) -> str:
        """Company + normalized title. The same role arrives via the employer
        ATS, two aggregators and a repost; this is what makes them ONE entry in
        the report instead of four.

        This is also byte-identical to the pre-2026-08-05 uid, which is what
        lets an existing database be migrated in place."""
        return hashlib.sha256(self._basis.encode()).hexdigest()[:20]

    @property
    def uid(self) -> str:
        """One OPENING. group_key plus the board's own requisition id when the
        sighting came from the employer's own ATS.

        group_key alone was the key until 2026-08-05. It silently merged
        genuinely distinct requisitions: a large employer running two "Product
        Manager" reqs in different cities produced ONE row, the second req's URL
        and location were discarded, and marking the first 'applied' removed
        every sibling from the report permanently. Aggregator sightings keep the
        bare group_key because each aggregator mints its own id, so they still
        collapse onto one another as before.

        Built from the raw basis string, NOT from the group_key hash: hashing a
        hash would make an aggregator's uid differ from its group_key, breaking
        both the "aggregators still collapse" property and in-place migration
        of an existing database."""
        basis = f"{_norm_company(self.company)}|{_norm_title_strict(self.title)}"
        if self.external_id and self.source in ATS_SOURCES:
            basis = f"{basis}|{self.external_id}"
        return hashlib.sha256(basis.encode()).hexdigest()[:20]

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.location}\n{self.department}\n{self.description}"

    def to_row(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        d["reasons"] = " | ".join(self.reasons)
        d["uid"] = self.uid
        return d


# Corporate suffixes and decoration that differ between an employer's own ATS
# and the aggregators reporting the same employer. Without this "Acme",
# "Acme Inc." and "ACME, Inc" were three different group_keys, so one role
# appeared three times, sightings could not aggregate, and marking one applied
# left the others on screen.
_CO_SUFFIX = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "gmbh", "plc", "sa", "nv", "bv", "ag", "holding", "holdings", "group",
    "technologies", "technology", "labs", "the",
}


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH = ("​", "‌", "‍", "﻿")
_MD_LEAD = re.compile(r"^\s{0,3}(#{1,6}\s|>|[-*+]\s|\d+\.\s)", re.M)


def sanitize_external(text: str, limit: int = 0) -> str:
    """Make text that came from a job board safe to render into a report.

    Posting text is written into a Markdown file the agent later reads back, so
    a posting can forge headings, lists, links and code fences in our own
    output, and can hide instructions from a human reader using zero-width
    characters while leaving them perfectly visible to a model.

    This reduces blast radius. It is NOT a solution to prompt injection, and the
    contract in CLAUDE.md still governs what the agent may act on."""
    t = _CTRL_RE.sub(" ", text or "")
    for z in _ZERO_WIDTH:
        t = t.replace(z, "")
    t = _MD_LEAD.sub(lambda m: "\\" + m.group(0).lstrip(), t)
    t = t.replace("`" * 3, "'" * 3)
    return t[:limit] if limit else t


def _norm_company(c: str) -> str:
    c = (c or "").lower()
    c = re.sub(r"[^a-z0-9 ]", " ", c)
    words = [w for w in c.split() if w and w not in _CO_SUFFIX]
    return " ".join(words) or c.strip()


def _norm_title(t: str) -> str:
    """Loose normalisation, used for GROUPING sibling sightings.

    Deliberately still drops parenthesised content, for two reasons. It keeps
    group_key byte-identical to the pre-2026-08-05 uid, which is what lets an
    existing database migrate in place without stranding applied status. And
    grouping is presentation: a role listed as "Product Owner (Salesforce)" on
    one board and "Product Owner" on another should appear as one entry with two
    sightings, not twice.

    Identity is a different question and uses _norm_title_strict below.
    """
    t = t.lower()
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\b(remote|us|usa|united states|hybrid|onsite|full time|fulltime|ft)\b", " ", t)
    return _WS_RE.sub(" ", t).strip()

def _norm_title_strict(t: str) -> str:
    """Strict normalisation, used for IDENTITY.

    Anything discarded here is a distinction the tool can never see again.
    Dropping parenthesised content did exactly that: "Success Architect
    (Agentforce)" and "Success Architect (Data Cloud)" produced one uid, so two
    genuinely different requisitions became one row and marking either applied
    hid the other permanently. That is the same failure the duplicate-title
    collapse was fixed for, surviving in a form nobody had tested.

    Brackets are punctuation and go; the words inside them stay. The noise list
    still removes what really is decoration, so "(Remote)" and "(US)" collapse
    while "(Agentforce)" does not.
    """
    t = t.lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\b(remote|us|usa|united states|hybrid|onsite|full time|fulltime|ft)\b", " ", t)
    return _WS_RE.sub(" ", t).strip()

