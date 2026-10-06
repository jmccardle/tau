#!/usr/bin/env bash
# gil_gate_negative.sh — experiment: prove build_runtime.py's GIL gate fails on a GIL-needing module.
#
# Usage: gil_gate_negative.sh <out-dir of a built free-threaded runtime> <pbs 3.14t python/ dir with include/>
# Env:   UV (required by build_runtime.py's argument handling)
set -uo pipefail
OUT="$(readlink -f "$1")"; PBS="$(readlink -f "$2")"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SITE="$(echo "$OUT"/tau/python/lib/python3.*t/site-packages)"
SO="$SITE/gilneeder.cpython-314t-x86_64-linux-gnu.so"
gcc -shared -fPIC -O2 -I"$(echo "$PBS"/include/python3.*t)" -o "$SO" "$(dirname "$0")/gilneeder.c"
python3 "$REPO/scripts/build_runtime.py" --out "$OUT" --uv "$UV" --verify-only
echo "gate exit with gilneeder present: $? (expected 1)"
rm -f "$SO"
python3 "$REPO/scripts/build_runtime.py" --out "$OUT" --uv "$UV" --verify-only
echo "gate exit after removing it: $? (expected 0)"
