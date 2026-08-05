#!/bin/bash
# Create a separate CareerKit instance for one person: ../careerkit-instances/<name>
# Each instance has its own gitignored profile/, data/ and tracker, fully isolated.
#
# The instance clones from the PUBLIC repo, not from this directory. Cloning a
# local path made `origin` a folder on one particular machine: the promise in
# this header that you can "cd in and git pull" was true only for the person who
# ran the script, and silently false for everyone they gave an instance to.
set -e
[ -n "$1" ] || { echo "usage: ./new-instance.sh <name> [--local]"; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$(dirname "$HERE")/careerkit-instances/$1"
[ -e "$DEST" ] && { echo "exists: $DEST"; exit 1; }

# Prefer this checkout's own origin, so a fork clones from the fork.
UPSTREAM="$(git -C "$HERE" remote get-url origin 2>/dev/null || true)"
[ -n "$UPSTREAM" ] || UPSTREAM="https://github.com/omoji-personal/careerkit.git"

if [ "$2" = "--local" ]; then
  UPSTREAM="$HERE"
  echo "Cloning from this working copy (--local): unreleased changes included,"
  echo "and 'git pull' inside the instance will only work on this machine."
fi

mkdir -p "$(dirname "$DEST")"
if ! git clone -q "$UPSTREAM" "$DEST" 2>/dev/null; then
  echo "Could not clone $UPSTREAM"
  echo "If you are offline or the repo is unreachable, use: ./new-instance.sh $1 --local"
  exit 1
fi

cd "$DEST" && ./setup.sh
echo
echo "Instance ready: $DEST"
echo "  updates:  cd $DEST && git pull"
echo "  next:     open it in Claude Code and run /setup"
