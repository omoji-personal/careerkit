"""Normalized posting record shared by every adapter."""
from __future__ import annotations

import hashlib
import html
import ipaddress
import re
import socket
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

# Sources that are an employer's OWN applicant tracking system. Their
# external_id is the employer's requisition id: stable across runs, and it
# distinguishes two genuinely different openings. Aggregator feeds are excluded
# on purpose, because each mints its own id for the same underlying role.
ATS_SOURCES = frozenset({
    "greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee",
    "bamboohr", "rippling", "teamtailor", "workday", "oracle_orc", "eightfold",
    "phenom", "icims", "jobvite", "paylocity", "personio", "hrmdirect",
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
    # Where the displayed band came from: board (structured/text field), body
    # (parsed from posting prose), absent, or unknown for a pre-migration row.
    # A number without this distinction overstates the confidence of a parsed
    # figure and makes an employer-published band indistinguishable from one the
    # engine inferred.
    comp_source: str = ""
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
    # The registry's lane, kept separate because `lane` does not survive scoring:
    # score() overwrites it with the lane key the title matched. lane_title_context
    # is keyed on the REGISTRY lane, so once `lane` was overwritten the context was
    # unrecoverable and rescore re-judged those postings without it.
    registry_lane: str = ""
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
        return self._identity_basis(_norm_title(self.title))

    def _identity_basis(self, normalized_title: str, *, legacy_blank: bool = False) -> str:
        company = _norm_company(self.company)
        basis = f"{company}|{normalized_title}"
        if not company and self.source not in ATS_SOURCES and not legacy_blank:
            basis += f"|anonymous:{_blank_company_discriminator(self)}"
        return basis

    @property
    def group_key(self) -> str:
        """Company + normalized title. The same role arrives via the employer
        ATS, two aggregators and a repost; this is what makes them ONE entry in
        the report instead of four.

        This stays byte-identical to the pre-2026-08-05 uid for named employers,
        which is what lets an existing database be migrated in place. A
        non-ATS posting whose company is blank cannot safely group on title
        alone: source + URL disambiguate it until the feed provides an employer."""
        return hashlib.sha256(self._basis.encode()).hexdigest()[:20]

    @property
    def uid(self) -> str:
        """One OPENING. group_key plus the board's own requisition id when the
        sighting came from the employer's own ATS.

        group_key alone was the key until 2026-08-05. It silently merged
        genuinely distinct requisitions: a large employer running two "Product
        Manager" reqs in different cities produced ONE row, the second req's URL
        and location were discarded, and marking the first 'applied' removed
        every sibling from the report permanently. Named-company aggregator
        sightings keep the bare group_key because each aggregator mints its own
        id, so they still collapse onto one another as before. Anonymous
        aggregator rows retain the source + URL discriminator described by
        ``group_key`` so unrelated blank-company postings cannot collapse.

        Built from the raw basis string, NOT from the group_key hash: hashing a
        hash would make an aggregator's uid differ from its group_key, breaking
        both the "aggregators still collapse" property and in-place migration
        of an existing database."""
        basis = self._identity_basis(_norm_title_strict(self.title))
        if self.external_id and self.source in ATS_SOURCES:
            basis = f"{basis}|{self.external_id}"
        return hashlib.sha256(basis.encode()).hexdigest()[:20]

    @property
    def legacy_blank_company_group_key(self) -> str:
        """Pre-disambiguation group key, for exact source+URL adoption only."""
        if _norm_company(self.company) or self.source in ATS_SOURCES:
            return ""
        basis = self._identity_basis(_norm_title(self.title), legacy_blank=True)
        return hashlib.sha256(basis.encode()).hexdigest()[:20]

    @property
    def legacy_blank_company_uid(self) -> str:
        """Pre-disambiguation UID, for preserving history on the first re-sighting."""
        if _norm_company(self.company) or self.source in ATS_SOURCES:
            return ""
        basis = self._identity_basis(_norm_title_strict(self.title), legacy_blank=True)
        return hashlib.sha256(basis.encode()).hexdigest()[:20]

    @property
    def legacy_external_uid(self) -> str:
        """UID emitted by the short-lived external-ID scheme before strict
        title identity preserved meaningful parenthesized text.

        Kept only for in-place adoption of rows written by that build. It uses
        the board requisition id for every source because aggregators were also
        briefly affected during the transition.
        """
        basis = self._basis
        if self.external_id:
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
_MD_INLINE = str.maketrans({
    "\\": "&#92;",
    "`": "&#96;",
    "*": "&#42;",
    "_": "&#95;",
    "~": "&#126;",
    "[": "&#91;",
    "]": "&#93;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
})
_LOCAL_HOST_SUFFIXES = (
    "localhost", "local", "localdomain", "internal", "intranet", "lan",
    "home", "home.arpa", "corp", "private",
)
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")


def _without_format_controls(text: str) -> str:
    """Remove invisible Unicode formatting controls from untrusted display text."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _neutralize_block_lead(match: re.Match) -> str:
    """Entity-encode the character that gives a Markdown block its meaning."""
    value = match.group(0)
    for i, ch in enumerate(value):
        if ch.isspace():
            continue
        if ch.isdigit():
            dot = value.find(".", i)
            return value[:dot] + "&#46;" + value[dot + 1:]
        return value[:i] + f"&#{ord(ch)};" + value[i + 1:]
    return value


def _public_link_literal(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Reject local addresses, including local IPv4 embedded in IPv6."""
    def globally_routable(item):
        return item.is_global and not any((
            item.is_private,
            item.is_loopback,
            item.is_link_local,
            item.is_multicast,
            item.is_reserved,
            item.is_unspecified,
            getattr(item, "is_site_local", False),
        ))

    if not globally_routable(address):
        return False
    embedded: list[ipaddress.IPv4Address] = []
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            embedded.append(address.ipv4_mapped)
        if address.sixtofour:
            embedded.append(address.sixtofour)
        if address.teredo:
            embedded.extend(address.teredo)
        if address in _IPV4_COMPATIBLE or address in _NAT64_WELL_KNOWN:
            embedded.append(ipaddress.IPv4Address(int(address) & 0xffffffff))
    return all(globally_routable(inner) for inner in embedded)


def sanitize_external(text: str, limit: int = 0) -> str:
    """Make text that came from a job board safe to render into a report.

    Posting text is written into a Markdown file the agent later reads back, so
    a posting can forge headings, lists, links and code fences in our own
    output, and can hide instructions from a human reader using zero-width
    characters while leaving them perfectly visible to a model.

    This reduces blast radius. It is NOT a solution to prompt injection, and the
    contract in CLAUDE.md still governs what the agent may act on."""
    # These report fields are values, not free-form Markdown documents. Flatten
    # line breaks so a title/reason cannot escape its list item and start a new
    # heading, code block, HTML block or instruction section.
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _CTRL_RE.sub(" ", t)
    for z in _ZERO_WIDTH:
        t = t.replace(z, "")
    t = _without_format_controls(t)
    t = " ".join(t.split())
    if limit:
        t = t[:limit]
    t = _MD_LEAD.sub(_neutralize_block_lead, t)
    if re.fullmatch(r"\s*(?:[-*_]\s*){3,}", t):
        i = next((i for i, ch in enumerate(t) if not ch.isspace()), 0)
        t = t[:i] + f"&#{ord(t[i])};" + t[i + 1:]
    # Raw HTML, Markdown links/images, inline code/emphasis, and escape
    # characters are neutralized with entities. They render as the same human
    # text, but the Markdown parser never sees an active delimiter.
    return t.translate(_MD_INLINE)


def sanitize_external_url(url: str, limit: int = 2048) -> str:
    """Return a Markdown-safe public link target, or ``""`` when invalid.

    This is a rendering policy, not a network authorization check: it accepts
    only ordinary credential-free HTTP(S) URLs and percent-encodes delimiters
    that could terminate a Markdown link. Network callers must additionally use
    :func:`engine.http.validate_public_url` to resolve and reject local targets.
    """
    if not isinstance(url, str):
        return ""
    raw = url
    if not raw or raw != raw.strip() or len(raw) > limit or "\\" in raw:
        return ""
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7f for ch in raw):
        return ""
    if any(unicodedata.category(ch) == "Cf" for ch in raw):
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""

    host = parsed.hostname.rstrip(".")
    if not host:
        return ""
    literal = None
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return ""
        # IDNA maps several Unicode full-stop characters to '.', so re-check
        # after normalisation; 127。0。0。1 must not become a clickable loopback.
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None

    host_lower = host.lower()
    if any(host_lower == suffix or host_lower.endswith("." + suffix)
           for suffix in _LOCAL_HOST_SUFFIXES):
        return ""
    if literal is not None:
        # Do not produce clickable links to loopback, LAN, link-local or other
        # special-use literal addresses. DNS resolution remains http.py's job.
        if not _public_link_literal(literal):
            return ""
        host = f"[{literal.compressed}]" if literal.version == 6 else str(literal)
    else:
        # Browsers and libc still accept historical one-, two-, and three-part
        # IPv4 as well as octal/hex spellings (for example 2130706433 or
        # 0177.0.0.1). Reject rather than render those ambiguous forms.
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            return ""
        if "." not in host:
            return ""  # DNS search paths can turn a single label into a LAN host
        if (len(host) > 253 or not re.fullmatch(r"[a-z0-9.-]+", host)
                or any(not label or label.startswith("-") or label.endswith("-")
                       for label in host.split("."))):
            return ""
    netloc = host + (f":{port}" if port is not None else "")

    # Parentheses can close a Markdown link destination; brackets, angle
    # brackets, quotes, backticks and whitespace are encoded/rejected above or
    # by quote's conservative safe sets. Existing percent escapes stay intact.
    path = quote(parsed.path, safe="/%:@!$&'+,;=-._~")
    query = quote(parsed.query, safe="%&=/:?@!$'+,;-._~")
    fragment = quote(parsed.fragment, safe="%&=/:?@!$'+,;-._~")
    return urlunsplit((scheme, netloc, path, query, fragment))


def _identity_url(value: str) -> str:
    """Conservative URL normalization for stable anonymous-posting identity."""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return raw.split("#", 1)[0]
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return raw.split("#", 1)[0]
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            return raw.split("#", 1)[0]
    else:
        hostname = f"[{literal.compressed}]" if literal.version == 6 else str(literal)
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    netloc = hostname + (f":{port}" if port is not None and port != default_port else "")
    return urlunsplit((
        parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "",
    ))


def _blank_company_discriminator(job: Job) -> str:
    """Stable source/locator token for a posting that has no employer identity."""
    source = (job.source or "").strip().lower()
    locator = _identity_url(job.url) or (job.external_id or "").strip()
    material = f"{len(source)}:{source}|{len(locator)}:{locator}"
    return hashlib.sha256(material.encode()).hexdigest()[:20]


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
