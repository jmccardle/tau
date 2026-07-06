"""Example 22: Gatekeeper — a ``tool_call`` veto that fences the agent (E2).

The enforcement layer that makes the agent-callable mutation tools safe. It
registers a single ``tool_call`` hook (E2, step S11 / S15) and vetoes two classes
of call *before they execute*:

1. **Scope guard (writes).** A ``write`` / ``edit`` whose target ``path`` does not
   resolve under one of the allowed prefixes listed in ``.tau/scope.txt`` is
   denied. The scope file is a plain newline-delimited list of path prefixes
   (``#`` comments and blank lines ignored), resolved relative to the run's
   ``cwd``. An **absent or empty** scope file means *no write scope is declared*,
   so every write is out of scope and denied — this is fail-CLOSED, not a
   permissive fallback: the gate exists precisely to refuse undeclared writes.

2. **Held-out guard (reads + bash).** Any ``read`` / ``ls`` / ``grep`` / ``find``
   whose ``path`` lands inside ``tests_heldout/``, or any ``bash`` whose
   ``command`` references ``tests_heldout``, is denied — the held-out test set
   must stay invisible to the agent so it cannot overfit to it. This guard also
   covers writes *into* ``tests_heldout/`` (checked before the scope rule).

Both rules are expressed the pi-faithful way: the hook returns
``{"block": True, "reason": ...}`` and the loop converts that into an error tool
result whose text is exactly ``reason`` (pi ``agent-loop.ts:597-602``; τ
``_prepare_tool_call`` → ``BlockedCall``). ``bash`` is deliberately governed by
the held-out rule only — its write target cannot be known statically, so the
scope rule (which needs a concrete ``path``) does not apply to it.

## Field contract

τ owns the tool-argument field names, so this reads ``event["input"]["path"]``
directly (no pi ``args ?? input`` dual-read): ``write`` / ``edit`` / ``read`` /
``ls`` / ``grep`` / ``find`` all take ``path``; ``bash`` takes ``command``.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.gatekeeper import gatekeeper_extension  # loaded via importlib in tests

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "edit", "bash"],
    extensions=[gatekeeper_extension],
)
```

.. note::

   ``gatekeeper_extension`` registers via ``api.on("tool_call", …)``, the
   pi-faithful surface an extension uses. The ``api.on`` → mutating-hook-runner
   bridge for the four E2 hooks lands in its own step; until then the *runner*
   (``session._extension_runner``) is the wired dispatch surface, which is what
   the smoke test drives to exercise the veto through the real loop.
"""

from __future__ import annotations

import os
from typing import Any

# The file (relative to the run cwd) listing allowed write-path prefixes.
SCOPE_FILE = os.path.join(".tau", "scope.txt")

# The held-out test directory (relative to the run cwd) the agent must not touch.
HELD_OUT_DIR = "tests_heldout"

# Path-based mutation tools governed by the scope guard. ``bash`` is NOT here:
# its write target is not statically known, so it is held-out-guarded only.
WRITE_TOOLS: frozenset[str] = frozenset({"write", "edit"})


def _resolve(cwd: str, path: str) -> str:
    """Absolute, normalized path for ``path`` interpreted relative to ``cwd``."""
    return os.path.abspath(os.path.join(cwd, path))


def _within(child_abs: str, parent_abs: str) -> bool:
    """True if ``child_abs`` is ``parent_abs`` itself or lives beneath it.

    Compares normalized absolute paths and requires a real path-segment boundary
    (``parent_abs`` + ``os.sep``) so ``/repo/srcx`` is not treated as being inside
    ``/repo/src``.
    """
    return child_abs == parent_abs or child_abs.startswith(parent_abs + os.sep)


def load_scope_prefixes(cwd: str) -> list[str]:
    """Read ``.tau/scope.txt`` under ``cwd`` into a list of absolute path prefixes.

    Blank lines and ``#`` comments are skipped. Returns ``[]`` when the file is
    absent — an undeclared scope, under which every write is out of scope (the
    scope guard is fail-CLOSED). Not a fallback: the gate's whole purpose is to
    refuse writes it was not told to allow.
    """
    scope_path = os.path.join(cwd, SCOPE_FILE)
    if not os.path.exists(scope_path):
        return []
    prefixes: list[str] = []
    with open(scope_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            prefixes.append(_resolve(cwd, line))
    return prefixes


def gatekeeper_decision(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    cwd: str,
    scope_prefixes: list[str],
) -> dict[str, Any] | None:
    """Pure veto decision for one prepared tool call.

    Returns a ``{"block": True, "reason": str}`` dict to deny the call, or
    ``None`` to allow it. The held-out guard runs first (it also covers writes
    into ``tests_heldout/``); the scope guard then applies to ``WRITE_TOOLS``.
    """
    held_out_abs = _resolve(cwd, HELD_OUT_DIR)

    # ── held-out guard (reads + bash + writes-into-heldout) ──────────────────
    if tool_name == "bash":
        command = str(tool_input.get("command") or "")
        if HELD_OUT_DIR in command:
            return {
                "block": True,
                "reason": (
                    f"Denied: bash command references the held-out test set "
                    f"({HELD_OUT_DIR}/); it must stay invisible to the agent."
                ),
            }
        # bash is not scope-guarded (its write target is not statically known).
        return None

    path = tool_input.get("path")
    if path is None:
        # A path-less call on a path-based tool: nothing concrete to fence.
        return None
    target_abs = _resolve(cwd, str(path))

    if _within(target_abs, held_out_abs):
        return {
            "block": True,
            "reason": (
                f"Denied: {path} is inside the held-out test set ({HELD_OUT_DIR}/); "
                f"it must stay invisible to the agent."
            ),
        }

    # ── scope guard (writes only) ────────────────────────────────────────────
    if tool_name in WRITE_TOOLS:
        if not any(_within(target_abs, prefix) for prefix in scope_prefixes):
            return {
                "block": True,
                "reason": (
                    f"Denied: write to {path} is outside the allowed scope "
                    f"(no matching prefix in {SCOPE_FILE})."
                ),
            }

    return None


def gatekeeper_tool_call(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
    """The ``tool_call`` hook handler (``handler(event, ctx)``).

    Reads the run ``cwd`` from the bound :class:`ExtensionContext`, loads the
    live scope prefixes, and delegates to :func:`gatekeeper_decision`.
    """
    cwd = getattr(ctx, "cwd", ".") or "."
    scope_prefixes = load_scope_prefixes(cwd)
    return gatekeeper_decision(
        tool_name=event["tool_name"],
        tool_input=event.get("input") or {},
        cwd=cwd,
        scope_prefixes=scope_prefixes,
    )


def gatekeeper_extension(api: Any) -> None:
    """Extension entry point: register the gatekeeper ``tool_call`` veto."""
    api.on("tool_call", gatekeeper_tool_call)


#: The module-level ``register`` the file-path loader looks up (``tau -e
#: examples/22_gatekeeper.py`` → ``sdk._load_one_extension`` → ``getattr(module,
#: "register")``). It IS :func:`gatekeeper_extension`; the alias makes the demo
#: loadable through the public ``-e`` surface used by the live procedures
#: (EXTENSIONS-LIVE-PROCEDURES.md; EXTENSIONS-E5-WIRING.md §6 / S37), not only via
#: the direct ``gatekeeper_extension(api)`` call the unit tests use.
register = gatekeeper_extension
