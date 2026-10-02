"""The storage seam every conversation tree is written through (docs/CURSORS.md).

A :class:`SessionLog` stores entries and keeps no position. Who appends where is a
:class:`~tau_agent_core.cursor.Cursor`'s job, so a store implements three members
and every store agrees on the entry algebra defined here: :func:`default_leaf`,
:func:`agent_spec_in_force`, :func:`event_iso`, :func:`normalize_loaded_entries`.

Implementations: :class:`InMemorySessionLog` (below), the file store
``tau_coding_agent.session_store.Session``, and the JMFTS store. Each is checked by
``tau_agent_core.testing.session_log_contract``.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable
from tau_llm.docs import agent_facing


@agent_facing(topic="sessions")
@runtime_checkable
class SessionLog(Protocol):
    """Append-only entry storage for one conversation tree.

    ``append_at`` is a coroutine and the two reads are not
    (docs/BLOCKING-PERSISTENCE.md): a store that does network I/O awaits it, and
    the reads are in memory in every shipped store.

    **Several cursors may append concurrently within one process.** A store must
    keep every concurrent ``append_at`` whole and give each a distinct id; the
    contract suite checks this. Two processes appending to one conversation is
    out of scope (docs/CURSORS.md §3).
    """

    @property
    def id(self) -> str:
        """Stable session identity (a UUID — never a filesystem path)."""
        ...

    def entries(self) -> list[dict[str, Any]]:
        """Every entry of every branch, in append order; a copy the caller may mutate."""
        ...

    async def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Write one entry parented at ``parent_id`` and return its new id.

        The entry's ``timestamp`` is :func:`event_iso` of ``payload``.

        Raises:
            ValueError: ``parent_id`` is not ``None`` and names no entry.
        """
        ...


@agent_facing(topic="sessions")
def default_leaf(entries: list[dict[str, Any]]) -> str | None:
    """Where a reopened tree continues: the newest entry a cursor wrote.

    Skipped: a legacy ``navigate``, which names a different position
    (docs/CURSORS.md §1.1, §4), and a namespaced kind (``system:kind``), which a
    store synthesizes for a document another system put in the tree — the JMFTS
    store's ``jmfts:document``. τ's own entry kinds are bare words.

    Args:
        entries: A log's entries in append order.

    Returns:
        The entry id, or ``None`` for a log with no such entry.
    """
    for entry in reversed(entries):
        kind = str(entry.get("type", ""))
        if kind != "navigate" and ":" not in kind:
            return str(entry["id"])
    return None


@agent_facing(topic="sessions")
def session_name(entries: list[dict[str, Any]]) -> str | None:
    """The newest ``session_info`` name in append order, or ``None`` if never named.

    Append order, not ancestry: a name belongs to the session, not to a position
    in it, so renaming from any branch renames the whole session.
    """
    for entry in reversed(entries):
        if entry.get("type") == "session_info":
            value = entry.get("name")
            return str(value) if value else None
    return None


