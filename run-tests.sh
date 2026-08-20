#!/bin/bash
# Regression suite. Every test here locks down a bug that actually shipped.
# No network, no touching your real database.
set -e
PYTHONUTF8=1
export PYTHONUTF8
cd "$(dirname "$0")"
if [ -x .venv/Scripts/python.exe ]; then
  PY=.venv/Scripts/python.exe
elif [ -x .venv/Scripts/python ]; then
  PY=.venv/Scripts/python
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3            # setup.sh has not been run yet
fi
"$PY" -c "import pytest" 2>/dev/null || {
  echo "pytest not installed. Run ./setup.sh first, or: $PY -m pip install pytest"; exit 1; }
"$PY" -m pytest tests/ -q "$@"
