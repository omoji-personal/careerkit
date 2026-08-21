#!/bin/bash
# CareerKit prerequisite check + bootstrap. Safe to re-run.
#
# Installs into a local .venv rather than the system Python. Modern Python
# installs (Homebrew, python.org 3.11+, most Linux distros) are marked
# externally-managed under PEP 668 and REFUSE `pip install --user`, which used
# to abort this script on its first step for anyone who did not already happen
# to have the dependencies sitting in their system Python.
set -e
cd "$(dirname "$0")"

echo "CareerKit setup"

# shellcheck source=scripts/select-python.sh
. ./scripts/select-python.sh
careerkit_select_bootstrap_python || {
  echo "MISSING: Python 3.10 or newer"
  echo "  macOS:   brew install python3      (or download from python.org)"
  echo "  Windows: install from python.org, then run this from Git Bash or WSL"
  exit 1
}

PYV=$("${CAREERKIT_BOOTSTRAP_PYTHON[@]}" -c \
  'import sys; print("%d.%d" % sys.version_info[:2])')
"${CAREERKIT_BOOTSTRAP_PYTHON[@]}" -c \
  'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' || {
  echo "MISSING: Python 3.10 or newer (you have $PYV)"; exit 1; }
echo "  ${CAREERKIT_BOOTSTRAP_PYTHON[*]} $PYV"

command -v git >/dev/null || {
  echo "MISSING: git"
  echo "  macOS: xcode-select --install"
  exit 1
}
echo "  git $(git --version | awk '{print $3}')"

# Claude Code is the interface this whole tool is built around, and setup called
# itself a prerequisite check while never looking for it. A user could complete
# setup, be told to run `claude`, and hit "command not found" with nothing in the
# output to explain it. A warning rather than a failure: the CLI still works.
# shellcheck source=scripts/detect-claude.sh
. ./scripts/detect-claude.sh
careerkit_detect_claude
CLAUDE_STATE="$CAREERKIT_CLAUDE_STATE"
CLAUDE_VERSION="$CAREERKIT_CLAUDE_VERSION"
case "$CLAUDE_STATE" in
  ready)
    echo "  claude $CLAUDE_VERSION"
    ;;
  desktop)
    echo "  claude Code desktop app ($CAREERKIT_CLAUDE_DESKTOP)"
    ;;
  unusable)
    echo "  ! claude is present but unusable: claude --version failed or returned no version."
    echo "    Fix or reinstall it:  curl -fsSL https://claude.ai/install.sh | bash"
    echo "    The ./careerkit.py commands work without it; the /search style"
    echo "    workflows do not."
    ;;
  not-on-path)
    echo "  ! claude is installed at ~/.local/bin/claude but is not on this shell's PATH."
    echo "    Add it:  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo "    The ./careerkit.py commands work without it; the /search style"
    echo "    workflows do not."
    ;;
  *)
    echo "  ! claude not found. CareerKit is driven from Claude Code."
    echo "    Install it:  ./bootstrap.sh          (installs prerequisites for you)"
    echo "    The ./careerkit.py commands work without it; the /search style"
    echo "    workflows do not."
    ;;
esac

# Python puts the venv binaries in Scripts/ on Windows and bin/ everywhere else.
# The README tells Windows users to run this from Git Bash, where the shell is
# POSIX but the Python is native, so hardcoding bin/ broke setup for exactly the
# users least able to diagnose it.
venv_python() {
  if [ -x .venv/Scripts/python.exe ]; then
    echo .venv/Scripts/python.exe
  elif [ -x .venv/Scripts/python ]; then
    echo .venv/Scripts/python
  else
    echo .venv/bin/python
  fi
}

# Gating on "does the folder exist" can never repair a broken or obsolete venv.
# A long-lived 3.9 venv still runs, but keeping it would bypass the >=3.10 floor
# verified above and let pip silently choose old dependency releases.
if [ -d .venv ] && ! "$(venv_python)" -c \
    'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' \
    >/dev/null 2>&1; then
  echo "  .venv is broken or uses Python older than 3.10; rebuilding ..."
  rm -rf .venv
