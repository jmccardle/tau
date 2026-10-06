#!/usr/bin/env bash
# try_pyinstaller.sh — experiment: freeze τ with PyInstaller (onedir and onefile) for comparison.
#
# Usage: try_pyinstaller.sh <work-dir> <python3.14 interpreter> <wheels dir>
# Env:   UV (path to a uv binary; required)
set -euo pipefail
WORK="$(readlink -f "$1")"; PYI_PY="$2"; WHEELS="$(readlink -f "$3")"
: "${UV:?set UV}"
VERSION="$(ls "$WHEELS" | sed -n 's/^ffwf_tau-\([0-9.]*\)-.*/\1/p')"
mkdir -p "$WORK"
cd "$WORK"
[ -d venv ] || "$UV" venv --python "$PYI_PY" venv
"$UV" pip install --python venv/bin/python --find-links "$WHEELS" "ffwf-tau==$VERSION" pyinstaller >/dev/null
printf 'import sys\nfrom tau_coding_agent.cli import main\nsys.exit(main())\n' > tau_entry.py
COMMON=(--noconfirm --log-level ERROR --name tau --collect-data tau_coding_agent --collect-data textual
        --collect-submodules tau_coding_agent --collect-submodules tau_agent_core --collect-submodules tau_llm
        --collect-submodules textual --collect-submodules anthropic --collect-submodules google.genai)
venv/bin/pyinstaller "${COMMON[@]}" --onedir --distpath dist-onedir tau_entry.py
venv/bin/pyinstaller "${COMMON[@]}" --onefile --distpath dist-onefile tau_entry.py
du -sh dist-onedir/tau dist-onefile/tau
