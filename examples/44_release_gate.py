"""Example 44: Release gate — an extension that stops the session and asks.

Reference: docs/EXTENSION-LOCKS.md. The demo for ``api.request_user_action``:
one reserved ``customEntry`` carrying a lock, an ask, or both.

## What this shows

A tool call to ``bash`` that looks like a deploy appends a LOCKED request with
an ask. The next prompt is refused — in the TUI the typed line stays in the
editor and the modal comes back, headless the refusal is the
:class:`~tau_agent_core.submission.SubmissionResult`. Answering the ask appends
a response entry, which moves the cursor, which is the release; the action's
command then runs with the request id as its one argument and reads the answer
back off the tree.

Nothing blocks. The lock is a tree node, so it survives ``ctrl+C`` and a
reload: start τ again on the same session and the request is still under the
cursor, still refusing, still rendered. Three ways out, all ordinary tree
operations: answer it, run ``/gate-clear``, or branch to the parent node in the
tree browser.

## Usage

    tau -e examples/44_release_gate.py
    > run `bash -lc "echo deploy"`          # the gate arms
    > anything at all                        # refused, with the reason
    /gate-status                             # what is outstanding, if anything
    /gate-clear                              # the declared way out

The four states in docs/EXTENSION-LOCKS.md §3 are all reachable from here:
``/gate-ask`` asks without locking, ``/gate-hold`` locks without asking.
"""

from typing import Any

from tau_agent_core.extension_locks import RESPONSE_ENTRY_TYPE, request_at_cursor
from tau_agent_core.session_log import resolve_cursor


def _answers(ctx: Any, request_id: str) -> dict[str, Any]:
    """The values the user filled in for ``request_id``, or ``{}``.

    Read off the tree rather than passed in: the action's one argument is the
    request id (docs/EXTENSION-LOCKS.md §8), and the filled form rides as the
    response entry the answer appended.
    """
    for entry in reversed(ctx.entries()):
        if entry.get("customType") != RESPONSE_ENTRY_TYPE:
            continue
        data = entry.get("data") or {}
        if data.get("requestId") == request_id:
            values: dict[str, Any] = data.get("values") or {}
            return values
    return {}


def register(api: Any) -> None:
    """Arm the gate on a deploy-shaped bash call; register the three commands."""

    ASK = {
        "title": "Release gate",
        "text": "This command touches a deployment. Say who approved it.",
        "fields": [
            {"name": "approver", "kind": "text", "label": "Approved by"},
            {
                "name": "scope",
                "kind": "select",
                "label": "Scope",
                "options": ["staging", "production"],
                "default": "staging",
            },
        ],
        "actions": [{"label": "Approve", "command": "gate-approve"}],
    }

    async def on_tool_call(event: dict[str, Any], ctx: Any) -> None:
        if event.get("tool_name") != "bash":
            return
        command = str((event.get("input") or {}).get("command") or "")
        if "deploy" not in command:
            return
        api.request_user_action(
            f"A deploy-shaped command ran: {command!r}",
            lock=True,
            ask=ASK,
            release="gate-clear",
        )

    async def approve(args: str, ctx: Any) -> str:
        values = _answers(ctx, args.strip())
        if not values:
            return f"No response entry for request {args.strip()!r}"
        return f"Approved by {values['approver']} for {values['scope']}"

    async def clear(args: str, ctx: Any) -> str:
        """The declared release. It has to MOVE THE CURSOR, not merely run.

        Being exempt from the lock (docs/EXTENSION-LOCKS.md §5) is what lets this
        command be admitted at all; it is not what releases anything. The cursor
        moving is the release, so a release command navigates off the request —
        here, to its parent, which is the same node the tree browser's "branch
        from the message before it" escape lands on.
        """
        entries = ctx.entries()
        request = request_at_cursor(entries, resolve_cursor(entries))
        if request is None:
            return "Nothing to clear at the cursor."
        parent = next(e["parentId"] for e in entries if str(e["id"]) == request.entry_id)
        await ctx.navigate(str(parent) if parent is not None else None)
        return "Gate cleared. The cursor moved off the request, which is the release."

    async def status(args: str, ctx: Any) -> str:
        entries = ctx.entries()
        request = request_at_cursor(entries, resolve_cursor(entries))
        if request is None:
            return "Nothing outstanding at the cursor."
        return f"{request.label}: {request.sentence}"

    async def ask_only(args: str, ctx: Any) -> str:
        api.request_user_action("A question you are free to ignore.", ask=ASK)
        return "Asked without locking — a prompt still advances."

    async def hold(args: str, ctx: Any) -> str:
        api.request_user_action(
            "Held with no form. /gate-clear is the way out.",
            lock=True,
            release="gate-clear",
        )
        return "Locked with no ask."

    api.on("tool_call", on_tool_call)
    api.register_command("gate-approve", {"description": "answer the gate", "handler": approve})
    api.register_command("gate-clear", {"description": "clear the gate", "handler": clear})
    api.register_command("gate-status", {"description": "what is pending", "handler": status})
    api.register_command("gate-ask", {"description": "ask, do not lock", "handler": ask_only})
    api.register_command("gate-hold", {"description": "lock, do not ask", "handler": hold})
