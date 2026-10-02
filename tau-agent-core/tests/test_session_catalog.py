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
from tau_agent_core.session_log import InMemorySessionLog, default_leaf, session_name
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
    """A RAM-only :class:`ConversationSession` over an ``InMemorySessionLog``.

    Config is written as entries (``model_change``, ``session_info``) and read
    back the way the file ``Session`` reads it, so the only difference from that
    store is the missing disk flush.
    """

    def __init__(self, cwd: str, parent: str | None = None) -> None:
        self._log = InMemorySessionLog()
        self._parent = parent
        self._cwd = cwd
        self._created = _now()
        self._modified = self._created
        self._shutdown_calls = 0

    # -- SessionLog surface (delegates to the wrapped log) ------------------

    @property
    def id(self) -> str:
        return self._log.id

    def entries(self) -> list[dict[str, Any]]:
        return self._log.entries()

    async def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        return self._append_at_now(parent_id, entry_type, payload)

    def _append_at_now(
        self, parent_id: str | None, entry_type: str, payload: dict[str, Any]
    ) -> str:
        """Synchronous write for ``create``/``fork``, which are not coroutines."""
        self._modified = _now()
        return self._log.append_at_now(parent_id, entry_type, payload)

    # -- ConversationSession additions ---------------------------------------

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def header(self) -> dict[str, Any]:
        return {"type": "session", "id": self.id, "cwd": self._cwd}

    def _tree(self) -> ConversationTree:
        entries = self.entries()
        return ConversationTree(entries, default_leaf(entries))

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [e["message"] for e in self._tree().path() if e.get("type") == "message"]

    @property
    def context(self) -> list[dict[str, Any]]:
        return self._tree().context_for()

    def _latest_model_change(self) -> dict[str, Any]:
        for entry in reversed(self.entries()):
            if entry.get("type") == "model_change":
                return entry
        raise ValueError(f"session {self.id} has no model_change entry")

    @property
    def model(self) -> str:
        return str(self._latest_model_change()["model"])

    @property
    def backend(self) -> str:
        return str(self._latest_model_change()["backend"])

    @property
    def name(self) -> str | None:
        return session_name(self.entries())

    def display_title(self) -> str:
        if self.name:
            return self.name
        for message in self.messages:
            if message.get("role") == "user":
                text = _extract_text(message).replace("\n", " ")
                if text:
                    return text[:50] + ("..." if len(text) > 50 else "")
        return f"Session ({self.model})"

    def shutdown(self) -> None:
        self._shutdown_calls += 1


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
        session = _InMemoryConversationSession(cwd)
        leaf = session._append_at_now(None, "model_change", {"model": model, "backend": backend})
        if name:
            leaf = session._append_at_now(leaf, "session_info", {"name": name})
        if system_prompt:
            session._append_at_now(
                leaf, "message", {"message": {"role": "system", "content": system_prompt}}
            )
        return session

    def load(self, ref: str) -> ConversationSession:
        try:
            return self._sessions[ref]
        except KeyError:
            raise FileNotFoundError(f"no in-memory session {ref!r}") from None

    def fork(
        self, source: ConversationSession, cwd: str, *, at: str | None = None
    ) -> ConversationSession:
        assert isinstance(source, _InMemoryConversationSession)
        entries = source.entries()
        if at is not None:
            tree = ConversationTree(entries, at)
            if not tree.contains(at):
                raise ValueError(f"fork point {at!r} not found")
            entries = tree.path()
        forked = _InMemoryConversationSession(cwd, parent=source.id)
        new_ids: dict[str | None, str | None] = {None: None}
        for entry in entries:
            payload = {
                k: v for k, v in entry.items() if k not in ("type", "id", "parentId", "timestamp")
            }
            new_ids[entry["id"]] = forked._append_at_now(
                new_ids[entry.get("parentId")], entry["type"], payload
            )
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
