"""W15 — agent-facing retrieval: ``jmfts_search`` / ``jmfts_read`` / ``jmfts_ingest``.

The payoff of the whole JMFTS integration. Everything before this made conversations
*storable* and (with :mod:`tau_jmfts.ext.enrich`) *findable*; this is what lets the
agent actually find them.

Works regardless of which session store is active — a file-backed session can still
search JMFTS. Only the ``scope="conversation"`` shorthand needs a JMFTS-backed
session, and it says so rather than quietly searching everything.

Configuration (``"extensions": {"tools": {...}}``)::

    {"url": "http://…:8007", "index": "tau", "default_scope": "conversation"}

``url`` may be omitted when the active session is JMFTS-backed: the tools then reuse
that session's own client, so they cannot end up querying a *different* instance than
the one holding the conversation.

---

**On tree-as-truth.** τ's invariant is that the model's input for a call is exactly
the inspectable path through the entry tree — no hidden channels. Retrieval is where
that invariant usually dies: the standard RAG move is to staple retrieved chunks into
the prompt behind the user's back, so what the model actually read is unrecoverable
afterwards.

Doing retrieval as a TOOL keeps the invariant for free, and that is the reason to
prefer it here. A ``jmfts_search`` call and its results are a ``toolCall`` /
``toolResult`` pair — real entries, on the path, persisted, visible in the tree
browser, forkable, and compacted like anything else. What the model retrieved is
exactly what the transcript says it retrieved. Nothing is injected; nothing is
invisible; there is no second channel to audit.
"""

from __future__ import annotations

import os
from typing import Any

from tau_jmfts.client import EMBED_MAX_CHARS, JmftsClient

_SCOPES = ("conversation", "all", "subtree")

SEARCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to search for."},
        "scope": {
            "type": "string",
            "enum": list(_SCOPES),
            "description": (
                "'conversation' = only this conversation's own history; "
                "'subtree' = only under doc_id (requires doc_id); "
                "'all' = the entire JMFTS corpus."
            ),
        },
        "doc_id": {
            "type": "integer",
            "description": "Root document id to scope to. Required when scope='subtree'.",
        },
        "method": {
            "type": "string",
            "enum": ["hybrid", "vector", "bm25", "fulltext"],
            "description": "Retrieval method. 'hybrid' unless you have a reason.",
        },
        "usetype": {
            "type": "string",
            "description": "Filter by document usetype; globs allowed (e.g. 'tau:*').",
        },
        "limit": {"type": "integer", "description": "Max hits (default 8)."},
    },
    "required": ["query"],
}

READ_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc_id": {"type": "integer", "description": "The document id to read."},
        "children": {
            "type": "boolean",
            "description": "Also list the document's immediate children (default false).",
        },
    },
    "required": ["doc_id"],
}

INGEST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "A short title for the document."},
        "content": {"type": "string", "description": "The document's full text."},
        "parent_id": {
            "type": "integer",
            "description": "File it under this document. Omit to create a new root.",
        },
        "usetype": {
            "type": "string",
            "description": "Document usetype (default 'note'). Must not start with 'tau:'.",
        },
    },
    "required": ["title", "content"],
}


