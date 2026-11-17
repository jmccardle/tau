#!/usr/bin/env bash
# Build the two isolated installs the races compare, under $DEMO_WORK (default $TMPDIR/tau-demo-UID):
#   venv15  ffwf-tau==0.15.0 from PyPI
#   venv16  a wheel built from this checkout (maturin build --release; not editable)
# Idempotent: an existing venv15 must hold the pinned version, and the 0.16 wheel is rebuilt when the
# checkout (HEAD, plus uncommitted changes to tracked files) is not the one it was built from, or
# with REBUILD=1. Needs: python3.12 (or $PYTHON), cargo (rustup), network for PyPI.
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
have15="$("$WORK/venv15/bin/tau" --version)"
[ "$have15" = "tau $V15" ] || {
  echo "setup: $WORK/venv15 holds '$have15', not tau $V15: remove it and rerun" >&2; exit 1; }

# The wheel is built from this checkout, so it is stamped with the commit (and, for a dirty tree,
# a hash of the uncommitted changes to tracked files); a different stamp means a rebuild.
stamp="$(git -C "$ROOT" rev-parse HEAD)"
if ! git -C "$ROOT" diff --quiet HEAD; then
  stamp="$stamp+dirty-$(git -C "$ROOT" diff HEAD | sha256sum | cut -c1-12)"
fi
if [ -n "${REBUILD:-}" ] || ! ls "$WORK"/wheels/ffwf_tau-*.whl >/dev/null 2>&1 \
   || [ "$(cat "$WORK/wheels/COMMIT" 2>/dev/null)" != "$stamp" ]; then
  echo "== 0.16: building a release wheel from $ROOT at $stamp"
  [ -x "$WORK/buildenv/bin/maturin" ] || {
    "$PYTHON" -m venv "$WORK/buildenv"
    "$WORK/buildenv/bin/pip" install -q 'maturin>=1.15,<2'
  }
  rm -f "$WORK"/wheels/ffwf_tau-*.whl "$WORK/wheels/COMMIT"
  # maturin refuses a bare venv without VIRTUAL_ENV when it needs an interpreter; name one.
  (cd "$ROOT" && VIRTUAL_ENV="$WORK/buildenv" "$WORK/buildenv/bin/maturin" build --release \
      --interpreter "$PYTHON" --out "$WORK/wheels")
  echo "$stamp" > "$WORK/wheels/COMMIT"
  rm -rf "$WORK/venv16"
fi

if [ ! -x "$WORK/venv16/bin/tau" ]; then
  "$PYTHON" -m venv "$WORK/venv16"
  "$WORK/venv16/bin/pip" install -q "$WORK"/wheels/ffwf_tau-*.whl
fi

echo "0.15: $have15"
echo "0.16: $("$WORK/venv16/bin/tau" --version) from $stamp"
