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
from tau_agent_core.session_log import session_log_is_addressable
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
