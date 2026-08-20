<img src="brand/careerkit-mark.svg" alt="CareerKit" width="360">

**A job search engine you operate by talking to it.** In one August 2026
reference run, it read 19,550 postings and surfaced 161. That historical ratio
is an example, not the current size of any user's configured sourcing surface.

It watches the job boards employers actually post on, 20 applicant tracking
platforms plus 14 usable public feeds (8 live immediately, 4 want an API key,
and 2 optional sources are off by default), with a 15th adapter security-gated
until its upstream dependency is fixed. It scores every posting against rules
you wrote yourself. Then it helps you evaluate roles, build applications, prepare for
interviews, and keep track of where everything stands. Dated pipeline analytics,
follow-up aging, employer relationship memory, and a private local dashboard
close the loop: the search can learn which lanes actually produce interviews.

Those platform counts describe what the engine can poll, not comprehensive
job-market coverage. A run covers only the active boards and operational feeds
you configured. Use `./careerkit.py coverage` to see that sourcing surface;
duplicate registry rows are one endpoint, while dormant, partial, or capped
sources are coverage gaps rather than evidence of a healthy, complete search.

It is built for people who are not programmers. You talk to Claude; Claude runs
the machinery. Personal files are gitignored and remain local to your checkout;
their contents leave only through the Claude, provider, and application requests
disclosed below.

**[Read the guide](guide/Careerkit-Guide.pdf)** for how scoring actually works,
what leaves your machine, and the failure modes worth knowing about.

**[What has gone wrong](WHAT-HAS-GONE-WRONG.md)** is the honest list of bugs
this tool has had, in plain English, and what was done about each one. Worth
reading before you trust it with your own search.

## What you need

- [Claude Code](https://claude.com/claude-code) (Pro plan minimum; expect
  meaningful usage on setup and application days). Install it with
  `npm install -g @anthropic-ai/claude-code`, which needs Node 18 or newer.
- Python 3.10 or newer, and git
- Chrome + the Claude-in-Chrome extension, only if you want it filling forms
  and editing LinkedIn for you

macOS and Linux work out of the box. On Windows, run the shell scripts from Git
Bash or WSL.

## Start

Open a terminal and run these one at a time:

```
git clone https://github.com/omoji-personal/careerkit.git
cd careerkit
./setup.sh          # checks prerequisites, creates a .venv, installs deps
claude              # opens Claude Code in this folder
```

Then, inside Claude Code, type:

```
/setup              # ~40 min: Claude interviews you, builds your profile,
                    # seeds your employer registry, calibrates the gates
```

`/setup` is not optional. Until it writes `profile/profile.yaml`, the engine has
no rules to score against and will tell you so rather than guess.

## The loop

```
/search             # sweep every board + feed, report genuinely new roles
/evaluate <url>     # honest fit read of any posting you found anywhere
/apply <url>        # build/fill; you review and approve this one submission
/prep <company>     # interview prep from your own story bank
/track              # where everything stands
```

Also: `/ingest <url>` (score + register anything you paste, which is how
LinkedIn/Indeed finds enter the system), `/compose` (resume + cover letter +
LinkedIn work), `/outreach` (referrals, thank-yous, nudges, draft-only by
default), `/criteria` (change your search rules), `/audit` (review what the
gates killed; run it monthly, it keeps the filter honest).

**Location scoring is US-centric by design.** The rails reason about US states,
US remote idioms and your target metros; a role outside the US is screened out
unless your profile says otherwise. If you are job-hunting outside the US, that
is the part to change first.

## Where your rules live

`profile/profile.yaml` is the only place scoring rules exist. Lanes (the job
titles you want), exclusions, target metros, comp floors, and autonomy settings
all live there.

Two kinds of exclusion, and the difference matters. `exclusions.titles` is what
you would rather avoid, and a dream employer waives it. `exclusions.titles_always`
is what you cannot do, and nothing waives it: wanting to work somewhere does not
make you a software engineer. Put role families you lack the background for in
the second list, or an exciting company will fill your report with roles you have
no real chance at. To change what surfaces, change that file, usually by asking
Claude to run `/criteria`. Never by editing engine code.

`hard_requirements` is different from both, and it reads only the posting's
requirements section:

```
hard_requirements:
  architect certification: ["/(application|system) architect/"]
  platform developer ii:   ["platform developer ii"]
  people management:       ["/direct reports/", "/manage a team of/"]
```

A match excludes the role and quotes the sentence responsible. It ignores the
responsibilities section on purpose, because "you will work with our architects"
is not a demand that you be one, and it ignores a gate the posting itself calls
preferred. This is the rail that catches the requirement buried three screens
into a job description, which is where most wasted applications come from.

