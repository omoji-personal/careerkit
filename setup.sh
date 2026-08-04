#!/bin/bash
# CareerKit prerequisite check + bootstrap. Safe to re-run.
set -e
echo "CareerKit setup"
command -v python3 >/dev/null || { echo "MISSING: python3 (install from python.org or 'brew install python3')"; exit 1; }
python3 -c "import yaml" 2>/dev/null || { echo "Installing PyYAML..."; python3 -m pip install --user pyyaml; }
command -v git >/dev/null || { echo "MISSING: git (install Xcode command line tools)"; exit 1; }
mkdir -p profile data out
[ -f profile/employers.yaml ] || printf 'employers: []\nfeeds:\n- {name: remotive, active: true}\n- {name: remoteok, active: true}\n- {name: himalayas, active: true}\n- {name: jobicy, active: true}\n- {name: themuse, active: true}\n- {name: weworkremotely, active: true}\n- {name: workingnomads, active: true}\n- {name: arbeitnow, active: true}\n' > profile/employers.yaml
python3 -c "import sys; sys.path.insert(0,'.'); import engine.score, engine.adapters" && echo "Engine OK."
echo
echo "Done. Open this folder in Claude Code and run /setup to build your profile."
