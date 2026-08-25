"""SessionCatalog seam (W10) — proven with a SECOND, non-file implementation.

``tau_coding_agent.session_store.FileSessionCatalog`` is the only production
``SessionCatalog`` today, but the whole point of the seam is that headless/the
TUI never talk to it by name — they talk to ``SessionCatalog``. If only
``FileSessionCatalog`` can ever satisfy that ABC, it isn't a seam. This suite
builds a second, RAM-only ``SessionCatalog`` (deliberately test-only — no
product code for it yet) and runs the shared conformance suite over it.

The *behaviours* are no longer written out here: they live in
``tau_agent_core.testing.SessionCatalogContractTests`` and run identically over
this catalog, ``FileSessionCatalog`` and ``JmftsSessionCatalog``. What stays in
this file is the thing only this file can provide — a second implementation that
is not a store at all, written against nothing but the ABC. If the contract
suite ever grows an assumption about disks or servers, this is where it breaks.

``most_recent`` and ``resolve_ref`` are NOT reimplemented here — they are the
concrete, shared ``SessionCatalog`` base-class methods (session_catalog.py),
built purely out of the five abstract methods. Exercising them against this
second catalog is exactly what proves they generalize instead of secretly
assuming a file store.

Reference: session_catalog.py (the ABC + ConversationSession Protocol + moved
SessionInfo); W10 work-item notes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog, SessionInfo
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.testing import SessionCatalogContractTests


def _extract_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and "text" in b
        )
    return ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _InMemoryConversationSession:
    """A RAM-only :class:`ConversationSession` — wraps an ``InMemorySessionLog``
    for the entry/cursor algebra (proving THAT seam composes cleanly under a
    second frontend type) and layers the config reads
    (cwd/model/backend/name/header/display_title/shutdown) on top as plain
    mutable state, exactly mirroring how the file ``Session`` layers them over
    its own entry log — just without a disk flush.
    """

    def __init__(
        self,
        cwd: str,
        model: str,
        backend: str,
        name: str | None = None,
        parent: str | None = None,
    ) -> None:
        self._log = InMemorySessionLog()
        self._parent = parent
        self._cwd = cwd
        self._model = model
        self._backend = backend
        self._name = name
        self._created = _now()
        self._modified = self._created
        self._shutdown_calls = 0

    def _touch(self) -> None:
        self._modified = _now()

    # -- SessionLog surface (delegates to the wrapped log) ------------------

    @property
    def id(self) -> str:
        return self._log.id

    @property
    def cursor(self) -> str | None:
        return self._log.cursor

    def entries(self) -> list[dict[str, Any]]:
        return self._log.entries()

    def append_message(self, message: dict[str, Any]) -> str:
        self._touch()
        return self._log.append_message(message)

    def append_custom_message(self, message: dict[str, Any], custom_type: str) -> str:
        self._touch()
        return self._log.append_custom_message(message, custom_type)

    def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        self._touch()
        return self._log.append_custom_entry(custom_type, data)

    def append_compaction(
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
        self._touch()
        return self._log.append_compaction(
            summary,
            first_kept_id,
            tokens_before,
            summarizer_model_id=summarizer_model_id,
            summary_usage=summary_usage,
            covered_entries=covered_entries,
            covered_tokens=covered_tokens,
            agent_spec_id=agent_spec_id,
        )

    def append_elide(
        self,
        first_kept_id: str,
        *,
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        self._touch()
        return self._log.append_elide(
            first_kept_id,
            covered_entries=covered_entries,
            covered_tokens=covered_tokens,
            agent_spec_id=agent_spec_id,
        )

    def append_navigate(self, target_id: str | None) -> str:
        self._touch()
        return self._log.append_navigate(target_id)

    def append_branch_summary(self, summary: str, from_id: str | None) -> str:
        self._touch()
        return self._log.append_branch_summary(summary, from_id)

    def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        """The C2/W14 explicit-parent append — delegated like every other appender."""
        self._touch()
        return self._log.append_at(parent_id, entry_type, payload)

    # -- ConversationSession additions ---------------------------------------

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def header(self) -> dict[str, Any]:
        return {"type": "session", "id": self.id, "cwd": self._cwd}

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [e["message"] for e in self._log.entries() if e.get("type") == "message"]

    @property
    def context(self) -> list[dict[str, Any]]:
        return ConversationTree(self.entries(), self.cursor).context_for()

    @property
    def model(self) -> str:
        return self._model

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def name(self) -> str | None:
        return self._name

    def display_title(self) -> str:
        if self._name:
            return self._name
        for message in self.messages:
            if message.get("role") == "user":
                text = _extract_text(message).replace("\n", " ")
                if text:
                    return text[:50] + ("..." if len(text) > 50 else "")
        return f"Session ({self._model})"

    def shutdown(self) -> None:
        self._shutdown_calls += 1

    def append_model_change(self, model: str, backend: str) -> str:
        self._model, self._backend = model, backend
        self._touch()
        return "model-change"

    def append_thinking_change(self, level: str) -> str:
        self._touch()
        return "thinking-change"

    def append_session_info(self, name: str) -> str:
        self._name = name
        self._touch()
        return "session-info"


class InMemorySessionCatalog(SessionCatalog):
    """A RAM-only :class:`SessionCatalog` — the second implementation that proves
    the seam. Test-only: lives in tau-agent-core's test tree, not its ``src``."""

    def __init__(self) -> None:
        self._sessions: dict[str, _InMemoryConversationSession] = {}

    def create(
        self,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> ConversationSession:
        session = self._build(cwd, model, backend, system_prompt=system_prompt, name=name)
        self._sessions[session.id] = session
        return session

    def create_ephemeral(
        self,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> ConversationSession:
        # Mirrors FileSessionCatalog.create_ephemeral / Session.create_in_memory:
        # same construction, just never registered — so it never appears in list()
        # or via load() (there is no "disk" for it to persist to).
        return self._build(cwd, model, backend, system_prompt=system_prompt, name=name)

    @staticmethod
    def _build(
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None,
        name: str | None,
    ) -> _InMemoryConversationSession:
        session = _InMemoryConversationSession(cwd, model, backend, name)
        if system_prompt:
            session.append_message({"role": "system", "content": system_prompt})
        return session

    def load(self, ref: str) -> ConversationSession:
        try:
            return self._sessions[ref]
        except KeyError:
            raise FileNotFoundError(f"no in-memory session {ref!r}") from None

    def fork(self, source: ConversationSession, cwd: str) -> ConversationSession:
        assert isinstance(source, _InMemoryConversationSession)
        forked = _InMemoryConversationSession(
            cwd, source.model, source.backend, source.name, parent=source.id
        )
        # Self-contained copy of the message history (mirrors Session.fork); the
        # source is never touched. Compaction/navigate/branch_summary entries
        # reference entry ids from the SOURCE log, so replaying them verbatim isn't
        # meaningful here — no test in this suite forks a compacted/branched
        # session, so plain messages are all this minimal double needs to carry.
        for entry in source.entries():
            if entry.get("type") == "message":
                forked.append_message(entry["message"])
        self._sessions[forked.id] = forked
        return forked

    def list(self, cwd: str | None = None) -> list[SessionInfo]:
        infos = [
            SessionInfo(
                ref=s.id,
                id=s.id,
                cwd=s.cwd,
                name=s.name,
                created=s._created,
                modified=s._modified,
                message_count=sum(1 for m in s.messages if m.get("role") in ("user", "assistant")),
                first_message=next(
                    (_extract_text(m) for m in s.messages if m.get("role") == "user"), ""
                ),
                last_message=next(
                    (
                        _extract_text(m)
                        for m in reversed(s.messages)
                        if m.get("role") in ("user", "assistant")
                    ),
                    "",
                ),
                parent=s._parent,
            )
            for s in self._sessions.values()
            if cwd is None or s.cwd == cwd
        ]
        infos.sort(key=lambda i: i.modified, reverse=True)
        return infos


class TestInMemorySessionCatalogContract(SessionCatalogContractTests):
    """The RAM-only catalog, driven through the shared conformance suite.

    Nothing store-specific is asserted here beyond the two knobs below — which is
    the point. This file used to carry fifteen hand-written tests spelling out
    create/load/list/fork/most_recent/resolve_ref by hand; every one of them was a
    behaviour the *other* stores need too, so they now live in
    ``tau_agent_core.testing.session_catalog_contract`` and run against all three.
    """

    def make_catalog(self) -> SessionCatalog:
        return InMemorySessionCatalog()

    #: RAM-only: there is no second instance that could see these sessions, so the
    #: durability tests skip rather than pretending. ``reopen`` is inherited as-is.
    missing_ref_error = FileNotFoundError


def test_the_second_implementation_needs_no_base_class_help():
    """``most_recent``/``resolve_ref`` are inherited, never overridden, here.

    The seam's claim is that those two are built purely out of the five abstract
    primitives. This catalog overrides neither, and the contract suite exercises
    both against it — which is the proof, but only as long as nobody quietly adds
    an override later.
    """
    assert InMemorySessionCatalog.most_recent is SessionCatalog.most_recent
    assert InMemorySessionCatalog.resolve_ref is SessionCatalog.resolve_ref
