#!/usr/bin/env bash
#
# bump-version.sh — set the τ monorepo's release version.
#
# The four distributions release in lockstep: one number, four wheels. That
# number is written in eleven places, and tau-coding-agent/tests/test_packaging.py
# fails until all eleven agree — so nothing drifts silently, but a release means
# eleven hand edits. This script is those eleven edits.
#
# The eleven:
#   * four ``__version__`` literals, one per package __init__.py. These are what
#     setuptools reads for [tool.setuptools.dynamic], so they ARE the wheels'
#     versions — NEVER add a ``version = "..."`` literal back to a package
#     pyproject.toml to "fix" a mismatch; a test forbids the second copy.
#   * one literal in the repo-root pyproject.toml, which is config rather than a
#     distribution and so has no package to read from.
#   * six ``ffwf-tau…==<version>`` pins on in-repo requirements. A requirement
#     string has nowhere to read a version from, so these are the copies this
#     repo cannot make dynamic — and the ones a manual bump forgets.
#
# This script ONLY edits files. It does not add, commit, tag, or push: the
# release decision, and the commit message that records it, stay with a human.
#
# Usage:  ./bump-version.sh 0.9.3
#
set -euo pipefail

cd "$(dirname "$0")"

usage() {
    echo "usage: ./bump-version.sh <version>      e.g. ./bump-version.sh 0.9.3" >&2
    echo "       sets every version literal and in-repo pin in the τ monorepo." >&2
    echo "       Edits files only — no commit, no tag, no push." >&2
}

# -- the argument ----------------------------------------------------------
#
# An unvalidated argument reaches sed as a pattern and eleven files as content,
# so a typo ("0.9.3 " / "v0.9.3" / "--help") would be written into the tree as a
# version and only surface at build time. Require a PEP 440 release number, with
# the pre/post/dev suffixes actually used for a τ release candidate.

if [ "$#" -ne 1 ]; then
    usage
    exit 2
fi

NEW="$1"
if ! [[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$ ]]; then
    echo "bump-version.sh: '$NEW' is not a version this repo releases." >&2
    echo "bump-version.sh: expected N.N.N, optionally aN/bN/rcN, .postN, .devN" >&2
    usage
    exit 2
fi

# -- the working tree ------------------------------------------------------
#
# Eleven in-place edits across eight files are only safe because `git checkout`
# undoes them. That escape hatch stops working if the bump lands on top of edits
# a human has not committed yet, so refuse rather than mix the two.
#
# Untracked files are deliberately not counted: `git checkout` never touches
# them, so they cannot be lost by a bump — and on a fresh clone this script is
# itself untracked, which would otherwise make it refuse to ever run.

if ! DIRTY="$(git status --porcelain --untracked-files=no 2>/dev/null)"; then
    echo "bump-version.sh: not a git repository (or git is unavailable)." >&2
    echo "bump-version.sh: the dirty-tree guard cannot run, so neither will the bump." >&2
    exit 1
fi
if [ -n "$DIRTY" ]; then
    echo "bump-version.sh: refusing to bump — the working tree has uncommitted changes:" >&2
    echo "$DIRTY" >&2
    echo "bump-version.sh: commit or stash them first, so a bad bump is one 'git checkout' away." >&2
    exit 1
fi

# -- what gets edited ------------------------------------------------------
#
# The five version literals are structural: four packages plus the root. They are
# named here because their absence is a fact about the tree this script must not
# guess at — a package that lost its __version__ is a broken build, not a file to
# skip.

VERSION_FILES=(
    "tau-llm/src/tau_llm/__init__.py"
    "tau-agent-core/src/tau_agent_core/__init__.py"
    "tau-coding-agent/src/tau_coding_agent/__init__.py"
    "tau-jmfts/src/tau_jmfts/__init__.py"
)
ROOT_PYPROJECT="pyproject.toml"

# The pins, by contrast, are DISCOVERED rather than listed. There are six today,
# spread over three of the four package pyprojects (tau-llm depends on nothing
# in-repo). A seventh added next month must be bumped too, and a hardcoded list
# of six would miss it silently — which is the exact failure this script exists
# to prevent. Anything matching this pattern is in scope, wherever it appears.
PIN_FILES=(tau-*/pyproject.toml)
PIN_PATTERN='"ffwf-tau[a-z-]*(\[[a-z,]+\])?==[^"]+"'

# -- record the "before" ---------------------------------------------------
#
# Read the old value per file before touching anything, so the report can show a
# real transition and so a file that has no version line at all is caught here
# rather than being silently left behind by a sed that matches nothing.

