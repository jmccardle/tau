#!/usr/bin/env bash
# Install vhs and ttyd (official GitHub release binaries, Linux x86_64) into $BIN_DIR
# (default ~/.local/bin) when they are not already on PATH. Checksums are verified.
set -euo pipefail

VHS_VERSION="${VHS_VERSION:-0.12.1}"
TTYD_VERSION="${TTYD_VERSION:-1.7.7}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

[ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] || {
  echo "install-tools.sh: only Linux x86_64 is scripted; install vhs and ttyd yourself" >&2; exit 1; }

mkdir -p "$BIN_DIR"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

if ! command -v vhs >/dev/null; then
  base="https://github.com/charmbracelet/vhs/releases/download/v$VHS_VERSION"
  f="vhs_${VHS_VERSION}_Linux_x86_64.tar.gz"
  curl -fsSL -o "$tmp/$f" "$base/$f"
  curl -fsSL -o "$tmp/checksums.txt" "$base/checksums.txt"
  (cd "$tmp" && grep " $f\$" checksums.txt | sha256sum -c -)
  mkdir "$tmp/vhs" && tar -xzf "$tmp/$f" -C "$tmp/vhs"
  install -m 0755 "$(find "$tmp/vhs" -name vhs -type f | head -1)" "$BIN_DIR/vhs"
fi

if ! command -v ttyd >/dev/null; then
  base="https://github.com/tsl0922/ttyd/releases/download/$TTYD_VERSION"
  curl -fsSL -o "$tmp/ttyd.x86_64" "$base/ttyd.x86_64"
  curl -fsSL -o "$tmp/SHA256SUMS" "$base/SHA256SUMS"
  (cd "$tmp" && grep " ttyd.x86_64\$" SHA256SUMS | sha256sum -c -)
  install -m 0755 "$tmp/ttyd.x86_64" "$BIN_DIR/ttyd"
fi

for t in vhs ttyd ffmpeg tmux; do
  command -v "$t" >/dev/null || { echo "missing: $t (is $BIN_DIR on PATH?)" >&2; exit 1; }
done
vhs --version
ttyd --version
