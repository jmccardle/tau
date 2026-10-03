"""B5 — RPC Tier B verb `set_session_name` (+ the `get_session_name` read).

Reference: docs/RPC-TIER-B.md B5, D-1, D-7.

`set_session_name` is MUTATING (D-1): it takes `commands.turn_safety_guard`,
refuses an unpersisted session (D-7 rule 1), and reuses
`extension_types.apply_session_name`, the same body `ExtensionAPI.set_session_name`
calls. That body appends `session_info` at the session's cursor, on any store.
`get_session_name` is read-only: no turn guard, no cursor in its result.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core import extension_types
from tau_agent_core.rpc import dialect
from tau_agent_core.rpc.handler import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog, session_name
from tau_llm.types import Model


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


class _DurableLog(InMemorySessionLog):
    """`InMemorySessionLog` that declares where it lives, as the file store does.

    `path=None` is the same log with the durability taken away: what
    `create_ephemeral` produces.
    """

    def __init__(self, path: Path | None = Path("/tmp/does-not-need-to-exist.jsonl")) -> None:
        super().__init__()
        self.path = path


@pytest.fixture
def real_session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])


@pytest.fixture
def named_session(real_session: AgentSession) -> AgentSession:
    """The success-path fixture: a session on a log that declares a durable location."""
    real_session.session_log = _DurableLog()
    return real_session


@pytest.fixture
def handler(named_session: AgentSession) -> RPCHandler:
    return RPCHandler(named_session)


def _name_on_log(session: AgentSession) -> str | None:
    return session_name(session.session_log.entries())


async def _dispatch(handler: RPCHandler, method: str, params: dict) -> dict:
    """Round-trip one request through `_handle_request` and return the
    single queued response dict (result or error)."""
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return await asyncio.wait_for(handler._output_queue.get(), timeout=5.0)


# ── set_session_name: the happy path ────────────────────────────────────


async def test_set_session_name_persists_and_returns_name_and_cursor(handler: RPCHandler) -> None:
    response = await _dispatch(handler, "set_session_name", {"name": "My Session"})
    assert "error" not in response
    assert response["result"]["name"] == "My Session"
    cursor = response["result"]["leaf"]
    assert cursor == handler.session.cursor.leaf
    (entry,) = [e for e in handler.session.session_log.entries() if e["id"] == cursor]
    assert entry["type"] == "session_info"
    assert _name_on_log(handler.session) == "My Session"


async def test_set_session_name_cursor_advances_on_a_second_call(handler: RPCHandler) -> None:
    """E5: the cursor returned is the session cursor's leaf after THIS write."""
    first = await _dispatch(handler, "set_session_name", {"name": "one"})
    second = await _dispatch(handler, "set_session_name", {"name": "two"})
    assert first["result"]["leaf"] != second["result"]["leaf"]
    assert second["result"]["leaf"] == handler.session.cursor.leaf


# ── set_session_name: refusals ──────────────────────────────────────────


async def test_set_session_name_empty_name_is_invalid_params(handler: RPCHandler) -> None:
    """validate_params has no `minLength` (the schema's own note) — the
    empty-string refusal comes from apply_session_name's ValueError, remapped
    to INVALID_PARAMS the same way switch_session remaps a bad id."""
    response = await _dispatch(handler, "set_session_name", {"name": ""})
    assert response["error"]["code"] == dialect.INVALID_PARAMS
    assert handler.session.session_log.entries() == []


async def test_set_session_name_on_an_in_memory_log_is_session_not_persisted(
    real_session: AgentSession,
) -> None:
    """`InMemorySessionLog` declares no durable location: `SESSION_NOT_PERSISTED`,
    a code a host can tell from a τ crash without matching English."""
    handler = RPCHandler(real_session)
    response = await _dispatch(handler, "set_session_name", {"name": "x"})
    assert response["error"]["code"] == dialect.SESSION_NOT_PERSISTED
    assert "declares no durable location" in response["error"]["message"]


