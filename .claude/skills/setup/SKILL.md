---
name: setup
description: Resumable first-run onboarding. Build a search-ready core first, then optionally expand sources and complete the career pack. Use when profile/profile.yaml or the privacy/Search Core checkpoint is missing or incomplete, or when the user asks to resume or redo a selected phase.
---
# /setup — become search-ready, then complete the career pack

Setup is resumable. A user can search after **Search Core** is complete; the
longer Career Pack can be finished now or deferred until `/compose`, `/prep`, or
`/apply` needs it. Never make application-stage questions a condition of
searching.

## Phase 0 — privacy gate

1. Before reading any personal artifact, run `./careerkit.py doctor`. Its local,
   non-echoing parser checks whether `profile/setup-progress.md` exists and has
   a valid `Privacy: complete` row without sending the checkpoint contents to
   Claude. Do not open the checkpoint with a file-reading tool: an accidentally
   pasted answer must not cross the privacy boundary merely because it is in the
   nominally content-free file. Do not open, validate, summarize, or run
   `profile-lint` against any other profile artifact yet. If doctor reports the
   checkpoint missing, malformed, or unacknowledged, give the disclosure below
   and obtain acknowledgment first.
## Privacy disclosure and acknowledgment — before documents or network requests

Explain plainly, and confirm the user wants to proceed:

- Everything Claude reads is processed by Anthropic under the user's Claude
  plan terms.
- CareerKit also makes outbound requests to employer boards, feeds, and pasted URLs.
  USAJobs and other optional keyed feeds transmit the configured API credential
  or account identifier to their provider; USAJobs also includes the registered
  email header.
- Freehire is shipped inactive. **Only if it is separately enabled**, CareerKit
  sends each configured `search_terms` phrase (quoted), optional country/age
  filters, and ordinary network metadata to `freehire.me`. It sends no resume,
  claims register, application data, secret, or API key. Enabling Freehire
  requires a separate, explicit activation confirmation at that time; general
  setup or sourcing consent is not activation consent.
- There is no CareerKit telemetry. Deleting `profile/`, `data/`, and `out/`
  removes CareerKit-owned local private state, but cannot retract data already sent
  to those services or remove copies saved elsewhere.

If the user does not acknowledge this, stop before reading personal documents,
making requests, or creating the progress file.

After acknowledgment, create or update `profile/setup-progress.md`. It is a
**content-free checklist**, never a second profile. It may contain only these
phase names and one status per line (`pending`, `complete`, `skipped`, or
`deferred`):

```text
Privacy: complete
Search Core: pending
First Win: pending
Source Expansion: pending
Career Pack: pending
Final Checks: pending
```

Never put answers, resume text, claims, employer names, URLs, credentials,
demographics, filenames supplied by the user, or interview notes in this file.
Update the checklist after every phase, including a deliberate skip or deferral.
Every checkpoint update must be atomic: render the complete allowed checklist
to a sibling temporary file, close it, then atomically replace
`profile/setup-progress.md`. If replacement fails, preserve the previous
checkpoint and tell the user; never leave a half-written checklist as truth.

### After acknowledgment — inspect and resume

1. Inspect which of these local, gitignored artifacts already exist:
   `profile/profile.yaml`, `profile/employers.yaml`, `profile/person.md`,
   `profile/claims.md`, `profile/style.md`, and `profile/tracker.md`. Validate an
   existing `profile/profile.yaml` with `./careerkit.py profile-lint`; do not
   assume that presence means completion.
2. Reconcile the checklist with the artifacts. Resume the first `pending`
   required or selected phase. If artifacts existed without a checklist
   (including an older CareerKit instance), infer their status conservatively
   only now, after acknowledgment. `deferred` and `skipped` record deliberate
   choices for optional phases: do not automatically resume them during general
   setup, but keep them visible as coverage or capability gaps and reopen only
   when the user asks or a later workflow needs one.
3. Show the user a short status summary — Privacy, Search Core, First Win,
   Source Expansion, Career Pack, Final Checks — and say exactly what will be
   asked or written next. Ask before continuing.
4. If the user asks to restart, redo a completed phase, or existing files would
   be replaced, summarize the current artifacts and proposed replacements, then
   obtain explicit confirmation before restarting or overwriting. Never delete
   or overwrite existing profile artifacts merely because `/setup` was invoked.

