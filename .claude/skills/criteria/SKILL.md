---
name: criteria
description: Re-run or amend the search-criteria interview (comp, locations, lanes, exclusions). Use when the user's targets change.
---
# /criteria — amend the search rules
Load profile/profile.yaml, interview only what is changing, show a diff of
the YAML before saving, then run `./careerkit.py audit` to confirm the new
gates behave. Never edit engine code to change behavior - the profile is the
only rule source.
