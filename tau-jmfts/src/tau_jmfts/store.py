"""tau_jmfts.store -- JmftsSessionLog: JMFTS as a SessionLog / ConversationSession.

The third SessionLog implementation (after InMemorySessionLog and the file
Session): the tau entry-tree is mirrored 1:1 onto a JMFTS document subtree
rooted at a ``tau:conversation`` document (the session header) rather than
onto RAM or a JSONL file. Depends on JmftsClient only -- no entry-shape or
tree-algebra knowledge lives in the client; that mapping is entirely here.

Reference: docs/JMFTS-INTEGRATION-PLAN.md Sec2 (the mapping, THE spec for this
module), Sec2.1 (per-document field mapping), Sec2.2 (root/header document),
Sec2.3 (entry ids, seq ordering, the cursor), Sec2.4 (foreign documents), Sec3.2
(write path), Sec3.3 (read path), Sec3.4 (fork/compaction/deletion), Sec8
(decisions: hard-fail on outage, numeric entry ids, sync writes).

Append algebra (parentId chaining off the leaf, navigate moving the leaf,
branch_summary re-parenting to the branch point, Fail-Early raises on unknown
ids) mirrors ``tau_agent_core.session_log.InMemorySessionLog`` and
``tau_coding_agent.session_store.Session`` exactly -- the ``SessionLogContractTests``
suite in ``tau-agent-core`` pins this. The one thing genuinely new here is the
``parentId is None`` <-> "parented under the conversation ROOT DOCUMENT" mapping
(Sec2.3): the tau entry-tree is a *forest* of root-level entries, but the JMFTS
document tree has exactly one root (the header) -- every root-level tau entry is
in fact a *child* of the header document in JMFTS.
"""

from __future__ import annotations

import copy
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

from tau_agent_core.session_log import resolve_cursor
from tau_jmfts.client import JmftsClient

#: Sentinel for "no explicit parent supplied" in ``JmftsSessionLog._append``. ``None``
#: cannot serve: it is a MEANINGFUL parent (root-level, the ``navigate(None)`` case).
_UNSET: Any = object()

SESSION_VERSION = 1

# The fields a well-formed tau:conversation header (Sec2.2) must carry. `load`
# raises if any are missing -- tau never opens an arbitrary document as a
# conversation (Sec2.4, last line).
_HEADER_REQUIRED = {"type", "version", "id", "timestamp", "cwd", "hostname", "parent"}

# Entry kinds whose `content` projection is the concatenated text of a message
# (Sec2.1). Everything else (navigate, compaction/branch_summary carry their own
# case below, config kinds) projects to empty content.
_MESSAGE_KINDS = ("message", "customMessage")
_SUMMARY_KINDS = ("compaction", "branch_summary")

