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

command -v python3 >/dev/null || {
  echo "MISSING: python3"
  echo "  macOS:   brew install python3      (or download from python.org)"
  echo "  Windows: install from python.org, then run this from Git Bash or WSL"
  exit 1
}

PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' || {
  echo "MISSING: Python 3.10 or newer (you have $PYV)"; exit 1; }
echo "  python3 $PYV"

command -v git >/dev/null || {
  echo "MISSING: git"
  echo "  macOS: xcode-select --install"
  exit 1
}
echo "  git $(git --version | awk '{print $3}')"

# Python puts the venv binaries in Scripts/ on Windows and bin/ everywhere else.
# The README tells Windows users to run this from Git Bash, where the shell is
# POSIX but the Python is native, so hardcoding bin/ broke setup for exactly the
# users least able to diagnose it.
venv_bin() { [ -d .venv/Scripts ] && echo .venv/Scripts || echo .venv/bin; }

# Gating on "does the folder exist" can never repair a BROKEN venv - the
# common case after `brew upgrade python`, which leaves the interpreter a
# dangling symlink. Test that it actually runs, and rebuild if it does not.
if [ -d .venv ] && ! "$(venv_bin)/python" -c "import sys" >/dev/null 2>&1; then
  echo "  .venv is broken (interpreter will not run); rebuilding ..."
  rm -rf .venv
fi
if [ ! -d .venv ]; then
  echo "  creating .venv ..."
  python3 -m venv .venv || {
    echo "FAILED to create a virtual environment."
    echo "  On Debian/Ubuntu: sudo apt install python3-venv"
    exit 1; }
fi
# shellcheck disable=SC1091
. "$(venv_bin)/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements-dev.txt
echo "  dependencies installed into .venv"

mkdir -p profile data out
[ -f profile/employers.yaml ] || printf 'employers: []\nfeeds:\n- {name: remotive, active: true}\n- {name: remoteok, active: true}\n- {name: himalayas, active: true}\n- {name: jobicy, active: true}\n- {name: themuse, active: true}\n- {name: weworkremotely, active: true}\n- {name: workingnomads, active: true}\n- {name: arbeitnow, active: true}\n' > profile/employers.yaml

python -c "import sys; sys.path.insert(0,'.'); import engine.score, engine.adapters" \
  && echo "  engine OK"

cat <<'EOF'

Done.

  Next:  claude          # open Claude Code in this folder
         /setup          # it interviews you and writes profile/profile.yaml

./careerkit.py activates .venv on its own, so you never have to remember to.
EOF