def _text(body: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": body}]}
    if details is not None:
        result["details"] = details
    return result


def _format_hits(hits: list[dict[str, Any]], *, max_chars: int = 900) -> str:
    """Render hits so the model can CITE them.

    The doc id leads every hit, because a hit the model cannot cite is a hit it can
    only paraphrase — and a paraphrase with no id is indistinguishable from something
    it made up. `jmfts_read` takes that id back.
    """
    if not hits:
        return "No results."
    lines = []
    for hit in hits:
        doc = hit["document"]
        content = (doc.get("content") or "").strip().replace("\n", " ")
        if len(content) > max_chars:
            # Say so, rather than letting a clean-looking cut read as the whole thing.
            content = content[:max_chars] + f"… [truncated; read doc {doc['id']} for the rest]"
        lines.append(
            f"[doc {doc['id']}] {doc.get('title') or '(untitled)'} "
            f"(usetype={doc.get('usetype')}, score={hit['score']:.3f})\n{content}"
        )
    return "\n\n".join(lines)


def _conversation_root(ctx: Any) -> int | None:
    log = ctx._require_session().session_log
    root = getattr(log, "root_doc_id", None)
    return int(root) if root is not None else None


def register(api: Any) -> None:
    cfg = api.config
    url = cfg.get("url")
    index = cfg.get("index", "default")
    default_scope = cfg.get("default_scope", "conversation")
    # CR-4: shared-bearer token for the config-built (non-borrowed) fallback
    # client. Config first, then $JMFTS_API_TOKEN. Not defaulted (Fail-Early):
    # a missing token means the fallback client 401s loudly against an auth'd
    # server. The primary path borrows the session's already-authenticated
    # client and never reaches this.
    token = cfg.get("token") or os.environ.get("JMFTS_API_TOKEN")

    def _client(ctx: Any) -> JmftsClient:
        """Prefer the ACTIVE SESSION's own client. Constructing a second one from config
        risks pointing at a different JMFTS instance than the one holding the
        conversation — at which case scope='conversation' would scope to a doc id that
        means something else entirely on that server."""
        log = ctx._require_session().session_log
        client = getattr(log, "client", None)
        if isinstance(client, JmftsClient):
            return client
        if not url:
            raise RuntimeError(
                "jmfts tools: the active session is not JMFTS-backed, so there is no "
                "client to borrow, and no 'url' is configured. Set "
                "'extensions.tools.url' to your JMFTS instance."
            )
        return JmftsClient(str(url), token=token)

    async def search(
        tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
    ) -> dict[str, Any]:
        client = _client(ctx)
        query = params["query"]
        scope = params.get("scope", default_scope)
        if scope not in _SCOPES:
            raise ValueError(f"jmfts_search: unknown scope {scope!r}; expected one of {_SCOPES}")

        parent_id: int | None = None
        if scope == "conversation":
            parent_id = _conversation_root(ctx)
            if parent_id is None:
                # Fail-Early. Falling back to an unscoped search would answer a
                # DIFFERENT question than the one asked — "anything, anywhere" instead
                # of "in this conversation" — and would look like it worked.
                raise RuntimeError(
                    "jmfts_search(scope='conversation'): this session is not JMFTS-backed, "
                    "so it has no conversation subtree to search. Use scope='all' or "
                    "scope='subtree' with an explicit doc_id."
                )
        elif scope == "subtree":
            if params.get("doc_id") is None:
                raise ValueError("jmfts_search(scope='subtree') requires doc_id")
            parent_id = int(params["doc_id"])

        hits = client.search(
            query,
            method=params.get("method", "hybrid"),
            limit=int(params.get("limit", 8)),
            parent_id=parent_id,
            usetype=params.get("usetype"),
            index_name=str(index),
        )
        return _text(
            _format_hits(hits),
            {"query": query, "scope": scope, "parent_id": parent_id, "hits": len(hits)},
        )

    async def read(
        tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
    ) -> dict[str, Any]:
        client = _client(ctx)
        doc_id = int(params["doc_id"])
        doc = client.get_document(doc_id)

        body = [
            f"[doc {doc['id']}] {doc.get('title') or '(untitled)'} "
            f"(usetype={doc.get('usetype')}, parent={doc.get('parent_id')})",
            "",
            doc.get("content") or "(no content)",
        ]
        if params.get("children"):
            kids = client.get_children(doc_id, limit=100)
            if kids:
                body.append("")
                body.append("Children:")
                body.extend(
                    f"  [doc {k['id']}] {k.get('title') or '(untitled)'} ({k.get('usetype')})"
                    for k in kids
                )
        return _text("\n".join(body), {"doc_id": doc_id})

    async def ingest(
        tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
    ) -> dict[str, Any]:
        client = _client(ctx)
        usetype = str(params.get("usetype") or "note")
        if usetype.startswith("tau:"):
            # `tau:*` is τ's own namespace: the loader reads those documents as
            # conversation ENTRIES and expects a structured_content.tau payload. Letting
            # the agent mint one would let it forge entries into a conversation tree.
            raise ValueError(
                f"jmfts_ingest: usetype {usetype!r} must not start with 'tau:' — that "
                "namespace belongs to τ's own conversation entries."
            )
        content = str(params["content"])
        # The embedder 400s on over-window content (the D1 server fix), so an oversized
        # ingest with auto_embed=True would fail the write. Mirror enrich's chunk-to-fit
        # path: for content past EMBED_MAX_CHARS, write the parent UNembedded and make it
        # findable by chunking it and embedding the (server-bounded) chunks. The parent
        # keeps its full text for BM25. Short content still embeds inline.
        oversized = len(content) > EMBED_MAX_CHARS
        doc = client.create_document(
            title=str(params["title"]),
            content=content,
            parent_id=int(params["parent_id"]) if params.get("parent_id") is not None else None,
            usetype=usetype,
            structured_content={
                "ingested_by": "tau",
                # Provenance: which conversation filed this. The τ session id (stable
                # across stores), not the JMFTS doc id.
                "session": ctx._require_session().session_log.id,
            },
            # Embed on the way in: an ingested document nobody can find is a write to
            # /dev/null. This is not the hot path — the agent is already waiting on a
            # tool call — so there is no reason to defer it the way the session write
            # path must. Oversized content is embedded via its chunks instead (below).
            auto_embed=not oversized,
        )
        if oversized:
            for chunk in client.chunk_document(doc["id"]):
                client.embed_document(chunk["id"])
        return _text(
            f"Ingested as doc {doc['id']} (usetype={usetype}).",
            {"doc_id": doc["id"], "usetype": usetype},
        )

    api.register_tool(
        {
            "name": "jmfts_search",
            "label": "JMFTS search",
            "description": (
                "Search the JMFTS knowledge store — including this conversation's own "
                "history. Returns hits with document ids you can pass to jmfts_read."
            ),
            "parameters": SEARCH_PARAMETERS,
            "execute": search,
            "prompt_snippet": "jmfts_search: retrieve from the knowledge store / past conversation",
            "execution_mode": "parallel",
        }
    )
    api.register_tool(
        {
            "name": "jmfts_read",
            "label": "JMFTS read",
            "description": "Read one JMFTS document by id (optionally with its children).",
            "parameters": READ_PARAMETERS,
            "execute": read,
            "prompt_snippet": "jmfts_read: read a document by id",
            "execution_mode": "parallel",
        }
    )
    api.register_tool(
        {
            "name": "jmfts_ingest",
            "label": "JMFTS ingest",
            "description": "File a new document into the JMFTS knowledge store.",
            "parameters": INGEST_PARAMETERS,
            "execute": ingest,
            "prompt_snippet": "jmfts_ingest: file a document into the knowledge store",
            "execution_mode": "sequential",
        }
    )
