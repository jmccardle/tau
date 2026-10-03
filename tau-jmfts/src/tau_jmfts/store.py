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

The store keeps no leaf; a ``Cursor`` decides where each entry goes
(docs/CURSORS.md). ``append_at`` matches ``InMemorySessionLog`` and the file
``Session`` exactly, which the ``SessionLogContractTests`` suite in
``tau-agent-core`` pins. The one thing genuinely new here is the
``parentId is None`` <-> "parented under the conversation ROOT DOCUMENT" mapping
(Sec2.3): the tau entry-tree is a *forest* of root-level entries, but the JMFTS
document tree has exactly one root (the header) -- every root-level tau entry is
in fact a *child* of the header document in JMFTS.
"""

from __future__ import annotations

import asyncio
import copy
import os
import socket
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from tau_agent_core.session_log import (
    CONFIG_ENTRY_TYPE,
    config_at,
    default_leaf,
    session_name,
    event_iso,
    finalized_entry,
    normalize_loaded_entries,
)
from tau_jmfts.client import JmftsClient

SESSION_VERSION = 1

_HEADER_REQUIRED = {"type", "version", "id", "timestamp", "cwd", "hostname", "parent"}

_MESSAGE_KINDS = ("message", "customMessage")
_SUMMARY_KINDS = ("compaction", "branch_summary")

# Namespaced, so session_log.default_leaf never opens a cursor on it (Sec2.4).
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


_CROSS_REF_FIELDS = ("targetId", "firstKeptId", "fromId")

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
            continue
        moved = old_to_new.get(int(ref))
        if moved is not None:
            tau[field] = str(moved)
    return sc


def _required(config: dict[str, Any], key: str, session_id: str) -> str:
    """``config[key]`` as a string, or raise naming the session that lacks it."""
    value = config.get(key)
    if value is None:
        raise ValueError(f"session {session_id} has no {key!r} in its config")
    return str(value)


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
        seqs: dict[str, int] | None = None,
    ) -> None:
        self._client = client
        self._root_doc_id = root_doc_id
        self._header = header
        self._entries = entries
        self._ids: set[str] = {e["id"] for e in entries}
        self._next_seq = next_seq
        self._seqs: dict[str, int] = dict(seqs or {})
        self._append_lock = threading.Lock()

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
    def header(self) -> dict[str, Any]:
        return dict(self._header)

    # --- reconstructed views -------------------------------------------------

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Unspliced ``message`` entries on the path to the default leaf, root first."""
        from tau_agent_core.conversation_tree import ConversationTree

        path = ConversationTree(self._entries, default_leaf(self._entries)).path()
        return [e["message"] for e in path if e.get("type") == "message"]

    @property
    def context(self) -> list[dict[str, Any]]:
        """The folded context at the default leaf."""
        from tau_agent_core.conversation_tree import ConversationTree

        return ConversationTree(self.entries(), default_leaf(self._entries)).context_for()

    @property
    def config(self) -> dict[str, Any]:
        """The folded config at the default leaf (``session_log.config_at``)."""
        return config_at(self._entries, default_leaf(self._entries))

    @property
    def model(self) -> str:
        """The config's model name. Raises if none — ``create`` always writes one."""
        return _required(self.config, "model", self.id)

    @property
    def backend(self) -> str:
        """The config's backend name. Raises if none."""
        return _required(self.config, "backend", self.id)

    def display_title(self) -> str:
        """A short human label: the name, else the first user message, else model."""
        name = session_name(self._entries)
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
        initial entries the file ``Session.create`` writes (config, optional
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
        loading a misordered tree.
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
                        "type": _FOREIGN_KIND,
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
        return cls(
            client,
            root_doc_id,
            header,
            normalize_loaded_entries(entries),
            next_seq=next_seq,
            seqs={str(doc_id): seq for doc_id, seq in tau_order},
        )

    @classmethod
    def fork(
        cls,
        client: JmftsClient,
        source: "JmftsSessionLog",
        cwd: str,
        *,
        host_parent_id: int | None = None,
        at: str | None = None,
    ) -> "JmftsSessionLog":
        """Fork ``source`` into a new root + a bulk copy of its entries, preserving
        topology (Sec3.4) -- semantics identical to the file ``Session.fork`` full
        copy, not a zero-copy share (that would make two conversations' trees
        overlap, breaking root discipline and delete semantics).

        Client-side loop (Sec3.4 -- CR-3 batch create would make this one request).
        Foreign documents in the source subtree are copied verbatim (usetype,
        title, content, structured_content unchanged) so the fork's tree looks
        exactly like the source's, foreign nodes included. ``source`` is untouched.
        With ``at``, only the documents on the path to that entry are copied, so the
        fork continues from it (docs/CURSORS.md §4).

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
        if at is not None:
            from tau_agent_core.conversation_tree import ConversationTree

            if at not in source._ids:
                raise ValueError(f"fork point {at!r} not found")
            on_path = {int(e["id"]) for e in ConversationTree(source._entries, at).path()}
            descendants = [d for d in descendants if d["id"] in on_path]
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
                sequential=True,
            )
            old_to_new[doc["id"]] = copied["id"]

        return cls.load(client, new_root_id)

    def _init_state(
        self, model: str, backend: str, system_prompt: str | None, name: str | None
    ) -> None:
        """Write the opening chain ``Session._init_state`` writes, synchronously.

        ``create`` is synchronous and may run before a head has an event loop.
        """
        config = {"model": model, "backend": backend, "cwd": self._header.get("cwd")}
        parent = self._append_now(
            None, "customEntry", {"customType": CONFIG_ENTRY_TYPE, "data": config}
        )
        if name is not None:
            parent = self._append_now(parent, "session_info", {"name": name})
        if system_prompt:
            self._append_now(
                parent, "message", {"message": {"role": "system", "content": system_prompt}}
            )

    async def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Write one entry at ``parent_id`` (the ``SessionLog`` contract).

        Nothing marks whose entry it is; the subtree is the record, and on this
        store it is searchable as documents under its parent (docs/LANE-REMOVAL.md
        §4). The POST runs on a worker thread, because it is synchronous HTTP and
        froze a head's screen on its own loop (docs/BLOCKING-PERSISTENCE.md §1).
        """
        if parent_id is not None and parent_id not in self._ids:
            raise ValueError(f"append parent {parent_id!r} not found")
        return await asyncio.to_thread(self._append_now, parent_id, entry_type, payload)

    async def finalize(self, entry_id: str, payload: dict[str, Any]) -> None:
        """Complete an incomplete entry by patching its document in place.

        The document keeps its id, parent and ``seq``, so ``load``'s order check
        still holds; only ``structured_content.tau`` and its projections change.

        Raises:
            ValueError: ``entry_id`` names no entry, or one that is not incomplete.
        """
        await asyncio.to_thread(self._finalize_now, entry_id, payload)

    def _finalize_now(self, entry_id: str, payload: dict[str, Any]) -> None:
        """The PATCH :meth:`finalize` runs on a worker thread, under the append lock."""
        with self._append_lock:
            for index, entry in enumerate(self._entries):
                if entry["id"] == entry_id:
                    break
            else:
                raise ValueError(f"finalize: entry {entry_id!r} not found")
            final = finalized_entry(entry, payload, _now_iso)
            tau_payload = {k: v for k, v in final.items() if k not in ("id", "parentId")}
            seq = self._seqs[entry_id]
            self._client.update_document(
                int(entry_id),
                title=_title_for(final["type"], payload, seq),
                content=_content_for(final["type"], payload),
                structured_content={"tau": tau_payload, "seq": seq},
                re_embed=False,
            )
            self._entries[index] = final

    # --- internals -------------------------------------------------------

    def _append_now(self, parent_id: str | None, kind: str, payload: dict[str, Any]) -> str:
        """POST the entry document, then adopt the returned id into the mirror.

        ``parent_id`` ``None`` (root-level) becomes the root document's id. Held
        under ``_append_lock`` from the seq draw to the mirror update, so concurrent
        cursors cannot draw seqs in one order and get doc ids in another: ``load``
        raises on that disagreement (docs/CURSORS.md §3). A ``session_info`` also
        retitles the root document; ``structured_content.tau`` stays authoritative.
        """
        parent_doc_id = self._root_doc_id if parent_id is None else int(parent_id)
        tau_payload: dict[str, Any] = {
            "type": kind,
            "timestamp": event_iso(payload, _now_iso),
            **payload,
        }
        with self._append_lock:
            seq = self._next_seq
            self._next_seq += 1
            doc = self._client.create_document(
                title=_title_for(kind, payload, seq),
                content=_content_for(kind, payload),
                parent_id=parent_doc_id,
                usetype=f"tau:{kind}",
                structured_content={"tau": tau_payload, "seq": seq},
                auto_embed=False,
                sequential=True,
            )
            entry_id = str(doc["id"])
            self._entries.append({**tau_payload, "id": entry_id, "parentId": parent_id})
            self._ids.add(entry_id)
            self._seqs[entry_id] = seq
        if kind == "session_info":
            self._client.update_document(self._root_doc_id, title=payload["name"], re_embed=False)
        return entry_id
