"""Do the safety checks fail when they should?

Three times in one day a check failed for the wrong reason. The publish gate
reported "client or employer names present" because it matched "kipp" inside the
word "skipped". A lint reported clean because pyflakes was absent from the
interpreter running it. A consistency regex hung on a file with very long lines
and was recorded as producing no findings.

A check that cannot fail correctly is worse than no check, because it is counted
as evidence. That principle is already stated in this project's own
documentation and had not been applied to its own tooling. These feed each gate
something that must trip it, and something that must not.
"""
from __future__ import annotations

import re

import pytest


# --------------------------------------------------------------------------
# Word-boundary matching. The "kipp" in "skipped" failure.
# --------------------------------------------------------------------------

SENSITIVE = re.compile(r"\b(techbridge|jacoby|kipp|nelson mullins)\b", re.I)


@pytest.mark.parametrize("text", [
    "added, skipped = 0, []",
    "if it says skipped, tell the user",
    "rather than being skipped, because a skipped exclusion",
    "the request was skipped",
])
def test_the_secret_scan_does_not_match_inside_a_word(text):
    assert not SENSITIVE.search(text), (
        f"a substring match would fail the publish gate on ordinary code: {text!r}")


@pytest.mark.parametrize("text", [
    "we built this for KIPP last year",
    "the Techbridge internal org",
    "Nelson Mullins required Platform Developer II",
])
def test_the_secret_scan_still_catches_the_real_thing(text):
    assert SENSITIVE.search(text), f"the gate missed a real client name: {text!r}"


# --------------------------------------------------------------------------
# Secret-shaped strings.
# --------------------------------------------------------------------------

SECRET = re.compile(r"(AKIA[0-9A-Z]{16}|sk-ant-[A-Za-z0-9-]{8,}|ghp_[A-Za-z0-9]{20,}|"
                    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def test_the_secret_scan_catches_each_shape_it_claims_to():
    """The samples are assembled at runtime rather than written out.

    A literal secret-shaped string in this file trips the repository's own
    publish gate, which is correct of the gate: it cannot tell a deliberate
    example from a leaked key, and it should not try. Weakening the scanner so
    its test can pass would be the wrong way round.
    """
    samples = ["AKIA" + "IOSFODNN7EXAMPLE",
               "sk-" + "ant-api03-abcdefghij",
               "ghp" + "_" + "a" * 24,
               "-" * 5 + "BEGIN RSA " + "PRIVATE" + " KEY" + "-" * 5]
    for s in samples:
        assert SECRET.search(s), f"the gate would not catch {s[:12]!r}"


def test_the_secret_scan_does_not_flag_ordinary_prose():
    for s in ("the AKIA key was rotated", "the sk-ant prefix", "short tokens",
              "we do not commit private keys"):
        assert not SECRET.search(s), f"false positive on {s!r}"


# --------------------------------------------------------------------------
# The linter has to actually be present.
# --------------------------------------------------------------------------

def test_pyflakes_is_importable_by_the_interpreter_running_the_tests():
    """A lint that cannot run reports nothing, which is indistinguishable from a
    lint that ran and found nothing. The publish gate recorded a pass on exactly
    this, because pyflakes was missing from the Python it invoked."""
    import importlib.util
    assert importlib.util.find_spec("pyflakes") is not None, (
        "pyflakes is not installed for this interpreter, so any lint result "
        "from it is meaningless")


def test_the_engine_has_no_pyflakes_findings():
    """Run it here rather than trusting a shell script's exit code."""
    import pathlib
    import subprocess
    import sys
    root = pathlib.Path(__file__).resolve().parent.parent
    # tests/ included because CI lints it and this gate did not: an unused
    # import shipped on 2026-08-11 sat invisible to a green local suite and
    # kept CI red for three days. Local and CI must lint the same files, or
    # "the suite is green" stops meaning "the push will be".
    files = ([str(p) for p in sorted(root.glob("engine/*.py"))]
             + [str(root / "careerkit.py")]
             + [str(p) for p in sorted(root.glob("tests/*.py"))])
    r = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------------------------
# Regexes that can hang. The third wrong-reason failure.
# --------------------------------------------------------------------------

def test_the_report_parsers_do_not_hang_on_a_very_long_line():
    """A search over a file whose rows are single very long lines stalled and was
    recorded as finding nothing. Anything parsing stored text has to cope with
    the shape real data actually has."""
    import time

    from engine import consistency
    long_line = "x" * 200_000
    start = time.time()
    consistency._blocks(long_line + "\n### 1. Title - Company\n- https://x/1\n")
    assert time.time() - start < 2.0, "block parsing is superlinear on long lines"


def test_ghost_scoring_is_safe_on_a_row_missing_every_optional_column():
    """The checks run against rows from older databases, which do not have the
    columns added since. Raising there aborts a command a user ran for an
    unrelated reason."""
    from engine import ghost

    class Row(dict):
        def keys(self):
            return dict.keys(self)

        def __getitem__(self, k):
            return dict.get(self, k)

    assert ghost.score_row(Row({"source": "greenhouse", "title": "Admin"})) == (0, [])
