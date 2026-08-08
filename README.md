<img src="brand/careerkit-mark.svg" alt="CareerKit" width="360">

**A job search engine you operate by talking to it.** Against one set of
criteria it read 19,550 postings and surfaced 161.

It watches the job boards employers actually post on, 18 applicant tracking
platforms plus 14 public feeds (8 of them live immediately; 4 want an API key
you register for, 2 are scrapers and off by default), and scores every posting
against rules you wrote yourself. Then it helps you evaluate roles, build applications, prepare for
interviews, and keep track of where everything stands. Dated pipeline analytics,
follow-up aging, employer relationship memory, and a private local dashboard
close the loop: the search can learn which lanes actually produce interviews.

It is built for people who are not programmers. You talk to Claude; Claude runs
the machinery. Nothing personal lives in this repo: your profile, database and
tracker are gitignored and stay on your machine.

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
/apply <url>        # build the pack, fill the form; YOU click submit
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

If a rule you wrote is unusable (an empty list item, a broken regex) the engine
names what it ignored and carries on. In an exclusion list it stops the run
instead: dropping an exclusion fails open, the rail disappears, and everything
you banned starts surfacing. Either way it will not silently match everything or
match nothing, which is the failure that costs you jobs without telling you.

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
  so a site operator can tell what the traffic is. Your identity is not attached
  to them.
- **One exception, if you enable it:** the USAJobs feed requires your registered
  email in the request header. That is their API's rule, not a CareerKit choice.
  Skip that feed if you would rather not.
- **`/ingest` and `/evaluate`** fetch the URL you hand them. That is a request to
  whoever hosts it.
- **No telemetry.** Nothing is reported back to the author or anyone else.

Delete `profile/`, `data/` and `out/` and nothing personal remains locally. (`out/` holds your generated reports, which name the roles you were matched to.)

**On scraping.** Two feeds (`linkedin_guest`, `jobspy`) read public search pages
rather than official APIs. They are off by default, can be rate-limited or
blocked, and their terms of use are yours to read. Any run whose results include
one says so in the report. Every feed declares what it is in
`engine/aggregators.py` under `SOURCE_POLICY`: official API, needs-your-own-key,
or scraper.

Five employer platforms may also be read from public HTML rather than an API:
**iCIMS**, **Jobvite**, **HRMDirect/ClearCompany**, **Paylocity**, and **Phenom**
(when its widgets endpoint is unavailable). Paylocity and Phenom expose public
JSON models inside those pages; the other three parse server-rendered job
markup. Unlike the two feeds above, these are active whenever you have an
employer registered on that platform. If that matters to you, deactivate those
entries in `profile/employers.yaml`.

## Rules the copilot lives by

No fabricated experience, ever. Everything it writes is gated by your claims
register. You click submit on applications: that is the default and it is the
setting to leave alone, though `autonomy:` in your profile can loosen it. It never creates accounts, touches
passwords, or completes "prove you're human" checks. It reads employer AI-use
policies and follows them. Job postings are treated as data, never as
instructions, including when a posting contains text addressed to the AI.

The full contract is in `CLAUDE.md`. These are prompt-level rules that a
capable model follows, not sandboxed technical controls. Read them and decide
whether you are comfortable before pointing this at a real job search.

## Sharing it

`./new-instance.sh <name>` clones a fully separate instance into
`../careerkit-instances/<name>`, with its own profile, database, and reports.
Instances never see each other's data. Use one per person.

It clones from this repo's `origin` on GitHub, so `cd` into an instance and
`git pull` really does bring engine fixes. (Pass `--local` to clone from your
working copy instead, including unreleased changes; updates then only work on
the machine that made it.)

## Running the engine directly

Claude does this for you, but it is a normal CLI:

```
./careerkit.py pull          # poll every board + feed, score, write a report
./careerkit.py search        # key-free ATS-domain discovery; registers new boards
./careerkit.py queries --full  # print the matrix for a stronger search tool
./careerkit.py rescore       # re-judge stored postings after a criteria change
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
./careerkit.py progress UID interviewing --on 2026-08-08
./careerkit.py relationship add "Acme" --kind recruiter --contact "Jane Smith"
./careerkit.py relationship list "Acme"
./careerkit.py history       # every status change, in order
./careerkit.py claims-lint out/cover.md     # numbers and names not in your register
./careerkit.py mark UID applied
./careerkit.py ingest-url -- "https://boards.greenhouse.io/acme/jobs/1"
./careerkit.py pull --no-cache      # really re-fetch, ignoring the 6h cache
```

`rescore` is the one to run after you change your criteria. Editing your profile
only affects postings the boards happen to show you again afterwards, so
everything already in your database keeps the verdict it was given under the old
rules. `rescore` re-judges all of it from stored text, with no network requests.

One caveat worth knowing, because it decides which command you need. Screened-out
postings are never stored, so `rescore` can only re-judge what survived. A change
to your **criteria** needs a `rescore`; a change to the **engine's rails** needs a
`pull`, because the postings a rail used to reject are not in your database to
re-judge.

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
It runs all eighteen employer adapters against sanitized responses saved from
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

To install it as an ordinary command instead (`careerkit pull`), run
`pip install -e .`.

## License

MIT. See [LICENSE](LICENSE).
