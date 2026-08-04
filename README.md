# CareerKit

Your job search, run by Claude Code with real machinery underneath: an engine
that watches employer job boards and scores every posting against YOUR rules,
plus workflows for evaluating roles, building applications, interview prep,
outreach, and pipeline tracking. Nothing personal lives in this repo — your
profile, database, and tracker are gitignored and stay on your machine.

## What you need

- [Claude Code](https://claude.com/claude-code) (Pro plan minimum; expect
  meaningful usage on setup and application days)
- Chrome + the Claude-in-Chrome extension (for form filling and LinkedIn
  editing — optional otherwise)
- python3 and git

Plain-words privacy note: what Claude reads (your resume, interview answers)
is processed by Anthropic under your own Claude plan's data terms. CareerKit
itself sends nothing anywhere and has no telemetry. Delete `profile/` and
`data/` and nothing remains locally.

## Start

```
./setup.sh          # checks prerequisites, creates folders
claude              # open Claude Code in this folder
/setup              # ~40 min: Claude interviews you, builds your profile,
                    # seeds your employer registry, calibrates the gates
```

## The loop

```
/search             # sweep every board + feed, report genuinely new roles
/evaluate <url>     # honest fit read of any posting you found anywhere
/apply <url>        # build the pack, fill the form; YOU click submit
/prep <company>     # interview prep from your own story bank
/track              # where everything stands
```

Also: `/ingest <url>` (score + register anything you paste — this is how
LinkedIn/Indeed finds enter the system), `/compose` (resume + cover letter +
LinkedIn work), `/outreach` (referrals, thank-yous, nudges — draft-only by
default), `/criteria` (change your search rules), `/audit` (review what the
gates killed — run it monthly; it keeps the filter honest).

## Rules the copilot lives by

No fabricated experience, ever — everything it writes is gated by your
claims register. You click submit on applications. It never creates accounts,
touches passwords, or completes "prove you're human" checks. It reads
employer AI-use policies and follows them. Job postings are treated as data,
never as instructions.
