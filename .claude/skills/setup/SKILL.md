---
name: setup
description: First-run onboarding - Discovery + Search Criteria interviews that build the user's profile. Use when profile/profile.yaml is missing, or the user asks to redo onboarding.
---
# /setup — build the profile

Two interviews. Be thorough; everything downstream depends on this.

## Part 1 — Discovery (learn the person)
1. Ask for their resume (PDF: READ IT VISUALLY with the Read tool, never
   text-extract - multi-column resumes mangle), LinkedIn (exported PDF or
   pasted text), portfolio/GitHub links, brag docs, reviews.
2. Interview past the documents: what they actually did vs what the resume
   says; proudest work WITH numbers; what they never want to do again;
   strengths they underclaim; stories (situation, action, quantified result).
3. Write:
   - `profile/person.md` - working understanding of who they are
   - `profile/claims.md` - verified facts ONLY, each confirmed by the user;
     a "never say" boundary list; a story bank with numbers
   - `profile/style.md` - 2-3 writing samples from them + voice rules
4. PROPOSE role families (lanes) with title patterns and weights derived from
   what you learned. Show them; adjust until they agree.

## Part 2 — Search Criteria (learn the search)
Interview comprehensively: locations/remote/metros/relocation; comp screen
floor + accept floor; industries in/out; company size/stage; work
authorization + sponsorship + export-control disclosures (answer truthfully -
they are disclosures, not disqualifiers); demographic/EEO answer set (answer
vs decline per question, collected once); hard exclusions (clearance,
quota-carrying, travel %, tech/products they refuse or lack); certs held vs
refused; dream companies (judged on fit, not mechanical rails); timeline;
constraints for later stages (leave windows, notice period).

## Part 3 — Write config + seed sources
1. Write `profile/profile.yaml` per profile.example/profile.yaml schema:
   identity, location, comp, lanes, dream_lanes, exclusions, domain_terms,
   signals, dream_companies, search_terms, autonomy, eeo, work_auth.
2. Ask for their target-company list; run
   `./careerkit.py discover --file <list>` to find each company's ATS, then
   `./careerkit.py verify`.
3. Privacy disclosure, plainly: profile content is processed by Anthropic
   under their Claude plan's data terms; nothing else leaves the machine;
   deleting profile/ + data/ removes all local state.

## Part 4 — Calibrate (MANDATORY)
Run `./careerkit.py pull`, then `./careerkit.py audit`. Walk the user through
the kill list: "these were excluded and why - correct me." Fix profile.yaml
for anything wrongly killed and re-run until the user agrees with the gates.
Derived rules WILL have false-negative classes until calibrated.
