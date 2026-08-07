"""How likely is it that this posting is not a real, open job?

Written after a listing reached the top of a report with the highest
compensation on the board: "Principal Salesforce Solution Consultant", $299,000
to $366,000, in the user's own metro. Every part of it was false. The band was
an hourly contract rate multiplied by 2,080. The company's domain was parked for
sale. No consultancy of that name existed in the city. Every sighting came from
a scraper aggregator and every link was dead.

Nothing in the tool disagreed with any of it, because nothing in the tool was
looking. The scoring rails answer "does this posting match what you want", which
is a different question from "is this posting real".

This deliberately SCORES rather than excludes. A ghost listing and a small
employer with a thin web presence look similar from here, and silently dropping
the second to catch the first is the failure this project exists to avoid. The
score is surfaced in the report so a person can decide.
"""
from __future__ import annotations

import re

# Aggregators re-list other people's postings. A role sighted only there, with no
# employer ATS anywhere, has never been corroborated by the employer itself.
_AGGREGATOR_PREFIXES = ("feed:", "jobspy", "linkedin_guest", "adzuna", "indeed",
                        "ziprecruiter", "glassdoor", "careerjet", "findwork")


def _is_aggregator(source: str) -> bool:
    s = (source or "").lower()
    return any(s.startswith(p) or s == p for p in _AGGREGATOR_PREFIXES)


def score_row(row) -> tuple[int, list[str]]:
    """Return (points, reasons). Higher means less likely to be a real opening."""
    pts, why = 0, []
    keys = row.keys() if hasattr(row, "keys") else {}

    def get(k, default=""):
        return (row[k] if k in keys else default) or default

    source = get("source")
    # NULL and "" mean different things and conflating them floods the report.
    # A row stored before this evidence was captured has NULL in both columns:
    # nobody looked, so the absence says nothing. A row fetched since has "",
    # meaning the feed looked and the employer resolved to nothing, which is a
    # real signal. Scoring "never looked" as "found nothing" flagged 55 of 56
    # live rows on the first run against a real database.
    looked = ("url_direct" in keys and row["url_direct"] is not None) or \
             ("company_site" in keys and row["company_site"] is not None)
    url_direct = get("url_direct")
    company_site = get("company_site")

    if looked and _is_aggregator(source) and not url_direct:
        pts += 2
        why.append("only ever seen on aggregators, with no employer apply link")

    if looked and _is_aggregator(source) and not company_site:
        pts += 2
        why.append("no corporate website resolved for the employer")

    # The Tria Prima tell. An hourly contract rate presented as a salary is
    # exactly divisible by 2,080, which a real salary band almost never is.
    # Requires BOTH ends to divide, because plenty of ordinary salaries happen
    # to: $208,000 is exactly $100 an hour x 2080, and flagging a real band on
    # that alone is how a useful check becomes noise. A band whose two ends are
    # both whole hourly rates is a contract rate that has been annualised.
    lo, hi = get("comp_min", 0) or 0, get("comp_max", 0) or 0
    if lo and hi and lo != hi and lo % 2080 == 0 and hi % 2080 == 0 and lo >= 100_000:
        pts += 3
        why.append(f"${lo:,} and ${hi:,} are both exactly an hourly rate x 2080 "
                   f"(${lo // 2080}/hr and ${hi // 2080}/hr), so this is probably a "
                   f"contract rate presented as a salary")

    # A title that carries its own advertisement is a listing-quality signal.
    title = get("title")
    if re.search(r"\$[\d,]{3,}|first year potential|earn up to|unlimited (earning|income)",
                 title, re.I):
        pts += 2
        why.append("the title advertises pay rather than naming a role")

    return pts, why


def flag(row, threshold: int = 4) -> str | None:
    """One line for the report, or None when the posting looks ordinary."""
    pts, why = score_row(row)
    if pts < threshold:
        return None
    return f"UNVERIFIED EMPLOYER ({pts}): " + "; ".join(why)


def review(con, threshold: int = 4) -> list[str]:
    """Every live row that looks unverified, worst first."""
    rows = con.execute(
        "SELECT * FROM jobs WHERE gate IN ('QUALIFIED','VERIFY') AND status='new'"
    ).fetchall()
    out = []
    for r in rows:
        pts, why = score_row(r)
        if pts >= threshold:
            out.append((pts, f"{r['company']} / {r['title'][:50]}: " + "; ".join(why)))
    return [m for _, m in sorted(out, reverse=True)]
