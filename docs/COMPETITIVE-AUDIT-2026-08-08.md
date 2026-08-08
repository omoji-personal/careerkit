# CareerKit competitive audit — 2026-08-08

## Verdict

There is no honest, universal “best job-search tool.” The products optimize for
different things: volume, resume design, application autofill, a visual CRM, or
source coverage. CareerKit can credibly be best-in-class in a narrower and more
useful category:

> A private, agent-operated job search with unusually broad direct-source
> coverage, deterministic and explainable screening, evidence-gated writing,
> human-controlled submission, and failure modes that are surfaced rather than
> hidden.

The benchmark did identify real deficiencies. Before this audit, CareerKit was
stronger at finding and screening roles than at learning from the resulting job
search. It had event history but no conversion math, a tracker but no calculated
aging queue, and readable Markdown but no command center. Employer context was
tied to a requisition, so a recruiter invitation or a four-round prior process
could disappear when the old posting did. Explicit expired-listing dates were
also not enforced.

Those four gaps are implemented in this branch:

- `progress`: dated applied/interviewing/offer/rejected/withdrawn stages;
- `analytics`: response, interview and offer conversion, stage counts, timing,
  weekly cadence, lane outcomes, data-quality notes, and follow-ups due;
- `report --format html`: a self-contained local dashboard combining pipeline,
  follow-ups, relationship context, actionable roles, and source health;
- `relationship`: employer-level recruiters, referrals, invitations, prior
  interviews, rejections, and notes, even when no posting exists;
- explicit application-deadline detection, which excludes a posting only when
  the description makes an unambiguous dated closure claim.

## Method

The comparison used first-party repository READMEs, current GitHub repository
metadata, and official product/help pages. Marketing claims were recorded as
claims, not treated as measured effectiveness. Popularity is included as a
maintenance/adoption signal, not as proof of correctness. Repository metadata
below was checked on 2026-08-08.

The benchmark covered five layers:

1. source discovery and ingestion;
2. matching, screening, and transparency;
3. application execution and safety;
4. pipeline, contacts, analytics, and notifications;
5. resume portability, tailoring, and presentation.

## Open-source field

