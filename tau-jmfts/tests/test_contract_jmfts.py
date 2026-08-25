"""JmftsSessionLog conformance + JMFTS-specific properties, against a live server.

Two things live here:

1. The shared ``SessionLogContractTests`` suite (W5), run over ``JmftsSessionLog``.
   This is W11's exit criterion: green here means the contract suite is now green
   over all three ``SessionLog`` implementations (``InMemorySessionLog``, the file
   ``Session``, ``JmftsSessionLog``) -- SESSION-TREE-IMPLEMENTATION.md's "same
   entries -> same tree" invariant, proven over a real durability layer.
2. Properties the storage-agnostic contract suite cannot know about, because
   they are specific to the JMFTS mapping (docs/JMFTS-INTEGRATION-PLAN.md Sec2):
   the seq/doc-id integrity check, foreign-document synthesis, root-usetype
   verification on load, and fork's topology-preserving bulk copy.

Marker: ``jmfts``. Skips (does not fail) only when the server is unreachable --
see conftest.py. Every root document created here is deleted in test teardown;
DELETE cascades over the whole subtree server-side.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import SessionLog
from tau_agent_core.testing.session_log_contract import SessionLogContractTests
from tau_jmfts.client import JmftsClient, JmftsError
from tau_jmfts.store import JmftsSessionLog

# TREE-BROWSER-AS-EDITOR.md §8/§11.3: the splice appenders now require the anchor's
# provenance as keyword-only arguments with no defaults. These tests are about
# something else, so they name plausible values once here.
_PROV = {
    "summarizer_model_id": "test-summarizer",
    "summary_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    "covered_entries": 1,
    "covered_tokens": 50,
    "agent_spec_id": None,
}


pytestmark = pytest.mark.jmfts

TEST_PREFIX = "tau-jmfts-test"


def _session_id() -> str:
    return f"{TEST_PREFIX}-{uuid.uuid4().hex}"


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _context_texts(log: JmftsSessionLog) -> list[str]:
    """The text of every block on the log's active-path context fold."""
    return [
        block["text"]
        for message in log.context
        for block in (message.get("content") or [])
        if isinstance(block, dict) and "text" in block
    ]


# ---------------------------------------------------------------------------
# 1. The shared W5 contract suite.
# ---------------------------------------------------------------------------


class TestJmftsSessionLogContract(SessionLogContractTests):
    """``make_log()`` creates a fresh live-server-backed conversation per test;
    every root document this suite creates is deleted in teardown."""

    @pytest.fixture(autouse=True)
    def _setup(self, jmfts_url: str, jmfts_token: str | None):
        self._client = JmftsClient(jmfts_url, token=jmfts_token)
        self._created_roots: list[int] = []
        yield
        for root_id in self._created_roots:
            try:
                self._client.delete_document(root_id)
            except JmftsError:
                pass
        self._client.close()

    def make_log(self) -> SessionLog:
        log = JmftsSessionLog.create(
            self._client,
            cwd="/tmp/tau-jmfts-contract",
            model="test-model",
            backend="test-backend",
            id=_session_id(),
        )
        self._created_roots.append(log.root_doc_id)
        return log

    def reload(self, log: SessionLog) -> SessionLog | None:
        assert isinstance(log, JmftsSessionLog)
        return JmftsSessionLog.load(self._client, log.root_doc_id)


# ---------------------------------------------------------------------------
# 2. JMFTS-specific properties the contract suite can't know about.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(jmfts_url: str, jmfts_token: str | None):
    c = JmftsClient(jmfts_url, token=jmfts_token)
    yield c
    c.close()


