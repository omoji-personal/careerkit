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
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Job

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
    r"anywhere in the u\.?s|nationwide|" + _US_STATES + r")\b|"
    r",\s?(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    r"MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|"
    r"WV|WI|WY|DC)\b", re.I)
# Trailing lowercase ISO country codes; CASE-SENSITIVE on purpose ("Remote, in"
# is India, "Indianapolis, IN" is not).
NON_US_CC = re.compile(
    r",\s*(cn|pl|de|in|mx|br|ar|co|cl|pe|uy|ca|gb|uk|ie|fr|es|it|nl|be|pt|gr|"
    r"cz|hu|ro|ua|rs|bg|hr|ee|lv|lt|at|ch|se|no|dk|fi|au|nz|sg|jp|kr|tw|hk|"
    r"my|th|vn|id|ph|pk|bd|ae|sa|qa|il|tr|eg|za|ng|ke|ma)\b(?!\w)")

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
_MONEY = re.compile(r"\$\s?(\d{2,3})(?:,(\d{3}))?(?:\s?[kK])?(?:\.\d+)?")
_RANGE_CTX = re.compile(
    r"(base (salary|pay|compensation)|salary range|pay range|compensation range|"
    r"hiring range|target salary|annual (salary|base))", re.I)


# --------------------------------------------------------------------------
# Profile: the person's rules, compiled once
# --------------------------------------------------------------------------

def _compile_alt(terms: list[str] | None, window: str = "") -> re.Pattern | None:
    """Word-bounded alternation of literal terms or raw /regex/ entries."""
    if not terms:
        return None
    parts = []
    for t in terms:
        t = str(t)
        parts.append(t[1:-1] if (t.startswith("/") and t.endswith("/")) else re.escape(t))
    return re.compile(r"\b(" + "|".join(parts) + r")\b" + window, re.I)


@dataclass
class Profile:
    screen_floor: int = 0
    accept_floor: int = 0
    remote_ok: bool = True
    relocation: bool = False
    metro_re: re.Pattern | None = None
    lanes: list[tuple[int, re.Pattern, str]] = field(default_factory=list)
    dream_lanes: list[tuple[int, re.Pattern, str]] = field(default_factory=list)
    slot_block: re.Pattern | None = None
    products_block: re.Pattern | None = None
    certs_refused: re.Pattern | None = None
    body_blocks: list[tuple[re.Pattern, str]] = field(default_factory=list)
    competing: re.Pattern | None = None
    domain_terms: re.Pattern | None = None
    block_clearance: bool = True
    block_quota: bool = True
    dream_companies: set = field(default_factory=set)
    signals: list[tuple[re.Pattern, int, str]] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)
    lane_title_context: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        cfg = yaml.safe_load(Path(path).read_text()) or {}
        comp = cfg.get("comp") or {}
        loc = cfg.get("location") or {}
        exc = cfg.get("exclusions") or {}
        p = cls(
            screen_floor=int(comp.get("screen_floor") or 0),
            accept_floor=int(comp.get("accept_floor") or comp.get("screen_floor") or 0),
            remote_ok=bool(loc.get("remote_us", True)),
            relocation=bool(loc.get("relocation", False)),
            block_clearance=bool(exc.get("clearance", True)),
            block_quota=bool(exc.get("quota", True)),
            dream_companies={c.lower() for c in (cfg.get("dream_companies") or [])},
            search_terms=list(cfg.get("search_terms") or []),
            lane_title_context=dict(cfg.get("lane_title_context") or {}),
        )
        metros = loc.get("metros") or []
        p.metro_re = _compile_alt(metros) if metros else None
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
        p.slot_block = _compile_alt(exc.get("titles"))
        p.products_block = _compile_alt(exc.get("products"))
        certs = exc.get("certs_refused") or []
        if certs:
            alt = "|".join(re.escape(c) for c in certs)
            p.certs_refused = re.compile(
                r"\b(" + alt + r")\b.{0,40}\b(required|must have|require|certification required)\b|"
                r"\b(required|must have)\b.{0,40}\b(" + alt + r")\b", re.I)
        for b in exc.get("body_patterns") or []:
            pat = _compile_alt([b] if isinstance(b, str) else b.get("terms", []))
            if pat:
                p.body_blocks.append((pat, b if isinstance(b, str) else b.get("label", "blocked")))
        p.competing = _compile_alt(exc.get("competing_platforms"))
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
        if lo and lo < 1000:
            lo, hi = lo * 2080, (hi * 2080 if hi else None)
        return lo, hi
    text = f"{job.comp_text}\n{job.description}"
    best: list[int] = []
    for m in _RANGE_CTX.finditer(text):
        window = text[m.start(): m.start() + 320]
        hourly = bool(re.search(r"\bper hour\b|/\s?h(?:ou)?r\b|\bhourly\b", window, re.I))
        vals = []
        for mm in _MONEY.finditer(window):
            a, b = mm.group(1), mm.group(2)
            raw = int(a + b) if b else int(a)
            if hourly and raw < 500:
                v = raw * 2080
            else:
                v = raw if b else raw * 1000
            if 40_000 <= v <= 500_000:
                vals.append(v)
        if len(vals) >= 2:
            best = [min(vals), max(vals)]
            break
        if vals and not best:
            best = vals[:1]
    if not best:
        return None, None
    return (best[0], best[-1] if len(best) > 1 else None)


