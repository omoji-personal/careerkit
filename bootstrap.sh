#!/bin/bash
# CareerKit prerequisite installer.  Safe to re-run; installs only what is missing.
#
# setup.sh deliberately only *checks* prerequisites and prints a hint, which is
# correct for anyone who already lives in a terminal.  For everyone else the hint
# is where the install ends: "brew install python3" fails with "command not
# found: brew", and the honest answer -- install Homebrew first, then remember to
# put it on PATH -- is three more steps nobody wrote down.  This script is that
# missing page.  It never installs anything without printing the exact command
# first and asking.
#
# Deliberately NOT a prerequisite: Node.js.  Claude Code's native installer
# ships a self-contained binary, and CareerKit only uses npm to rebuild the
# guide PDF, which is committed.  An earlier draft of this script installed Node
# purely to run `npm install -g`, which is a package manager pulled in to
# install something that does not need it.
set -u
cd "$(dirname "$0")"

YES=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    -n|--dry-run) DRY_RUN=1 ;;
    -h|--help)
      cat <<'EOF'
CareerKit prerequisite installer

  ./bootstrap.sh              check what is missing, show the plan, ask, install
  ./bootstrap.sh --dry-run    show the plan and exit without changing anything
  ./bootstrap.sh --yes        skip the confirmation prompt (for unattended use)

Installs, only when missing: Homebrew (macOS, only if needed for Python),
Python 3.10+, and Claude Code.  Then hands off to ./setup.sh.

Claude Code counts as installed if you have either the terminal command or
the desktop app.
EOF
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# A fresh Mac can have no shell profile at all -- not ~/.zprofile, not
# ~/.bash_profile, nothing.  Homebrew's installer does not always create one,
# so tools installed here work in the current shell and then vanish from the
# next terminal window, which reads exactly like a failed setup.  Append (and
# create, if absent) so what we install survives a reboot.
shell_profile() {
  if [ "${SHELL##*/}" = "bash" ]; then echo "$HOME/.bash_profile"; else echo "$HOME/.zprofile"; fi
}
persist_line() { # persist_line <line> <description>
  local target; target="$(shell_profile)"
  if grep -qsF "$1" "$target"; then
    say "    already in $target"
  else
    printf '\n%s\n' "$1" >> "$target"
    say "    added to $target so a new terminal keeps it"
  fi
}

# ---------------------------------------------------------------- platform ---
case "$(uname -s 2>/dev/null || echo unknown)" in
  Darwin) PLATFORM=macos ;;
  Linux)  PLATFORM=linux ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM=windows ;;
  *) PLATFORM=unknown ;;
esac

# Apple Silicon and Intel Macs put Homebrew in different prefixes, and guessing
# wrong produces a shellenv line that silently does nothing.
if [ "$(uname -m 2>/dev/null || true)" = "arm64" ]; then
  BREW_PREFIX=/opt/homebrew
else
  BREW_PREFIX=/usr/local
fi

# ------------------------------------------------------------ requirements ---
# A name on PATH is not enough: macOS ships python3 3.9.6, which is below the
# floor CareerKit needs.  Check what actually runs.
python_ok() {
  # shellcheck source=scripts/select-python.sh
  . ./scripts/select-python.sh 2>/dev/null || return 1
  careerkit_select_bootstrap_python 2>/dev/null || return 1
}
python_version() {
  if python_ok; then
    "${CAREERKIT_BOOTSTRAP_PYTHON[@]}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'
  fi
}

# shellcheck source=scripts/detect-claude.sh
. ./scripts/detect-claude.sh

STATUS_LINES=()
MISSING=()
record() { # record <label> <ok:0|1> <detail>
  if [ "$2" -eq 0 ]; then
    STATUS_LINES+=("  ok       $1${3:+  $3}")
  else
    STATUS_LINES+=("  MISSING  $1${3:+  $3}")
    MISSING+=("$1")
  fi
}

if have git; then record git 0 "$(git --version 2>/dev/null | awk '{print $3}')"; else record git 1 ""; fi
if python_ok; then record python 0 "$(python_version)"; else record python 1 "(3.10+ required)"; fi

careerkit_detect_claude
CLAUDE_STATE="$CAREERKIT_CLAUDE_STATE"
case "$CLAUDE_STATE" in
  ready)   record "claude code" 0 "$CAREERKIT_CLAUDE_VERSION" ;;
  desktop) record "claude code" 0 "desktop app" ;;
  not-on-path) record "claude code" 1 "(installed, but not on this shell's PATH)" ;;
  missing) record "claude code" 1 "" ;;
esac

say "CareerKit prerequisites"
for line in "${STATUS_LINES[@]}"; do say "$line"; done
say ""

