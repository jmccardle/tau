"""W13 enrich: a τ conversation becomes FINDABLE (JMFTS-INTEGRATION-PLAN.md Phase 4).

Live, against the real server, because every interesting property here is a property
of JMFTS's actual behaviour: what its embedder truncates, what its chunker refuses to
split, what its BM25 statistics do when you index a document twice. A mock would
assert my beliefs about those, and my beliefs about two of them were wrong.

Marker: ``jmfts``.
"""

from __future__ import annotations

import base64
import os
import uuid
from typing import Any

import pytest

from tau_jmfts.catalog import JmftsSessionCatalog
from tau_jmfts.client import CHUNK_USETYPE, JmftsClient, JmftsError
from tau_jmfts.ext.enrich import enrich_conversation

pytestmark = pytest.mark.jmfts

TEST_PREFIX = "tau-jmfts-test"


@pytest.fixture
def client(jmfts_url: str, jmfts_token: str | None):
    c = JmftsClient(jmfts_url, token=jmfts_token)
    yield c
    c.close()


@pytest.fixture
def catalog(client: JmftsClient) -> JmftsSessionCatalog:
    return JmftsSessionCatalog(client)


@pytest.fixture
def run_id() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def index_name(client: JmftsClient, run_id: str):
    name = f"{TEST_PREFIX}-idx-{run_id}"
    yield name
    try:
        client._request("DELETE", f"/indexes/{name}")
    except JmftsError:
        pass


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _session(catalog: JmftsSessionCatalog, run_id: str):
    return catalog.create(
        f"/tmp/{TEST_PREFIX}-{run_id}", "test-model", "test-backend", system_prompt="sys"
    )


LONG_ANSWER = (
    "The liveness probe fires before the JVM finishes warming up, so kubelet restarts "
    "the pod. Add a startupProbe with a generous failureThreshold; liveness only begins "
    "after the startup probe passes. " * 12
)


def test_an_enriched_conversation_becomes_semantically_searchable(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str, index_name: str
) -> None:
    """The exit criterion of the whole JMFTS integration, and the thing that was NOT
    true before this extension existed: τ wrote conversations into a retrieval appliance
    that could not retrieve them (every write passes auto_embed=False).

    The query deliberately shares almost no words with the answer -- "boots" vs
    "warming up", "restarting" vs "restarts" -- so a lexical match cannot explain a hit.
    """
    session = _session(catalog, run_id)
    try:
        session.append_message(_msg("user", "Why is our pod getting killed on startup?"))
        session.append_message(_msg("assistant", LONG_ANSWER))
        session.append_message(_msg("user", "And the Redis connection pool exhaustion?"))
        session.append_message(
            _msg("assistant", "Raise maxTotal and set testOnBorrow; an unclosed Jedis leaked.")
        )

        report = enrich_conversation(client, session.root_doc_id, index=index_name)
        assert report.embedded, "nothing was embedded — the conversation is still unsearchable"

        hits = client.search(
            "container keeps rebooting while it is still coming up",
            method="vector",
            parent_id=session.root_doc_id,
            limit=5,
        )
        texts = " ".join((h["document"].get("content") or "") for h in hits)
        assert "startupProbe" in texts, "semantic retrieval did not surface the relevant answer"
    finally:
        catalog.delete(str(session.root_doc_id))


