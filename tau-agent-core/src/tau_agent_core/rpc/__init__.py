"""RPC protocol message types for τ-agent-core.

Phase 6 Subphase 0: Finalize the RPC protocol and export types.

All messages are LF-delimited JSON (JSON-RPC 2.0 format):
    {"jsonrpc": "2.0", "id": 1, "method": "send_prompt", "params": {"text": "hello"}}
    {"jsonrpc": "2.0", "id": null, "method": "event", "params": {"type": "text_delta", "delta": "H"}}
    {"jsonrpc": "2.0", "id": 1, "result": {"status": "done", "messages": [...]}}

Framing is LF-only (``\\n``). A single trailing ``\\r`` is tolerated on input
(CRLF-framed peers) and stripped; nothing else is. In particular the read path
never uses ``str.splitlines()``, which also breaks on U+2028/U+2029
(LINE/PARAGRAPH SEPARATOR) — legal, unescaped characters inside a JSON string
value — and would silently corrupt any payload containing one. See
``_read_stdin`` below; this is the same reasoning behind pi's jsonl.ts
refusing Node's `readline`.

Reference: docs/PHASE-6-SUBPHASE-0.md
Reference: docs/SUBPHASE-0.0.md lines 260-340
Reference: docs/tau-coding-agent.md lines 220-280
Reference: docs/IMPLEMENTATION-PLAN.md lines 460-500

---

This module was a single file (rpc.py) until docs/REMOTE-CONTROL.md section 3's
block decomposition split it into a package: dialect.py ([2] Dialect),
transport.py ([1] Transport), handler.py (RPCHandler: lifecycle + [3]/[4]
dispatch). This __init__ re-exports the same names the single file used to, so
every existing ``from tau_agent_core.rpc import ...`` / ``from tau_agent_core
import rpc; rpc.<name>`` keeps working unchanged.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.rpc.dialect import RPCEvent as RPCEvent
from tau_agent_core.rpc.dialect import RPCRequest as RPCRequest
from tau_agent_core.rpc.dialect import RPCResponse as RPCResponse
from tau_agent_core.rpc.handler import (
    DEFAULT_OUTPUT_QUEUE_EVENT_BOUND as DEFAULT_OUTPUT_QUEUE_EVENT_BOUND,
)
from tau_agent_core.rpc.handler import RPCHandler as RPCHandler
from tau_agent_core.rpc.transport import _SIGNAL_EXIT_CODES as _SIGNAL_EXIT_CODES
from tau_agent_core.rpc.transport import _release_stdout as _release_stdout
from tau_agent_core.rpc.transport import _take_over_stdout as _take_over_stdout
from tau_agent_core.rpc.transport import is_stdout_taken_over as is_stdout_taken_over


def __getattr__(name: str) -> Any:
    """Proxy the stdout-takeover module globals through to transport.py.

    `_real_stdout`/`_stdout_takeover_depth` are mutated in place by
    `_take_over_stdout`/`_release_stdout` (``global`` assignments inside
    those functions target *transport.py's* module dict). A plain `from
    .transport import _real_stdout` would copy today's value once at import
    time and never see later mutations, so callers reading
    `tau_agent_core.rpc._real_stdout` (test_rpc_transport.py) would observe a
    permanently stale value. Looking the name up on `transport` itself, on
    every access, keeps `rpc.<name>` and `transport.<name>` the same
    reference.
    """
    if name in ("_real_stdout", "_stdout_takeover_depth"):
        from tau_agent_core.rpc import transport

        return getattr(transport, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
