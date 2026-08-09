"""Live integration tests for JmftsClient against a real JMFTS server.

Marker: ``jmfts``. Select with ``pytest -m jmfts``; deselect with
``pytest -m "not jmfts"``. Skips (does not fail) only when the server is
unreachable -- see conftest.py. Every document created here is deleted in a
``finally`` block; the root delete cascades over its subtree server-side.

Reference: docs/JMFTS-INTEGRATION-PLAN.md Sec2 (mapping contract exercised
here: structured_content.tau must round-trip byte-shape-identical), Sec3.2/3.3
(write/read path), Sec8 (Fail-Early: no fallback, no skip-on-error).
"""

from __future__ import annotations

import uuid

import pytest

from tau_jmfts.client import JmftsClient, JmftsError

pytestmark = pytest.mark.jmfts

# Every document this test creates carries this prefix so a human (or a
# cleanup script) can find and nuke anything left behind by a crashed run.
TEST_PREFIX = "tau-jmfts-test"


@pytest.fixture
def client(jmfts_url: str, jmfts_token: str | None):
    c = JmftsClient(jmfts_url, token=jmfts_token)
    yield c
    c.close()


def test_health(client: JmftsClient) -> None:
    result = client.health()
    assert result["status"] == "ok"
    assert isinstance(result["version"], str)
    assert isinstance(result["database"], str)


def test_context_manager_closes(jmfts_url: str, jmfts_token: str | None) -> None:
    with JmftsClient(jmfts_url, token=jmfts_token) as c:
        assert c.health()["status"] == "ok"
    # httpx.Client raises RuntimeError on request after close(); confirm the
    # underlying transport was actually torn down rather than left open.
    with pytest.raises(RuntimeError):
        c.health()


def test_create_read_topology_and_delete_cascade(client: JmftsClient) -> None:
    """The load-bearing round trip: create a root + a small parent/child
    topology mirroring the tau entry tree, read the whole subtree back in one
    call, verify structured_content survives verbatim (byte-shape-identical
    per Sec2.1), then delete the root and confirm the cascade removed
    everything.
    """
    run_id = uuid.uuid4().hex[:8]
    tau_header = {
        "type": "session",
        "version": 1,
        "id": f"{TEST_PREFIX}-session-{run_id}",
        "timestamp": "2026-07-12T00:00:00+00:00",
        "cwd": "/home/john/Development/agent-harness-py",
        "hostname": "test-harness",
        "parent": None,
        # Nested structures + unicode + a float, to make "byte-shape-identical"
        # a real assertion rather than a string-equality one.
        "nested": {"list": [1, 2, 3], "unicode": "τ agent — café", "flag": True},
        "score": 3.14159,
    }
    root_id: int | None = None
    try:
        root = client.create_document(
            title=f"[{TEST_PREFIX}] root {run_id}",
            usetype="tau:conversation",
            parent_id=None,
            structured_content={"tau": tau_header},
            auto_embed=False,
        )
        root_id = root["id"]
        assert root["parent_id"] is None
        assert root["usetype"] == "tau:conversation"
        assert root["structured_content"]["tau"] == tau_header

        user_entry = {
            "type": "message",
            "id": "placeholder",
            "parentId": None,
            "role": "user",
            "text": "hello from the round-trip test",
        }
        user_msg = client.create_document(
            title=f"[{TEST_PREFIX}] user 0001",
            usetype="tau:message",
            parent_id=root_id,
            content="hello from the round-trip test",
            structured_content={"tau": user_entry, "seq": 1},
            auto_embed=False,
        )
        assert user_msg["parent_id"] == root_id

        assistant_entry = {
            "type": "message",
            "id": "placeholder",
            "parentId": str(user_msg["id"]),
            "role": "assistant",
            "text": "hi back",
        }
        assistant_msg = client.create_document(
            title=f"[{TEST_PREFIX}] assistant 0002",
            usetype="tau:message",
            parent_id=user_msg["id"],
            content="hi back",
            structured_content={"tau": assistant_entry, "seq": 2},
            auto_embed=False,
        )
        assert assistant_msg["parent_id"] == user_msg["id"]

        # A second child of the root -- exercises real branching, not just a chain.
        branch_entry = {
            "type": "message",
            "id": "placeholder",
            "parentId": None,
            "role": "user",
            "text": "a sibling branch off the root",
        }
        branch_msg = client.create_document(
            title=f"[{TEST_PREFIX}] user-branch 0003",
            usetype="tau:message",
            parent_id=root_id,
            content="a sibling branch off the root",
            structured_content={"tau": branch_entry, "seq": 3},
            auto_embed=False,
        )
        assert branch_msg["parent_id"] == root_id

        # -- read path: one subtree query recovers the whole topology --
        subtree = client.get_subtree(root_id, max_depth=None)
        assert subtree["root"]["id"] == root_id
        assert subtree["total"] == 4  # root + 3 entries
        by_id = {doc["id"]: doc for doc in subtree["descendants"]}
        assert set(by_id) == {user_msg["id"], assistant_msg["id"], branch_msg["id"]}
        assert by_id[user_msg["id"]]["parent_id"] == root_id
        assert by_id[assistant_msg["id"]]["parent_id"] == user_msg["id"]
        assert by_id[branch_msg["id"]]["parent_id"] == root_id

        # structured_content.tau survives byte-shape-identical, including
        # nested structures/unicode/float -- the load-bearing property.
        assert subtree["root"]["structured_content"]["tau"] == tau_header
        assert by_id[user_msg["id"]]["structured_content"]["tau"] == user_entry
        assert by_id[assistant_msg["id"]]["structured_content"]["tau"] == assistant_entry
        assert by_id[branch_msg["id"]]["structured_content"]["tau"] == branch_entry

        # -- get_children: immediate vs. all-descendants --
        immediate = client.get_children(root_id, depth=1)
        assert {d["id"] for d in immediate} == {user_msg["id"], branch_msg["id"]}
        all_descendants = client.get_children(root_id, depth=-1)
        assert {d["id"] for d in all_descendants} == {
            user_msg["id"],
            assistant_msg["id"],
            branch_msg["id"],
        }

        # -- list_documents / get_document round trip --
        fetched_root = client.get_document(root_id)
        assert fetched_root["id"] == root_id
        assert fetched_root["structured_content"]["tau"] == tau_header

        found = client.list_documents(usetype="tau:conversation", title_prefix=f"[{TEST_PREFIX}]")
        assert any(d["id"] == root_id for d in found)

        # -- update_document: content/title projection can change without
        # disturbing structured_content.tau --
        updated = client.update_document(user_msg["id"], title="renamed", re_embed=False)
        assert updated["title"] == "renamed"
        assert updated["structured_content"]["tau"] == user_entry

        # -- delete: cascades over the whole subtree --
        client.delete_document(root_id)
        deleted_root_id = root_id
        root_id = None  # tell the finally block cleanup already happened

        with pytest.raises(JmftsError) as excinfo:
            client.get_document(deleted_root_id)
        assert excinfo.value.status_code == 404

        with pytest.raises(JmftsError) as excinfo:
            client.get_document(user_msg["id"])
        assert excinfo.value.status_code == 404
    finally:
        if root_id is not None:
            # Best-effort cleanup on assertion failure -- delete cascades.
            try:
                client.delete_document(root_id)
            except JmftsError:
                pass
