"""W13 — deferred enrichment: make a conversation FINDABLE (JMFTS-INTEGRATION-PLAN.md Sec6).

τ's write path never embeds. Every ``create_document`` in ``store.py`` passes
``auto_embed=False``, because embedding is a GPU forward pass and an append must not
wait on one. That is the right call for the hot path — and it means that, until this
extension runs, **τ conversations sit in a retrieval appliance that cannot retrieve
them.** This is the deferred half of that bargain.

On ``session_shutdown`` it does two things to the conversation's document subtree:

1. **Embed** every τ entry that carries text. Anything longer than the embedder's
   window is CHUNKED rather than embedded whole (see below).
2. **Index** the conversation root into a BM25 index, if one is configured.

Both are idempotent and resumable, which is not a nicety — a session that crashes
mid-enrichment must be fixable by running it again, and re-running must not corrupt
anything.

Configuration (``"extensions": {"enrich": {...}}``)::

    {"index": "tau", "embed": true}

``index: null`` (the default) skips BM25 indexing and does embeddings only — vector
and maxsim search still work; only BM25/hybrid need the index.

---

**Two former traps, both now fixed server-side** (jmfts ``280d650``; see
``docs/KNOWN-DEFECTS.md``). This pass once worked around them client-side; those
workarounds are reverted.

Incremental indexing (``index-document``) is now safe. The server's ``index_document``
is idempotent: it subtracts a document's old contribution before re-adding it, so
indexing the same document twice no longer inflates the BM25 collection statistics
(``total_docs`` / ``doc_freq``). This pass therefore indexes each of the conversation's
documents incrementally, at O(new docs) — re-runs are no-ops. (It previously used
root-registration + a full ``refresh`` at O(corpus), the only replay-safe option before
the fix.)

Embedding a long document whole now fails LOUDLY: the embedder refuses over-window text
with HTTP 400 rather than truncating and dropping the tail. Long entries are still
chunked first (``JmftsClient.chunk_document``) to avoid a guaranteed 400 — and the
server bounds every chunk and hard-splits whitespace-free ones, so each chunk fits.
"""

from __future__ import annotations

from typing import Any

from tau_jmfts.client import CHUNK_USETYPE, EMBED_MAX_CHARS, JmftsClient

# Only these τ entry kinds carry text worth embedding. `navigate` / `model_change` /
# `session_info` project to an empty `content` (store._content_for), and the server
# 404s on embedding a document with no content — correctly, since there is nothing to
# search. Chunk documents are embedded by the chunker itself (auto_embed=True).
EMBEDDABLE_USETYPES = ("tau:message", "tau:compaction", "tau:branch_summary", "tau:customMessage")


class EnrichmentReport:
    """What one pass actually did — so the result is inspectable, not a claim."""

    def __init__(self) -> None:
        self.embedded: list[int] = []
        self.chunked: list[int] = []
        self.already_done: list[int] = []
        self.skipped_empty: list[int] = []
        self.indexed_into: str | None = None
        self.indexed_docs: int = 0

    def summary(self) -> str:
        parts = [
            f"{len(self.embedded)} embedded",
            f"{len(self.chunked)} chunked",
            f"{len(self.already_done)} already done",
        ]
        if self.indexed_into:
            parts.append(f"indexed {self.indexed_docs} docs into {self.indexed_into!r}")
        return ", ".join(parts)


def enrich_conversation(
    client: JmftsClient,
    root_doc_id: int,
    *,
    index: str | None = None,
    embed: bool = True,
    max_chars: int = EMBED_MAX_CHARS,
) -> EnrichmentReport:
    """Embed + index one conversation subtree. Idempotent; safe to re-run.

    Resumability is DERIVED from the server's own state, never from a progress marker
    we store: "is this embedded?" is a question JMFTS can answer, and "does this
    document already have chunk children?" likewise. A stored marker would be a second
    source of truth that can disagree with reality after a crash — precisely when it
    is relied upon.
    """
    report = EnrichmentReport()

    if embed:
        for doc in _entry_documents(client, root_doc_id):
            doc_id = doc["id"]
            content = doc.get("content") or ""
            if len(content.strip()) <= 10:
                # The server refuses to embed content this short (`len(content) > 10`),
                # and it is right to: there is nothing to match on.
                report.skipped_empty.append(doc_id)
            elif len(content) > max_chars:
                _enrich_long_document(client, doc_id, report)
            elif client.is_embedded(doc_id):
                report.already_done.append(doc_id)
            else:
                client.embed_document(doc_id)
                report.embedded.append(doc_id)

    if index is not None:
        _ensure_index(client, index)
        # index_document is idempotent server-side (the D3 fix), so indexing each of the
        # conversation's own entry documents is safe to replay: a re-run re-indexes the
        # same set at O(new docs) and leaves the BM25 collection statistics unchanged.
        # (This replaced root-registration + a full refresh, which was O(corpus) but the
        # only replay-safe option before the fix — see the module docstring.) The parent
        # entries carry the full text, so indexing them covers all lexical content; the
        # chunk children exist for vector search and need no separate BM25 posting.
        entry_docs = _entry_documents(client, root_doc_id)
        for doc in entry_docs:
            client.index_document_into(index, doc["id"])
        report.indexed_into = index
        report.indexed_docs = len(entry_docs)

    return report


