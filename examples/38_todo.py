"""Example 38: Todo — tree-backplane state + a slash-command report (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S62. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/todo.ts``.

## What this shows

A ``todo`` tool the LLM can call (list/add/toggle/clear) with NO side database:
every mutation is persisted as a durable ``customEntry`` node via
:class:`ext_kit.state.TreeStore` (E8 §4 / S56) — the tree IS the state store.
Branch to a different point in the conversation and the todo list read back is
exactly what it was *there*, because :class:`TreeStore` reconstructs from the
active path, not a global side-channel. A ``/todos`` slash command reports the
current list as display-only text via the S46 command-output channel
(``ExtensionCommandResult.output`` -> a rendered box in the TUI, printed text
headless) — this is the τ-flavored swap for pi's ``ctx.ui.custom`` popup (§6.1
of the roadmap: τ deliberately does not chase the ``ctx.ui`` widget-factory
surface; a report command is the right-sized answer here).

## Field contract (faithful adaptation, not lazy)

pi keeps ``todos``/``nextId`` in a closure, reconstructed on ``session_start``/
``session_tree`` by scanning ``ctx.sessionManager.getBranch()`` for this tool's
past ``toolResult`` messages. τ's E5 durable-hook rework means a tool's own
result IS already a node on the active path — but ``TreeStore`` gives a typed,
purpose-built read/write pair over a *dedicated* ``customEntry`` kind rather
than re-parsing prior ``toolResult`` payloads by tool name, so this port reads
current state fresh via ``store.load()`` at the top of every mutating call (no
extension-lifetime in-memory cache to go stale across a branch/navigate) and
writes the new snapshot via ``store.append()``. A pure ``list`` read makes no
state change, so it does not append a new record.

pi's ``/todos`` command opens ``ctx.ui.custom(...)`` — a bespoke scrollable
overlay widget. τ does not build that widget-factory surface (roadmap §6.1,
"we deliberately do NOT chase that surface 1:1"); S46's command-output channel
(a returned string rendered as a display-only report box) is the declared
right-sized substitute for exactly this kind of "point-in-time report" need,
so ``/todos`` here returns a formatted string instead of opening a custom view.

## Usage

```python
import importlib.util
import sys

from tau_agent_core.sdk import create_agent_session

# examples/ is not an importable package: there is no __init__.py and every
# filename starts with a digit. Load the file by path, as `tau -e` does.
_spec = importlib.util.spec_from_file_location("ext", "examples/38_todo.py")
ext = importlib.util.module_from_spec(_spec)
# Register BEFORE exec_module: a module using `from __future__ import
# annotations` resolves its own dataclass annotations through sys.modules.
sys.modules[_spec.name] = ext
_spec.loader.exec_module(ext)

session = create_agent_session(
    model="gpt-4o",
    extensions=[ext.register],
)
```

Or load directly through the public ``-e`` surface::

    tau -e examples/38_todo.py
    > Add a todo to buy milk
    > /todos

## Debugger hits

A ``tool_result`` hook emits ``debug_hit`` on ``ext:<name>:debug_hit`` after each
landed ``todo`` call, for a break set such as ``/break add 38_todo:todo_completed``
(docs/STEP-DEBUGGER.md §5.4). Payload: ``{condition, evidence}`` with evidence a
list of ``{label, text}``.

| condition | after |
|---|---|
| ``todo_created`` | ``add`` |
| ``todo_completed`` | ``toggle`` that marks a todo done |
| ``todo_changed`` | ``toggle`` that reopens one, and ``clear`` |

A refused call (an ``error`` in its details) and a ``list`` emit nothing. The
debugger asks for the conditions on ``ext:tau_debugger:need_conditions`` and
this extension answers on ``ext:38_todo:debug_conditions`` with ``[{name, describe}]``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_EXAMPLES_DIR = str(Path(__file__).resolve().parent)
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from ext_kit.state import TreeStore  # noqa: E402  (path insertion must precede this)

#: The ``customEntry`` type this demo's records live under (S39/S56).
TODO_CUSTOM_TYPE = "todo"

_REPORT_PREVIEW_COUNT = 5


def _current_state(store: TreeStore[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Reconstruct ``(todos, next_id)`` from the active path's latest snapshot."""
    store.load()
    latest = store.latest()
    if latest is None:
        return [], 1
    return list(latest["todos"]), latest["next_id"]


def _snapshot(
    todos: list[dict[str, Any]],
    next_id: int,
    action: str,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "action": action,
        "todos": [dict(t) for t in todos],
        "next_id": next_id,
    }
    if error is not None:
        record["error"] = error
    return record


def _debug_hit(details: dict[str, Any], call_input: dict[str, Any]) -> dict[str, Any] | None:
    """The ``debug_hit`` payload for one finished ``todo`` call, or ``None`` for no hit."""
    action = details["action"]
    if details.get("error") is not None or action == "list":
        return None
    if action == "clear":
        return {
            "condition": "todo_changed",
            "evidence": [{"label": "action", "text": "cleared all todos"}],
        }
    todo = details["todos"][-1] if action == "add" else _find(details["todos"], call_input["id"])
    if action == "add":
        condition = "todo_created"
    else:
        condition = "todo_completed" if todo["done"] else "todo_changed"
    return {
        "condition": condition,
        "evidence": [
            {"label": "todo", "text": f"#{todo['id']}: {todo['text']}"},
            {"label": "done", "text": str(todo["done"]).lower()},
        ],
    }


def _find(todos: list[dict[str, Any]], todo_id: int) -> dict[str, Any]:
    return next(t for t in todos if t["id"] == todo_id)


