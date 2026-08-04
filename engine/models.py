"""Normalized posting record shared by every adapter."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field, asdict
from typing import Any

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
    lane: str = ""                   # which employer lane this company sits in
    rails_exempt: bool = False       # owner carve-out: judge on fit, not mechanical rails
    employer_tier: str = ""          # A / B / C from the registry
    raw: dict[str, Any] = field(default_factory=dict)

    # populated by the scorer
    score: int = 0
    gate: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        """Company + normalized title only - deliberately NOT the URL or the
        board's id. The same role arrives via the employer ATS, two aggregators
        and a repost, each with its own id; keying on those means seeing it
        four times and marking it 'applied' only once. Each sighting is still
        recorded separately in the sightings table, so provenance survives."""
        basis = f"{self.company.lower().strip()}|{_norm_title(self.title)}"
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


def _norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\b(remote|us|usa|united states|hybrid|onsite|full time|fulltime|ft)\b", " ", t)
    return _WS_RE.sub(" ", t).strip()
