"""Example 31: Protected Paths — a pure policy gate, zero UI (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S60. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/protected-paths.ts``.

## What this shows

A ``tool_call`` veto that blocks ``write``/``edit`` calls whose target path
touches a protected path — no dialog, no human, just a rule. This is the
counterpoint to ``30_permission_gate.py``: a gate that governs entirely by
policy and never awaits anything, so it behaves identically headless or in
the TUI (``ctx.ui.notify`` is non-blocking either way).

## The path list is S40 ``api.config``, not a hardcoded literal

pi hardcodes the list inline (``const protectedPaths = [".env", ".git/",
"node_modules/"]``). τ sources it from this extension's own per-extension
config slice (E6 §2 / S40) — ``~/.tau/config.json``
``{"extensions": {"31_protected_paths": {"paths": [...]}}}`` or a per-run
``--ext-config 31_protected_paths.paths='[".env", "secrets/"]'`` override —
falling back to :data:`DEFAULT_PROTECTED_PATHS` (the same three pi entries)
when unconfigured. This is a documented default, not a Fail-Early violation:
an unconfigured extension reading its own shipped default is ordinary demo
design, the same way pi's own list is a hardcoded default; the harness never
fabricates the VALUE of ``api.config`` itself (an unconfigured extension gets
``{}``, per S40) — this demo just chooses what to do with an empty config.

## Field contract

τ owns the tool-argument field names, so the target path is read directly from
``event["input"]["path"]`` (no pi ``args ?? input`` dual-read); only ``write``
and ``edit`` carry a ``path``, matching the tools pi's original governs.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.protected_paths import protected_paths_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["write", "edit"],
    extensions=[protected_paths_extension],
)
```

Or with a custom path list via config::

    tau -e examples/31_protected_paths.py \\
        --ext-config 31_protected_paths.paths='[".env", ".git/", "secrets/"]'
"""

from __future__ import annotations

from typing import Any

#: The shipped default protected-path fragments (pi ``protected-paths.ts`` parity).
#: A path is protected if any of these substrings appears in it.
DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (".env", ".git/", "node_modules/")

#: Tools this gate governs — both carry a ``path`` argument to fence.
GUARDED_TOOLS: frozenset[str] = frozenset({"write", "edit"})


def protected_paths_decision(
    *, tool_name: str, tool_input: dict[str, Any], protected_paths: tuple[str, ...]
) -> dict[str, Any] | None:
    """Pure veto decision for one prepared tool call.

    Returns ``{"block": True, "reason": str}`` when ``tool_name`` is a guarded
    tool and its ``path`` contains one of ``protected_paths``, else ``None``.
    """
    if tool_name not in GUARDED_TOOLS:
        return None
    path = tool_input.get("path")
    if not path:
        return None
    path = str(path)
    if any(protected in path for protected in protected_paths):
        return {"block": True, "reason": f'Path "{path}" is protected'}
    return None


def protected_paths_extension(api: Any) -> None:
    """Extension entry point: register the protected-path ``tool_call`` veto.

    Reads the path list from ``api.config["paths"]`` (S40), falling back to
    :data:`DEFAULT_PROTECTED_PATHS` when unconfigured.
    """
    protected_paths = tuple(api.config.get("paths") or DEFAULT_PROTECTED_PATHS)

    def protected_paths_tool_call(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        decision = protected_paths_decision(
            tool_name=event["tool_name"],
            tool_input=event.get("input") or {},
            protected_paths=protected_paths,
        )
        if decision is not None:
            ctx.ui.notify(
                f"Blocked write to protected path: {(event.get('input') or {}).get('path')}",
                "warning",
            )
        return decision

    api.on("tool_call", protected_paths_tool_call)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/31_protected_paths.py`` → ``getattr(module, "register")``).
register = protected_paths_extension
