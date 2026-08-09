"""Tests for the versioned-tree strategy store (``tau_jmfts.ext.strategy_store``).

Reference: ``docs/M3-DESIGN.md`` §3. The unit tests run the versioned-tree logic against
an IN-MEMORY FAKE (``FakeJmftsClient`` below) that implements exactly the subset of
:class:`JmftsClient` the store calls — it is a TEST DOUBLE, not shipped. The production
``strategy_store.py`` uses the real client with no fallback; the one ``jmfts``-marked
test at the bottom exercises it against a live server and SKIPS when none is configured.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tau_jmfts.client import JmftsClient
from tau_jmfts.ext.strategy_store import (
    HEAD_USETYPE,
    LOG_KIND,
    LOG_USETYPE,
    ROOT_USETYPE,
    StrategyStore,
)

# ======================================================================================
# TEST DOUBLE — in-memory fake of the JmftsClient subset the store uses. Test-only.
# ======================================================================================


class FakeJmftsClient:
    """A minimal in-memory stand-in for :class:`JmftsClient`.

    Models exactly what the store touches: ``create_document`` (with CR-1 ``sequential``
    position assignment), ``get_document``, ``update_document`` (PATCH semantics —
    only-passed-fields, and ``structured_content`` fully replaced), ``get_children``
    (with usetype/title filters), ``list_documents`` (paginated, usetype filter), and
    ``search`` (returns canned results, records its call args).

    NOT a general JMFTS emulator — anything the store does not call is absent by design.
    """

    def __init__(self) -> None:
        self._docs: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        # Canned search behaviour + a record of the last search call, for `find` tests.
        self.search_results: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    # -- position helper (CR-1) --------------------------------------------------

    def _assign_position(self, parent_id: int | None, sequential: bool | None) -> int | None:
        parent_ordered = False
        if parent_id is not None:
            parent = self._docs.get(parent_id)
            parent_ordered = parent is not None and parent.get("position") is not None
        # sequential=True opts in; None inherits the parent's ordered-ness; False → NULL.
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
    ) -> dict[str, Any]:
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

    def get_document(self, doc_id: int, *, include_embed: bool = False) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        doc = self._docs[doc_id]
        # PATCH semantics: only fields the caller actually passed are changed;
        # structured_content is REPLACED wholesale when passed.
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
    ) -> list[dict[str, Any]]:
        kids = [d for d in self._docs.values() if d["parent_id"] == doc_id]
        if usetype is not None:
            kids = [d for d in kids if d["usetype"] == usetype]
        if title is not None:
            kids = [d for d in kids if d["title"] == title]
        if title_prefix is not None:
            kids = [d for d in kids if (d["title"] or "").startswith(title_prefix)]
        # Adversarial order: return NEWEST first so the store cannot rely on server
        # order and must sort by CR-1 position itself.
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
    ) -> list[dict[str, Any]]:
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
        self.search_calls.append(
            {
                "query": query,
                "method": method,
                "limit": limit,
                "parent_id": parent_id,
                "usetype": usetype,
            }
        )
        return [dict(hit) for hit in self.search_results[:limit]]


@pytest.fixture
def store() -> StrategyStore:
    # The fake is duck-typed against the JmftsClient surface the store calls; the store
    # never introspects the client's concrete type, so this is sound.
    return StrategyStore(FakeJmftsClient())  # type: ignore[arg-type]


def _fake(store: StrategyStore) -> FakeJmftsClient:
    client = store._client
    assert isinstance(client, FakeJmftsClient)
    return client


# ======================================================================================
# Root + family
# ======================================================================================


def test_root_is_created_once_and_reused() -> None:
    fake = FakeJmftsClient()
    store = StrategyStore(fake)  # type: ignore[arg-type]
    root_id = store.root_id
    assert store.root_id == root_id  # cached, not re-created
    roots = [d for d in fake._docs.values() if d["usetype"] == ROOT_USETYPE]
    assert len(roots) == 1
    assert roots[0]["parent_id"] is None


def test_root_reopened_not_duplicated() -> None:
    fake = FakeJmftsClient()
    StrategyStore(fake).root_id  # type: ignore[arg-type]  # create
    StrategyStore(fake).root_id  # type: ignore[arg-type]  # reopen
    roots = [d for d in fake._docs.values() if d["usetype"] == ROOT_USETYPE]
    assert len(roots) == 1


def test_family_get_or_create_is_idempotent(store: StrategyStore) -> None:
    a = store.family("endgame")
    b = store.family("endgame")
    assert a == b
    heads = [d for d in _fake(store)._docs.values() if d["usetype"] == HEAD_USETYPE]
    assert len(heads) == 1
    assert heads[0]["structured_content"] == {"kind": "strategy_head"}


# ======================================================================================
# Append-only log + temporal order
# ======================================================================================


def test_append_preserves_temporal_order_and_is_append_only(store: StrategyStore) -> None:
    fam = store.family("openings")
    first = store.append_log(fam, "control the center")
    second = store.append_log(fam, "develop knights early")

    # A second append does not rewrite the first.
    assert first["id"] != second["id"]
    reread_first = _fake(store).get_document(first["id"])
    assert reread_first["content"] == "control the center"

    hist = store.history(fam)
    assert [d["content"] for d in hist] == ["control the center", "develop knights early"]
    # CR-1 positions are set and strictly increasing.
    positions = [d["position"] for d in hist]
    assert positions == sorted(positions)
    assert all(p is not None for p in positions)


def test_log_child_shape(store: StrategyStore) -> None:
    fam = store.family("f")
    entry = store.append_log(fam, "a lesson", source="game-7")
    sc = entry["structured_content"]
    assert sc["kind"] == LOG_KIND
    assert sc["consolidated"] is False
    assert sc["source"] == "game-7"
    assert entry["usetype"] == LOG_USETYPE
    assert entry["parent_id"] == fam.head_id


def test_append_extra_cannot_shadow_reserved_keys(store: StrategyStore) -> None:
    fam = store.family("f")
    with pytest.raises(ValueError, match="store-owned keys"):
        store.append_log(fam, "x", extra={"consolidated": True})


# ======================================================================================
# Footer (unconsolidated tree-read)
# ======================================================================================


def test_footer_returns_only_unconsolidated_logs(store: StrategyStore) -> None:
    fam = store.family("f")
    store.append_log(fam, "lesson one")
    store.append_log(fam, "lesson two")

    footer = store.footer(fam)
    # Only the log children — never the head, never anything else.
    assert [d["content"] for d in footer] == ["lesson one", "lesson two"]
    assert all(d["usetype"] == LOG_USETYPE for d in footer)
    assert all(d["structured_content"]["consolidated"] is False for d in footer)


def test_footer_excludes_consolidated_logs(store: StrategyStore) -> None:
    fam = store.family("f")
    store.append_log(fam, "old lesson")
    store.consolidate(fam, "consolidated head text")
    store.append_log(fam, "fresh lesson")

    footer = store.footer(fam)
    assert [d["content"] for d in footer] == ["fresh lesson"]


# ======================================================================================
# Consolidation
# ======================================================================================


def test_consolidate_rewrites_head_and_clears_flags(store: StrategyStore) -> None:
    fam = store.family("f")
    store.append_log(fam, "a")
    store.append_log(fam, "b")

    n = store.consolidate(fam, "new consolidated strategy")
    assert n == 2

    head = _fake(store).get_document(fam.head_id)
    assert head["content"] == "new consolidated strategy"

    # Footer now empty; the previously-unconsolidated flags are all set.
    assert store.footer(fam) == []
    hist = store.history(fam)
    assert all(d["structured_content"]["consolidated"] is True for d in hist)
    # History is NOT destroyed — the log children still exist.
    assert [d["content"] for d in hist] == ["a", "b"]


def test_consolidate_only_flips_currently_unconsolidated(store: StrategyStore) -> None:
    fam = store.family("f")
    store.append_log(fam, "first")
    store.consolidate(fam, "head v1")  # flips "first"
    store.append_log(fam, "second")

    # Second consolidation should only touch "second".
    n = store.consolidate(fam, "head v2")
    assert n == 1
    assert store.footer(fam) == []


def test_consolidate_preserves_log_content_and_metadata(store: StrategyStore) -> None:
    fam = store.family("f")
    entry = store.append_log(fam, "the lesson text", source="game-3")
    store.consolidate(fam, "head")

    reread = _fake(store).get_document(entry["id"])
    # The ONLY mutation is the flag; content, title, source all survive.
    assert reread["content"] == "the lesson text"
    assert reread["structured_content"]["source"] == "game-3"
    assert reread["structured_content"]["kind"] == LOG_KIND
    assert reread["structured_content"]["consolidated"] is True


def test_consolidate_with_empty_footer_still_rewrites_head(store: StrategyStore) -> None:
    fam = store.family("f")
    n = store.consolidate(fam, "seeded head")
    assert n == 0
    assert _fake(store).get_document(fam.head_id)["content"] == "seeded head"


# ======================================================================================
# History / provenance (the poison audit)
# ======================================================================================


def test_history_returns_all_entries_in_order_including_consolidated(store: StrategyStore) -> None:
    fam = store.family("f")
    store.append_log(fam, "one")
    store.append_log(fam, "two")
    store.consolidate(fam, "head")
    store.append_log(fam, "three")

    hist = store.history(fam)
    assert [d["content"] for d in hist] == ["one", "two", "three"]


def test_provenance_poison_survives_consolidation(store: StrategyStore) -> None:
    """After a poison entry is consolidated into the head, the audit can still find it.

    This is the payoff of the immutable log: un-poisoning re-consolidates, but the
    record of WHERE the bad lesson entered is never destroyed.
    """
    fam = store.family("f")
    store.append_log(fam, "good: control the center")
    store.append_log(fam, "POISON: rush the queen out early", source="lucky-win-game-4")
    store.consolidate(fam, "folded-in head (poison included)")

    # The footer no longer shows it (it was folded in) ...
    assert store.footer(fam) == []
    # ... but walking the log still surfaces it, with its provenance intact.
    hist = store.history(fam)
    poison = [d for d in hist if d["content"].startswith("POISON")]
    assert len(poison) == 1
    assert poison[0]["structured_content"]["source"] == "lucky-win-game-4"


# ======================================================================================
# Assemble (the read-back)
# ======================================================================================


def test_assemble_is_head_plus_footer(store: StrategyStore) -> None:
    fam = store.family("f")
    store.consolidate(fam, "HEAD BODY")
    store.append_log(fam, "consideration A")
    store.append_log(fam, "consideration B")

    text = store.assemble(fam)
    assert text.startswith("HEAD BODY")
    assert "consideration A" in text
    assert "consideration B" in text


def test_assemble_empty_footer_is_head_verbatim(store: StrategyStore) -> None:
    fam = store.family("f")
    store.consolidate(fam, "just the head")
    assert store.assemble(fam) == "just the head"


# ======================================================================================
# Fail-Early: corrupt log state raises
# ======================================================================================


def test_missing_consolidated_flag_raises(store: StrategyStore) -> None:
    fam = store.family("f")
    entry = store.append_log(fam, "x")
    # Corrupt the log doc: drop the flag (simulating a bad writer).
    _fake(store)._docs[entry["id"]]["structured_content"] = {"kind": LOG_KIND}
    with pytest.raises(ValueError, match="missing its 'consolidated' flag"):
        store.footer(fam)


def test_non_boolean_consolidated_flag_raises(store: StrategyStore) -> None:
    fam = store.family("f")
    entry = store.append_log(fam, "x")
    _fake(store)._docs[entry["id"]]["structured_content"]["consolidated"] = "yes"
    with pytest.raises(ValueError, match="non-boolean"):
        store.history(fam)


# ======================================================================================
# Find (retrieval step 1)
# ======================================================================================


def test_find_scopes_to_subtree_and_heads_and_returns_ranked(store: StrategyStore) -> None:
    fam = store.family("endgame")
    fake = _fake(store)
    head_doc = fake.get_document(fam.head_id)
    # Canned ranked results.
    fake.search_results = [{"document": head_doc, "score": 0.9, "method": "hybrid"}]

    results = store.find("king and pawn endgame", k=3)
    assert results == [head_doc]

    call = fake.search_calls[-1]
    assert call["query"] == "king and pawn endgame"
    assert call["limit"] == 3
    # Subtree scoping to the store root, narrowed to heads.
    assert call["parent_id"] == store.root_id
    assert call["usetype"] == HEAD_USETYPE


# ======================================================================================
# Live integration — real JmftsClient against a server. SKIPS when none configured.
# ======================================================================================

pytest_live = pytest.mark.jmfts


@pytest_live
def test_live_versioned_tree_roundtrip(jmfts_url: str, jmfts_token: str | None) -> None:
    """End-to-end against a real server: create a family, append, footer, consolidate,
    history, assemble — then delete the root (cascade cleans the subtree).
    """
    run_id = uuid.uuid4().hex[:8]
    client = JmftsClient(jmfts_url, token=jmfts_token)
    store = StrategyStore(client, root_title=f"tau-jmfts-test-strategy-{run_id}")
    root_id: int | None = None
    try:
        root_id = store.root_id
        fam = store.family("test-family")

        store.append_log(fam, "lesson one", source="run-1")
        store.append_log(fam, "lesson two", source="run-2")

        footer = store.footer(fam)
        assert [d["content"] for d in footer] == ["lesson one", "lesson two"]

        n = store.consolidate(fam, "consolidated head text")
        assert n == 2
        assert store.footer(fam) == []
        assert client.get_document(fam.head_id)["content"] == "consolidated head text"

        store.append_log(fam, "lesson three")
        hist = store.history(fam)
        assert [d["content"] for d in hist] == ["lesson one", "lesson two", "lesson three"]

        text = store.assemble(fam)
        assert text.startswith("consolidated head text")
        assert "lesson three" in text
    finally:
        if root_id is not None:
            from tau_jmfts.client import JmftsError

            try:
                client.delete_document(root_id)
            except JmftsError:
                pass
        client.close()
