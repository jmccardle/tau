"""The versioned-tree strategy store — the reusable B-SEP spine of the memory track.

Reference: ``docs/M3-DESIGN.md`` §3 (THE STRATEGY STORE), with §4.6 (the retrieval
key) and §7 (the gate) as the downstream consumers of what is built here. This module
implements ONLY §3 — the store shape — not the induction policy, the chess engine, or
the gate.

**git, modeled in the JMFTS tree.** One ``memory:strategy`` root holds a *family* per
strategy: a **head** document carrying the current consolidated strategy text, plus an
**immutable, append-only child log** recording every modification in temporal order
(CR-1 sibling positions). The head is a materialized view and MAY be rewritten on
consolidation because it is always reconstructible from the log; the log is NEVER
rewritten or deleted — that invariant is what makes the poison audit (§3 "walking the
log") possible. The only mutation a log document ever receives is its ``consolidated``
flag flipping ``false → true`` when a consolidation pass folds it into the head.

**Fail-Early (repo rule).** Nothing here fabricates state. A log document missing its
``consolidated`` flag, or carrying a non-boolean one, or lacking a sibling ``position``
(so temporal order is undefined), is a corruption of the store's invariants and RAISES
rather than being papered over with a default. A duplicate head for one family name
likewise raises rather than silently picking one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tau_jmfts.client import DocumentDict, JmftsClient

# The root document's usetype. `StrategyStore` opens-or-creates exactly one root under
# this usetype (per condition/seed the experiment uses a distinct root *title* so no
# store bleeds into another — the M2 cache-separation hygiene, M3-DESIGN §6).
ROOT_USETYPE = "memory:strategy"

# Head and log documents carry distinct usetypes so `find` can narrow to heads
# SERVER-SIDE (search's usetype glob) instead of over-fetching the whole subtree and
# dropping non-heads client-side — which would silently lose heads that ranked below a
# log child. The `structured_content.kind` discriminator (below) is the authoritative
# classifier per §3; the usetypes mirror it for the search path.
HEAD_USETYPE = "memory:strategy:head"
LOG_USETYPE = "memory:strategy:log"

# `structured_content.kind` values — the authoritative document-role discriminator (§3).
HEAD_KIND = "strategy_head"
LOG_KIND = "strategy_log"

# A generous single-page fetch for a family's log. §3's answer to an unbounded log is
# RAPTOR digestion (cluster + summarize into digest nodes), which is out of this
# module's scope; until then this matches the sibling enrich pass's convention.
_LOG_PAGE_LIMIT = 1000

# Reserved structured_content keys the store owns; a caller's `extra` metadata may not
# shadow them (Fail-Early: a silent overwrite of `consolidated` would corrupt the footer
# read and the consolidation invariant).
_RESERVED_LOG_KEYS = frozenset({"kind", "consolidated"})


@dataclass(frozen=True)
class Family:
    """A handle to one strategy family: its name and the id of its head document.

    Returned by :meth:`StrategyStore.family` and accepted by the read/write methods.
    Carries only the head id (not a snapshot of its content), so it never goes stale
    when :meth:`StrategyStore.consolidate` rewrites the head — the current content is
    always re-fetched at read time.
    """

    name: str
    head_id: int


class StrategyStore:
    """The versioned-tree strategy store (M3-DESIGN §3).

    Opens-or-creates a single ``memory:strategy`` root and exposes the versioned-tree
    operations over it: ``family`` (get-or-create a head), ``append_log`` (immutable
    append), ``footer`` (the unconsolidated tail, a tree-read), ``consolidate``
    (rewrite head + flip flags), ``history`` (the full ordered log, for the poison
    audit), ``assemble`` (head + footer, the read-back), and ``find`` (the two-step
    retrieval's scoped head search).

    Uses the real :class:`JmftsClient` with no fallback — a dead or erroring server
    propagates as ``JmftsError``.
    """

    def __init__(
        self,
        client: JmftsClient,
        *,
        root_title: str = "memory:strategy",
    ) -> None:
        self._client = client
        self._root_title = root_title
        self._root_id: int | None = None

    # -- root ------------------------------------------------------------------

    @property
    def root_id(self) -> int:
        """The store root's document id, opening-or-creating it on first access."""
        if self._root_id is None:
            self._root_id = self._open_or_create_root()
        return self._root_id

    def _open_or_create_root(self) -> int:
        """Find the ``memory:strategy`` root with this store's title, or create it.

        Pages ``list_documents`` (which is paginated — a full page is not proof of a
        complete answer) narrowing by usetype server-side, then matches the exact title
        and root-ness (``parent_id is None``) client-side. A duplicate root is a
        corruption of the get-or-create invariant and raises rather than picking one.
        """
        matches: list[DocumentDict] = []
        offset = 0
        while True:
            page = self._client.list_documents(
                usetype=ROOT_USETYPE, limit=_LOG_PAGE_LIMIT, offset=offset
            )
            for doc in page:
                if doc.get("parent_id") is None and doc.get("title") == self._root_title:
                    matches.append(doc)
            if len(page) < _LOG_PAGE_LIMIT:
                break
            offset += _LOG_PAGE_LIMIT
        if len(matches) > 1:
            ids = sorted(d["id"] for d in matches)
            raise ValueError(
                f"strategy_store: {len(matches)} roots with usetype {ROOT_USETYPE!r} and "
                f"title {self._root_title!r} (ids {ids}) — the store root must be unique; "
                "the store cannot decide which is authoritative"
            )
        if matches:
            return int(matches[0]["id"])
        root = self._client.create_document(
            title=self._root_title,
            usetype=ROOT_USETYPE,
            parent_id=None,
            structured_content={"kind": "strategy_root"},
            auto_embed=False,
            # No `sequential` here, and nothing downstream wants one. CR-1 ordering is a
            # relationship between SIBLINGS under a parent, so it is undefined for a root
            # (`parent_id is None`) — the server has rejected `sequential=True` on a root
            # since jmfts d70cc57. Nor was it doing any work: inheritance is
            # `sequential = parent is not None and parent.position is not None`, and both
            # levels below set it themselves — `family()` on each head, `append_log()` on
            # each log entry — so the ordering the store depends on is asserted, not
            # inherited.
        )
        return int(root["id"])

    # -- families (heads) ------------------------------------------------------

    def family(self, name: str) -> Family:
        """Get-or-create the head document for ``name``; return a :class:`Family` handle.

        The head is a child of the root with ``usetype=memory:strategy:head``, its
        ``content`` the current consolidated strategy text (empty on creation), and
        ``structured_content={"kind": "strategy_head"}``. A second head with the same
        name is a corrupted invariant and raises.
        """
        existing = self._client.get_children(
            self.root_id, usetype=HEAD_USETYPE, title=name, limit=_LOG_PAGE_LIMIT
        )
        # `title` is a server-side exact-match filter, but re-check client-side: it is
        # the load-bearing uniqueness key and a server that ever loosened it (to a
        # prefix, say) must not silently return the wrong head.
        heads = [d for d in existing if d.get("title") == name]
        if len(heads) > 1:
            ids = sorted(d["id"] for d in heads)
            raise ValueError(
                f"strategy_store: {len(heads)} head documents named {name!r} under the "
                f"root (ids {ids}) — a family head must be unique"
            )
        if heads:
            return Family(name=name, head_id=int(heads[0]["id"]))
        head = self._client.create_document(
            title=name,
            content="",
            parent_id=self.root_id,
            usetype=HEAD_USETYPE,
            structured_content={"kind": HEAD_KIND},
            auto_embed=False,
            # A CR-1 position among its sibling heads, in creation order. `append_log`
            # asks for its children's positions itself rather than relying on this
            # propagating, so the log's temporal order (the footer/history invariant)
            # does not depend on the value here.
            sequential=True,
        )
        return Family(name=name, head_id=int(head["id"]))

    # -- append-only log -------------------------------------------------------

    def append_log(
        self,
        family: Family,
        description: str,
        *,
        title: str | None = None,
        source: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> DocumentDict:
        """Append one immutable log child to ``family``'s head.

        The child's ``content`` is ``description`` (the lesson/modification text), and
        its ``structured_content`` is ``{"kind": "strategy_log", "consolidated": false,
        ...}`` — ``source`` (a provenance tag, §7) and any ``extra`` metadata merged in.
        ``sequential=True`` gives it a CR-1 sibling ``position`` so the log carries
        temporal order.

        This is the ONLY way to add to the log, and log documents are never rewritten or
        deleted afterwards (append-only is the invariant that makes the poison audit
        possible). ``extra`` may not shadow the store-owned ``kind``/``consolidated``
        keys.
        """
        structured: dict[str, Any] = {"kind": LOG_KIND, "consolidated": False}
        if source is not None:
            structured["source"] = source
        if extra:
            clashes = _RESERVED_LOG_KEYS & set(extra)
            if clashes:
                raise ValueError(
                    f"append_log: extra metadata may not set the store-owned keys "
                    f"{sorted(clashes)} — they are managed by the store"
                )
            structured.update(extra)
        entry = self._client.create_document(
            title=title if title is not None else _default_log_title(description),
            content=description,
            parent_id=family.head_id,
            usetype=LOG_USETYPE,
            structured_content=structured,
            auto_embed=False,
            sequential=True,
        )
        return entry

    # -- the footer (a tree-read, not a search) --------------------------------

    def footer(self, family: Family) -> list[DocumentDict]:
        """The unconsolidated log tail — "my latest considerations not yet folded in".

        A CLIENT-SIDE filter of ``family``'s log children to ``consolidated is False``
        (there is no server-side ``structured_content`` filter). Ordered oldest-first by
        CR-1 position.
        """
        return [doc for doc in self._log_children(family) if not _consolidated_flag(doc)]

    # -- deferred consolidation ------------------------------------------------

    def consolidate(self, family: Family, new_head_content: str) -> int:
        """Fold the current footer into the head: rewrite head content, flip the flags.

        Rewrites the head's ``content`` to ``new_head_content`` (the head is a
        materialized view; the immutable log reconstructs history, so overwriting it is
        allowed — and we do NOT fork per-version head snapshots, decided in §3), then
        flips each currently-unconsolidated log child's ``consolidated`` flag to
        ``true``. The log children still EXIST afterwards — history is not destroyed.

        Returns the number of log children consolidated (0 if the footer was already
        empty; the head is still rewritten).
        """
        pending = self.footer(family)
        self._client.update_document(family.head_id, content=new_head_content)
        for doc in pending:
            # PATCH-with-structured_content REPLACES the whole field, so carry the
            # existing content forward and change ONLY the flag — the sole mutation a
            # log document is ever allowed. Content/title are untouched.
            updated = dict(doc["structured_content"])
            updated["consolidated"] = True
            self._client.update_document(doc["id"], structured_content=updated)
        return len(pending)

    # -- history / provenance --------------------------------------------------

    def history(self, family: Family) -> list[DocumentDict]:
        """The full ordered log-child sequence — consolidated and not alike.

        This is the poison-audit surface (§3 "walking the log"): because the log is
        immutable, a lesson that later proved to be poison is still here to be found,
        even after the consolidation that folded it into the head. Ordered oldest-first.
        """
        return self._log_children(family)

    # -- assemble (the read-back) ----------------------------------------------

    def assemble(self, family: Family) -> str:
        """Head content + the unconsolidated footer — the text to inject before a task.

        Between consolidation passes the *truth* is head + unconsolidated children (§3);
        this renders exactly that. With an empty footer it is the head content verbatim.
        """
        head = self._client.get_document(family.head_id)
        head_content = head.get("content") or ""
        pending = self.footer(family)
        if not pending:
            return head_content
        lines = [head_content, "", "## Latest considerations (not yet consolidated)"]
        lines.extend(f"- {doc.get('content') or ''}" for doc in pending)
        return "\n".join(lines)

    # -- find (retrieval step 1, §3) -------------------------------------------

    def find(
        self,
        query: str,
        k: int = 5,
        *,
        method: str = "hybrid",
    ) -> list[DocumentDict]:
        """Rank this store's head documents for a task query (§3 retrieval step 1).

        Scopes ``client.search`` to the strategy subtree (``parent_id = root``) and to
        heads only (``usetype = memory:strategy:head``), returning the ranked head
        documents. Deliberately thin: the task→query abstraction (§4.6) that decides
        WHAT to search for is a separate component, not this store's concern — this
        takes a query string and searches.
        """
        hits = self._client.search(
            query,
            method=method,
            limit=k,
            parent_id=self.root_id,
            usetype=HEAD_USETYPE,
        )
        return [hit["document"] for hit in hits]

    # -- internals -------------------------------------------------------------

    def _log_children(self, family: Family) -> list[DocumentDict]:
        """Every log child of ``family``'s head, validated and ordered oldest-first.

        Fail-Early on any invariant break: a document under the head with the log
        usetype but the wrong ``kind``, a missing/non-boolean ``consolidated`` flag, or
        a missing CR-1 ``position`` (temporal order undefined) all raise — the store
        never guesses a default for corrupt state.
        """
        children = self._client.get_children(
            family.head_id, usetype=LOG_USETYPE, limit=_LOG_PAGE_LIMIT
        )
        logs: list[DocumentDict] = []
        for doc in children:
            sc = doc.get("structured_content") or {}
            if sc.get("kind") != LOG_KIND:
                raise ValueError(
                    f"strategy_store: document {doc.get('id')} under head "
                    f"{family.head_id} has usetype {LOG_USETYPE!r} but "
                    f"structured_content.kind={sc.get('kind')!r} (expected {LOG_KIND!r})"
                )
            if "consolidated" not in sc:
                raise ValueError(
                    f"strategy_store: log document {doc.get('id')} is missing its "
                    "'consolidated' flag — this is a corrupt log entry, not a default-"
                    "false one"
                )
            if not isinstance(sc["consolidated"], bool):
                raise ValueError(
                    f"strategy_store: log document {doc.get('id')} has a non-boolean "
                    f"'consolidated' flag ({sc['consolidated']!r})"
                )
            if doc.get("position") is None:
                raise ValueError(
                    f"strategy_store: log document {doc.get('id')} has no CR-1 position — "
                    "temporal order is undefined; the append path must set sequential=True"
                )
            logs.append(doc)
        logs.sort(key=lambda d: d["position"])
        return logs


def _consolidated_flag(doc: DocumentDict) -> bool:
    """The validated boolean ``consolidated`` flag of a log document.

    Assumes ``doc`` already passed :meth:`StrategyStore._log_children` validation, so
    the flag is present and boolean.
    """
    flag: bool = doc["structured_content"]["consolidated"]
    return flag


def _default_log_title(description: str) -> str:
    """A short title derived from the log entry's text (first line, clipped)."""
    first_line = description.strip().splitlines()[0] if description.strip() else "(empty)"
    return first_line[:80]
