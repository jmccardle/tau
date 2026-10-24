"""A deferred JMFTS conversation POSTs nothing until its first append.

Offline: a fake client holds documents in a dict, enough for the write path,
``get_subtree`` and therefore ``load`` and ``fork``. The deferred case is the one
place a mirror id differs from a document id (docs/SESSION-AT-MOUNT.md), so
these tests check the mapping at every boundary the store translates.
"""

from __future__ import annotations

from typing import Any

import pytest

from tau_agent_core.cursor import Cursor
from tau_agent_core.session_log import (
    OpenSideEntryError,
    default_leaf,
    session_log_is_addressable,
)
from tau_jmfts.catalog import JmftsSessionCatalog
from tau_jmfts.store import JmftsSessionLog


class _Client:
    """Documents in a dict; ids ascend, as the server's do."""

    def __init__(self) -> None:
        self.docs: dict[int, dict[str, Any]] = {}
        self._next_id = 100
        self.fail_on_post: int | None = None
        self.posts = 0

    def create_document(self, **kwargs: Any) -> dict[str, Any]:
        self.posts += 1
        if self.posts == self.fail_on_post:
            raise ConnectionError("server went away")
        doc = {"id": self._next_id, "created_at": None, **kwargs}
        self.docs[self._next_id] = doc
        self._next_id += 1
        return doc

    def update_document(self, doc_id: int, **kwargs: Any) -> None:
        assert doc_id in self.docs, f"update of unknown document {doc_id}"
        self.docs[doc_id].update({k: v for k, v in kwargs.items() if k != "re_embed"})

    def get_subtree(self, root_id: int, max_depth: Any = None) -> dict[str, Any]:
        under: set[int] = {root_id}
        for doc_id in sorted(self.docs):
            if self.docs[doc_id].get("parent_id") in under:
                under.add(doc_id)
        under.discard(root_id)
        return {"root": self.docs[root_id], "descendants": [self.docs[i] for i in sorted(under)]}


def _deferred(client: _Client) -> JmftsSessionLog:
    catalog = JmftsSessionCatalog(client)  # type: ignore[arg-type]
    return catalog.create_deferred(".", "m", "b", system_prompt="sys", name="N")


async def test_nothing_is_posted_until_the_first_append():
    client = _Client()
    log = _deferred(client)
    assert client.docs == {}
    assert log.root_doc_id is None
    assert [e["type"] for e in log.entries()] == ["customEntry", "session_info", "message"]
    assert log.display_title() == "N" and log.model == "m"


async def test_the_first_append_posts_root_chain_and_entry_in_order():
    client = _Client()
    log = _deferred(client)
    held = log.entries()
    cursor = Cursor.newest(log)
    leaf = await cursor.append_message({"role": "user", "content": "hi"})

    root = log.root_doc_id
    assert root is not None and client.docs[root]["usetype"] == "tau:conversation"
    assert client.docs[root]["title"] == "N"
    written = log.entries()
    assert written[:3] == held, "held ids must stay the mirror's ids"
    reloaded = JmftsSessionLog.load(client, root)  # type: ignore[arg-type]
    assert [e["type"] for e in reloaded.entries()] == [e["type"] for e in written]
    assert reloaded.context == log.context
    assert reloaded.entries()[-1]["id"] == leaf
    system_doc = reloaded.entries()[2]["id"]
    assert client.docs[int(leaf)]["parent_id"] == int(system_doc)


async def test_a_reference_to_a_held_entry_is_stored_as_its_document_id():
    client = _Client()
    log = _deferred(client)
    config_id = log.entries()[0]["id"]
    cursor = Cursor.newest(log)
    await cursor.append_message({"role": "user", "content": "hi"})
    nav = await log.append_at(cursor.leaf, "navigate", {"targetId": config_id})

    stored = client.docs[int(nav)]["structured_content"]["tau"]["targetId"]
    assert stored == str(log._doc_id(config_id))
    assert log.entries()[-1]["targetId"] == config_id


async def test_forking_an_unwritten_conversation_writes_it_first():
    client = _Client()
    log = _deferred(client)
    catalog = JmftsSessionCatalog(client)  # type: ignore[arg-type]
    fork = catalog.fork(log, ".", at=log.entries()[-1]["id"])
    assert log.root_doc_id is not None
    assert [e["type"] for e in fork.entries()] == ["customEntry", "session_info", "message"]
    assert fork.header["parent"] == log.id


