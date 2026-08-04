---
name: audit
description: Calibration sweep - re-fetch boards, re-score, review every kill so silent false negatives get caught. Run after /setup, after criteria changes, and monthly.
---
# /audit — catch the silent kills
1. `./careerkit.py audit` (add --grep 'term' to focus).
2. Walk the kill groups with the user: for each reason bucket, sample titles;
   ask "any of these wrong?" A wrongly-killed role means profile.yaml needs a
   fix (lane pattern, metro, floor, exclusion too broad) - never engine code.
3. Also check `./careerkit.py status` for failing sources (silent coverage
   loss) and boards stuck at round-number counts (pagination caps).
4. Re-run until the user agrees with every gate. Log the date in tracker.md.
Derived rules rot as titles drift; this loop is what keeps precision honest.
