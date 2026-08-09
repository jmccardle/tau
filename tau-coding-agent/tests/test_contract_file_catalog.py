"""SessionCatalog conformance — the on-disk ``FileSessionCatalog`` (τ's default store).

The same suite that runs against the RAM-only catalog in tau-agent-core and against
``JmftsSessionCatalog`` in tau-jmfts. Green in all three is the actual claim behind
``--store``: whichever catalog the config resolves, the TUI and headless get the same
answers out of ``create``/``list``/``load``/``fork``/``resolve_ref``.

This store is the one whose ref *spelling* leaked into frontend tests (a
``SessionInfo.ref`` here is a ``.jsonl`` path, so assertions could accidentally depend
on paths existing). The contract only ever hands a ref back to ``load()``, which is
the whole guarantee a frontend is entitled to.
"""

from __future__ import annotations

import pytest

from tau_agent_core.testing import SessionCatalogContractTests
from tau_coding_agent.session_store import FileSessionCatalog


class TestFileSessionCatalogContract(SessionCatalogContractTests):
    @pytest.fixture(autouse=True)
    def _base_dir(self, tmp_path):
        """Root every session under ``tmp_path`` — never the developer's ~/.tau."""
        self._sessions_dir = tmp_path / "sessions"

    def make_catalog(self) -> FileSessionCatalog:
        return FileSessionCatalog(self._sessions_dir)

    def reopen(self, catalog) -> FileSessionCatalog:
        """A second catalog over the same directory — i.e. the next ``tau`` run."""
        return FileSessionCatalog(self._sessions_dir)

    def unknown_ref(self) -> str:
        """A ref in this store's own spelling (a path) that names no file."""
        return str(self._sessions_dir / "nope" / "does-not-exist.jsonl")

    missing_ref_error = FileNotFoundError
