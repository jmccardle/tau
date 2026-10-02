"""§8.2/§8.3 provenance on the elide the TUI creates (TREE-BROWSER-AS-EDITOR.md).

``TauBackend.elide_span`` writes the elide at the bound cursor. It records the span
it folds (``coveredEntries``/``coveredTokens``) and the frame in force, not only
``firstKeptId``.

Two things are pinned here that the contract suite cannot pin, because they are
properties of the CALL SITE rather than of a store:

* the recorded span is the span the fold actually loses, not a number the caller
  made up — asserted against ``context_entries`` before and after, and
* the recorded frame is the ``agent_spec`` in force **at the anchor**, which for a
  browser-driven elide aimed at old history is not the session's current one.

A separate file from ``test_tree_elide.py`` (which drives the same backend method
through the TUI action) because these are backend-level assertions about the entry
that gets written, not about the modal flow that asks for it.

Reference: TREE-BROWSER-AS-EDITOR.md §8.2, §8.3, §11.3; NODE-ADDRESSABLE-AGENTS.md W3.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.cursor import Cursor
from tau_agent_core.session_log import InMemorySessionLog
from tau_coding_agent.backends import TauBackend


def _backend(cursor: Cursor) -> TauBackend:
    """A real TauBackend bound to ``cursor`` — ``elide_span`` makes no model call."""
    backend = TauBackend(
        {
            "model": "gpt-4o",
            "backend": "openai",
            "api_key": "test-key",
            "base_url": "https://api.openai.com/v1",
            "tools": [],
        }
    )
    backend.bind_cursor(cursor)
    return backend


def _cursor() -> Cursor:
    return Cursor.newest(InMemorySessionLog())


def _um(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _anchor_of(cursor: Cursor) -> dict[str, Any]:
    return next(e for e in cursor.entries() if e["type"] == "elide")


async def test_elide_records_the_span_the_fold_actually_loses() -> None:
    """``coveredEntries`` is checkable against the tree; ``coveredTokens`` is the
    figure §8.2 names as the one nothing can recompute afterwards, so the only
    guard on it is that it is a positive measurement of a non-empty span."""
    cursor = _cursor()
    ids = [await cursor.append_message(_um(f"turn {i}")) for i in range(5)]

    before = len(cursor.tree().context_entries(ids[4]))
    await _backend(cursor).elide_span(ids[4], ids[2])
    after = len(cursor.tree().context_entries())

    anchor = _anchor_of(cursor)
    # `after` counts the elide node itself, which did not exist in `before`.
    assert anchor["coveredEntries"] == before - (after - 1)
    assert anchor["coveredEntries"] == 2  # turns 0 and 1
    assert anchor["coveredTokens"] > 0


async def test_elide_records_the_frame_in_force_at_the_anchor_not_the_newest_one() -> None:
    """§8.3. A browser-driven elide aims at an arbitrary historical anchor, and the
    frame that governed the span it folds is the one on THAT anchor's ancestor
    chain. Recording "whatever spec the session most recently wrote" would label
    every historical fold with the current model."""
    cursor = _cursor()
    old_spec = await cursor.append_custom_entry("agent_spec", {"model": {"id": "the old model"}})
    await cursor.append_message(_um("turn 0"))
    keep = await cursor.append_message(_um("turn 1"))
    anchor = await cursor.append_message(_um("turn 2"))

    await cursor.append_custom_entry("agent_spec", {"model": {"id": "the new model"}})
    await cursor.append_message(_um("turn 3"))

    await _backend(cursor).elide_span(anchor, keep)

    assert _anchor_of(cursor)["configId"] == old_spec


async def test_elide_records_no_frame_when_the_path_has_none() -> None:
    """An honest ``None``, not a fabricated id: a log written without an
    ``AgentSession`` (or imported from pi) has no ``agent_spec`` node at all. §11.3
    keeps this distinguishable from a caller that never looked, by giving the
    parameter no default."""
    cursor = _cursor()
    ids = [await cursor.append_message(_um(f"turn {i}")) for i in range(3)]

    await _backend(cursor).elide_span(ids[2], ids[1])

    assert _anchor_of(cursor)["configId"] is None


async def test_elide_provenance_does_not_change_what_the_fold_returns() -> None:
    """§8 called the change additive on the payload. The elide still renders
    nothing and still splices exactly the same span."""
    cursor = _cursor()
    ids = [await cursor.append_message(_um(f"turn {i}")) for i in range(4)]

    messages = await _backend(cursor).elide_span(ids[3], ids[2])

    texts = [b["text"] for m in messages for b in m["content"]]
    assert texts == ["turn 2", "turn 3"]
