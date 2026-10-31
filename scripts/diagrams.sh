#!/usr/bin/env bash
# Regenerate the diagrams in docs/as-built/diagrams/ from the code (rule 6).
#
#   scripts/diagrams.sh            rewrite the DOT/Markdown sources, render the SVGs
#   scripts/diagrams.sh --check    regenerate into a temp dir and fail if a
#                                  committed DOT/Markdown source differs
#
# Only the text sources are deterministic and compared; SVGs are rendered from
# them (graphviz `dot`, needed to regenerate, not to check).
#
# Needs the in-repo venv (.venv, see CLAUDE.md Commands) with the dev group and
# a build of tau_next (`maturin develop`), and cargo. It builds the core with its
# `transitions` feature into target/diagrams, apart from the normal build.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
MODE="${1:-write}"
case "$MODE" in write | --check) ;; *)
    echo "usage: scripts/diagrams.sh [--check]" >&2
    exit 2
    ;;
esac
PY="${TAU_PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
    echo "diagrams.sh: $PY not found; create the venv first (CLAUDE.md, Commands)" >&2
    exit 1
fi
PY="$(cd "$(dirname "$PY")" && pwd)/python"
export PYO3_PYTHON="$PY"
OUT=docs/as-built/diagrams
D=scripts/diagrams
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
GEN="$TMP/gen"
mkdir -p "$GEN"
step() { printf '\n== %s\n' "$*"; }

# One target dir for every feature build, so the normal build is untouched.
export CARGO_TARGET_DIR="$ROOT/target/diagrams"
# Property tests draw the same cases every run, so scenario counts hold still.
export PROPTEST_RNG_SEED=1

step "type shapes (syn)"
cargo run -q --release --locked --manifest-path "$D/shapes/Cargo.toml" -- \
    crates/tau-core/src/tree.rs:Body crates/tau-core/src/tree.rs:Node \
    crates/tau-core/src/tree.rs:Session crates/tau-core/src/tree.rs:Outcome \
    crates/tau-core/src/tree/part.rs:Part crates/tau-core/src/machine.rs:Machine \
    crates/tau-core/src/step.rs:Step crates/tau-core/src/step.rs:Job \
    crates/tau-core/src/step.rs:Answer crates/tau-core/src/step.rs:Pos \
    >"$TMP/types.json"
"$PY" -I "$D/type_diagrams.py" "$TMP/types.json" "$GEN"

step "frontend refusals (every call in every state)"
"$PY" "$D/refusals.py" >"$TMP/refusals.json"
"$PY" -I "$D/refusals_render.py" "$TMP/refusals.json" "$GEN"

step "step positions: tau-core tests"
mkdir -p "$TMP/logs"
for t in crates/tau-core/tests/*.rs; do
    name="$(basename "$t" .rs)"
    cargo test -q --locked -p tau-core --features transitions --test "$name" \
        -- --nocapture >/dev/null 2>"$TMP/logs/rust-$name.log"
done

step "step positions: behavior suite through the bindings"
cargo build -q --release --locked -p tau-py --features abi3,transitions
PKG="$CARGO_TARGET_DIR/pkg"
rm -rf "$PKG"
mkdir -p "$PKG"
cp -r python/tau_next "$PKG/tau_next"
cp "$CARGO_TARGET_DIR/release/lib_core.so" "$PKG/tau_next/_core.abi3.so"
PYTHONPATH="$PKG:$ROOT/$D" "$PY" -m pytest -q -s -p no:cacheprovider -p pytest_scenario \
    tests/behavior tests/core >/dev/null 2>"$TMP/logs/python.log"
"$PY" -I "$D/positions.py" "$TMP/types.json" "$GEN" "$TMP"/logs/*.log

if [ "$MODE" = "--check" ]; then
    status=0
    for f in "$GEN"/*; do
        if ! diff -u "$OUT/$(basename "$f")" "$f"; then
            echo "diagrams.sh: $OUT/$(basename "$f") is stale; run scripts/diagrams.sh" >&2
            status=1
        fi
    done
    [ "$status" = 0 ] && echo "diagrams: up to date"
    exit "$status"
fi

command -v dot >/dev/null || {
    echo "diagrams.sh: graphviz 'dot' is needed to render SVGs" >&2
    exit 1
}
step "write $OUT"
mkdir -p "$OUT"
cp "$GEN"/* "$OUT"/
for f in "$GEN"/*.dot; do
    dot -Tsvg "$f" -o "$OUT/$(basename "$f" .dot).svg"
done