def _enrich_long_document(client: JmftsClient, doc_id: int, report: EnrichmentReport) -> None:
    """A document too long to embed whole: chunk it, then embed the chunks.

    Embedding it as-is would 400 (the server refuses over-window text since the D1 fix),
    so it is chunked first into pieces that each fit the embedder's window.

    Chunking is done ONCE — re-chunking would mint a second full set of chunk documents,
    doubling this message's weight in every future search result. Existing chunks are
    the marker that a previous pass got here, but the pass still walks them, because a
    run that chunked and then died before embedding must be completable by re-running.
    That is the difference between "resumable" and "runs twice without crashing".
    """
    chunks = client.get_children(doc_id, usetype=CHUNK_USETYPE, limit=1000)
    if chunks:
        report.already_done.append(doc_id)
    else:
        chunks = client.chunk_document(doc_id)
        report.chunked.append(doc_id)

    for chunk in chunks:
        chunk_id = chunk["id"]
        # Every chunk is embeddable: the server bounds each chunk to the window and
        # hard-splits whitespace-free ones (the D2/D4 fixes), so there is nothing to
        # re-verify client-side. If that guarantee were ever violated, embed_document
        # would raise a loud HTTP 400 rather than storing a truncated prefix.
        if client.is_embedded(chunk_id):
            report.already_done.append(chunk_id)
        else:
            client.embed_document(chunk_id)
            report.embedded.append(chunk_id)


def _entry_documents(client: JmftsClient, root_doc_id: int) -> list[dict[str, Any]]:
    """Every τ entry document under the conversation root that could carry text.

    ``depth=-1`` walks the whole subtree. Chunk documents are excluded by usetype —
    they are already embedded by the chunker, and feeding them back in would re-embed
    them on every pass.
    """
    docs: list[dict[str, Any]] = []
    for usetype in EMBEDDABLE_USETYPES:
        docs.extend(client.get_children(root_doc_id, usetype=usetype, depth=-1, limit=1000))
    return docs


def _ensure_index(client: JmftsClient, name: str) -> None:
    """Create the index if absent. A 409 means someone else just created it, which is
    the outcome we wanted — but any OTHER error is a real failure and must not be
    swallowed into "probably fine"."""
    from tau_jmfts.client import JmftsError

    existing = {i["name"] for i in client.list_indexes()}
    if name in existing:
        return
    try:
        client.create_index(name, description="τ conversations")
    except JmftsError as exc:
        if exc.status_code != 409:
            raise


def register(api: Any) -> None:
    """Wire the pass to ``session_shutdown``."""
    cfg = api.config
    index = cfg.get("index")
    embed = bool(cfg.get("embed", True))

    async def on_session_shutdown(event: dict[str, Any], ctx: Any) -> None:
        log = ctx._require_session().session_log
        # Fail-Early: this extension's whole job is to enrich documents in JMFTS. On a
        # file-backed session there are none. Silently doing nothing would be the worst
        # outcome — the user would believe their conversations were being made
        # searchable, and only discover otherwise when a search came back empty.
        if not hasattr(log, "root_doc_id"):
            raise RuntimeError(
                "enrich: the active session is not JMFTS-backed "
                f"({type(log).__name__}), so there is nothing to embed or index. "
                "Run with --store jmfts, or unload this extension."
            )

        report = enrich_conversation(log.client, log.root_doc_id, index=index, embed=embed)
        ctx.ui.notify(f"enrich: {report.summary()}")

    api.on("session_shutdown", on_session_shutdown)
