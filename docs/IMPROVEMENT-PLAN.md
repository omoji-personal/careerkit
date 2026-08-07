# CareerKit improvement plan

Written 2026-08-06, after a full day of operating the tool on a live search and
finding nine defects in it. Everything below is grounded in something that
actually went wrong, not in a general sense that more tests would be nice.

Current state: 4,261 lines of engine, 828 of CLI, 2,062 of tests, 185 tests
passing in 0.45s, 4 of 17 adapters covered by fixtures.

The ordering is by expected value, not by difficulty. P0 items are ones where
the tool currently gives a confidently wrong answer.

---

## The pattern worth naming first

Every defect found today was silent. None crashed. The report kept its shape and
the numbers kept their formatting, and they were wrong. That is the failure mode
this codebase actually has, so both the enhancements and the tests below are
aimed at it rather than at robustness in the ordinary sense.

Second pattern, from the audit rounds: **every serious round-two defect was
introduced by a round-one fix.** That is an argument for invariant tests over
example tests, because an invariant survives a rewrite and an example does not.

---

# P0. The tool recommends things it should already know are dead

## 1. Ingest email as ground truth for application state

**The failure.** On 2026-08-06 the tool surfaced Delta "Business Technology
Product Owner (Salesforce)" as a top recommendation. The owner had applied on
7/30 and been rejected on 8/05, the day before. Separately, a Gmail audit found
roughly twenty applications in forty five days against six the database knew
about, and five with no trace in either the database or the tracker.

**Why it matters more than anything else here.** A job search tool whose central
promise is "surface genuinely new roles" cannot see the single most important
fact about a role: whether you already applied and were told no. Every wrong
recommendation costs the owner attention and, worse, credibility if he acts on it.

**Build.**
- `engine/mailsync.py`: read-only Gmail search for the confirmation and rejection
  shapes already observed. Confirmations: `"thank you for applying"`,
  `"we received your application"`, `"your application was sent to"`,
  `"application has been received"`. Rejections: `"not to move forward"`,
  `"will not advance"`, `"decided to move forward with other candidates"`,
  `"pursuing other candidates"`.
- Match to stored postings on `company` first, then fuzzy title. **Never
  auto-mark on a company-only match**: KnowBe4 and Delta matched cleanly, but
  "Smartsheet" and "OpenAI" confirmations name no role at all. Company-only hits
  go to a review queue, not to `status`.
- New command `./careerkit.py mailsync [--apply]`. Default prints the diff; the
  flag writes it. Same shape as the existing `--no-cache` discipline.
- Extend `doctor` to fail loudly when a QUALIFIED row belongs to a company with a
  rejection on file.

**Guard.** This reads the user's mail. It must be opt-in in `profile.yaml`
(`integrations.gmail: true`), documented in "What actually leaves your machine",
and it must never write to or send mail. Read-only scopes only.

**Effort.** One to two days. **Value: the highest in this document.**

## 2. Fetch the Qualifications section before scoring, not after

**The failure.** Four roles were recommended on their titles and died on their
requirements the same day: KIPP (needed Application Architect plus Platform
Developer II), Optomi (Microsoft Dynamics 365, not Salesforce at all), Nelson
Mullins and Axiom (Platform Developer II or ten years plus direct reports). The
aggregator rows that surfaced them carried a title, a company, a location and
sometimes a band. The disqualifier lived in a Qualifications block the row never
contained.

**Build.**
- `engine/jd.py`: for any row reaching QUALIFIED or VERIFY whose stored
  description is under ~1,500 characters, fetch the canonical posting and
  re-extract. The LinkedIn guest endpoint
  `linkedin.com/jobs-guest/jobs/api/jobPosting/<id>` worked reliably today and
  needs no auth.
- Add `profile.yaml` keys `hard_requirements.certs_absent` (e.g. Platform
  Developer II, any architect credential) and `hard_requirements.people_management`.
  A match inside a Qualifications block sets gate `EXCLUDED` with the quoted line
  as the reason, so the report shows *why* rather than just dropping it.
- Store `description_source` (`feed` or `canonical`) so the report can mark a row
  as scored on thin data.

**Effort.** Two to three days. **Value: removes the single most common class of
wasted recommendation.**

## 3. Detect postings that are not real, or no longer live

