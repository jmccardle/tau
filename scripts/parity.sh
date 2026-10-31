#!/usr/bin/env bash
# Parity: the API diff against τ 0.15 (see parity/README.md). Separate from
# check.sh on purpose: the test suite must pass without τ 0.15 installed.
#
# Requires the venv with the dev and parity groups, and a current build
# (scripts/check.sh, or `maturin develop`).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${TAU_PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
    echo "parity.sh: $PY not found; create the venv first (CLAUDE.md, Commands)" >&2
    exit 1
fi
if ! "$PY" -c "import tau_agent_core" 2>/dev/null; then
    echo "parity.sh: τ 0.15 is not installed; pip install --group parity" >&2
    exit 1
fi

"$PY" -m pytest -q parity