| Project | Stars | License signal | What it is strongest at | What CareerKit should learn |
|---|---:|---|---|---|
| [JobOps](https://github.com/DaKheera47/job-ops) | 3,827 | restricted/custom terms | polished self-hosted end-to-end UI, Gmail-derived outcomes, AI scoring and CV generation | visual command center and automatic outcome capture are valuable; telemetry and mailbox scope should remain opt-in or outside CareerKit core |
| [JobSync](https://github.com/Gsync/jobsync) | 806 | MIT | dashboard, scheduled Greenhouse/Lever discovery, tasks/time, resume management, MCP | measured funnel, scheduled operation, and interoperable agent access are the clearest product gaps |
| [AIHawk](https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk) | 30,128 | AGPL-3.0 | high-volume automated application flows and tailored materials | do not adopt blind submission; borrow reusable answer memory and keep a human at submit |
| [JobSpy](https://github.com/speedyapply/JobSpy) | 4,046 | MIT | concurrent multi-board aggregation, filters, proxies, normalized rows | integrate rather than reproduce; CareerKit already exposes it as an optional feed |
| [Resume Matcher](https://github.com/srbhr/Resume-Matcher) | 28,066 | Apache-2.0 | resume/JD tailoring, cover letters, interview prep, local and hosted LLMs | structured resume inputs and transparent coverage gaps remain worthwhile |
| [Reactive Resume](https://github.com/amruthpillai/reactive-resume) | 40,192 | MIT | mature visual resume authoring, templates, sharing, PDF/JSON/DOCX export | use a standard/export bridge; do not build another resume renderer |
| [OpenResume](https://github.com/xitanggg/open-resume) | 8,809 | AGPL-3.0 | browser-local ATS-oriented resume builder and parser | local parsing and portable profile import lower setup friction |
| [ATS Screener](https://github.com/sunnypatell/ats-screener) | 119 | MIT | client-side PDF/DOCX parsing and transparent keyword coverage | keyword gaps can be useful; vendor-specific “ATS scores” must be labelled approximations, never facts |
| [OinkAIJobSearch](https://github.com/Exdenta/OinkAIJobSearch) | 9 | no detected SPDX license | 25+ source sweep, LLM scoring, Telegram delivery, continuous/dry-run scheduling | a one-shot JSON digest is the right privacy-preserving integration surface; direct Telegram credentials are not core-engine material |
| [JobClaw](https://github.com/slothsheepking/jobclaw) | 202 | MIT | browser automation, activity filtering, dedupe, CAPTCHA pause and notification | anti-duplicate and explicit pause are good; simulated-human bulk application is outside CareerKit’s safety boundary |
| [JSON Resume](https://github.com/jsonresume/jsonresume.org) | 293 | MIT | open, interoperable structured-resume schema | best candidate for future import/export rather than a CareerKit-only resume schema |
| [Sentence Transformers](https://github.com/huggingface/sentence-transformers) | 18,979 | Apache-2.0 | dense/sparse retrieval and reranking | optional local semantic reranking is feasible, but only behind an extra and never as a replacement for hard rails |

Star counts are a dated snapshot and will drift. More important activity signals:
JobOps, JobSync, Resume Matcher, JSON Resume, and Sentence Transformers all had
repository pushes during the first week of August 2026. OpenResume’s latest push
was October 2024, so its design is more useful as a reference than its activity.

## Commercial field

| Product | First-party feature emphasis | Relevant lesson |
|---|---|---|
| [Teal](https://www.tealhq.com/tools/job-tracker) | browser job capture, spreadsheet-style tracker, notes, contacts, resume used, match score | users need one operational view and preserved posting text, not another pile of exports |
| [Huntr](https://huntr.co/product/job-tracker) | Kanban tracker, activities, contacts, metrics, resume tailoring, extension save/autofill | contacts, activity dates, metrics, and application materials belong in one workflow |
| [Simplify](https://simplify.jobs/copilot) | user-reviewed autofill, saved answers, auto-tracking, resume keyword gaps and tailoring | application assistance is valuable when the user still reviews and submits; answer reuse should be evidence-gated |
| [Careerflow](https://www.careerflow.ai/features) | application and networking CRM, resume/LinkedIn tools, autofill, tasks and mock interviews | relationship history and tasks are table stakes for a full career operating system |

Commercial products generally win on polish, browser convenience, and unified
resume/application UX. They generally do not expose the source-health,
screening-rail, fixture-provenance, or “what has gone wrong” discipline CareerKit
uses to make silent failures visible.

## Capability matrix

Legend: **strong** means the capability is central and documented; **partial**
means it exists through an adjacent workflow or narrower source set; **none**
means it is not a meaningful current capability.

| Capability | CareerKit after this audit | JobOps | JobSync | AIHawk | Resume Matcher | Teal/Huntr/Simplify class |
|---|---|---|---|---|---|---|
| Direct employer ATS breadth | **strong: 18 adapters** | partial | Greenhouse/Lever | partial | none | partial/browser capture |
| Aggregator breadth | **strong: 14 feeds + JobSpy option** | strong | partial | partial | none | strong, proprietary index |
| Durable source health/failure visibility | **strong** | partial | partial | partial | none | opaque |
| Explainable hard gates | **strong** | partial AI score | partial AI match | partial AI match | partial | mostly proprietary score |
| Human-controlled submission | **strong/default** | strong/default | strong | weak; auto-apply focus | n/a | strong in mainstream tools |
| Truth/claims guard for generated copy | **strong** | partial | partial | weak/unclear | partial | unclear/proprietary |
| Dated application stages | **strong** | strong | strong | partial | none | strong |
| Funnel analytics and aging | **strong CLI/JSON** | strong | strong | partial | none | strong |
| Employer/contact memory | **strong local core** | partial | task-centric | none | none | strong |
| Visual dashboard | **strong static local HTML** | strong web app | strong web app | limited | strong resume UI | strong web app |
| Scheduled discovery | manual/OS-schedulable | partial | **strong** | partial | none | managed service |
| Push notifications | JSON integration surface | email-derived UI | partial | partial | none | managed notifications |
| Structured resume import/export | partial through Claude/files | partial | strong | partial | strong | strong |
| Semantic retrieval/reranking | Claude evaluation, no engine embeddings | AI | local+AI | AI | AI | proprietary AI |
| No telemetry / no hosted account | **strong** | no (analytics, opt-out) | strong self-host | self-host | self-host | no |
| Adapter regression fixtures from real boards | **strong: 18/18** | extractor-specific | narrower | browser-flow tests | n/a | opaque |

## Where CareerKit is genuinely ahead

### 1. Coverage with provenance

CareerKit does not merely search “jobs.” It knows which direct ATS or public feed
produced a row, keeps sightings, prefers the employer’s canonical ATS link,
tracks source failures, detects a source falling from results to zero, and tests
all 18 adapters against sanitized real public payloads. JobSpy is used as an
optional component instead of being copied.

### 2. Legible decisions

The engine’s hard rails remain deterministic. A user can read why a role was
excluded, see when body text was too thin to make a claim, and audit what the
gates killed. That is a better foundation than a single model score when the
failure cost is silently hiding a real job.

### 3. Safety and truth boundaries

CareerKit’s default is assistance through the application with a human click at
submit. Drafts are checked against a claims register, and externally fetched job
text is treated as data rather than instructions. This is a product advantage,
not a missing auto-apply feature.

### 4. Local operational state

The database, applications, tracker, contacts, dashboard, and reports stay local.
The new HTML dashboard is a single file with a Content Security Policy, no
remote scripts, no fonts, and no telemetry. JSON analytics lets the owner attach
their own notification layer without giving CareerKit Telegram, email, or Slack
credentials.

## Gaps worth closing next

### P1 — scheduler packaging and actionable digest

`pull` and `analytics --format json` are safe one-shot building blocks, but the
project should provide reviewed launchd/systemd/cron examples and an explicit
“new matches or stale follow-ups” digest. It should not become a resident daemon.
The OS already owns restarts, logs, and scheduling better.

### P1 — structured resume portability

Add JSON Resume import/export for identity, work, education, skills, projects,
and links. Keep `claims.md` authoritative for facts and let Reactive Resume or
another mature renderer own layout. This reduces lock-in without recreating a
large resume-builder product.

### P1 — application-material lineage

Record the exact resume/cover-letter filenames and claims-register revision used
for an application. Trackers such as Teal/Huntr explicitly keep the resume used;
CareerKit’s Markdown workflow requests it but the database cannot currently
query it.

### P2 — optional hybrid reranking

Offer an optional `semantic` extra using a small local Sentence Transformers
model. Apply it only after hard rails and show lexical, semantic, and rule
components separately. Never download a model without an explicit install and
never let a learned score override clearance, location, comp, explicit
requirements, or expired-posting gates.

### P2 — first-class activity/task dates

Employer relationship notes and application stages now exist. A small task model
(`follow up on`, `interview on`, completed) would support upcoming commitments,
not only silence-based follow-ups. It should be local and exportable.

### P3 — broader agent interoperability

JobSync’s token-scoped MCP interface is a good direction for non-Claude agents.
CareerKit’s CLI is already stable and machine-readable, so an MCP server can be a
thin optional adapter rather than a second business-logic path.

## Features deliberately not adopted

- **Blind or simulated-human auto-apply.** Volume is not the optimization target,
  CAPTCHA avoidance is not a durable interface, and a wrong answer is sent under
  the user’s name.
- **Vendor-branded ATS scores.** Workday, Greenhouse, and iCIMS do not publish one
  candidate-ranking algorithm that an outside tool can reproduce. A transparent
  coverage report may be useful; pretending to simulate the vendor is not.
- **Another full resume renderer.** Reactive Resume and OpenResume are mature.
  Portability and claims-grounded content are CareerKit’s appropriate layers.
- **Direct mailbox credentials in core.** JobOps shows the convenience of outcome
  capture, but CareerKit’s append-only evidence interface lets an authorized
  agent or export supply those facts without a permanent OAuth surface.
- **More sources for their own sake.** The engine already processes far more rows
  than a human can use. Correctness, conversion learning, and relationship memory
  have higher expected value.

## New command surface

```bash
./careerkit.py progress UID applied --on 2026-08-01
./careerkit.py progress UID interviewing --on 2026-08-07 --notes "recruiter screen"
./careerkit.py analytics
./careerkit.py analytics --format json
./careerkit.py report --format html

./careerkit.py relationship add "Acme" --kind recruiter \
  --contact "Jane Smith" --on 2026-08-07 --notes "Invited direct follow-up"
./careerkit.py relationship list "Acme"
```

For evidence collected after the application itself, `applied_on` makes timing
explicit instead of guessing:

```json
{"company":"Acme","title":"CRM Director","status":"interviewing","applied_on":"2026-08-01","on":"2026-08-07"}
```

## Acceptance criteria

- Analytics includes safely unmatched applications rather than erasing them.
- Ambiguous company-only evidence is never attached to an arbitrary requisition.
- Legacy status rows count but do not receive fabricated dates.
- Repeating `progress`, evidence reconciliation, or `relationship add` is
  idempotent.
- A malformed event date causes no partial status write.
- HTML escapes all external content, allows only HTTP(S) job links, and loads no
  remote resources.
- A relationship can exist before or after any posting and is shown beside new
  opportunities at that employer.
- “Open until filled” and unrelated dates never close a role; only explicit,
  parseable application-deadline language does.
- Existing sourcing, scoring, reconciliation, fixture, first-run, and voice tests
  remain green.