def location_verdict(job: Job, p: Profile) -> tuple[str, str]:
    """pass / fail / unknown. A board's own remote flag is corroboration, not
    US evidence (aggregators set it on foreign roles); a pass needs positive
    US evidence. Metro offices pass when the profile allows that metro."""
    loc = job.location or ""
    blob = f"{loc} {job.title}"
    body = job.description[:2500]
    company = job.company or ""

    us_here = bool(US_EVIDENCE.search(blob)) or bool(US_EVIDENCE.search(body[:600]))

    if NON_US.search(f"{blob} {company}") and not us_here:
        return "fail", f"non-US: {loc[:60] or company[:40]}"
    cc = NON_US_CC.search(loc)
    if cc and not re.search(r",\s*us\b", loc, re.I):
        return "fail", f"non-US country code '{cc.group(1)}': {loc[:50]}"

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
            return "pass", f"remote-US: {loc[:60] or 'flagged remote'}"
        if loc and not re.search(r"^\s*(remote|anywhere)?\s*$", loc, re.I):
            return "unknown", f"'{loc[:50]}' - remote but US eligibility unconfirmed"
        return "unknown", "remote, no country stated"

    if re.search(r"\b(hybrid|on.?site|in.?office)\b", blob, re.I):
        return "fail", f"onsite/hybrid outside target metros: {loc[:60]}"
    if not loc:
        return "unknown", "no location given"
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
    ctx = p.lane_title_context.get(job.lane or "")
    if ctx and ctx.lower() not in title.lower():
        title = f"{ctx} {title}"
    exempt = job.company.lower() in p.dream_companies or getattr(job, "rails_exempt", False)
    job.rails_exempt = exempt

    # --- title fit -------------------------------------------------------
    base, lane_key = 0, ""
    tiers = (p.dream_lanes + p.lanes) if exempt else p.lanes
    for weight, pat, key in tiers:
        if pat.search(title):
            base, lane_key = weight, key
            break
    if not base:
        for weight, pat, key in p.lanes[:12]:
            if pat.search(text[:1500]):
                base, lane_key = max(10, weight - 22), key
                reasons.append("fit only in body, not title")
                break
    if not base:
        job.gate, job.score = "EXCLUDED", 0
        job.reasons = ["no role-family match"]
        return job
    if lane_key:
        job.lane = lane_key

    # --- slot block ------------------------------------------------------
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
            ctx = text[max(0, m.start() - 70):m.start()]
            if re.search(r"(\d\+? ?years?|require[sd]?|must have|deep (knowledge|expertise)|"
                         r"speciali[sz]|expert(ise)? in|proficien)", ctx, re.I):
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
        m = QUOTA.search(text)
        if m:
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [f"revenue/quota rail: '{m.group(0)[:40]}'"]
            return job

    for pat, label in p.body_blocks:
        if exempt:
            break
        m = pat.search(text)
        if m:
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [f"{label}: '{m.group(0)[:60]}'"]
            return job

    if p.certs_refused and not exempt:
        m = p.certs_refused.search(text)
        if m:
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [f"requires refused cert: '{m.group(0)[:60]}'"]
            return job

    if p.block_clearance:
        m = CLEARANCE.search(text)
        if m:
            job.gate, job.score = "EXCLUDED", base
            job.reasons = [f"clearance-gated: '{m.group(0)[:60]}'"]
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
    if loc_v == "unknown":
        unknowns.append(loc_e)
    if comp_state == "unknown" and p.screen_floor:
        unknowns.append("comp unstated")
    if comp_state == "thin":
        unknowns.append("comp below accept floor")

    job.gate = "VERIFY" if unknowns else "QUALIFIED"
    job.score = total
    job.reasons = ([loc_e] if loc_v == "pass" else []) + reasons + \
                  ([f"NEEDS CHECK: {'; '.join(unknowns)}"] if unknowns else [])
    return job


def score_all(jobs: list[Job], profile: Profile) -> list[Job]:
    return [score(j, profile) for j in jobs]
