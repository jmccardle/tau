#!/usr/bin/env bash
# The whole gate, in the order CI runs it. Local and CI run this same file.
#
# Requires the in-repo venv (.venv) with the dev group installed; see
# CLAUDE.md "Commands". Does not need τ 0.15: parity is scripts/parity.sh. Uses no tool from PATH except cargo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${TAU_PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
    echo "check.sh: $PY not found; create the venv first (CLAUDE.md, Commands)" >&2
    exit 1
fi
BIN="$(dirname "$PY")"
# PyO3 build scripts otherwise pick `python3` from PATH, which may be a
# different interpreter (or below the floor) for clippy and cargo test.
export PYO3_PYTHON="$(cd "$BIN" && pwd)/python"

step() { printf '\n== %s\n' "$*"; }

step "rustfmt";      cargo fmt --all --check
step "clippy";       cargo clippy --workspace --all-targets --locked -- -D warnings
step "cargo test";   cargo test --workspace --locked
step "build";        VIRTUAL_ENV="$(cd "$BIN/.." && pwd)" "$BIN/maturin" develop --release --locked
step "rules";        "$PY" scripts/check_rules.py
step "stubtest";     "$PY" -m mypy.stubtest tau_next._core
step "ruff";         "$BIN/ruff" check .
"$BIN/ruff" format --check .
step "pyright";      "$BIN/pyright"
step "pytest";       "$PY" -m pytest -q tests
