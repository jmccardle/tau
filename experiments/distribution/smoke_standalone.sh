#!/usr/bin/env bash
# smoke_standalone.sh — experiment: what of the Rio variant can be checked without a display.
#
# Usage: smoke_standalone.sh <OUT dir containing tau/ from --variant standalone>
set -uo pipefail
ROOT="$(readlink -f "$1")/tau"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
echo "== layout: $(ls "$ROOT/terminal" "$ROOT/terminal/config" | tr '\n' ' ')"
echo "== rio --version from the tree, clean env"
env -i PATH=/usr/bin:/bin HOME="$WORK" "$ROOT/terminal/rio" --version
echo "== launcher parses: $(sh -n "$ROOT/bin/tau-window" && echo ok)"
echo "== RIO_CONFIG_HOME is honoured (rio writes its default config where the variable points)"
mkdir -p "$WORK/cfg"
env -i PATH=/usr/bin:/bin HOME="$WORK" RIO_CONFIG_HOME="$WORK/cfg" "$ROOT/terminal/rio" --write-config >/dev/null 2>&1
ls "$WORK/cfg"
echo "== highest glibc symbol rio needs: $(objdump -T "$ROOT/terminal/rio" | grep -o 'GLIBC_2\.[0-9]*' | sort -uV | tail -1)"
echo "== the launcher's exec line:"
grep exec "$ROOT/bin/tau-window"
