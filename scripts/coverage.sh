#!/usr/bin/env bash
# Rust line coverage, reported and never gated. Not part of check.sh.
#
#   scripts/coverage.sh     # writes docs/as-built/metrics/data/coverage-{rust,py,both}.{txt,json}
#
# Three reports from one instrumented build tree (target/llvm-cov-target):
#   rust : `cargo test --workspace`
#   py   : `maturin develop` (instrumented debug cdylib) + pytest tests/
#   both : the profiles of the two merged
# Inline `#[cfg(test)]` modules in src/ are part of their files and count; files
# under tests/ and benches/ are excluded.
#
# This overwrites python/tau/_core*.so with the instrumented debug build;
# the script puts back the release build check.sh uses before it exits.
# Needs cargo-llvm-cov, rustup component llvm-tools-preview, and the dev venv.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"
PY="${TAU_PY:-.venv/bin/python}"
BIN="$(cd "$(dirname "$PY")" && pwd)"
export PYO3_PYTHON="$BIN/python"
export VIRTUAL_ENV="$(cd "$BIN/.." && pwd)"
OUT="$ROOT/docs/as-built/metrics/data"
T="$ROOT/target/llvm-cov-target"
IGNORE='(^|/)(tests|benches)/'
mkdir -p "$OUT"

restore() {
    echo "== restoring the release build"
    env -u RUSTC_WRAPPER -u CARGO_LLVM_COV -u CARGO_LLVM_COV_SHOW_ENV -u CARGO_LLVM_COV_TARGET_DIR \
        -u CARGO_LLVM_COV_BUILD_DIR -u CARGO_TARGET_DIR -u LLVM_PROFILE_FILE \
        "$BIN/maturin" develop --release --locked
}
trap restore EXIT

rm -rf "$T"
mkdir -p "$T/prof-rust"
# show-env instruments through a rustc wrapper but shares target/ with check.sh's
# uninstrumented artifacts (cargo would reuse them, uninstrumented), so build apart.
# shellcheck disable=SC1090
source <(cargo llvm-cov show-env --sh 2>/dev/null)
export CARGO_TARGET_DIR="$T" CARGO_LLVM_COV_TARGET_DIR="$T" CARGO_LLVM_COV_BUILD_DIR="$T"
export LLVM_PROFILE_FILE="$T/trusty-%p-%8m.profraw"

report() {  # report <name>; file names relative to the checkout, so no local path is committed
    cargo llvm-cov report --ignore-filename-regex "$IGNORE" >"$OUT/coverage-$1.txt"
    cargo llvm-cov report --ignore-filename-regex "$IGNORE" --json --summary-only --output-path "$OUT/coverage-$1.json"
    sed -i -e "s|\"filename\":\"$ROOT/|\"filename\":\"|g" -e "s|\"manifest_path\":\"$ROOT/|\"manifest_path\":\"|g" \
        "$OUT/coverage-$1.json"
}

echo "== rust tests"
cargo test --workspace --locked
report rust
mv "$T"/*.profraw "$T/prof-rust/"

echo "== python tests against the instrumented cdylib"
"$BIN/maturin" develop --locked
"$PY" -m pytest -q tests
report py

echo "== merged"
mv "$T/prof-rust/"*.profraw "$T/"
report both
tail -n 2 "$OUT"/coverage-{rust,py,both}.txt