def test_root_document_shape_and_id_mapping(client: JmftsClient) -> None:
    """The root doc is a tau:conversation carrying the header; SessionLog.id is
    the τ uuid (never the JMFTS doc id), and a root-level append (cursor is
    None) parents directly under the root document (Sec2.2/Sec2.3)."""
    log = JmftsSessionLog.create(
        client, cwd="/tmp/tau-jmfts-contract", model="m", backend="b", id=_session_id()
    )
    try:
        root_doc = client.get_document(log.root_doc_id)
        assert root_doc["usetype"] == "tau:conversation"
        assert root_doc["structured_content"]["tau"]["id"] == log.id
        assert log.id != str(log.root_doc_id)
        assert "/" not in log.id

        # `create()` already seeded a model_change entry (cursor != None), so its
        # own doc is the one that parents directly at the root document -- the
        # crux mapping: parentId None <-> parent_id == the ROOT DOCUMENT's id.
        seeded_id = log.entries()[0]["id"]
        assert log.entries()[0]["parentId"] is None
        seeded_doc = client.get_document(int(seeded_id))
        assert seeded_doc["parent_id"] == log.root_doc_id

        # navigate(None) + append is the general way to force a fresh root-level
        # append and re-confirm the same mapping mid-conversation.
        log.append_navigate(None)
        second_id = log.append_message(_msg("user", "hello"))
        second_doc = client.get_document(int(second_id))
        assert second_doc["parent_id"] == log.root_doc_id
        entries_by_id = {e["id"]: e for e in log.entries()}
        assert entries_by_id[second_id]["parentId"] is None
    finally:
        client.delete_document(log.root_doc_id)


def test_seq_counter_increments_and_survives_reload(client: JmftsClient) -> None:
    log = JmftsSessionLog.create(
        client, cwd="/tmp/tau-jmfts-contract", model="m", backend="b", id=_session_id()
    )
    try:
        a = log.append_message(_msg("user", "one"))
        b = log.append_message(_msg("user", "two"))
        doc_a = client.get_document(int(a))
        doc_b = client.get_document(int(b))
        assert doc_a["structured_content"]["seq"] < doc_b["structured_content"]["seq"]

        reloaded = JmftsSessionLog.load(client, log.root_doc_id)
        c = reloaded.append_message(_msg("user", "three"))
        doc_c = client.get_document(int(c))
        assert doc_c["structured_content"]["seq"] > doc_b["structured_content"]["seq"]
    finally:
        client.delete_document(log.root_doc_id)


def test_entries_take_cr1_sibling_positions(client: JmftsClient) -> None:
    """CR-1: entries carry an explicit ``position`` (birth order among siblings),
    so a fork point — a node with several children — is deterministically ordered
    rather than resolved only by a created_at tie. The conversation root itself
    carries no position: it is a recency collection, not a reading sequence."""
    log = JmftsSessionLog.create(
        client, cwd="/tmp/tau-jmfts-contract", model="m", backend="b", id=_session_id()
    )
    try:
        # The root is never position-ordered (store passes sequential=False).
        assert client.get_document(log.root_doc_id)["position"] is None

        # create() seeded a model_change as the root's first child (position 0).
        # Two navigate(None)+append pairs make it a real fork point: three siblings
        # directly under the root.
        log.append_navigate(None)
        b = log.append_message(_msg("user", "b"))
        log.append_navigate(None)
        c = log.append_message(_msg("user", "c"))

        children = client.get_children(log.root_doc_id)
        # Contiguous birth-order positions, returned in that order by the contract.
        assert [ch["position"] for ch in children] == [0, 1, 2]
        ordered_ids = [str(ch["id"]) for ch in children]
        assert ordered_ids.index(b) < ordered_ids.index(c)  # deterministic fork order

        # A linear step (single child) still takes a position — position 0 among the
        # sole child of its parent — proving the append path always opts in.
        d = log.append_message(_msg("user", "d"))  # chains off c's leaf
        assert client.get_document(int(d))["position"] == 0
    finally:
        client.delete_document(log.root_doc_id)


