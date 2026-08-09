"""tau-jmfts: JMFTS (Fusion Tree Search) session store backend and REST client.

Public API (see docs/JMFTS-INTEGRATION-PLAN.md Sec3):

- JmftsClient: thin synchronous httpx wrapper over the JMFTS REST API.
- JmftsError: raised for any non-2xx JMFTS response (Fail-Early -- no fallback).
- JmftsSessionLog: the JMFTS-backed SessionLog / ConversationSession -- the τ
  entry tree mirrored 1:1 onto a JMFTS document subtree (Sec2).
- JmftsSessionCatalog: the SessionCatalog seam over JMFTS -- discovery
  (list/most_recent/resolve_ref), create/create_ephemeral/load/fork, delete.
- import_session / export_session: lossless JSONL <-> JMFTS conversation
  subtree round-trip (topology + cross-references remapped, entry ids are
  storage-local).

Reference: docs/JMFTS-INTEGRATION-PLAN.md
"""

from tau_jmfts.catalog import JmftsSessionCatalog
from tau_jmfts.client import DocumentDict, JmftsClient, JmftsError
from tau_jmfts.importer import export_session, import_session
from tau_jmfts.store import JmftsSessionLog

__all__ = [
    "JmftsClient",
    "JmftsError",
    "DocumentDict",
    "JmftsSessionLog",
    "JmftsSessionCatalog",
    "import_session",
    "export_session",
]