async def test_set_session_name_refuses_an_unpersisted_session(
    real_session: AgentSession,
) -> None:
    """Blocker 2: a log that declares `path` but whose `path is None` (what
    `create_ephemeral` produces). A host must get an error, NOT `{name, cursor}`:
    the cursor would promise an entry no later replay can see.
    """
    real_session.session_log = _DurableLog(path=None)
    handler = RPCHandler(real_session)

    response = await _dispatch(handler, "set_session_name", {"name": "gone-on-exit"})

    assert "result" not in response
    assert response["error"]["code"] == dialect.SESSION_NOT_PERSISTED
    assert response["error"]["data"]["method"] == "set_session_name"
    assert "unpersisted" in response["error"]["message"]
    assert real_session.session_log.entries() == []


async def test_set_session_name_respects_turn_safety_guard(handler: RPCHandler) -> None:
    """D-1: a turn holding `turn_lock` blocks the write — TURN_STILL_RUNNING,
    not a silent wait or a bypass — and nothing is appended."""
    await handler.session.turn_lock.acquire()
    try:
        response = await _dispatch(handler, "set_session_name", {"name": "blocked"})
    finally:
        handler.session.turn_lock.release()
    assert response["error"]["code"] == dialect.TURN_STILL_RUNNING
    assert handler.session.session_log.entries() == []


# ── get_session_name ─────────────────────────────────────────────────────


async def test_get_session_name_returns_null_when_never_set(handler: RPCHandler) -> None:
    response = await _dispatch(handler, "get_session_name", {})
    assert "error" not in response
    assert response["result"]["name"] is None


async def test_get_session_name_reflects_a_prior_set(handler: RPCHandler) -> None:
    await _dispatch(handler, "set_session_name", {"name": "reflected"})
    response = await _dispatch(handler, "get_session_name", {})
    assert response["result"]["name"] == "reflected"


async def test_get_session_name_result_carries_no_cursor(handler: RPCHandler) -> None:
    """docs/RPC-TIER-B.md B5: 'the read does not' carry a cursor, unlike the
    write."""
    response = await _dispatch(handler, "get_session_name", {})
    assert "leaf" not in response["result"]


async def test_get_session_name_reads_an_in_memory_log_too(
    real_session: AgentSession,
) -> None:
    """The read appends nothing, so durability is not its question (D-7 rule 2):
    it answers on an `InMemorySessionLog` like on any other store."""
    handler = RPCHandler(real_session)
    assert (await _dispatch(handler, "get_session_name", {}))["result"]["name"] is None

    await real_session.set_session_name("unpersisted but named")

    response = await _dispatch(handler, "get_session_name", {})
    assert response["result"]["name"] == "unpersisted but named"


async def test_get_session_name_takes_no_turn_guard(handler: RPCHandler) -> None:
    """Read-only: an in-flight turn does NOT block a read, unlike the write."""
    await handler.session.turn_lock.acquire()
    try:
        response = await _dispatch(handler, "get_session_name", {})
    finally:
        handler.session.turn_lock.release()
    assert "error" not in response
    assert response["result"]["name"] is None


# ── ONE shared definition (docs/RPC-TIER-B.md B5: "do not copy-paste it") ──


async def test_rpc_verb_and_extension_api_share_one_apply_function(
    handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof, not assertion-by-reading-the-diff: monkeypatching
    `extension_types.apply_session_name` changes BOTH the RPC verb's
    behavior and `ExtensionAPI.set_session_name`'s — because both call the
    exact same function object, not two copies that happen to agree today.
    """
    calls: list[tuple[object, str]] = []

    async def _fake_apply(session: object, name: str) -> None:
        calls.append((session, name))

    monkeypatch.setattr(extension_types, "apply_session_name", _fake_apply)

    # Route 1: the RPC verb.
    await _dispatch(handler, "set_session_name", {"name": "via-rpc"})
    # Route 2: the extension API, on the same underlying session.
    api = ExtensionAPI(session=handler.session)
    await api.set_session_name("via-extension-api")

    assert [name for _session, name in calls] == ["via-rpc", "via-extension-api"]
    assert handler.session.session_log.entries() == []


async def test_rpc_verb_and_extension_api_share_one_read_function(
    handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extension_types, "read_session_name", lambda session: "patched")

    response = await _dispatch(handler, "get_session_name", {})
    assert response["result"]["name"] == "patched"

    api = ExtensionAPI(session=handler.session)
    assert api.get_session_name() == "patched"