def test_the_search_is_scoped_to_this_conversation(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    """``parent_id`` is a subtree filter. Without it, "search my conversation" silently
    becomes "search everyone's", which returns confident answers from the wrong
    conversation."""
    mine = _session(catalog, run_id)
    theirs = _session(catalog, run_id + "b")
    try:
        secret = f"quokka-{run_id}"  # a token that exists in exactly one conversation
        theirs.append_message(_msg("assistant", f"The deployment codename is {secret}."))
        mine.append_message(_msg("assistant", "This conversation is about something else."))

        enrich_conversation(client, mine.root_doc_id)
        enrich_conversation(client, theirs.root_doc_id)

        scoped = client.search(
            "deployment codename", method="vector", parent_id=mine.root_doc_id, limit=10
        )
        ids = {h["document"]["id"] for h in scoped}
        their_docs = {d["id"] for d in client.get_children(theirs.root_doc_id, depth=-1)}
        assert not (ids & their_docs), "the scoped search leaked into another conversation"
    finally:
        catalog.delete(str(mine.root_doc_id))
        catalog.delete(str(theirs.root_doc_id))


def test_a_long_message_is_chunked_so_its_TAIL_is_searchable_too(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    """The embedder truncates at 512 tokens and drops the rest with NO error, so a long
    message embedded whole is searchable only by its opening. Chunking is what makes the
    back half exist; every chunk must fit, or it is the same bug again one level down."""
    session = _session(catalog, run_id)
    try:
        tail_marker = f"the final clause mentions {run_id} explicitly"
        session.append_message(_msg("assistant", LONG_ANSWER + " " + tail_marker))

        report = enrich_conversation(client, session.root_doc_id)
        assert report.chunked, "a message well past the embed window was not chunked"

        chunks = client.get_children(report.chunked[0], usetype=CHUNK_USETYPE, limit=100)
        assert len(chunks) > 1
        # "It fits" is a token fact, so assert the token fact: an embedded chunk is one
        # the server measured and accepted. A character bound would only assert τ's old
        # guess about the exchange rate.
        assert all(client.is_embedded(c["id"]) for c in chunks), (
            "a chunk was not embedded — it did not fit the embedder's window, which is "
            "the bug chunking exists to prevent"
        )
        assert any(tail_marker in (c["content"] or "") for c in chunks), "the tail was lost"
    finally:
        catalog.delete(str(session.root_doc_id))


def test_text_with_no_word_boundaries_is_split_and_embedded(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    """A base64 blob (or minified JS, or a long path) is ONE enormous "word" that a
    word-based chunker cannot bound, AND it tokenises far denser than the ~3.5 chars per
    token that prose does. Both facts are invisible to a character count. The pieces have
    to be held to the embedder's real window -- which the server will do, but only when
    asked to chunk FOR embedding -- so every piece is embedded rather than refused,
    silently truncated to a prefix, or reported as unembeddable (that client-side
    workaround is reverted; the field no longer exists).
    """
    session = _session(catalog, run_id)
    try:
        blob = base64.b64encode(os.urandom(4000)).decode()
        session.append_message(_msg("assistant", "Here is the dump: " + blob))

        report = enrich_conversation(client, session.root_doc_id)

        assert report.chunked, "the oversized blob message was not chunked"
        assert not hasattr(report, "unembeddable"), "the reverted unembeddable field is back"

        chunks = client.get_children(report.chunked[0], usetype=CHUNK_USETYPE, limit=100)
        assert len(chunks) > 1, "the whitespace-free blob was not split"
        assert all(client.is_embedded(c["id"]) for c in chunks), (
            "a chunk was not embedded -- its content is not vector-searchable. A chunk "
            "bounded in CHARACTERS is not bounded in tokens: 1800 chars of base64 is "
            "~1350 tokens, well past the 512-token window."
        )
    finally:
        catalog.delete(str(session.root_doc_id))


def test_dense_content_short_enough_for_prose_is_still_over_the_token_window(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    """The bug a character threshold cannot see, and the one prose hides.

    τ decided "does this fit the embedder?" with ``len(content) > EMBED_MAX_CHARS``,
    1800. The embedder's limit is 512 TOKENS. At English prose's ~3.5 chars/token the
    two agree closely enough to look correct; base64 runs ~1.35 chars/token, so a blob
    of 1800 chars -- one character UNDER the threshold, so routed down the "short enough
    to embed whole" path -- is ~1350 tokens and the server refuses it outright.

    This message is deliberately just inside the old threshold. Anything that decides by
    counting characters fails here; only asking the server, which owns the tokenizer,
    gets it right.
    """
    session = _session(catalog, run_id)
    try:
        blob = base64.b64encode(os.urandom(2048)).decode()[:1800]
        assert len(blob) == 1800, "the fixture must sit just inside the old 1800-char proxy"
        session.append_message(_msg("assistant", blob))

        report = enrich_conversation(client, session.root_doc_id)

        assert report.chunked, (
            "dense content under the old character threshold was embedded whole -- a "
            "character count decided a token question"
        )
        chunks = client.get_children(report.chunked[0], usetype=CHUNK_USETYPE, limit=100)
        assert chunks, "the refused document produced no chunks"
        assert all(client.is_embedded(c["id"]) for c in chunks), (
            "a chunk was not embedded -- the blob is still not vector-searchable"
        )
    finally:
        catalog.delete(str(session.root_doc_id))


def test_enrichment_is_idempotent(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str, index_name: str
) -> None:
    """Re-running must not re-embed, must not RE-CHUNK (which would mint a second full
    set of chunks and double the message's weight in every future search), and must not
    inflate the BM25 statistics. Enrichment indexes each document incrementally via the
    now-idempotent index-document endpoint (the D3 fix), so re-indexing the same set is
    a no-op and the reported doc count is stable."""
    session = _session(catalog, run_id)
    try:
        session.append_message(_msg("user", "Why is our pod getting killed on startup?"))
        session.append_message(_msg("assistant", LONG_ANSWER))

        first = enrich_conversation(client, session.root_doc_id, index=index_name)
        assert first.embedded or first.chunked

        chunks_after_first = client.get_children(first.chunked[0], usetype=CHUNK_USETYPE, limit=100)

        second = enrich_conversation(client, session.root_doc_id, index=index_name)
        assert not second.embedded, "re-embedded work that was already done"
        assert not second.chunked, "RE-CHUNKED — every chunk now exists twice"
        assert second.already_done

        chunks_after_second = client.get_children(
            first.chunked[0], usetype=CHUNK_USETYPE, limit=100
        )
        assert len(chunks_after_second) == len(chunks_after_first), "chunks were duplicated"

        # index-document is idempotent (the D3 fix), so re-indexing the same entry docs
        # reports the same count rather than creeping upward the way the pre-fix
        # double-counting endpoint would have.
        assert second.indexed_docs == first.indexed_docs
    finally:
        catalog.delete(str(session.root_doc_id))


def test_a_pass_that_died_before_embedding_is_completed_by_re_running(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    """Resumable, not merely "runs twice without crashing". A crash between chunking and
    embedding leaves chunks that exist but are unsearchable; re-running must finish them
    rather than see "chunks exist" and call the document done."""
    session = _session(catalog, run_id)
    try:
        session.append_message(_msg("assistant", LONG_ANSWER))
        # The LONGEST message, not the first: the first tau:message is the system prompt.
        messages = client.get_children(
            session.root_doc_id, usetype="tau:message", depth=-1, limit=10
        )
        doc_id = max(messages, key=lambda d: len(d.get("content") or ""))["id"]

        # Simulate the interrupted pass: chunk, but die before embedding any chunk.
        # `auto_embed=False` is what makes the crash state reachable — the real path
        # embeds inside the chunk call, so nothing else can leave chunks unembedded.
        chunks = client.chunk_document(doc_id, auto_embed=False)
        assert not any(client.is_embedded(c["id"]) for c in chunks)

        report = enrich_conversation(client, session.root_doc_id)

        assert report.embedded, "the re-run skipped the orphaned chunks — they stay unsearchable"
        assert all(client.is_embedded(c["id"]) for c in chunks)
    finally:
        catalog.delete(str(session.root_doc_id))
