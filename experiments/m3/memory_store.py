"""In-memory JMFTS client backing for the M3 induction experiment (experiment 1).

Reference: docs/M3-DESIGN.md §6 (strict store isolation per condition/seed — a fresh
``memory:strategy`` root per run so no store bleeds into another) and §3 (the
versioned-tree strategy store this backs). This is a FAITHFUL in-memory implementation of
the subset of :class:`tau_jmfts.client.JmftsClient` that
:class:`tau_jmfts.ext.strategy_store.StrategyStore` exercises — it is NOT a test stub. It
honours the real client's contract: the parent/child document model, wholesale-replace
``structured_content`` PATCH semantics, CR-1 ``position`` assignment on ``sequential``
appends, and the ``title``/``usetype`` filters on ``get_children``/``list_documents``.
``StrategyStore`` runs against it with no behavioural difference from a live server for the
per-game-injection path experiment 1 uses.

``search`` is intentionally unimplemented. Experiment 1 injects the whole assembled family
document per game (§4.4 curriculum play) and never invokes the §3 retrieval step, so a
search backing would be dead, unexercised code. Per the repo's Fail-Early rule it RAISES
:class:`NotImplementedError` rather than returning a fabricated empty hit list that would
silently mask a call arriving on the wrong path.

get_children deliberately returns children NEWEST-FIRST (by descending id): the live
server's sibling order is unspecified and ``StrategyStore`` re-sorts by CR-1 ``position``,
so returning the adversarial order proves the store never leans on insertion order.
"""

from __future__ import annotations

from typing import Any

from tau_jmfts.client import DocumentDict


