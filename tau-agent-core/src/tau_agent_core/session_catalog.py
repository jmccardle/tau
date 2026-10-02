"""τ-agent-core session-catalog SEAM (W10): the storage-agnostic construction surface.

Three pieces, all storage-agnostic (``tau-agent-core`` owns zero file I/O today and
this module must not change that):

- :class:`ConversationSession` — a **derived** ``Protocol`` (``SessionLog`` plus the
  frontend surface the concrete file ``Session`` (``tau_coding_agent.session_store.Session``)
  already exposes: identity/config reads, the mutable transcript views, and the two
  config appenders ``AgentSession`` itself never calls (mirroring why
  :class:`~tau_agent_core.session_log.SessionLog` leaves them off — see that
  docstring). Kept OFF ``SessionLog`` itself so ``InMemorySessionLog`` and every SDK
  embedder is not forced to grow members ``AgentSession`` never touches.
- :class:`SessionInfo` — the picker's lightweight listing record, moved down from
  ``session_store`` so a catalog implementation can hand back listing metadata
  without a concrete ``Path``. ``ref`` is the storage-agnostic handle a catalog's
  ``load()`` accepts back: a filesystem path for the file store, a document id for a
  future JMFTS-backed one. Deliberately carries NO file-reading method — that stays
  in ``tau_coding_agent.session_store`` (``read_session_info``), the one place that
  is allowed to touch a JSONL file.
- :class:`SessionCatalog` — an ABC (not a ``Protocol``): one injected instance per
  run, so ``create``/``create_ephemeral``/``load``/``fork``/``list`` are the seam a
  second, non-file store slots into (proven by an in-memory test double in
  ``tau-agent-core``'s test tree — no product code for it yet). ``most_recent`` and
  ``resolve_ref`` are concrete, shared defaults built ONLY out of the five
  abstract methods, so every catalog gets them for free and agrees on the same
  "continue"/"REF" resolution semantics headless.py used to hand-roll.

Reference: SESSION-TREE-IMPLEMENTATION.md (the ``SessionLog`` seam this extends);
W10 (session-catalog seam) work-item notes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from tau_agent_core.session_log import SessionLog
from tau_llm.docs import agent_facing


@agent_facing(topic="sessions")
@runtime_checkable
class ConversationSession(SessionLog, Protocol):
    """A stored session as a catalog hands it to a head: storage plus listing reads.

    Kept off :class:`SessionLog` so the SDK's ``InMemorySessionLog`` need not grow
    members only a head calls. The views read at
    :func:`~tau_agent_core.session_log.default_leaf`, which is where a reopened
    session continues. A head reads position-dependent state from its own
    :class:`~tau_agent_core.cursor.Cursor`, never from these.

    ``@runtime_checkable`` checks member names only; behaviour is the contract
    suite's job (``tau_agent_core.testing.session_catalog_contract``).
    """

    @property
    def header(self) -> dict[str, Any]:
        """The line-1 header (raw, mutable copy per call)."""
        ...

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Unspliced ``message`` entries on the path to the default leaf."""
        ...

    @property
    def context(self) -> list[dict[str, Any]]:
        """The folded context at the default leaf."""
        ...

    @property
    def config(self) -> dict[str, Any]:
        """The folded config at the default leaf (``session_log.config_at``)."""
        ...

    @property
    def model(self) -> str:
        """The config's model name at the default leaf. Raises if there is none."""
        ...

    @property
    def backend(self) -> str:
        """The config's backend name at the default leaf. Raises if there is none."""
        ...

    def display_title(self) -> str:
        """A short human label: the name, else the first user message, else model."""
        ...


@agent_facing(topic="sessions")
@dataclass
class SessionInfo:
    """Lightweight listing metadata for one session — the picker's fast-list record.

    ``ref`` replaces what used to be a concrete ``Path`` (``tau_coding_agent``'s
    former ``session_store.SessionInfo.path``): a storage-agnostic handle that a
    :class:`SessionCatalog`'s ``load()`` accepts back to reconstruct the full
    session. For the file store it is ``str(path)``; a future JMFTS-backed catalog
    would put a document id here instead. This dataclass does no I/O of its own —
    building one from an on-disk file is ``tau_coding_agent.session_store``'s job
    (``read_session_info``), which is file-store-specific and stays there.
    """

    ref: str
    id: str
    cwd: str
    name: str | None
    created: datetime
    modified: datetime
    message_count: int
    first_message: str
    last_message: str
    parent: str | None
    error: str | None = None

    def display_title(self) -> str:
        if self.error:
            return f"⚠ unreadable session ({self.ref}) — {self.error}"
        if self.name:
            return self.name
        text = self.first_message.replace("\n", " ")
        if not text:
            return f"Session ({self.id[:8]})"
        return text[:50] + ("..." if len(text) > 50 else "")


