#!/usr/bin/env bash
# Regenerate docs/as-built/metrics: data and SVGs. Not part of check.sh.
#
#   scripts/metrics.sh            # Rust metrics, machete, graphs, core IO check
#   scripts/coverage.sh           # coverage, separately (slow)
#
# Needs (versions in docs/as-built/metrics/README.md and scripts/ci-toolchain.sh):
# rust-code-analysis-cli, cargo-machete, cargo-modules, cargo-depgraph, graphviz (dot).
# The tau 0.15 baseline is separate (scripts/py_baseline.py, needs radon).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"
PY="${TAU_PY:-.venv/bin/python}"
OUT=docs/as-built/metrics
mkdir -p "$OUT/data" "$OUT/svg"

"$PY" scripts/metrics.py

# Unused dependencies. machete exits 1 when it finds any; that is a finding, not a failure here.
{ cargo machete --with-metadata 2>&1 || true; } >"$OUT/data/machete.txt"

# Dependency graph between workspace crates, and with all external crates.
cargo depgraph --workspace-only --dedup-transitive-deps | dot -Tsvg >"$OUT/svg/deps-workspace.svg"
cargo depgraph --dedup-transitive-deps | dot -Tsvg >"$OUT/svg/deps-all.svg"

# Module graphs per crate (modules only, `uses` edges) and item graphs (types too).
# neato: cargo-modules' dot layout drops edges between record nodes.
: >"$OUT/data/module-edges.txt"
for c in tau-core tau-store tau-tools tau-py; do
    cargo modules dependencies -p "$c" --lib --no-externs --no-sysroot --no-fns --no-traits \
        --no-types --no-owns --layout neato 2>/dev/null >"$OUT/data/modules-$c.dot"
    dot -Kneato -Tsvg -Goverlap=false "$OUT/data/modules-$c.dot" >"$OUT/svg/modules-$c.svg"
    grep -- '->' "$OUT/data/modules-$c.dot" | sed -E 's/^ *//; s/ \[.*//' | sed "s/^/$c: /" >>"$OUT/data/module-edges.txt" || true
    cargo modules dependencies -p "$c" --lib --no-externs --no-sysroot --no-fns --no-traits \
        --layout neato 2>/dev/null | dot -Kneato -Tsvg -Goverlap=false >"$OUT/svg/items-$c.svg"
done
rm -f "$OUT"/data/modules-*.dot

"$PY" scripts/check_core_io.py --list | tee "$OUT/data/core-io.txt"
# Dead code: what the lint gate is, as configured (not changed here).
{
    echo "workspace lints (Cargo.toml):"
    sed -n '/^\[workspace.lints/,/^$/p' Cargo.toml
    echo "per-crate #![deny/forbid/warn] attributes:"
    grep -rn '#!\[\(deny\|forbid\|warn\|allow\)' crates/*/src | grep -v bench-formats || echo "(none)"
    echo "check.sh runs clippy with -D warnings, which makes rustc's dead_code (warn by default) an error."
} >"$OUT/data/lints.txt"
echo "metrics written to $OUT"