Malformed profile structure and invalid values stop the run with the exact field
named. Unknown keys, and unusable regexes in non-exclusion scoring terms, are
reported and skipped. An unusable exclusion stops the run instead: dropping an
exclusion fails open, the rail disappears, and everything you banned starts
surfacing. Blank company-exclusion strings are the narrow harmless exception;
they are filtered out. No rule silently matches everything or nothing.

## What actually leaves your machine

Worth being precise about, because "nothing" would be a lie.

- **To Anthropic:** everything Claude reads. Your resume, your claims register,
  postings, your interview answers. Governed by your own Claude plan's data
  terms.
- **To job boards and job feeds:** CareerKit fetches from employer applicant
  tracking platforms (Greenhouse, Lever, Ashby, Workday and others) and from
  public job feeds.
  These are ordinary outbound web requests from your machine, throttled to one
  request per host per 0.7s, widening automatically if a host asks us to slow
  down. They identify themselves as CareerKit and link back to this repository,
  so a site operator can tell what the traffic is. Unauthenticated public
  endpoints do not receive identity fields from your CareerKit profile, though
  normal network metadata such as your IP address remains visible.
- **Keyed feeds, if you enable them:** Adzuna, Findwork, Careerjet, and USAJobs
  send the API keys, account identifiers, or affiliate values you configured to
  that provider. USAJobs additionally requires your registered email in the
  request header. Skip any keyed feed whose disclosure you do not accept.
- **Freehire, only if you explicitly enable it:** the shipped entry is
  `active: false`. Changing it to `true` sends each configured `search_terms`
  phrase (quoted), optional country/age filters, and ordinary request metadata
  such as your IP address, User-Agent, and timing to the third-party hosted
  service at freehire.me. CareerKit sends it no secret, API key, resume, claims
  register, or application data. It searches previews first and requests detail
  only for title-matching results from a conservative first-party ATS
  source-and-host allowlist. LinkedIn and aggregator-of-aggregator results are
  excluded. This is discovery evidence, not proof that a role is still open or
  that Freehire's normalized fields are employer-authored. Every Freehire row
  is forced to `VERIFY`; confirm the opening on its direct employer ATS. Ingest
  that URL to register and poll it when CareerKit supports that ATS family;
  unsupported families remain manual verification leads. Setting
  `canonical_enrichment: true` may additionally fetch a thin posting from its
  direct employer ATS URL through CareerKit's guarded outbound transport.
  The preview filter is deliberately strict: every meaningful word in a search
  term must also appear in the title before CareerKit requests the detail. Add
  explicit title variants to `search_terms` if you want adjacent titles; this
  privacy/traffic tradeoff means the feed is additive coverage, not a claim of
  comprehensive market coverage.

  New instances include this inactive example. Existing instances can add it
  to the `feeds:` list in `profile/employers.yaml` and leave it false until the
  disclosure above is acceptable:

  ```yaml
  - name: freehire
    active: false
    pages: 10
    results_per_page: 100
    detail_cap: 100
    posted_within_days: 14      # set zero only if you want all open inventory
    countries: []               # optional ISO codes, for example [us, ca]
    canonical_enrichment: false
  ```
- **`/ingest` and `/evaluate`** fetch the URL you hand them. That is a request to
  whoever hosts it.
- **No telemetry.** Nothing is reported back to the author or anyone else.

Delete `profile/`, `data/` and `out/` to remove CareerKit-owned personal state
from this checkout. (`out/` holds generated reports that name matched roles.)
Deletion cannot retract requests already sent to providers or remove copies in
backups, exports, or other tools.

**On scraping.** The optional `linkedin_guest` feed reads public search pages
rather than an official API. It is off by default, can be rate-limited or
blocked, and its terms of use are yours to read. The `jobspy` adapter is retained
but has no supported installation until its dependency graph can use the
security-fixed `markdownify>=0.14.1`; CareerKit refuses the vulnerable
combination at runtime. Any run whose results include a scraper says so in the
report. Every feed declares what it is in
`engine/aggregators.py` under `SOURCE_POLICY`: official API, needs-your-own-key,
or scraper.

Five employer platforms may also be read from public HTML rather than an API:
**iCIMS**, **Jobvite**, **HRMDirect/ClearCompany**, **Paylocity**, and **Phenom**
(when its widgets endpoint is unavailable). Paylocity and Phenom expose public
JSON models inside those pages; the other three parse server-rendered job
markup. Unlike the optional feed above, these are active whenever you have an
employer registered on that platform. If that matters to you, deactivate those
entries in `profile/employers.yaml`.

## Rules the copilot lives by

