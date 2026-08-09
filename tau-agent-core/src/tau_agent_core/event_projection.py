"""τ-agent-core event projection: cumulative message -> delta.

Reference: docs/REMOTE-CONTROL.md §4 block [4] "Event stream" (E1), §9 (R-T6).

``AgentEvent.message_update`` carries the WHOLE cumulative assistant message on
every emission (``events.py``, ``message: dict[str, Any] | None``) — the agent
loop re-sends the full accumulated ``partial_text`` (and ``partial_reasoning``)
on every provider chunk (``agent_loop.py:598-659``). In-process that is free to
consume; serialized onto a wire it is quadratic: a 32 KB reply becomes ~32K
lines averaging 16 KB (REMOTE-CONTROL.md E1). Any consumer that wants a bounded
per-chunk payload has to turn the cumulative snapshot back into a delta itself.

This module is that projection, extracted from the ad hoc suffix-diff that used
to live only in ``tau-coding-agent``'s ``TauBackend.stream_chat`` (`backends.py`,
``capture_event``'s ``message_update`` branch) so it can be reused by a
non-TUI consumer (a future RPC wire, §6 D3) without pulling in Textual.

Scope note: what is actually bounded per update is TEXT — REMOTE-CONTROL.md's
own consistency ledger scopes E1 as "copy pi's [text_delta] shape", and that is
what this module delivers for the ``text``/``thinking`` blocks. A non-diffable
block (today, only ``toolCall``) is passed through via :attr:`BlockDelta.block`
whenever it changes, whole, because the projector has no way to compute a
partial delta for an arbitrary block shape — and since the agent loop rebuilds
a tool call's ``arguments`` dict from scratch on every argument fragment, it
"changes" on every fragment. That means a non-diffable block's total projected
bytes across a turn is O(n²) in its final size, the exact wire cost E1 exists to
eliminate — it is just not eliminated for this block kind. This module does not
silently drop or fabricate a bound it doesn't have; a future consumer that
needs a bounded tool-call payload must either coalesce/throttle passthrough
deltas itself or extend the projector with a real incremental encoding for
``toolCall`` arguments, which needs the raw JSON fragment (not yet threaded
through ``AgentEvent.message`` — out of this module's scope, see D3/E1 above).

``MessageDeltaProjector`` is deliberately a small stateful object, not a free
function: projecting a delta requires remembering what was already emitted for
each block, and that memory is the whole point (a stateless function would need
the caller to pass the previous snapshot in on every call, which is just the
state moved to the wrong side of the interface).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BlockDelta:
    """One content block's projected change between two message snapshots.

    Attributes:
        index: The block's position in the message's ``content`` list AT THE
            TIME this delta was produced — a snapshot, not an identity.
            Positions are NOT stable across updates within one streamed
            message: e.g. a tool call streams alone at index 0 until any text
            starts accumulating, at which point ``_consolidate_text_and_thinking``
            (tau_ai openai.py) puts text ahead of it and the same call is now
            at index 1. Diffable blocks are tracked by ``type`` and
            non-diffable blocks by their ``id`` (see the class docstring) for
            exactly this reason — ``index`` on a :class:`BlockDelta` is
            informational for the caller, never used by the projector itself
            to recognize a block across calls. A fresh
            :class:`MessageDeltaProjector` (or a call to :meth:`reset`) starts
            a new message.
        type: The block's ``"type"`` discriminator (``"text"``, ``"thinking"``,
            or anything else a provider/tool produces, e.g. ``"toolCall"``).
        delta: For a diffable kind (``"text"``, ``"thinking"``) — see
            :attr:`replace` for how to interpret it. ``None`` for every other
            kind, where :attr:`block` carries the payload instead.
        replace: Only meaningful when ``delta`` is not ``None``. ``False``
            (the common case): ``delta`` is an incremental suffix — append it
            to whatever was already accumulated for this block's type.
            ``True``: the provider replaced rather than extended the block's
            content (it does not start with what was previously seen) — the
            defensive case τ's TUI backend has always special-cased. ``delta``
            is then the block's ENTIRE new value; the caller must RESET its
            local accumulator to ``delta`` rather than appending it.
        block: For a non-diffable kind, the block's full current payload — an
            independent copy, safe for the caller to hold onto even if the
            producer later mutates its own block dict (only produced when it
            changed since last seen for this block's identity; see the class
            docstring on how "changed" is decided). The projector does not
            know how to compute an incremental delta for an arbitrary block
            shape, so it surfaces the whole thing rather than silently
            dropping it — a consumer that has no use for it (today's TUI,
            which renders tool activity off
            ``tool_execution_start``/``_end`` instead) simply filters on
            ``type``. NOTE: "whole thing" is not bounded — a block that
            changes on every streamed fragment (e.g. tool-call arguments
            growing character by character) is re-emitted whole on every
            fragment, so the total bytes projected for that block across a
            turn is O(n²) in the block's final size, not O(n). Only the
            diffable kinds get an actual bounded-per-update delta; see the
            class docstring.
    """

    index: int
    type: str
    delta: str | None = None
    replace: bool = False
    block: dict[str, Any] | None = None


# Content-block types whose text grows incrementally and can therefore be
# suffix-diffed against what was last seen. Every other type is passed through
# whole (see BlockDelta.block) rather than diffed.
_DIFFABLE_FIELD: dict[str, str] = {"text": "text", "thinking": "thinking"}


class MessageDeltaProjector:
    """Turns a stream of cumulative assistant-message snapshots into deltas.

    One instance projects ONE streamed message. Call :meth:`project` with each
    successive cumulative ``message`` dict (the same shape carried on
    ``AgentEvent.message``); it returns only the blocks that actually changed,
    as incremental deltas where possible. Call :meth:`reset` at the start of a
    new streamed message (τ's agent loop re-uses the same accumulator across
    the whole turn, resetting only at ``turn_start`` — see
    ``tau_coding_agent.backends.TauBackend.stream_chat``, which is this
    projector's first caller and reset point).

    Two independent kinds of state are kept, and NEITHER is keyed by
    content-list position — position is a snapshot, not an identity (see
    :attr:`BlockDelta.index`):

    - **Diffable kinds** (``text``, ``thinking``) are tracked PER TYPE. The
      agent loop's per-kind streaming events each re-emit a single-block
      message whose block sits at index 0 regardless of how many blocks of
      the OTHER kind have already streamed this turn (a ``ThinkingDeltaEvent``
      message is ``[{"type": "thinking", ...}]``; a ``TextDeltaEvent`` message
      is ``[{"type": "text", ...}]``) — so a position-keyed accumulator would
      misattribute a delta the moment the two channels interleave (reasoning,
      then an answer). There is at most one active streaming instance of each
      diffable kind at a time; this mirrors the pre-extraction
      ``streaming_text`` / ``streaming_reasoning`` pair.
    - **Non-diffable kinds** (e.g. ``toolCall``) CAN appear more than once at
      once (parallel tool calls streaming their arguments concurrently), so
      position alone can't disambiguate them either — and unlike the diffable
      channels they don't even stay put: once any text starts accumulating, a
      tool call that streamed alone at index 0 is pushed to index 1 by
      ``_consolidate_text_and_thinking`` (tau_ai openai.py) putting
      ``[thinking?, text?, toolCall...]`` in that order. These are therefore
      tracked by the block's own ``id`` field when it has one (every
      ``tau_ai.types.ToolCall`` does — ``id: str`` is required, not optional),
      which survives both the position shift and disambiguates two distinct
      calls that land at the same index across snapshots. A block with no
      string ``id`` (there is no such kind in the agent loop's actual output
      today) falls back to position, which reintroduces the shift/conflation
      risk for that block only.
    """

    def __init__(self) -> None:
        self._diffable_seen: dict[str, str] = {}
        self._passthrough_seen: dict[str | int, tuple[str, dict[str, Any]]] = {}

    def reset(self) -> None:
        """Forget everything projected so far — start a fresh message."""
        self._diffable_seen.clear()
        self._passthrough_seen.clear()

    def project(self, message: dict[str, Any]) -> list[BlockDelta]:
        """Project one cumulative ``message`` snapshot into its new deltas.

        ``message`` is a dict shaped like ``AgentEvent.message``: a
        ``{"role": ..., "content": [...]}`` mapping, ``content`` a list of
        block dicts. Returns one :class:`BlockDelta` per block that actually
        changed since the last call (or since construction/``reset``), in
        ``content`` order — an unchanged block (the "no actual change" case:
        the same text re-sent, or an unchanged non-text block) contributes
        nothing, matching the pre-extraction behaviour exactly.
        """
        content = message.get("content")
        if not isinstance(content, list):
            return []

        deltas: list[BlockDelta] = []
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if not isinstance(block_type, str):
                # A block with no string "type" is not a well-formed content
                # block; skip it rather than guess (mirrors the pre-extraction
                # code, which never handled anything but "text"/"thinking" and
                # dropped the rest).
                continue

            field = _DIFFABLE_FIELD.get(block_type)
            delta_item: BlockDelta | None
            if field is not None:
                delta_item = self._project_diffable(index, block_type, block.get(field) or "")
            else:
                delta_item = self._project_passthrough(index, block_type, block)
            if delta_item is not None:
                deltas.append(delta_item)

        return deltas

    def _project_diffable(self, index: int, block_type: str, full: str) -> BlockDelta | None:
        if not full:
            return None  # nothing to diff against an empty/absent value

        prev_full = self._diffable_seen.get(block_type)
        if prev_full is not None and full.startswith(prev_full):
            delta = full[len(prev_full) :]
            replace = False
        else:
            # Either the first time this type is seen (delta == the whole
            # value, replace=False — ordinary growth from nothing) or the
            # provider replaced rather than extended (replace=True).
            delta = full
            replace = prev_full is not None

        if not delta:
            return None  # re-sent verbatim: no actual change

        self._diffable_seen[block_type] = full
        return BlockDelta(index=index, type=block_type, delta=delta, replace=replace)

    def _project_passthrough(
        self, index: int, block_type: str, block: dict[str, Any]
    ) -> BlockDelta | None:
        key = self._passthrough_key(index, block_type, block)
        prev = self._passthrough_seen.get(key)
        if prev is not None and prev[0] == block_type and prev[1] == block:
            return None  # unchanged
        # Snapshot rather than store the caller's own dict: the caller (and,
        # per this delta, the eventual wire consumer) must be free to hold
        # onto `block` after this call without a later in-place mutation by
        # the producer silently rewriting history — either "prev" here on the
        # next call, or the payload we hand back below.
        snapshot = copy.deepcopy(block)
        self._passthrough_seen[key] = (block_type, snapshot)
        return BlockDelta(index=index, type=block_type, block=snapshot)

    @staticmethod
    def _passthrough_key(index: int, block_type: str, block: dict[str, Any]) -> str | int:
        """Identity for a non-diffable block across snapshots.

        Prefer the block's own ``id`` (type-qualified, so e.g. a ``toolCall``
        and some other kind can never collide on the same id string) — it
        survives the position shift `_consolidate_text_and_thinking` causes
        and disambiguates two distinct calls sharing an index. Position is
        the fallback only for a block with no string ``id``.
        """
        block_id = block.get("id")
        if isinstance(block_id, str) and block_id:
            return f"{block_type}:{block_id}"
        return index
