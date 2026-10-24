"""A position a conversation tree is extended from (docs/CURSORS.md).

A :class:`~tau_agent_core.session_log.SessionLog` stores entries and nothing else.
Every writer holds a :class:`Cursor`: the entry its next append is parented to,
the typed appenders that write there and move it, and the state of the turn that
is extending it (§2). A head, ``tau -p``, an RPC client and a sub-agent each hold
one; none of them is privileged.
"""

from __future__ import annotations

import asyncio
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from tau_llm.abort import AbortSignal
from tau_llm.docs import agent_facing

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import (
    CONFIG_ENTRY_TYPE,
    CONFIG_KEYS,
    AGENT_SOURCE,
    HUMAN_SOURCES,
    INCOMPLETE,
    SIDE_KINDS,
    SessionLog,
    default_leaf,
    is_incomplete,
    is_side_entry,
)

__all__ = ["Cursor", "TURN_CURSOR", "TurnFrame"]


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class TurnFrame:
    """How a cursor's turns differ from its session's (docs/CURSORS.md §6).

    A sub-agent's cursor carries one; the head's carries none and runs as the
    session is configured. The frame is recorded in the tree like any other
    config the first time the cursor's turn appends.

    Attributes:
        tools: Names the turn may call, a subset of the session's; required, so a
            sub-agent never inherits ``write`` and ``bash`` by accident. ``()`` is none.
        model: The model to run, or ``None`` for the session's.
        api_key: ``model``'s key, ``None`` for the provider's environment
            variable. Ignored when ``model`` is ``None``. Never recorded.
        model_name: The config name ``model`` was resolved from, recorded as the
            ``model`` config key; ``None`` when ``model`` was handed in directly.
        reasoning: The thinking level to request, or ``None`` for the session's.
        system_prompt: The prompt to run under, or ``None`` for the session's.
        max_turns: The loop's turn ceiling for this cursor; ``None`` is no ceiling.
        hooks: Whether extension hooks run on this cursor's turns. Off by default:
            a sub-agent stays as constrained as its spawner asked.
    """

    tools: tuple[str, ...]
    model: Any = None
    api_key: str | None = None
    model_name: str | None = None
    reasoning: str | None = None
    system_prompt: str | None = None
    max_turns: int | None = None
    hooks: bool = False


def _require_entry(log: SessionLog, entry_id: str | None, what: str) -> bool:
    """Raise unless ``entry_id`` is ``None`` (before the root) or names a path entry of ``log``.

    A side entry is refused: nothing is ever parented on one (docs/STEP-DEBUGGER.md §3.7.2).

    Returns:
        Whether the entry is incomplete; ``False`` for ``None``.
    """
    if entry_id is None:
        return False
    for entry in log.entries():
        if str(entry["id"]) == entry_id:
            if is_side_entry(entry):
                raise ValueError(f"{what} {entry_id!r} is a side entry, never on a path")
            return is_incomplete(entry)
    raise ValueError(f"{what} {entry_id!r} not found")


@agent_facing(topic="sessions")
@dataclass(eq=False)
class EnqueuedWait:
    """An ``enqueue`` submission waiting for a cursor's turn lock.

    Compared by identity, so two waits with the same text stay distinct.

    Attributes:
        text: The submitted text, as typed; no input hook has run on it yet.
        dropped: Set by a user abort; the submission refuses once it gets the lock.
    """

    text: str
    dropped: bool = False


