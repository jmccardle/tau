"""Example 80: Step mode, a suspension before every tool call.

Reference: docs/STEP-DEBUGGER.md §3.1 (``ctx.suspend``), §3.2 (``tool_call`` is a
site), §3.9 (refusing a turn nobody can step).

## What this shows

Every top-level tool call stops the turn before the tool runs. The head shows
the tool, its arguments and three actions:

- **step** runs the call;
- **skip** blocks it, so the model reads "skipped at a step" as the result;
- **abort** aborts the turn; core trips the abort signal, and no tool runs.

The handler runs under the ``human`` policy, which has no deadline: a human
takes as long as they take. Each stop is a durable ``suspension`` side entry,
and the tree browser marks it ``⏸`` on the entry it hangs from.

## Why the turn is refused at ``input``

A suspension needs something to answer it: a live head, and a submission that
set ``allow_user_input`` (§3.8). Where neither holds, ``ctx.suspend`` raises
``NoResponderError``, which would block every call. So the ``input`` hook
refuses the turn up front instead, naming why (§3.9).

## Usage

    tau -e examples/80_step_mode.py
    > list the files here

The TUI opens the step modal before each call.
"""

from __future__ import annotations

import json
from typing import Any

ACTIONS = [{"label": "step"}, {"label": "skip"}, {"label": "abort"}]


def step_spec(event: dict[str, Any]) -> dict[str, Any]:
    """The suspension spec for one ``tool_call`` event: the tool, its arguments, the actions."""
    arguments = json.dumps(event.get("input") or {}, indent=2, sort_keys=True)
    return {
        "title": f"Step: {event['tool_name']}",
        "text": f"The agent is about to call {event['tool_name']}.",
        "evidence": [{"label": "arguments", "text": arguments}],
        "actions": ACTIONS,
    }


def step_mode_extension(api: Any) -> None:
    """Extension entry point: refuse an unsteppable turn, then stop before each call."""

    async def on_input(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """Refuse a turn no one could step, rather than block every call in it (§3.9)."""
        if api.can_suspend():
            return None
        return {
            "refuse": "step mode is on, but nothing can answer a step here: use the TUI, "
            "or a head that declares suspensions with allow_user_input"
        }

    async def on_tool_call(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """Suspend before the call; ``skip`` blocks it, ``abort`` is core's."""
        resolution = await ctx.suspend(step_spec(event))
        if resolution["action"] == "skip":
            return {"block": True, "reason": "skipped at a step by the human"}
        return None

    api.on("input", on_input)
    api.on("tool_call", on_tool_call, policy="human")


register = step_mode_extension
