"""tau-jmfts test fixtures.

The live-integration tests (marker ``jmfts``) talk to a real JMFTS server.
Target URL: ``$JMFTS_TEST_URL`` if set, else the LAN dev instance used during
W11 development. Auth: the server now requires a bearer token, read from
``$JMFTS_API_TOKEN`` (never hardcoded here) and threaded into every live client
via the ``jmfts_token`` fixture.

Skip (not fail) when the server is plain unreachable (connection refused/timeout)
OR when it rejects the token with 401/403 -- a missing/invalid token is an
"environment not configured" condition, not a code bug. But any OTHER error
(e.g. a 500) must fail the test loudly, never skip it (Fail-Early: a 500 is a
real bug signal, not an "environment not ready" signal).
"""

from __future__ import annotations

import os

import httpx
import pytest

JMFTS_TEST_URL = os.environ.get("JMFTS_TEST_URL", "http://192.168.1.100:8007")
JMFTS_API_TOKEN = os.environ.get("JMFTS_API_TOKEN")

# Every document this test suite creates carries this usetype/title prefix so
# a human (or a cleanup script) can find and nuke anything left behind by a
# crashed run without guessing.
TEST_PREFIX = "tau-jmfts-test"


def _probe_server(url: str, token: str | None) -> str:
    """Classify the server's reachability: ``"ok"`` | ``"unreachable"`` | ``"unauthorized"``.

    Deliberately does NOT treat a generic non-2xx (e.g. a 500) as "unreachable" --
    only a transport-level failure counts, so a server that connects but errors
    fails tests loudly. A 401/403 is singled out as ``"unauthorized"``: that is a
    misconfigured/missing ``JMFTS_API_TOKEN``, an environment problem the caller
    should be told about with a clear skip, not a confusing hard failure.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=3.0) as probe:
            response = probe.get(url, headers=headers)
    except httpx.TransportError:
        return "unreachable"
    if response.status_code in (401, 403):
        return "unauthorized"
    return "ok"


@pytest.fixture(scope="session")
def jmfts_token() -> str | None:
    """The bearer token for the live server, from ``$JMFTS_API_TOKEN`` (or None)."""
    return JMFTS_API_TOKEN


@pytest.fixture(scope="session")
def jmfts_url() -> str:
    status = _probe_server(JMFTS_TEST_URL, JMFTS_API_TOKEN)
    if status == "unreachable":
        pytest.skip(
            f"JMFTS server unreachable at {JMFTS_TEST_URL} (set JMFTS_TEST_URL to override)"
        )
    if status == "unauthorized":
        pytest.skip(
            f"JMFTS server at {JMFTS_TEST_URL} rejected the token with 401/403 -- "
            "set JMFTS_API_TOKEN to a valid bearer token for this instance"
        )
    return JMFTS_TEST_URL
