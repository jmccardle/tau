#!/usr/bin/env bash
# A Windows x64 test bundle, cross-compiled here: one zip to unpack on a Windows machine and run
# `powershell -ExecutionPolicy Bypass -File .\run-tests.ps1`, which writes one results file.
#
#     scripts/windows_bundle.sh            -> dist-win/tau-win-<commit>.zip
#
# The zip holds the ffwf-tau wheel (cp312-abi3, win_amd64) built with the MinGW target
# (x86_64-pc-windows-gnu), a pinned uv.exe (verified by sha256) that installs a pinned CPython
# and the test dependencies, the source tree of the commit (for its tests), and
# scripts/windows/run-tests.ps1. The wheel's native module must import only Windows' own DLLs
# and python3.dll: a MinGW runtime DLL (libgcc, winpthread, libstdc++) is refused, as a user's
# machine has none.
#
# The bundle is of a commit, so the tree must be clean. The official Windows wheel is built
# natively with MSVC (publish.yml); this one is for testing on a machine without CI.
#
# Requires: the in-repo venv (.venv, with maturin), rustup's x86_64-pc-windows-gnu target for the
# pinned toolchain, x86_64-w64-mingw32-gcc and -objdump, curl, sha256sum, zip, git.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

UV_VERSION="0.13.7"
UV_SHA256="0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021"
PYTHON_VERSION="3.12.16"
TARGET="x86_64-pc-windows-gnu"
ALLOWED_DLLS="advapi32.dll api-ms-win-core-synch-l1-2-0.dll bcryptprimitives.dll kernel32.dll msvcrt.dll ntdll.dll python3.dll userenv.dll ws2_32.dll"

[ $# -eq 0 ] || { sed -n '2,19p' "$0"; exit 2; }
for tool in curl sha256sum zip git x86_64-w64-mingw32-objdump x86_64-w64-mingw32-gcc; do
    command -v "$tool" >/dev/null || { echo "windows_bundle: $tool not found" >&2; exit 1; }
done
[ -x .venv/bin/maturin ] || { echo "windows_bundle: .venv/bin/maturin not found (CLAUDE.md, Commands)" >&2; exit 1; }
rustup target list --installed | grep -qx "$TARGET" || {
    echo "windows_bundle: rustup target $TARGET missing: rustup target add $TARGET" >&2; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "windows_bundle: the tree has uncommitted changes; the bundle is of a commit" >&2
    exit 1
fi

commit="$(git rev-parse --short=10 HEAD)"
name="tau-win-$commit"
out="$ROOT/dist-win"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
stage="$work/$name"
mkdir -p "$stage/wheels" "$out"

echo "== wheel ($TARGET)"
PATH="$ROOT/.venv/bin:$PATH" .venv/bin/maturin build --release --locked --target "$TARGET" \
    --out "$stage/wheels"
wheel="$(ls "$stage"/wheels/*.whl)"

echo "== the native module's DLLs"
mkdir "$work/whl"
(cd "$work/whl" && unzip -q "$wheel" 'tau/_core.pyd')
dlls="$(x86_64-w64-mingw32-objdump -p "$work/whl/tau/_core.pyd" | sed -n 's/^\s*DLL Name: //p' \
    | tr 'A-Z' 'a-z' | sort -u)"
echo "$dlls"
for dll in $dlls; do
    case " $ALLOWED_DLLS " in
        *" $dll "*) ;;
        *) echo "windows_bundle: _core.pyd needs $dll, which a Windows machine may not have" >&2; exit 1 ;;
    esac
done

echo "== uv $UV_VERSION"
cache="$ROOT/target/windows-bundle"
mkdir -p "$cache"
uvzip="$cache/uv-$UV_VERSION-x86_64-pc-windows-msvc.zip"
if [ ! -f "$uvzip" ]; then
    curl -fsSL --retry 3 -o "$uvzip.part" \
        "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-pc-windows-msvc.zip"
    mv "$uvzip.part" "$uvzip"
fi
echo "$UV_SHA256  $uvzip" | sha256sum -c --quiet - || {
    echo "windows_bundle: $uvzip is not uv $UV_VERSION (sha256); remove it and retry" >&2; exit 1; }
(cd "$stage" && unzip -q -j "$uvzip" uv.exe)

echo "== source tree"
mkdir "$stage/src"
git archive HEAD | tar -x -C "$stage/src"

cp scripts/windows/run-tests.ps1 "$stage/"
echo "$PYTHON_VERSION" >"$stage/PYTHON_VERSION"
printf '%s, commit %s (%s), %s, uv %s, CPython %s\n' "$name" "$(git rev-parse HEAD)" \
    "$(git log -1 --format=%cs)" "$(basename "$wheel")" "$UV_VERSION" "$PYTHON_VERSION" \
    >"$stage/BUNDLE"
cat >"$stage/README.txt" <<EOF
tau Windows test bundle ($name)

1. Unzip this folder anywhere you can write (Desktop is fine).
2. In PowerShell, in the unzipped folder, run:

       powershell -ExecutionPolicy Bypass -File .\\run-tests.ps1

   (-Quick skips the whole suite and runs the smoke check and the serve tests only.)
3. Bring back the file it names at the end: results-<computer>-<time>.txt

It needs the network once (CPython from uv's mirror, the test dependencies from PyPI). Everything
it installs stays in this folder (venv\\), apart from uv's CPython download in uv's cache.
EOF

rm -f "$out/$name.zip"
(cd "$work" && zip -qr "$out/$name.zip" "$name")
ls -l "$out/$name.zip"
