"""Error-path tests for JmftsClient: Fail-Early, no swallowing.

A non-2xx response must raise JmftsError with the server's detail preserved
-- never return None, never retry, never fall back to a default value.

Reference: docs/JMFTS-INTEGRATION-PLAN.md Sec8 (hard-fail on outage/error).
"""

from __future__ import annotations

import pytest

from tau_jmfts.client import JmftsClient, JmftsError

pytestmark = pytest.mark.jmfts


@pytest.fixture
def client(jmfts_url: str, jmfts_token: str | None):
    c = JmftsClient(jmfts_url, token=jmfts_token)
    yield c
    c.close()


def test_get_nonexistent_document_raises_404(client: JmftsClient) -> None:
    # A document id this large should not exist on any real corpus.
    with pytest.raises(JmftsError) as excinfo:
        client.get_document(2_147_483_647)
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail  # server's own message preserved, not swallowed


def test_delete_nonexistent_document_raises_404(client: JmftsClient) -> None:
    with pytest.raises(JmftsError) as excinfo:
        client.delete_document(2_147_483_647)
    assert excinfo.value.status_code == 404


def test_bad_payload_type_raises_422(client: JmftsClient) -> None:
    # parent_id must be an int|null; a non-numeric string fails FastAPI/pydantic
    # validation server-side -> 422, with the ValidationError detail preserved.
    with pytest.raises(JmftsError) as excinfo:
        client.create_document(title="bad payload test", parent_id="not-an-int")  # type: ignore[arg-type]
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail  # the ValidationError list, not swallowed


def test_update_nonexistent_document_raises_404(client: JmftsClient) -> None:
    with pytest.raises(JmftsError) as excinfo:
        client.update_document(2_147_483_647, title="does not matter")
    assert excinfo.value.status_code == 404


def test_error_message_includes_method_and_url(client: JmftsClient) -> None:
    with pytest.raises(JmftsError) as excinfo:
        client.get_document(2_147_483_647)
    err = excinfo.value
    assert err.method == "GET"
    assert "/documents/2147483647" in err.url
    assert "404" in str(err)
