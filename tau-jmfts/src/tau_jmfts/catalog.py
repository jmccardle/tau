"""tau_jmfts.catalog -- JmftsSessionCatalog: the SessionCatalog seam over JMFTS.

Implements ``tau_agent_core.session_catalog.SessionCatalog``'s five abstract
methods (``create``, ``create_ephemeral``, ``load``, ``fork``, ``list``) over
``JmftsClient``/``JmftsSessionLog``, plus a JMFTS-specific ``delete`` extra
(not on the ABC -- the picker wants it, per Sec3.4).

Reference: docs/JMFTS-INTEGRATION-PLAN.md Sec2.2 (header shape), Sec2.3 (entry
ids), Sec3.1 (config), Sec3.3 (read path), Sec3.4 (fork/delete);
``tau_agent_core.session_catalog`` (the ABC this implements);
``tau_coding_agent.session_store.FileSessionCatalog`` (the reference sibling
implementation over the file store).

Design notes (see the W11/W12 final report for the full writeup):

- ``list(cwd)`` PAGES over ``JmftsClient.list_documents`` (Sec-critical: a full
  page is never proof of completeness -- see ``client.py``'s own docstring).
  Root discovery uses ``list_documents(usetype="tau:conversation")``, never
  ``GET /documents/roots`` (no usetype filter there). "Root-ness" is confirmed
  by validating ``structured_content.tau`` against the same
  ``_HEADER_REQUIRED`` set ``JmftsSessionLog.load`` enforces -- NOT by checking
  ``parent_id is None``, because a conversation root may itself be planted
  under a host document (``host_parent_id``/config ``parent_id``, Sec3.1) and
  still be a perfectly valid conversation root.
- ``cwd`` scoping is client-side (CR-2 unlanded): every ``list_documents`` page
  already returns full ``structured_content``, so cwd/name/created are cheap
  (one paginated query set, no extra round trips). ``message_count``/
  ``first_message``/``last_message``/``modified`` are NOT soundly derivable
  from the root document alone (a root's own ``updated_at`` only changes when
  its title is patched, never when a child entry is appended) -- they require
  one ``JmftsSessionLog.load`` (one ``get_subtree`` round trip) PER SESSION,
  paid only for cwd-filtered candidates. This is the same asymptotic cost
  ``FileSessionCatalog.list`` already pays (it streams every ``.jsonl`` file in
  the scope to build each ``SessionInfo``) -- see the report for measured
  numbers against the live server.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from typing import List as _List
from typing import Tuple as _Tuple

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog, SessionInfo
from tau_agent_core.session_log import InMemorySessionLog
from tau_jmfts.client import JmftsClient, JmftsError
from tau_jmfts.store import _HEADER_REQUIRED, SESSION_VERSION, _extract_text
from tau_jmfts.store import JmftsSessionLog

# Default page size for the list_documents pagination loop (Sec-critical: this
# is a PAGE size, not a result cap -- list() pages on offset until a short page
# comes back, so this only trades round-trip count for per-request payload
# size). Overridable per catalog instance so tests can exercise the paging
# loop without creating 100+ live sessions.
DEFAULT_LIST_PAGE_SIZE = 100


def _parse_iso(value: str) -> datetime:
    """Parse one of our own ``_now_iso()``-shaped timestamps."""
    return datetime.fromisoformat(value)


def _validate_header(doc: dict[str, Any]) -> dict[str, Any] | None:
    """``None`` unless ``doc`` carries a well-formed ``tau:conversation`` header.

    Mirrors ``JmftsSessionLog.load``'s own header check exactly (Sec2.4: "τ
    never opens an arbitrary document as a conversation"), but returns
    ``None`` instead of raising -- at the LIST edge a single malformed root
    must not break the whole picker, exactly the same "skip at the list edge"
    contract ``tau_coding_agent.session_store.read_session_info`` already
    applies to a corrupt ``.jsonl`` file.
    """
    sc = doc.get("structured_content")
    if not isinstance(sc, dict):
        return None
    header = sc.get("tau")
    if (
        not isinstance(header, dict)
        or header.get("type") != "session"
        or not _HEADER_REQUIRED.issubset(header)
    ):
        return None
    return header


def _derive_name(title: str | None, header: dict[str, Any]) -> str | None:
    """The session's ``name``, derived from the root document's ``title``
    WITHOUT a per-session query.

    ``JmftsSessionLog.append_session_info`` is the ONLY code path that ever
    patches a root document's ``title`` (store.py, ``append_session_info``),
    and it always sets it to exactly the new name. So whenever ``title``
    differs from the auto-generated default ``create()`` seeds
    (``f"tau:conversation {id[:8]}"``), it IS the current name -- a real read
    of stored data, not a guess. The one edge case this misreports: a session
    explicitly renamed back to a string that happens to equal its own
    auto-generated default reports as unnamed. Accepted -- the alternative is
    a per-session query for a field this cheap read already answers 999,999
    times out of 1,000,000.
    """
    if title is None:
        return None
    default_title = f"tau:conversation {header['id'][:8]}"
    return None if title == default_title else title


class _EphemeralConversationSession:
    """``create_ephemeral``'s honest answer: a JMFTS-backed session cannot be
    "unpersisted", because every ``JmftsSessionLog`` append is a real
    ``POST /documents`` round trip to the server -- there is no local-only
    write mode to fall into. Two dishonest outs exist and are both rejected:

    1. Silently writing to JMFTS anyway (defeats the entire point of
       ``--no-session``: the caller asked for NOTHING to be persisted).
    2. Silently returning a file-backed ``Session`` (plants an unrelated
       on-disk artifact under a catalog whose whole contract is "this is a
       JMFTS session"; also ``tau_coding_agent`` is a sibling package this
       one must not depend on -- see the monorepo layering in CLAUDE.md).

    The honest reading: "ephemeral" is a property of every backend alike --
    RAM-only, no durability layer touched at all, full stop. That is exactly
    what ``tau_agent_core.session_log.InMemorySessionLog`` already is, but it
    alone does NOT satisfy ``ConversationSession`` (checked against the
    Protocol in ``session_catalog.py``: no ``header``/``messages``/``context``/
    ``model``/``backend``/``display_title``/``append_model_change``/
    ``append_session_info``). This class layers exactly those on top of an
    ``InMemorySessionLog`` -- the same composition
    ``tau_coding_agent.session_store.Session.create_in_memory`` and the
    tau-agent-core test double (``_InMemoryConversationSession``,
    ``test_session_catalog.py``) already use for their own ephemeral cases.
    Not a new pattern, and not "inventing something" to route around a gap:
    it is the correct, storage-agnostic realization of "ephemeral", built
    entirely out of primitives ``tau_jmfts`` already depends on
    (``tau_agent_core``), with zero JMFTS I/O anywhere in it.
    """

    def __init__(
        self,
        cwd: str,
        model: str,
        backend: str,
        *,
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        self._log = InMemorySessionLog(id=id)
        self._cwd = os.path.abspath(cwd)
        self._model = model
        self._backend = backend
        self._name = name

    @classmethod
    def create(
        cls,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> "_EphemeralConversationSession":
        session = cls(cwd, model, backend, name=name)
        if system_prompt:
            session.append_message({"role": "system", "content": system_prompt})
        return session

    # -- SessionLog surface (delegates to the wrapped in-memory log) --------

    @property
    def id(self) -> str:
        return self._log.id

    @property
    def cursor(self) -> str | None:
        return self._log.cursor

    def entries(self) -> list[dict[str, Any]]:
        return self._log.entries()

    def append_message(self, message: dict[str, Any]) -> str:
        return self._log.append_message(message)

    def append_custom_message(self, message: dict[str, Any], custom_type: str) -> str:
        return self._log.append_custom_message(message, custom_type)

    def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        return self._log.append_custom_entry(custom_type, data)

    def append_compaction(self, summary: str, first_kept_id: str, tokens_before: int) -> str:
        return self._log.append_compaction(summary, first_kept_id, tokens_before)

    def append_elide(self, first_kept_id: str) -> str:
        """W3 splice anchor, delegated like every other appender (§ ``SessionLog``)."""
        return self._log.append_elide(first_kept_id)

    def append_navigate(self, target_id: str | None) -> str:
        return self._log.append_navigate(target_id)

    def append_branch_summary(self, summary: str, from_id: str | None) -> str:
        return self._log.append_branch_summary(summary, from_id)

    def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        """The C2/W14 explicit-parent append. Delegated like every other appender, so a
        branch sub-agent works in an ephemeral session too -- branching is a property of
        the entry algebra, not of durability."""
        return self._log.append_at(parent_id, entry_type, payload)

    # -- ConversationSession additions --------------------------------------

    @property
    def header(self) -> dict[str, Any]:
        return {
            "type": "session",
            "version": SESSION_VERSION,
            "id": self.id,
            "cwd": self._cwd,
            "parent": None,
        }

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

    def display_title(self) -> str:
        if self._name:
            return self._name
        for message in self.messages:
            if message.get("role") == "user":
                text = _extract_text(message).replace("\n", " ")
                if text:
                    return text[:50] + ("..." if len(text) > 50 else "")
        return f"Session ({self._model})"

    def append_model_change(self, model: str, backend: str) -> str:
        self._model, self._backend = model, backend
        return "model-change"

    def append_session_info(self, name: str) -> str:
        self._name = name
        return "session-info"


class JmftsSessionCatalog(SessionCatalog):
    """The JMFTS-backed :class:`~tau_agent_core.session_catalog.SessionCatalog`.

    One instance per run, injected with an already-constructed
    :class:`JmftsClient` -- config/CLI wiring (choosing the ``jmfts`` backend,
    building the client from ``~/.tau/config.json``) is out of scope here
    (Sec3.1's job, done elsewhere).

    ``host_parent_id`` mirrors the optional config ``parent_id`` (Sec3.1): a
    JMFTS document that hosts every conversation root this catalog creates or
    forks. ``list_page_size`` is a testability knob (see
    ``DEFAULT_LIST_PAGE_SIZE``); production callers should leave it at the
    default.
    """

    def __init__(
        self,
        client: JmftsClient,
        *,
        host_parent_id: int | None = None,
        list_page_size: int = DEFAULT_LIST_PAGE_SIZE,
    ) -> None:
        self._client = client
        self._host_parent_id = host_parent_id
        self._list_page_size = list_page_size

    # -- SessionCatalog's five abstract methods ------------------------------

    def create(
        self,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> JmftsSessionLog:
        return JmftsSessionLog.create(
            self._client,
            cwd,
            model,
            backend,
            system_prompt=system_prompt,
            name=name,
            host_parent_id=self._host_parent_id,
        )

    def create_ephemeral(
        self,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> ConversationSession:
        return _EphemeralConversationSession.create(
            cwd, model, backend, system_prompt=system_prompt, name=name
        )

    def load(self, ref: str) -> JmftsSessionLog:
        return JmftsSessionLog.load(self._client, ref)

    def fork(self, source: ConversationSession, cwd: str) -> JmftsSessionLog:
        # A real type gate (not `assert`, which `python -O` strips): forking a
        # non-JMFTS session through this catalog would be a silently-wrong
        # cross-store call, exactly the failure FileSessionCatalog.fork's own
        # gate exists to prevent.
        if not isinstance(source, JmftsSessionLog):
            raise TypeError(
                f"JmftsSessionCatalog.fork requires a JMFTS-backed JmftsSessionLog, "
                f"got {type(source)!r}"
            )
        return JmftsSessionLog.fork(self._client, source, cwd, host_parent_id=self._host_parent_id)

    def list(self, cwd: str | None = None) -> list[SessionInfo]:
        scope_cwd = os.path.abspath(cwd) if cwd is not None else None
        candidates = self._list_conversation_roots()
        if scope_cwd is not None:
            candidates = [c for c in candidates if c[1]["cwd"] == scope_cwd]

        infos = [
            info
            for info in (self._session_info_for(doc, header) for doc, header in candidates)
            if info is not None
        ]
        infos.sort(key=lambda i: i.modified, reverse=True)
        return infos

    # -- JMFTS-specific extra (not on the ABC) -------------------------------

    def delete(self, ref: str) -> None:
        """``DELETE /documents/{root}`` -- cascades over the whole subtree
        server-side (Sec3.4). Not on ``SessionCatalog`` (the file store has no
        analogous single-call delete); the picker uses this directly."""
        self._client.delete_document(int(ref))

    # -- resolve_ref: JMFTS's own directly-addressable ref form -------------

    def resolve_ref(self, ref: str, *, cwd: str | None = None) -> ConversationSession:
        """A JMFTS-store REF may be a document id, in addition to a τ session
        id/prefix (the storage-agnostic base's job).

        Mirrors ``FileSessionCatalog.resolve_ref``'s ``.jsonl``-path fast path
        exactly (session_store.py): an all-digits ref is tried as a document
        id FIRST; a clean 404 (the doc simply doesn't exist -- e.g. the ref
        was actually a numeric-looking τ id prefix) falls through to the
        storage-agnostic id/prefix search below. Any OTHER failure (a
        malformed header, a non-``tau:conversation`` root) is a REAL
        corruption and must surface loudly, not be silently reinterpreted as
        "try again as an id" -- same rule the file store's own override
        documents for a ``.jsonl`` path that exists but is malformed.
        """
        if ref.isdigit():
            try:
                return self.load(ref)
            except JmftsError as exc:
                if exc.status_code != 404:
                    raise
        return super().resolve_ref(ref, cwd=cwd)

    # -- internals ------------------------------------------------------------

    # NOTE: `_List`/`_Tuple` (typing aliases), not bare `list`/`tuple`, in the
    # two annotations below: this class defines a method named `list` (the ABC
    # requires that exact name), which shadows the builtin `list` name within
    # this class body's scope for annotation resolution -- a bare `list[...]`
    # here resolves to `JmftsSessionCatalog.list` itself, not the builtin, and
    # mypy correctly rejects that as "not valid as a type".
    def _list_conversation_roots(self) -> _List[_Tuple[dict[str, Any], dict[str, Any]]]:
        """Every well-formed ``tau:conversation`` root, unscoped by cwd.

        PAGES over ``list_documents(usetype="tau:conversation")`` on
        ``offset`` until a short page comes back -- never trusts a full page
        as proof of completeness (``JmftsClient.list_documents``'s own
        contract). ``GET /documents/roots`` is deliberately NOT used: it has
        no ``usetype`` filter, so it would mean pulling every root document in
        the whole JMFTS instance just to find the tau ones. Each returned
        ``(doc, header)`` pair reuses the ``structured_content`` the list
        response already carries -- no extra round trip per candidate.
        """
        roots: _List[_Tuple[dict[str, Any], dict[str, Any]]] = []
        offset = 0
        while True:
            page = self._client.list_documents(
                usetype="tau:conversation", limit=self._list_page_size, offset=offset
            )
            for doc in page:
                header = _validate_header(doc)
                if header is not None:
                    roots.append((doc, header))
            if len(page) < self._list_page_size:
                break
            offset += self._list_page_size
        return roots

    def _session_info_for(self, doc: dict[str, Any], header: dict[str, Any]) -> SessionInfo | None:
        """One ``SessionInfo`` per candidate root -- the expensive half.

        ``ref``/``id``/``cwd``/``name``/``created`` come from data the
        (cheap, already-paged) ``list_documents`` response carries. But
        ``message_count``/``first_message``/``last_message``/``modified``
        need the actual entries -- a root document's own ``updated_at`` only
        moves when ``append_session_info`` patches its title, never when a
        child entry is appended, so it is NOT a sound proxy for "last
        activity". There is no cheaper sound source for these four fields
        than the conversation's own entries, so this pays one
        ``JmftsSessionLog.load`` (one unbounded ``get_subtree`` round trip)
        PER CANDIDATE -- see the module docstring / final report for the
        measured cost.

        The three ways that load can fail are NOT the same failure, and this
        used to collapse all of them into ``return None`` -- a silent skip:

        * **404** -- the conversation was deleted between the list page and this
          load. It is genuinely GONE, so omitting it is the true answer, not a
          workaround. The only safe skip.
        * **Any other JmftsError** (500, timeout, transport) -- we do not know
          whether the session is fine. Dropping the row would report a PARTIAL
          list as if it were complete, so this raises: a picker that is missing
          sessions must say so rather than look healthy.
        * **ValueError** -- corruption, and by the time we get here it can only
          be ONE thing. ``_validate_header`` already rejected malformed and
          non-``tau:conversation`` roots from the cheap list response, so the
          only ``ValueError`` ``load`` has left to raise is the seq/doc-id
          cross-check at ``store.py``: **a second writer touched the tree.** That
          check exists to "fail loudly rather than silently resolving the wrong
          cursor" (its own docstring) -- and swallowing it here was the exact
          silent failure it was written to prevent. It surfaces as an error ROW:
          the header data we already hold is enough to identify the session, and
          ``load()`` still raises the real reason if the user opens it.
        """
        root_id = doc["id"]
        try:
            log = JmftsSessionLog.load(self._client, root_id)
        except JmftsError as exc:
            if exc.status_code == 404:
                return None
            raise
        except ValueError as exc:
            created = _parse_iso(header["timestamp"])
            return SessionInfo(
                ref=str(root_id),
                id=header["id"],
                cwd=header["cwd"],
                name=_derive_name(doc.get("title"), header),
                created=created,
                # No entries were readable, so there is no sound "last activity"
                # to report. `created` is the one timestamp we actually know.
                modified=created,
                message_count=0,
                first_message="",
                last_message="",
                parent=header.get("parent"),
                error=str(exc),
            )

        messages = [m for m in log.messages if m.get("role") in ("user", "assistant")]
        entries = log.entries()
        created = _parse_iso(header["timestamp"])
        modified = _parse_iso(str(entries[-1]["timestamp"])) if entries else created

        return SessionInfo(
            ref=str(root_id),
            id=header["id"],
            cwd=header["cwd"],
            name=_derive_name(doc.get("title"), header),
            created=created,
            modified=modified,
            message_count=len(messages),
            first_message=_extract_text(messages[0]) if messages else "",
            last_message=_extract_text(messages[-1]) if messages else "",
            parent=header.get("parent"),
        )