async def test_a_post_failing_partway_resumes_on_the_next_append():
    """The server cannot roll back, so the written prefix stays and a retry continues."""
    client = _Client()
    log = _deferred(client)
    held = log.entries()
    cursor = Cursor.newest(log)
    client.fail_on_post = 3
    with pytest.raises(ConnectionError):
        await cursor.append_message({"role": "user", "content": "first try"})
    assert log.root_doc_id is not None
    assert len(client.docs) == 2, "root and the config entry landed; the rest is held"
    assert log._held == [held[1]["id"], held[2]["id"]]

    client.fail_on_post = None
    leaf = await cursor.append_message({"role": "user", "content": "second try"})
    root = log.root_doc_id
    assert root is not None
    reloaded = JmftsSessionLog.load(client, root)  # type: ignore[arg-type]
    assert [e["type"] for e in reloaded.entries()] == [
        "customEntry",
        "session_info",
        "message",
        "message",
    ]
    assert reloaded.entries()[-1]["id"] == leaf
    assert reloaded.context == log.context


def test_an_unwritten_conversation_is_not_addressable():
    client = _Client()
    log = _deferred(client)
    assert session_log_is_addressable(log) is False
    log._append_now(
        log.entries()[-1]["id"], "message", {"message": {"role": "user", "content": "x"}}
    )
    assert session_log_is_addressable(log) is True


async def test_a_fork_copies_finalized_side_entries_and_refuses_an_open_one():
    client = _Client()
    log = _deferred(client)
    catalog = JmftsSessionCatalog(client)  # type: ignore[arg-type]
    cursor = Cursor.newest(log)
    leaf = await cursor.append_message({"role": "user", "content": "hi"})
    verdict = await cursor.open_side("suspension", site="turn_end")
    await cursor.finalize_side(verdict, site="turn_end", resolution={"action": "step"})
    pending = await cursor.open_side("suspension", site="turn_end")

    with pytest.raises(OpenSideEntryError, match=pending):
        catalog.fork(log, ".", at=leaf)
    with pytest.raises(OpenSideEntryError, match=pending):
        catalog.fork(log, ".")

    await cursor.finalize_side(pending, site="turn_end", resolution={"action": "abort"})
    for at in (leaf, None):
        fork = catalog.fork(log, ".", at=at)
        kinds = [e["type"] for e in fork.entries()]
        assert kinds.count("suspension") == 2, at
        assert all("status" not in e for e in fork.entries() if e["type"] == "suspension")
        assert fork.entries()[-1]["type"] == "suspension"
        assert default_leaf(fork.entries()) == fork.entries()[-3]["id"]


async def test_a_fork_reads_the_mirror_and_the_subtree_with_no_append_between():
    """An append or finalize landing between the two reads could copy an open side entry."""
    client = _Client()
    log = _deferred(client)
    cursor = Cursor.newest(log)
    await cursor.append_message({"role": "user", "content": "hi"})
    reads: list[bool] = []
    get_subtree = client.get_subtree

    def watched(root_id: int, max_depth: Any = None) -> dict[str, Any]:
        reads.append(log._append_lock.locked())
        return get_subtree(root_id, max_depth)

    client.get_subtree = watched  # type: ignore[method-assign]
    JmftsSessionCatalog(client).fork(log, ".")  # type: ignore[arg-type]
    assert reads[0] is True, "the source's subtree is read under its append lock"


async def _annotated_session() -> tuple[_Client, JmftsSessionLog, list[str]]:
    """Three message entries; a non-τ child (empty, unordered) under entries 2 and 3."""
    client = _Client()
    log = _deferred(client)
    cursor = Cursor.newest(log)
    ids = [await cursor.append_message({"role": "user", "content": f"m{i}"}) for i in range(3)]
    for entry_id in ids[1:]:
        doc = client.create_document(
            title="a",
            content="",
            parent_id=log._doc_id(entry_id),
            usetype="horopter:annotations",
            structured_content={"n": entry_id},
            auto_embed=False,
            sequential=False,
        )
        doc["position"] = None
    return client, log, ids


def _notes(client: _Client, fork: JmftsSessionLog) -> list[dict[str, Any]]:
    sub = client.get_subtree(fork._root_doc_id)["descendants"]  # type: ignore[arg-type]
    return [d for d in sub if d.get("usetype") == "horopter:annotations"]


async def test_a_fork_copies_non_tau_children_of_copied_entries_only():
    client, log, ids = await _annotated_session()
    catalog = JmftsSessionCatalog(client)  # type: ignore[arg-type]
    for at, expect in ((ids[2], ids[1:]), (ids[1], ids[1:2]), (None, ids[1:])):
        fork = catalog.fork(log, ".", at=at)
        notes = _notes(client, fork)
        assert [n["structured_content"]["n"] for n in notes] == expect, at
        by_text = {
            e["message"]["content"]: fork._doc_id(e["id"])
            for e in fork.entries()
            if e["type"] == "message" and e["message"]["role"] == "user"
        }
        assert [n["parent_id"] for n in notes] == [by_text[f"m{ids.index(e)}"] for e in expect], at
        assert all(n["sequential"] is False and n["content"] == "" for n in notes), at
    assert not any(d.get("dedup") for d in client.docs.values())