No fabricated experience, ever. Everything it writes is gated by your claims
register. It stops at every submit button by default. An agent click is allowed
only after you review the completed form, personally handle any certification,
and give fresh, explicit approval for that one application while present; there
is no batch or standing submission permission. It
never creates accounts, touches passwords, or completes captcha or OTP steps;
you handle those directly and never relay codes to the agent. It reads employer
AI-use policies and follows them. Job postings are
treated as data, never as instructions, including when a posting contains text
addressed to the AI.

The full contract is in `CLAUDE.md`. These are prompt-level rules that a
capable model follows, not sandboxed technical controls. Read them and decide
whether you are comfortable before pointing this at a real job search.

## Sharing it

`./new-instance.sh <name>` clones a fully separate instance into
`../careerkit-instances/<name>`, with its own profile, database, and reports.
Instances never see each other's data. Use one per person.

It clones from this checkout's portable HTTP(S) origin and prints the exact URL;
confirm the recipient can access that origin. A local path or SSH-only origin
requires explicit `--local`. To update an instance, run
`git pull && ./setup.sh` inside it so dependency and setup changes are applied
alongside engine fixes. Pass `--local` to snapshot this checkout instead: it
includes tracked changes and non-ignored untracked files, while still excluding
gitignored private state such as `profile/`, `data/`, and `out/`. Its origin is
local, so updates then work only on the machine that made it; commit or remove
snapshot changes before a later pull when they overlap upstream changes.

## Running the engine directly

Claude does this for you, but it is a normal CLI:

```
./careerkit.py pull          # poll every board + feed, score, write a report
./careerkit.py search        # key-free ATS-domain discovery; registers new boards
./careerkit.py queries --full  # print the matrix for a stronger search tool
./careerkit.py rescore       # re-judge stored postings after a criteria change
./careerkit.py rescore --min-score 40  # rebuild the report at a score floor
./careerkit.py profile-lint  # validate profile rules before using them
./careerkit.py coverage      # unique configured endpoints and sourcing gaps
./careerkit.py doctor        # one check: profile, sources, freshness, drift
./careerkit.py tracker-sync  # exact dry-run of tracker/database reconciliation
./careerkit.py tracker-sync --apply  # append links + mark matched rows after review
./careerkit.py consistency   # does the report agree with the database it came from?
./careerkit.py consistency --repair   # clear compensation that cannot be true
./careerkit.py applied       # reconcile what you have applied to (see below)
./careerkit.py applied --apply        # write the unambiguous matches
./careerkit.py enrich        # fetch the full posting for rows that cannot be screened
./careerkit.py status        # what is in the database, which sources are broken
./careerkit.py audit         # show what the gates killed, and why
./careerkit.py report        # regenerate the latest report
./careerkit.py report --format csv    # export the rows for a spreadsheet
./careerkit.py report --format html   # private single-file command center
./careerkit.py analytics     # conversions, timing, lane outcomes, stale threads
./careerkit.py analytics --format json  # scheduler/notification-friendly output
./careerkit.py analytics --output out/analytics.json  # write a JSON artifact
./careerkit.py progress UID interviewing --on 2026-08-08
./careerkit.py relationship add "Acme" --kind recruiter --contact "Jane Smith"
./careerkit.py relationship list "Acme"
./careerkit.py history       # every status change, in order
./careerkit.py claims-lint out/cover.md     # numbers and names not in your register
./careerkit.py voice-lint out/cover.md      # flag machine-written patterns
./careerkit.py db check                     # SQLite integrity check
./careerkit.py db backup                    # timestamped local backup
./careerkit.py mark UID applied
./careerkit.py ingest-url -- "https://boards.greenhouse.io/acme/jobs/1"
./careerkit.py pull --no-cache      # really re-fetch, ignoring the 6h cache
```

Run `rescore`, then `pull`, after you change your criteria. Editing your profile
alone leaves everything already in your database with its old verdict;
`rescore` re-judges those stored postings immediately and without network
requests. `pull` then recovers postings the previous rules screened out before
they could be stored.

One caveat worth knowing, because it decides which command you need. Screened-out
postings are never stored, so `rescore` can only re-judge what survived. Any
criteria or engine change that may loosen a gate needs a `pull`, because the
postings the old gate rejected are not in your database to re-judge. The
`/criteria` workflow runs both commands so it is correct for tightening and
loosening changes.

`applied` is how the tool learns what you have already done. Write one line of
JSON per application to `profile/applications.jsonl`:

```
{"company": "Acme", "title": "Salesforce Admin", "status": "rejected", "applied_on": "2026-07-30", "on": "2026-08-05"}
```