**The failures.** Tria Prima "Principal Salesforce Solution Consultant" scored 43
with the highest band on the board, $299-366K. The band was an hourly contract
rate annualised, the domain was parked for sale, and no such consultancy existed
in Atlanta. Separately, Slalom's Life Sciences CRM Architect was recommended on
2026-08-06 although the posting says "We will accept applicants until July 30,
2026". Nobody read the sentence.

**Build.**
- **Ghost score**, surfaced in the report rather than used to silently drop:
  aggregator-only provenance with no ATS URL (+2), no resolvable company website
  (+2), comp band exactly divisible by 2080 (+3, the annualised-hourly tell),
  employer absent from the registry and undiscoverable (+1). At 4 or more, mark
  the row UNVERIFIED EMPLOYER.
- **Expiry parsing**: regex for `accept(ing)? applications until <date>`,
  `apply by <date>`, `posting closes <date>`. Past date sets gate `EXCLUDED`
  with reason `posting closed <date>`.
- **Repost detection**: same `group_key` reappearing with a new posting date
  after the owner already applied. That is what Delta looked like.

**Effort.** One to two days.

---

# P1. Correctness of what it already claims

## 4. Report and database must agree, mechanically

**The failure.** A clean-clone first run produced a row reading `Comp not stated`
in its header and `comp $150,000-$208,000` in the reasons line directly beneath.
`score()` had resolved the band, used it to make the gate decision, and never
written it back. `report --format csv` exported an empty comp column for 55 of
418 rows.

Fixed today, but the class is not closed. **Nothing prevents the next field from
doing the same thing.**

**Build.** A `consistency` command, and a test, that walks every row in the
latest report and asserts each rendered value against the database it came from.
Any disagreement is a hard failure. This is cheap and would have caught the comp
bug, the `case-note review` mislabel, and the truncated screening reason.

**Effort.** Half a day. **Value: high, because it generalises.**

## 5. Make `rescore` and `pull` provably equivalent

**The failure.** `remote_flag` was set by twelve adapters and feeds, trusted by
`location_verdict`, and never stored. `rescore` rebuilt every job without it and
demoted genuinely remote roles to VERIFY. The README tells users to run `rescore`
after a criteria change, so following the documentation degraded results.

Fixed for `remote_flag`. **`rails_exempt` is still not persisted**, and is
re-derived only from `dream_companies`; a registry-level exemption is still lost.

**Build.** A property test: for a corpus of stored postings, `score()` on the
freshly-fetched Job and `score()` on the Job reconstructed from the database must
produce identical gate, score and reasons. Any field that fails the test is by
definition a missing column. This turns a whole bug class into an arithmetic check.

**Effort.** Half a day. **Value: high, same reason.**

## 6. Verify a discovered board actually belongs to the employer

**The failures.** SmartRecruiters answers `200 {"totalFound": 0}` for any slug,
which registered 27 employers that do not exist. Two-letter initials found a
Polish IT company for Fisher Phillips and a Belgian firm for DLA Piper. Today,
"National Public Radio" produced the candidate `national`, a live greenhouse
board belonging to a public-affairs firm in Toronto.

Each was patched individually. The underlying problem is that **discovery never
checks who answered.**

**Build.** After a probe succeeds, fetch one posting and compare its employer
name, and the locations of the first few roles, against the name being searched.
Mismatch means quarantine, not registration. `discover --review` prints the
quarantine for a human. This replaces three heuristics with one check that would
have caught all three.

**Effort.** One day. **Value: closes the class rather than the instance.**

## 7. Comp intelligence

Two live bugs came from comp parsing: a `$500 home office stipend` read as
$500,000, and an Indeed band of `$1.00 - $250,000.00 per year` annualised to
$520,000,000. Both are fixed. What is still missing is **judgement**:

- Warn when a QUALIFIED row's band tops out below `screen_floor` rather than
  waiting for a human to notice. NPR at $85,500-$105,000 against a $130,000 floor
  should never present as a clean match.
- Record comp provenance (`board field`, `parsed from body`, `absent`) so the
  report can distinguish "they said $120K" from "we guessed $120K".

**Effort.** Half a day.

---

# P2. Judgement and history

## 8. Remember the relationship, not just the posting

