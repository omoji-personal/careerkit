#!/bin/bash
# Create a separate CareerKit instance for one person: ../careerkit-instances/<name>
# Each instance has its own gitignored profile/, data/ and tracker, fully isolated.
#
# By default the instance clones this checkout's HTTP(S) origin, so later pulls
# are portable for anyone who can access that exact URL. `--local` is the
# explicit exception: it snapshots this working tree and leaves a machine-local
# origin, which the script and README disclose.
set -euo pipefail

usage() {
  echo "usage: ./new-instance.sh <name> [--local]"
  echo "  name may contain letters, numbers, dots, underscores, and hyphens"
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || { usage; exit 1; }
case "$1" in
  [A-Za-z0-9]*) ;;
  *) echo "Invalid instance name: $1"; usage; exit 1 ;;
esac
case "$1" in
  *[!A-Za-z0-9._-]*) echo "Invalid instance name: $1"; usage; exit 1 ;;
esac
if [ "$#" -eq 2 ] && [ "$2" != "--local" ]; then
  echo "Unknown option: $2"
  usage
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTANCE_ROOT="$(dirname "$HERE")/careerkit-instances"
DEST="$INSTANCE_ROOT/$1"
[ -e "$DEST" ] && { echo "exists: $DEST"; exit 1; }

# Prefer this checkout's own origin, so a fork clones from the fork.
UPSTREAM="$(git -C "$HERE" remote get-url origin 2>/dev/null || true)"
[ -n "$UPSTREAM" ] || UPSTREAM="https://github.com/omoji-personal/careerkit.git"

LOCAL=false
if [ "$#" -eq 2 ]; then
  LOCAL=true
  UPSTREAM="$HERE"
  echo "Cloning this checkout and snapshotting its non-ignored working changes."
  echo "The instance's origin is local, so updates work only on this machine."
else
  case "$UPSTREAM" in
    http://*@*|https://*@*)
      echo "Origin URL contains embedded credentials; refusing to print or clone it."
      echo "Use a credential-free HTTP(S) origin or explicit --local mode."
      exit 1
      ;;
    http://*|https://*) ;;
    *)
      echo "Origin is not a portable HTTP(S) URL: $UPSTREAM"
      echo "Use --local for an explicit machine-local snapshot, or set a portable origin."
      exit 1
      ;;
  esac
  echo "Cloning portable origin: $UPSTREAM"
fi

mkdir -p "$INSTANCE_ROOT"
if ! git clone -q "$UPSTREAM" "$DEST" 2>/dev/null; then
  echo "Could not clone $UPSTREAM"
  echo "If you are offline or the repo is unreachable, use: ./new-instance.sh $1 --local"
  exit 1
fi

if [ "$LOCAL" = true ]; then
  # A local clone contains only committed objects. Overlay both tracked changes
  # and non-ignored untracked files so "--local" means the working snapshot the
  # operator can actually see. Ignored profile/data/out secrets stay excluded.
  if ! git -C "$HERE" diff --binary HEAD | \
       git -C "$DEST" apply --whitespace=nowarn; then
    echo "Could not copy tracked working changes into $DEST"
    exit 1
  fi
  while IFS= read -r -d '' rel; do
    mkdir -p "$DEST/$(dirname "$rel")"
    cp -pP "$HERE/$rel" "$DEST/$rel"
  done < <(git -C "$HERE" ls-files --others --exclude-standard -z)
fi

cd "$DEST" && ./setup.sh
echo
echo "Instance ready: $DEST"
if [ "$LOCAL" = true ]; then
  echo "  updates:  first commit/remove snapshot changes, then git pull && ./setup.sh"
else
  echo "  updates:  cd $DEST && git pull && ./setup.sh"
fi
echo "  next:     open it in Claude Code and run /setup"