class InMemoryJmftsClient:
    """A faithful in-memory backing for the ``JmftsClient`` subset ``StrategyStore`` calls.

    Models exactly the methods the store touches — ``create_document`` (with CR-1
    ``sequential`` position assignment), ``get_document``, ``update_document`` (PATCH:
    only-passed-fields, ``structured_content`` fully replaced), ``get_children`` (with
    ``usetype``/``title``/``title_prefix`` filters), and ``list_documents`` (paginated,
    ``usetype``/``parent_id`` filters). ``search`` raises (see the module docstring).

    Only the ``DocumentResponse`` fields the store reads are modelled (``id``,
    ``parent_id``, ``title``, ``content``, ``usetype``, ``structured_content``,
    ``position``); anything the store never inspects is absent by design.
    """

    def __init__(self) -> None:
        self._docs: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    # -- CR-1 position assignment ------------------------------------------------

    def _assign_position(self, parent_id: int | None, sequential: bool | None) -> int | None:
        """The CR-1 sibling position a new child receives, or ``None`` when unordered.

        ``sequential=True`` opts the child into ordering; ``None`` inherits the parent's
        ordered-ness (an already-positioned parent yields positioned children); ``False``
        forces ``None``. A positioned child gets the next index after its positioned
        siblings — append order, which is temporal order for the log.
        """
        parent_ordered = False
        if parent_id is not None:
            parent = self._docs.get(parent_id)
            parent_ordered = parent is not None and parent.get("position") is not None
        ordered = sequential is True or (sequential is None and parent_ordered)
        if not ordered:
            return None
        siblings = [
            d
            for d in self._docs.values()
            if d["parent_id"] == parent_id and d["position"] is not None
        ]
        return len(siblings)

    # -- documents ---------------------------------------------------------------

    def create_document(
        self,
        *,
        title: str | None = None,
        content: str | None = None,
        parent_id: int | None = None,
        usetype: str | None = None,
        structured_content: dict[str, Any] | None = None,
        auto_embed: bool = False,
        sequential: bool | None = None,
    ) -> DocumentDict:
        """``POST /documents`` — create a document and return it verbatim.

        ``auto_embed`` is accepted for signature parity and ignored (this backing has no
        embedder). ``sequential`` drives CR-1 position assignment (see
        :meth:`_assign_position`).
        """
        doc_id = self._next_id
        self._next_id += 1
        doc = {
            "id": doc_id,
            "parent_id": parent_id,
            "title": title,
            "content": content,
            "usetype": usetype,
            "structured_content": dict(structured_content) if structured_content else {},
            "position": self._assign_position(parent_id, sequential),
        }
        self._docs[doc_id] = doc
        return dict(doc)

    def get_document(self, doc_id: int, *, include_embed: bool = False) -> DocumentDict:
        """``GET /documents/{id}`` — a copy of the stored document.

        ``include_embed`` is accepted for parity; this backing never stores an embedding.
        A missing id raises ``KeyError`` (the live server would raise ``JmftsError`` 404;
        the store never fetches an id it did not just create).
        """
        return dict(self._docs[doc_id])

    def update_document(
        self,
        doc_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        usetype: str | None = None,
        structured_content: dict[str, Any] | None = None,
        re_embed: bool = False,
    ) -> DocumentDict:
        """``PATCH /documents/{id}`` — mutate only the passed fields.

        PATCH semantics matching the real client: a field left ``None`` is untouched;
        ``structured_content``, when passed, REPLACES the field wholesale (it is not
        merged). This is exactly the semantics ``StrategyStore.consolidate`` relies on
        when it carries an existing ``structured_content`` forward and flips one flag.
        """
        doc = self._docs[doc_id]
        if title is not None:
            doc["title"] = title
        if content is not None:
            doc["content"] = content
        if usetype is not None:
            doc["usetype"] = usetype
        if structured_content is not None:
            doc["structured_content"] = dict(structured_content)
        return dict(doc)

    def get_children(
        self,
        doc_id: int,
        *,
        usetype: str | None = None,
        title: str | None = None,
        title_prefix: str | None = None,
        depth: int = 1,
        limit: int = 100,
    ) -> list[DocumentDict]:
        """``GET /documents/{id}/children`` — immediate children, filtered, newest-first.

        Applies the same server-side ``usetype``/``title``/``title_prefix`` narrowing the
        real endpoint offers. Fail-Early: ``depth != 1`` (descendant walks) is not
        modelled because the store never asks for it — it raises rather than silently
        returning only immediate children.
        """
        if depth != 1:
            raise NotImplementedError(
                f"InMemoryJmftsClient.get_children models only depth=1; got depth={depth}"
            )
        kids = [d for d in self._docs.values() if d["parent_id"] == doc_id]
        if usetype is not None:
            kids = [d for d in kids if d["usetype"] == usetype]
        if title is not None:
            kids = [d for d in kids if d["title"] == title]
        if title_prefix is not None:
            kids = [d for d in kids if (d["title"] or "").startswith(title_prefix)]
        # Newest-first: the server order is unspecified and the store must sort by CR-1
        # position itself; returning the adversarial order keeps it honest.
        kids.sort(key=lambda d: d["id"], reverse=True)
        return [dict(d) for d in kids[:limit]]

    def list_documents(
        self,
        *,
        parent_id: int | None = None,
        usetype: str | None = None,
        title_prefix: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentDict]:
        """``GET /documents`` — filters AND-ed, id-ascending, paginated on ``offset``.

        ``parent_id`` is an equality filter (as on the real endpoint), so passing ``None``
        matches any parent — the store filters root-ness (``parent_id is None``)
        client-side after narrowing by ``usetype``. Pagination is honoured so the store's
        page-until-short-page loop is genuinely exercised.
        """
        docs = list(self._docs.values())
        if parent_id is not None:
            docs = [d for d in docs if d["parent_id"] == parent_id]
        if usetype is not None:
            docs = [d for d in docs if d["usetype"] == usetype]
        if title_prefix is not None:
            docs = [d for d in docs if (d["title"] or "").startswith(title_prefix)]
        docs.sort(key=lambda d: d["id"])
        return [dict(d) for d in docs[offset : offset + limit]]

    def search(
        self,
        query: str,
        *,
        method: str = "hybrid",
        limit: int = 10,
        parent_id: int | None = None,
        usetype: str | None = None,
        exclude_types: list[str] | None = None,
        index_name: str = "default",
    ) -> list[dict[str, Any]]:
        """Unimplemented — experiment 1 uses per-game injection, never retrieval (§4.4).

        Fail-Early: raising here surfaces any accidental use of the §3 retrieval path,
        which a fabricated empty result would silently swallow.
        """
        raise NotImplementedError(
            "InMemoryJmftsClient.search is intentionally unimplemented: experiment 1 "
            "injects the assembled family document per game and never calls the retrieval "
            "step. A search-driven experiment needs a real JMFTS server."
        )
