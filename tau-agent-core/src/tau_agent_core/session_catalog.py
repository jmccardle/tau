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


@runtime_checkable
class ConversationSession(SessionLog, Protocol):
    """The frontend surface the concrete file ``Session`` already has, as a Protocol.

    A **derived** Protocol (``SessionLog`` plus more), not a widening of
    ``SessionLog`` itself: ``AgentSession`` never calls ``header``/``messages``/
    ``context``/``model``/``backend``/``display_title``/``append_model_change``/
    ``append_session_info`` (it only touches the members on ``SessionLog``), so
    keeping them off ``SessionLog`` avoids forcing ``InMemorySessionLog`` — the SDK's
    default, no-frontend log — to grow members it would never use. They live here
    instead because the TUI (``app.py``) and headless (``headless.py``) DO call them,
    through whatever :class:`SessionCatalog` handed them the session.

    That same rule is why this Protocol is SMALLER than the concrete file
    ``Session``. ``Session`` also has ``cwd``, ``name``, ``shutdown()`` and
    ``append_thinking_change()`` — all four have **zero callers** anywhere in
    ``src`` (``shutdown()`` in particular is shadowed by the unrelated
    ``AgentSession.emit_session_shutdown``, which is what the frontends actually
    call). Putting them here would force every future store — the JMFTS one next —
    to implement four members nobody invokes, which is precisely the cost
    ``SessionLog``'s docstring exists to avoid. Add a member here when a caller
    appears, not before.

    ``@runtime_checkable`` only verifies member NAMES are present (via
    ``isinstance``/``hasattr``), never signatures — an ``isinstance(x,
    ConversationSession)`` pass is not a contract pass. It does not check that
    ``display_title`` takes no arguments, that ``model`` raises rather than
    returning ``None``, or any other behavioural promise; that is what a contract
    test suite (in the spirit of the W5 ``SessionLog`` suite) is for, not this
    Protocol.
    """

    @property
    def header(self) -> dict[str, Any]:
        """The line-1 header (raw, mutable copy per call)."""
        ...

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Raw linear fold: every ``message`` entry in load order (ignores cursor)."""
        ...

    @property
    def context(self) -> list[dict[str, Any]]:
        """The active-path context at the current cursor — the model-input source."""
        ...

    @property
    def model(self) -> str:
        """The latest ``model_change`` model. Raises if the session has none."""
        ...

    @property
    def backend(self) -> str:
        """The latest ``model_change`` backend. Raises if the session has none."""
        ...

    def display_title(self) -> str:
        """A short human label: the name, else the first user message, else model."""
        ...

    def append_model_change(self, model: str, backend: str) -> str: ...

    def append_session_info(self, name: str) -> str: ...


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
    # Why the session's ENTRIES could not be read, or None if they were.
    #
    # A listing is built from two sources: cheap per-session metadata (id, cwd,
    # name, created — a store can always produce these) and the entries themselves
    # (message_count, first/last_message, modified). The second half can fail on a
    # session the first half describes perfectly well: a corrupt tree, a broken
    # integrity invariant, a truncated file.
    #
    # The choice at that point is NOT "raise or skip". Raising lets one bad session
    # brick the whole picker, so the user cannot reach the twenty good ones. Skipping
    # makes the session VANISH — the user sees a picker missing a conversation they
    # remember having, with no signal that anything is wrong, which is precisely the
    # silent failure that persisting a session was supposed to make impossible.
    #
    # So the row stays, and it says what happened. ``display_title`` marks it; the
    # store's ``load()`` still raises with the real reason when it is opened.
    error: str | None = None

    def display_title(self) -> str:
        if self.error:
            # A corrupt session's own name/first_message may be missing or nonsense
            # (that is what "corrupt" means), so the ref is what identifies the row.
            return f"⚠ unreadable session ({self.ref}) — {self.error}"
        if self.name:
            return self.name
        text = self.first_message.replace("\n", " ")
        if not text:
            return f"Session ({self.id[:8]})"
        return text[:50] + ("..." if len(text) > 50 else "")


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
    - ``fork(source, cwd)`` — a new session carrying ``source``'s history.
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
    def fork(self, source: ConversationSession, cwd: str) -> ConversationSession:
        """A new session carrying ``source``'s history; ``source`` is untouched."""

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
