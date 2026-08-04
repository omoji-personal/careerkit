---
name: compose
description: Audit or create career collateral - resume, cover letter, LinkedIn sections - in the user's voice, claims-gated. Use for "review my resume", "write a cover letter", "update my LinkedIn".
---
# /compose — collateral, claims-gated
AUDIT mode: read the artifact (PDFs visually), score against target lanes +
claims.md, give concrete line edits. Check ATS-parseability without keyword
stuffing.
CREATE mode: cover letters, answers, resume variants, LinkedIn section text -
in the USER'S voice per style.md. Every fact must exist in claims.md; new
facts require user confirmation and get added to the register first.
LINKEDIN EDITING (browser-driven, autonomy: linkedin_edits governs):
1. Always produce the paste-ready section blocks FIRST.
2. With user present + confirming, drive their logged-in session via
   Claude-in-Chrome: one section at a time, show before/after diff, user
   approves each save. Human-paced; own profile only.
3. On any element-not-found: STOP retrying, open the edit page, hand them the
   block to paste. LinkedIn's DOM churns - degrade gracefully.
4. Disclose once per session: automated profile editing sits outside
   LinkedIn's ToS; account-restriction risk is theirs to accept.
