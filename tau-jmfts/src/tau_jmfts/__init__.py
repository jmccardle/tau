"""tau-jmfts: JMFTS (Fusion Tree Search) session store backend and REST client.

Public API (see docs/JMFTS-INTEGRATION-PLAN.md Sec3):

- JmftsClient: thin synchronous httpx wrapper over the JMFTS REST API.
- JmftsError: raised for any non-2xx JMFTS response (Fail-Early -- no fallback).
- JmftsTextTooLongError: the one subclass, for the server's structured
  ``text_too_long`` refusal -- its measurement of whether text fits the
  embedder's 512-token window, which a caller that can chunk should act on.
- JmftsSessionLog: the JMFTS-backed SessionLog / ConversationSession -- the τ
  entry tree mirrored 1:1 onto a JMFTS document subtree (Sec2).
- JmftsSessionCatalog: the SessionCatalog seam over JMFTS -- discovery
  (list/most_recent/resolve_ref), create/create_ephemeral/load/fork, delete.
- import_session / export_session: lossless JSONL <-> JMFTS conversation
  subtree round-trip (topology + cross-references remapped, entry ids are
  storage-local).

Reference: docs/JMFTS-INTEGRATION-PLAN.md
"""

# This distribution's version, read at build time by pyproject.toml's
# [tool.setuptools.dynamic]. Kept in lockstep with the other three packages.
__version__ = "0.9.6"

from tau_jmfts.catalog import JmftsSessionCatalog
from tau_jmfts.client import DocumentDict, JmftsClient, JmftsError, JmftsTextTooLongError
from tau_jmfts.importer import export_session, import_session
from tau_jmfts.store import JmftsSessionLog

__all__ = [
    "__version__",
    "JmftsClient",
    "JmftsError",
    "JmftsTextTooLongError",
    "DocumentDict",
    "JmftsSessionLog",
    "JmftsSessionCatalog",
    "import_session",
    "export_session",
]
