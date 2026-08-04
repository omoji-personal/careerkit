#!/bin/bash
# Create a separate CareerKit instance for one person: ../careerkit-instances/<name>
# Each instance has its own gitignored profile/, data/, tracker - fully isolated.
# Engine updates: cd into an instance and `git pull`.
set -e
[ -n "$1" ] || { echo "usage: ./new-instance.sh <name>"; exit 1; }
DEST="$(dirname "$(cd "$(dirname "$0")" && pwd)")/careerkit-instances/$1"
[ -e "$DEST" ] && { echo "exists: $DEST"; exit 1; }
mkdir -p "$(dirname "$DEST")"
git clone -q "$(cd "$(dirname "$0")" && pwd)" "$DEST"
cd "$DEST" && ./setup.sh
echo
echo "Instance ready: $DEST  (open it in Claude Code, run /setup)"
