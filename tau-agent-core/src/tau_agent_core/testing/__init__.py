"""Test kits τ ships **with the interface**, not beside it.

Two suites, one per seam a second store has to satisfy:

- ``SessionLogContractTests`` — the ``SessionLog`` *entry* algebra (append, cursor,
  branch, compaction: what a session's history does).
- ``SessionCatalogContractTests`` — the ``SessionCatalog`` *construction* algebra
  (create, list, load-by-ref, fork, resolve: how a session is found and made).

They live in the library (rather than in some package's ``tests/``) for three
reasons:

- Dependency direction. ``tau-agent-core`` must never import ``tau-coding-agent``
  (session_log.py:8-9), so a suite that runs over *all* implementations cannot live in
  core's own test dir — but it can be *imported by* each package's tests.
- ``tau-jmfts`` is optional and not installed by default; a single root-level suite
  importing every store would need ``importorskip`` gymnastics.
- A downstream fork writing its own database-backed store gets the conformance suite
  by importing it. The contract is the code, not a document.

Usage::

    from tau_agent_core.testing import SessionCatalogContractTests, SessionLogContractTests

    class TestMyStore(SessionLogContractTests):
        def make_log(self):
            return MyStore(...)

    class TestMyCatalog(SessionCatalogContractTests):
        def make_catalog(self):
            return MyCatalog(...)
"""

from tau_agent_core.testing.session_catalog_contract import SessionCatalogContractTests
from tau_agent_core.testing.session_log_contract import SessionLogContractTests

__all__ = ["SessionCatalogContractTests", "SessionLogContractTests"]
