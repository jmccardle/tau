"""import_session / export_session: lossless JSONL <-> JMFTS round trip, live.

"Lossless" here (see importer.py's module docstring for the full argument) is
NOT byte-identity -- entry ids necessarily change (8-hex file ids -> JMFTS
numeric doc ids). It means: topology (the ``parentId`` chain), every
cross-reference (``navigate.targetId`` / ``compaction.firstKeptId`` /
``branch_summary.fromId``), and the ``ConversationTree`` context fold at every
interesting cursor position round-trip exactly, and the τ session uuid is
preserved. This suite proves all three, not just asserts them:

1. A positional "topology signature" (kind + parent's POSITION, ids erased)
   equality across the original file entries, the live JMFTS entries, and the
   re-exported-and-reloaded file entries.
2. A positional "cross-reference signature" (same idea, for
   targetId/firstKeptId/fromId) -- the direct proof that remapping happened
   and landed on the RIGHT new id, not just some id.
3. ``ConversationTree(...).context_for(leaf=...)`` equality at several
   cursors (including one anchored past the compaction splice and one inside
   the abandoned/branch-summarized branch), which is what would visibly break
   for an end user if either of the above were wrong.

Marker: ``jmfts``. Skips only when the server is unreachable -- see
conftest.py. Every root document created here is deleted in teardown.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

import tau_coding_agent.session_store as _session_store_module
from tau_agent_core.conversation_tree import ConversationTree
from tau_coding_agent.session_store import Session
from tau_jmfts.client import JmftsClient
from tau_jmfts.importer import export_session, import_session
from tau_jmfts.store import JmftsSessionLog

pytestmark = pytest.mark.jmfts

TEST_PREFIX = "tau-jmfts-test"


@pytest.fixture(autouse=True)
def _isolate_session_listeners(monkeypatch):
    """``tau_coding_agent.session_store._session_listeners`` is a process-wide
    module global; a REAL ``Parley`` app (elsewhere in this monorepo's test
    suite) registers a listener on it via ``subscribe_session_events`` and
    never unsubscribes ("harmless in a one-shot process" -- session_store.py's
    own comment). Across a shared pytest process that leaks into any LATER
    test file that calls ``Session.create``/``append_*`` outside a running
    event loop, surfacing as ``RuntimeError: no running event loop`` from an
    unrelated listener. Two tau-coding-agent test files already guard against
    exactly this with this same reset (test_app_extension_loading.py,
    test_e5_integration_floor.py); this file uses the file ``Session`` too
    (per the importer's own "load both with the file Session" contract), so
    it needs the identical isolation -- not a fix for MY code, defensive
    isolation from a documented shared-global-state hazard in a package this
    task's scope forbids touching."""
    monkeypatch.setattr(_session_store_module, "_session_listeners", [])


@pytest.fixture
def client(jmfts_url: str, jmfts_token: str | None):
    c = JmftsClient(jmfts_url, token=jmfts_token)
    yield c
    c.close()


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _topology_signature(entries: list[dict[str, Any]]) -> list[tuple[str, int | None]]:
    """(kind, parent's POSITION in this same list) per entry, ids erased."""
    index = {e["id"]: i for i, e in enumerate(entries)}
    return [
        (str(e["type"]), index[e["parentId"]] if e.get("parentId") is not None else None)
        for e in entries
    ]


_CROSS_REF_FIELD = {
    "navigate": "targetId",
    "compaction": "firstKeptId",
    "branch_summary": "fromId",
    # ``elide`` (W3, NODE-ADDRESSABLE-AGENTS.md) reuses ``compaction``'s field --
    # the summary-less splice anchor. Included here so the crossref signature
    # below actually exercises it; see test_import_preserves_elide_crossref.
    "elide": "firstKeptId",
}


def _crossref_signature(entries: list[dict[str, Any]]) -> list[tuple[str, int | None]]:
    """(kind, referenced entry's POSITION) for every navigate/compaction/
    branch_summary entry, ids erased -- the direct proof the importer's
    cross-reference remap landed on the structurally-equivalent target."""
    index = {e["id"]: i for i, e in enumerate(entries)}
    sig: list[tuple[str, int | None]] = []
    for e in entries:
        field = _CROSS_REF_FIELD.get(str(e["type"]))
        if field is None:
            continue
        ref = e.get(field)
        sig.append((str(e["type"]), index[ref] if ref is not None else None))
    return sig


def _context_at(entries: list[dict[str, Any]], leaf: str) -> list[dict[str, Any]]:
    return ConversationTree(entries, leaf).context_for()


def _path_of(session: Session) -> Path:
    """``Session.path`` is ``Path | None`` (``None`` only for an in-memory
    session); every session built here is persisted via ``Session.create``,
    so this narrows the type for ``import_session``'s benefit."""
    assert session.path is not None
    return session.path


def _build_rich_source_session(tmp_path: Path) -> Session:
    """A file-store session exercising every cross-reference kind: branching
    (navigate to an ancestor), a branch_summary (re-parenting re-branch), a
    compaction (splice anchor), and a final root-level re-branch
    (navigate(None))."""
    session = Session.create(
        "/tmp/tau-jmfts-importer-source",
        "test-model",
        "test-backend",
        base_dir=tmp_path / "sessions",
        system_prompt="sys",
    )
    # entries(): [0]=model_change, [1]=message(system, "sys")
    session.append_message(_msg("user", "hello"))  # [2]
    session.append_message(_msg("assistant", "hi"))  # [3] branch A tip

    # Branch B: navigate back to "hello" ([2]), diverge.
    hello_id = session.entries()[2]["id"]
    session.append_navigate(hello_id)  # [4]
    alt_id = session.append_message(_msg("assistant", "alt reply"))  # [5]
    session.append_branch_summary("summarized the alt branch", from_id=alt_id)  # [6]
    session.append_message(_msg("user", "back on track"))  # [7]

    # Back to branch A ([3]): append a compaction anchored at "hi".
    hi_id = session.entries()[3]["id"]
    session.append_navigate(hi_id)  # [8]
    session.append_compaction("early chat summary", first_kept_id=hi_id, tokens_before=500)  # [9]
    session.append_message(_msg("user", "continue after compaction"))  # [10]

    # A brand-new root-level branch.
    session.append_navigate(None)  # [11]
    session.append_message(_msg("user", "fresh root-level branch"))  # [12]
    return session


@pytest.fixture
def rich_source(tmp_path: Path) -> Session:
    return _build_rich_source_session(tmp_path)


# ---------------------------------------------------------------------------
# 1. Simple round trip: topology + uuid.
# ---------------------------------------------------------------------------


def test_import_preserves_uuid_and_topology(client: JmftsClient, tmp_path: Path) -> None:
    session = Session.create(
        "/tmp/tau-jmfts-importer-simple",
        "test-model",
        "test-backend",
        base_dir=tmp_path / "sessions",
    )
    a = session.append_message(_msg("user", "hi"))
    session.append_message(_msg("assistant", "yo"))
    session.append_navigate(a)
    session.append_message(_msg("assistant", "alt"))

    log: JmftsSessionLog | None = None
    try:
        log = import_session(_path_of(session), client)
        assert log.id == session.id
        assert log.header["cwd"] == session.cwd
        assert log.header["parent"] == session.parent
        assert _topology_signature(log.entries()) == _topology_signature(session.entries())
    finally:
        if log is not None:
            client.delete_document(log.root_doc_id)


def test_import_rejects_header_missing_required_field(client: JmftsClient, tmp_path: Path) -> None:
    path = tmp_path / "malformed.jsonl"
    # No "cwd" -- import_session must Fail-Early rather than import a header
    # it cannot make well-formed.
    header = {
        "type": "session",
        "version": 1,
        "id": uuid.uuid4().hex,
        "timestamp": "2026-07-12T00:00:00.000Z",
        "parent": None,
    }
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required field"):
        import_session(path, client)


# ---------------------------------------------------------------------------
# 2. The full round trip: branches + compaction + navigate + branch_summary.
# ---------------------------------------------------------------------------


def test_import_export_round_trip_preserves_topology_and_crossrefs(
    client: JmftsClient, rich_source: Session, tmp_path: Path
) -> None:
    original_entries = rich_source.entries()

    log: JmftsSessionLog | None = None
    try:
        log = import_session(_path_of(rich_source), client)
        jmfts_entries = log.entries()

        assert _topology_signature(jmfts_entries) == _topology_signature(original_entries)
        assert _crossref_signature(jmfts_entries) == _crossref_signature(original_entries)

        exported_path = tmp_path / "exported.jsonl"
        export_session(log, exported_path)
        exported_reloaded = Session.load(exported_path)
        exported_entries = exported_reloaded.entries()

        assert exported_reloaded.id == rich_source.id
        assert _topology_signature(exported_entries) == _topology_signature(original_entries)
        assert _crossref_signature(exported_entries) == _crossref_signature(original_entries)
    finally:
        if log is not None:
            client.delete_document(log.root_doc_id)


def test_import_export_round_trip_preserves_context_fold_at_every_branch(
    client: JmftsClient, rich_source: Session, tmp_path: Path
) -> None:
    original_entries = rich_source.entries()

    log: JmftsSessionLog | None = None
    try:
        log = import_session(_path_of(rich_source), client)
        jmfts_entries = log.entries()
        assert len(jmfts_entries) == len(original_entries)

        exported_path = tmp_path / "exported.jsonl"
        export_session(log, exported_path)
        exported_entries = Session.load(exported_path).entries()

        # Positions of interest: the tip of each branch, and the compaction's
        # own anchor position -- the set of cursors that would visibly show a
        # broken splice or a dangling cross-reference to an end user.
        interesting_positions = {
            "branch_summary_tip": 7,  # "back on track" (after the branch_summary)
            "compaction_tip": 10,  # "continue after compaction"
            "final_root_branch": 12,  # "fresh root-level branch"
        }
        for label, idx in interesting_positions.items():
            orig_ctx = _context_at(original_entries, original_entries[idx]["id"])
            jmfts_ctx = _context_at(jmfts_entries, jmfts_entries[idx]["id"])
            exported_ctx = _context_at(exported_entries, exported_entries[idx]["id"])
            assert jmfts_ctx == orig_ctx, f"JMFTS context diverged at {label}"
            assert exported_ctx == orig_ctx, f"exported context diverged at {label}"

        # And the compaction message itself is really in the folded context
        # (not silently dropped -- the dangling-firstKeptId failure mode).
        compaction_ctx = _context_at(jmfts_entries, jmfts_entries[10]["id"])
        assert any(
            "early chat summary" in block.get("text", "")
            for m in compaction_ctx
            for block in (m.get("content") if isinstance(m.get("content"), list) else [])
            if isinstance(block, dict)
        )
    finally:
        if log is not None:
            client.delete_document(log.root_doc_id)


def test_import_preserves_elide_crossref_and_context_fold(
    client: JmftsClient, tmp_path: Path
) -> None:
    """Regression for the importer's kind-keyed ``_CROSS_REF_FIELD`` gap:
    ``elide`` (W3) was not a recognized kind, so its ``firstKeptId`` -- naming
    another entry by id -- was copied through verbatim as a stale 8-hex
    file-store id instead of the newly-assigned JMFTS doc id.
    ``ConversationTree`` still finds the anchor by KIND
    (``_SPLICE_ANCHOR_KINDS``), but the forward scan for the boundary id then
    never matches anything in the JMFTS-id-keyed tree, so ``found`` stays
    ``False`` for the entire pre-anchor span and every ancestor of the anchor
    silently drops out of ``context_for`` -- no exception, exactly the
    corruption ``append_elide``'s own ValueError exists to prevent
    (session_store.py), reintroduced through the import path. Proven two ways:
    the crossref signature (position-based, ids erased) and the actual folded
    context at the leaf.
    """
    session = Session.create(
        "/tmp/tau-jmfts-importer-elide",
        "test-model",
        "test-backend",
        base_dir=tmp_path / "sessions",
    )
    session.append_message(_msg("user", "before the elide"))  # dropped by the fold
    kept_id = session.append_message(_msg("assistant", "kept from here"))
    session.append_navigate(kept_id)
    session.append_elide(first_kept_id=kept_id)
    session.append_message(_msg("user", "after the elide"))

    original_entries = session.entries()

    log: JmftsSessionLog | None = None
    try:
        log = import_session(_path_of(session), client)
        jmfts_entries = log.entries()

        assert _topology_signature(jmfts_entries) == _topology_signature(original_entries)
        assert _crossref_signature(jmfts_entries) == _crossref_signature(original_entries)

        orig_ctx = _context_at(original_entries, original_entries[-1]["id"])
        jmfts_ctx = _context_at(jmfts_entries, jmfts_entries[-1]["id"])
        assert jmfts_ctx == orig_ctx

        # The direct proof: "kept from here" must survive the fold. Under the
        # bug this silently vanishes with the assertion above still able to
        # pass only if both sides were equally broken -- so pin the content,
        # not just the equality.
        def _has_text(ctx: list[dict[str, Any]], needle: str) -> bool:
            return any(
                needle in block.get("text", "")
                for m in ctx
                for block in (m.get("content") if isinstance(m.get("content"), list) else [])
                if isinstance(block, dict)
            )

        assert _has_text(jmfts_ctx, "kept from here")
        assert not _has_text(jmfts_ctx, "before the elide")
    finally:
        if log is not None:
            client.delete_document(log.root_doc_id)


def test_export_output_is_loadable_by_file_session(client: JmftsClient, tmp_path: Path) -> None:
    log = JmftsSessionLog.create(
        client,
        cwd="/tmp/tau-jmfts-importer-export",
        model="test-model",
        backend="test-backend",
    )
    try:
        log.append_message(_msg("user", "hello"))
        log.append_message(_msg("assistant", "hi"))

        path = tmp_path / "exported.jsonl"
        export_session(log, path)
        reloaded = Session.load(path)

        assert reloaded.id == log.id
        assert reloaded.messages == log.messages
        assert reloaded.context == log.context
    finally:
        client.delete_document(log.root_doc_id)
