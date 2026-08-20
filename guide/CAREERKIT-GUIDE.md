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
2. **Setup.** Prerequisites, the 40-minute interview, and why it is not optional.
3. **How scoring actually works.** Lanes, rails, gates, and the four verdicts:
   QUALIFIED, VERIFY, SLOT-BLOCKED, EXCLUDED. What each means and what to do
   about it.
4. **Where your rules live.** `profile/profile.yaml` is the only place scoring
   rules exist. How to change what surfaces without touching engine code.
5. **The daily loop.** `/search`, `/evaluate`, `/apply`, `/prep`, `/track`, and
   `/ingest` for anything you found elsewhere.
6. **Calibration.** Why `/audit` exists, and why a filter you never review will
   quietly cost you jobs.
7. **What leaves your machine.** Precisely, including keyed-feed credentials,
   the USAJobs email header, and search terms sent only when the optional
   Freehire discovery bridge is enabled.
8. **Failure modes worth knowing.** How the engine tells a dead board from a
   quiet one, and why zero results is a claim that has to be earned.
9. **Reference.** Every CLI command, the layout, how to share it.

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
