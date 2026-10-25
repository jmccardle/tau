#!/usr/bin/env bash
# The whole gate, in the order CI runs it. Local and CI run this same file.
#
# Requires the in-repo venv (.venv) with the dev and oracle groups installed;
# see CLAUDE.md "Commands". Uses no tool from PATH except cargo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${TAU_PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
    echo "check.sh: $PY not found; create the venv first (CLAUDE.md, Commands)" >&2
    exit 1
fi
BIN="$(dirname "$PY")"

step() { printf '\n== %s\n' "$*"; }

step "rustfmt";      cargo fmt --all --check
step "clippy";       cargo clippy --workspace --all-targets --locked -- -D warnings
step "cargo test";   cargo test --workspace --locked
step "build";        VIRTUAL_ENV="$(cd "$BIN/.." && pwd)" "$BIN/maturin" develop --release --locked
step "rules";        "$PY" scripts/check_rules.py
step "stubs";        "$PY" scripts/check_stubs.py
step "ruff";         "$BIN/ruff" check . && "$BIN/ruff" format --check .
step "pyright";      "$BIN/pyright"
step "pytest";       "$PY" -m pytest -q