fi
if [ ! -d .venv ]; then
  echo "  creating .venv ..."
  "${CAREERKIT_BOOTSTRAP_PYTHON[@]}" -m venv .venv || {
    echo "FAILED to create a virtual environment."
    echo "  On Debian/Ubuntu: sudo apt install python3-venv"
    exit 1; }
fi
VENV_PY="$(venv_python)"

dependency_install_failed() {
  local failed_step="$1"
  local failed_status="$2"
  echo "FAILED: could not $failed_step." >&2
  echo "  Setup is incomplete, but it is safe to rerun." >&2
  echo "  Check your network or proxy, then run: ./setup.sh" >&2
  echo "  CareerKit will reuse .venv and retry the dependency installation." >&2
  exit "$failed_status"
}

echo "  upgrading pip in .venv ..."
if "$VENV_PY" -m pip install --quiet --upgrade pip; then
  :
else
  dependency_install_failed "upgrade pip in .venv" "$?"
fi

echo "  installing CareerKit runtime and development dependencies ..."
if "$VENV_PY" -m pip install --quiet -r requirements-dev.txt; then
  :
else
  dependency_install_failed "install CareerKit runtime and development dependencies" "$?"
fi

# pip installs and upgrades declared requirements but never removes an optional
# dependency that disappeared from the file. Migrate the retired vulnerable
# JobSpy graph out of this CareerKit-owned venv; a future compatible graph with
# markdownify>=0.14.1 is deliberately left alone.
"$VENV_PY" scripts/remove_unsafe_jobspy.py
"$VENV_PY" -m pip check
echo "  dependencies installed into .venv"

mkdir -p profile data out
[ -f profile/employers.yaml ] || printf 'employers: []\nfeeds:\n- {name: remotive, active: true}\n- {name: remoteok, active: true}\n- {name: himalayas, active: true}\n- {name: jobicy, active: true}\n- {name: themuse, active: true}\n- {name: weworkremotely, active: true}\n- {name: workingnomads, active: true}\n- {name: arbeitnow, active: true}\n- {name: freehire, active: false, pages: 10, results_per_page: 100, detail_cap: 100, posted_within_days: 14, countries: [], canonical_enrichment: false}\n' > profile/employers.yaml

"$VENV_PY" -c "import sys; sys.path.insert(0,'.'); import engine.score, engine.adapters" \
  && echo "  engine OK"

case "$CLAUDE_STATE" in
  desktop)
    cat <<'EOF'

Done.

  Next:  open this folder in the Claude Code desktop app
         /setup          # it interviews you and writes profile/profile.yaml

  The terminal command is optional; the desktop app runs /setup just as well.
EOF
    ;;
  not-on-path)
    cat <<'EOF'

CareerKit's Python environment is ready, but this shell cannot see Claude Code.

  Next:  export PATH="$HOME/.local/bin:$PATH"
         ./setup.sh      # safe recheck; it should print a Claude version

  Add that export line to your shell profile to make it stick, or just open a
  new terminal window.
EOF
    ;;
  ready)
    cat <<'EOF'

Done.

  Next:  claude          # open Claude Code in this folder
         /setup          # it interviews you and writes profile/profile.yaml

  If the Claude CLI later has an installation problem, run: claude doctor
EOF
    ;;
  unusable)
    cat <<'EOF'

CareerKit's Python environment is ready, but Claude Code is not usable yet.

  Next:  claude doctor   # diagnose the present Claude installation
         npm install -g @anthropic-ai/claude-code
         ./setup.sh      # safe recheck; it should print a Claude version

Do not start /setup until this check reports a usable Claude version.
EOF
    ;;
  missing)
    cat <<'EOF'

CareerKit's Python environment is ready, but Claude Code is not installed yet.

  Next:  ./bootstrap.sh  # installs Claude Code (and anything else missing)
         ./setup.sh      # safe recheck; it should print a Claude version

  Prefer a graphical app, or installing by hand?
         desktop app:  https://claude.ai/download
         terminal:     curl -fsSL https://claude.ai/install.sh | bash

After that recheck succeeds, follow the Claude and /setup instructions it prints.
EOF
    ;;
esac

cat <<'EOF'

./careerkit.py activates .venv on its own, so you never have to remember to.
EOF
