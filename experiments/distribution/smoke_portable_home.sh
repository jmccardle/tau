#!/usr/bin/env bash
# smoke_portable_home.sh — experiment: a portable bundle keeps ALL its state on the "drive".
#
# Usage: smoke_portable_home.sh <tau-*-linux-*.tar.gz from build_runtime.py --portable-home> [<.AppImage>]
# Unpacks to a fake mount path, runs with env -i and an empty HOME and TMPDIR, and lists
# every file written outside the drive (expected: none).
set -euo pipefail
TARBALL="$(readlink -f "$1")"; APPIMAGE_FILE="${2:+$(readlink -f "$2")}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'kill "${SRV:-0}" 2>/dev/null || true; rm -rf "$WORK"' EXIT
DRIVE="$WORK/media/USBSTICK"
mkdir -p "$DRIVE" "$WORK/home" "$WORK/tmp" "$WORK/cwd"
tar -xzf "$TARBALL" -C "$DRIVE"
PY="$DRIVE/tau/$(sed -n 's/.*"interpreter": "\([^"]*\)".*/\1/p' "$DRIVE/tau/manifest.json")"
PORT=18799
"$PY" -I -B "$HERE/stub_openai.py" "$PORT" & SRV=$!
sleep 1
CONFIG="{\"default_model\": \"stub\", \"models\": {\"stub\": {\"backend\": \"openai\", \"model\": \"stub\", \"base_url\": \"http://127.0.0.1:$PORT/v1\", \"api_key\": \"x\"}}}"
clean() { (cd "$WORK/cwd" && env -i PATH=/usr/bin:/bin HOME="$WORK/home" TMPDIR="$WORK/tmp" TERM=xterm-256color "$@"); }
outside() { find "$WORK/home" "$WORK/tmp" "$WORK/cwd" -mindepth 1 | sed "s#^$WORK#<work>#"; }

echo "== drive layout: $(ls -A "$DRIVE/tau" | tr '\n' ' ')"
echo "$CONFIG" > "$DRIVE/tau/.tau/config.json"
echo "== tau -p, config only on the drive"
clean "$DRIVE/tau/bin/tau" -p "ping"
echo "== the session landed on the drive:"
find "$DRIVE/tau/.tau" -type f | sed "s#^$DRIVE#<drive>#"
echo "== TUI for 6 s in a pty"
clean timeout -s INT 6 script -qfec "stty cols 120 rows 40; '$DRIVE/tau/bin/tau'" /dev/null </dev/null >/dev/null 2>&1 || true
echo "== files written outside the drive (HOME, TMPDIR, cwd); expected none:"
outside
echo "== [end of list]"

echo "== same bundle, .tau/ removed: an ordinary install"
mv "$DRIVE/tau/.tau" "$DRIVE/tau/.tau.off"
mkdir -p "$WORK/home/.tau" && echo "$CONFIG" > "$WORK/home/.tau/config.json"
clean "$DRIVE/tau/bin/tau" -p "ping"
find "$WORK/home/.tau" -type f -name '*.jsonl' | sed "s#^$WORK#<work>#"
rm -rf "$WORK/home/.tau"; mv "$DRIVE/tau/.tau.off" "$DRIVE/tau/.tau"

if [ -n "$APPIMAGE_FILE" ]; then
    echo "== AppImage on a drive with .tau/ beside it"
    STICK2="$WORK/media/STICK2"
    mkdir -p "$STICK2/.tau"
    cp "$APPIMAGE_FILE" "$STICK2/tau.AppImage"
    echo "$CONFIG" > "$STICK2/.tau/config.json"
    clean "$STICK2/tau.AppImage" -p "ping"
    find "$STICK2/.tau" -type f | sed "s#^$WORK#<work>#"
    echo "== files written outside the drive; expected none:"
    outside
    echo "== [end of list]"
fi