def test_seq_doc_id_integrity_check_fires_on_tampered_tree(client: JmftsClient) -> None:
    """Sec2.3: doc-id order must agree with seq order. Simulate a second writer by
    forcing a later doc's seq to precede an earlier doc's seq, then verify
    ``load`` fails loudly instead of silently resolving a bogus cursor/order."""
    log = JmftsSessionLog.create(
        client, cwd="/tmp/tau-jmfts-contract", model="m", backend="b", id=_session_id()
    )
    try:
        first = log.append_message(_msg("user", "one"))
        second = log.append_message(_msg("user", "two"))

        first_doc = client.get_document(int(first))
        first_seq = first_doc["structured_content"]["seq"]
        second_doc = client.get_document(int(second))
        tampered_sc = dict(second_doc["structured_content"])
        tampered_sc["seq"] = first_seq  # out of order relative to doc-id order
        client.update_document(int(second), structured_content=tampered_sc, re_embed=False)

        with pytest.raises(ValueError, match="second writer"):
            JmftsSessionLog.load(client, log.root_doc_id)
    finally:
        client.delete_document(log.root_doc_id)


def test_foreign_document_is_synthesized_and_walked_through(client: JmftsClient) -> None:
    """Sec2.4: a non-tau:* document (or one lacking structured_content.tau) inside
    the conversation subtree is surfaced as a synthesized jmfts:document entry --
    ConversationTree walks through it and it never reaches model input."""
    log = JmftsSessionLog.create(
        client, cwd="/tmp/tau-jmfts-contract", model="m", backend="b", id=_session_id()
    )
    try:
        a = log.append_message(_msg("user", "question"))
        log.append_message(_msg("assistant", "answer"))

        foreign = client.create_document(
            title=f"[{TEST_PREFIX}] a RAPTOR summary",
            usetype="raptor:summary",
            parent_id=int(a),
            content="some other subsystem's content",
            structured_content={"note": "not tau shaped"},
            auto_embed=False,
        )

        reloaded = JmftsSessionLog.load(client, log.root_doc_id)
        entries_by_id = {e["id"]: e for e in reloaded.entries()}
        fe = entries_by_id[str(foreign["id"])]
        assert fe["type"] == "jmfts:document"
        assert fe["parentId"] == a
        assert fe["usetype"] == "raptor:summary"
        assert fe["title"] == f"[{TEST_PREFIX}] a RAPTOR summary"

        tree = ConversationTree(reloaded.entries(), reloaded.cursor)
        assert tree.tree()  # renders without raising
        context_texts = [
            block.get("text")
            for m in tree.context_for()
            for block in (m.get("content") if isinstance(m.get("content"), list) else [])
            if isinstance(block, dict)
        ]
        assert "question" in context_texts
        assert "answer" in context_texts
        # the foreign node contributes no message -- it's walked through, not folded in.
        assert not any(
            isinstance(v, str) and "RAPTOR" in v for m in tree.context_for() for v in m.values()
        )
    finally:
        client.delete_document(log.root_doc_id)


def test_load_rejects_non_conversation_root(client: JmftsClient) -> None:
    doc = client.create_document(
        title=f"[{TEST_PREFIX}] not a conversation",
        usetype="some:other-type",
        structured_content={"whatever": True},
        auto_embed=False,
    )
    try:
        with pytest.raises(ValueError):
            JmftsSessionLog.load(client, doc["id"])
    finally:
        client.delete_document(doc["id"])


def test_load_rejects_malformed_header(client: JmftsClient) -> None:
    doc = client.create_document(
        title=f"[{TEST_PREFIX}] malformed header",
        usetype="tau:conversation",
        structured_content={"tau": {"type": "session"}},  # missing required fields
        auto_embed=False,
    )
    try:
        with pytest.raises(ValueError):
            JmftsSessionLog.load(client, doc["id"])
    finally:
        client.delete_document(doc["id"])


