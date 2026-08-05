# CareerKit

Your job search, run by Claude Code with real machinery underneath: an engine
that watches employer job boards and scores every posting against YOUR rules,
plus workflows for evaluating roles, building applications, interview prep,
outreach, and pipeline tracking. Nothing personal lives in this repo. Your
profile, database, and tracker are gitignored and stay on your machine.

It is built for people who are not programmers. You talk to Claude; Claude runs
the machinery.

## What you need

- [Claude Code](https://claude.com/claude-code) (Pro plan minimum; expect
  meaningful usage on setup and application days)
- Python 3.10 or newer, and git
- Chrome + the Claude-in-Chrome extension, only if you want it filling forms
  and editing LinkedIn for you

macOS and Linux work out of the box. On Windows, run the shell scripts from Git
Bash or WSL.

## Start

```
./setup.sh          # checks prerequisites, creates a .venv, installs deps
claude              # open Claude Code in this folder
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
all live there. To change what surfaces, change that file, usually by asking
Claude to run `/criteria`. Never by editing engine code.

If a rule you wrote is unusable (an empty list item, a broken regex) the engine
prints what it ignored and carries on. It will not silently match everything or
match nothing, which is the failure that costs you jobs without telling you.

## What actually leaves your machine

Worth being precise about, because "nothing" would be a lie.

- **To Anthropic:** everything Claude reads. Your resume, your claims register,
  postings, your interview answers. Governed by your own Claude plan's data
  terms.
- **To job boards and aggregators:** CareerKit fetches from employer ATS
  platforms (Greenhouse, Lever, Ashby, Workday and others) and public job feeds.
  These are ordinary outbound web requests from your machine, throttled to one
  request per host per 0.7s. Your identity is not attached to them.
- **One exception, if you enable it:** the USAJobs feed requires your registered
  email in the request header. That is their API's rule, not a CareerKit choice.
  Skip that feed if you would rather not.
- **`/ingest` and `/evaluate`** fetch the URL you hand them. That is a request to
  whoever hosts it.
- **No telemetry.** Nothing is reported back to the author or anyone else.

Delete `profile/`, `data/` and `out/` and nothing personal remains locally. (`out/` holds your generated reports, which name the roles you were matched to.)

**On scraping:** two feeds (`linkedin_guest`, `jobspy`) read public search pages
rather than official APIs. They are off by default and can be rate-limited or
blocked. Enable them knowingly.

## Rules the copilot lives by

No fabricated experience, ever. Everything it writes is gated by your claims
register. You click submit on applications. It never creates accounts, touches
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

## Running the engine directly

Claude does this for you, but it is a normal CLI:

```
./careerkit.py pull          # poll every board + feed, score, write a report
./careerkit.py status        # what is in the database, which sources are broken
./careerkit.py audit         # show what the gates killed, and why
./careerkit.py report        # regenerate the latest report
./careerkit.py mark UID applied
./careerkit.py ingest-url -- "https://boards.greenhouse.io/acme/jobs/1"
./careerkit.py pull --no-cache      # really re-fetch, ignoring the 6h cache
```

`./run-tests.sh` runs the regression suite. Every test in it locks down a bug
that actually shipped, so it is worth keeping green if you change the engine.

## License

MIT. See [LICENSE](LICENSE).