**The failure.** NPR was recommended as a fresh find. The owner had been through
four rounds there in 2025, including a stakeholder panel, and holds a written
invitation from the Talent Acquisition Manager to contact her directly. The tool
knew none of it, and would have sent him through the portal to a more junior,
lower-banded req.

**Build.** An `employer_history` table: applications, interview stages, named
contacts, outcomes, and any standing invitation. Populated by `mailsync` (item 1)
and by hand. Any row at a company with history gets a banner in the report:
`4 interview rounds 2025, declined, open invitation from csmith@npr.org`.

**Value.** This is the difference between a job board and something that knows
your search. It is also what turns a rejection into an asset.

**Effort.** One to two days, mostly after item 1 exists.

## 9. Lane debt

`crm-mgr` keyed on bare "CRM" and admitted a Microsoft Dynamics req. It is
patched, but no test asserts that a lane cannot admit a competing platform.

**Build.** A profile-level lint: for every lane, run a fixed adversarial corpus
of near-miss titles (Dynamics, HubSpot, ServiceNow, Zoho, SAP CRM) and fail if
any matches. Ships as `./careerkit.py profile-lint`.

**Effort.** Half a day.

---

# Testing strategy

185 tests in 0.45s is a good base and the suite is fast enough to run on every
save. The gaps are specific.

## T1. The thirteen uncovered adapters

4 of 17 have fixtures. The other thirteen can change their JSON shape and return
nothing, and no test fails. **This is the most-recommended contribution in the
README and remains undone.**

Capture one real payload per adapter, redact, commit as a fixture, assert that
parsing yields a posting with the fields the scorer requires. Mechanical, roughly
a day, and it converts thirteen silent failure modes into red tests.

## T2. Invariant tests over example tests

Today's audit produced this repeatedly: a fix satisfies its example test and
breaks a neighbour. Invariants that would have caught real bugs:

- Every value rendered in the report exists in the database (item 4).
- `score(fetched) == score(from_db)` for every stored row (item 5).
- No gate decision depends on a field that is not persisted.
- Every number in a generated document appears in `claims.md` (`claims-lint`
  already does a token version; make it structural).
- No lane admits a title containing a competing CRM platform (item 9).

## T3. A golden corpus, end to end

There is no test that runs the full chain: fixtures in, scoring, storage,
report out, and asserts the report text. Build one from ~200 redacted real
postings with expected gates. This is what catches "the report disagrees with the
run that produced it", which happened twice today.

## T4. Test the checkers

Three times today a check failed for the wrong reason: the publish gate matched
"kipp" inside "skipped", pyflakes was absent from the Python being used so the
lint silently passed as failure, and a consistency check regex ran on lines long
enough to hang. **A check that cannot fail correctly is worse than no check**,
which is already a stated theme of the project and is not yet applied to its own
tooling. Add fixtures that deliberately violate each gate and assert the gate
catches them.

## T5. First-run and upgrade paths

The clean-clone run is where three defects surfaced. Automate it: fresh clone,
`setup.sh`, example profile, `pull` against recorded fixtures, assert the report
is internally consistent and the messages are sane. Also test migration from an
old database, since `remote_flag` was added today and the next column will have
the same question.

---

# Suggested order

1. **Item 4 and 5** (consistency, and pull/rescore equivalence). Half a day each,
   and they close whole classes rather than instances.
2. **Item 1** (mailsync). Highest value in the document, and item 8 depends on it.
3. **T1** (thirteen fixtures). Mechanical, unblocks contribution.
4. **Item 2** (Qualifications fetch). Removes the most common wasted recommendation.
5. **Item 6** (discovery verification), **item 3** (ghost and expiry), **item 7**
   (comp judgement).
6. **Item 8** (employer history), **item 9** (lane lint), **T3-T5**.

Roughly two weeks of focused work for all of it; the first three items are about
four days and would have prevented most of what went wrong today.

# Deliberately not on this list

- **Rewrites.** Nothing here needs a framework or a schema overhaul. Every defect
  found today was a missing check, not a bad structure.
- **More sources.** Coverage is not the problem. Judgement is. Fourteen feeds and
  seventeen platforms already produce 19,856 postings a run; the tool's weakness
  is what it does with them.
- **Scoring sophistication.** No embeddings, no model-based ranking. The scoring
  rails are legible and debuggable, which is why every defect today was findable.
  A learned scorer would have hidden them.
