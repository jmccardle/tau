"""tau_jmfts.client -- JmftsClient: thin synchronous httpx wrapper over the JMFTS REST API.

Reference: docs/JMFTS-INTEGRATION-PLAN.md Sec3 (architecture), Sec3.2 (write path),
Sec3.3 (read path). This module is REST-transport only -- no SessionLog algebra,
no entry-shape mapping (that's tau_jmfts.store, layered on top, a later step).

Fail-Early: every non-2xx response raises JmftsError carrying the HTTP status
and the server's own detail payload. There is no retry policy, no fallback
return value (e.g. None on 404), and no swallowed exception anywhere in this
module -- a dead or erroring server must propagate to the caller.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

DEFAULT_TIMEOUT = 10.0

# The usetype τ writes chunk documents under. Deliberately NOT `tau:*` -- chunks live
# as children INSIDE the conversation subtree, and τ's loader treats a `tau:` usetype
# as a promise that `structured_content.tau` is a real entry payload. Chunks have no
# such payload, so a `tau:` usetype here would corrupt the load path (store._is_tau_doc).
CHUNK_USETYPE = "jmfts:chunk"

# The server REFUSES over-window text with HTTP 400 rather than embedding a truncated
# prefix -- `POST /documents/{id}/embed` 400s when the content exceeds the embedder's
# 512-token window. 512 tokens is ~2000 chars of English; this leaves margin, and
# anything above it must be chunked first (JmftsClient.chunk_document) to avoid a
# guaranteed round-trip failure. (This is the D1 server fix; before it, the embedder
# silently truncated at 512 tokens and lost the tail -- see docs/KNOWN-DEFECTS.md.)
EMBED_MAX_CHARS = 1800

# Words per chunk. The server's `max_tokens` counts WHITESPACE-SPLIT WORDS, not the
# subword tokens the embedder budgets (chunking.py: "word count as a fast proxy"), and
# NOT the chars EMBED_MAX_CHARS is expressed in -- three different units for one
# constraint. 300 words was the first guess and it overshot: technical prose ran ~6.4
# chars/word, so chunks came back at ~1925 chars, past the guard. At 220 words even
# long-worded text (identifiers, paths, stack traces) stays under EMBED_MAX_CHARS.
#
# This is a budget for the common case. Text with no word boundaries (a base64 blob is
# one enormous "word") cannot be bounded by a word count alone -- but the server now
# hard-splits any such over-window chunk at a character boundary (the D4 fix), so every
# returned chunk is guaranteed to fit the embedder's window regardless of this number.
CHUNK_MAX_WORDS = 220

_SEARCH_METHODS = frozenset({"hybrid", "vector", "bm25", "fulltext", "maxsim"})


class JmftsError(Exception):
    """Raised for any non-2xx JMFTS response, or a transport-level failure.

    Carries the HTTP status code and the server's detail payload (FastAPI's
    ``{"detail": ...}`` body -- a string for plain HTTPExceptions, a list of
    ``ValidationError`` dicts for 422s) so callers can inspect *why* without
    re-parsing the response body themselves. ``status_code`` is ``0`` for a
    transport-level failure (connection refused, timeout, DNS) where no HTTP
    response was ever received.
    """

    def __init__(self, status_code: int, detail: Any, *, url: str, method: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.url = url
        self.method = method
        super().__init__(f"JMFTS {method} {url} -> {status_code}: {detail!r}")


DocumentDict = dict[str, Any]
"""A JMFTS ``DocumentResponse`` JSON object, as returned verbatim by the server.

