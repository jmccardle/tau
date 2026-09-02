"""tau_jmfts.importer -- lossless JSONL <-> JMFTS conversation subtree.

``import_session`` reads a file-store ``.jsonl`` session (line 1 = header,
lines 2..N = append-only entries -- ``tau_coding_agent.session_store.Session``'s
on-disk shape) and materializes it as a JMFTS conversation subtree via
``JmftsClient.create_document`` calls, one per entry, preserving topology
(``parentId`` chains) and the τ session uuid. ``export_session`` is the
inverse: read a live ``JmftsSessionLog`` and write a ``.jsonl`` file the file
``Session.load`` can open directly.

**What "lossless" means here** (the defensible reading, per
docs/JMFTS-INTEGRATION-PLAN.md Sec2's own round-trip contract): the *tree and
its semantics* round-trip -- same topology, same cursor position, same
``ConversationTree`` context fold -- with entry IDS being storage-local. A
byte-identical round trip is impossible by construction: the file store's
8-hex entry ids and JMFTS's server-assigned numeric doc ids are different id
spaces, so SOME id necessarily changes on import. What must NOT change is
what those ids are used to reconstruct: the ``parentId`` chain (structural
topology) and the three cross-reference FIELDS that point at another entry by
id -- ``navigate.targetId``, ``compaction.firstKeptId``,
``branch_summary.fromId`` -- plus ``elide.firstKeptId`` (W3,
NODE-ADDRESSABLE-AGENTS.md), the summary-less splice anchor that reuses
``compaction``'s field rather than minting its own. Remapping is done by
FIELD NAME, not by entry kind (see ``_CROSS_REF_FIELDS`` below), precisely so
a new kind that reuses an existing field is covered without editing this
module. All are remapped consistently as each new JMFTS document is created
(old id -> newly-assigned id, in file order, which is always a valid
processing order because every one of these references only ever points at
an EARLIER entry -- append-only). Skipping this remapping is exactly the bug
this module's tests catch (and, as documented below, one this module
deliberately does NOT inherit from ``JmftsSessionLog.fork``, which does not
perform it -- see the final report).

``export_session`` needs NO remapping at all: ``Session.load``/
``ConversationTree`` never assume anything about id SHAPE, only string
uniqueness + ``parentId`` chaining, so JMFTS's own numeric-string ids are
already valid file-store entry ids as-is, self-consistently cross-referencing
each other within the exported file.

Reference: docs/JMFTS-INTEGRATION-PLAN.md Sec2 (the mapping), Sec2.3
(cross-references), Sec3.4 (fork -- the sibling bulk-copy operation this
module's remapping logic is NOT shared with, on purpose: fork's copy is
JMFTS->JMFTS with its own id space concerns, its bug is out of this module's
scope to fix).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from tau_jmfts.client import JmftsClient
from tau_jmfts.store import (
    _CROSS_REF_FIELDS,
    _HEADER_REQUIRED,
    _PROVENANCE_REF_FIELDS,
    _content_for,
    _title_for,
)
from tau_jmfts.store import JmftsSessionLog

# Cross-reference fields that name ANOTHER entry by id (Sec2.3) -- these must
# be remapped id-for-id as new JMFTS documents are created. Deliberately
# FIELD-keyed, not kind-keyed: ``store._remap_cross_refs`` (fork's own
# remapper) already matches this way and is the single source of truth, so a
# future splice-anchor kind that reuses ``firstKeptId`` -- as ``elide`` (W3,
# NODE-ADDRESSABLE-AGENTS.md) already does -- is covered without this module
# being told its name. A kind-keyed dict here once mapped only
# ``navigate``/``compaction``/``branch_summary`` and silently forwarded
# ``elide``'s ``firstKeptId`` unremapped, copying a stale file-store id into
# the JMFTS tree; ``ConversationTree`` still finds the anchor (its kind is in
# ``_SPLICE_ANCHOR_KINDS``) but the forward scan for the dangling id never
# matches, so every ancestor of the anchor silently drops out of
# ``context_for`` -- no exception, exactly the corruption ``append_elide``'s
# own ValueError exists to prevent. Every other kind (message, customMessage,
# customEntry, model_change, thinking_change, session_info, and any future/
# unknown kind) simply has none of these fields and is copied verbatim -- the
# whole point of tolerating unknown kinds (plan Sec1 research note: "unknown
# entry types are already tolerated").


def _read_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Stream a file-store ``.jsonl`` session: header (line 1) + entries."""
    header: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if header is None:
                if obj.get("type") != "session":
                    raise ValueError(f"{path}: first line is not a session header")
                header = obj
            else:
                entries.append(obj)
    if header is None:
        raise ValueError(f"{path}: empty session file (no header)")
    return header, entries


