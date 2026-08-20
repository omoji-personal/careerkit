#!/bin/bash
# Shared bootstrap interpreter selection.  Python.org's native Windows install
# commonly exposes `python` or the `py` launcher but not `python3`, including
# inside Git Bash.

careerkit_select_bootstrap_python() {
  CAREERKIT_BOOTSTRAP_PYTHON=()

  # A name existing is not enough: macOS and mixed Windows environments often
  # expose an older system `python3` before a newer `python` on PATH. Select the
  # first candidate that actually runs and meets CareerKit's version floor.
  if command -v python3 >/dev/null 2>&1 &&
      python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' \
        >/dev/null 2>&1; then
    CAREERKIT_BOOTSTRAP_PYTHON=(python3)
  elif command -v python >/dev/null 2>&1 &&
      python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' \
        >/dev/null 2>&1; then
    CAREERKIT_BOOTSTRAP_PYTHON=(python)
  elif command -v py >/dev/null 2>&1 &&
      py -3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' \
        >/dev/null 2>&1; then
    CAREERKIT_BOOTSTRAP_PYTHON=(py -3)
  else
    return 1
  fi
}
