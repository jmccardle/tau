#!/usr/bin/env bash
# The whole gate, in the order CI runs it. Local and CI run this same file.
#
# Requires the in-repo venv (.venv) with the dev group installed; see
# CLAUDE.md "Commands". Uses no tool from PATH except cargo and rust-code-analysis-cli (the ratchet; both
# in ~/.cargo/bin, installed by scripts/ci-toolchain.sh).
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

export PATH="$HOME/.cargo/bin:$PATH"

step() { printf '\n== %s\n' "$*"; }

step "rustfmt";      cargo fmt --all --check
step "clippy";       cargo clippy --workspace --all-targets --locked -- -D warnings
step "cargo test";   cargo test --workspace --locked
step "build";        VIRTUAL_ENV="$(cd "$BIN/.." && pwd)" "$BIN/maturin" develop --release --locked
step "rules";        "$PY" scripts/check_rules.py
step "ratchet";      "$PY" scripts/ratchet.py          # rule 7, Rust and Python; needs rust-code-analysis-cli, radon
step "core is IO-free"; "$PY" scripts/check_core_io.py
step "stubtest";     "$PY" -m mypy.stubtest tau._core
step "ruff";         "$BIN/ruff" check .
"$BIN/ruff" format --check .
step "pyright";      "$BIN/pyright"
step "pytest";       "$PY" -m pytest -q tests
step "wire coding";  "$PY" scripts/check_wire.py
step "wire schema";  "$PY" scripts/schema.py --check   # docs/protocol.schema.json is generated (D21 §8)
step "api docs";     "$PY" scripts/api_docs.py --check   # docs/api/surface.txt, docs/reference/, and _core.pyi's docstrings against the built module
step "protocol guide"; "$PY" scripts/protocol_guide.py --check   # docs/guide/protocol.md is generated from the schema
step "guide examples"; "$PY" scripts/guide_examples.py   # every python and bash block in docs/guide runs, on a temporary TAU_HOME
step "diagrams";     scripts/diagrams.sh --check   # rule 6; builds a feature-enabled core apart