def _import_header(header: dict[str, Any]) -> dict[str, Any]:
    """The importable header, preserving every field the file store already
    has -- id/timestamp/cwd/parent are load-bearing for "lossless" and must
    NOT change. The one gap: the file header predates the ``hostname`` field
    (JMFTS-INTEGRATION-PLAN.md Sec2.2 / Sec5 item 3 -- not yet landed on the
    file-store side), so JMFTS's required-header check
    (``JmftsSessionLog._HEADER_REQUIRED``) would otherwise reject it. Filled
    with the CURRENT host running the import -- an honestly-labeled
    synthesized value for a field the source never captured, not a claim
    about where the original session actually ran.
    """
    imported = dict(header)
    imported.setdefault("hostname", socket.gethostname())
    missing = _HEADER_REQUIRED - imported.keys()
    if missing:
        raise ValueError(
            f"session header missing required field(s) {sorted(missing)}; "
            "refusing to import a header import_session cannot make well-formed"
        )
    return imported


def import_session(
    jsonl_path: str | Path,
    client: JmftsClient,
    *,
    host_parent_id: int | None = None,
) -> JmftsSessionLog:
    """Materialize a file-store ``.jsonl`` session as a JMFTS conversation
    subtree, preserving topology and the τ session uuid.

    One ``POST /documents`` per entry (Sec3.2's granularity -- no batch create
    yet, CR-3), processed in file order: every ``parentId``/``targetId``/
    ``firstKeptId``/``fromId`` reference in an append-only log only ever
    points at an entry that appears EARLIER in the file, so by the time an
    entry is processed, every id it could reference has already been assigned
    its new JMFTS doc id.
    """
    path = Path(jsonl_path)
    header, entries = _read_jsonl(path)
    imported_header = _import_header(header)

    root = client.create_document(
        title=f"tau:conversation {imported_header['id'][:8]}",
        usetype="tau:conversation",
        parent_id=host_parent_id,
        structured_content={"tau": imported_header},
        auto_embed=False,
    )
    root_doc_id = root["id"]

    old_to_new: dict[str, str] = {}
    for seq, entry in enumerate(entries, start=1):
        old_id = entry["id"]
        old_parent = entry.get("parentId")
        new_parent_doc_id = root_doc_id if old_parent is None else int(old_to_new[old_parent])

        kind = str(entry["type"])
        payload = {
            k: v for k, v in entry.items() if k not in ("id", "parentId", "type", "timestamp")
        }
        for field in _CROSS_REF_FIELDS:
            if field not in payload:
                continue
            old_ref = payload[field]
            payload[field] = old_to_new[old_ref] if old_ref is not None else None
        for field in _PROVENANCE_REF_FIELDS:
            # Remapped when the source came in with the file (the ordinary case for
            # a whole-log import), kept as it was when it did not. Unlike a splice
            # anchor, ``copiedFrom`` is history rather than structure: nothing folds
            # on it, so an id this import cannot resolve costs a hop of provenance
            # and not a region of context. See ``store._PROVENANCE_REF_FIELDS``.
            old_ref = payload.get(field)
            if old_ref is not None and old_ref in old_to_new:
                payload[field] = old_to_new[old_ref]

        tau_payload: dict[str, Any] = {"type": kind, "timestamp": entry["timestamp"], **payload}
        doc = client.create_document(
            title=_title_for(kind, payload, seq),
            content=_content_for(kind, payload),
            parent_id=new_parent_doc_id,
            usetype=f"tau:{kind}",
            structured_content={"tau": tau_payload, "seq": seq},
            auto_embed=False,
        )
        old_to_new[old_id] = str(doc["id"])

    return JmftsSessionLog.load(client, root_doc_id)


def export_session(log: JmftsSessionLog, jsonl_path: str | Path) -> None:
    """Write ``log`` as a file-store-shaped ``.jsonl`` -- ``Session.load`` can
    open the result directly.

    No id remapping needed on this direction (see the module docstring):
    JMFTS's numeric-string ids are already valid, self-consistent file-store
    entry ids exactly as ``log.entries()`` hands them back.
    """
    path = Path(jsonl_path)
    lines = [json.dumps(log.header)]
    lines.extend(json.dumps(entry) for entry in log.entries())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