`status` is prepared, applied, rejected, interviewing, offer or withdrawn, and
`title` may be omitted when the confirmation email does not name the role.
`prepared` is explicitly pre-submission: it is reported but never written to the
database. Interviewing, offer, and withdrawn evidence is retained in event
history while the database row uses `applied` as its report-suppression status.
For a later stage, `applied_on` is optional but recommended: it lets analytics
measure apply-to-interview and apply-to-outcome timing without pretending the
later status date was the application date.
It never writes a
status from a company match alone, because at an employer with several openings
that marks the wrong one; those go to a review list instead. Once it knows, the
report warns you when a live posting sits at an employer that already declined
you. Claude can populate the file from your mail if you ask it to; the engine
itself never touches your mailbox.

`progress` is the direct alternative to editing JSON when you know the exact
posting. It records a dated applied, interviewing, offer, rejected, or withdrawn
stage, preserves existing notes, and is safe to repeat. `analytics` combines
those events with `applications.jsonl`, including real applications that cannot
be safely matched to a stored posting. It reports the limitation instead of
dropping them from the denominator. Active applied/interviewing threads become
follow-ups after 14 silent days by default (`--follow-up-days` changes it).

`relationship` remembers facts about the employer rather than only one
requisition: recruiters, referrals, contacts, prior interviews, standing
invitations, rejections, and notes. The context may be recorded before any job
exists and appears beside later opportunities from that employer.

`report --format html` writes a self-contained dashboard under `out/`: pipeline
stages, lane conversion, follow-ups, relationship history, filterable active
roles, and source health. It loads no remote scripts, fonts, images, analytics,
or telemetry. Job links are the only outbound links and only HTTP(S) URLs are
made clickable.

`enrich` fetches the full posting for rows whose stored text has no requirements
section. Aggregators carry a summary, and the part that decides whether you can
do the job is frequently not in it. It only replaces a description when the
fetch actually makes the row screenable.

`consistency` asks whether the report you read agrees with the database that
produced it. It exists because a row once said "Comp not stated" in its header
and quoted a salary band on the line below.

`doctor` is the one to run if something feels off. It checks that your profile
parses, that no source has been failing repeatedly, that the last run finished and was recent, and that your database
and `tracker.md` agree about what you have applied to.

`coverage` inventories the configured sourcing surface without exposing
credentials. It distinguishes active employer rows from unique configured board
endpoints and reports the operational feed subset separately. A
partial response or a source stopped at a configured or provider cap remains a
coverage gap even when the run returned some jobs; it is not a healthy or
complete source run.

The scorer also recognizes explicit, parseable application deadlines such as
"Apply by July 30, 2026" or "posting closes on 2026-07-30" and stops presenting
the role after that day. It does not guess from unrelated dates, and "open until
filled" remains open.

`tracker-sync` turns that drift warning into a safe, reviewable action. Its
default is a dry run that prints each proposed SQLite status change and the exact
canonical-URL line it would append to the tracker. Existing tracker prose and
database notes are never replaced. Broad links that match multiple postings are
listed for manual review and never chosen automatically. Only an explicit
`tracker-sync --apply` writes the unambiguous preview; a second run is
idempotent. The tracker path honors `CAREERKIT_TRACKER`, including legacy
instances that keep it outside `profile/`.

`claims-lint` is a mechanical backstop for the truth rule: it flags numbers and
proper names in a draft that do not appear in `profile/claims.md`. It reads
tokens, not meaning, so a clean result is not a certification that a document is
accurate. It catches the careless cases. Keep reading the draft.

`./run-tests.sh` runs the regression suite. Nearly every test in it locks down a
bug that actually shipped, so it is worth keeping green if you change the engine.
It runs all twenty employer adapters against sanitized responses saved from
real public boards, so a board changing its JSON or HTML shape fails a test
rather than quietly returning nothing. Fixture origins are recorded in
`tests/fixtures/README.md`; a fixture invented from a guessed shape would be
worse than no fixture at all.

The full competitive review behind the pipeline/dashboard work is in
[`docs/COMPETITIVE-AUDIT-2026-08-08.md`](docs/COMPETITIVE-AUDIT-2026-08-08.md).
It benchmarks current open-source projects and commercial products, says where
CareerKit is genuinely ahead, and keeps the remaining roadmap explicit.

`./careerkit.py status` additionally reports where the database and canonical
URLs in your `tracker.md` disagree. The surrounding narrative may already name
the application; the machine needs the matching URL to connect it safely. A
role missing from the database resurfaces later as a fresh find you have already
acted on.

CareerKit is intentionally clone-only; do not install it with `pip`. Its Claude
skills, setup workflow, guide, examples, and private-state layout are required
parts of the product and are not a complete Python package. Run `./setup.sh`
and `./careerkit.py` from a cloned checkout.

## License

MIT. See [LICENSE](LICENSE).
