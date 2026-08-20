---
name: setup
description: First-run onboarding - Discovery + Search Criteria interviews that build the user's profile. Use when profile/profile.yaml is missing, or the user asks to redo onboarding.
---
# /setup — build the profile

Two interviews. Be thorough; everything downstream depends on this.

## Privacy disclosure — before documents or network requests
Explain plainly, and confirm the user wants to proceed: everything Claude reads
is processed by Anthropic under the user's Claude plan terms. CareerKit also
makes outbound requests to employer boards, feeds, and pasted URLs; USAJobs
and other optional keyed feeds transmit the configured API credential or
account identifier to their provider, and USAJobs also includes the registered
email header.
There is no CareerKit telemetry. Deleting `profile/`, `data/`, and `out/`
removes CareerKit's local private state, but cannot retract data already sent to
those services or remove copies saved elsewhere.

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
   signals, dream_companies, search_terms, autonomy, eeo, work_auth. Set
   `autonomy.submit: ask_each`; CareerKit has no batch or standing submission
   permission.
2. After the privacy confirmation above, ask for their target-company list; run
   `./careerkit.py discover --file <list>` to find each company's ATS, then
   `./careerkit.py verify`.

## Part 4 — Calibrate (MANDATORY)
Run `./careerkit.py profile-lint`, then `./careerkit.py pull`, then
`./careerkit.py audit`. Walk the user through the kill list: "these were
excluded and why - correct me." After each approved criteria correction, run
`profile-lint`, `rescore`, `pull`, and `audit` again until the user agrees with
the gates. `pull` is mandatory because previously screened-out postings were
never stored for `rescore` to recover. Derived rules WILL have false-negative
classes until calibrated.
