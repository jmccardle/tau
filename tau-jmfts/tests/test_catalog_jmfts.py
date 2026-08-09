"""JmftsSessionCatalog: what is TRUE OF JMFTS, against a live server.

The catalog algebra itself — create/load/list/fork/most_recent/resolve_ref — is
not restated here. It is ``SessionCatalogContractTests``, run over this store in
test_contract_catalog_jmfts.py and over the file and RAM-only catalogs elsewhere;
twelve tests in this file spelled out behaviours the other stores owe too, and
they moved there. What remains is everything the storage-agnostic contract cannot
know: the doc-id ref spelling and its fast path, server-side paging, root-ness
verification, deletion, and the three distinct ways a *live server* can fail a
listing (corrupt row, deleted mid-listing, 500).

Marker: ``jmfts``. Skips (does not fail) only when the server is unreachable --
see conftest.py. Every root document created here is deleted in test teardown
(delete cascades over the whole subtree server-side).

Reference: docs/JMFTS-INTEGRATION-PLAN.md Sec2.4 (root-ness), Sec3.1 (config),
Sec3.3 (read path), Sec3.4 (fork/delete); tau_agent_core.session_catalog (the
ABC); tau_agent_core.testing.SessionCatalogContractTests (the shared suite).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from tau_agent_core.session_catalog import ConversationSession
from tau_agent_core.session_log import InMemorySessionLog
from tau_jmfts.catalog import JmftsSessionCatalog, _EphemeralConversationSession
from tau_jmfts.client import JmftsClient, JmftsError
from tau_jmfts.store import JmftsSessionLog

pytestmark = pytest.mark.jmfts

TEST_PREFIX = "tau-jmfts-test"


def _cwd(run_id: str) -> str:
    return f"/tmp/{TEST_PREFIX}-{run_id}"


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


@pytest.fixture
def client(jmfts_url: str, jmfts_token: str | None):
    c = JmftsClient(jmfts_url, token=jmfts_token)
    yield c
    c.close()


@pytest.fixture
def catalog(client: JmftsClient) -> JmftsSessionCatalog:
    return JmftsSessionCatalog(client)


@pytest.fixture
def run_id() -> str:
    return uuid.uuid4().hex[:8]


def _cleanup(client: JmftsClient, *roots: int) -> None:
    for root_id in roots:
        try:
            client.delete_document(root_id)
        except JmftsError:
            pass


# ---------------------------------------------------------------------------
# create_ephemeral -- the honest, RAM-only answer
# ---------------------------------------------------------------------------


def test_create_ephemeral_writes_nothing_to_jmfts(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    """The load-bearing property: an ephemeral session must not appear
    anywhere in JMFTS, before or after activity on it -- proving
    ``create_ephemeral`` never issues a single ``POST /documents``."""
    scope = _cwd(run_id)
    before = client.list_documents(usetype="tau:conversation", title_prefix=f"[{TEST_PREFIX}]")

    session = catalog.create_ephemeral(scope, "test-model", "test-backend", system_prompt="sys")
    session.append_message(_msg("user", "hello"))
    session.append_message(_msg("assistant", "hi"))
    session.append_navigate(None)
    session.append_session_info("renamed")

    after = client.list_documents(usetype="tau:conversation", title_prefix=f"[{TEST_PREFIX}]")
    assert before == after
    assert session.messages == [
        {"role": "system", "content": "sys"},
        _msg("user", "hello"),
        _msg("assistant", "hi"),
    ]
    assert session.display_title() == "renamed"


def test_create_ephemeral_not_backed_by_in_memory_session_log_alone() -> None:
    """Confirms the documented reason ``create_ephemeral`` needed its own
    wrapper: a bare ``InMemorySessionLog`` does NOT satisfy
    ``ConversationSession`` (no header/messages/context/model/backend/
    display_title/append_model_change/append_session_info)."""
    assert not isinstance(InMemorySessionLog(), ConversationSession)


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------


def test_fork_rejects_non_jmfts_source(catalog: JmftsSessionCatalog, run_id: str) -> None:
    foreign = _EphemeralConversationSession.create(_cwd(run_id), "m", "b")
    with pytest.raises(TypeError, match="JMFTS-backed"):
        catalog.fork(foreign, _cwd(run_id))


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_conversation(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    session = catalog.create(_cwd(run_id), "test-model", "test-backend")
    root_id = session.root_doc_id
    catalog.delete(str(root_id))
    with pytest.raises(JmftsError) as excinfo:
        catalog.load(str(root_id))
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# resolve_ref: the JMFTS doc-id fast path
# ---------------------------------------------------------------------------


def test_resolve_ref_by_doc_id_fast_path(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    session = catalog.create(_cwd(run_id), "test-model", "test-backend")
    try:
        resolved = catalog.resolve_ref(str(session.root_doc_id), cwd=_cwd(run_id))
        assert resolved.id == session.id
    finally:
        _cleanup(client, session.root_doc_id)


def test_resolve_ref_nonexistent_doc_id_falls_through_to_id_search(
    catalog: JmftsSessionCatalog,
) -> None:
    # An all-digits ref that (overwhelmingly likely) names neither a real doc
    # id nor a real session-id prefix (session ids are uuid4 hex, essentially
    # never 12 digits) -- the 404 must fall through cleanly to the
    # storage-agnostic id search, which then correctly reports no match,
    # rather than the fast path's JmftsError propagating raw.
    with pytest.raises(LookupError, match="no session matches"):
        catalog.resolve_ref("999999999999", cwd=None)


def test_resolve_ref_malformed_doc_surfaces_loudly_not_reinterpreted(
    catalog: JmftsSessionCatalog, client: JmftsClient
) -> None:
    """A digit ref naming a REAL but non-conversation document must raise the
    real corruption (ValueError from JmftsSessionLog.load), never be silently
    swallowed and reinterpreted as a session-id search."""
    doc = client.create_document(
        title=f"[{TEST_PREFIX}] not a conversation",
        usetype="some:other-type",
        structured_content={"whatever": True},
        auto_embed=False,
    )
    try:
        with pytest.raises(ValueError):
            catalog.resolve_ref(str(doc["id"]), cwd=None)
    finally:
        client.delete_document(doc["id"])


# ---------------------------------------------------------------------------
# list: cwd scoping, pagination, root-ness, malformed-root tolerance
# ---------------------------------------------------------------------------


def test_list_pages_across_multiple_pages(client: JmftsClient, run_id: str) -> None:
    # list_page_size=2 forces the paging loop to run multiple round trips for
    # 5 sessions -- proving list() doesn't silently truncate at the first
    # (short-of-total) page.
    catalog = JmftsSessionCatalog(client, list_page_size=2)
    scope = _cwd(run_id)
    created = [catalog.create(scope, "test-model", "test-backend") for _ in range(5)]
    try:
        result_ids = {i.id for i in catalog.list(scope)}
        assert result_ids == {s.id for s in created}
    finally:
        _cleanup(client, *[s.root_doc_id for s in created])


def test_list_skips_malformed_root_without_raising(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    scope = _cwd(run_id)
    good = catalog.create(scope, "test-model", "test-backend")
    # Same usetype as a real conversation root, but a header missing required
    # fields -- must be filtered out client-side (Sec2.4), never raise.
    malformed = client.create_document(
        title=f"[{TEST_PREFIX}] malformed root",
        usetype="tau:conversation",
        structured_content={"tau": {"type": "session", "cwd": scope}},
        auto_embed=False,
    )
    try:
        infos = catalog.list(scope)
        assert [i.id for i in infos] == [good.id]
        assert str(malformed["id"]) not in {i.ref for i in infos}
    finally:
        _cleanup(client, good.root_doc_id, malformed["id"])


# ---------------------------------------------------------------------------
# Cost measurement (W11 report requirement): list()'s per-session query cost.
# ---------------------------------------------------------------------------


def test_list_cost_for_twenty_sessions(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    """Not a correctness test -- measures list()'s real cost against the live
    server so the report can state a real number instead of a guess. Each
    session carries a system prompt + 2 messages (4 entries incl. the seeded
    model_change), the minimum realistic shape."""
    scope = _cwd(run_id)
    created = []
    try:
        for i in range(20):
            s = catalog.create(scope, "test-model", "test-backend", system_prompt="sys")
            s.append_message(_msg("user", f"question {i}"))
            s.append_message(_msg("assistant", f"answer {i}"))
            created.append(s)

        start = time.perf_counter()
        infos = catalog.list(scope)
        elapsed = time.perf_counter() - start

        assert len(infos) == 20
        print(
            f"\n[cost] JmftsSessionCatalog.list() over 20 sessions: "
            f"{elapsed:.3f}s total, {elapsed / 20 * 1000:.1f}ms/session"
        )
    finally:
        _cleanup(client, *[s.root_doc_id for s in created])


# ---------------------------------------------------------------------------
# list(): the three ways a load can fail are three DIFFERENT answers
#
# These corrupt / break a real conversation on the live server rather than
# mocking the store, because the bug being pinned was that the catalog's
# `except (ValueError, JmftsError): return None` swallowed a REAL integrity
# violation the store raises on purpose.
# ---------------------------------------------------------------------------


def _forge_second_writer(client: JmftsClient, session: JmftsSessionLog) -> None:
    """Simulate a SECOND WRITER on a conversation (the invariant τ's store treats
    as hard, Sec2.3): write an entry document straight through the client with a
    seq the writer has already used. It lands with the highest doc id, so doc-id
    order (the load path's "insertion order") now disagrees with the writer's own
    seq counter -- exactly what `JmftsSessionLog.load`'s cross-check exists to
    catch, and what a real concurrent τ process would produce.
    """
    client.create_document(
        title="forged",
        content="forged",
        parent_id=session.root_doc_id,
        usetype="tau:message",
        structured_content={
            "tau": {
                "type": "message",
                "timestamp": "2020-01-01T00:00:00+00:00",
                "message": _msg("user", "written by a second process"),
            },
            "seq": 1,  # stale: seq 1 was already consumed by this conversation
        },
        auto_embed=False,
    )


def test_a_corrupt_session_surfaces_as_an_error_row_and_does_not_vanish(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    """The bug: an integrity violation made the session SILENTLY DISAPPEAR from the
    picker. The store raises that ValueError specifically to "fail loudly rather
    than silently resolving the wrong cursor" -- and the catalog swallowed it.

    It must not vanish, and it must not brick the picker either: the healthy
    session listed beside it still loads.
    """
    scope = _cwd(run_id)
    healthy = catalog.create(scope, "test-model", "test-backend", system_prompt="sys")
    broken = catalog.create(scope, "test-model", "test-backend", system_prompt="sys")
    try:
        healthy.append_message(_msg("user", "i am fine"))
        broken.append_message(_msg("user", "i am about to be corrupted"))
        _forge_second_writer(client, broken)

        infos = {i.ref: i for i in catalog.list(scope)}

        assert str(broken.root_doc_id) in infos, "the corrupt session must NOT vanish"
        bad = infos[str(broken.root_doc_id)]
        assert bad.error is not None
        assert "second writer" in bad.error
        assert "⚠" in bad.display_title(), "the row says what happened"

        good = infos[str(healthy.root_doc_id)]
        assert good.error is None, "one bad session must not poison the good ones"
        assert good.message_count == 1
    finally:
        _cleanup(client, healthy.root_doc_id, broken.root_doc_id)


def test_opening_a_corrupt_session_still_raises_the_real_reason(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str
) -> None:
    """The error ROW is a listing affordance, not a repair. Loading is still
    Fail-Early: the corrupt tree is never opened and silently mis-folded."""
    broken = catalog.create(_cwd(run_id), "test-model", "test-backend", system_prompt="sys")
    try:
        broken.append_message(_msg("user", "hi"))
        _forge_second_writer(client, broken)

        with pytest.raises(ValueError, match="second writer"):
            catalog.load(str(broken.root_doc_id))
    finally:
        _cleanup(client, broken.root_doc_id)


def test_a_session_deleted_mid_listing_is_the_one_legitimate_skip(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str, monkeypatch
) -> None:
    """A 404 between the list page and the per-session load means the conversation
    is genuinely GONE. Omitting it is the TRUE answer, not a workaround -- so this
    is the only failure that may be skipped silently."""
    scope = _cwd(run_id)
    healthy = catalog.create(scope, "test-model", "test-backend", system_prompt="sys")
    doomed = catalog.create(scope, "test-model", "test-backend", system_prompt="sys")
    try:
        healthy.append_message(_msg("user", "i am fine"))
        doomed.append_message(_msg("user", "i am about to be deleted"))

        real_roots = catalog._list_conversation_roots

        def _delete_then_list():
            # The race, made deterministic: the root is in the page, then it's gone.
            candidates = real_roots()
            client.delete_document(doomed.root_doc_id)
            return candidates

        monkeypatch.setattr(catalog, "_list_conversation_roots", _delete_then_list)

        refs = {i.ref for i in catalog.list(scope)}
        assert str(doomed.root_doc_id) not in refs, "a deleted session is simply absent"
        assert str(healthy.root_doc_id) in refs
    finally:
        _cleanup(client, healthy.root_doc_id, doomed.root_doc_id)


def test_a_server_failure_mid_listing_raises_rather_than_reporting_a_partial_list(
    catalog: JmftsSessionCatalog, client: JmftsClient, run_id: str, monkeypatch
) -> None:
    """A 500/timeout is not "the session is absent", it is "I don't know". Dropping
    the row would hand back a SHORT list that looks complete -- the user would see a
    healthy picker missing a conversation. Fail-Early: say so."""
    scope = _cwd(run_id)
    session = catalog.create(scope, "test-model", "test-backend", system_prompt="sys")
    try:
        session.append_message(_msg("user", "hi"))

        def _boom(*a, **k):
            raise JmftsError(500, "upstream exploded", url="http://x", method="GET")

        monkeypatch.setattr(client, "get_subtree", _boom)

        with pytest.raises(JmftsError):
            catalog.list(scope)
    finally:
        _cleanup(client, session.root_doc_id)
