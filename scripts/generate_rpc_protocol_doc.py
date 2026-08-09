#!/usr/bin/env python3
"""Regenerate `docs/RPC-PROTOCOL.md` from the RPC capability document (K3).

Reference: docs/REMOTE-CONTROL.md §4[8] K3.

    The document is machine-readable and is the same artifact the generated
    reference is built from.

All the actual rendering lives in `tau_agent_core.rpc.protocol_doc.render()`
(pure, no I/O) so `tests/test_rpc_protocol_doc.py` can call it directly and
assert the checked-in file matches. This script is the thin, checked-in CLI
that writes that output to disk — run it after any change to
`tau_agent_core.rpc.commands.COMMAND_TABLE` or
`tau_agent_core.rpc_event_schema.WireEvent`, and commit the result:

    venv/bin/python scripts/generate_rpc_protocol_doc.py
"""

from __future__ import annotations

from pathlib import Path

from tau_agent_core.rpc.protocol_doc import render

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "RPC-PROTOCOL.md"


def main() -> None:
    OUTPUT_PATH.write_text(render())
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
