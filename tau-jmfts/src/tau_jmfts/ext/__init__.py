"""τ extensions shipped with ``tau-jmfts`` (JMFTS-INTEGRATION-PLAN.md Sec6).

Loaded with ``-e`` like any other extension and configured under the usual
``"extensions": {"<stem>": {...}}`` slice:

* :mod:`tau_jmfts.ext.enrich` (W13) — the deferred work the hot write path skips:
  embed and index a conversation so it becomes *findable*. Requires the jmfts store.
* :mod:`tau_jmfts.ext.tools` (W15) — agent-facing ``jmfts_search`` / ``jmfts_read`` /
  ``jmfts_ingest``. Works regardless of which session store is active: a file-backed
  session can still search JMFTS.

Together they close the loop the rest of the integration was built for. Until they
exist, τ writes conversations into a retrieval appliance that cannot retrieve them —
every ``create_document`` on the write path passes ``auto_embed=False``, so nothing
τ persists is searchable by anything.
"""
