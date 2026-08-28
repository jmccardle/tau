#!/usr/bin/env python3
"""Fail if a ``@agent_facing`` object is marked but not actually documented.

Reference: docs/AGENT-DOCS.md

The rule, from CLAUDE.md:

    Any object an extension author or an agent calls carries ``@agent_facing``.
    A marked object with an undocumented parameter fails
    ``scripts/check_docs_coverage.py``, which the pre-commit hook runs alongside
    mypy and ruff.

There is no threshold and no ratchet. The marked set is opt-in, so 100% is the
only defensible bar: marking an object is a claim that an agent will call it,
and a claim that comes with no prose is the silent-omission failure the whole
library exists to prevent. If an object should not be documented, remove the
marker -- that is a visible decision in a diff. Lowering a number is not.

Four faults fail the check:

1. A marked object with no docstring at all.
2. A marked object whose docstring omits a parameter. The parameter is still
   published -- name, annotation and default come from the signature -- so the
   reference never hides an argument. This catches the missing *meaning*.
3. A callable annotated to return something, with no ``Returns:`` section.
4. griffe's own docstring warnings, chiefly a docstring naming a parameter the
   signature does not have. griffe reports this without being asked, which is
   why no separate docstring linter (pydoclint, darglint) is installed.

Run it directly for the report:

    venv/bin/python scripts/check_docs_coverage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from tau_agent_core.docs_build import (
    PACKAGES,
    DocsBuildError,
    collect,
    coverage,
    source_paths,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        objects, warnings = collect(PACKAGES, source_paths(REPO_ROOT))
    except DocsBuildError as exc:
        print(f"docs coverage: {exc}", file=sys.stderr)
        return 1

    report = coverage(objects, warnings)

    if report.total == 0:
        # Not a pass. An empty marked set means the marker was removed, the
        # packages moved, or griffe stopped resolving the decorator -- and the
        # check would report 100% for all three.
        print("docs coverage: no @agent_facing objects found at all.", file=sys.stderr)
        print(f"docs coverage: searched {', '.join(PACKAGES)}.", file=sys.stderr)
        return 1

    for obj in report.missing_docstring:
        print(f"{obj.filepath}:{obj.lineno}: {obj.path} is marked but has no docstring")
    for obj, missing in report.missing_params:
        if not obj.has_docstring:
            continue  # already reported above; do not say it twice
        names = ", ".join(missing)
        print(f"{obj.filepath}:{obj.lineno}: {obj.path} does not document: {names}")
    for obj in report.missing_returns:
        if not obj.has_docstring:
            continue
        print(
            f"{obj.filepath}:{obj.lineno}: {obj.path} returns {obj.annotation} "
            f"with no Returns: section"
        )
    for message in report.drift:
        print(f"{message} (docstring/signature drift)")

    print(
        f"docs coverage: {report.complete}/{report.total} marked objects complete "
        f"({report.fraction:.1%}), {len(report.drift)} drift warning(s)"
    )
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
