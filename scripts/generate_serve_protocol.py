#!/usr/bin/env python3
"""Regenerate ``docs/SERVE-PROTOCOL.md`` and ``docs/serve-protocol.schema.json``.

Both are rendered by ``tau_coding_agent.serve.protocol_doc`` from the dataclasses
in ``tau_coding_agent/serve/protocol.py``; ``tests/test_serve.py`` fails when the
checked-in copies differ. Run after any protocol change and commit both files:

    venv/bin/python scripts/generate_serve_protocol.py
"""

from __future__ import annotations

from pathlib import Path

from tau_coding_agent.serve.protocol_doc import render_markdown, render_schema

DOCS = Path(__file__).resolve().parent.parent / "docs"


def main() -> None:
    for name, text in (
        ("SERVE-PROTOCOL.md", render_markdown()),
        ("serve-protocol.schema.json", render_schema()),
    ):
        (DOCS / name).write_text(text)
        print(f"wrote docs/{name}")


if __name__ == "__main__":
    main()
