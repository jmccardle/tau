"""``ext`` reaches ``structured_content.tau`` and survives a reload (docs/ENTRY-ANNOTATIONS.md §5.1).

Offline, over the same dict-backed fake client ``test_store_deferred.py`` uses.
The finalize PATCH replaces ``structured_content``, so it must carry ``ext``.
"""

from __future__ import annotations

from tau_agent_core.cursor import Cursor
from tau_jmfts.catalog import JmftsSessionCatalog
from tau_jmfts.store import JmftsSessionLog

from test_store_deferred import _Client


async def test_ext_lands_in_structured_content_and_reloads():
    client = _Client()
    log = JmftsSessionCatalog(client).create(".", "m", "b")  # type: ignore[arg-type]
    cursor = Cursor.newest(log)
    user = await cursor.append_message({"role": "user", "content": "hi"}, ext={"a.b": {"n": 1}})
    handle = await cursor.open("message", message={"role": "assistant", "content": []})
    await cursor.finalize(
        handle, message={"role": "assistant", "content": []}, ext={"a.b": {"n": 2}}
    )
    plain = await cursor.append_message({"role": "user", "content": "again"})

    assert client.docs[int(user)]["structured_content"]["tau"]["ext"] == {"a.b": {"n": 1}}
    assert client.docs[int(handle)]["structured_content"]["tau"]["ext"] == {"a.b": {"n": 2}}
    assert "ext" not in client.docs[int(plain)]["structured_content"]["tau"]

    root = log.root_doc_id
    assert root is not None
    reloaded = {e["id"]: e for e in JmftsSessionLog.load(client, root).entries()}  # type: ignore[arg-type]
    assert reloaded[user]["ext"] == {"a.b": {"n": 1}}
    assert reloaded[handle]["ext"] == {"a.b": {"n": 2}}
    assert "ext" not in reloaded[plain]
