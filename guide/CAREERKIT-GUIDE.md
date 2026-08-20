# CareerKit, the guide

**A job search engine you operate by talking to it.** It polls 20 employer
applicant tracking platforms directly plus 14 usable public job feeds (and one
security-gated dormant adapter), scores every posting against rules you wrote
yourself, and helps you evaluate, apply, prepare and track from there.

That is supported-platform capacity, not a claim of comprehensive sourcing. A
run reaches only the active employer boards and operational feeds you have
configured. `./careerkit.py coverage` separates configured rows from unique
endpoints and identifies dormant or capped gaps; partial or capped results are
not evidence that a source was healthy or complete.

In one August 2026 reference run it read 19,550 postings and surfaced 161. That
historical ratio is an example, not the current size of a user's sourcing
surface.

It is built for people who do not write code. You talk to Claude Code; Claude
runs the machinery.

**The complete guide is [Careerkit-Guide.pdf](Careerkit-Guide.pdf) in this
folder.** This file is the short version, and the map.

## What is in the PDF

1. **What it does.** The loop, the commands, the ratio.
2. **Setup.** Search Core first, resumable checkpoints, an optional first result,
   and the Career Pack that can bring complete onboarding to about 40 minutes.
3. **How scoring actually works.** Lanes, rails, gates, and the four verdicts:
   QUALIFIED, VERIFY, SLOT-BLOCKED, EXCLUDED. What each means and what to do
   about it.
4. **Where your rules live.** `profile/profile.yaml` is the only place scoring
   rules exist. How to change what surfaces without touching engine code.
5. **The daily loop.** `/search` across active configured sources, `/evaluate`,
   `/apply`, `/prep`, `/track`, and `/ingest` for anything found elsewhere.
6. **Calibration.** Why `/audit` exists, and why a filter you never review will
   quietly cost you jobs.
7. **What leaves your machine.** Precisely, including keyed-feed credentials,
   the USAJobs email header, and search terms sent only after the optional
   Freehire bridge receives separate activation confirmation.
8. **Failure modes worth knowing.** How the engine tells a dead board from a
   quiet one, and why zero results is a claim that has to be earned.
9. **Reference.** Every CLI command, the layout, how to share it.

## First run, in brief

Follow the [Claude Code setup guide](https://code.claude.com/docs/en/setup), run
`./setup.sh`, then open `claude` from the cloned CareerKit folder. Claude Code
may open a browser to sign in and then show Workspace Trust; authenticate through
its own flow and grant trust only after checking that it is the repository you
intended to run. Type `/setup` inside Claude Code.

`/setup` first checks the gitignored, content-free
`profile/setup-progress.md`. If privacy is not already acknowledged, it gives
the disclosure before reading any personal profile artifact. After
acknowledgment it inventories existing work, resumes the first pending phase,
and leaves deliberately deferred optional phases alone until requested or
needed. It asks before restarting or replacing existing work. If the slash command is
missing, confirm that `.claude/skills/setup/SKILL.md` exists, use `/skills` to
inspect project-skill discovery, reopen `claude` from the repository root, and
run `claude doctor` from the terminal if discovery still fails. You can also ask
Claude in plain language to follow the setup skill file.

Search Core asks only for the rules needed to score and source jobs; resumes and
other documents are optional. It keeps preferred-to-avoid titles in
`exclusions.titles`, which dream companies may waive, and role families the user
cannot credibly do in `exclusions.titles_always`, which nothing waives. After
`profile/profile.yaml` passes `profile-lint`, searching is available. Claude then
offers one optional, feed-only First Win and must describe it as incomplete
configured-feed coverage, not a complete market search. It does not activate
Freehire.

Source expansion and calibration come next. The claims/story/voice,
work-authorization, EEO, and tracker Career Pack can be completed now or deferred
until `/compose`, `/prep`, or `/apply`; it never blocks searching. Complete setup
can take about 40 minutes, but the first useful search comes earlier. Claims,
voice, human context, application fields and outcomes go only to their documented
profile artifacts; application/context fields do not score jobs. The final
handoff runs database, consistency, coverage, and doctor checks and names the
first shortlist, every coverage gap, deferred work, and one exact next action.

## The design rules

Three, and they explain most of the code.

**Your rules are the only rules.** Every gate reads your profile. There is no
hidden preference, no default job family, no home metro baked into the engine. A
test parses every module in `engine/` and fails the build if one of a list of
person-specific terms appears outside a docstring. A tripwire, not a proof, but
the engine cannot quietly acquire an opinion about what you want.

**Never fail open, never fail silent.** In an exclusion list, a rule that cannot
be compiled stops the run rather than being skipped, because a skipped exclusion
means everything you banned starts surfacing. Elsewhere the engine names what it
ignored and carries on. A board that returns HTTP 200 with an unparseable body
is recorded as broken, not as empty, because "no openings" and "we are blocking
you" look identical from the outside and only one of them is true.
Likewise, a source that returned some jobs can still be partial or capped. Its
results remain useful, but the run records a coverage gap instead of treating
the source as healthy or complete.

**Verified live, or it does not ship.** Postings persist in the database after
they close. Anything the engine surfaces has been sighted on the source board,
and anything that vanished from a healthy board gets written back as closed.

## The rule behind the rules

The suite is written against defects that were really in the code rather than
for coverage. Not hypothetical: specific failures that had shipped and would
otherwise come back.

That discipline is the honest measure of this tool. A job-search engine that is
subtly wrong does not crash. It hands you a clean, confident, plausible report
with the wrong jobs in it, and you never find out. Most of the engineering here
is aimed at that failure mode rather than at the happy path.

## Rebuilding the PDF

Requires Node 20 or newer.

```
npm ci
npx playwright install chromium
npm run guide
```

Source is `careerkit-guide.html`. The lockfile pins the renderer library and its
matching Chromium build; the guide uses local assets and makes no network
requests while printing. If you change the guide, rebuild the PDF in the same
commit.
