"""Example 30: Permission Gate — human-in-the-loop veto (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S60 (replaces the broken original
``01_permission_gate.py``, which was a pattern-only auto-block with no human in
the loop — see ``examples/01_permission_gate.py`` for that one; this is a
DIFFERENT demo, not a fix of it). Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/permission-gate.ts``.

## What this shows

A ``tool_call`` veto that, on a *dangerous* ``bash`` command, does not auto-deny
— it ASKS a human via ``ctx.ui.confirm`` (E7 §3 / S47) and blocks only on "no".
This is the pi ``permission-gate.ts`` pattern faithfully ported: dangerous-pattern
match → confirm dialog → block unless approved.

## Porting the ``ctx.hasUI`` branch (the one deliberate divergence)

pi's original explicitly branches on ``ctx.hasUI``:

```ts
if (!ctx.hasUI) {
    return { block: true, reason: "Dangerous command blocked (no UI for confirmation)" };
}
const choice = await ctx.ui.select(...);
```

τ has no ``ctx.hasUI`` — and does not need one. Under the S48 headless-dialog
policy, ``ctx.ui.confirm(...)`` called with no TUI delegate and no
``--ui-defaults`` policy RAISES ``HeadlessDialogError`` rather than silently
resolving. The ``tool_call`` hook call-site is already Fail-CLOSED on ANY
handler exception (``agent_loop.py`` ``_prepare_tool_call``: "a throwing
handler blocks execution rather than letting the tool run unguarded" — the
τ/pi-parity home of pi's own ``agent-session.ts:419-424`` fail-closed rule).
So simply AWAITING ``ctx.ui.confirm`` reproduces pi's "no UI → block" behavior
for free, through the general mechanism, instead of a demo-local
``hasUI`` special case — and a run WITH an explicit
``--ui-defaults confirm=yes`` (or ``=no``) policy resolves for real, honoring
the S48 policy exactly where pi has no non-interactive story at all. This is
the one hook that can actually stop the call: a ``tool_execution_start`` notify
subscriber cannot block, and there is no ``api.notify`` (only ``api.ui.notify``,
which is non-blocking) — the mistake the original ``01`` demo made.

## Field contract

τ owns the tool-argument field names, so the ``bash`` command is read directly
from ``event["input"]["command"]`` (no pi ``args ?? input`` dual-read).

## Usage

```python
import importlib.util
import sys

from tau_agent_core.sdk import create_agent_session

# examples/ is not an importable package: there is no __init__.py and every
# filename starts with a digit. Load the file by path, as `tau -e` does.
_spec = importlib.util.spec_from_file_location("ext", "examples/30_permission_gate.py")
ext = importlib.util.module_from_spec(_spec)
# Register BEFORE exec_module: a module using `from __future__ import
# annotations` resolves its own dataclass annotations through sys.modules.
sys.modules[_spec.name] = ext
_spec.loader.exec_module(ext)

session = create_agent_session(
    model="gpt-4o",
    tools=["bash"],
    extensions=[ext.register],
)
```

Headless, with an explicit auto-answer policy (S48 — Fail-Early otherwise)::

    tau -p "clean up the repo" -e examples/30_permission_gate.py \\
        --ui-defaults confirm=no

Or interactively through the TUI, where a real confirm dialog (S47) pops up::

    tau -e examples/30_permission_gate.py
"""

from __future__ import annotations

import re
from typing import Any

# Dangerous bash patterns (pi ``permission-gate.ts`` parity): rm -rf/--recursive,
# any sudo, chmod/chown ... 777.
DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+(-rf?|--recursive)", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\b(chmod|chown)\b.*777", re.IGNORECASE),
]


def is_dangerous(command: str) -> bool:
    """True if ``command`` matches any of the dangerous bash patterns."""
    return any(pattern.search(command) for pattern in DANGEROUS_PATTERNS)


async def permission_gate_tool_call(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
    """The ``tool_call`` hook handler: confirm dangerous ``bash`` commands.

    Non-``bash`` calls and non-dangerous ``bash`` commands pass through
    untouched (``None``). A dangerous command awaits a real confirm dialog; "no"
    (or a headless run resolving the confirm policy to ``False``) blocks. A
    headless run with NO policy raises ``HeadlessDialogError``, which the
    ``tool_call`` call-site converts into a block automatically (see module
    docstring) — the demo does not need to catch it itself.
    """
    if event["tool_name"] != "bash":
        return None
    command = str((event.get("input") or {}).get("command") or "")
    if not is_dangerous(command):
        return None

    allowed = await ctx.ui.confirm(
        "⚠️ Dangerous command",
        f"The agent wants to run:\n\n  {command}\n\nAllow it?",
    )
    if not allowed:
        return {"block": True, "reason": "Blocked by user"}
    return None


def permission_gate_extension(api: Any) -> None:
    """Extension entry point: register the confirm-gated ``tool_call`` veto."""
    api.on("tool_call", permission_gate_tool_call)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/30_permission_gate.py`` → ``getattr(module, "register")``).
register = permission_gate_extension
