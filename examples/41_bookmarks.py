"""Example 41: Bookmarks — labeled tree waypoints (E9, S64).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S64 row 1. No pi original — pi has
no tree-shaped session to bookmark a position in; this is a τ-native demo of
the tree-as-backplane thesis (roadmap §1.1) applied to navigation instead of
scalar state (contrast ``38_todo``, which uses the same primitive for a list).

## What this shows

``/bookmark <label>`` records "here" — the CURRENT active-path leaf id
(:func:`ext_kit.state.active_cursor` replayed over ``ctx.entries()``, S56) — as
a labeled waypoint, persisted via :class:`ext_kit.state.TreeStore` (S56) under
its own ``customEntry`` type. Because ``customEntry`` is excluded from
``convert_to_llm``, the bookmark list is backplane state: durable, rendered in
the tree, reload-invariant, but never model input. ``/goto <label>`` moves the
cursor back to a bookmarked entry via ``ctx.navigate`` (E3-ctx / S19) — zero LLM
calls, the abandoned branch (if any) simply drops out of context via the
``parentId`` walk and stays on disk. ``/bookmarks`` is the S46 listing command:
a display-only report of every waypoint, the same command-output channel
``38_todo``'s ``/todos`` and ``40_handoff`` use in place of pi's
``ctx.ui.custom`` (roadmap §6.1).

## Why ``active_cursor`` and not ``ctx.entries()[-1]``

The naive "bookmark the last entry" is wrong once the user has already
navigated away from the tip: the last RAW entry in the log is then the
``navigate`` entry itself, whose ``targetId`` (not its own id) is where the
conversation actually sits. :func:`ext_kit.state.active_cursor` replays the
log's append/navigate algebra exactly like a live ``SessionLog.cursor`` would,
so a bookmark always names the entry the user is actually looking at, not the
bookkeeping entry that moved them there. This is the same primitive
:class:`ext_kit.state.TreeStore` uses internally to find the active path,
promoted to a public helper (S64) because ``41_bookmarks`` needs the identical
"where am I now" answer for a different purpose (recording a position, not
filtering records).

## Usage

    tau -e examples/41_bookmarks.py
    > let's refactor auth
    > /bookmark before-auth-refactor
    Bookmarked 'before-auth-refactor' at a1b2c3d4
    > ... more turns, maybe a wrong turn ...
    > /bookmarks
    before-auth-refactor -> a1b2c3d4
    > /goto before-auth-refactor
    Jumped to bookmark 'before-auth-refactor' (a1b2c3d4)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# ``ext_kit`` lives alongside the numbered examples, not inside an installed
# package — add ``examples/`` to the path the same way the other ext_kit-using
# demos (e.g. 38_todo, S56) do when run standalone.
_EXAMPLES_DIR = str(Path(__file__).resolve().parent)
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from ext_kit.state import TreeStore, active_cursor  # noqa: E402  (path insertion must precede this)

#: The ``customEntry`` type this demo's records live under (S39/S56).
BOOKMARK_CUSTOM_TYPE = "bookmark"


def _bookmarks_by_label(store: TreeStore[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reconstruct ``{label: record}`` from the active path — latest write per
    label wins (re-bookmarking a label moves it, it does not duplicate it)."""
    store.load()
    by_label: dict[str, dict[str, Any]] = {}
    for record in store.records:
        by_label[str(record["label"])] = record
    return by_label


async def _bookmark_command(args: str, ctx: Any, *, store: TreeStore[dict[str, Any]]) -> str:
    """``/bookmark <label>``: record the current active-path leaf under ``label``."""
    label = args.strip()
    if not label:
        return "Usage: /bookmark <label>"

    entries = ctx.entries()
    # A brand-new session already carries bookkeeping entries (model change,
    # etc.) before a single word is exchanged (pi parity, see ``40_handoff``'s
    # identical check) — "nothing to bookmark" means no actual conversation,
    # not a literally-empty log.
    if not any(e.get("type") in ("message", "customMessage") for e in entries):
        return "Nothing to bookmark yet — start a conversation first."
    cursor = active_cursor(entries)

    store.append({"label": label, "entry_id": cursor})
    return f"Bookmarked '{label}' at {cursor}"


def _bookmarks_command(args: str, ctx: Any, *, store: TreeStore[dict[str, Any]]) -> str:
    """``/bookmarks``: a display-only report of every waypoint (S46 output
    channel — τ's substitute for pi's ``ctx.ui.custom``)."""
    by_label = _bookmarks_by_label(store)
    if not by_label:
        return "No bookmarks yet. Use /bookmark <label> to create one."
    lines = [f"{label} -> {record['entry_id']}" for label, record in sorted(by_label.items())]
    return "\n".join(lines)


async def _goto_command(args: str, ctx: Any, *, store: TreeStore[dict[str, Any]]) -> str:
    """``/goto <label>``: move the cursor to a bookmarked entry via ``ctx.navigate``."""
    label = args.strip()
    if not label:
        return "Usage: /goto <label>"

    by_label = _bookmarks_by_label(store)
    record = by_label.get(label)
    if record is None:
        return f"No bookmark named '{label}'"

    entry_id = str(record["entry_id"])
    await ctx.navigate(entry_id)
    return f"Jumped to bookmark '{label}' ({entry_id})"


def bookmarks_extension(api: Any) -> None:
    """Extension entry point: register ``/bookmark``, ``/bookmarks``, ``/goto``."""
    store: TreeStore[dict[str, Any]] = TreeStore(api, BOOKMARK_CUSTOM_TYPE)

    async def bookmark_handler(args: str, ctx: Any) -> str:
        return await _bookmark_command(args, ctx, store=store)

    def bookmarks_handler(args: str, ctx: Any) -> str:
        return _bookmarks_command(args, ctx, store=store)

    async def goto_handler(args: str, ctx: Any) -> str:
        return await _goto_command(args, ctx, store=store)

    api.register_command(
        "bookmark",
        {
            "description": "Bookmark the current point in the conversation (usage: /bookmark <label>)",
            "handler": bookmark_handler,
        },
    )
    api.register_command(
        "bookmarks",
        {
            "description": "List all bookmarks",
            "handler": bookmarks_handler,
        },
    )
    api.register_command(
        "goto",
        {
            "description": "Jump to a bookmarked point (usage: /goto <label>)",
            "handler": goto_handler,
        },
    )


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/41_bookmarks.py`` → ``getattr(module, "register")``).
register = bookmarks_extension