if [ "$CLAUDE_STATE" = "desktop" ]; then
  say "Using the Claude Code desktop app${CAREERKIT_CLAUDE_DESKTOP:+ ($CAREERKIT_CLAUDE_DESKTOP)}."
  say "Open this folder in it, then type /setup.  The terminal command is optional."
  if [ "$CAREERKIT_CLAUDE_ON_DISK" -eq 1 ]; then
    say ""
    say "You also have the terminal command installed at ~/.local/bin/claude, but"
    say "this shell cannot see it.  To use it too:"
    say "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zprofile"
  fi
  say ""
fi

# A present-but-unreachable install is a PATH problem; installing again fixes
# nothing, so say exactly what to do instead of planning a download.
if [ "$CLAUDE_STATE" = "not-on-path" ]; then
  say "Claude Code is installed at ~/.local/bin/claude but this shell cannot see it."
  say ""
  say "  Fix it with:"
  say "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zprofile"
  say "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  say ""
  say "  Then re-run ./bootstrap.sh   (or just open a new terminal window)"
  say ""
fi

if [ ${#MISSING[@]} -eq 0 ]; then
  say "Everything CareerKit needs is already installed."
  say ""
  say "  Next:  ./setup.sh"
  exit 0
fi

# -------------------------------------------------------------------- plan ---
# Each planned step is a label and the exact shell it will run, so the summary
# below and the commands actually executed can never drift apart.
STEP_LABELS=()
STEP_CMDS=()
plan() { STEP_LABELS+=("$1"); STEP_CMDS+=("$2"); }

needs() { # needs <name> -> 0 when that requirement is missing
  local want="$1" item
  for item in "${MISSING[@]}"; do [ "$item" = "$want" ] && return 0; done
  return 1
}

UNSUPPORTED=0
case "$PLATFORM" in
  macos)
    needs git && plan "Xcode Command Line Tools (provides git)" "xcode-select --install"
    if needs python && ! have brew; then
      plan "Homebrew (the macOS package manager)" \
        '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
      plan "put Homebrew on your PATH (this is the step people miss)" \
        "eval \"\$($BREW_PREFIX/bin/brew shellenv)\"  # and append the same line to your shell profile"
    fi
    needs python && plan "Python 3.13" "brew install python@3.13"
    ;;
  linux)
    if have apt-get; then
      needs git    && plan "git" "sudo apt-get install -y git"
      needs python && plan "Python 3 and venv" "sudo apt-get install -y python3 python3-venv python3-pip"
    elif have dnf; then
      needs git    && plan "git" "sudo dnf install -y git"
      needs python && plan "Python 3" "sudo dnf install -y python3 python3-pip"
    else
      UNSUPPORTED=1
    fi
    ;;
  *)
    UNSUPPORTED=1
    ;;
esac

# Claude Code's native installer is self-contained and works the same on macOS,
# Linux and WSL, so it is planned outside the platform switch.  It needs no
# Node.js, unlike the npm route.
if [ "$CLAUDE_STATE" = "missing" ] && [ "$PLATFORM" != "windows" ] && [ "$UNSUPPORTED" -eq 0 ]; then
  plan "Claude Code" "curl -fsSL https://claude.ai/install.sh | bash"
fi