Fields (openapi ``DocumentResponse``, confirmed against the live server
2026-07-12): ``id`` (int), ``parent_id`` (int | None), ``title`` (str | None),
``content`` (str | None), ``structured_content`` (dict, always present --
never null, defaults to ``{}``), ``path`` (list), ``depth`` (int), ``usetype``
(str | None), ``position`` (int | None -- CR-1 sibling order, NULL for
unordered documents), ``created_at``/``updated_at`` (iso str | None),
``content_hash`` (str | None), ``embed`` (list[float] | None, only populated
when ``include_embed=True`` is passed to ``get_document``).
"""


class JmftsClient:
    """Thin synchronous httpx wrapper over the JMFTS REST API.

    One ``httpx.Client`` for the wrapper's lifetime (constructed once in
    ``__init__``, reused for every call) -- NOT a fresh client per request;
    see ``docs/PROVIDER-LIFETIME.md`` for the sibling bug (a fresh HTTP client
    per call cost +42 ms/call, 51% slower) this deliberately avoids. Use as a
    context manager or call ``close()`` explicitly when done.

    Synchronous by design (JMFTS-INTEGRATION-PLAN.md Sec3): it matches tau's
    existing blocking file-write pattern on the session hot path (do not wrap
    this in async -- see Sec3's rationale and Sec8 decision 3).
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JmftsClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- transport ------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise JmftsError(status_code=0, detail=str(exc), url=path, method=method) from exc
        if response.status_code >= 400:
            try:
                body = response.json()
                detail = body.get("detail", body) if isinstance(body, dict) else body
            except ValueError:
                detail = response.text
            raise JmftsError(
                status_code=response.status_code, detail=detail, url=path, method=method
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- health -----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """``GET /`` -- health check. Raises JmftsError if unreachable or erroring.

        Response (``HealthResponse``): ``status``, ``version``, ``database``,
        ``embedding_model`` (all str), ``llm`` (nullable).
        """
        result: dict[str, Any] = self._request("GET", "/")
        return result

    # -- documents --------------------------------------------------------

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
        """``POST /documents``.

        ``auto_embed`` defaults to False here (the server's own default is
        True): the write path never wants synchronous embedding inference on
        every entry append (Sec3.2 of the plan).

        ``sequential`` controls CR-1 explicit sibling ordering. ``None`` (the
        default) inherits ordered-ness from the parent -- so within an already
        ordered subtree children get positions automatically, but an unordered
        tree stays unordered. Pass ``True`` at the top of an ordered region (e.g.
        the first conversation entry under a root, since roots carry no position)
        to opt in; every deeper node then inherits. ``False`` forces NULL.
        """
        payload = {
            "title": title,
            "content": content,
            "parent_id": parent_id,
            "usetype": usetype,
            "structured_content": structured_content,
            "auto_embed": auto_embed,
            "sequential": sequential,
        }
        result: DocumentDict = self._request("POST", "/documents", json=payload)
        return result

    def get_document(self, doc_id: int, *, include_embed: bool = False) -> DocumentDict:
        """``GET /documents/{id}``."""
        params = {"include_embed": include_embed} if include_embed else None
        result: DocumentDict = self._request("GET", f"/documents/{doc_id}", params=params)
        return result

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
        """``PATCH /documents/{id}``. No ``parent_id`` param -- the server offers
        no reparent API (confirmed: ``DocumentUpdate`` has no ``parent_id`` field).

        ``re_embed`` defaults to False here (server default True), matching
        ``create_document``'s ``auto_embed`` override.

        **Only the fields the caller actually passed are sent.** A PATCH body
        carrying an explicit ``"content": null`` for an argument the caller merely
        omitted would be asking the server to decide our intent for us, and today it
        happens to decide the way we want (measured 2026-07-12: the live server
        ignores explicit nulls, so a title-only patch left ``content``/``usetype``/
        ``structured_content`` intact). That is luck, not a contract: the moment
        JMFTS adopts the *other* conventional reading of PATCH -- null means "clear
        this field" -- a rename would silently wipe ``structured_content``, which
        Sec2.1 makes the AUTHORITATIVE copy of the entry, with ``title``/``content``
        as mere projections. Omitting unset keys makes the request mean what we
        actually mean, under either server convention.
        """
        payload: dict[str, Any] = {"re_embed": re_embed}
        if title is not None:
            payload["title"] = title
        if content is not None:
            payload["content"] = content
        if usetype is not None:
            payload["usetype"] = usetype
        if structured_content is not None:
            payload["structured_content"] = structured_content
        result: DocumentDict = self._request("PATCH", f"/documents/{doc_id}", json=payload)
        return result

    def delete_document(self, doc_id: int) -> None:
        """``DELETE /documents/{id}`` -- cascades over the subtree (server-side ORM cascade)."""
        self._request("DELETE", f"/documents/{doc_id}")

    def get_subtree(self, doc_id: int, *, max_depth: int | None = None) -> dict[str, Any]:
        """``GET /documents/{id}/subtree``.

        Returns ``{"root": DocumentDict, "descendants": list[DocumentDict], "total": int}``.
        ``max_depth=None`` (the default) is unbounded -- one query fetches the
        whole subtree regardless of size (Sec1/Sec3.3 of the plan).
        """
        params = {"max_depth": max_depth} if max_depth is not None else None
        result: dict[str, Any] = self._request("GET", f"/documents/{doc_id}/subtree", params=params)
        return result

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
        """``GET /documents/{id}/children``. ``depth=1`` immediate children (default),
        ``depth=-1`` all descendants."""
        params: dict[str, Any] = {"depth": depth, "limit": limit}
        if usetype is not None:
            params["usetype"] = usetype
        if title is not None:
            params["title"] = title
        if title_prefix is not None:
            params["title_prefix"] = title_prefix
        result: list[DocumentDict] = self._request(
            "GET", f"/documents/{doc_id}/children", params=params
        )
        return result

    def list_documents(
        self,
        *,
        parent_id: int | None = None,
        usetype: str | None = None,
        title_prefix: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentDict]:
        """``GET /documents`` -- filters are AND-ed server-side.

        NOTE: there is no ``structured_content`` filter (JMFTS-INTEGRATION-PLAN.md
        CR-2 is not yet landed) -- cwd/hostname-scoped discovery stays
        client-side. ``parent_id`` here is an equality filter, not a "roots
        only" flag -- omit it (leave as ``None``) to match any parent; use
        ``get_roots()`` for roots specifically.

        **PAGINATED -- a full page is not proof of a complete answer.** This returns
        at most ``limit`` rows and says nothing about how many more exist; a caller
        that ignores that gets a silently truncated result, which for the session
        catalog would mean "your older conversations vanished from the picker". The
        API offers no total count, so the only sound reading is: a response of
        exactly ``limit`` rows means MORE MAY EXIST -- keep paging on ``offset``
        until a short page comes back. Callers that need every match must page;
        they must not pass a hopefully-big-enough ``limit``.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if parent_id is not None:
            params["parent_id"] = parent_id
        if usetype is not None:
            params["usetype"] = usetype
        if title_prefix is not None:
            params["title_prefix"] = title_prefix
        result: list[DocumentDict] = self._request("GET", "/documents", params=params)
        return result

    def get_roots(self) -> list[DocumentDict]:
        """``GET /documents/roots`` -- ALL root documents (no parent), unfiltered.

        The live server offers no ``usetype`` (or any other) query parameter
        on this endpoint -- confirmed against the openapi schema and the
        running instance. To find conversation roots specifically, filter
        client-side:
        ``[d for d in client.get_roots() if d["usetype"] == "tau:conversation"]``,
        or prefer ``list_documents(usetype="tau:conversation")`` for large
        corpora since that endpoint at least narrows by usetype server-side
        (then check ``d["parent_id"] is None`` client-side to confirm root-ness).
        """
        result: list[DocumentDict] = self._request("GET", "/documents/roots")
        return result

    # -- Enrichment: embedding (W13) -----------------------------------------

    def embed_document(self, doc_id: int, *, with_tokens: bool = True) -> dict[str, Any]:
        """``POST /documents/{id}/embed`` -- embed a document written with ``auto_embed=False``.

        τ's write path never embeds (every ``create_document`` here passes
        ``auto_embed=False``), because embedding is a GPU forward pass and the hot
        append path must not pay for it. This is the deferred half.

        **Idempotent, and safe to replay.** The server deletes existing token
        embeddings before re-inserting and overwrites ``doc.embed``, so calling this
        twice costs GPU time but cannot corrupt anything. (:meth:`index_document_into`
        is likewise replay-safe since the D3 server fix.)

        **The server REFUSES over-window text with HTTP 400** -- ``embedding.py``'s
        512-token window (~2000 chars) is now enforced as a hard limit, so handing a
        long document straight to this method is a guaranteed round-trip failure rather
        than a silently truncated prefix. Chunk first to avoid it: use
        :meth:`chunk_document` (see :data:`EMBED_MAX_CHARS`). (Before the D1 server fix
        this method silently truncated and lost the tail -- see docs/KNOWN-DEFECTS.md.)

        Raises ``JmftsError`` 404 if the document does not exist OR has no content --
        a ``content=None`` document (τ's ``navigate`` / config entries) can never be
        embedded, which is correct: they have no searchable text.
        """
        result: dict[str, Any] = self._request(
            "POST", f"/documents/{doc_id}/embed", params={"with_tokens": with_tokens}
        )
        return result

    def is_embedded(self, doc_id: int) -> bool:
        """Whether ``doc_id`` already has a document embedding.

        JMFTS exposes **no embedding-status field and no way to query for un-embedded
        documents** -- the list endpoint always returns ``embed=None`` regardless. The
        only sound check is fetching the document with the vector attached and seeing
        whether it is there, which is why this costs a request (and ~10KB of floats)
        per document. It is still far cheaper than an unnecessary GPU embed, which is
        what makes an enrichment pass resumable instead of all-or-nothing.
        """
        doc = self.get_document(doc_id, include_embed=True)
        return doc.get("embed") is not None

    def chunk_document(
        self,
        doc_id: int,
        *,
        max_tokens: int = CHUNK_MAX_WORDS,
        overlap: int = 40,
        child_usetype: str = CHUNK_USETYPE,
    ) -> list[DocumentDict]:
        """``POST /documents/{id}/chunk`` -- split a long document into embeddable children.

        The answer to :meth:`embed_document`'s 512-token truncation: rather than embed a
        prefix and silently lose the tail, cut the text into pieces that each fit, and
        embed those. The parent keeps its full ``content`` verbatim -- nothing is
        destroyed; the chunks are simply what vector search matches on. Returns the
        chunk documents.

        The server now caps EVERY strategy to ``max_tokens`` and bounds the merge step,
        so no strategy pin is needed: the default strategy returns bounded chunks (the
        D2 server fix). Earlier this method pinned ``strategy="token_count"`` and passed
        ``min_chunk_length=1`` to disable the unbounded merge -- both were load-bearing
        workarounds and are no longer required.

        ``max_tokens`` counts WORDS, not subword tokens (the server uses whitespace
        splitting as a proxy). :data:`CHUNK_MAX_WORDS` is set well under 512 to leave
        room for both the subword expansion ratio and the ``"search_document: "`` prefix
        the embedder prepends.

        **Chunks come back UNEMBEDDED** (``auto_embed=False``): the enrichment pass
        embeds them in a separate step so that embedding a chunk that failed to fit
        would surface as a loud HTTP 400 rather than being folded into the chunk write.
        The server hard-splits any whitespace-free chunk still over the window (the D4
        fix), so every returned chunk is guaranteed embeddable; a 400 is the loud
        backstop if that guarantee is ever violated.

        ``child_usetype`` **must not begin with** ``tau:``. Chunks land as CHILD
        DOCUMENTS inside the conversation subtree, and τ's loader classifies anything in
        there by ``_is_tau_doc`` -- a ``tau:`` usetype without a
        ``structured_content.tau`` payload would be read as a malformed τ entry and
        break the load path. Under a foreign usetype they are correctly seen as foreign
        documents: present, inert, excluded from cursor resolution and the context fold
        (Sec2.4).
        """
        if child_usetype.startswith("tau:"):
            raise ValueError(
                f"chunk_document: child_usetype {child_usetype!r} must not start with 'tau:' -- "
                "chunks are foreign documents inside the conversation subtree, and a tau: "
                "usetype without a structured_content.tau payload would corrupt the load path"
            )
        self._request(
            "POST",
            f"/documents/{doc_id}/chunk",
            json={
                "max_tokens": max_tokens,
                "overlap": overlap,
                "child_usetype": child_usetype,
                "auto_embed": False,
            },
        )
        return self.get_children(doc_id, usetype=child_usetype, limit=1000)

    # -- Enrichment: BM25 indexes (W13) ---------------------------------------

    def list_indexes(self) -> list[dict[str, Any]]:
        """``GET /indexes``."""
        result: list[dict[str, Any]] = self._request("GET", "/indexes")
        return result

    def create_index(
        self, name: str, *, description: str | None = None, config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """``POST /indexes`` -- raises ``JmftsError`` 409 if the name is taken."""
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        if config is not None:
            body["config"] = config
        result: dict[str, Any] = self._request("POST", "/indexes", json=body)
        return result

    def get_index_roots(self, index_name: str) -> list[int]:
        """``GET /indexes/{name}/roots`` -- the document ids registered as index roots."""
        result: dict[str, Any] = self._request("GET", f"/indexes/{index_name}/roots")
        roots: list[int] = [int(r) for r in result.get("roots", [])]
        return roots

    def add_index_root(self, index_name: str, root_document_id: int) -> dict[str, Any]:
        """``POST /indexes/{name}/roots`` -- register a subtree for indexing.

        **This does not index anything.** It only records membership; the postings are
        built by :meth:`refresh_index`. Registering a root and forgetting to refresh
        yields an index that is silently empty for that subtree.
        """
        result: dict[str, Any] = self._request(
            "POST",
            f"/indexes/{index_name}/roots",
            params={"root_document_id": root_document_id},
        )
        return result

    def refresh_index(self, index_name: str) -> dict[str, Any]:
        """``POST /indexes/{name}/refresh`` -- rebuild the whole index from its roots.

        Replay-safe: it resets ``total_docs`` / ``avg_doc_length`` to 0, deletes the
        postings, and re-indexes every registered root's subtree, deduplicated by
        document id -- so running it twice is a no-op, and a crash halfway through is
        fixed by running it again.

        Cost is O(all registered subtrees), not O(what changed), so it grows with the
        corpus rather than with the session. For incremental indexing at O(new docs),
        prefer :meth:`index_document_into` -- now idempotent (the D3 fix), so it too is
        safe to replay. ``refresh`` remains the way to rebuild a whole index from
        scratch (e.g. after a config change).
        """
        result: dict[str, Any] = self._request("POST", f"/indexes/{index_name}/refresh")
        return result

    def index_document_into(self, index_name: str, doc_id: int) -> dict[str, Any]:
        """``POST /indexes/{name}/index-document/{id}`` -- index ONE document.

        **Idempotent, and safe to replay** (the D3 server fix). The server checks for an
        existing index entry and, if present, subtracts the document's old contribution
        before adding the new one (short-circuiting on an unchanged ``content_hash``), so
        ``index.total_docs`` and per-term ``doc_freq`` no longer double-count when the
        same document is indexed twice. Re-running is a no-op for BM25 statistics.

        This is what makes it the incremental step of a resumable enrichment pass: a pass
        that crashes and re-runs re-indexes only the documents it touched, at O(new docs)
        rather than the O(entire corpus) cost of a full :meth:`refresh_index`. τ's
        ``enrich`` extension uses this per-document call.

        (Before the D3 fix this endpoint incremented the statistics unconditionally,
        corrupting BM25 IDF/length-normalization permanently -- see
        docs/KNOWN-DEFECTS.md. That is why enrichment previously used root-registration +
        :meth:`refresh_index` instead.)
        """
        result: dict[str, Any] = self._request(
            "POST", f"/indexes/{index_name}/index-document/{doc_id}"
        )
        return result

    # -- Search (W15) ---------------------------------------------------------

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
        """Search, returning the raw hit list (``{document, score, method}``).

        ``method`` is one of ``hybrid`` | ``vector`` | ``bm25`` | ``fulltext`` |
        ``maxsim``, each a distinct ``POST /search/{method}`` endpoint.

        ``parent_id`` is a **subtree** filter, not an immediate-parent one: the server
        matches on the document ``path``, so passing a conversation root scopes the
        search to everything under it. This is what makes "search only this
        conversation" possible.

        ``usetype`` supports globs (``"tau:*"``), and setting it OVERRIDES the server's
        default ``exclude_types`` of ``["entity", "summary"]``.

        **``/search/auto`` is deliberately not offered.** Its router picks a method per
        query, and the branch that picks ``maxsim`` fails to forward ``parent_id`` --
        so a scoped search would silently widen to the entire corpus and return hits
        from other people's conversations while still looking like it worked. A scope
        that holds only for some inputs is not a scope. Likewise ``rerank`` is not
        exposed: JMFTS's own docs record the cross-encoder as quality-broken.
        """
        if method not in _SEARCH_METHODS:
            raise ValueError(
                f"search: unknown method {method!r}; expected one of {sorted(_SEARCH_METHODS)}"
            )
        body: dict[str, Any] = {"query": query, "limit": limit}
        if parent_id is not None:
            body["parent_id"] = parent_id
        if usetype is not None:
            body["usetype"] = usetype
        if exclude_types is not None:
            body["exclude_types"] = exclude_types
        if method == "hybrid":
            body["index_name"] = index_name

        params = {"index_name": index_name} if method == "bm25" else None
        response: dict[str, Any] = self._request(
            "POST", f"/search/{method}", json=body, params=params
        )
        hits: list[dict[str, Any]] = response.get("results", [])
        return hits

    def get_ancestors(self, doc_id: int) -> list[DocumentDict]:
        """``GET /documents/{id}/ancestors`` -- root-first breadcrumbs, for citing a hit."""
        result: list[DocumentDict] = self._request("GET", f"/documents/{doc_id}/ancestors")
        return result
