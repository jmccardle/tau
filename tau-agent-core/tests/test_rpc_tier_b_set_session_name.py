"""B5 — RPC Tier B verb `set_session_name` (+ the `get_session_name` read).

Reference: docs/RPC-TIER-B.md B5, D-1, §1.1.

`set_session_name` is MUTATING (D-1): it takes `commands.turn_safety_guard`
before writing, and its handler reuses
`tau_agent_core.extension_types.apply_session_name` — the SAME body
`ExtensionAPI.set_session_name` calls — rather than a second, hand-rolled
copy of the §1.1 "raise if the log has no durable slot" guard.
`get_session_name` is read-only: no turn guard, no cursor in its result.

Own file (docs/RPC-TIER-B.md §3 "a new file, so no unit contends on it") —
mirrors test_rpc_tier_b_scaffolding.py's self-contained-fake convention
(`_FakeNamedLog` below) rather than importing across test files or across
packages (this file lives in tau-agent-core, which must not depend on
tau-coding-agent's `session_store.Session` one layer up).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core import extension_types
from tau_agent_core.rpc import commands, dialect
from tau_agent_core.rpc.handler import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog
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


class _FakeNamedLog:
    """A minimal stand-in for the file-backed `tau_coding_agent
    .session_store.Session` log, carrying exactly the surfaces
    `apply_session_name`/`read_session_name` need: `append_session_info`
    (§1.1's appender) and the derived `.name`/`.cursor` reads
    (session_store.py:264-271, 197) — the real `Session` computes both from
    its entry list; this fake tracks the same two facts directly rather than
    replaying that scan.

    Plus `path`, which is what Blocker 2's `require_durable_session` reads:
    the real `Session` declares where it durably lives, and a fake standing
    in for the SUCCESS path has to answer that question the same way (a
    non-`None` path). `path=None` is the same object with the durability
    taken away — the unpersisted session every RPC run used to start on.
    """

    def __init__(self, path: Path | None = Path("/tmp/does-not-need-to-exist.jsonl")) -> None:
        self.path = path
        self._names: list[str] = []
        self._cursor: str | None = None
        self._next_id = 0

    def append_session_info(self, name: str) -> str:
        self._next_id += 1
        entry_id = f"entry-{self._next_id}"
        self._names.append(name)
        self._cursor = entry_id
        return entry_id

    @property
    def name(self) -> str | None:
        return self._names[-1] if self._names else None

    @property
    def cursor(self) -> str | None:
        return self._cursor


@pytest.fixture
def real_session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])


@pytest.fixture
def named_session(real_session: AgentSession) -> AgentSession:
    """A session bound to a log that DOES support durable naming — the
    success-path fixture. `real_session` (bare `InMemorySessionLog`) is the
    failure-path fixture instead: it deliberately has neither appender
    (session_log.py:38-48)."""
    real_session.session_log = _FakeNamedLog()  # type: ignore[assignment]
    return real_session


@pytest.fixture
def handler(named_session: AgentSession) -> RPCHandler:
    return RPCHandler(named_session)


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
    assert response["result"]["cursor"] == "entry-1"
    # Actually landed on the log, not merely echoed back unpersisted.
    assert handler.session.session_log.name == "My Session"  # type: ignore[attr-defined]


async def test_set_session_name_cursor_advances_on_a_second_call(handler: RPCHandler) -> None:
    """E5: the cursor returned is the log's own tip after THIS write, not a
    constant — a second rename produces a different cursor."""
    first = await _dispatch(handler, "set_session_name", {"name": "one"})
    second = await _dispatch(handler, "set_session_name", {"name": "two"})
    assert first["result"]["cursor"] != second["result"]["cursor"]
    assert second["result"]["cursor"] == "entry-2"


# ── set_session_name: refusals ──────────────────────────────────────────


async def test_set_session_name_empty_name_is_invalid_params(handler: RPCHandler) -> None:
    """validate_params has no `minLength` (the schema's own note) — the
    empty-string refusal comes from apply_session_name's ValueError, remapped
    to INVALID_PARAMS the same way switch_session remaps a bad id."""
    response = await _dispatch(handler, "set_session_name", {"name": ""})
    assert response["error"]["code"] == dialect.INVALID_PARAMS
    # And nothing was appended.
    assert handler.session.session_log.name is None  # type: ignore[attr-defined]


async def test_set_session_name_on_an_in_memory_log_is_session_not_persisted(
    real_session: AgentSession,
) -> None:
    """`InMemorySessionLog` declares no durable location AND has no
    `append_session_info` — nowhere to land the name, by either measure.
    Blocker 2's durability check runs FIRST, so it is the one that speaks,
    and since round-3 finding 4 it speaks as `SESSION_NOT_PERSISTED` rather
    than `INTERNAL_ERROR`: a host must be able to tell a considered refusal
    from a τ crash without matching English. The appender-presence case —
    which still surfaces as INTERNAL_ERROR, because a store missing the
    method entirely is wired wrong rather than merely unpersisted — is
    pinned separately in test_rpc_tier_b_scaffolding.py, on a log that HAS
    a location."""
    handler = RPCHandler(real_session)
    response = await _dispatch(handler, "set_session_name", {"name": "x"})
    assert response["error"]["code"] == dialect.SESSION_NOT_PERSISTED
    assert "declares no durable location" in response["error"]["message"]


async def test_set_session_name_refuses_an_unpersisted_session(
    real_session: AgentSession,
) -> None:
    """Blocker 2, the defect by name: a log with a perfectly good
    `append_session_info` whose writes reach no storage (`path is None` —
    what `create_ephemeral` produces, which is what every RPC run started
    on). §1.1's appender check passes here, so only a durability check can
    fail this test.

    A host must get an error, NOT `{name, cursor}`: the cursor would be a
    durable-write promise for an entry no later replay can see.
    """
    real_session.session_log = _FakeNamedLog(path=None)  # type: ignore[assignment]
    handler = RPCHandler(real_session)

    response = await _dispatch(handler, "set_session_name", {"name": "gone-on-exit"})

    assert "result" not in response
    assert response["error"]["code"] == dialect.SESSION_NOT_PERSISTED
    assert response["error"]["data"]["method"] == "set_session_name"
    assert "unpersisted" in response["error"]["message"]
    # Refused before the write: the name was never applied either.
    assert real_session.session_log.name is None  # type: ignore[attr-defined]


async def test_set_session_name_respects_turn_safety_guard(handler: RPCHandler) -> None:
    """D-1: a turn holding `turn_lock` blocks the write — TURN_STILL_RUNNING,
    not a silent wait or a bypass — and nothing is appended."""
    await handler.session.turn_lock.acquire()
    try:
        response = await _dispatch(handler, "set_session_name", {"name": "blocked"})
    finally:
        handler.session.turn_lock.release()
    assert response["error"]["code"] == dialect.TURN_STILL_RUNNING
    assert handler.session.session_log.name is None  # type: ignore[attr-defined]


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
    assert "cursor" not in response["result"]


async def test_get_session_name_on_an_in_memory_log_is_internal_error(
    real_session: AgentSession,
) -> None:
    """§1.1's raise on the read side: `InMemorySessionLog` has no `.name`."""
    handler = RPCHandler(real_session)
    response = await _dispatch(handler, "get_session_name", {})
    assert response["error"]["code"] == dialect.INTERNAL_ERROR
    assert "durable name" in response["error"]["message"]


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

    def _fake_apply(session: object, name: str) -> None:
        calls.append((session, name))

    monkeypatch.setattr(extension_types, "apply_session_name", _fake_apply)

    # Route 1: the RPC verb.
    await _dispatch(handler, "set_session_name", {"name": "via-rpc"})
    # Route 2: the extension API, on the same underlying session.
    api = ExtensionAPI(session=handler.session)
    api.set_session_name("via-extension-api")

    assert [name for _session, name in calls] == ["via-rpc", "via-extension-api"]
    # And, since the fake never touched the log, nothing was actually appended.
    assert handler.session.session_log.name is None  # type: ignore[attr-defined]


async def test_rpc_verb_and_extension_api_share_one_read_function(
    handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extension_types, "read_session_name", lambda session: "patched")

    response = await _dispatch(handler, "get_session_name", {})
    assert response["result"]["name"] == "patched"

    api = ExtensionAPI(session=handler.session)
    assert api.get_session_name() == "patched"
