"""Example 1: Permission Gate — a real ``tool_call`` veto (E6 §2 / S38).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S38 (fix the broken safety demo).

Blocks destructive ``bash`` commands (``rm -rf /``, ``chmod 777``, ``dd``,
``mkfs``, …) *before they execute*. This is a safety extension: the whole point
is to REFUSE the call, so it MUST use the one hook that can actually veto.

## Why this was rewritten

The original demo subscribed to ``tool_execution_start`` — a **notify** event
whose return value is ignored — and called ``api.notify(...)``. Neither can stop
a command: the notify bus cannot block, and ``api.notify`` never existed (the UI
sink is ``api.ui.notify``). So the "gate" printed a warning and then let the
destructive command run. A safety demo that does not actually block is worse than
none. It now registers a ``tool_call`` hook — the pi-faithful veto surface — and
returns ``{"block": True, "reason": ...}``, which the agent loop converts into an
error tool result the model reacts to (pi ``agent-loop.ts:597-602``; τ
``_prepare_tool_call`` → ``BlockedCall``). Same mechanism the gatekeeper demo
(``22_gatekeeper.py``) uses.

## Field contract

τ owns the tool-argument field names, so the ``bash`` command is read directly
from ``event["input"]["command"]`` (no pi ``args ?? input`` dual-read).

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.permission_gate import permission_gate_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["bash", "read", "write"],
    extensions=[permission_gate_extension],
)
```

Or load the file directly through the public ``-e`` surface::

    tau -e examples/01_permission_gate.py
"""

from __future__ import annotations

import re
from typing import Any

# Patterns for destructive commands that must be blocked before execution.
BLOCKED_PATTERNS = [
    r"\brm\s+-rf\s+/",         # rm -rf /
    r"\bchmod\s+777",          # chmod 777
    r"\bdd\s+",                # dd
    r"\bmkfs\b",               # mkfs
    r"\bmkdisk\b",             # mkdisk
    r">\s*/dev/(sd|vd|nvme)",  # redirect to a block device
    r"\bfdisk\b",              # fdisk
    r"\bparted\b",             # parted
    r"\bsudo\s+rm",            # sudo rm
    r"\bshred\b",              # shred
]

COMPILED_PATTERNS = [re.compile(p) for p in BLOCKED_PATTERNS]


def permission_gate_decision(
    *, tool_name: str, tool_input: dict[str, Any]
) -> dict[str, Any] | None:
    """Pure veto decision for one prepared tool call.

    Returns ``{"block": True, "reason": str}`` to deny a destructive ``bash``
    command, or ``None`` to allow the call. Only ``bash`` is inspected — the other
    tools carry no shell command to match against.
    """
    if tool_name != "bash":
        return None
    command = str(tool_input.get("command") or "")
    for pattern in COMPILED_PATTERNS:
        if pattern.search(command):
            return {
                "block": True,
                "reason": (
                    f"Denied by permission gate: the command matches a destructive "
                    f"pattern ({pattern.pattern!r}) and was blocked before execution: "
                    f"{command[:120]}"
                ),
            }
    return None


def permission_gate_tool_call(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
    """The ``tool_call`` hook handler (``handler(event, ctx)``)."""
    return permission_gate_decision(
        tool_name=event["tool_name"],
        tool_input=event.get("input") or {},
    )


def permission_gate_extension(api: Any) -> None:
    """Extension entry point: register the destructive-command ``tool_call`` veto."""
    api.on("tool_call", permission_gate_tool_call)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/01_permission_gate.py`` → ``getattr(module, "register")``), so the
#: demo is loadable through the public ``-e`` surface, not only by importing
#: ``permission_gate_extension`` directly.
register = permission_gate_extension
