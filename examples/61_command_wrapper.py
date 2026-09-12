"""Example 61: Wrapping another extension's command by name.

Reference: docs/EXTENSION-NAMESPACE.md §4. Every extension command has two
names: ``ext:<extension>.<command>``, which nothing can take from it, and the
typed name it asks for, which is first-come. This example claims ``/ni`` if
nobody has it, and wraps whoever does if somebody has.

## What this shows

Three calls, and none of them captures a callable:

* ``api.register_command`` returns ``None`` when the claim succeeded, and
  otherwise the qualified name that holds it — a name, not a handler. Because
  the typed name is first-come, those two cases are exactly "nothing to wrap"
  and "here is what to wrap".
* ``api.run_command(qualified, args)`` dispatches at CALL time. That is the
  reason to prefer a name over a captured callable: if the wrapped extension is
  disabled, this raises instead of quietly running a dead extension's handler.
* ``api.get_command(name)`` reads what a name resolves to right now, which is
  what ``/wrap-status`` reports.

## Usage

Load the wrapper AFTER something that registers ``/todos``:

    tau -e examples/38_todo.py -e examples/61_command_wrapper.py
    > /todos
    No todos yet. Ask the agent to add some!
    > /ext:61_command_wrapper.todos
    [wrapped] No todos yet. Ask the agent to add some!
    > /wrap-status
    wrapping ext:38_todo.todos ('Show all todos on the current branch')

Load it alone and it takes ``/todos`` itself, with nothing underneath:

    tau -e examples/61_command_wrapper.py
    > /wrap-status
    holding /todos, wrapping nothing
"""

from __future__ import annotations

from typing import Any

#: The typed name this extension both wraps and tries to claim.
WRAPPED = "todos"


def register(api: Any) -> None:
    """Claim ``/todos`` if it is free, and wrap whoever holds it if it is not."""
    target: str | None = None

    async def wrapped(args: str, ctx: Any) -> str:
        if target is None:
            return f"/{WRAPPED} is mine and there was nothing underneath it"
        return f"[wrapped] {await api.run_command(target, args)}"

    async def status(args: str, ctx: Any) -> str:
        if target is None:
            return f"holding /{WRAPPED}, wrapping nothing"
        under = api.get_command(target) or {}
        return f"wrapping {target} ({under.get('description', '')!r})"

    target = api.register_command(WRAPPED, {"description": "todos, wrapped", "handler": wrapped})
    api.register_command("wrap-status", {"description": "what is wrapped", "handler": status})
