"""SessionCatalog conformance — ``JmftsSessionCatalog``, against a live server.

The third implementation of the suite that also runs over the RAM-only catalog
(tau-agent-core) and ``FileSessionCatalog`` (tau-coding-agent). This is the run that
makes the seam a claim rather than a hope: the store here is a remote document
database with integer document ids, paging, and no filesystem anywhere — if the
contract quietly assumed a path or a local dict, it fails here.

Properties this suite deliberately does NOT cover stay in ``test_catalog_jmfts.py``:
the doc-id fast path in ``resolve_ref``, listing paging, corrupt-root error rows, and
the "deleted mid-listing" race. Those are about the JMFTS *mapping*, not about what a
catalog is.

Marker: ``jmfts``. Skips (never fails) when the server is unreachable — see
conftest.py. Every root document created here is deleted in teardown; DELETE cascades
over the subtree server-side.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tau_agent_core.testing import SessionCatalogContractTests
from tau_jmfts.catalog import JmftsSessionCatalog
from tau_jmfts.client import JmftsClient, JmftsError

pytestmark = pytest.mark.jmfts

TEST_PREFIX = "tau-jmfts-test"


class _RecordingCatalog(JmftsSessionCatalog):
    """A ``JmftsSessionCatalog`` that remembers every root it created.

    Teardown could instead re-``list()`` the run's scopes, but then a bug in
    ``list`` would silently leak documents on the shared dev server — exactly the
    failure this suite exists to detect. Recording at the point of creation keeps
    cleanup independent of the thing under test.
    """

    def __init__(self, client: JmftsClient, roots: list[int]) -> None:
        super().__init__(client)
        self._roots = roots

    def create(self, *args: Any, **kwargs: Any) -> Any:
        session = super().create(*args, **kwargs)
        self._roots.append(session.root_doc_id)
        return session

    def fork(self, *args: Any, **kwargs: Any) -> Any:
        session = super().fork(*args, **kwargs)
        self._roots.append(session.root_doc_id)
        return session


class TestJmftsSessionCatalogContract(SessionCatalogContractTests):
    @pytest.fixture(autouse=True)
    def _live_server(self, jmfts_url: str, jmfts_token: str | None):
        self._client = JmftsClient(jmfts_url, token=jmfts_token)
        self._roots: list[int] = []
        # Scopes are run-unique: this server is shared, and a contract test that
        # asserts "this cwd lists exactly one session" must not see another run's.
        self._run = uuid.uuid4().hex[:8]
        yield
        for root_id in self._roots:
            try:
                self._client.delete_document(root_id)
            except JmftsError:
                pass
        self._client.close()

    @pytest.fixture
    def cwd(self) -> str:
        return f"/tmp/{TEST_PREFIX}-{self._run}-a"

    @pytest.fixture
    def other_cwd(self) -> str:
        return f"/tmp/{TEST_PREFIX}-{self._run}-b"

    def make_catalog(self) -> JmftsSessionCatalog:
        return _RecordingCatalog(self._client, self._roots)

    def reopen(self, catalog) -> JmftsSessionCatalog:
        """A second catalog over the same server — i.e. τ started on another machine."""
        return _RecordingCatalog(self._client, self._roots)

    def unknown_ref(self) -> str:
        """A ref in this store's own spelling (a document id) that names no document."""
        return "2147483647"

    missing_ref_error = JmftsError
