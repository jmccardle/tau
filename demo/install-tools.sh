#!/usr/bin/env bash
# Install vhs and ttyd (official GitHub release binaries, Linux x86_64) into $BIN_DIR
# (default ~/.local/bin), at the pinned versions below, verified against the SHA256 pinned here.
# The copy in $BIN_DIR is the one used: render.sh puts $BIN_DIR first on PATH. A vhs or ttyd of
# another version elsewhere on PATH is ignored, not trusted.
#
# Another version needs its checksum too: VHS_VERSION=x.y.z VHS_SHA256=<sha256 of the tarball>
# (likewise TTYD_VERSION and TTYD_SHA256, of the ttyd.x86_64 binary).
set -euo pipefail

PINNED_VHS=0.12.1
PINNED_VHS_SHA256=eb33787a5b1cebf91fe0b18bd231e2d71d36c1ff4babd99ddac1a6ab411efd10   # vhs_0.12.1_Linux_x86_64.tar.gz
PINNED_TTYD=1.7.7
PINNED_TTYD_SHA256=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55   # ttyd.x86_64

VHS_VERSION="${VHS_VERSION:-$PINNED_VHS}"
TTYD_VERSION="${TTYD_VERSION:-$PINNED_TTYD}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

sum_for() {  # NAME VERSION PINNED_VERSION PINNED_SUM OVERRIDE_SUM
  if [ "$2" = "$3" ]; then echo "${5:-$4}"; return; fi
  [ -n "${5:-}" ] || { echo "install-tools: $1 $2 is not the pinned $3: give its checksum in ${1^^}_SHA256" >&2; exit 1; }
  echo "$5"
}
VHS_SUM="$(sum_for vhs "$VHS_VERSION" "$PINNED_VHS" "$PINNED_VHS_SHA256" "${VHS_SHA256:-}")"
TTYD_SUM="$(sum_for ttyd "$TTYD_VERSION" "$PINNED_TTYD" "$PINNED_TTYD_SHA256" "${TTYD_SHA256:-}")"

[ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] || {
  echo "install-tools: only Linux x86_64 is scripted; install vhs and ttyd yourself" >&2; exit 1; }

mkdir -p "$BIN_DIR"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

has_version() {  # BINARY VERSION
  [ -x "$1" ] && "$1" --version 2>&1 | grep -q -F "$2"
}

if ! has_version "$BIN_DIR/vhs" "$VHS_VERSION"; then
  f="vhs_${VHS_VERSION}_Linux_x86_64.tar.gz"
  curl -fsSL -o "$tmp/$f" "https://github.com/charmbracelet/vhs/releases/download/v$VHS_VERSION/$f"
  echo "$VHS_SUM  $tmp/$f" | sha256sum -c -
  mkdir "$tmp/vhs" && tar -xzf "$tmp/$f" -C "$tmp/vhs"
  install -m 0755 "$(find "$tmp/vhs" -name vhs -type f | head -1)" "$BIN_DIR/vhs"
fi

if ! has_version "$BIN_DIR/ttyd" "$TTYD_VERSION"; then
  curl -fsSL -o "$tmp/ttyd.x86_64" "https://github.com/tsl0922/ttyd/releases/download/$TTYD_VERSION/ttyd.x86_64"
  echo "$TTYD_SUM  $tmp/ttyd.x86_64" | sha256sum -c -
  install -m 0755 "$tmp/ttyd.x86_64" "$BIN_DIR/ttyd"
fi

for t in ffmpeg tmux; do
  command -v "$t" >/dev/null || { echo "install-tools: $t not found: install it with your package manager" >&2; exit 1; }
done
# vhs drives a browser (rod finds one on PATH, or downloads one on first use: ask for it instead).
for b in chromium chromium-browser google-chrome google-chrome-stable chrome; do
  command -v "$b" >/dev/null && { browser="$b"; break; }
done
[ -n "${browser:-}" ] || { echo "install-tools: no Chromium or Chrome on PATH: install one" >&2; exit 1; }

echo "browser: $browser"
"$BIN_DIR/vhs" --version
"$BIN_DIR/ttyd" --version
