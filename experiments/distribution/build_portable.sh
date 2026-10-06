#!/usr/bin/env bash
# build_portable.sh — experiment: a relocatable τ bundle on python-build-standalone.
#
# Usage: build_portable.sh <out-dir> [gil|freethreaded]
# Env:   PY_VERSION (3.14.8)  PBS_TAG (20261003)  TARGET (x86_64-unknown-linux-gnu)
#        WHEEL_PLATFORM (x86_64-manylinux_2_28; the bundle's glibc floor)  TRIM (1)  ALLOW_SOURCE (0)
#        UV (path to a uv binary; required)  CACHE (download cache; default <out-dir>/cache)
#
# Produces <out-dir>/tau-<ver>-<os>-<arch>[-ft].tar.gz holding tau/{bin/tau,python/}.
set -euo pipefail

OUT="${1:?usage: build_portable.sh <out-dir> [gil|freethreaded]}"
FLAVOR="${2:-gil}"
PY_VERSION="${PY_VERSION:-3.14.8}"
PBS_TAG="${PBS_TAG:-20261003}"
TARGET="${TARGET:-x86_64-unknown-linux-gnu}"
: "${UV:?set UV to a uv binary}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CACHE="${CACHE:-$OUT/cache}"

case "$FLAVOR" in
    gil) PBS_FLAVOR="install_only_stripped"; SUFFIX="" ;;
    freethreaded) PBS_FLAVOR="freethreaded-install_only_stripped"; SUFFIX="-ft" ;;
    *) echo "flavor must be gil or freethreaded, got: $FLAVOR" >&2; exit 2 ;;
esac

VERSION="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' "$REPO/tau-coding-agent/src/tau_coding_agent/__init__.py")"
[ -n "$VERSION" ] || { echo "could not read τ version" >&2; exit 1; }

PBS_NAME="cpython-${PY_VERSION}+${PBS_TAG}-${TARGET}-${PBS_FLAVOR}.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_NAME}"

mkdir -p "$OUT" "$CACHE"
[ -f "$CACHE/$PBS_NAME" ] || curl -fsSL -o "$CACHE/$PBS_NAME" "$PBS_URL"

WHEELS="$OUT/wheels-$VERSION"
if [ ! -d "$WHEELS" ]; then
    mkdir -p "$WHEELS"
    for pkg in tau-llm tau-agent-core tau-coding-agent tau-jmfts tau-meta; do
        "$UV" build --wheel --out-dir "$WHEELS" "$REPO/$pkg"
    done
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/tau"
mkdir -p "$ROOT/bin"
tar -xzf "$CACHE/$PBS_NAME" -C "$ROOT"
PY="$ROOT/python/bin/python3"

# PBS marks its stdlib EXTERNALLY-MANAGED; this interpreter is ours alone, so the marker goes.
rm -f "$ROOT"/python/lib/python3.*/EXTERNALLY-MANAGED

# Pin the glibc floor: left alone, uv picks wheels for the BUILD host's glibc (2.34 seen on a 2.36 host).
# The pin binds wheels only; a source build links the host glibc, so sdists are refused unless asked.
BINARY_ONLY=(--only-binary :all:)
[ "${ALLOW_SOURCE:-0}" = 1 ] && BINARY_ONLY=()
"$UV" pip install --python "$PY" --find-links "$WHEELS" --compile-bytecode \
    --python-platform "${WHEEL_PLATFORM:-x86_64-manylinux_2_28}" "${BINARY_ONLY[@]}" \
    "ffwf-tau==$VERSION"

# pip-style console scripts carry an absolute shebang into the staging dir; they cannot relocate.
find "$ROOT/python/bin" -mindepth 1 -maxdepth 1 ! -name 'python3*' ! -name 'python' -delete

if [ "${TRIM:-1}" = 1 ]; then
    P="$ROOT/python"
    LIBPY="$(echo "$P"/lib/python3.*)"
    # The PBS interpreter links libpython statically on Linux; the shared copy is dead weight there.
    if ! ldd "$P/bin/python3" | grep -q libpython; then rm -f "$P"/lib/libpython3*.so*; fi
    rm -rf "$P/include" "$P/share" "$P"/lib/tcl* "$P"/lib/tk* "$P"/lib/itcl* "$P"/lib/thread* \
        "$P"/lib/pkgconfig "$LIBPY"/{idlelib,tkinter,turtledemo,turtle.py,ensurepip} \
        "$LIBPY"/lib-dynload/_tkinter* "$LIBPY"/config-* \
        "$LIBPY"/site-packages/pip "$LIBPY"/site-packages/pip-*.dist-info
fi

# PBS ships only part of the stdlib precompiled; a read-only install (AppImage, /opt as root) would
# recompile the rest in memory on every launch. unchecked-hash .pyc survive mtime-mangling packagers.
"$PY" -I -m compileall -q -j0 --invalidation-mode unchecked-hash "$ROOT"/python/lib/python3.* >/dev/null

cat > "$ROOT/bin/tau" <<'LAUNCHER'
#!/bin/sh
# Relocatable launcher: resolves its own location, runs the bundled interpreter isolated (-I).
here="$(dirname "$(readlink -f "$0")")"
exec "$here/../python/bin/python3" -I -m tau_coding_agent.cli "$@"
LAUNCHER
chmod 755 "$ROOT/bin/tau"
cp "$REPO/LICENSE" "$ROOT/LICENSE"

ARCH="${TARGET%%-*}"
OS="linux"
NAME="tau-${VERSION}-${OS}-${ARCH}${SUFFIX}"
tar -C "$STAGE" -czf "$OUT/$NAME.tar.gz" tau
echo "built $OUT/$NAME.tar.gz (python $PY_VERSION $FLAVOR)"
