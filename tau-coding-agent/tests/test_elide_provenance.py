"""§8.2/§8.3 provenance on the elide the TUI creates (TREE-BROWSER-AS-EDITOR.md).

``TauBackend.elide_span`` is the only non-test caller of ``append_elide``, and until
§8 it recorded nothing but ``firstKeptId`` — less than a compaction, which at least
carried ``tokensBefore``. It had the span in hand the whole time: ``hidden`` is
computed a few lines earlier for the "this elide would hide nothing" refusal and was
then discarded, which is §8.1's pattern verbatim.

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

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import InMemorySessionLog
from tau_coding_agent.backends import TauBackend


def _backend() -> TauBackend:
    """A real TauBackend — ``elide_span`` makes no model call, so no network."""
    return TauBackend(
        {
            "model": "gpt-4o",
            "backend": "openai",
            "api_key": "test-key",
            "base_url": "https://api.openai.com/v1",
            "tools": [],
        }
    )


def _um(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _anchor_of(log: InMemorySessionLog) -> dict[str, Any]:
    return next(e for e in log.entries() if e["type"] == "elide")


def test_elide_records_the_span_the_fold_actually_loses() -> None:
    """``coveredEntries`` is checkable against the tree; ``coveredTokens`` is the
    figure §8.2 names as the one nothing can recompute afterwards, so the only
    guard on it is that it is a positive measurement of a non-empty span."""
    log = InMemorySessionLog()
    ids = [log.append_message(_um(f"turn {i}")) for i in range(5)]

    before = len(ConversationTree(log.entries(), log.cursor).context_entries(ids[4]))
    _backend().elide_span(log, ids[4], ids[2])
    after = len(ConversationTree(log.entries(), log.cursor).context_entries())

    anchor = _anchor_of(log)
    # `after` counts the elide node itself, which did not exist in `before`.
    assert anchor["coveredEntries"] == before - (after - 1)
    assert anchor["coveredEntries"] == 2  # turns 0 and 1
    assert anchor["coveredTokens"] > 0


def test_elide_records_the_frame_in_force_at_the_anchor_not_the_newest_one() -> None:
    """§8.3. A browser-driven elide aims at an arbitrary historical anchor, and the
    frame that governed the span it folds is the one on THAT anchor's ancestor
    chain. Recording "whatever spec the session most recently wrote" would label
    every historical fold with the current model."""
    log = InMemorySessionLog()
    old_spec = log.append_custom_entry("agent_spec", {"model": {"id": "the old model"}})
    log.append_message(_um("turn 0"))
    keep = log.append_message(_um("turn 1"))
    anchor = log.append_message(_um("turn 2"))

    # The session moved on afterwards: a set_model wrote a second spec, which is the
    # most recent one in the log and governs nothing at `anchor`.
    log.append_custom_entry("agent_spec", {"model": {"id": "the new model"}})
    log.append_message(_um("turn 3"))

    _backend().elide_span(log, anchor, keep)

    assert _anchor_of(log)["agentSpecId"] == old_spec


def test_elide_records_no_frame_when_the_path_has_none() -> None:
    """An honest ``None``, not a fabricated id: a log written without an
    ``AgentSession`` (or imported from pi) has no ``agent_spec`` node at all. §11.3
    keeps this distinguishable from a caller that never looked, by giving the
    parameter no default."""
    log = InMemorySessionLog()
    ids = [log.append_message(_um(f"turn {i}")) for i in range(3)]

    _backend().elide_span(log, ids[2], ids[1])

    assert _anchor_of(log)["agentSpecId"] is None


def test_elide_provenance_does_not_change_what_the_fold_returns() -> None:
    """§8 called the change additive on the payload. The elide still renders
    nothing and still splices exactly the same span."""
    log = InMemorySessionLog()
    ids = [log.append_message(_um(f"turn {i}")) for i in range(4)]

    messages = _backend().elide_span(log, ids[3], ids[2])

    texts = [b["text"] for m in messages for b in m["content"]]
    assert texts == ["turn 2", "turn 3"]