## Phase 1 — Search Core (required to search)

Keep this interview bounded. A resume, LinkedIn export, portfolio, brag document,
or other source document is optional here: offer to use one to propose criteria,
but let the user answer directly or defer documents.

Every question needs an explicit supported destination. Name it before asking;
if an answer has no field or artifact below, do not collect it as though it will
affect scoring.

1. Learn only what scoring and sourcing can represent, using this destination
   map:
   - role families and title variants -> `lanes` or `dream_lanes`; feed query
     phrases -> `search_terms`
   - US remote, target metros, and relocation -> `location`; compensation
     screen and accept floors -> `comp`
   - named dream or excluded employers -> `dream_companies` or
     `exclusions.companies`; do not ask for generic industry, company-size, or
     stage preferences as if a dedicated scoring field exists
   - product, refused-certification, clearance, quota, and competing-platform
     boundaries -> the matching supported key under `exclusions`
   - a travel or skill phrase that disqualifies only when the posting requires
     it -> `hard_requirements`; use `exclusions.body_patterns` only when the user
     explicitly intends a full-posting phrase gate and accepts its broader
     false-positive risk
   - must-mention evidence -> `domain_terms`; positive evidence that should
     nudge rather than gate -> `signals`
2. Separate title exclusions explicitly. Put titles the user would prefer to
   avoid in `exclusions.titles`; a dream company may waive those. Put role
   families the user cannot credibly do in `exclusions.titles_always`; no dream
   company ever waives those. Explain the distinction with the user's proposed
   titles before writing either list.
3. Propose lanes, title patterns, the two exclusion lists, and domain terms. Show
   the user the plain-English effect of each gate and revise until they approve
   it. Preserve existing approved rules unless they explicitly approve a change.
4. Write the search-ready fields in `profile/profile.yaml` using
   `profile.example/profile.yaml` as the schema: `location`, `comp`, `lanes`,
   `dream_lanes`, `exclusions`, `hard_requirements`, `domain_terms`, `signals`,
   `dream_companies`, and `search_terms`. Set `autonomy.submit: ask_each`;
   CareerKit has no batch, session-wide, or standing submission permission.
5. Run `./careerkit.py profile-lint`. Resolve failures with the user; report
   warnings without silently inventing criteria.
6. Tell the user: **Search Core is ready. Career Pack is not required to search.**
   Mark Search Core complete in the progress checklist.

## Phase 2 — First Win (optional and bounded)

Offer one bounded first-results run now. Before running it, say that it makes
fresh network requests only to the currently active feeds, does not add employer
boards, does not activate Freehire, and cannot represent complete job-market
coverage. Ask for explicit confirmation; skipping it does not undo Search Core.
Show the active feed names before asking. If Freehire is already `active: true`,
repeat its disclosure and obtain separate confirmation to include it before any
pull; First Win consent alone is insufficient. If that confirmation is declined,
do not silently edit the registry or run a feed pull that includes Freehire.

If approved, run exactly:

```text
./careerkit.py pull --feeds --no-cache
```

Show the first shortlist from that report, or say explicitly that nothing
qualified. Always name failed, partial, capped, dormant, or skipped sources and
use this language: **these are results from the active configured feeds, not a
complete search; every source gap limits the conclusion.** Do not immediately
run `/search` and repeat the same fetch.

If the user separately asks to activate Freehire, repeat its exact disclosure
from the Privacy section and obtain a separate explicit confirmation before
changing Freehire from `active: false` to `active: true`. Never infer Freehire
activation from approval of this feed-only run. Mark First Win `complete` or
`skipped` after the decision.

## Phase 3 — Source Expansion and calibration

1. Ask for a target-company list, but permit an empty list or deferral. If they
   provide one, keep the working list under `profile/`, run
   `./careerkit.py discover --file <profile-list>`, then
   `./careerkit.py verify`. Explain that discovery and verification make
   outbound requests and that supported-but-unrepresented ATS families remain
   coverage gaps, not proof of a complete market search.
2. Run the configured-source pull with the normal cache so a just-completed First
   Win is not fetched again unnecessarily. The separate Freehire confirmation
   rule still applies if it is active:

   ```text
   ./careerkit.py pull
   ./careerkit.py audit
   ```

