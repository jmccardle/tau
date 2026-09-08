"""Example 30: Permission Gate — human-in-the-loop veto (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S60 (replaces the broken original
``01_permission_gate.py``, which was a pattern-only auto-block with no human in
the loop — see ``examples/01_permission_gate.py`` for that one; this is a
DIFFERENT demo, not a fix of it). Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/permission-gate.ts``.

## What this shows

A ``tool_call`` veto that, on a *dangerous* ``bash`` command, does not auto-deny.
It blocks the call and, when the turn is over, STOPS the session on a locked
request (docs/EXTENSION-LOCKS.md §3, row 3) whose ask offers "allow this
command" and "deny". Answering "allow" records the command in an allowlist, so
the model's next attempt at the same command goes through; answering "deny"
leaves it blocked. Either way the lock releases, because answering appends and
appending moves the cursor.

## Why the request is raised at ``user_turn_end`` and not in the veto

A lock is read at the CURSOR (docs/EXTENSION-LOCKS.md §2), and a turn keeps
appending after a ``tool_call`` hook returns — the tool result, the next
completion, the answer. A request appended from inside the veto would be four
entries behind the leaf by the time the turn ended, and inert. ``user_turn_end``
is the last hook of a prompt, so a request raised there IS the leaf.

Blocking and asking are therefore two steps here, which is also the honest
sequence: the model is told the call was refused and gets to finish its turn,
and only then does the session stop for a human.

## Why this is not ``ctx.ui.confirm`` any more

It used to be, and the deliberate divergence from pi documented here was about
``ctx.hasUI``. That whole discussion is gone with the method: a blocking dialog
holds ``AgentSession._turn_lock`` for as long as a human takes, dies with the
process, and describes itself to no head but the Textual one
(docs/EXTENSION-LOCKS.md §1). The lock has none of those properties — it is a
tree node, so it survives a restart, every head renders it, and nothing is
parked on a lock while nobody answers.

The veto itself is unchanged and still Fail-CLOSED: the hook returns
``{"block": True}``, and the ``tool_call`` call-site blocks on any handler
exception too (``agent_loop.py`` ``_prepare_tool_call``). What the lock adds is
the half a veto cannot do — "deny and tell the model" is not "stop and ask a
human", and the loop would otherwise carry straight on with an error result.

## Field contract

τ owns the tool-argument field names, so the ``bash`` command is read directly
from ``event["input"]["command"]`` (no pi ``args ?? input`` dual-read).

## Usage

    tau -e examples/30_permission_gate.py
    > delete the build directory with rm -rf

The call is blocked and the session stops with "Extension 30_permission_gate
requires a response". Answer the ask, run ``/gate-allowed`` to see what is
allowed, or branch to the parent node in the tree browser to get out from under
it entirely.

Headless, the refusal is the ``SubmissionResult``: ``tau -p`` prints the reason
and exits non-zero rather than silently continuing.
"""

from __future__ import annotations

import re
from typing import Any

from tau_agent_core.extension_locks import request_at_cursor
from tau_agent_core.session_log import resolve_cursor

DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+(-rf?|--recursive)", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\b(chmod|chown)\b.*777", re.IGNORECASE),
]


def is_dangerous(command: str) -> bool:
    """True if ``command`` matches any of the dangerous bash patterns."""
    return any(pattern.search(command) for pattern in DANGEROUS_PATTERNS)


def permission_gate_extension(api: Any) -> None:
    """Extension entry point: the veto, the lock it raises, and its two actions."""
    #: Commands a human approved this session, and the request each answer belongs to.
    allowed: set[str] = set()
    asked: dict[str, str] = {}
    #: What this turn's veto blocked, held until the turn edge can ask about it.
    blocked: list[str] = []

    async def on_tool_call(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """Block a dangerous ``bash`` command and remember it for the turn edge."""
        if event["tool_name"] != "bash":
            return None
        command = str((event.get("input") or {}).get("command") or "")
        if not is_dangerous(command) or command in allowed:
            return None
        blocked.append(command)
        return {"block": True, "reason": "Blocked pending human approval"}

    async def on_user_turn_end(event: dict[str, Any], ctx: Any) -> None:
        """Raise ONE locked request for whatever this turn blocked (see the docstring)."""
        if not blocked:
            return
        command = blocked.pop()
        blocked.clear()
        asked[
            api.request_user_action(
                f"Blocked a dangerous command: {command}",
                lock=True,
                release="gate-deny",
                ask={
                    "title": "⚠️ Dangerous command",
                    "text": f"The agent wants to run:\n\n  {command}",
                    "actions": [
                        {"label": "Allow this command", "command": "gate-allow"},
                        {"label": "Deny", "command": "gate-deny"},
                    ],
                },
            )
        ] = command

    async def gate_allow(args: str, ctx: Any) -> str:
        """The ask's first action; its one argument is the request id (§8)."""
        command = asked.pop(args.strip(), None)
        if command is None:
            return f"No blocked command for request {args.strip()!r}"
        allowed.add(command)
        return f"Allowed: {command}. Ask the agent to run it again."

    async def gate_deny(args: str, ctx: Any) -> str:
        """The ask's second action, and the declared release.

        Two callers, and the difference is who moved the cursor. Dispatched as an
        ACTION, ``answer_request`` has already appended the response and released
        the lock, so there is nothing at the cursor and this only records the
        decision. Typed as ``/gate-deny``, nothing has moved — and being exempt
        from the lock is what lets the command RUN, not what releases it
        (docs/EXTENSION-LOCKS.md §5, §6) — so it navigates off the request itself.
        """
        asked.pop(args.strip(), None)
        entries = ctx.entries()
        request = request_at_cursor(entries, resolve_cursor(entries))
        if request is not None:
            parent = next(e["parentId"] for e in entries if str(e["id"]) == request.entry_id)
            await ctx.navigate(str(parent) if parent is not None else None)
        return "Denied. The command stays blocked."

    async def gate_allowed(args: str, ctx: Any) -> str:
        return "\n".join(sorted(allowed)) or "Nothing has been allowed this session."

    api.on("tool_call", on_tool_call)
    api.on("user_turn_end", on_user_turn_end)
    api.register_command(
        "gate-allow", {"description": "allow the blocked command", "handler": gate_allow}
    )
    api.register_command("gate-deny", {"description": "keep it blocked", "handler": gate_deny})
    api.register_command(
        "gate-allowed", {"description": "list what is allowed", "handler": gate_allowed}
    )


register = permission_gate_extension
