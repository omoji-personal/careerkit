#!/bin/sh
# Portable entrypoint for ./careerkit.py. Python.org's native Windows install
# commonly exposes python.exe to Git Bash without providing a python3 command,
# so /usr/bin/env python3 could fail before CareerKit had a chance to enter its
# project venv.
set -eu

script=${1:?missing CareerKit script path}
shift

case "$script" in
  /*) script_path=$script ;;
  *) script_path=$PWD/$script ;;
esac
script_dir=${script_path%/*}
root=$(CDPATH= cd -- "$script_dir" 2>/dev/null && pwd -P) || {
  echo "Could not resolve the CareerKit checkout for $script" >&2
  exit 1
}

if [ -x "$root/.venv/Scripts/python.exe" ]; then
  runner=$root/.venv/Scripts/python.exe
elif [ -x "$root/.venv/bin/python" ]; then
  runner=$root/.venv/bin/python
else
  echo "CareerKit's virtual environment is missing. Run ./setup.sh first." >&2
  exit 1
fi

exec "$runner" "$script_path" "$@"
