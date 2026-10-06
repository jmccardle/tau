#!/usr/bin/env bash
# build_linux_packages.sh — experiment: wrap a portable tau-*.tar.gz as an AppImage and a .deb.
#
# Usage: build_linux_packages.sh <tau-VER-linux-x86_64[-ft].tar.gz> <out-dir>
# Env:   APPIMAGETOOL (path to appimagetool-x86_64.AppImage; AppImage step skipped if unset)
set -euo pipefail
TARBALL="$(readlink -f "${1:?usage: build_linux_packages.sh <tarball> <out-dir>}")"
OUT="$(readlink -f "${2:?usage: build_linux_packages.sh <tarball> <out-dir>}")"
BASE="$(basename "$TARBALL" .tar.gz)"
VERSION="$(echo "$BASE" | sed -n 's/^tau-\([0-9][^-]*\)-.*/\1/p')"
[ -n "$VERSION" ] || { echo "cannot read version from $BASE" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"

ICON_PY='import sys
from PIL import Image, ImageDraw
im = Image.new("RGBA", (256, 256), (30, 30, 46, 255))
d = ImageDraw.Draw(im)
d.rectangle((60, 70, 196, 100), fill=(203, 166, 247, 255))
d.rectangle((113, 70, 143, 200), fill=(203, 166, 247, 255))
im.save(sys.argv[1])'

DESKTOP="[Desktop Entry]
Type=Application
Name=tau
Comment=Programmable coding agent
Exec=tau
Icon=tau
Terminal=true
Categories=Development;"

# --- AppImage: the bundle is the AppDir payload; AppRun execs the relocatable launcher.
if [ -n "${APPIMAGETOOL:-}" ]; then
    APPDIR="$WORK/tau.AppDir"
    mkdir -p "$APPDIR"
    tar -xzf "$TARBALL" -C "$APPDIR"
    "$APPDIR/tau/python/bin/python3" -I -c "$ICON_PY" "$APPDIR/tau.png"
    printf '%s\n' "$DESKTOP" > "$APPDIR/tau.desktop"
    cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
here="$(dirname "$(readlink -f "$0")")"
exec "$here/tau/bin/tau" "$@"
APPRUN
    chmod 755 "$APPDIR/AppRun"
    ARCH=x86_64 "$APPIMAGETOOL" --no-appstream "$APPDIR" "$OUT/$BASE.AppImage"
fi

# --- .deb: the bundle lives under /opt/tau, /usr/bin/tau is a symlink to its launcher.
DEB="$WORK/deb"
mkdir -p "$DEB/opt" "$DEB/usr/bin" "$DEB/usr/share/applications" "$DEB/DEBIAN" \
    "$DEB/usr/share/icons/hicolor/256x256/apps" "$DEB/usr/share/doc/tau"
tar -xzf "$TARBALL" -C "$DEB/opt"
ln -s /opt/tau/bin/tau "$DEB/usr/bin/tau"
printf '%s\n' "$DESKTOP" > "$DEB/usr/share/applications/tau.desktop"
"$DEB/opt/tau/python/bin/python3" -I -c "$ICON_PY" "$DEB/usr/share/icons/hicolor/256x256/apps/tau.png"
cp "$DEB/opt/tau/LICENSE" "$DEB/usr/share/doc/tau/copyright"
SIZE_KB="$(du -sk "$DEB" | cut -f1)"
case "$BASE" in *-ft) FT="-ft" ;; *) FT="" ;; esac
cat > "$DEB/DEBIAN/control" <<CONTROL
Package: ffwf-tau
Version: ${VERSION}
Architecture: amd64
Maintainer: Fight Fire with Fire Robotics, LLC
Installed-Size: ${SIZE_KB}
Depends: libc6 (>= 2.28)
Section: devel
Priority: optional
Description: τ, a programmable coding agent with its own Python runtime
 Self-contained: bundles CPython (python-build-standalone) under /opt/tau.
CONTROL
dpkg-deb --root-owner-group -Zxz --build "$DEB" "$OUT/ffwf-tau${FT}_${VERSION}_amd64.deb"
ls -la "$OUT"