def test_fork_remaps_cross_references_not_just_parent_ids(client: JmftsClient) -> None:
    """A fork must rewrite ``firstKeptId``/``targetId``/``fromId``, not copy them.

    Under this store an entry id IS a JMFTS doc id, so a fork's fresh documents get
    fresh ids — and copying ``structured_content`` verbatim leaves these three fields
    aimed at the SOURCE's documents. Nothing raises when that happens: the fold simply
    never finds the anchor and quietly drops a whole region. Regression for a real bug
    (measured: forking a compacted session lost its kept messages outright, with the
    forked context coming back as summary + tail and "keep me" gone).

    ``test_fork_preserves_topology_with_new_doc_ids`` did not catch it, and the reason
    is instructive: its ``navigate`` was not the LAST entry, so the dangling
    ``targetId`` was never actually consulted. The bug only bites where a
    cross-reference is *read* — a compaction anchor (always read by the fold) or a
    TRAILING navigate (read by cursor resolution). This test forces both.
    """
    source = JmftsSessionLog.create(
        client, cwd="/tmp/tau-jmfts-contract", model="m", backend="b", id=_session_id()
    )
    forked: JmftsSessionLog | None = None
    try:
        source.append_message(_msg("user", "compacted away"))
        keep = source.append_message(_msg("assistant", "keep me"))
        source.append_compaction("the summary", keep, tokens_before=10, **_PROV)
        tail = source.append_message(_msg("user", "after compaction"))

        # a branch + branch_summary (exercises fromId), then a TRAILING navigate
        # (exercises targetId via cursor resolution, which the old test never did).
        source.append_navigate(keep)
        source.append_message(_msg("assistant", "doomed branch"))
        source.append_branch_summary("abandoned", keep)
        source.append_navigate(tail)

        forked = JmftsSessionLog.fork(client, source, cwd="/tmp/tau-jmfts-contract")

        forked_ids = {e["id"] for e in forked.entries()}

        # Every cross-reference must name an entry that exists IN THE FORK.
        for entry in forked.entries():
            for field in ("targetId", "firstKeptId", "fromId"):
                ref = entry.get(field)
                if ref is not None:
                    assert ref in forked_ids, (
                        f"{entry['type']}.{field}={ref!r} dangles in the fork — it still "
                        "names a source document, so the fold will silently lose a region"
                    )

        # And the observable consequence: the kept region survives the fork.
        forked_texts = _context_texts(forked)
        assert "keep me" in forked_texts, "the compaction's kept region vanished from the fork"
        assert "after compaction" in forked_texts
        assert "compacted away" not in forked_texts  # still correctly compacted out
        assert "doomed branch" not in forked_texts  # still correctly branched off
        assert forked_texts == _context_texts(source)
    finally:
        client.delete_document(source.root_doc_id)
        if forked is not None:
            client.delete_document(forked.root_doc_id)


def test_fork_preserves_topology_with_new_doc_ids(client: JmftsClient) -> None:
    source = JmftsSessionLog.create(
        client, cwd="/tmp/tau-jmfts-contract", model="m", backend="b", id=_session_id()
    )
    forked: JmftsSessionLog | None = None
    try:
        a = source.append_message(_msg("user", "hi"))
        source.append_message(_msg("assistant", "yo"))
        source.append_navigate(a)
        source.append_message(_msg("assistant", "alt"))

        forked = JmftsSessionLog.fork(client, source, cwd="/tmp/tau-jmfts-contract")

        assert forked.id != source.id
        assert forked.header["parent"] == source.id
        assert forked.root_doc_id != source.root_doc_id

        source_texts = [
            block.get("text")
            for m in source.context
            for block in (m.get("content") if isinstance(m.get("content"), list) else [])
        ]
        forked_texts = [
            block.get("text")
            for m in forked.context
            for block in (m.get("content") if isinstance(m.get("content"), list) else [])
        ]
        assert forked_texts == source_texts

        # the fork's entries carry brand-new (JMFTS-assigned) ids, not the source's.
        source_ids = {e["id"] for e in source.entries()}
        forked_ids = {e["id"] for e in forked.entries()}
        assert source_ids.isdisjoint(forked_ids)
    finally:
        client.delete_document(source.root_doc_id)
        if forked is not None:
            client.delete_document(forked.root_doc_id)