class Cursor:
    """A position in one tree, and the only thing that appends at a position.

    Not durable: moving a cursor writes nothing, and a process exit ends it. The
    position a reopened tree starts from is :func:`default_leaf` (§4).

    Attributes:
        id: Runtime identity, unique per cursor; never written to an entry.
        log: The tree's storage, shared with every other cursor on it.
        owner: The cursor that opened this one (a sub-agent's spawner), or ``None``.
        label: What the cursor was opened to do, for display.
        turn_lock: Held while a turn extends this cursor; one turn at a time here,
            any number across cursors.
        abort_signal: The running turn's signal; a fresh one per admitted turn.
        steer_queue: ``UserMessage``s the running loop delivers before its next call.
        follow_up_queue: Texts that re-enter the loop when the current prompt ends.
        enqueued: ``enqueue`` submissions waiting for :attr:`turn_lock`, in order.
        dropped_enqueued: Texts of waits a user abort dropped, not yet announced
            by ``follow_ups_dropped`` (docs/TURN-SETTINGS.md §5.1).
        next_turn_queue: Texts injected alongside the next prompt's user turn.
        deferred_ops: Compact/fork intents recorded mid-turn, applied at its tail.
        pre_turn_leaf: The leaf just before the running turn's user node (rollback's
            target).
        turn_token: Which admitted turn is running; a rollback checks it is unchanged.
        turn_task: The ``asyncio.Task`` running the turn, for the reentrancy guard.
        submission: The submission whose turn is running, for event provenance.
        is_streaming: Whether a turn is running here.
        last_usage: The newest completion's usage on this cursor's path.
        turn_writer: The running turn's writer, which a mid-turn compaction reads.
        in_flight_context: The running turn's context, for a mid-turn estimate.
        persistence_settled: Clear from a turn's ``agent_end`` until its messages
            are written (docs/ASYNC-SESSION-LOG.md §3.3).
        frame: The :class:`TurnFrame` this cursor's turns run under, or ``None``
            for the session's own.
    """

    def __init__(
        self,
        log: SessionLog,
        leaf: str | None,
        *,
        owner: Cursor | None = None,
        label: str = "",
    ) -> None:
        """Place a cursor at ``leaf``.

        Args:
            log: The tree's storage.
            leaf: The entry to sit on; ``None`` is before the root.
            owner: The cursor that opened this one, or ``None``.
            label: What the cursor was opened to do, for display.

        Raises:
            ValueError: ``leaf`` names no entry of ``log``, or a side entry.
        """
        self._leaf_incomplete = _require_entry(log, leaf, "cursor leaf")
        self.id = uuid.uuid4().hex[:8]
        self.log = log
        self.owner = owner
        self.label = label
        self._leaf = leaf
        self._open: dict[str, str | None] = {}
        self.turn_lock = asyncio.Lock()
        self.abort_signal = AbortSignal()
        self.steer_queue: list[Any] = []
        self.follow_up_queue: list[str] = []
        self.enqueued: list[EnqueuedWait] = []
        self.dropped_enqueued: list[str] = []
        self.next_turn_queue: list[str] = []
        self.deferred_ops: list[dict[str, Any]] = []
        self.pre_turn_leaf: str | None = None
        self.turn_token: int | None = None
        self.turn_task: asyncio.Task[Any] | None = None
        self.submission: Any = None
        self.is_streaming = False
        self.last_usage: dict[str, Any] | None = None
        self.turn_writer: Any = None
        self.in_flight_context: list[dict[str, Any]] | None = None
        self.persistence_settled = asyncio.Event()
        self.persistence_settled.set()
        self.frame: TurnFrame | None = None

    @classmethod
    def newest(cls, log: SessionLog, *, owner: Cursor | None = None, label: str = "") -> Cursor:
        """A cursor at ``log``'s default leaf: where a reopened tree continues.

        Args:
            log: The tree's storage.
            owner: The cursor that opened this one, or ``None``.
            label: What the cursor was opened to do, for display.

        Returns:
            A new cursor at :func:`default_leaf` of ``log``'s entries.
        """
        return cls(log, default_leaf(log.entries()), owner=owner, label=label)

    @property
    def leaf(self) -> str | None:
        """The entry the next append is parented to; ``None`` before the root."""
        return self._leaf

    @property
    def busy(self) -> bool:
        """Whether a turn holds this cursor."""
        return self.turn_lock.locked()

    @property
    def has_queued(self) -> bool:
        """Whether a steer, follow-up or next-turn message waits here."""
        return bool(self.steer_queue or self.follow_up_queue or self.next_turn_queue)

    @property
    def session_id(self) -> str:
        """The id of the tree this cursor extends."""
        return self.log.id

    def entries(self) -> list[dict[str, Any]]:
        """Every entry of the tree, all branches — not only this cursor's path.

        Returns:
            The log's entries, in storage order.
        """
        return self.log.entries()

    def tree(self) -> ConversationTree:
        """The tree folded at this cursor's leaf.

        Returns:
            A fresh :class:`ConversationTree` over the log's current entries.
        """
        return ConversationTree(self.log.entries(), self._leaf)

    def context(self) -> list[dict[str, Any]]:
        """What the model receives from this position (``ConversationTree.context_for``).

        Returns:
            The context messages folded from this cursor's path.
        """
        return self.tree().context_for()

    def move(self, target: str | None) -> None:
        """Move to ``target``; writes nothing.

        Args:
            target: The entry to sit on; ``None`` is before the root.

        Raises:
            ValueError: ``target`` names no entry, or a side entry.
        """
        self._leaf_incomplete = _require_entry(self.log, target, "move target")
        self._leaf = target

    async def append(self, entry_type: str, **payload: Any) -> str:
        """Write an entry at the leaf and move onto it.

        Args:
            entry_type: The entry's ``type``.
            payload: The entry's fields, written as given.

        Returns:
            The new entry's id.

        Raises:
            ValueError: the leaf is an incomplete entry; nothing grows from one.
        """
        self._refuse_incomplete_leaf()
        entry_id = await self.log.append_at(self._leaf, entry_type, self._stamped(payload))
        self._leaf = entry_id
        self._leaf_incomplete = False
        return entry_id

    async def open(self, entry_type: str, **payload: Any) -> str:
        """Write an incomplete entry at the leaf, and stay where it was (docs/TAU-SERVE.md §4).

        The cursor moves onto the entry when :meth:`finalize` completes it, so
        the leaf never names an incomplete entry and a read of :meth:`context`
        mid-message still folds.

        Args:
            entry_type: The entry's ``type``.
            payload: The entry's fields so far; ``status`` is set to incomplete.

        Returns:
            The incomplete entry's id, to pass to :meth:`finalize`.

        Raises:
            ValueError: the leaf is an incomplete entry.
        """
        self._refuse_incomplete_leaf()
        entry_id = await self.log.append_at(
            self._leaf, entry_type, self._stamped({**payload, "status": INCOMPLETE})
        )
        self._open[entry_id] = self._leaf
        return entry_id

    async def finalize(
        self, entry_id: str, *, ext: dict[str, Any] | None = None, **payload: Any
    ) -> None:
        """Complete the entry :meth:`open` wrote, and move onto it.

        Args:
            entry_id: The id :meth:`open` returned.
            ext: Extension annotations, written beside ``message`` when non-empty
                (docs/ENTRY-ANNOTATIONS.md §2).
            payload: The entry's whole final payload; it replaces the incomplete one.

        Raises:
            ValueError: this cursor did not open ``entry_id``, or the leaf moved
                while it was open, so the entry no longer extends this position.
        """
        if entry_id not in self._open:
            raise ValueError(f"finalize: entry {entry_id!r} was not opened by this cursor")
        if self._open[entry_id] != self._leaf:
            raise ValueError(
                f"finalize: entry {entry_id!r} hangs from {self._open[entry_id]!r}, not "
                f"this cursor's leaf {self._leaf!r}"
            )
        await self.log.finalize(
            entry_id, self._stamped({**payload, **({"ext": ext} if ext else {})})
        )
        del self._open[entry_id]
        self._leaf = entry_id
        self._leaf_incomplete = False

    async def open_side(self, kind: str, **payload: Any) -> str:
        """Write an incomplete side entry under the leaf, moving nothing (docs/STEP-DEBUGGER.md §3.7.1).

        Args:
            kind: A side kind (:data:`~tau_agent_core.session_log.SIDE_KINDS`).
            payload: The entry's fields so far; ``status`` is set to incomplete.

        Returns:
            The side entry's id, to pass to :meth:`finalize_side`.

        Raises:
            ValueError: ``kind`` is not a side kind, or the leaf is an incomplete entry.
        """
        if kind not in SIDE_KINDS:
            raise ValueError(
                f"open_side: {kind!r} is not a side kind ({', '.join(sorted(SIDE_KINDS))})"
            )
        self._refuse_incomplete_leaf()
        return await self.log.append_at(
            self._leaf, kind, self._stamped({**payload, "status": INCOMPLETE})
        )

    async def finalize_side(self, entry_id: str, **payload: Any) -> None:
        """Complete a side entry :meth:`open_side` wrote, moving nothing.

        Args:
            entry_id: The id :meth:`open_side` returned.
            payload: The entry's whole final payload; it replaces the incomplete one.

        Raises:
            ValueError: ``entry_id`` names no side entry or a finished one, or
                ``payload`` sets a ``status`` (``SessionLog.finalize``).
        """
        entry = next((e for e in self.log.entries() if str(e["id"]) == entry_id), None)
        if entry is None or not is_side_entry(entry):
            raise ValueError(f"finalize_side: {entry_id!r} names no side entry")
        await self.log.finalize(entry_id, self._stamped(payload))

    def _stamped(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``payload`` with ``source: "agent"`` when a child cursor writes it, unless a person's turn does.

        A child cursor has an :attr:`owner`. The head's writes, and a turn whose
        submission source is in :data:`~tau_agent_core.session_log.HUMAN_SOURCES`,
        are returned unchanged, so their entries stay byte-identical;
        ``session_log.default_leaf`` reads the field.
        """
        if self.owner is None:
            return payload
        submission = self.submission
        if submission is not None and submission.source in HUMAN_SOURCES:
            return payload
        return {**payload, "source": AGENT_SOURCE}

    def _refuse_incomplete_leaf(self) -> None:
        """Raise when the leaf is an interrupted entry, which a cursor never extends."""
        if self._leaf_incomplete:
            raise ValueError(
                f"cursor leaf {self._leaf!r} is an incomplete entry; move to a finished "
                "one before appending"
            )

    async def append_message(
        self, message: dict[str, Any], *, ext: dict[str, Any] | None = None
    ) -> str:
        """Append a ``message`` entry.

        Args:
            message: The message dict, stored under ``message``.
            ext: Extension annotations, written beside ``message`` when non-empty.

        Returns:
            The new entry's id.
        """
        return await self.append("message", message=message, **({"ext": ext} if ext else {}))

    async def append_custom_message(
        self, message: dict[str, Any], custom_type: str, *, ext: dict[str, Any] | None = None
    ) -> str:
        """Append a ``customMessage``: extension content that does reach the model.

        Args:
            message: The message dict the model receives.
            custom_type: The extension's type tag, stored as ``customType``.
            ext: Extension annotations, written beside ``message`` when non-empty.

        Returns:
            The new entry's id.
        """
        extra = {"ext": ext} if ext else {}
        return await self.append("customMessage", customType=custom_type, message=message, **extra)

    async def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        """Append a ``customEntry``: durable data the model never sees.

        Args:
            custom_type: The extension's type tag, stored as ``customType``.
            data: The data to store.

        Returns:
            The new entry's id.
        """
        return await self.append("customEntry", customType=custom_type, data=data)

    async def append_config(self, **keys: Any) -> str:
        """Append a config entry setting ``keys`` from here on (docs/CURSORS.md §5).

        Args:
            keys: The config values to set; unnamed keys keep their prior value.

        Returns:
            The new entry's id.

        Raises:
            ValueError: no keys, or a key outside :data:`~tau_agent_core.session_log.CONFIG_KEYS`.
        """
        unknown = sorted(set(keys) - CONFIG_KEYS)
        if not keys or unknown:
            raise ValueError(
                f"append_config: unknown key(s) {unknown}" if unknown else "append_config: no keys"
            )
        return await self.append_custom_entry(CONFIG_ENTRY_TYPE, dict(keys))

    async def append_compaction(
        self,
        summary: str,
        first_kept_id: str,
        tokens_before: int,
        *,
        summarizer_model_id: str,
        summary_usage: dict[str, int],
        covered_entries: int,
        covered_tokens: int,
        config_id: str | None,
    ) -> str:
        """Append a compaction splice anchor and its provenance.

        The provenance keywords have no defaults (TREE-BROWSER-AS-EDITOR.md §8,
        §11.3): a caller that cannot name one fails there rather than recording ``None``.

        Args:
            summary: The summary text that stands in for the covered span.
            first_kept_id: The first path entry the fold keeps after the summary.
            tokens_before: Estimated context tokens of the path before compaction.
            summarizer_model_id: The id of the model that wrote ``summary``.
            summary_usage: The summarizer call's token usage.
            covered_entries: How many entries the summary replaces.
            covered_tokens: Estimated context tokens of those entries.
            config_id: The nearest config-setting entry on the anchor's path, or ``None``.

        Returns:
            The new entry's id.

        Raises:
            ValueError: ``first_kept_id`` names no entry (the fold would drop the
                whole kept region silently), or the leaf is an incomplete entry.
        """
        _require_entry(self.log, first_kept_id, "compaction first_kept_id")
        return await self.append(
            "compaction",
            summary=summary,
            firstKeptId=first_kept_id,
            tokensBefore=tokens_before,
            summarizerModelId=summarizer_model_id,
            summaryUsage=dict(summary_usage),
            coveredEntries=covered_entries,
            coveredTokens=covered_tokens,
            configId=config_id,
        )

    async def append_elide(
        self,
        first_kept_id: str,
        *,
        covered_entries: int,
        covered_tokens: int,
        config_id: str | None,
    ) -> str:
        """Append a summary-less splice anchor (NODE-ADDRESSABLE-AGENTS.md W3).

        Args:
            first_kept_id: The first path entry the fold keeps after the anchor.
            covered_entries: How many entries the anchor hides.
            covered_tokens: Estimated context tokens of those entries.
            config_id: The nearest config-setting entry on the anchor's path, or ``None``.

        Returns:
            The new entry's id.

        Raises:
            ValueError: ``first_kept_id`` names no entry, or the leaf is an
                incomplete entry, as for :meth:`append_compaction`.
        """
        _require_entry(self.log, first_kept_id, "elide first_kept_id")
        return await self.append(
            "elide",
            firstKeptId=first_kept_id,
            coveredEntries=covered_entries,
            coveredTokens=covered_tokens,
            configId=config_id,
        )

    async def append_branch_summary(self, summary: str, from_id: str | None) -> str:
        """Move to the branch point ``from_id``, then append a ``branch_summary`` there.

        Parenting at the branch point leaves the summarized children as a sibling
        branch, so they drop out of the context by ancestry alone.

        Args:
            summary: The summary of the abandoned branch.
            from_id: The branch point; ``None`` is before the root.

        Returns:
            The new entry's id.

        Raises:
            ValueError: ``from_id`` names no entry, or names an incomplete one;
                the cursor has already moved there when the second is raised.
        """
        self.move(from_id)
        return await self.append("branch_summary", summary=summary, fromId=from_id)


TURN_CURSOR: ContextVar[Cursor | None] = ContextVar("TURN_CURSOR", default=None)
"""The cursor the running turn extends, published by ``AgentSession.submit``.

A ContextVar for the reason ``DRIVING_SUBMISSION_DEPTH`` is one: it is causal, so a
hook, a tool and any task they start see the cursor of the turn that started them,
without that cursor being threaded through every call (docs/CURSORS.md §7).
"""
