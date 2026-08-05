# CareerKit, the guide

**A job search engine you operate by talking to it.** It polls 17 employer
applicant tracking platforms directly plus 14 public job feeds, scores every
posting against rules you wrote yourself, and helps you evaluate, apply, prepare
and track from there.

Against one set of criteria it read 19,550 postings and surfaced 161.

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
7. **What leaves your machine.** Precisely, including the one feed that carries
   your email.
8. **Failure modes worth knowing.** How the engine tells a dead board from a
   quiet one, and why zero results is a claim that has to be earned.
9. **Reference.** Every CLI command, the layout, how to share it.

## The design rules

Three, and they explain most of the code.

**Your rules are the only rules.** Every gate reads your profile. There is no
hidden preference, no default job family, no home city baked into the engine. A
test walks the source tree and fails the build if a personal term appears in it.

**Never fail open, never fail silent.** A rule that cannot be compiled raises
rather than being skipped, because a skipped exclusion means everything you
banned starts surfacing. A board that returns HTTP 200 with an unparseable body
is recorded as broken, not as empty, because "no openings" and "we are blocking
you" look identical from the outside and only one of them is true.

**Verified live, or it does not ship.** Postings persist in the database after
they close. Anything the engine surfaces has been sighted on the source board,
and anything that vanished from a healthy board gets written back as closed.

## The rule behind the rules

Every test in the suite locks down a defect that was really in the code. Not
hypothetical coverage: specific failures that had shipped and would otherwise
come back.

That count is the honest measure of this tool. A job-search engine that is
subtly wrong does not crash. It hands you a clean, confident, plausible report
with the wrong jobs in it, and you never find out. Most of the engineering here
is aimed at that failure mode rather than at the happy path.

## Rebuilding the PDF

```
npm install
node guide/build-pdf.mjs
```

Source is `careerkit-guide.html`. System fonts only, so it renders identically
offline. If you change the guide, rebuild the PDF in the same commit.
