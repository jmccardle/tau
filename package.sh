#!/usr/bin/env bash
#
# package.sh — build τ's distributable archive: tau-<version>.tar.gz
#
# The version is read from the SAME single source the CLI's --version flag uses
# (tau_coding_agent.__version__ in tau-coding-agent/src/tau_coding_agent/__init__.py),
# so a release bumps exactly one line and the flag + tarball never disagree.
#
# Contents are the FUNCTIONAL-ONLY subset: the three packages' src/ Python trees
# plus each package's pyproject.toml and the LICENSE — enough to install and run
# τ. Tests, examples, docs, caches, and the venv are deliberately excluded.
set -euo pipefail

cd "$(dirname "$0")"

VERSION_FILE="tau-coding-agent/src/tau_coding_agent/__init__.py"

# Extract the version literal (e.g. __version__ = "0.9.0" -> 0.9.0). Fail loudly
# if it is missing rather than shipping a mislabelled archive.
VERSION="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' "$VERSION_FILE")"
if [ -z "$VERSION" ]; then
    echo "package.sh: could not read __version__ from $VERSION_FILE" >&2
    exit 1
fi

PKG="tau-${VERSION}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
DEST="$STAGE/$PKG"
mkdir -p "$DEST"

# Functional-only Python: the three src/ trees, minus compiled __pycache__.
for pkg in tau-ai tau-agent-core tau-coding-agent; do
    find "$pkg/src" -name "*.py" -not -path "*/__pycache__/*" -print0 \
        | while IFS= read -r -d '' f; do
            mkdir -p "$DEST/$(dirname "$f")"
            cp "$f" "$DEST/$f"
        done
    # Packaging metadata needed to install the functional code.
    mkdir -p "$DEST/$pkg"
    cp "$pkg/pyproject.toml" "$DEST/$pkg/pyproject.toml"
done
cp LICENSE "$DEST/LICENSE"

TARBALL="tau-${VERSION}.tar.gz"
tar -czf "$TARBALL" -C "$STAGE" "$PKG"
echo "built $TARBALL ($(find "$DEST" -name '*.py' | wc -l | tr -d ' ') python files, version $VERSION)"