@agent_facing(topic="sessions")
def agent_spec_in_force(entries: list[dict[str, Any]], leaf_id: str | None) -> str | None:
    """The id of the ``agent_spec`` record governing ``leaf_id``, or ``None``.

    What a splice anchor's ``agentSpecId`` must be set to
    (TREE-BROWSER-AS-EDITOR.md §8.3): the nearest ``agent_spec`` ``customEntry``
    among ``leaf_id``'s ANCESTORS, walking ``parentId`` leaf→root.

    Ancestry, not "the last one this session wrote", for the same reason
    ``ConversationTree._previous_agent_spec`` uses it and docs/LANE-REMOVAL.md §1
    removed the tag that pretended otherwise: a leaf's frame is its ancestor chain
    and nothing else. The two answers diverge exactly where it matters — an elide
    the tree browser aims at a historical anchor is governed by whatever spec was in
    force *there*, which may be two ``set_model`` swaps behind the session's current
    one, and a spec written on a sibling branch never governed this path at all.

    Lives here beside :func:`default_leaf`, and for the same reason: it is part of
    the entry algebra every ``SessionLog`` implementation must agree on exactly, not
    a property of any one durability layer. Implemented as a plain ``parentId`` walk
    rather than through ``ConversationTree`` so this module keeps its zero-dependency
    position under the tree, and so the two callers (one in ``tau-agent-core``, one in
    ``tau-coding-agent``) share one spelling.

    Returns ``None`` when no ancestor is an ``agent_spec`` — an honest answer, and a
    reachable one: a pi-imported log has no such node, and neither does a store
    driven directly rather than through ``AgentSession``. §11.3's "no defaults" rule
    is what keeps that answer distinct from a caller who never looked.
    """
    by_id = {str(e["id"]): e for e in entries if e.get("id") is not None}
    current = leaf_id
    visited: set[str] = set()
    while current is not None and current not in visited:
        visited.add(current)  # cycle guard, mirroring ConversationTree._walk
        node = by_id.get(current)
        if node is None:
            return None
        if node.get("type") == "customEntry" and node.get("customType") == "agent_spec":
            return current
        parent = node.get("parentId")
        current = str(parent) if parent is not None else None
    return None


DURABLE_LOCATION_ATTRS: tuple[str, ...] = ("path", "root_doc_id")
"""The attribute names a store declares to say WHERE a session is written.

``path`` is the file store's; ``root_doc_id`` is the JMFTS store's. A log that
declares neither, such as :class:`InMemorySessionLog`, is not addressable.
"""


def declared_durable_locations(log: object) -> dict[str, Any]:
    """Which of :data:`DURABLE_LOCATION_ATTRS` ``log`` declares, and to what.

    One implementation, two callers with different jobs.
    :func:`session_log_is_addressable` needs the yes/no;
    ``tau_agent_core.rpc.commands.require_durable_session`` needs to tell "declares
    nothing" from "declares ``None``", because those get different refusal messages.

    Args:
        log: Any object; the check is structural, never an isinstance test.

    Returns:
        A mapping of each declared attribute name to its value. Empty when the log
        declares no durable location at all.
    """
    return {name: getattr(log, name) for name in DURABLE_LOCATION_ATTRS if hasattr(log, name)}


