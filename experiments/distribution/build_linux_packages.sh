#!/usr/bin/env bash
# build_linux_packages.sh — experiment: wrap a build_runtime.py Linux archive as an AppImage and a .deb.
#
# Usage: build_linux_packages.sh <tau-VER-linux-x64-VARIANT-pyXY.tar.gz> <out-dir>
# Env:   APPIMAGETOOL (path to appimagetool-x86_64.AppImage; AppImage step skipped if unset)
#
# Neither wrapper is portable by default: the archive's empty .tau/ is removed. An AppImage
# becomes portable when a .tau/ sits beside the .AppImage file (docs/PORTABLE-HOME.md).
set -euo pipefail
TARBALL="$(readlink -f "${1:?usage: build_linux_packages.sh <tarball> <out-dir>}")"
OUT="$(readlink -f "${2:?usage: build_linux_packages.sh <tarball> <out-dir>}")"
BASE="$(basename "$TARBALL" .tar.gz)"
VERSION="$(echo "$BASE" | sed -n 's/^tau-\([0-9][^-]*\)-.*/\1/p')"
[ -n "$VERSION" ] || { echo "cannot read version from $BASE" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"

interp() { sed -n 's/.*"interpreter": "\([^"]*\)".*/\1/p' "$1/manifest.json"; }

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

# --- AppImage: AppRun names the directory holding the .AppImage as the portable base.
if [ -n "${APPIMAGETOOL:-}" ]; then
    APPDIR="$WORK/tau.AppDir"
    mkdir -p "$APPDIR"
    tar -xzf "$TARBALL" -C "$APPDIR"
    rm -rf "$APPDIR/tau/.tau"
    "$APPDIR/tau/$(interp "$APPDIR/tau")" -I -B -c "$ICON_PY" "$APPDIR/tau.png"
    if [ -x "$APPDIR/tau/bin/tau-window" ]; then
        printf '%s\n' "$DESKTOP" | sed 's/^Exec=tau$/Exec=tau-window/; s/^Terminal=true$/Terminal=false/' \
            > "$APPDIR/tau.desktop"
    else
        printf '%s\n' "$DESKTOP" > "$APPDIR/tau.desktop"
    fi
    # Standalone: opened with no arguments and no terminal (a file manager, a launcher), it opens
    # Rio; from a shell it is the CLI. Everything else always runs bin/tau.
    cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
here="$(dirname "$(readlink -f "$0")")"
: "${APPIMAGE:?AppRun must be started by the AppImage runtime}"
export TAU_PORTABLE_BASE="$(dirname "$APPIMAGE")"
if [ -x "$here/tau/bin/tau-window" ] && [ "$#" -eq 0 ] && [ ! -t 0 ]; then
    exec "$here/tau/bin/tau-window"
fi
exec "$here/tau/bin/tau" "$@"
APPRUN
    chmod 755 "$APPDIR/AppRun"
    ARCH=x86_64 "$APPIMAGETOOL" --no-appstream "$APPDIR" "$OUT/$BASE.AppImage" >/dev/null 2>&1
fi

# --- .deb: the tree under /opt/tau, /usr/bin/tau a symlink to its launcher; no .tau/ shipped.
DEB="$WORK/deb"
mkdir -p "$DEB/opt" "$DEB/usr/bin" "$DEB/usr/share/applications" "$DEB/DEBIAN" \
    "$DEB/usr/share/icons/hicolor/256x256/apps" "$DEB/usr/share/doc/tau"
tar -xzf "$TARBALL" -C "$DEB/opt"
rm -rf "$DEB/opt/tau/.tau"
ln -s /opt/tau/bin/tau "$DEB/usr/bin/tau"
if [ -x "$DEB/opt/tau/bin/tau-window" ]; then
    ln -s /opt/tau/bin/tau-window "$DEB/usr/bin/tau-window"
    printf '%s\n' "$DESKTOP" | sed 's/^Exec=tau$/Exec=tau-window/; s/^Terminal=true$/Terminal=false/' \
        > "$DEB/usr/share/applications/tau.desktop"
else
    printf '%s\n' "$DESKTOP" > "$DEB/usr/share/applications/tau.desktop"
fi
"$DEB/opt/tau/$(interp "$DEB/opt/tau")" -I -B -c "$ICON_PY" "$DEB/usr/share/icons/hicolor/256x256/apps/tau.png"
cp "$DEB/opt/tau/LICENSE" "$DEB/usr/share/doc/tau/copyright"
SIZE_KB="$(du -sk "$DEB" | cut -f1)"
VARIANT="$(echo "$BASE" | sed -n 's/^tau-[^-]*-linux-x64-\([a-z]*\)-.*/\1/p')"
PKG="ffwf-tau"; [ "$VARIANT" = full ] || PKG="ffwf-tau-$VARIANT"
# Rio's own binary needs GLIBC_2.35 (objdump); the Python tree alone needs 2.28.
GLIBC_FLOOR=2.28; [ "$VARIANT" = standalone ] && GLIBC_FLOOR=2.35
cat > "$DEB/DEBIAN/control" <<CONTROL
Package: ${PKG}
Version: ${VERSION}
Architecture: amd64
Maintainer: Fight Fire with Fire Robotics, LLC
Installed-Size: ${SIZE_KB}
Depends: libc6 (>= ${GLIBC_FLOOR})
Section: devel
Priority: optional
Description: τ, a programmable coding agent with its own Python runtime
 Self-contained: bundles CPython (python-build-standalone) under /opt/tau.
CONTROL
dpkg-deb --root-owner-group -Zxz --build "$DEB" "$OUT/${PKG}_${VERSION}_amd64.deb" >/dev/null
ls -la "$OUT"
