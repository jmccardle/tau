"""A position a conversation tree is extended from (docs/CURSORS.md).

A :class:`~tau_agent_core.session_log.SessionLog` stores entries and nothing else.
Every writer holds a :class:`Cursor`: the entry its next append is parented to,
plus the typed appenders that write there and move it. A head, ``tau -p``, an
RPC client and a sub-agent each hold one; none of them is privileged.
"""

from __future__ import annotations

import uuid
from typing import Any

from tau_llm.docs import agent_facing

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import SessionLog, default_leaf

__all__ = ["Cursor"]


def _require_entry(log: SessionLog, entry_id: str | None, what: str) -> None:
    """Raise unless ``entry_id`` is ``None`` (before the root) or names an entry of ``log``."""
    if entry_id is not None and entry_id not in {str(e["id"]) for e in log.entries()}:
        raise ValueError(f"{what} {entry_id!r} not found")


@agent_facing(topic="sessions")
class Cursor:
    """A position in one tree, and the only thing that appends at a position.

    Not durable: moving a cursor writes nothing, and a process exit ends it. The
    position a reopened tree starts from is :func:`default_leaf` (§4).

    Attributes:
        id: Runtime identity, unique per cursor; never written to an entry.
        log: The tree's storage, shared with every other cursor on it.
        owner: The cursor that opened this one (a sub-agent's spawner), or ``None``.
        label: What the cursor was opened to do, for display.
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

        Raises:
            ValueError: ``leaf`` names no entry of ``log``.
        """
        _require_entry(log, leaf, "cursor leaf")
        self.id = uuid.uuid4().hex[:8]
        self.log = log
        self.owner = owner
        self.label = label
        self._leaf = leaf

    @classmethod
    def newest(cls, log: SessionLog, *, owner: Cursor | None = None, label: str = "") -> Cursor:
        """A cursor at ``log``'s default leaf: where a reopened tree continues."""
        return cls(log, default_leaf(log.entries()), owner=owner, label=label)

    @property
    def leaf(self) -> str | None:
        """The entry the next append is parented to; ``None`` before the root."""
        return self._leaf

    @property
    def session_id(self) -> str:
        """The id of the tree this cursor extends."""
        return self.log.id

    def entries(self) -> list[dict[str, Any]]:
        """Every entry of the tree, all branches — not only this cursor's path."""
        return self.log.entries()

    def tree(self) -> ConversationTree:
        """The tree folded at this cursor's leaf."""
        return ConversationTree(self.log.entries(), self._leaf)

    def context(self) -> list[dict[str, Any]]:
        """What the model receives from this position (``ConversationTree.context_for``)."""
        return self.tree().context_for()

    def move(self, target: str | None) -> None:
        """Move to ``target``; writes nothing.

        Raises:
            ValueError: ``target`` names no entry.
        """
        _require_entry(self.log, target, "move target")
        self._leaf = target

    async def append(self, entry_type: str, **payload: Any) -> str:
        """Write an entry at the leaf, move onto it, and return its id."""
        entry_id = await self.log.append_at(self._leaf, entry_type, payload)
        self._leaf = entry_id
        return entry_id

    async def append_message(self, message: dict[str, Any]) -> str:
        """Append a ``message`` entry."""
        return await self.append("message", message=message)

    async def append_custom_message(self, message: dict[str, Any], custom_type: str) -> str:
        """Append a ``customMessage``: extension content that does reach the model."""
        return await self.append("customMessage", customType=custom_type, message=message)

    async def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        """Append a ``customEntry``: durable data the model never sees."""
        return await self.append("customEntry", customType=custom_type, data=data)

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
        agent_spec_id: str | None,
    ) -> str:
        """Append a compaction splice anchor and its provenance.

        The provenance keywords have no defaults (TREE-BROWSER-AS-EDITOR.md §8,
        §11.3): every value exists at the call site, so a caller that cannot name one
        fails there rather than recording ``None``.

        Raises:
            ValueError: ``first_kept_id`` names no entry. The fold never finds an
                unknown anchor and would drop the whole kept region silently.
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
            agentSpecId=agent_spec_id,
        )

    async def append_elide(
        self,
        first_kept_id: str,
        *,
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        """Append a summary-less splice anchor (NODE-ADDRESSABLE-AGENTS.md W3).

        Raises:
            ValueError: ``first_kept_id`` names no entry, for the reason
                :meth:`append_compaction` gives.
        """
        _require_entry(self.log, first_kept_id, "elide first_kept_id")
        return await self.append(
            "elide",
            firstKeptId=first_kept_id,
            coveredEntries=covered_entries,
            coveredTokens=covered_tokens,
            agentSpecId=agent_spec_id,
        )

    async def append_branch_summary(self, summary: str, from_id: str | None) -> str:
        """Move to the branch point ``from_id``, then append a ``branch_summary`` there.

        Parenting at the branch point leaves the summarized children as a sibling
        branch, so they drop out of the context by ancestry alone.

        Raises:
            ValueError: ``from_id`` names no entry.
        """
        self.move(from_id)
        return await self.append("branch_summary", summary=summary, fromId=from_id)
