"""Profile-driven rail screening and scoring.

Every gate reads the user's profile (profile/profile.yaml) - there is exactly
one source of truth for a person's rules and this engine enforces it. The
universal patterns below (location idioms, comp parsing, clearance language)
are battle-tested and shared; everything person-specific is compiled from the
profile at load time.

Gate states, most severe first:
  EXCLUDED     - fails a hard rail. Never surfaces.
  SLOT-BLOCKED - passes rails but the role family is one the user rules out.
  VERIFY       - looks right but a rail could not be evidenced from the text.
  QUALIFIED    - passes every screenable rail with evidence.

Deliberate omission carried over from the parent tool: constraints that are
undiscoverable from posting text (e.g. tolerance for a long annual absence)
are NOT screened - filtering on them only discards real matches. They live in
the profile and move to the human-screening stage.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

from .models import Job, _norm_company

# --------------------------------------------------------------------------
# Universal patterns (person-independent, keep in sync with lessons learned)
# --------------------------------------------------------------------------

REMOTE_OK = re.compile(
    r"\b(fully remote|100% remote|remote.?first|work from anywhere in the u\.?s|"
    r"remote \(?(us|usa|united states|anywhere in the us)\)?|us.?remote|remote - us|"
    r"telecommute|distributed team)\b", re.I)
# "Anywhere" in a location or title is the same claim as "remote".
REMOTE_WORD = re.compile(r"\b(remote|anywhere)\b", re.I)
# A location field naming only the country - can never mean a specific office.
BARE_US_LOC = re.compile(
    r"^\s*(?:remote\s*[-,(]?\s*)?(?:united states(?: of america)?|usa?|"
    r"u\.s\.a?\.?|north america)(?:\s*[-,(]?\s*(?:remote|only)\)?)?\s*$", re.I)
NON_US = re.compile(
    r"\b(india|bangalore|bengaluru|hyderabad|pune|noida|gurgaon|chennai|mumbai|"
    r"canada|toronto|vancouver|ontario|montreal|mexico|guadalajara|brazil|sao paulo|"
    r"united kingdom|london|ireland|dublin|germany|munich|berlin|hamburg|cologne|"
    r"france|paris|spain|madrid|barcelona|italy|milan|rome|netherlands|amsterdam|"
    r"poland|warsaw|krakow|australia|sydney|melbourne|singapore|japan|tokyo|"
    r"philippines|manila|argentina|colombia|costa rica|chile|peru|uruguay|"
    r"emea|apac|latam|anz|uae|united arab emirates|dubai|abu dhabi|qatar|saudi|israel|tel aviv|"
    r"turkey|istanbul|egypt|cairo|south africa|nigeria|lagos|kenya|nairobi|"
    r"pakistan|karachi|lahore|bangladesh|dhaka|vietnam|hanoi|indonesia|jakarta|"
    r"thailand|bangkok|malaysia|kuala lumpur|china|shanghai|beijing|shenzhen|"
    r"korea|seoul|taiwan|taipei|hong kong|new zealand|auckland|wellington|"
    r"switzerland|zurich|geneva|austria|vienna|sweden|stockholm|norway|oslo|"
    r"denmark|copenhagen|finland|helsinki|belgium|brussels|portugal|lisbon|"
    r"greece|athens|czech|prague|hungary|budapest|romania|bucharest|ukraine|"
    r"kyiv|uk|serbia|belgrade|bulgaria|sofia|croatia|estonia|latvia|lithuania|belarus|minsk|"
    r"kazakhstan|armenia|georgia \(country\)|moldova|slovakia|slovenia|bratislava|"
    r"gmbh|s\.r\.l|b\.v\.|pty ltd|sarl|\(all genders\))\b", re.I)

_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    "florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|"
    "maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|"
    "nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|"
    "north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
    "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|"
    "wisconsin|wyoming|district of columbia"
)
US_EVIDENCE = re.compile(
    r"\b(united states|u\.?s\.?a\.?|us[- ]based|usa?[- ]remote|remote[ -]+u\.?s|"
    r"anywhere in the u\.?s|nationwide|" + _US_STATES + r")\b", re.I)
# CASE-SENSITIVE on purpose, and deliberately separate from US_EVIDENCE above.
# Two things depend on the distinction:
#   - "US" alone never matched, because `u\.?s\.?a\.?` requires the "a". Every
#     posting labelled "Remote (US)" - one of the commonest forms there is -
#     therefore had no US evidence and fell to VERIFY instead of QUALIFIED.
#   - The two-letter state codes must NOT be case-insensitive. Lowercased,
#     ", ca" is Canada as readily as California, and folding them into the
#     case-insensitive pattern made a Toronto posting look US-based.
# Real listings write country and state tokens in caps; English prose does not.
US_TOKEN = re.compile(
    r"\b(USA|U\.S\.|U\.S\.A\.)\b|"
    r"\((?:US|USA)\)|\bUS[- ]remote\b|\bremote[ -]+US\b|"
    r",\s?(AL|AK|AZ|CA|CT|FL|GA|HI|IA|KS|KY|LA|ME|MD|MI|MN|"
    r"MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|"
    r"WV|WI|WY|DC)\b")
# Deliberately ABSENT from US_TOKEN: AR, CO, DE, ID, IL, IN, MA. Each is a US
# state abbreviation AND an ISO country code, and real boards write "Munich, DE"
# and "Bangalore, IN" in exactly the same caps as "Denver, CO". Treating those
# as US evidence suppressed the NON_US rail and presented German and Indian
# postings as US offices. They are resolved instead by AMBIGUOUS_CC below, which
# only applies AFTER the NON_US pass has failed to find a foreign city or
# country - by which point ", CO" really is Colorado.
# A bare "US" is likewise excluded: it appears in prose ("join US"), and an
# all-caps title would otherwise manufacture US evidence for a London role.
# The parenthesised and hyphenated forms above cover the real location idioms.
# Trailing lowercase ISO country codes; CASE-SENSITIVE on purpose ("Remote, in"
# is India, "Indianapolis, IN" is not).
NON_US_CC = re.compile(
    r",\s*(cn|pl|de|in|mx|br|ar|co|cl|pe|uy|ca|gb|uk|ie|fr|es|it|nl|be|pt|gr|"
    r"cz|hu|ro|ua|rs|bg|hr|ee|lv|lt|at|ch|se|no|dk|fi|au|nz|sg|jp|kr|tw|hk|"
    r"my|th|vn|id|ph|pk|bd|ae|sa|qa|il|tr|eg|za|ng|ke|ma)\b(?!\w)")
# Country codes that are ALSO US state abbreviations. Reaching the country-code
# check means the NON_US pass above found no foreign city or country name, so
# for these eight the US state reading is the right one: ", ca" after a city is
# California far more often than Canada, and a genuinely Canadian posting says
# Canada, Toronto, or Ontario, all of which NON_US catches first.
AMBIGUOUS_CC = frozenset({"ca", "co", "de", "in", "id", "il", "ma", "ar"})

# A location field that names no place. Workday boards routinely collapse a
# multi-city requisition to "3 Locations", and some emit "Multiple Locations" or
# "Various". That string is not evidence of a disqualifying location, it is the
# absence of evidence, and failing on it discarded 28 Salesforce reqs in one run,
# any of which could have listed the user's own metro among the hidden cities.
UNRESOLVED_LOC = re.compile(
    r"^\s*(?:\d+\s*locations?|multiple\s+locations?|various(?:\s+locations?)?|"
    r"several\s+locations?|see\s+(?:job\s+)?description|multiple\s+sites?)\s*$", re.I)

VAGUE_REMOTE = re.compile(
    r"\b(anywhere in the world|worldwide|global(ly)?|any location|"
    r"work from anywhere)\b", re.I)
US_STATE_LOCK = re.compile(
    r"\bmust (reside|live|be located) in\b|\bresidents? of\b.{0,40}\bonly\b", re.I)

CLEARANCE = re.compile(
    r"\(cleared\)|\b(active|current)\s+(ts/?\s?sci|top secret|secret)\b|"
    r"\b(secret|ts/?\s?sci|top.secret)\b.{0,15}\bclearance\b|"
    r"\bsecurity clearance\b.{0,40}\b(required|must)\b|"
    r"\bmust\b.{0,50}\bsecurity clearance\b|"
    r"\b(obtain|acquire|attain)\b.{0,60}\bclearance\b|"
    r"\bdoes not hold dual citizenship\b", re.I)

QUOTA = re.compile(
    r"\b(sales quota|carry a quota|quota.?carrying|revenue (target|quota|number)|"
    r"book of business|new logo|upsell target|cross.?sell target|"
    r"own(ing|s)? (the )?(renewal|expansion|revenue) (target|number|quota)|"
    r"achieve (annual |quarterly )?(sales|revenue) target|commission plan|"
    r"pipeline generation|prospecting|net new business)\b", re.I)

# Comp.
_MONEY = re.compile(r"\$\s?(\d{1,3})(?:,(\d{3}))?(?:\s?[kK])?(?:\.\d+)?")
_RANGE_CTX = re.compile(
    r"(base (salary|pay|compensation)|salary range|pay range|compensation range|"
    r"hiring range|target salary|annual (salary|base))", re.I)

# A listing can remain available through an aggregator after the employer's
# explicit deadline.  Parse only sentences that unmistakably introduce an
# application deadline; a loose date search would turn benefit dates, start
# dates, and copyright years into false closures.
_DEADLINE = re.compile(
    r"\b(?:apply(?:\s+no\s+later\s+than)?\s+by|application\s+deadline(?:\s+is)?|"
    r"posting\s+closes?(?:\s+on)?|position\s+closes?(?:\s+on)?|"
    r"accept(?:ing)?\s+(?:applications|applicants)\s+until|"
    r"applications?\s+(?:will\s+be\s+)?accepted\s+(?:until|through))\s*"
    r"[:\-]?\s*("
    r"[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*|\s+)\d{4}|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", re.I)


def posting_deadline(text: str, *, today: date | None = None) -> tuple[date, str] | None:
    """Return the earliest explicit application deadline and its evidence."""
    found = []
    for m in _DEADLINE.finditer(text or ""):
        raw = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", m.group(1), flags=re.I)
        parsed = None
        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"):
            try:
                parsed = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                pass
        if parsed:
            evidence = re.sub(r"\s+", " ", m.group(0)).strip()
            found.append((parsed, evidence))
    return min(found, key=lambda x: x[0]) if found else None


# --------------------------------------------------------------------------
# Profile: the person's rules, compiled once
# --------------------------------------------------------------------------

def _is_word_char(c: str) -> bool:
    return c.isalnum() or c == "_"


class ProfileError(ValueError):
    """A profile rule could not be compiled and silence would be unsafe."""


# The shape the loader actually reads. Anything the loader ignores is a key the
# user believes is doing work: `screen-floor` instead of `screen_floor` leaves
# the comp floor at 0, `metro` instead of `metros` turns off metro scoring, and
# both fail silently while producing a plausible-looking report. Values are the
# accepted Python types after YAML parsing.
PROFILE_SCHEMA: dict = {
    "comp": {"screen_floor": (int, float), "accept_floor": (int, float)},
    "location": {"remote_us": bool, "relocation": bool, "metros": list,
                 "home_metro": str, "states": list},
    "exclusions": {"titles": list, "titles_always": list, "products": list, "certs_refused": list,
                   "body_patterns": list, "competing_platforms": list,
                   "clearance": bool, "quota": bool, "companies": list},
    # Gates read from a posting's REQUIREMENTS section only, never from its
    # responsibilities. "You will work with our architects" is not a demand that
    # you be one, and matching it anywhere in the body is how a good posting gets
    # discarded. Each entry is a label mapped to a list of phrases.
    "hard_requirements": dict,
    "lanes": list, "dream_lanes": list, "dream_companies": list,
    "search_terms": list, "lane_title_context": dict, "signals": list,
    "domain_terms": list, "notes": str,
    # Read by the application and outreach skills rather than the scorer. They
    # belong in the schema anyway: a typo in `work_auth` is how a form answer
    # goes missing, and validating only the scorer's half would flag the
    # shipped template as full of unknown keys.
    "autonomy": {"submit": str, "outreach": str, "linkedin_edits": str,
                 "policy": str, "ask_each": bool, "auto_apply": bool},
    "identity": {"name": str, "email": str, "phone": str, "city": str,
                 "state": str, "linkedin": str, "website": str, "github": str},
    "work_auth": {"authorized_us": bool, "sponsorship": (bool, str), "visa": str},
    "eeo": {"gender": str, "race": str, "hispanic": str, "veteran": str,
            "disability": str, "policy": str},
}
_LANE_KEYS = {"key", "titles", "weight", "label", "note"}


def _closest(key: str, known) -> str:
    """Nearest known key by cheap edit distance, so the warning is actionable."""
    import difflib
    m = difflib.get_close_matches(key, list(known), n=1, cutoff=0.6)
    return f" (did you mean '{m[0]}'?)" if m else ""


def validate_profile(cfg: dict) -> list[str]:
    """Return human-readable problems with a parsed profile.

    Structural mistakes raise (a rule the engine cannot honour must never be
    silently skipped); unknown or misspelled keys are returned as warnings so a
    profile with one typo still runs."""
    warn: list[str] = []
    if not isinstance(cfg, dict):
        raise ProfileError("profile.yaml must be a mapping at the top level, "
                           f"got {type(cfg).__name__}.")
    for key, val in cfg.items():
        if key not in PROFILE_SCHEMA:
            warn.append(f"unknown key '{key}'{_closest(key, PROFILE_SCHEMA)} - ignored")
            continue
        spec = PROFILE_SCHEMA[key]
        if isinstance(spec, dict):
            if val is None:
                continue
            if not isinstance(val, dict):
                raise ProfileError(f"profile.yaml: '{key}' must be a mapping, "
                                   f"got {type(val).__name__}.")
            for sub, sval in val.items():
                if sub not in spec:
                    warn.append(f"unknown key '{key}.{sub}'{_closest(sub, spec)} - ignored")
                elif sval is not None and not isinstance(sval, spec[sub]):
                    raise ProfileError(
                        f"profile.yaml: '{key}.{sub}' must be "
                        f"{getattr(spec[sub], '__name__', 'one of ' + str(spec[sub]))}, "
                        f"got {type(sval).__name__} ({sval!r}).")
        elif val is not None and not isinstance(val, spec):
            raise ProfileError(f"profile.yaml: '{key}' must be {spec.__name__}, "
                               f"got {type(val).__name__}.")
    for section in ("lanes", "dream_lanes"):
        for i, lane in enumerate(cfg.get(section) or []):
            where = f"{section}[{i}]"
            if not isinstance(lane, dict):
                raise ProfileError(f"profile.yaml: {where} must be a mapping with "
                                   f"'titles', got {type(lane).__name__}.")
            if not lane.get("titles"):
                # A lane with no titles matches nothing, so the whole lane is
                # dead weight the user thinks is live.
                warn.append(f"{where} ('{lane.get('key', '?')}') has no titles - "
                            "it can never match a posting")
            for k in lane:
                if k not in _LANE_KEYS:
                    warn.append(f"unknown key '{where}.{k}'{_closest(k, _LANE_KEYS)} - ignored")
    comp = cfg.get("comp") or {}
    if comp.get("accept_floor") and comp.get("screen_floor") and \
            comp["accept_floor"] < comp["screen_floor"]:
        warn.append(f"comp.accept_floor ({comp['accept_floor']}) is below "
                    f"comp.screen_floor ({comp['screen_floor']}) - screening is "
                    "stricter than accepting, which is probably backwards")
    if not (cfg.get("lanes") or cfg.get("dream_lanes")):
        warn.append("no lanes defined - nothing can score above the title gate")
    return warn


# Strings no sane rule should match. A pattern that matches all of these is
# matching everything, which in an exclusion list means the user silently sees
# zero jobs. Catches "/.+/", "/ /", "/\\b/" and friends that the empty-string
# probe alone lets through.
_SENTINELS = ("Chief Financial Officer", "Warehouse Associate II", "zzq wumpus 4718")


def _compile_alt(terms: list[str] | None, window: str = "",
                 where: str = "profile", strict: bool = False) -> re.Pattern | None:
    """Compile a profile rule.

    strict=True is for EXCLUSION lists. Dropping an unusable term there fails
    OPEN: the rail disappears and everything the user banned starts surfacing.
    A typo'd regex in `exclusions.titles` used to crash loudly; silently
    dropping it moved the failure from actionable to invisible, so exclusions
    now raise instead.
    """
    """Alternation of literal terms or raw /regex/ entries.

    Boundaries are applied PER TERM and only on edges that are word characters.
    A single wrapping \\b(...)\\b is wrong in both directions and both failures
    are silent: a term whose edge is punctuation (".NET", "C++", "(remote)")
    can never match, while an empty alternative - from a stray "" in the YAML
    list, or a raw regex ending in "|" - matches EVERY posting, which in an
    exclusion list means the user silently sees zero jobs forever.
    """
    if not terms:
        return None
    parts, dropped = [], []
    for raw in terms:
        if raw is None:
            dropped.append("(null list item)")
            continue
        t = str(raw).strip()
        if not t:
            dropped.append("(empty string)")
            continue
        if t.startswith("/") and t.endswith("/") and len(t) > 2:
            body = t[1:-1]
            try:
                probe = re.compile(body)
            except re.error as e:
                dropped.append(f"{t} [bad regex: {e}]")
                continue
            # A negative lookahead is how someone writes an allow-list
            # ("exclude everything that is NOT X"). Such a pattern matches the
            # empty string and every sentinel BY DESIGN, so both guards below
            # would reject a perfectly valid rule. Someone writing "(?!" is
            # being deliberate; the accidents these guards exist to catch - a
            # stray "" or a regex ending in "|" - contain no lookahead.
            deliberate = "(?!" in body or "(?<!" in body
            if not deliberate and probe.match(""):
                dropped.append(f"{t} [matches the empty string - would match every posting]")
                continue
            if not deliberate and all(probe.search(x) for x in _SENTINELS):
                dropped.append(f"{t} [matches everything - would hide every posting]")
                continue
            parts.append(f"(?:{body})")
        else:
            lead = r"\b" if _is_word_char(t[0]) else ""
            trail = r"\b" if _is_word_char(t[-1]) else ""
            parts.append(lead + re.escape(t) + trail)
    if dropped:
        detail = "; ".join(dropped[:4]) + ("..." if len(dropped) > 4 else "")
        if strict:
            raise ProfileError(
                f"{where}: {len(dropped)} unusable term(s) in an EXCLUSION list: {detail}\n"
                f"Exclusions must not be silently dropped - the rule would stop applying "
                f"and everything you banned would start appearing. Fix them in "
                f"profile/profile.yaml and re-run.")
        print(f"  ! {where}: ignored {len(dropped)} unusable term(s): " + detail)
    if not parts:
        return None
    return re.compile("(" + "|".join(parts) + ")" + window, re.I)


@dataclass
class Profile:
    screen_floor: int = 0
    accept_floor: int = 0
    remote_ok: bool = True
    relocation: bool = False
    metros: list[str] = field(default_factory=list)
    metro_re: re.Pattern | None = None
    lanes: list[tuple[int, re.Pattern, str]] = field(default_factory=list)
    dream_lanes: list[tuple[int, re.Pattern, str]] = field(default_factory=list)
    slot_block: re.Pattern | None = None
    slot_block_always: re.Pattern | None = None
    products_block: re.Pattern | None = None
    certs_refused: re.Pattern | None = None
    hard_requirements: dict = field(default_factory=dict)
    body_blocks: list[tuple[re.Pattern, str]] = field(default_factory=list)
    competing: re.Pattern | None = None
    domain_terms: re.Pattern | None = None
    block_clearance: bool = True
    block_quota: bool = True
    dream_companies: set = field(default_factory=set)
    signals: list[tuple[re.Pattern, int, str]] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)
    relevance_terms: list[str] = field(default_factory=list)
    lane_title_context: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        cfg = yaml.safe_load(Path(path).read_text()) or {}
        for problem in validate_profile(cfg):
            print(f"  profile warning: {problem}", file=sys.stderr)
        comp = cfg.get("comp") or {}
        loc = cfg.get("location") or {}
        exc = cfg.get("exclusions") or {}
        p = cls(
            screen_floor=int(comp.get("screen_floor") or 0),
            accept_floor=int(comp.get("accept_floor") or comp.get("screen_floor") or 0),
            remote_ok=bool(loc.get("remote_us", True)),
            relocation=bool(loc.get("relocation", False)),
            metros=[str(m) for m in (loc.get("metros") or []) if m],
            block_clearance=bool(exc.get("clearance", True)),
            block_quota=bool(exc.get("quota", True)),
            dream_companies={c.lower() for c in (cfg.get("dream_companies") or [])},
            search_terms=list(cfg.get("search_terms") or []),
            lane_title_context=dict(cfg.get("lane_title_context") or {}),
        )
        p.metro_re = _compile_alt(p.metros) if p.metros else None
        # Raw lane titles, kept for the adapters' detail pre-filter. Compiled
        # patterns cannot be reused there: the pre-filter needs the terms
        # themselves so it can say what THIS user is looking for.
        for _l in (cfg.get("lanes") or []) + (cfg.get("dream_lanes") or []):
            p.relevance_terms += [str(t) for t in (_l.get("titles") or []) if t]
        p.relevance_terms += [str(t) for t in (cfg.get("search_terms") or []) if t]
        for lane in cfg.get("lanes") or []:
            pat = _compile_alt(lane.get("titles"))
            if pat:
                p.lanes.append((int(lane.get("weight", 35)), pat, lane.get("key", "?")))
        p.lanes.sort(key=lambda t: -t[0])
        for lane in cfg.get("dream_lanes") or []:
            pat = _compile_alt(lane.get("titles"))
            if pat:
                p.dream_lanes.append((int(lane.get("weight", 40)), pat, lane.get("key", "?")))
        p.dream_lanes.sort(key=lambda t: -t[0])
        p.slot_block = _compile_alt(exc.get("titles"), where="exclusions.titles", strict=True)
        # Role families the person cannot do, as opposed to ones they would
        # rather avoid. Never waived, not even for a dream employer, for the
        # same reason clearance is not: enthusiasm does not make someone a
        # software engineer. Waiving these was producing a list of roles at
        # exciting companies that the user had no real chance at.
        p.slot_block_always = _compile_alt(exc.get("titles_always"),
                                           where="exclusions.titles_always", strict=True)
        p.products_block = _compile_alt(exc.get("products"), where="exclusions.products", strict=True)
        # Blank entries in an EXCLUSION list were filtered out silently. An
        # exclusion that quietly compiles to nothing stops applying, and
        # everything it banned starts surfacing, which is the one failure the
        # docs promise cannot happen. Raise instead.
        certs_raw = exc.get("certs_refused") or []
        certs = [str(c).strip() for c in certs_raw
                 if c is not None and str(c).strip()]
        if certs_raw and len(certs) != len(certs_raw):
            raise ProfileError(
                "exclusions.certs_refused: blank or null entries in an EXCLUSION "
                "list. Remove them in profile/profile.yaml and re-run.")
        if certs:
            alt = "|".join(re.escape(c) for c in certs)
            p.certs_refused = re.compile(
                r"\b(" + alt + r")\b.{0,40}\b(required|must have|require|certification required)\b|"
                r"\b(required|must have)\b.{0,40}\b(" + alt + r")\b", re.I)
        # hard_requirements: label -> phrases the candidate cannot satisfy.
        # Same strict treatment: dropping one silently reopens a gate.
        hr_raw = cfg.get("hard_requirements") or {}
        if not isinstance(hr_raw, dict):
            raise ProfileError("hard_requirements must be a mapping of label to phrases")
        for label, phrases in hr_raw.items():
            kept = [str(x).strip() for x in (phrases or []) if x is not None and str(x).strip()]
            if phrases and len(kept) != len(phrases):
                raise ProfileError(
                    f"hard_requirements.{label}: blank or null entries in a gate "
                    f"list. Remove them in profile/profile.yaml and re-run.")
            if kept:
                pat = _compile_alt(kept, where=f"hard_requirements.{label}",
                                   strict=True)
                if pat is not None:
                    p.hard_requirements[str(label)] = pat

        # body_patterns is a hard kill rail (a match sets EXCLUDED), so it gets
        # the same strict treatment as the other exclusion lists. It did not,
        # which made the documented "an exclusion never fails open" guarantee
        # false for the one exclusion type the example profile demonstrates.
        for b in exc.get("body_patterns") or []:
            terms = [b] if isinstance(b, str) else (b.get("terms") or [])
            pat = _compile_alt(terms, where="exclusions.body_patterns", strict=True)
            if not pat:
                raise ProfileError(
                    f"exclusions.body_patterns: entry {b!r} has no usable terms. "
                    "An exclusion that compiles to nothing stops applying, and "
                    "everything it banned starts appearing. Fix it in "
                    "profile/profile.yaml and re-run.")
            p.body_blocks.append((pat, b if isinstance(b, str) else b.get("label", "blocked")))
        p.competing = _compile_alt(exc.get("competing_platforms"), where="exclusions.competing_platforms", strict=True)
        p.domain_terms = _compile_alt(cfg.get("domain_terms"))
        for s in cfg.get("signals") or []:
            pat = _compile_alt(s.get("terms"))
            if pat:
                p.signals.append((pat, int(s.get("points", 4)), s.get("label", "signal")))
        return p


def extract_comp(job: Job) -> tuple[int | None, int | None]:
    """Prefer the board's structured field; fall back to parsing the body near
    an explicit base-salary phrase. Multi-geo-band postings span min..max of
    ALL figures in the window; hourly bands normalize x2080."""
    if job.comp_min or job.comp_max:
        lo, hi = job.comp_min, job.comp_max
        # A low floor alone does not mean the board quoted an hourly rate. Boards
        # accept junk: an Indeed listing advertised "Pay: $1.00 - $250,000.00 per
        # year", and treating lo < 1000 as hourly multiplied BOTH ends by 2080,
        # reporting a band of $2,080 to $520,000,000. The top of the range settles
        # it - nobody is paid $250,000 an hour - so only annualise when the whole
        # band looks hourly, not just its floor.
        if lo and lo < 1000 and (hi is None or hi < 1000):
            lo, hi = lo * 2080, (hi * 2080 if hi else None)
        return lo, hi
    text = f"{job.comp_text}\n{job.description}"
    best: list[int] = []
    for m in _RANGE_CTX.finditer(text):
        window = text[m.start(): m.start() + 320]
        hourly = bool(re.search(r"\bper hour\b|/\s?h(?:ou)?r\b|\bhourly\b", window, re.I))
        weekly = bool(re.search(r"\bper week\b|/\s?w(?:ee)?k\b|\bweekly\b", window, re.I))
        monthly = bool(re.search(r"\bper month\b|/\s?mo(?:nth)?\b|\bmonthly\b", window, re.I))
        # A bare 2-3 digit figure is only shorthand for thousands when the window
        # says so. Without this a "$500 home office stipend" sitting next to a
        # salary line was read as $500,000 and inflated the band.
        thousands_ok = bool(re.search(r"\d\s?[kK]\b", window)) or bool(
            re.search(r"\$\s?\d{2,3},\d{3}", window))
        vals, explicit = [], []
        for mm in _MONEY.finditer(window):
            a, b = mm.group(1), mm.group(2)
            raw = int(a + b) if b else int(a)
            if hourly and raw < 500:
                v = raw * 2080
            elif weekly and raw < 20_000:
                v = raw * 52
            elif monthly and raw < 60_000:
                v = raw * 12
            elif b:
                v = raw
            elif thousands_ok:
                v = raw * 1000
            else:
                continue        # a bare small figure with no thousands signal
            if 40_000 <= v <= 1_500_000:   # 500k dropped real exec bands whole
                vals.append(v)
                if b or re.match(r"\$\s?\d{1,3}\s?[kK]", mm.group(0)):
                    explicit.append(v)      # states its own magnitude
        # A figure that states its own magnitude (a comma or a k suffix) beats a
        # bare one in the same window. Otherwise a "$500 home office stipend"
        # next to a real band was read as $500,000 and inflated the maximum.
        use = explicit if len(explicit) >= 2 else vals
        if len(use) >= 2:
            best = [min(use), max(use)]
            break
        if use and not best:
            best = use[:1]
    if not best:
        return None, None
    return (best[0], best[-1] if len(best) > 1 else None)


# A binding attendance requirement, not a passing mention of the word hybrid.
# Requires a commitment verb or a cadence, so "we have been hybrid since 2020"
# and "hybrid teams welcome" do not trip it, while "requires 3 days per week in
# the office" and "onsite 5x per week" do.
ONSITE_REQUIREMENT = re.compile(
    r"(?:\b(?:require[sd]?|must|expected|expectation)\b[^.]{0,80}?"
    r"\b(?:on.?site|in.?office|in the office|hybrid)\b)"
    r"|(?:\b(?:on.?site|in.?office|in the office|hybrid)\b[^.]{0,60}?"
    r"\b\d\s*(?:\+|x)?\s*(?:days?|times?)\b[^.]{0,30}?\b(?:per|a|each)\s*(?:week|month))"
    r"|(?:\b\d\s*(?:\+|x)?\s*(?:days?|times?)\s*(?:per|a|each)\s*(?:week|month)"
    r"[^.]{0,40}?\b(?:on.?site|in.?office|in the office))"
    r"|(?:\bon.?site\s*\d\s*x?\s*per\s*week\b)",
    re.I)


def _clause_before(text: str, m: "re.Match") -> str:
    """The part of the match's own sentence that comes before it.

    A fixed lookbehind cannot see requirement framing that opens a long bullet,
    and bullets in senior reqs are long. Sentence-bounded, so framing from the
    PREVIOUS bullet never leaks in."""
    start = max(text.rfind(".", 0, m.start()) + 1,
                text.rfind("\n", 0, m.start()) + 1,
                text.rfind("!", 0, m.start()) + 1,
                text.rfind("?", 0, m.start()) + 1,
                text.rfind(";", 0, m.start()) + 1)
    return " ".join(text[start:m.start()].split())


def _sentence_around(text: str, m: "re.Match") -> str:
    """Quote the sentence a rail fired on, not a 40-character slice of it.

    A reason reading "onsite 5x per w" is unusable for deciding whether the rail
    was right, which is the only thing the reason is for."""
    # The scored blob is title, location, department and description joined by
    # newlines, so a newline is as much a sentence boundary as a full stop.
    # Without that, a rail firing early in the description quoted the title and
    # location as part of "the sentence".
    start = max(text.rfind(".", 0, m.start()) + 1,
                text.rfind("\n", 0, m.start()) + 1,
                text.rfind("!", 0, m.start()) + 1,
                text.rfind("?", 0, m.start()) + 1)
    ends = [i for i in (text.find(".", m.end()), text.find("\n", m.end())) if i != -1]
    end = (min(ends) + 1) if ends else len(text)
    return " ".join(text[start:end].split())[:180]


# A rail fires on a phrase, but a posting can use the phrase to say the opposite.
# "This is not a quota-carrying role" and "No security clearance is required" both
# tripped the rail that exists to screen those very things out, and the failure is
# in the expensive direction: it silently discards a job the user wanted.
#
# Deliberately narrow. It only looks backwards inside the same sentence, and only
# for negators that actually reverse the phrase. Anything cleverer starts throwing
# away real exclusions, which is the worse error of the two.
_NEGATOR = re.compile(
    r"\b(?:not?|never|without|no longer|isn'?t|aren'?t|does not|do not|don'?t|"
    r"will not|won'?t|is not|are not|free from|exempt from|nor)\b", re.I)


def _negated(text: str, m: "re.Match", window: int = 90) -> bool:
    """Is this match inside a clause that negates it?

    Looks back to the start of the sentence, capped, and asks whether a negator
    sits between that boundary and the match. "You will own a sales quota" has
    none; "you will not carry a sales quota" does.
    """
    start = max(text.rfind(".", 0, m.start()) + 1,
                text.rfind("\n", 0, m.start()) + 1,
                m.start() - window)
    return bool(_NEGATOR.search(text[start:m.start()]))


def _rail_hit(pattern, text: str):
    """First match of `pattern` that the posting is not explicitly denying."""
    for m in pattern.finditer(text):
        if not _negated(text, m):
            return m
    return None


# Below this, a posting has no description worth screening. Feed rows routinely
# carry a title, a location and a stub, and the rails then pass on silence.
THIN_BODY = 200

# A requirement the posting itself downgrades. "3+ years with Marketing Cloud
# preferred, not required" tripped the product rail on the "3+ years" context
# alone, discarding a role the user could do.
_PREFERENCE = re.compile(
    r"\b(preferred|preferable|nice[ -]to[ -]have|a plus|bonus|desirable|"
    r"not required|but not required|advantageous|ideally|would be great)\b", re.I)


def location_verdict(job: Job, p: Profile) -> tuple[str, str]:
    """pass / fail / unknown. A board's own remote flag is corroboration, not
    US evidence (aggregators set it on foreign roles); a pass needs positive
    US evidence. Metro offices pass when the profile allows that metro."""
    loc = job.location or ""
    blob = f"{loc} {job.title}"
    body = job.description[:2500]
    company = job.company or ""

    # US_TOKEN is checked against the body too. Moving the state codes out of
    # US_EVIDENCE removed them from the body check, so a posting whose only US
    # evidence was ", CA" in its text silently lost it.
    us_here = (bool(US_EVIDENCE.search(blob)) or bool(US_TOKEN.search(blob))
               or bool(US_EVIDENCE.search(body[:600])) or bool(US_TOKEN.search(body[:600])))

    if NON_US.search(f"{blob} {company}") and not us_here:
        return "fail", f"non-US: {loc[:60] or company[:40]}"
    cc = NON_US_CC.search(loc)
    if cc and not re.search(r",\s*us\b", loc, re.I):
        if cc.group(1) not in AMBIGUOUS_CC:
            return "fail", f"non-US country code '{cc.group(1)}': {loc[:50]}"
        us_here = True   # a US state abbreviation, not a country code

    if p.metro_re and p.metro_re.search(blob):
        return "pass", f"target metro: {loc[:60]}"

    loc_says_remote = (bool(REMOTE_WORD.search(loc)) or bool(REMOTE_WORD.search(job.title))
                       or job.remote_flag is True)
    remote_claim = loc_says_remote or REMOTE_OK.search(blob) or REMOTE_OK.search(body)

    if remote_claim and p.remote_ok:
        if NON_US.search(f"{loc} {company}"):
            return "fail", f"remote but non-US: {loc[:60]}"
        if VAGUE_REMOTE.search(blob):
            return "unknown", f"'{loc[:40]}' - global remote, US payroll unconfirmed"
        if us_here:
            if not loc_says_remote and loc and US_EVIDENCE.search(loc):
                return "unknown", (f"'{loc[:45]}' - named US office; 'remote' appears "
                                   f"only in body text")
            if US_STATE_LOCK.search(body):
                return "unknown", "remote but state-restricted; needs check"
            # The location field and the body can contradict each other, and the
            # field is the one that lies. A req labelled "Remote" whose body says
            # "Onsite 5x per week (Outside of Atlanta, GA)" passed straight to
            # QUALIFIED; a human reading the description caught it, the tool
            # never would have. Gate on a BINDING attendance clause rather than
            # on the word "hybrid" appearing anywhere, because "we were hybrid
            # before 2020" and "hybrid teams welcome" are not requirements.
            onsite = ONSITE_REQUIREMENT.search(body)
            if onsite:
                return "unknown", (f"labelled remote but the description says "
                                   f"{_sentence_around(body, onsite)!r}")
            return "pass", f"remote-US: {loc[:60] or 'flagged remote'}"
        if loc and not re.search(r"^\s*(remote|anywhere)?\s*$", loc, re.I):
            return "unknown", f"'{loc[:50]}' - remote but US eligibility unconfirmed"
        return "unknown", "remote, no country stated"

    if re.search(r"\b(hybrid|on.?site|in.?office)\b", blob, re.I):
        return "fail", f"onsite/hybrid outside target metros: {loc[:60]}"
    if not loc:
        return "unknown", "no location given"
    if UNRESOLVED_LOC.match(loc):
        # The board hid the cities behind a count. Route to VERIFY so the user
        # can open it, rather than discarding a role that may well be in their
        # own metro.
        return "unknown", (f"'{loc[:40]}' - board did not name the cities; "
                           f"open it to see whether one is yours")
    if BARE_US_LOC.match(loc):
        # A bare country name is not an office. Route to VERIFY, never auto-fail.
        return "unknown", f"'{loc[:40]}' - bare US location, remote vs onsite unstated"
    if us_here:
        if p.relocation:
            return "unknown", f"US office outside target metros (relocation open): {loc[:50]}"
        return "fail", f"US but onsite outside target metros: {loc[:60]}"
    return "fail", f"located {loc[:60]}"


def score(job: Job, p: Profile) -> Job:
    reasons: list[str] = []
    text = job.text
    title = job.title or ""
    # Some employers never repeat their own name in titles (e.g. a company's
    # internal reqs). lane_title_context: {registry_lane: prefix} makes the
    # implicit context explicit before title matching.
    if not title.strip() and not (job.description or "").strip():
        job.gate, job.score = "EXCLUDED", 0
        job.reasons = ["malformed posting: no title and no body"]
        return job
    deadline = posting_deadline(job.description or "")
    if deadline and deadline[0] < date.today():
        job.gate, job.score = "EXCLUDED", 0
        job.reasons = [f"posting closed {deadline[0].isoformat()}: {deadline[1]!r}"]
        return job
    # Keyed on the registry lane. `job.lane` is only the same thing until the
    # first score() call overwrites it with the matched lane key, so reading it
    # here worked at pull and silently stopped working at rescore.
    ctx = p.lane_title_context.get(job.registry_lane or job.lane or "")
    # Only ever ENRICHES a real title. Applied to an empty one the prefix
    # becomes the whole string, so a malformed posting matched the lane on the
    # injected word alone and scored as in-family. Seen live 2026-08-05: a
    # Salesforce Workday row with no title, no body, and the board root as its
    # URL was surfaced as VERIFY.
    if ctx and title.strip() and ctx.lower() not in title.lower():
        title = f"{ctx} {title}"
    exempt = (job.company.lower() in p.dream_companies
              or _norm_company(job.company) in {_norm_company(c) for c in p.dream_companies}) or getattr(job, "rails_exempt", False)
    job.rails_exempt = exempt

    # --- title fit -------------------------------------------------------
    base, lane_key = 0, ""
    body_only_fit = False
    tiers = (p.dream_lanes + p.lanes) if exempt else p.lanes
    for weight, pat, key in tiers:
        if pat.search(title):
            base, lane_key = weight, key
            break
    if not base:
        for weight, pat, key in p.lanes:   # was [:12]; lanes 13+ silently lost the body fallback
            if pat.search(text[:1500]):
                base, lane_key = max(10, weight - 22), key
                body_only_fit = True
                reasons.append("fit only in body, not title")
                break
    if not base:
        job.gate, job.score = "EXCLUDED", 0
        job.reasons = ["no role-family match"]
        return job
    if lane_key:
        job.lane = lane_key

    # --- slot block ------------------------------------------------------
    if p.slot_block_always:
        m = p.slot_block_always.search(title)
        if m:
            job.gate, job.score = "SLOT-BLOCKED", base
            job.reasons = [f"role family not pursued: '{m.group(0)[:40]}'"]
            return job

    if p.slot_block and not exempt:
        m = p.slot_block.search(title)
        if m:
            job.gate, job.score = "SLOT-BLOCKED", base
            job.reasons = [f"role family not pursued: {m.group(0)}"]
            return job

    # --- competing platform ---------------------------------------------
    if p.competing and not exempt:
        m = p.competing.search(title)
        if m:
            job.gate, job.score = "SLOT-BLOCKED", base
            job.reasons = [f"competing platform: {m.group(0)}"]
            return job

    # --- product specialization not held --------------------------------
    if p.products_block and not exempt:
        m = p.products_block.search(title)
        if m:
            job.gate, job.score = "SLOT-BLOCKED", base
            job.reasons = [f"product specialization not held: {m.group(0)}"]
            return job
        # Body can REQUIRE an unheld product even when the title is clean
        # (e.g. "3 years specializing in B2B commerce" under a plain SA title).
        for m in p.products_block.finditer(text):
            # Context is the match's own sentence UP TO the match. This replaced
            # a fixed 70-character lookbehind, which was wrong in both directions:
            # too short for a long bullet, and it reached back across sentence
            # boundaries into the previous one. PointClickCare's "Led at least two
            # large-scale Product-to-Cash (CPQ/RCA) implementations", under a
            # literal "Required Experience & Skills" heading, survived as VERIFY
            # 47 because the framing that makes it a requirement opens the bullet
            # (2026-08-10). Deliberately only what comes BEFORE the match:
            # scanning the whole sentence would block on "partner with the CPQ
            # team; 5+ years of Salesforce required", where the requirement
            # attaches to something else entirely.
            ctx = _clause_before(text, m)
            if _PREFERENCE.search(_sentence_around(text, m)):
                continue          # the posting says this one is optional
            if re.search(r"(\d\+? ?years?|require[sd]?|must have|deep (knowledge|expertise)|"
                         r"speciali[sz]|expert(ise)? in|proficien|"
                         # experience stated as a COUNT of deliveries rather than
                         # years, which is how senior architecture reqs phrase it
                         r"\b(led|delivered|implemented|owned|shipped)\b.{0,40}?"
                         r"\b(at least|a minimum of|minimum of|\d+\+?)\b|"
                         r"\bat least \d+\b|\ba minimum of \d+\b)", ctx, re.I):
                job.gate, job.score = "SLOT-BLOCKED", base
                job.reasons = [f"body requires unheld product: '{text[max(0, m.start()-40):m.end()][-60:]}'"]
                return job

    # --- hard rails ------------------------------------------------------
    if not exempt:
        v, e = location_verdict(job, p)
        if v == "fail":
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [e]
            return job
        loc_v, loc_e = v, e
    else:
        loc_v, loc_e = "pass", "rails-exempt employer"

    if p.block_quota and not exempt:
        m = _rail_hit(QUOTA, text)
        if m:
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [f"revenue/quota rail: {_sentence_around(text, m)!r}"]
            return job

    for pat, label in p.body_blocks:
        if exempt:
            break
        m = _rail_hit(pat, text)
        if m:
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [f"{label}: {_sentence_around(text, m)!r}"]
            return job

    if p.certs_refused and not exempt:
        m = _rail_hit(p.certs_refused, text)
        if m:
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [f"requires refused cert: {_sentence_around(text, m)!r}"]
            return job

    # Deliberately NOT exempt-gated, unlike every other rail: a dream employer
    # cannot waive a clearance requirement, so enthusiasm should not bypass it.
    if p.block_clearance:
        m = _rail_hit(CLEARANCE, text)
        if m:
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [f"clearance-gated: {_sentence_around(text, m)!r}"]
            return job

    # Gates the candidate cannot clear, read from the REQUIREMENTS section only.
    # Four roles were recommended on their titles in one day and every one died
    # here: an architect credential, a Platform Developer II, ten years plus
    # direct reports. The rails could not see any of it because the gate lives in
    # a section the feed row did not carry, and a rail that cannot read its
    # subject passes.
    if p.hard_requirements and not exempt:
        from .jd import hard_requirements as _hard
        for label, sentence in _hard(job.description or "", p.hard_requirements):
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [f"requirement not met ({label}): {sentence!r}"]
            return job

    # --- domain evidence -------------------------------------------------
    if p.domain_terms and not p.domain_terms.search(text) and not exempt:
        if len(job.description or "") < 250:
            job.gate, job.score = "VERIFY", max(1, base - 25)
            job.reasons = ["domain NOT CONFIRMED - board gave no body text",
                           f"title matched: {title[:60]}"]
            return job
        job.gate, job.score = "EXCLUDED", base
        job.reasons = ["domain terms never mentioned in posting"]
        return job

    # --- comp ------------------------------------------------------------
    lo, hi = extract_comp(job)
    # Write the resolved figures back, or they exist only inside this function.
    # Two things were lost without this, both silent: a band parsed out of the
    # posting body never reached the row, so the report printed "Comp not stated"
    # in the header while the reasons line right under it quoted the band, and
    # `report --format csv` exported an empty comp column for a third of the
    # postings that had one. And extract_comp's hourly x2080 normalization was
    # discarded too, leaving a raw "75" in the database for an hourly role -
    # the same annualization confusion that made a contract req look like a
    # $299-366K salary. Only overwrite when something was resolved.
    if lo is not None:
        job.comp_min = lo
    if hi is not None:
        job.comp_max = hi
    comp_state = "unknown"
    if lo and p.screen_floor:
        top = hi or lo
        if top < p.screen_floor and not exempt:
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [f"comp ceiling ${top:,} below ${p.screen_floor:,} screen floor"]
            return job
        comp_state = "pass" if top >= p.accept_floor else "thin"
        reasons.append(f"comp ${lo:,}" + (f"-${hi:,}" if hi else "") +
                       ("" if comp_state == "pass" else " (below accept floor, negotiable?)"))
    elif lo:
        comp_state = "pass"
        reasons.append(f"comp ${lo:,}" + (f"-${hi:,}" if hi else ""))
    else:
        reasons.append("comp not stated")

    # --- positive signals ------------------------------------------------
    bonus = 0
    for pat, pts, label in p.signals:
        if pat.search(text):
            bonus += pts
            reasons.append(label)
    if job.employer_tier == "A":
        bonus += 6
    elif job.employer_tier == "B":
        bonus += 3

    total = min(100, base + bonus + (8 if comp_state == "pass" else 0))

    # --- gate ------------------------------------------------------------
    unknowns = []
    # Every body rail above "passed" when there was no body to read. That is not
    # the posting being clean, it is the rails never having run, and the two are
    # indistinguishable in the output: an empty description scored QUALIFIED at
    # 50 with reasons that mentioned only location and comp. This is the shape
    # behind every recommendation that died on a requirement nobody had read,
    # so say so in the gate, which is exactly what VERIFY is defined to mean.
    if len((job.description or "").strip()) < THIN_BODY:
        unknowns.append(f"only {len((job.description or '').strip())} characters of "
                        f"description, so the requirement rails could not run")
    if loc_v == "unknown":
        unknowns.append(loc_e)
    if comp_state == "unknown" and p.screen_floor:
        unknowns.append("comp unstated")
    if comp_state == "thin":
        unknowns.append("comp below accept floor")
    # A product name buried in an unrelated description is not enough evidence
    # that the role itself belongs to the requested family. Seen live in an IAM
    # Architect listing whose title was wholly outside the profile: one mention
    # of Salesforce in a list of enterprise applications promoted it all the
    # way to QUALIFIED. Keep body fallback useful for sparse aggregator titles,
    # but make the uncertainty visible and require human verification.
    if body_only_fit:
        unknowns.append("role family matched only in body, not title")

    job.gate = "VERIFY" if unknowns else "QUALIFIED"
    job.score = total
    job.reasons = ([loc_e] if loc_v == "pass" else []) + reasons + \
                  ([f"NEEDS CHECK: {'; '.join(unknowns)}"] if unknowns else [])
    return job


def score_all(jobs: list[Job], profile: Profile) -> list[Job]:
    return [score(j, profile) for j in jobs]