3. Walk the user through representative kills and their reasons. After each
   approved criteria correction, run `profile-lint`, `rescore`, `pull`, and `audit`
   in that order until the user accepts the calibration. `pull` remains
   required because postings excluded before storage cannot be recovered by
   `rescore` alone.
4. Mark Source Expansion complete, or deferred if the user knowingly stops with
   feed-only coverage. A deferral is a gap to report, not a reason to block
   searching.

## Phase 4 — Career Pack (optional until a later workflow needs it)

Explain what each item enables, then ask whether to complete it now or defer it.
Searching, ingesting, evaluating, changing criteria, and auditing remain
available when this phase is deferred.

If completing it:

1. Ask for their resume only if they choose to complete this phase, and read it
   visually. Also optionally read a LinkedIn export or pasted text,
   portfolio/GitHub links, brag documents, reviews, and 2-3 real writing samples.
   Interview past the documents: what they actually did; quantified work they
   are proud of; what they will not do again; underclaimed strengths; and useful
   situation/action/result stories.
2. Name the destination and scoring effect before each question:
   - verified facts, quantified stories, and "never say" boundaries ->
     `profile/claims.md`; these constrain compose/prep/apply and do not score jobs
   - voice rules and approved writing samples -> `profile/style.md`; these shape
     drafts and do not score jobs
   - working context and later-stage constraints such as leave windows or notice
     period -> `profile/person.md`; this is human/application context and does
     not score jobs
   - identity -> `profile.yaml.identity`, work authorization/sponsorship/visa ->
     `profile.yaml.work_auth`, and voluntary answer-or-decline EEO choices ->
     `profile.yaml.eeo`; these are validated application fields, not scorer inputs
   - pipeline sections and later outcomes -> `profile/tracker.md`
   If volunteered context has no documented schema field or artifact, explain
   that and do not imply it changes scoring. Route a desired scoring change back
   through `/criteria` and a supported Search Core field.
3. Write `profile/person.md`, `profile/claims.md`, and `profile/style.md`.
   `claims.md` contains verified facts only, each confirmed by the user, plus a
   "never say" boundary list. No resume line, answer, outreach message, or story
   may use an unconfirmed claim.
4. Collect the approved truthful application fields above. Add only those
   documented schema fields to `profile/profile.yaml`; preserve
   `autonomy.submit: ask_each`.
5. Initialize `profile/tracker.md` with empty APPLIED, INTERVIEWING, SEEN, and
   RULED-OUT sections if it does not exist. Never overwrite an existing tracker.

If deferred, say which command will need which missing material: `/compose`
needs confirmed claims and voice samples, `/prep` needs the story bank, and
`/apply` needs confirmed claims plus identity/work-auth/EEO choices. Resume only
the needed Career Pack work at that time. The `Career Pack` checkpoint is an
umbrella, not evidence that every artifact is complete: inspect the named
destinations, fill only what that workflow needs, and leave it `deferred` until
all Career Pack destinations are complete. Never re-ask a completed destination:
for example, after `/compose` has completed claims and voice, a later `/apply`
asks only for missing application fields. Mark it `complete` only when every
destination is complete.

## Phase 5 — Final Checks and handoff

Run these in order. A warning or failure becomes a named gap; never silently
repair state or describe setup as fully healthy:

```text
./careerkit.py db check
./careerkit.py consistency
./careerkit.py coverage
./careerkit.py doctor
```

Mark Final Checks complete after all four ran, even if the handoff must name
problems. Finish with one compact status report containing:

1. **First shortlist:** the strongest roles from the latest report, or an
   explicit statement that none qualified.
2. **Coverage:** which configured sources completed and every failed, partial,
   capped, dormant, skipped, or deferred source/expansion gap.
3. **Deferred work:** Career Pack items, employer expansion, or optional feeds
   still deferred; never call deferred work complete.
4. **Local state:** which profile artifacts now exist and the deletion boundary.
5. **Exact next action:** one command suited to the state, normally
   `/evaluate <url>`, `/search` on a later cadence, or `/setup` to resume a
   deferred phase. State that the First Win already counted as the first search
   and should not be repeated immediately.
