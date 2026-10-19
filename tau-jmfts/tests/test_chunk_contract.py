"""``chunk_document`` speaks JMFTS 0.7.0's ``ChunkRequest`` and refuses an older server.

No live server: an ``httpx.MockTransport`` stands in. Not marked ``jmfts``, so it
always runs.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tau_jmfts.client import JmftsClient, JmftsError, JmftsVersionError


def _client(version: str | None, seen: list[httpx.Request], *, health_down: int = 0) -> JmftsClient:
    failures = [health_down]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/":
            if failures[0]:
                failures[0] -= 1
                raise httpx.ConnectError("refused", request=request)
            body: dict[str, Any] = {"status": "ok"}
            if version is not None:
                body["version"] = version
            return httpx.Response(200, json=body)
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


def test_the_strategy_is_one_that_reads_max_words() -> None:
    """The server's default ``sentence`` strategy ignores ``max_words`` (#45)."""
    seen: list[httpx.Request] = []
    with _client("0.7.1", seen) as client:
        client.chunk_document(7)
    (body,) = _chunk_bodies(seen)
    assert body["strategy"] == "sentence_packed"
    assert "overlap" not in body


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


@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        ("0.7.0", True),
        ("0.7.1", True),
        ("0.8", True),
        ("0.8.0rc1", True),
        ("0.8.0.dev0", True),
        ("0.7.0.post1", True),
        ("1.0.0+local.3", True),
        ("0.7.0rc1", False),
        ("0.7.0.dev0", False),
        ("0.6.99", False),
    ],
)
def test_the_version_check_follows_pep_440(version: str, accepted: bool) -> None:
    seen: list[httpx.Request] = []
    with _client(version, seen) as client:
        if accepted:
            client.chunk_document(7)
            assert len(_chunk_bodies(seen)) == 1
        else:
            with pytest.raises(
                JmftsVersionError, match=version.replace(".", r"\.").replace("+", r"\+")
            ):
                client.chunk_document(7)
            assert _chunk_bodies(seen) == []


def test_a_health_body_without_a_version_is_refused() -> None:
    seen: list[httpx.Request] = []
    with _client(None, seen) as client, pytest.raises(JmftsVersionError, match="unreadable"):
        client.chunk_document(7)
    assert _chunk_bodies(seen) == []


def test_an_unreachable_health_check_is_a_transport_error_and_is_asked_again() -> None:
    seen: list[httpx.Request] = []
    with _client("0.7.1", seen, health_down=1) as client:
        with pytest.raises(JmftsError) as excinfo:
            client.chunk_document(7)
        assert not isinstance(excinfo.value, JmftsVersionError)
        assert excinfo.value.status_code == 0
        client.chunk_document(7)
    assert [r.url.path for r in seen].count("/") == 2
    assert len(_chunk_bodies(seen)) == 1


def test_the_session_store_path_never_asks_the_server_version() -> None:
    seen: list[httpx.Request] = []
    with _client("0.7.1", seen) as client:
        client.create_document(title="t", content="c", usetype="tau:message")
        client.get_document(1)
    assert "/" not in [r.url.path for r in seen]


async def test_ingest_refuses_an_old_server_before_writing_anything() -> None:
    """``jmfts_ingest`` used to create the document, then fail the version check."""
    from types import SimpleNamespace

    from tau_jmfts.ext import tools as tools_module

    seen: list[httpx.Request] = []
    client = _client("0.6.2", seen)
    registered: dict[str, Any] = {}
    api = SimpleNamespace(config={}, register_tool=lambda d: registered.__setitem__(d["name"], d))
    tools_module.register(api)
    log = SimpleNamespace(client=client, id="s")
    ctx = SimpleNamespace(_require_session=lambda: SimpleNamespace(session_log=log))
    with client, pytest.raises(JmftsVersionError):
        await registered["jmfts_ingest"]["execute"](
            "t1", {"title": "t", "content": "some text"}, None, None, ctx
        )
    assert [r.url.path for r in seen] == ["/"], "nothing written before the refusal"
