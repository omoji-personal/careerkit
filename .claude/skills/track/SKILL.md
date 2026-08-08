---
name: track
description: Pipeline hygiene - log outcomes, surface stale threads, weekly view. Use for "where do things stand", after any application/response/interview.
---
# /track — pipeline memory
1. Run `./careerkit.py analytics`. Its denominators include safely-unmatched
   application evidence; do not replace them with a hand count from prose.
2. For a known posting transition, run `./careerkit.py progress UID STAGE --on
   YYYY-MM-DD --notes "..."`. Then update `profile/tracker.md`: APPLIED (date,
   role, link, comp, resume used, commitments) / INTERVIEWING (stage, next step,
   prep link) / SEEN / RULED-OUT (with reason - prevents re-surfacing).
3. Employer context that outlives a requisition goes through `relationship add`:
   recruiter, referral, contact, prior interview, invitation, rejection or note.
4. Surface the calculated 14+ day follow-up queue with a suggested `/outreach`
   nudge. Weekly view = stage counts, upcoming interviews, aging, response and
   interview conversion by lane; feed sustained lane differences into
   `/criteria`, never one anecdote.
5. Offer `./careerkit.py report --format html` for the private command center.