declare -A OLD_OF
for f in "${VERSION_FILES[@]}"; do
    old="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$f")"
    if [ -z "$old" ]; then
        echo "bump-version.sh: no '__version__ = \"…\"' line in $f" >&2
        exit 1
    fi
    OLD_OF["$f"]="$old"
done

old_root="$(sed -n 's/^version = "\([^"]*\)"$/\1/p' "$ROOT_PYPROJECT")"
if [ -z "$old_root" ]; then
    echo "bump-version.sh: no 'version = \"…\"' line in $ROOT_PYPROJECT" >&2
    exit 1
fi
OLD_OF["$ROOT_PYPROJECT"]="$old_root"

# Count the pins now, so the post-edit sweep can prove it saw the same set. A pin
# that vanishes mid-run means the pattern changed under us, not that the bump worked.
PINS_BEFORE="$(grep -hoE "$PIN_PATTERN" "${PIN_FILES[@]}" | wc -l | tr -d ' ')"
if [ "$PINS_BEFORE" -eq 0 ]; then
    echo "bump-version.sh: found no in-repo pins matching $PIN_PATTERN" >&2
    echo "bump-version.sh: the pin syntax has changed; this script's pattern is stale." >&2
    exit 1
fi

# -- edit ------------------------------------------------------------------

for f in "${VERSION_FILES[@]}"; do
    sed -i "s/^__version__ = \"[^\"]*\"\$/__version__ = \"$NEW\"/" "$f"
done

# Anchored at column 0, which is what distinguishes the [project] version from
# mypy's `python_version` and ruff's `target-version` in the same file.
sed -i "s/^version = \"[^\"]*\"\$/version = \"$NEW\"/" "$ROOT_PYPROJECT"

# The extras bracket, where present, is part of the requirement name and must
# survive: "ffwf-tau-coding-agent[tui,jmfts]==0.9.3", not "ffwf-…==0.9.3".
sed -i -E "s/(\"ffwf-tau[a-z-]*(\[[a-z,]+\])?)==[^\"]+\"/\1==$NEW\"/g" "${PIN_FILES[@]}"

# -- verify ----------------------------------------------------------------
#
# A sed that matches nothing exits 0. Every edit above is therefore re-read from
# disk and held against the new number; a bump that silently missed one pin is
# precisely the failure this script exists to prevent, so a miss is fatal and
# named rather than reported as success.

FAILED=()

for f in "${VERSION_FILES[@]}"; do
    grep -qx "__version__ = \"$NEW\"" "$f" || FAILED+=("$f (__version__)")
done
grep -qx "version = \"$NEW\"" "$ROOT_PYPROJECT" || FAILED+=("$ROOT_PYPROJECT (version)")

# Every discovered pin, individually: a pin left on the old version is a wheel
# that resolves against a τ nobody tested this one against.
PINS_AFTER=0
while IFS=: read -r file pin; do
    PINS_AFTER=$((PINS_AFTER + 1))
    case "$pin" in
        *"==$NEW\"") ;;
        *) FAILED+=("$file → $pin") ;;
    esac
done < <(grep -oE "$PIN_PATTERN" "${PIN_FILES[@]}")

if [ "$PINS_AFTER" -ne "$PINS_BEFORE" ]; then
    FAILED+=("pin count changed during the bump: $PINS_BEFORE before, $PINS_AFTER after")
fi

if [ "${#FAILED[@]}" -ne 0 ]; then
    echo "bump-version.sh: FAILED — these locations do not read $NEW:" >&2
    printf '  %s\n' "${FAILED[@]}" >&2
    echo "bump-version.sh: the tree is now half-bumped; 'git checkout -- .' to undo." >&2
    exit 1
fi

# -- report ----------------------------------------------------------------

TOTAL=$((${#VERSION_FILES[@]} + 1 + PINS_AFTER))
echo "bumped $TOTAL locations to $NEW"
echo
for f in "${VERSION_FILES[@]}" "$ROOT_PYPROJECT"; do
    printf '  %-48s %s → %s\n' "$f" "${OLD_OF[$f]}" "$NEW"
done
grep -nE "$PIN_PATTERN" "${PIN_FILES[@]}" | while IFS= read -r line; do
    printf '  %s\n' "$line"
done
echo
echo "no commit, no tag, no push — that is yours to make. Verify with:"
echo "  pytest tau-coding-agent/tests/test_packaging.py"
