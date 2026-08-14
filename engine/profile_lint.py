"""Lint a profile against failure classes that have actually shipped.

Two classes, both from live incidents rather than speculation:

1. A lane that admits a competing platform. `crm-mgr` keyed on bare "CRM" and
   admitted a Microsoft Dynamics 365 req (Optomi, 2026-08-06) that reached the
   report as an Atlanta find. The lane was patched; nothing stopped the next
   lane from shipping the same hole. The lint scores a fixed corpus of
   real-shaped competing-platform postings through the FULL scorer - not the
   lane regex alone - so whatever rail is supposed to stop them is tested as a
   system, exactly the way a live pull exercises it.

2. A product exclusion written under a name the vendor no longer uses.
   Vendors rename products: the profile excluded "data cloud", the vendor
   renamed it Data 360, and JR356148 "Data360 Success Architect" led the
   2026-08-14 report at 84 while its minimum requirement was two implementation
   lifecycles of a product the user has never run. The same rename family
   (CPQ -> Revenue Cloud) had already produced the same bug on 2026-08-12.
   The engine maintains the rename families; the lint checks that a profile
   excluding ANY name in a family excludes enough of its siblings to see
   current postings. Instances inherit new renames by updating the engine.
"""
from __future__ import annotations

import re

from .models import Job
from .score import Profile, score

#: Product-name drift, maintained HERE so every instance inherits a rename by
#: pulling the engine rather than by hitting the bug first. A family lists the
#: names one product has carried; it is not a list of similar products.
PRODUCT_FAMILIES: dict[str, list[str]] = {
    "Data Cloud / Data 360": ["data cloud", "data 360", "data360",
                              "customer data platform"],
    "CPQ / Revenue Cloud": ["cpq", "configure price quote", "revenue cloud",
                            "lead to cash"],
    "Pardot / Account Engagement": ["pardot", "account engagement", "mcae"],
    "Einstein Analytics / CRM Analytics": ["einstein analytics",
                                           "crm analytics", "tableau crm"],
    "Field Service": ["field service lightning", "fsl", "field service"],
}

#: Real-shaped postings from platforms this tool's scope excludes. Each body is
#: long enough not to trip the thin-body rail, so the verdict comes from the
#: lanes and platform rails - the ones this lint exists to test.
_PAD = " The team collaborates across departments to deliver business value." * 12
ADVERSARIAL_POSTINGS: list[tuple[str, str]] = [
    ("Microsoft Dynamics 365 CRM Manager",
     "Administer and extend our Microsoft Dynamics 365 CE environment, Power "
     "Platform and SharePoint integrations." + _PAD),
    ("Dynamics CRM Solution Architect",
     "Architect solutions on Microsoft Dynamics 365 Customer Engagement, "
     "Power Automate and Dataverse." + _PAD),
    ("HubSpot CRM Administrator",
     "Own our HubSpot CRM: workflows, pipelines, reporting and integrations "
     "with the marketing stack." + _PAD),
    ("CRM Platform Owner, ServiceNow",
     "Own the ServiceNow CSM platform roadmap, intake and release cadence "
     "for customer service management." + _PAD),
    ("Zoho CRM Consultant",
     "Configure Zoho CRM for small-business clients: modules, blueprints, "
     "Deluge scripting and integrations." + _PAD),
    ("SAP CRM Functional Consultant",
     "Deliver SAP CRM and C4C functional workstreams, integration with S/4HANA "
     "and the SAP BTP landscape." + _PAD),
]


def _terms(raw_profile: dict) -> list[str]:
    """The profile's product exclusions as comparable lowercase strings.

    Terms may be plain strings or /regex/ literals; a regex is compared by its
    pattern text as well as executed, so "/field service( lightning)?/" still
    counts as covering "field service"."""
    exc = (raw_profile.get("exclusions") or {})
    out = []
    for t in exc.get("products") or []:
        t = str(t).strip().lower()
        out.append(t)
    return out


def _covers(term: str, alias: str) -> bool:
    pat = term[1:-1] if term.startswith("/") and term.endswith("/") else None
    if pat:
        try:
            return re.search(pat, alias, re.I) is not None
        except re.error:
            return False
    return term in alias or alias in term


def lint(raw_profile: dict, profile: Profile) -> list[tuple[str, str]]:
    """Returns (severity, message) findings. severity: "fail" or "note"."""
    findings: list[tuple[str, str]] = []

    # 1. Competing platforms, through the full scorer.
    for title, body in ADVERSARIAL_POSTINGS:
        j = Job(company="Contoso Partners", title=title,
                url="https://lint.invalid/1", source="greenhouse",
                location="Remote, United States", description=body)
        score(j, profile)
        if j.gate in ("QUALIFIED", "VERIFY"):
            findings.append((
                "fail",
                f"a competing-platform posting surfaces as {j.gate}: "
                f"{title!r} (lane {j.lane or '?'}, score {j.score}). A lane is "
                "matching a title family the platform rails do not stop."))

    # 2. Rename families. Only families the profile already engages with are
    # checked: not excluding a product anywhere is a scope choice, not a bug.
    terms = _terms(raw_profile)
    for family, aliases in PRODUCT_FAMILIES.items():
        covered = [a for a in aliases if any(_covers(t, a) for t in terms)]
        if covered and len(covered) < len(aliases):
            missing = [a for a in aliases if a not in covered]
            findings.append((
                "fail",
                f"exclusions.products covers {family} as {covered} but not "
                f"{missing} - the vendor has used every name in this family, "
                "so postings written under the missing ones pass the screen. "
                "The 2026-08-14 Data360 miss is this exact shape."))

    return findings
