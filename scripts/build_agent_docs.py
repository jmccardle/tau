#!/usr/bin/env python3
"""Regenerate the reference section of the agent-facing documentation library.

Reference: docs/AGENT-DOCS.md

Reads every object marked ``@agent_facing`` (:mod:`tau_llm.docs`) out of the
headless source trees and writes one markdown page per topic under
``docs/library/reference/``, plus an index. The output is checked in, so a
change to a marked object's docstring shows up in the diff of the pull request
that made it. ``tests/test_agent_docs.py`` asserts the checked-in files match a
fresh build, the same way ``tests/test_rpc_protocol_doc.py`` guards
``docs/RPC-PROTOCOL.md``.

All the rendering lives in ``tau_agent_core.docs_build`` (pure, no I/O) so the
tests call it directly. This script is the thin CLI that writes to disk:

    venv/bin/python scripts/build_agent_docs.py

Run it after adding or changing an ``@agent_facing`` object, and commit the
result. It exits non-zero without writing anything if the marked surface has a
fault -- an unknown topic, a non-literal marker argument -- because a reference
built around a broken marker is a reference that quietly omits something.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tau_agent_core.docs_build import (
    PACKAGES,
    DocsBuildError,
    collect,
    pages,
    render_index,
    render_topic,
    source_paths,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "docs" / "library" / "reference"


def build() -> dict[Path, str]:
    """Render every reference page for this checkout.

    Returns:
        Absolute output path to file contents. Nothing is written; the caller
        decides, which is what lets the test compare against disk.

    Raises:
        DocsBuildError: On a fault in the marked surface.
    """
    objects, _warnings = collect(PACKAGES, source_paths(REPO_ROOT))
    built = pages(objects)
    files = {OUTPUT_DIR / f"{page.topic}.md": render_topic(page) for page in built}
    files[OUTPUT_DIR / "index.md"] = render_index(built)
    return files


def main() -> int:
    try:
        files = build()
    except DocsBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Pages for topics that no longer have any marked object are removed, not
    # left behind. A stale page still answers when an agent reads it.
    written = set(files)
    for existing in OUTPUT_DIR.glob("*.md"):
        if existing not in written:
            existing.unlink()
            print(f"removed {existing.relative_to(REPO_ROOT)}")

    for path, text in sorted(files.items()):
        path.write_text(text)
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
