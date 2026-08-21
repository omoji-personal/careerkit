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
  fi

  [ ${#CAREERKIT_BOOTSTRAP_PYTHON[@]} -gt 0 ] && return 0

  # Last resort: a usable interpreter that is simply not the one `python3`
  # resolves to. Reported from a first run where `python3` was Apple's 3.9.6 and
  # Homebrew's 3.14 sat at /opt/homebrew/bin/python3, so setup said "MISSING:
  # Python 3.10 or newer" with a valid interpreter already on disk. That is the
  # default outcome whenever someone installs Python and does not update PATH,
  # which is exactly the user least able to diagnose it.
  # The absolute prefixes below exist on any provisioned machine, so a test that
  # merely restricts PATH still finds a real interpreter and cannot describe a
  # bare system. This override is that seam, and doubles as an escape hatch for
  # an interpreter installed somewhere unusual. A single space means "none".
  local careerkit_candidates="${CAREERKIT_PYTHON_SEARCH_PATHS-\
python3.14 python3.13 python3.12 python3.11 python3.10 \
/opt/homebrew/bin/python3 /usr/local/bin/python3}"

  local careerkit_candidate
  for careerkit_candidate in $careerkit_candidates; do
    if command -v "$careerkit_candidate" >/dev/null 2>&1 &&
        "$careerkit_candidate" -c \
          'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' \
          >/dev/null 2>&1; then
      CAREERKIT_BOOTSTRAP_PYTHON=("$careerkit_candidate")
      return 0
    fi
  done

  return 1
}