# The synthesized kind for a foreign (non-tau:*) document in the subtree (Sec2.4).
_FOREIGN_KIND = "jmfts:document"


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string with ms precision + ``Z``.

    Mirrors ``session_log._now_iso`` / ``session_store._now_iso`` so JMFTS-backed
    entries carry an identically-shaped ``timestamp``."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _extract_text(message: dict[str, Any]) -> str:
    """Flatten a τ message's content to plain text (for the `content` projection)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and "text" in block
        ]
        return " ".join(parts)
    return ""


def _elide_content(payload: dict[str, Any]) -> str:
    """The searchable text projection of an ``elide`` (TREE-BROWSER-AS-EDITOR.md §7.3).

    An elide carries no ``summary``, so it used to fall through to ``return ""`` and
    land in JMFTS as a document with empty content: a query could never surface
    *where someone folded history*, only the (fully preserved) history itself. But
    an elide is not a deletion — it is a record of the conversation being engineered
    to proceed in a new direction, which is exactly the kind of thing a later search
    is looking for. Empty content makes the one node that marks the decision the one
    node that cannot be found.

    **What is honestly reachable from here, and what is not.** The browser row
    (``ConversationTree._splice_anchor_preview``) says "hides N entries, resumes at
    X", but N is a function of the *tree*: it walks ``parentId`` to the root and
    diffs ``context_entries`` at the parent. This function is handed one entry's
    payload — at ``_append`` time the document does not exist yet and at import time
    the ancestors are mid-remap — so N is not computable here, and per "Fail Early"
    a fabricated count in a search index is worse than no count: it would read as a
    recorded measurement forever. So the projection states only what the payload
    actually holds — that this node folds history, and where the fold resumes —
    which is enough for the entry to be findable and for the resume point to be
    followed by hand.

    A missing ``firstKeptId`` is stated rather than raised. Both appenders reject an
    anchor that names no entry (``JmftsSessionLog.append_elide``,
    ``session_store.Session.append_elide``), so a payload without one is a
    hand-written or corrupt log — and the importer is precisely the tool one reaches
    for to get such a log somewhere it can be inspected. Refusing to project its text
    would block the forensics; saying "no resume point recorded" makes the defect
    searchable, which is the same policy ``ConversationTree`` applies to the row.
    """
    reference = payload.get("firstKeptId")
    if reference is None:
        return "elide: history folded here, no resume point recorded"
    return f"elide: history folded here, context resumes at entry {reference}"


def _content_for(kind: str, payload: dict[str, Any]) -> str:
    """The searchable text projection of an entry (Sec2.1): concatenated text
    blocks of a message, the compaction/branch-summary text, the computed
    ``elide`` marker (§7.3), empty otherwise (navigate + config kinds)."""
    if kind in _MESSAGE_KINDS:
        message = payload.get("message")
        return _extract_text(message) if isinstance(message, dict) else ""
    if kind in _SUMMARY_KINDS:
        return str(payload.get("summary", ""))
    if kind == "elide":
        return _elide_content(payload)
    return ""


def _title_for(kind: str, payload: dict[str, Any], seq: int) -> str:
    """A short label, e.g. ``"user — 0007"`` (Sec2.1)."""
    if kind in _MESSAGE_KINDS:
        message = payload.get("message")
        role = message.get("role", kind) if isinstance(message, dict) else kind
        return f"{role} — {seq:04d}"
    if kind == "customEntry":
        return f"{payload.get('customType', 'customEntry')} — {seq:04d}"
    return f"{kind} — {seq:04d}"


def _build_header(
    session_id: str, timestamp: str, cwd: str, *, parent: str | None
) -> dict[str, Any]:
    return {
        "type": "session",
        "version": SESSION_VERSION,
        "id": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "hostname": socket.gethostname(),
        "parent": parent,
    }


#: The τ payload fields that hold an ENTRY ID (as opposed to content). Under the
#: JMFTS store an entry id *is* a doc id, so any operation that mints new documents
#: for existing entries -- ``fork`` today, a CR-3 batch copy tomorrow -- must rewrite
#: these or leave them pointing at the wrong tree. Keep this list in sync with the
#: appenders in :class:`JmftsSessionLog`: ``navigate`` (``targetId``), ``compaction``
#: and ``elide`` (both ``firstKeptId``), ``branch_summary`` (``fromId``) —
#: TREE-BROWSER-AS-EDITOR.md §7.3 caught ``elide`` missing from this list, which was
#: a stale comment and not a behavioural gap: ``firstKeptId`` was already covered by
#: the tuple. A ``None`` value is MEANINGFUL (pre-root cursor / root-level
#: branch point) and must survive as ``None``, not be mistaken for a broken link.
_CROSS_REF_FIELDS = ("targetId", "firstKeptId", "fromId")

#: Payload fields that hold an entry id as PROVENANCE rather than as structure.
#: ``copiedFrom`` (TREE-BROWSER-AS-EDITOR.md §7.1, written by
#: ``tau_agent_core.tree_surgery.copy_of``) says where a copy's content came from.
#: Nothing folds on it, so an unresolvable one costs a reader one hop of history,
#: not a region of context — which is why it is remapped *when it can be* and left
#: alone when it cannot, instead of raising like :data:`_CROSS_REF_FIELDS`.
#:
#: **Unresolvable is the NORMAL case here.** A copy's source is usually outside the
#: subtree being copied — that is what makes it a copy rather than a move — so a
#: fork that demanded every ``copiedFrom`` resolve would refuse to fork any tree
#: anyone had pasted into. ``agentSpecId`` is left out of both tuples for the same
#: reason it is not validated at the appender (§8): it is a record, never read back.
_PROVENANCE_REF_FIELDS = ("copiedFrom",)


def _remap_cross_refs(
    structured_content: dict[str, Any], old_to_new: dict[int, int]
) -> dict[str, Any]:
    """Rewrite a copied entry's entry-id references onto the new documents.

    Fail-Early: a non-``None`` reference that ``old_to_new`` cannot resolve is not
    silently dropped or passed through — a dangling anchor produces no error at read
    time, it just makes the tree fold quietly lose a whole region (see
    :meth:`JmftsSessionLog.fork`). If it is unresolvable the SOURCE was already
    corrupt, and that must surface here rather than be copied into a second tree.
    """
    sc = copy.deepcopy(structured_content)
    tau = sc.get("tau")
    if not isinstance(tau, dict):
        return sc  # a foreign document: no τ payload, nothing to remap

    for field in _CROSS_REF_FIELDS:
        if field not in tau:
            continue
        ref = tau[field]
        if ref is None:
            continue  # pre-root / root-level: a real value, not a missing link
        old_id = int(ref)
        if old_id not in old_to_new:
            raise ValueError(
                f"cannot fork: entry references {field}={ref!r}, which names no document "
                "in the source subtree. The source tree is already corrupt; copying this "
                "reference would silently drop a region of the forked context."
            )
        tau[field] = str(old_to_new[old_id])

    for field in _PROVENANCE_REF_FIELDS:
        ref = tau.get(field)
        if ref is None or not str(ref).isdigit():
            # Not a doc id at all — an imported log's provenance still naming a
            # file-store id. It was already outside this store's numbering, so
            # there is nothing here to point it at.
            continue
        moved = old_to_new.get(int(ref))
        # Kept as-is when the source is outside the copied subtree: it still names
        # the entry the content came from, in the tree it came from.
        if moved is not None:
            tau[field] = str(moved)
    return sc


def _is_tau_doc(doc: dict[str, Any]) -> bool:
    """True if this document is a τ entry (Sec2.4): ``usetype`` is ``tau:*`` AND
    ``structured_content.tau`` is present. Anything else is a foreign document."""
    usetype = doc.get("usetype") or ""
    sc = doc.get("structured_content")
    return (
        isinstance(usetype, str)
        and usetype.startswith("tau:")
        and isinstance(sc, dict)
        and "tau" in sc
    )


class JmftsSessionLog:
    """A JMFTS-backed ``SessionLog`` + ``ConversationSession``.

    One conversation = one ``tau:conversation`` root document plus one JMFTS
    document per entry, topology-mirrored (Sec2). Entries are held in an
    in-memory mirror (``self._entries``, built at ``create``/``load``/``fork``
    time and kept current on every append) so reads never re-hit the network --
    exactly the shape ``Session``/``InMemorySessionLog`` already have, just with
    JMFTS as the durability layer instead of a JSONL file / nothing.

    Construct via :meth:`create`, :meth:`load`, or :meth:`fork` -- never call
    ``__init__`` directly (it takes an already-hydrated entry list).
    """

    def __init__(
        self,
        client: JmftsClient,
        root_doc_id: int,
        header: dict[str, Any],
        entries: list[dict[str, Any]],
        *,
        next_seq: int,
    ) -> None:
        self._client = client
        self._root_doc_id = root_doc_id
        self._header = header
        self._entries = entries
        self._ids: set[str] = {e["id"] for e in entries}
        # Cursor resolution must only ever be driven by tau's OWN writes. A foreign
        # document (Sec2.4) can land in the subtree out-of-band -- e.g. an extension
        # attaching a reference to a message between turns -- via a doc-id that sorts
        # after every tau entry. Feeding the combined list to resolve_cursor would let
        # that foreign write silently become the new cursor on the next `load`, which
        # would be a foreign actor moving tau's own tip. Filtering foreign entries out
        # before resolving keeps the invariant "the cursor only moves on a tau append"
        # true across a reload, exactly as it is on the live path (a foreign create_document
        # call never goes through self._leaf_id at all).
        self._leaf_id: str | None = resolve_cursor(
            [e for e in entries if e.get("type") != _FOREIGN_KIND]
        )
        self._next_seq = next_seq

    # --- identity / header --------------------------------------------------

    @property
    def id(self) -> str:
        """The stable τ session uuid (never the JMFTS doc id, never a path)."""
        return str(self._header["id"])

    @property
    def root_doc_id(self) -> int:
        """The JMFTS document id of the conversation root -- the storage-agnostic
        ``ref`` a future catalog resolves ``load()`` against (Sec3.1)."""
        return self._root_doc_id

    @property
    def client(self) -> JmftsClient:
        """The client this log writes through.

        Public so the ``enrich`` extension (W13) can do its deferred work against the
        SAME server and credentials the conversation was written with, rather than
        constructing a second client from config and hoping the two agree -- a
        mismatch there would silently embed and index a *different* JMFTS instance
        than the one holding the conversation.
        """
        return self._client

    @property
    def cursor(self) -> str | None:
        return self._leaf_id

    @property
    def header(self) -> dict[str, Any]:
        return dict(self._header)

    # --- reconstructed views -------------------------------------------------

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Raw linear fold: every ``message`` entry in load order."""
        return [e["message"] for e in self._entries if e.get("type") == "message"]

    @property
    def context(self) -> list[dict[str, Any]]:
        # Imported lazily to keep the module import graph obvious (store.py's job
        # is the mapping; ConversationTree is the shared fold every store reuses).
        from tau_agent_core.conversation_tree import ConversationTree

        return ConversationTree(self.entries(), self.cursor).context_for()

    @property
    def model(self) -> str:
        for entry in reversed(self._entries):
            if entry.get("type") == "model_change":
                return str(entry["model"])
        raise ValueError(f"session {self.id} has no model_change entry")

    @property
    def backend(self) -> str:
        for entry in reversed(self._entries):
            if entry.get("type") == "model_change":
                return str(entry["backend"])
        raise ValueError(f"session {self.id} has no model_change entry")

    def _latest_session_info_name(self) -> str | None:
        for entry in reversed(self._entries):
            if entry.get("type") == "session_info":
                value = entry.get("name")
                return str(value) if value else None
        return None

    def display_title(self) -> str:
        """A short human label: the name, else the first user message, else model."""
        name = self._latest_session_info_name()
        if name:
            return name
        for message in self.messages:
            if message.get("role") == "user":
                text = _extract_text(message).replace("\n", " ")
                if text:
                    return text[:50] + ("..." if len(text) > 50 else "")
        return f"Session ({self.model})"

    def entries(self) -> list[dict[str, Any]]:
        """Ordered, append-only raw entries, all kinds, in load order.

        deepcopy: the nested payload must not be shared with the live mirror --
        see InMemorySessionLog/Session's identical docstring; the contract suite
        (``test_entries_returns_a_deep_copy``) pins this."""
        return copy.deepcopy(self._entries)

    # --- construction ---------------------------------------------------------

    @classmethod
    def create(
        cls,
        client: JmftsClient,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
        id: str | None = None,
        host_parent_id: int | None = None,
    ) -> "JmftsSessionLog":
        """Create a new conversation: POST the root document, then seed the same
        initial entries the file ``Session.create`` writes (model_change, optional
        name, optional system message)."""
        timestamp = _now_iso()
        session_id = id if id is not None else uuid.uuid4().hex
        header = _build_header(session_id, timestamp, os.path.abspath(cwd), parent=None)
        root = client.create_document(
            title=f"tau:conversation {session_id[:8]}",
            usetype="tau:conversation",
            parent_id=host_parent_id,
            structured_content={"tau": header},
            auto_embed=False,
            # CR-1: a conversation root is never position-ordered — it is a
            # recency-sorted collection, not a reading sequence. Force NULL even if
            # host_parent_id happens to point at a positioned document.
            sequential=False,
        )
        session = cls(client, root["id"], header, [], next_seq=1)
        session._init_state(model, backend, system_prompt, name)
        return session

    @classmethod
    def load(cls, client: JmftsClient, ref: str | int) -> "JmftsSessionLog":
        """Reconstruct a conversation from its root JMFTS document id.

        One ``get_subtree`` query (Sec3.3), then: verify the root is a well-formed
        ``tau:conversation`` (Sec2.4 -- never open an arbitrary document as a
        conversation), partition descendants into τ entries and foreign documents
        (Sec2.4), sort by doc id (== insertion order == "load order" under the
        single-writer rule), and run the seq/doc-id integrity cross-check (Sec2.3):
        if sorting by doc id disagrees with the writer's own ``seq`` counter, a
        second writer touched the tree -- fail loudly rather than silently
        resolving the wrong cursor.
        """
        root_doc_id = int(ref)
        subtree = client.get_subtree(root_doc_id, max_depth=None)
        root_doc = subtree["root"]
        if root_doc.get("usetype") != "tau:conversation":
            raise ValueError(
                f"document {root_doc_id} is not a tau:conversation root "
                f"(usetype={root_doc.get('usetype')!r}); refusing to open it as a session"
            )
        sc = root_doc.get("structured_content") or {}
        header = sc.get("tau")
        if (
            not isinstance(header, dict)
            or header.get("type") != "session"
            or not _HEADER_REQUIRED.issubset(header)
        ):
            raise ValueError(
                f"document {root_doc_id} has a malformed tau:conversation header "
                f"(structured_content.tau={header!r}); refusing to open it as a session"
            )

        descendants = sorted(subtree["descendants"], key=lambda d: d["id"])
        entries: list[dict[str, Any]] = []
        tau_order: list[tuple[int, int]] = []  # (doc_id, seq), in doc-id order
        for doc in descendants:
            doc_id = doc["id"]
            parent_id = None if doc["parent_id"] == root_doc_id else str(doc["parent_id"])
            if _is_tau_doc(doc):
                doc_sc = doc["structured_content"]
                seq = doc_sc["seq"]
                tau_order.append((doc_id, seq))
                tau_payload = doc_sc["tau"]
                entries.append({**tau_payload, "id": str(doc_id), "parentId": parent_id})
            else:
                entries.append(
                    {
                        "type": "jmfts:document",
                        "id": str(doc_id),
                        "parentId": parent_id,
                        "timestamp": doc.get("created_at"),
                        "usetype": doc.get("usetype"),
                        "title": doc.get("title"),
                    }
                )

        seqs = [seq for _, seq in tau_order]
        if any(seqs[i] >= seqs[i + 1] for i in range(len(seqs) - 1)):
            raise ValueError(
                f"tau:conversation {root_doc_id}: doc-id order disagrees with seq order "
                f"({tau_order!r}) -- this means a second writer touched the tree "
                "(single-writer-per-conversation is a hard invariant, Sec2.3/Sec8)"
            )

        next_seq = (max(seqs) + 1) if seqs else 1
        return cls(client, root_doc_id, header, entries, next_seq=next_seq)

    @classmethod
    def fork(
        cls,
        client: JmftsClient,
        source: "JmftsSessionLog",
        cwd: str,
        *,
        host_parent_id: int | None = None,
    ) -> "JmftsSessionLog":
        """Fork ``source`` into a new root + a bulk copy of its entries, preserving
        topology (Sec3.4) -- semantics identical to the file ``Session.fork`` full
        copy, not a zero-copy share (that would make two conversations' trees
        overlap, breaking root discipline and delete semantics).

        Client-side loop (Sec3.4 -- CR-3 batch create would make this one request).
        Foreign documents in the source subtree are copied verbatim (usetype,
        title, content, structured_content unchanged) so the fork's tree looks
        exactly like the source's, foreign nodes included. ``source`` is untouched.

        **Cross-references are remapped, not just ``parent_id``.** An entry id IS a
        JMFTS doc id here, so the fork's fresh documents get fresh ids -- and three
        payload fields point AT entry ids: ``navigate.targetId``,
        ``compaction.firstKeptId``, ``branch_summary.fromId``. Copying
        ``structured_content`` verbatim leaves those aimed at the SOURCE's documents,
        which do not exist in the fork. Nothing raises: the tree fold simply never
        finds the anchor, so (for a compaction) the entire kept region silently drops
        out of the forked context -- history vanishing with no error, the exact
        dangling-anchor failure Sec2.3 warns about, and the worst class of bug in this
        codebase. Measured before the fix: forking a compacted session lost its kept
        messages outright.

        The single pass below is sound because the log is append-only, so every
        reference points BACKWARD: processing descendants in doc-id (== insertion)
        order guarantees a referent is already in ``old_to_new`` by the time anything
        refers to it. A reference that is nevertheless missing means the SOURCE tree
        was already corrupt, and we raise rather than propagate it into the fork.
        """
        timestamp = _now_iso()
        session_id = uuid.uuid4().hex
        header = _build_header(session_id, timestamp, os.path.abspath(cwd), parent=source.id)
        new_root = client.create_document(
            title=f"tau:conversation {session_id[:8]}",
            usetype="tau:conversation",
            parent_id=host_parent_id,
            structured_content={"tau": header},
            auto_embed=False,
            sequential=False,  # CR-1: conversation roots are never position-ordered
        )
        new_root_id = new_root["id"]

        subtree = client.get_subtree(source._root_doc_id, max_depth=None)
        descendants = sorted(subtree["descendants"], key=lambda d: d["id"])
        old_to_new: dict[int, int] = {}
        for doc in descendants:
            old_parent = doc["parent_id"]
            new_parent = (
                new_root_id if old_parent == source._root_doc_id else old_to_new[old_parent]
            )
            copied = client.create_document(
                title=doc.get("title"),
                content=doc.get("content"),
                parent_id=new_parent,
                usetype=doc.get("usetype"),
                structured_content=_remap_cross_refs(
                    doc.get("structured_content") or {}, old_to_new
                ),
                auto_embed=False,
                # CR-1: carry sibling ordering into the fork. Descendants are copied
                # in ascending-id (birth) order, so re-numbering positions here
                # reproduces each sibling group's original order under its new parent.
                sequential=True,
            )
            old_to_new[doc["id"]] = copied["id"]

        return cls.load(client, new_root_id)

    def _init_state(
        self, model: str, backend: str, system_prompt: str | None, name: str | None
    ) -> None:
        """Mirrors ``Session._init_state``: the entries every new session carries."""
        self.append_model_change(model, backend)
        if name is not None:
            self.append_session_info(name)
        if system_prompt:
            self.append_message({"role": "system", "content": system_prompt})

    # --- append API -------------------------------------------------------

    def append_message(self, message: dict[str, Any]) -> str:
        return self._append("message", message=message)

    def append_custom_message(self, message: dict[str, Any], custom_type: str) -> str:
        return self._append("customMessage", customType=custom_type, message=message)

    def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        return self._append("customEntry", customType=custom_type, data=data)

    def append_model_change(self, model: str, backend: str) -> str:
        return self._append("model_change", model=model, backend=backend)

    def append_session_info(self, name: str) -> str:
        """Mirrors ``Session.append_session_info``, plus keeping the root
        document's ``title`` projection current (Sec2.1: title is mutable via
        session_info) -- purely cosmetic, ``structured_content.tau`` stays
        authoritative regardless."""
        entry_id = self._append("session_info", name=name)
        self._client.update_document(self._root_doc_id, title=name, re_embed=False)
        return entry_id

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
        """Fail-Early on an unknown splice anchor -- see
        ``InMemorySessionLog.append_compaction`` for why this is the most
        damaging of the three unknown-id cases to skip.

        The five keyword-only provenance fields (TREE-BROWSER-AS-EDITOR.md §8,
        widened onto the Protocol by §11.3) ride in the entry payload like every
        other field, so they reach JMFTS through the same ``structured_content.tau``
        projection and need no schema of their own."""
        if first_kept_id not in self._ids:
            raise ValueError(
                f"compaction first_kept_id {first_kept_id!r} not found; the splice anchor "
                "must name a real entry, or the whole kept region silently drops out of "
                "the context fold"
            )
        return self._append(
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

    def append_elide(
        self,
        first_kept_id: str,
        *,
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        """Fail-Early on an unknown splice anchor, mirroring
        ``append_compaction`` -- the summary-less generalization (W3,
        NODE-ADDRESSABLE-AGENTS.md) drops ``summary``/``tokensBefore`` but keeps
        the same anchor semantics ``ConversationTree`` folds on.

        Three provenance fields, not five (§8.2, §8.3): no summary means no
        summarizer model and no summary cost to record."""
        if first_kept_id not in self._ids:
            raise ValueError(
                f"elide first_kept_id {first_kept_id!r} not found; the splice anchor "
                "must name a real entry, or the whole kept region silently drops out of "
                "the context fold"
            )
        return self._append(
            "elide",
            firstKeptId=first_kept_id,
            coveredEntries=covered_entries,
            coveredTokens=covered_tokens,
            agentSpecId=agent_spec_id,
        )

    def append_navigate(self, target_id: str | None) -> str:
        """Move the leaf to ``target_id`` (``None`` = before the root document).
        The next append after ``navigate(None)`` parents directly under the root
        document -- a second, sibling root-level τ entry (Sec2.3's crux; the
        contract's ``test_navigate_to_none_starts_a_new_root_level_branch``)."""
        if target_id is not None and target_id not in self._ids:
            raise ValueError(f"navigate target {target_id!r} not found")
        entry_id = self._append("navigate", targetId=target_id)
        self._leaf_id = target_id
        return entry_id

    def append_branch_summary(self, summary: str, from_id: str | None) -> str:
        """Re-parent to the branch point BEFORE appending, mirroring
        ``Session.append_branch_summary`` / ``InMemorySessionLog.append_branch_summary``:
        the summary's ``parentId`` (and, here, its JMFTS ``parent_id``) is the
        branch point, so the abandoned subtree becomes a sibling and drops out of
        the ``context_for`` fold."""
        if from_id is not None and from_id not in self._ids:
            raise ValueError(f"branch_summary from {from_id!r} not found")
        self._leaf_id = from_id  # branch point, not the current leaf
        return self._append("branch_summary", summary=summary, fromId=from_id)

    def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Explicit-parent append -- the C2/W14 branch primitive (see the SessionLog
        Protocol). Does NOT move this log's leaf: a branch's writes must never move the
        tip of the cursor that spawned it.

        Nothing marks the entry as a branch's. The subtree it forms IS the record
        (docs/LANE-REMOVAL.md §4), and on this store that subtree is already searchable
        as documents parented under the branch root.
        """
        if parent_id is not None and parent_id not in self._ids:
            raise ValueError(f"append parent {parent_id!r} not found")
        return self._append(entry_type, _parent=parent_id, **payload)

    # --- internals -------------------------------------------------------

    def _append(self, kind: str, _parent: str | None = _UNSET, **payload: Any) -> str:
        """POST the entry document first, then adopt the returned id (Sec2.3) and
        advance the in-memory mirror. ``parent_leaf`` -> ``parent_id`` is exactly
        the crux mapping: ``None`` (root-level) becomes the ROOT DOCUMENT's id.

        ``_parent`` overrides the leaf (the ``append_at`` / branch path) and, when
        given, this append does NOT advance ``self._leaf_id``. ``_UNSET`` rather than
        ``None`` as the sentinel because ``None`` is a MEANINGFUL parent here -- it is
        "root-level", the ``navigate(None)`` case -- so it cannot double as "not
        supplied" without silently rewriting a root-level branch append into a
        chain-off-the-leaf one.
        """
        explicit_parent = _parent is not _UNSET
        parent_leaf = self._leaf_id if not explicit_parent else _parent
        parent_doc_id = self._root_doc_id if parent_leaf is None else int(parent_leaf)

        tau_payload: dict[str, Any] = {"type": kind, "timestamp": _now_iso(), **payload}
        seq = self._next_seq
        self._next_seq += 1

        doc = self._client.create_document(
            title=_title_for(kind, payload, seq),
            content=_content_for(kind, payload),
            parent_id=parent_doc_id,
            usetype=f"tau:{kind}",
            structured_content={"tau": tau_payload, "seq": seq},
            auto_embed=False,
            # CR-1: every entry takes an explicit sibling position (birth order).
            # The root carries no position, so ordering must be opted into here at
            # the top of the entry region; it then makes fork points (a node with
            # several children) deterministically ordered rather than created_at-tied.
            sequential=True,
        )

        entry_id = str(doc["id"])
        entry: dict[str, Any] = {**tau_payload, "id": entry_id, "parentId": parent_leaf}
        self._entries.append(entry)
        self._ids.add(entry_id)
        if not explicit_parent:
            self._leaf_id = entry_id  # explicit-parent appends leave the leaf alone
        return entry_id
