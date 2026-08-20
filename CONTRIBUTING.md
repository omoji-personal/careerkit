# Contributing

## The one rule that matters

**Every test in `tests/` locks down a defect that was really in the code.** Not
hypothetical coverage: a specific failure that shipped and would otherwise come
back. If you fix a bug, add the test that would have caught it, and write the
test's docstring as a description of the failure rather than of the function.

Compare:

```python
def test_extract_comp_handles_stipends():        # describes the function
def test_a_stipend_beside_a_band_does_not_become_the_ceiling():  # describes the failure
    """A "$500 home office stipend" next to a real salary band was parsed as
    $500,000 and became the reported top of the range."""
```

The second one still makes sense to someone reading it in a year.

## Running it

```
./setup.sh          # builds .venv, installs runtime + dev deps
./run-tests.sh      # the full suite, no network required
```

Tests never touch the network and never touch a real database. Adapter tests run
against saved payloads in `tests/fixtures/`.

## Where changes belong

**Scoring rules do not belong in the engine.** `profile/profile.yaml` is the only
place a person's criteria live. A test walks the source tree and fails the build
if a personal term, a city or an employer name appears in engine code. If you
find yourself wanting to special-case your own search, that is a profile change.

**Shared logic belongs in `engine/`.** Two front ends drive this engine, and the
polling loop was once copy-pasted into both. They drifted silently: one recorded
which run first saw a posting and the other did not, so the same feature worked
in one tool and quietly did not in the other. `engine/pull.py` exists because of
that. A guard test fails if the loop is re-inlined into a CLI.

## What to be careful about

The failure mode that matters here is not a crash. It is a clean, confident,
plausible report with the wrong jobs in it. Two habits follow.

**Never fail open, never fail silent.** A rule that cannot be compiled raises
rather than being skipped, because a skipped exclusion means everything the user
banned starts surfacing. A board returning HTTP 200 with an unparseable body is
recorded as broken, not as empty: "no openings" and "we are blocking you" look
identical from outside and only one is true.

**Prove a check can fail.** A consistency check in this repo once had a pattern
that matched nothing, so it reported no problems forever. Before trusting a new
check, break the thing it watches and confirm it fails.

## Style

- No em-dashes.
- Comments explain *why*, and ideally name the failure that motivated the code.
  The code says what it does; the comment should say what goes wrong without it.
- Match the surrounding code. It is plain Python with no framework on purpose.

## The guide

`guide/careerkit-guide.html` is the source; `guide/Careerkit-Guide.pdf` is built
with Node 20 or newer: `npm ci`, `npx playwright install chromium`, then
`npm run guide`. The lockfile pins Playwright and its matching Chromium build;
local assets keep the print itself offline. If you change the guide, rebuild the
PDF in the same commit.
