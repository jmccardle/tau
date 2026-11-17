#!/usr/bin/env bash
# Build the two isolated installs the races compare, under $DEMO_WORK (default $TMPDIR/tau-demo-UID):
#   venv15  ffwf-tau==0.15.0 from PyPI
#   venv16  a wheel built from this checkout (maturin build --release; not editable)
# Idempotent: an existing venv is reused, and the 0.16 wheel is rebuilt only when asked
# (REBUILD=1) or missing. Needs: python3.12 (or $PYTHON), cargo (rustup), network for PyPI.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
WORK="${DEMO_WORK:-${TMPDIR:-/tmp}/tau-demo-$(id -u)}"
PYTHON="${PYTHON:-python3.12}"
V15="${TAU15_VERSION:-0.15.0}"

command -v "$PYTHON" >/dev/null || { echo "setup: $PYTHON not found (set PYTHON=/path/to/python3.12)" >&2; exit 1; }
command -v cargo >/dev/null || { echo "setup: cargo not on PATH (install rustup; 'source ~/.cargo/env')" >&2; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "setup: $PYTHON is older than 3.12" >&2; exit 1; }

mkdir -p "$WORK/wheels"

if [ ! -x "$WORK/venv15/bin/tau" ]; then
  echo "== 0.15: ffwf-tau==$V15 from PyPI"
  "$PYTHON" -m venv "$WORK/venv15"
  "$WORK/venv15/bin/pip" install -q "ffwf-tau==$V15"
fi

if [ -n "${REBUILD:-}" ] || ! ls "$WORK"/wheels/ffwf_tau-*.whl >/dev/null 2>&1; then
  echo "== 0.16: building a release wheel from $ROOT"
  [ -x "$WORK/buildenv/bin/maturin" ] || {
    "$PYTHON" -m venv "$WORK/buildenv"
    "$WORK/buildenv/bin/pip" install -q 'maturin>=1.15,<2'
  }
  rm -f "$WORK"/wheels/ffwf_tau-*.whl
  # maturin refuses a bare venv without VIRTUAL_ENV when it needs an interpreter; name one.
  (cd "$ROOT" && VIRTUAL_ENV="$WORK/buildenv" "$WORK/buildenv/bin/maturin" build --release \
      --interpreter "$PYTHON" --out "$WORK/wheels")
  rm -rf "$WORK/venv16"
fi

if [ ! -x "$WORK/venv16/bin/tau" ]; then
  "$PYTHON" -m venv "$WORK/venv16"
  "$WORK/venv16/bin/pip" install -q "$WORK"/wheels/ffwf_tau-*.whl
fi

echo "0.15: $("$WORK/venv15/bin/tau" --version)"
echo "0.16: $("$WORK/venv16/bin/tau" --version)"
