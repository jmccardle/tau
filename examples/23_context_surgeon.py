"""Example 23: Context Surgeon — agent tools that operate on the conversation itself.

The capstone of the E0→E4 extension chain (step S22). It composes the two prior
E-demos into one bundle of **model-callable session-control tools** built on the
landed E3-ctx op surface (``ExtensionContext.compact`` / ``summarize_branch`` /
``fork``), made safe by the E2 gatekeeper veto (example 22):

* **``compact_now``** — *turn-deferred* compaction. A tool cannot compact under the
  live agent loop (it is mid-turn), so it calls ``ctx.compact(defer=True)``, which
  RECORDS the intent and returns immediately; the compaction is applied exactly
  once at the tail of ``prompt()`` — the same drain site as auto-compaction
  (decision 3). The tool returns a normal result now; the log grows a ``compaction``
  entry at end-of-turn.

* **``summarize_history(from_entry)``** — summarize a branch of the conversation and
  splice the summary onto the active path. Delegates to ``ctx.summarize_branch``:
  extract the subtree text at ``from_entry``, summarize it via the LLM, and APPEND
  a ``branch_summary`` entry so the abandoned siblings drop out of context. Returns
  the re-rendered active-path length.

* **``fork_session(entry_id)``** — copy the conversation into a NEW session file via
  ``ctx.fork(mode="export")`` and return the forked path. Optionally, when a
  ``delegate_task`` is supplied, it **spawns a delegate** (example 20) — an isolated
  ``tau -p`` child process — to carry out follow-up work while the fork preserves
  the branch point. The forked path plus the delegate's output are returned.

## Composition (demos 20 + 22)

* **20_delegate** — ``fork_session`` reuses the delegate's child-spawning machinery
  (``_delegate_execute``): after exporting the fork it can hand a task to an
  isolated subagent whose own context window is separate from this one.
* **22_gatekeeper** — these agent-callable mutation tools are only safe because the
  ``tool_call`` veto fences the *filesystem* tools the agent (and the delegate
  children) can reach. :func:`context_surgeon_gatekeeper` re-exports the demo-22
  hook so a host can load the surgeon tools and the veto together as one bundle
  (plan decision 2: extensions may expose fork/compact as tools *because* E2 is the
  safety). ``context_surgeon_extension`` registers only the tools; wire the
  gatekeeper veto onto the session's mutating-hook runner alongside it.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.context_surgeon import context_surgeon_extension  # via importlib in tests

session = create_agent_session(
    model="gpt-4o",
    tools=["read"],
    extensions=[context_surgeon_extension],
)
```

Reference: EXTENSIONS-IMPLEMENTATION.md §E-demo-3, §8 S22.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


# ── load the sibling demos (their filenames are not valid identifiers) ────────
def _load_sibling(filename: str, modname: str) -> ModuleType:
    """Import a sibling ``examples/*.py`` whose numeric filename is not a valid
    Python identifier, via ``importlib`` file-path loading — the same mechanism
    the tests use to load these demos. Registered in ``sys.modules`` first so a
    module using ``from __future__ import annotations`` resolves its own dataclass
    annotations during ``exec_module``.
    """
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling example {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


_delegate = _load_sibling("20_delegate.py", "tau_example_delegate")
_gatekeeper = _load_sibling("22_gatekeeper.py", "tau_example_gatekeeper")

# Re-export the demo-22 veto so a host can load it onto the session's mutating-hook
# runner alongside the surgeon tools (the E2 safety these tools depend on).
context_surgeon_gatekeeper = _gatekeeper.gatekeeper_tool_call


# ── compact_now (turn-deferred) ──────────────────────────────────────────────


async def _compact_now_execute(
    tool_call_id: str,
    params: dict[str, Any],
    signal: Any,
    on_update: Callable | None,
    ctx: Any,
) -> dict[str, Any]:
    """Schedule an end-of-turn compaction (``ctx.compact(defer=True)``).

    Deferred (decision 3): the compaction cannot run under the live loop, so the
    intent is recorded and applied once at the tail of ``prompt()``. Returns a
    normal result immediately.
    """
    custom_instructions = params.get("custom_instructions") or None
    result = await ctx.compact(custom_instructions=custom_instructions, defer=True)
    # A deferred compact only records intent — Fail-Early: it must NOT have run
    # under the live loop.
    if result is not None:
        raise RuntimeError("compact_now: deferred compaction unexpectedly ran mid-turn")
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    "Compaction scheduled: it will run once at the end of this turn, "
                    "summarizing the earlier conversation to reclaim context."
                ),
            }
        ],
        "details": {"deferred": True, "custom_instructions": custom_instructions},
    }


COMPACT_NOW_TOOL = {
    "name": "compact_now",
    "label": "Compact now",
    "description": (
        "Schedule a compaction of the conversation so far. It runs at the end of "
        "the current turn, replacing the earlier history with a summary to reclaim "
        "context window. Optionally pass custom_instructions to focus the summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "custom_instructions": {
                "type": "string",
                "description": "Optional extra focus for the compaction summary.",
            }
        },
        "required": [],
    },
    "execute": _compact_now_execute,
    "execution_mode": "sequential",
}


# ── summarize_history(from_entry) ─────────────────────────────────────────────


async def _summarize_history_execute(
    tool_call_id: str,
    params: dict[str, Any],
    signal: Any,
    on_update: Callable | None,
    ctx: Any,
) -> dict[str, Any]:
    """Summarize the branch at ``from_entry`` and splice it onto the active path.

    Delegates to ``ctx.summarize_branch`` (append a ``branch_summary`` entry). The
    ``from_entry`` must be a real entry id — Fail-Early: a missing/blank id RAISES
    rather than silently summarizing nothing.
    """
    from_entry = params.get("from_entry")
    if not from_entry or not str(from_entry).strip():
        raise ValueError("summarize_history: 'from_entry' (an entry id) is required")
    custom_instructions = params.get("custom_instructions") or None
    rendered = await ctx.summarize_branch(str(from_entry), custom_instructions=custom_instructions)
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Summarized the conversation branch at {from_entry}; the active "
                    f"context is now {len(rendered)} message(s)."
                ),
            }
        ],
        "details": {"from_entry": from_entry, "rendered_messages": len(rendered)},
    }


SUMMARIZE_HISTORY_TOOL = {
    "name": "summarize_history",
    "label": "Summarize history",
    "description": (
        "Summarize a branch of the conversation starting at a given entry id and "
        "splice the summary onto the active path, dropping the summarized detail "
        "out of context. Use an entry id from the session's entry list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "from_entry": {
                "type": "string",
                "description": "The entry id to summarize the subtree from.",
            },
            "custom_instructions": {
                "type": "string",
                "description": "Optional extra focus for the branch summary.",
            },
        },
        "required": ["from_entry"],
    },
    "execute": _summarize_history_execute,
    "execution_mode": "sequential",
}


# ── fork_session(entry_id) → forked path + optional delegate ──────────────────


async def _fork_session_execute(
    tool_call_id: str,
    params: dict[str, Any],
    signal: Any,
    on_update: Callable | None,
    ctx: Any,
) -> dict[str, Any]:
    """Export the conversation to a new session file, optionally spawning a delegate.

    ``ctx.fork(mode="export")`` copies the session into a NEW file (the source log
    is never touched) and returns its path — Fail-Early: an in-memory SDK log has
    no file to export to and RAISES. When ``delegate_task`` is supplied, a single
    isolated ``tau -p`` child (example 20) is spawned to carry out follow-up work;
    its output is rolled into the result alongside the forked path.
    """
    entry_id = params.get("entry_id") or None
    forked_path = await ctx.fork(entry_id, mode="export")
    if not isinstance(forked_path, str):
        raise RuntimeError("fork_session: export fork did not return a file path")

    delegate_task = params.get("delegate_task")
    text = f"Forked the session to {forked_path}."
    delegate_details: dict[str, Any] | None = None

    if delegate_task and str(delegate_task).strip():
        # Compose demo 20: hand the follow-up task to an isolated subagent whose
        # own context window is separate from this (now forked) one.
        delegate_params: dict[str, Any] = {"task": str(delegate_task)}
        if params.get("delegate_model"):
            delegate_params["model"] = params["delegate_model"]
        if params.get("delegate_tools") is not None:
            delegate_params["tools"] = params["delegate_tools"]
        delegate_result = await _delegate._delegate_execute(
            tool_call_id, delegate_params, signal, on_update, ctx
        )
        delegate_details = delegate_result.get("details")
        delegate_text = _delegate_text(delegate_result)
        text += f"\n\nDelegate result:\n{delegate_text}"

    return {
        "content": [{"type": "text", "text": text}],
        "details": {
            "forked_path": forked_path,
            "entry_id": entry_id,
            "delegate": delegate_details,
        },
    }


def _delegate_text(delegate_result: dict[str, Any]) -> str:
    """Pull the delegate's text content out of its tool-result payload."""
    content = delegate_result.get("content") or []
    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "".join(parts) or "(no delegate output)"


FORK_SESSION_TOOL = {
    "name": "fork_session",
    "label": "Fork session",
    "description": (
        "Copy the current conversation into a new session file (returning its path) "
        "so the current branch is preserved before diverging. Optionally pass "
        "entry_id to position the fork's cursor at a branch point, and delegate_task "
        "to spawn an isolated subagent that carries out follow-up work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "description": "Optional entry id to position the fork's cursor at.",
            },
            "delegate_task": {
                "type": "string",
                "description": "Optional task handed to a spawned isolated subagent.",
            },
            "delegate_model": {
                "type": "string",
                "description": "Optional model for the spawned delegate.",
            },
            "delegate_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tool allowlist for the spawned delegate.",
            },
        },
        "required": [],
    },
    "execute": _fork_session_execute,
    "execution_mode": "sequential",
}


# ── the extension ─────────────────────────────────────────────────────────────


def context_surgeon_extension(api: Any) -> None:
    """Register the three session-control tools (compact / summarize / fork).

    The E2 safety these agent-callable mutation tools depend on is the gatekeeper
    veto (:data:`context_surgeon_gatekeeper`); wire it onto the session's
    mutating-hook runner alongside this extension to fence the filesystem tools the
    agent and its delegate children can reach.
    """
    api.register_tool(COMPACT_NOW_TOOL)
    api.register_tool(SUMMARIZE_HISTORY_TOOL)
    api.register_tool(FORK_SESSION_TOOL)