if [ "$UNSUPPORTED" -eq 1 ] || [ ${#STEP_CMDS[@]} -eq 0 ]; then
  say "This installer cannot install packages on this system automatically, so"
  say "here is what to install by hand:"
  say ""
  needs git    && say "  git       https://git-scm.com/downloads"
  needs python && say "  Python    https://www.python.org/downloads/   (3.10 or newer)"
  if [ "$CLAUDE_STATE" = "missing" ]; then
    say "  Claude    desktop app, no terminal needed:  https://claude.ai/download"
    say "            or in a terminal:  curl -fsSL https://claude.ai/install.sh | bash"
    say "            (Windows PowerShell:  irm https://claude.ai/install.ps1 | iex)"
  fi
  say ""
  say "Then run: ./setup.sh"
  exit 1
fi

say "This will install:"
say ""
i=0
while [ "$i" -lt ${#STEP_LABELS[@]} ]; do
  say "  $((i + 1)). ${STEP_LABELS[$i]}"
  say "     ${STEP_CMDS[$i]}"
  i=$((i + 1))
done
say ""
if [ "$CLAUDE_STATE" = "missing" ]; then
  say "Prefer clicking to typing?  The Claude Code desktop app works just as well"
  say "with CareerKit: https://claude.ai/download   (then skip that step below)"
  say ""
fi
NEEDS_TTY=0
for step in "${STEP_LABELS[@]}"; do
  case "$step" in Homebrew*|"Xcode Command Line Tools"*) NEEDS_TTY=1 ;; esac
done
case "$PLATFORM" in
  linux) say "Some steps use sudo and will ask for your password." ;;
esac
if [ "$NEEDS_TTY" -eq 1 ]; then
  say "Homebrew asks for your macOS password, so it needs a real terminal window."
fi
say "Nothing has been changed yet."
say ""

if [ "$DRY_RUN" -eq 1 ]; then
  say "Dry run: stopping here."
  exit 0
fi

if [ "$NEEDS_TTY" -eq 1 ] && [ ! -t 0 ]; then
  say "This plan includes a step that cannot run without a terminal:"
  say "Homebrew's installer needs your macOS password and refuses to run when"
  say "stdin is not a TTY, so --yes cannot answer it either."
  say ""
  say "Open a normal Terminal window, cd to this folder, and run ./bootstrap.sh"
  say "there.  (If an assistant is driving this, hand these commands to a human.)"
  exit 1
fi

if [ "$YES" -eq 0 ]; then
  if [ ! -t 0 ]; then
    say "Not running interactively and --yes was not given, so nothing was installed."
    say "Re-run with --yes to proceed, or run the commands above yourself."
    exit 1
  fi
  printf 'Run these now? [y/N] '
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) say "Nothing was installed."; exit 1 ;;
  esac
  say ""
fi

# ----------------------------------------------------------------- install ---
run_step() { # run_step <label> <command>
  say "==> $1"
  if ! bash -c "$2"; then
    say ""
    say "FAILED: $1"
    say "  Nothing after this step ran.  Fix the error above, then re-run"
    say "  ./bootstrap.sh -- it skips whatever already succeeded."
    exit 1
  fi
}

i=0
while [ "$i" -lt ${#STEP_CMDS[@]} ]; do
  LABEL="${STEP_LABELS[$i]}"
  CMD="${STEP_CMDS[$i]}"
  case "$LABEL" in
    "put Homebrew on your PATH"*)
      # Homebrew's own installer prints this and stops; skipping it is why a
      # successful install is so often followed by "command not found: brew".
      if [ -x "$BREW_PREFIX/bin/brew" ]; then
        eval "$("$BREW_PREFIX/bin/brew" shellenv)"
        say "==> $LABEL"
        persist_line "eval \"\$($BREW_PREFIX/bin/brew shellenv)\"" "Homebrew"
      else
        say "Homebrew is not at $BREW_PREFIX/bin/brew; follow the PATH instructions"
        say "its installer printed, then re-run ./bootstrap.sh"
        exit 1
      fi
      ;;
    "Xcode Command Line Tools"*)
      # This opens a GUI installer and returns immediately, so verifying git in
      # the same run would fail for a reason the user cannot act on.
      run_step "$LABEL" "$CMD"
      say "    a macOS installer window may have opened; finish it, then re-run ./bootstrap.sh"
      ;;
    "Claude Code")
      run_step "$LABEL" "$CMD"
      # The native installer writes to ~/.local/bin.  Adopt it now so the
      # recheck below is meaningful, and persist it so `claude` is still there
      # in tomorrow's terminal.
      if [ -d "$HOME/.local/bin" ]; then
        case ":$PATH:" in
          *":$HOME/.local/bin:"*) ;;
          *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
        esac
        persist_line 'export PATH="$HOME/.local/bin:$PATH"' "Claude Code"
      fi
      ;;
    *)
      run_step "$LABEL" "$CMD"
      ;;
  esac
  i=$((i + 1))
done

# ------------------------------------------------------------------ verify ---
say ""
say "Rechecking"
FAILED=0
if have git;  then say "  ok       git $(git --version | awk '{print $3}')"; else say "  MISSING  git"; FAILED=1; fi
if python_ok; then say "  ok       python $(python_version)"; else say "  MISSING  python 3.10+"; FAILED=1; fi
careerkit_detect_claude
CLAUDE_STATE="$CAREERKIT_CLAUDE_STATE"
case "$CLAUDE_STATE" in
  ready)   say "  ok       claude code $CAREERKIT_CLAUDE_VERSION" ;;
  desktop) say "  ok       claude code (desktop app)" ;;
  not-on-path)
    say "  MISSING  claude code is installed but not on this shell's PATH"
    say "           run: export PATH=\"\$HOME/.local/bin:\$PATH\""
    FAILED=1 ;;
  *) say "  MISSING  claude code"; FAILED=1 ;;
esac
say ""

if [ "$FAILED" -eq 1 ]; then
  say "Something is still missing.  The usual cause is that a newly installed"
  say "tool is not on this shell's PATH yet: open a new terminal window, come"
  say "back to this folder, and run ./bootstrap.sh again."
  exit 1
fi

say "All prerequisites are installed."
say ""
say "  Next:  ./setup.sh"
