#!/bin/bash
# Shared Claude Code detection for setup.sh and bootstrap.sh.
#
# Claude Code ships four ways that matter here: the native installer (a launcher
# at ~/.local/bin/claude), Homebrew, npm, and the desktop app.  CareerKit works
# with any of them, because /setup is a skill inside this checkout and whichever
# one opens this folder can run it.  Detecting only `command -v claude` told
# desktop-app users their perfectly good install was missing and sent them to
# install a second copy.
#
# Sets, in the caller's shell:
#   CAREERKIT_CLAUDE_STATE    ready | desktop | unusable | not-on-path | missing
#   CAREERKIT_CLAUDE_VERSION  version string when one could be read
#   CAREERKIT_CLAUDE_DESKTOP  path to the desktop app when that is what was found
#   CAREERKIT_CLAUDE_ON_DISK  1 when a launcher exists at ~/.local/bin/claude
#
# Must be called directly, never through $( ): a subshell would discard every
# one of those assignments.

careerkit_detect_claude() {
  CAREERKIT_CLAUDE_STATE=missing
  CAREERKIT_CLAUDE_VERSION=""
  CAREERKIT_CLAUDE_DESKTOP=""
  CAREERKIT_CLAUDE_ON_DISK=0

  if command -v claude >/dev/null 2>&1; then
    if CAREERKIT_CLAUDE_VERSION_OUTPUT="$(claude --version 2>/dev/null)"; then
      CAREERKIT_CLAUDE_VERSION="$(printf '%s\n' "$CAREERKIT_CLAUDE_VERSION_OUTPUT" \
        | awk 'NF {print $1; exit}')"
    fi
    if [ -n "$CAREERKIT_CLAUDE_VERSION" ]; then
      CAREERKIT_CLAUDE_STATE=ready
      return 0
    fi
    # On PATH but refusing to report a version: a broken install is its own
    # state, because reinstalling and fixing PATH are different remedies.
    CAREERKIT_CLAUDE_STATE=unusable
    return 0
  fi

  [ -x "$HOME/.local/bin/claude" ] && CAREERKIT_CLAUDE_ON_DISK=1

  # The desktop app lives at an absolute path, so a test that merely hides the
  # `claude` command still sees whatever is installed on the developer's own
  # machine and asserts different behavior there than on CI.  This override is
  # that seam -- set it to a single space to mean "no desktop app" -- and it
  # doubles as the escape hatch for anyone whose install is somewhere unusual.
  if [ -n "${CAREERKIT_CLAUDE_DESKTOP_PATHS-}" ]; then
    careerkit_desktop_candidates="$CAREERKIT_CLAUDE_DESKTOP_PATHS"
  else
    case "$(uname -s 2>/dev/null || echo unknown)" in
      Darwin)
        careerkit_desktop_candidates="/Applications/Claude.app:$HOME/Applications/Claude.app" ;;
      Linux)
        if command -v claude-desktop >/dev/null 2>&1; then
          CAREERKIT_CLAUDE_DESKTOP=claude-desktop
          CAREERKIT_CLAUDE_STATE=desktop
          return 0
        fi
        careerkit_desktop_candidates="" ;;
      MINGW*|MSYS*|CYGWIN*)
        careerkit_desktop_candidates="$HOME/AppData/Local/Programs/Claude/Claude.exe:/c/Program Files/Claude/Claude.exe" ;;
      *)
        careerkit_desktop_candidates="" ;;
    esac
  fi

  careerkit_saved_ifs="$IFS"
  IFS=:
  for careerkit_app in $careerkit_desktop_candidates; do
    IFS="$careerkit_saved_ifs"
    [ -n "$careerkit_app" ] || continue
    if [ -d "$careerkit_app" ] || [ -f "$careerkit_app" ]; then
      CAREERKIT_CLAUDE_DESKTOP="$careerkit_app"
      CAREERKIT_CLAUDE_STATE=desktop
      return 0
    fi
    IFS=:
  done
  IFS="$careerkit_saved_ifs"

  # A launcher on disk that the shell cannot see is a PATH problem, not an
  # absent install, and reinstalling would not fix it.
  if [ "$CAREERKIT_CLAUDE_ON_DISK" -eq 1 ]; then
    CAREERKIT_CLAUDE_STATE=not-on-path
    return 0
  fi

  CAREERKIT_CLAUDE_STATE=missing
}
