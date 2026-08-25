#!/usr/bin/env bash
#
# package.sh — build τ's distributable archive: tau-<version>.tar.gz
#
# The version is read from the SAME source the CLI's --version flag uses, and that
# tau-coding-agent's pyproject.toml builds its wheel metadata from
# (tau_coding_agent.__version__ in tau-coding-agent/src/tau_coding_agent/__init__.py),
# so the flag, the wheel and the tarball never disagree. The other three packages
# carry the same literal in their own __init__.py; a release bumps all four, and
# tests/test_packaging.py fails if one of them is missed.
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

# Functional-only source: the three src/ trees, minus compiled __pycache__.
#
# NOT just *.py. The installed package reads two data files at runtime —
# tau_default_config.json (first-run config bootstrap) and parley.tcss (the
# TUI's stylesheet, resolved by Parley.CSS_PATH). A tarball of .py files alone
# installs cleanly and then dies on first run, which is the worst kind of
# release. Every extension here must also be declared in the owning package's
# [tool.setuptools.package-data]; tests/test_packaging.py holds the two lists
# against each other.
SRC_PATTERNS=(-name "*.py" -o -name "*.json" -o -name "*.tcss")

for pkg in tau-llm tau-agent-core tau-coding-agent; do
    find "$pkg/src" \( "${SRC_PATTERNS[@]}" \) -not -path "*/__pycache__/*" -print0 \
        | while IFS= read -r -d '' f; do
            mkdir -p "$DEST/$(dirname "$f")"
            cp "$f" "$DEST/$f"
        done
    # Packaging metadata needed to install the functional code. The LICENSE and
    # README.md copies are not decoration: each pyproject.toml declares
    # license-files = ["LICENSE"] and readme = "README.md", and setuptools
    # resolves both INSIDE the package directory, so a staged package missing
    # either one cannot be installed from the unpacked tarball at all.
    # tests/test_packaging.py holds this list against the pyproject metadata.
    mkdir -p "$DEST/$pkg"
    cp "$pkg/pyproject.toml" "$DEST/$pkg/pyproject.toml"
    cp "$pkg/LICENSE" "$DEST/$pkg/LICENSE"
    cp "$pkg/README.md" "$DEST/$pkg/README.md"
done
cp LICENSE "$DEST/LICENSE"

# NOTE: this script deliberately rewrites NO source. It used to `sed` tagline.py
# to flip FUN_DEFAULT on for a release, which worked for this tarball and only
# this tarball — the PyPI wheels are built by .github/workflows/publish.yml with
# `python -m build`, straight from the source tree, so they never saw the flip
# and every published wheel shipped the developer default. FUN_DEFAULT is now
# True in the source; a staged tree is a copy of the working tree and nothing
# else. If you are about to add a "just patch this one line at package time"
# step here, that is the bug that motivated its removal.

TARBALL="tau-${VERSION}.tar.gz"
tar -czf "$TARBALL" -C "$STAGE" "$PKG"
echo "built $TARBALL ($(find "$DEST" -name '*.py' | wc -l | tr -d ' ') python files, \
$(find "$DEST" -type f ! -name '*.py' | wc -l | tr -d ' ') data files, version $VERSION)"