#: The conditions announced on ``debug_conditions``: ``(name, describe)``.
DEBUG_CONDITIONS = (
    ("todo_created", "a todo was added"),
    ("todo_completed", "a todo was marked done"),
    ("todo_changed", "a todo was reopened, or the list was cleared"),
)


def _format_list(todos: list[dict[str, Any]]) -> str:
    if not todos:
        return "No todos"
    lines = [f"[{'x' if t['done'] else ' '}] #{t['id']}: {t['text']}" for t in todos]
    return "\n".join(lines)


async def _todo_execute(
    tool_call_id: str,
    params: dict[str, Any],
    signal: Any,
    on_update: Any,
    ctx: Any,
    *,
    store: TreeStore[dict[str, Any]],
) -> dict[str, Any]:
    """The ``todo`` tool: list/add/toggle/clear, backed entirely by ``store``."""
    action = params.get("action")
    todos, next_id = _current_state(store)

    if action == "list":
        # Read-only: no state change, so no new record.
        return {
            "content": [{"type": "text", "text": _format_list(todos)}],
            "details": _snapshot(todos, next_id, "list"),
        }

    if action == "add":
        text = params.get("text")
        if not text:
            return {
                "content": [{"type": "text", "text": "Error: text required for add"}],
                "details": _snapshot(todos, next_id, "add", error="text required"),
            }
        new_todo = {"id": next_id, "text": text, "done": False}
        todos = [*todos, new_todo]
        record = _snapshot(todos, next_id + 1, "add")
        await store.append(record)
        return {
            "content": [
                {"type": "text", "text": f"Added todo #{new_todo['id']}: {new_todo['text']}"}
            ],
            "details": record,
        }

    if action == "toggle":
        todo_id = params.get("id")
        if todo_id is None:
            return {
                "content": [{"type": "text", "text": "Error: id required for toggle"}],
                "details": _snapshot(todos, next_id, "toggle", error="id required"),
            }
        match = next((t for t in todos if t["id"] == todo_id), None)
        if match is None:
            return {
                "content": [{"type": "text", "text": f"Todo #{todo_id} not found"}],
                "details": _snapshot(todos, next_id, "toggle", error=f"#{todo_id} not found"),
            }
        toggled = [{**t, "done": not t["done"]} if t["id"] == todo_id else t for t in todos]
        new_done = next(t["done"] for t in toggled if t["id"] == todo_id)
        record = _snapshot(toggled, next_id, "toggle")
        await store.append(record)
        status = "completed" if new_done else "uncompleted"
        return {
            "content": [{"type": "text", "text": f"Todo #{todo_id} {status}"}],
            "details": record,
        }

    if action == "clear":
        count = len(todos)
        record = _snapshot([], 1, "clear")
        await store.append(record)
        return {
            "content": [{"type": "text", "text": f"Cleared {count} todos"}],
            "details": record,
        }

    return {
        "content": [{"type": "text", "text": f"Unknown action: {action}"}],
        "details": _snapshot(todos, next_id, "list", error=f"unknown action: {action}"),
    }


TODO_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "add", "toggle", "clear"],
            "description": "Which todo operation to perform.",
        },
        "text": {
            "type": "string",
            "description": "Todo text (for add).",
        },
        "id": {
            "type": "integer",
            "description": "Todo ID (for toggle).",
        },
    },
    "required": ["action"],
}


def _todos_command(args: str, ctx: Any, *, store: TreeStore[dict[str, Any]]) -> str:
    """``/todos`` handler: a display-only report of the current active-path state
    (S46's command-output channel — τ's substitute for pi's ``ctx.ui.custom``)."""
    todos, _next_id = _current_state(store)
    if not todos:
        return "No todos yet. Ask the agent to add some!"

    done = sum(1 for t in todos if t["done"])
    lines = [f"{done}/{len(todos)} completed", ""]
    for t in todos[:_REPORT_PREVIEW_COUNT]:
        check = "✓" if t["done"] else "○"
        lines.append(f"{check} #{t['id']} {t['text']}")
    remaining = len(todos) - _REPORT_PREVIEW_COUNT
    if remaining > 0:
        lines.append(f"... {remaining} more")
    return "\n".join(lines)


def todo_extension(api: Any) -> None:
    """Extension entry point: register the ``todo`` tool + ``/todos`` report."""
    store: TreeStore[dict[str, Any]] = TreeStore(api, TODO_CUSTOM_TYPE)

    async def execute(
        tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
    ) -> dict[str, Any]:
        return await _todo_execute(tool_call_id, params, signal, on_update, ctx, store=store)

    api.register_tool(
        {
            "name": "todo",
            "label": "Todo",
            "description": ("Manage a todo list. Actions: list, add (text), toggle (id), clear"),
            "parameters": TODO_TOOL_PARAMETERS,
            "execute": execute,
            "prompt_snippet": "todo: manage a per-conversation todo list",
            "execution_mode": "sequential",
        }
    )

    async def on_tool_result(event: dict[str, Any], ctx: Any) -> None:
        """``tool_result`` hook: a hit is emitted from a hook, never from a notify event."""
        if event["tool_name"] != "todo":
            return None
        hit = _debug_hit(event["details"], event["input"])
        if hit is not None:
            await api.emit("debug_hit", hit)
        return None

    api.on("tool_result", on_tool_result)

    async def on_need_conditions(payload: Any) -> None:
        """Answer the debugger's pull (docs/STEP-DEBUGGER.md §5.2)."""
        await api.emit(
            "debug_conditions", [{"name": n, "describe": d} for n, d in DEBUG_CONDITIONS]
        )

    api.on("ext:tau_debugger:need_conditions", on_need_conditions)

    api.register_command(
        "todos",
        {
            "description": "Show all todos on the current branch",
            "handler": lambda args, ctx: _todos_command(args, ctx, store=store),
        },
    )


register = todo_extension
