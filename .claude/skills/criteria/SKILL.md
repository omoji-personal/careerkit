---
name: criteria
description: Re-run or amend the search-criteria interview (comp, locations, lanes, exclusions). Use when the user's targets change.
---
# /criteria — amend the search rules
Load profile/profile.yaml, interview only what is changing, show a diff of
the YAML before saving, then run, in order:

1. `./careerkit.py profile-lint` to reject malformed or contradictory rules.
2. `./careerkit.py rescore` to apply the approved criteria to stored postings.
3. `./careerkit.py pull --no-cache` to recover postings that the previous rules screened
   out before they could be stored.
4. `./careerkit.py audit` to group the freshly pulled exclusions and review
   representative false-negative candidates.

Walk through any changed verdicts and audit kills with the user. Never edit
engine code to change behavior - the profile is the only rule source.