@agent_facing(topic="sessions")
class SessionCatalog(ABC):
    """The injected, storage-agnostic seam for constructing/finding sessions.

    One instance per run (TUI or headless), replacing direct calls to the concrete
    file ``Session.create``/``create_in_memory``/``load``/``fork`` and
    ``list_sessions``. An ABC, not a ``Protocol``, because the two orchestration
    methods below (``most_recent``, ``resolve_ref``) have exactly one correct
    implementation — built purely out of the five abstract methods — that every
    catalog should share rather than reimplement.

    The five abstract methods are the storage-specific primitives a concrete
    catalog must supply:

    - ``create`` / ``create_ephemeral`` — new persisted / in-memory session.
    - ``load(ref)`` — reconstruct a session from a :class:`SessionInfo`'s ``ref``.
    - ``fork(source, cwd, at=)`` — a new session carrying ``source``'s history.
    - ``list(cwd)`` — newest-first listing metadata, ``cwd=None`` for every dir.
    """

    @abstractmethod
    def create(
        self,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> ConversationSession:
        """Create a new persisted session."""

    @abstractmethod
    def create_ephemeral(
        self,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> ConversationSession:
        """Create a new in-memory (unpersisted) session — ``--no-session``."""

    @abstractmethod
    def load(self, ref: str) -> ConversationSession:
        """Reconstruct a session from a :class:`SessionInfo`'s ``ref``.

        Raises when ``ref`` names nothing loadable (Fail-Early — never guess).
        """

    @abstractmethod
    def fork(
        self, source: ConversationSession, cwd: str, *, at: str | None = None
    ) -> ConversationSession:
        """A new session carrying ``source``'s history; ``source`` is untouched.

        ``at`` given → only the path to that entry, so the fork continues from it.
        ``None`` → every entry.

        Raises:
            ValueError: ``at`` names no entry of ``source``.
        """

    @abstractmethod
    def list(self, cwd: str | None = None) -> list[SessionInfo]:
        """Listing metadata, newest (by ``modified``) first.

        ``cwd`` given → that directory/scope only; ``None`` → every scope.
        """

    def most_recent(self, cwd: str | None = None) -> ConversationSession | None:
        """The most recently modified session in ``cwd``, loaded — or ``None``.

        Shared across every catalog: built purely from ``list`` + ``load``, so a
        second implementation gets "continue the last session" for free.
        """
        infos = self.list(cwd)
        if not infos:
            return None
        return self.load(infos[0].ref)

    def resolve_ref(self, ref: str, *, cwd: str | None = None) -> ConversationSession:
        """Resolve a ``--session``/``--fork`` REF to a loaded session, **by id**.

        Generalizes the file-store-era ``headless._resolve_session_ref``. The
        storage-agnostic half lives here: a REF is a session **id** — an exact match
        wins, else a unique id *prefix*. ``cwd`` scopes the search (``None`` searches
        every scope, mirroring the old ``all_sessions`` widening, which no CLI flag
        ever set, so the dead parameter is folded into ``cwd``). Zero or multiple
        matches raise ``LookupError`` (Fail-Early: never guess which session was
        meant).

        A catalog whose refs have their own *directly addressable* form — the file
        store's ``.jsonl`` path — OVERRIDES this to try that form first and then
        delegates back here via ``super()``. That knowledge is deliberately NOT in
        this base class: ``.jsonl`` and ``FileNotFoundError`` are filesystem
        concepts, and core owns zero I/O. A JMFTS catalog's document ids resolve
        through the id path below unchanged.
        """
        infos = self.list(cwd)
        exact = [i for i in infos if i.id == ref]
        matches = exact or [i for i in infos if i.id.startswith(ref)]
        if not matches:
            scope = "any directory" if cwd is None else "this directory"
            raise LookupError(f"no session matches {ref!r} (looked for a session id under {scope})")
        if len(matches) > 1:
            ids = ", ".join(sorted(i.id for i in matches))
            raise LookupError(f"{ref!r} matches multiple sessions ({ids}); be more specific")
        return self.load(matches[0].ref)
