"""Client-only tests that need no live JMFTS server -- transport-level failure
mapping (connection refused) and lifecycle (close/reuse). Not marked ``jmfts``:
always runs.
"""

from __future__ import annotations

import pytest

from tau_jmfts.client import JmftsClient, JmftsError


def test_unreachable_server_raises_jmfts_error_not_httpx_error() -> None:
    # Port 1 is a reserved/unassigned TCP port -- connection refused immediately,
    # no risk of accidentally hitting a real service.
    client = JmftsClient("http://127.0.0.1:1", timeout=2.0)
    try:
        with pytest.raises(JmftsError) as excinfo:
            client.health()
        assert excinfo.value.status_code == 0
    finally:
        client.close()


def test_close_then_call_raises() -> None:
    client = JmftsClient("http://127.0.0.1:1")
    client.close()
    with pytest.raises(RuntimeError):
        client.health()


def test_one_httpx_client_reused_across_calls() -> None:
    """The client must not construct a fresh httpx.Client per request --
    verified by identity of the underlying transport across two calls.
    """
    client = JmftsClient("http://127.0.0.1:1", timeout=2.0)
    try:
        inner = client._client
        with pytest.raises(JmftsError):
            client.health()
        with pytest.raises(JmftsError):
            client.health()
        assert client._client is inner
    finally:
        client.close()