@agent_facing(topic="sessions")
def session_log_is_addressable(log: object) -> bool:
    """Whether a later ``switch_session`` could reach the session ``log`` holds.

    A session is addressable exactly when it declares a durable location and that
    location is set, because that is also what puts it in the store's listing:
    ``SessionCatalog.list`` is what the RPC ``list_sessions`` verb returns and what
    ``resolve_ref`` resolves against. So this is not an opinion — it is
    "``list_sessions`` will return this id", answerable at the moment the id is
    minted.

    An ephemeral session (``create_ephemeral`` — the file store's ``path``-less
    ``Session``, the JMFTS store's ``_EphemeralConversationSession``) declares
    neither attribute and is therefore ``False``.

    A predicate rather than a raise: a caller that was ASKED for an unpersisted
    session made the right one. What it must not do is describe it as addressable.
    The raising form is ``rpc.commands.require_durable_session``, which asks this
    same question of the verbs that append (docs/RPC-PROTOCOL.md, D-7 rule 1).

    Args:
        log: The session log to ask about.

    Returns:
        Whether the session is one the store can hand back later.
    """
    declared = declared_durable_locations(log)
    return bool(declared) and any(value is not None for value in declared.values())


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string with ms precision + ``Z``.

    Mirrors ``session_store._now_iso`` so in-memory and on-disk entries carry an
    identically-shaped ``timestamp`` (JS ``new Date().toISOString()``)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _iso_from_epoch_ms(epoch_ms: int) -> str:
    """Epoch milliseconds in :func:`_now_iso`'s exact format.

    The same format matters rather than merely a valid one: entry ``timestamp``
    is the sibling sort key in ``ConversationTree.children_of``, which compares
    the strings, so a differently-shaped ISO string would sort against its
    neighbours by punctuation.
    """
    moment = datetime.fromtimestamp(epoch_ms / 1000, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


@agent_facing(topic="sessions")
def event_iso(payload: dict[str, Any], now: Callable[[], str]) -> str:
    """The event time of an entry payload, or now when it carries no clock.

    A ``message``/``customMessage`` payload holds the message dict, whose
    ``timestamp`` is epoch ms set where the event happened — the user's send, the
    tool result's collection, the completion's end. Anything else (a navigate, a
    compaction, a system message) has no event of its own and takes the write
    time, which for those is the same moment. All three ``SessionLog``
    implementations call this from ``append_at``, so the same session reads the
    same whichever one wrote it (docs/MESSAGE-TIMESTAMPS.md §4).

    Args:
        payload: The entry payload about to be written.
        now: The CALLER's write-time clock, passed rather than imported so each
            store keeps its own ``_now_iso`` as the one thing a test can
            substitute.

    Returns:
        An ISO-8601 UTC string in ``_now_iso``'s exact format.
    """
    message = payload.get("message")
    if isinstance(message, dict):
        stamp = message.get("timestamp")
        if isinstance(stamp, int) and not isinstance(stamp, bool):
            return _iso_from_epoch_ms(stamp)
    return now()


@agent_facing(topic="sessions")
def normalize_loaded_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map legacy ``timestamp: 0`` on a loaded assistant message to ``None``.

    The ONE place a 0 is interpreted. τ did not run in 1970, so a 0 in a stored
    message is the fabricated value ``openai-completions`` wrote before
    docs/MESSAGE-TIMESTAMPS.md; everything downstream reads ``None`` and never
    tests for 0. Every session store calls this on its load path, so a session
    reads the same whichever store it came from.

    Mutates the message dicts in place and returns the same list, because a store
    loading tens of thousands of entries should not copy them all to fix a field
    that is usually already absent.

    Args:
        entries: Session-log entries as read from storage.

    Returns:
        The same list, with legacy assistant zeros replaced by ``None``.
    """
    for entry in entries:
        message = entry.get("message")
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and message.get("timestamp") == 0
        ):
            message["timestamp"] = None
    return entries


def _generate_entry_id(existing: set[str]) -> str:
    """8-hex collision-checked entry id (mirrors ``session_store._generate_entry_id``)."""
    for _ in range(100):
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate
    return uuid.uuid4().hex  # pragma: no cover — 100 collisions is astronomically unlikely


@agent_facing(topic="sessions")
class InMemorySessionLog:
    """A RAM-only :class:`SessionLog`: the SDK default and the test double.

    Entries are byte-shaped like the file store's (camelCase ``parentId``,
    8-hex ids), so :class:`~tau_agent_core.conversation_tree.ConversationTree`
    folds both the same way. A fresh log has no entries.
    """

    def __init__(self, id: str | None = None) -> None:
        self._id = id if id is not None else uuid.uuid4().hex
        self._entries: list[dict[str, Any]] = []
        self._ids: set[str] = set()

    @property
    def id(self) -> str:
        return self._id

    def entries(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._entries)

    async def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        return self.append_at_now(parent_id, entry_type, payload)

    def append_at_now(self, parent_id: str | None, entry_type: str, payload: dict[str, Any]) -> str:
        """:meth:`append_at` without the coroutine, for a caller with no event loop.

        A catalog's synchronous ``create`` writes a new session's opening chain
        through this; nothing else should need it.
        """
        if parent_id is not None and parent_id not in self._ids:
            raise ValueError(f"append parent {parent_id!r} not found")
        entry: dict[str, Any] = {
            "type": entry_type,
            "id": _generate_entry_id(self._ids),
            "parentId": parent_id,
            "timestamp": event_iso(payload, _now_iso),
            **payload,
        }
        self._entries.append(entry)
        self._ids.add(entry["id"])
        return str(entry["id"])
