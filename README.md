<img src="brand/careerkit-mark.svg" alt="CareerKit" width="360">

**A job search engine you operate by talking to it.** Against one set of
criteria it read 19,550 postings and surfaced 161.

It watches the job boards employers actually post on, 17 applicant tracking
platforms plus 14 public feeds (8 of them live immediately; 4 want an API key
you register for, 2 are scrapers and off by default), and scores every posting
against rules you wrote yourself. Then it helps you evaluate roles, build applications, prepare for
interviews, and keep track of where everything stands.

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
  request per host per 0.7s. Your identity is not attached to them.
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

Two employer platforms are also read from HTML rather than an API, because they
publish no JSON: **iCIMS** parses its server-rendered search page, and
**Jobvite** parses its careers page. Unlike the two feeds above, these are active
whenever you have an employer registered on that platform. If that matters to
you, deactivate those entries in `profile/employers.yaml`.

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
./careerkit.py rescore       # re-judge stored postings after a criteria change
./careerkit.py doctor        # one check: profile, sources, freshness, drift
./careerkit.py status        # what is in the database, which sources are broken
./careerkit.py audit         # show what the gates killed, and why
./careerkit.py report        # regenerate the latest report
./careerkit.py report --format csv    # export the rows for a spreadsheet
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

`doctor` is the one to run if something feels off. It checks that your profile
parses, that no source has been failing repeatedly, that the last run finished and was recent, and that your database
and `tracker.md` agree about what you have applied to.

`claims-lint` is a mechanical backstop for the truth rule: it flags numbers and
proper names in a draft that do not appear in `profile/claims.md`. It reads
tokens, not meaning, so a clean result is not a certification that a document is
accurate. It catches the careless cases. Keep reading the draft.

`./run-tests.sh` runs the regression suite. Nearly every test in it locks down a
bug that actually shipped, so it is worth keeping green if you change the engine.
It also runs four adapters (Greenhouse, Lever, Ashby, SmartRecruiters) against
saved real payloads, so those boards changing their JSON shape fails a test
rather than quietly returning nothing. The other thirteen have no fixture yet,
which is the most useful contribution anyone could make.

`./careerkit.py status` additionally reports where the database and your
`tracker.md` disagree: an application recorded in one but not the other. Those
two do drift, and a role missing from the database resurfaces later as a fresh
find you have already acted on.

To install it as an ordinary command instead (`careerkit pull`), run
`pip install -e .`.

## License

MIT. See [LICENSE](LICENSE).
