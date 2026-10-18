"""``chunk_document`` speaks JMFTS 0.7.0's ``ChunkRequest`` and refuses an older server.

No live server: an ``httpx.MockTransport`` stands in. Not marked ``jmfts``, so it
always runs.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tau_jmfts.client import JmftsClient, JmftsVersionError


def _client(version: str, seen: list[httpx.Request]) -> JmftsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/":
            return httpx.Response(200, json={"status": "ok", "version": version})
        if request.url.path.endswith("/chunk"):
            return httpx.Response(200, json={"parent_id": 7, "chunks": []})
        return httpx.Response(200, json=[])

    client = JmftsClient("http://jmfts.test")
    client._client.close()
    client._client = httpx.Client(
        base_url="http://jmfts.test", transport=httpx.MockTransport(handler)
    )
    return client


def _chunk_bodies(seen: list[httpx.Request]) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in seen if r.url.path.endswith("/chunk")]


def test_the_word_budget_is_sent_as_max_words() -> None:
    seen: list[httpx.Request] = []
    with _client("0.7.1", seen) as client:
        client.chunk_document(7, max_words=150)
    (body,) = _chunk_bodies(seen)
    assert body["max_words"] == 150
    assert "max_tokens" not in body


def test_the_server_version_is_asked_once_per_client() -> None:
    seen: list[httpx.Request] = []
    with _client("0.8.0", seen) as client:
        client.chunk_document(7)
        client.chunk_document(8)
    assert [r.url.path for r in seen].count("/") == 1
    assert len(_chunk_bodies(seen)) == 2


@pytest.mark.parametrize("version", ["0.6.9", "0.6.12"])
def test_a_server_older_than_0_7_0_is_refused_before_chunking(version: str) -> None:
    seen: list[httpx.Request] = []
    with _client(version, seen) as client, pytest.raises(JmftsVersionError, match=version):
        client.chunk_document(7)
    assert _chunk_bodies(seen) == []


def test_an_unreadable_version_is_refused() -> None:
    seen: list[httpx.Request] = []
    with _client("dev", seen) as client, pytest.raises(JmftsVersionError, match="unreadable"):
        client.chunk_document(7)
    assert _chunk_bodies(seen) == []
