"""RPC Tier B — `set_model` (B1, docs/RPC-TIER-B.md §3 "B1 | set_model").

D-2: switches `AgentSession`'s active model (`AgentSession.set_model`) AND
persists the switch as a `model_change` log entry — something the bare
session method deliberately does NOT do. The append happens in the RPC verb
itself (D-2's scope boundary), so a TUI switch through
`AgentSession.set_model` directly still does not persist; see the verb's
own `notes` on `commands.COMMAND_TABLE["set_model"]`.

D-1: mutating, so every success/failure path here also proves the verb
actually took `turn_safety_guard` — not merely that `AgentSession.set_model`
works (that is `test_agent_session.py`'s job).

§1.1: `require_log_appender` on `append_model_change` — a log with nowhere
durable to put the entry raises rather than silently skipping the persist.

A new, session-log-less `AgentSession` (real, not a `MagicMock`) rather than
the `session` fixture `test_rpc.py` uses: this verb reads `turn_lock` (a
real `asyncio.Lock`, D-1) and calls the real `set_model`/resolver machinery,
neither of which a `MagicMock` reproduces faithfully — the same choice
`test_rpc_tier_b_scaffolding.py` and `test_rpc.py`'s own `real_session`
fixture already make.

Reference: docs/RPC-TIER-B.md D-1, D-2, §1.1, §6 "Every test you write must
be able to fail."
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.rpc import RPCHandler, commands, dialect
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model


def _model(name: str = "m1", provider: str = "openai") -> Model:
    return Model(
        id=name,
        provider=provider,
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name=name,
        context_window=8192,
        max_tokens=256,
    )


#: The two config names `_resolver` below knows, so `test_set_model_*`
#: switches between models with DIFFERENT providers — a same-provider switch
#: would not catch a handler that forgot to read `model["provider"]` off the
#: resolver's actual return value and instead echoed the OLD model's.
_MODELS: dict[str, Model] = {
    "m1": _model("m1", "openai"),
    "m2": _model("m2", "anthropic"),
}


def _resolver(name: str) -> Model:
    """Mirrors `backends.make_model_resolver`'s Fail-Early contract (a
    ``KeyError`` naming the known models) — the RPC verb converts exactly
    this shape to ``INVALID_PARAMS``."""
    try:
        return _MODELS[name]
    except KeyError:
        raise KeyError(
            f"unknown model {name!r}; configured models: {', '.join(sorted(_MODELS))}"
        ) from None


class _LogWithModelChange:
    """A minimal stand-in session log carrying §1.1's ``append_model_change``
    — mirrors ``test_rpc_tier_b_scaffolding.py``'s ``_LogWithAppenders`` (a
    small test-local fake; no precedent in this suite for a cross-test-file
    import). Tracks its own ``cursor`` the way the real file-backed
    ``Session`` does: the leaf id moves to whatever was just appended, so
    E5's "return the resulting cursor" has something real to observe.

    ``path`` is not decoration: Blocker 2's ``require_durable_session`` asks
    the log to declare WHERE it durably lives, and a fake that wants the
    success path must answer the same question the real ``Session`` does (a
    non-``None`` ``path``). A fake that stayed silent would be asserting the
    success path on a session the verb is now required to refuse.

    Also implements ``append_custom_entry``: ``AgentSession.set_model``
    itself calls ``_record_agent_spec`` (W2, agent_session.py:836), which
    appends a ``customEntry`` BEFORE this verb ever touches the log — not
    an appender §1.1 is about (it is on the ``SessionLog`` Protocol
    proper, unlike ``append_model_change``), but this fake still needs it
    to avoid an ``AttributeError`` that has nothing to do with what this
    test file is checking."""

    def __init__(self, path: Path | None = Path("/tmp/does-not-need-to-exist.jsonl")) -> None:
        self.path = path
        self.model_changes: list[tuple[str, str]] = []
        self.custom_entries: list[tuple[str, dict]] = []
        self.cursor: str | None = None

    def append_custom_entry(self, custom_type: str, data: dict) -> str:
        self.custom_entries.append((custom_type, data))
        self.cursor = f"custom-entry-{len(self.custom_entries)}"
        return self.cursor

    def append_model_change(self, model: str, backend: str) -> str:
        self.model_changes.append((model, backend))
        self.cursor = f"model-change-{len(self.model_changes)}"
        return self.cursor


@pytest.fixture
def session() -> AgentSession:
    s = AgentSession(session_log=InMemorySessionLog(), model=_MODELS["m1"], tools=[])
    s.set_model_resolver(_resolver)
    return s


@pytest.fixture
def handler(session: AgentSession) -> RPCHandler:
    return RPCHandler(session)


async def _drain(handler: RPCHandler) -> list[dict]:
    out = []
    while not handler._output_queue.empty():
        out.append(await handler._output_queue.get())
    return out


async def _set_model(handler: RPCHandler, name: str, *, msg_id: int = 1) -> dict:
    await handler._handle_request(
        {"jsonrpc": "2.0", "id": msg_id, "method": "set_model", "params": {"name": name}}
    )
    (response,) = await _drain(handler)
    return response


# ── table wiring ─────────────────────────────────────────────────────────


def test_set_model_is_a_tier_b_verb_with_schemas():
    entry = commands.COMMAND_TABLE["set_model"]
    assert entry.tier == "B"
    assert entry.handler is not None
    assert entry.declined_because is None
    assert entry.result_schema is not None
    assert entry.params_schema["required"] == ["name"]
    assert entry.params_schema["additionalProperties"] is False


# ── success path (D-2) ──────────────────────────────────────────────────


async def test_set_model_switches_persists_and_returns_the_cursor(handler, session):
    session.session_log = _LogWithModelChange()

    response = await _set_model(handler, "m2")

    assert "error" not in response
    assert response["result"]["model"] == {
        "id": "m2",
        "provider": "anthropic",
        "context_window": 8192,
    }
    assert response["result"]["cursor"] == "model-change-1"
    assert response["result"]["method"] == "set_model"
    # Persisted with the CALLER-supplied name (the config key, matching
    # headless.py's own append_model_change(model_name, backend_name) —
    # not model.id, which a config key may alias) and the newly resolved
    # model's provider (not the old model's).
    assert session.session_log.model_changes == [("m2", "anthropic")]
    # The switch actually took effect, not merely reported.
    assert session.get_model()["id"] == "m2"


async def test_set_model_is_idempotent_against_the_current_model(handler, session):
    """Switching to the model already active still appends a fresh entry —
    D-2 does not special-case a no-op switch; a host that asked to persist
    a record of "still on m1" gets one."""
    session.session_log = _LogWithModelChange()

    response = await _set_model(handler, "m1")

    assert response["result"]["model"]["id"] == "m1"
    assert session.session_log.model_changes == [("m1", "openai")]


# ── §1.1: no durable place to put the entry ────────────────────────────


async def test_set_model_raises_when_the_log_has_no_appender(handler, session):
    """A log that declares a durable location but has no
    ``append_model_change`` — §1.1's own case, isolated from Blocker 2's
    (below) so each guard is pinned by a test only IT can fail. Fail-Early:
    raise, never skip the persist step. An unclassified exception from a
    handler becomes INTERNAL_ERROR (C2), the same bucket _require_runtime's
    construction-gap RuntimeError lands in."""

    class _DurableButNoAppender:
        path = Path("/tmp/does-not-need-to-exist.jsonl")
        cursor: str | None = None

        def append_custom_entry(self, custom_type: str, data: dict) -> str:
            return "custom-entry-1"

    session.session_log = _DurableButNoAppender()

    response = await _set_model(handler, "m2")

    assert response["error"]["code"] == dialect.INTERNAL_ERROR
    assert "append_model_change" in response["error"]["message"]
    # Blocker 2: both persistence preconditions run BEFORE the switch, so a
    # refusal is total — this verb never reports "maybe switched, definitely
    # not persisted" (which is what it DID report until the guards moved).
    assert session.get_model()["id"] == "m1"


# ── Blocker 2: a durable-looking append that would land nowhere ────────


async def test_set_model_refuses_an_unpersisted_session(handler, session):
    """The exact defect Blocker 2 names: a real, fully-appender-equipped
    session whose writes go nowhere (the file store's ``path is None``,
    which every RPC run started on until this fix). ``require_log_appender``
    passes here — the appender IS present — so only a durability check can
    fail this test, and a host must not get a cursor back."""
    session.session_log = _LogWithModelChange(path=None)

    response = await _set_model(handler, "m2")

    assert "result" not in response
    assert response["error"]["code"] == dialect.SESSION_NOT_PERSISTED
    assert response["error"]["data"]["method"] == "set_model"
    assert "unpersisted" in response["error"]["message"]
    # Refused before anything was touched: no switch, nothing appended.
    assert session.get_model()["id"] == "m1"
    assert session.session_log.model_changes == []


async def test_set_model_refuses_a_log_that_declares_no_durable_location(handler, session):
    """``InMemorySessionLog`` — the SDK default — declares neither ``path``
    nor ``root_doc_id``. Unknown durability is refused, never assumed
    (``_DURABLE_LOCATION_ATTRS``' own note: unknown means no, not yes)."""
    response = await _set_model(handler, "m2")

    assert response["error"]["code"] == dialect.SESSION_NOT_PERSISTED
    assert "declares no durable location" in response["error"]["message"]
    assert session.get_model()["id"] == "m1"


# ── unknown model name: a caller error, not a crash ─────────────────────


async def test_set_model_unknown_name_is_invalid_params(handler, session):
    session.session_log = _LogWithModelChange()

    response = await _set_model(handler, "no-such-model")

    assert "result" not in response
    assert response["error"]["code"] == dialect.INVALID_PARAMS
    # The resolver's own sentence, verbatim — no double-quoting (finding 10
    # of the Tier B review: `str(KeyError)` is `repr(args[0])`, so this used
    # to reach the wire as `"unknown model 'no-such-model'; ..."`, quotes
    # included, inside a JSON string that quotes it again). Pinned as an
    # equality on the whole message rather than a substring, because a
    # substring check is exactly what let the quotes through.
    assert (
        response["error"]["message"] == "unknown model 'no-such-model'; configured models: m1, m2"
    )
    assert response["error"]["data"]["name"] == "no-such-model"
    # Refused before anything was touched: no switch, nothing appended.
    assert session.get_model()["id"] == "m1"
    assert session.session_log.model_changes == []


def test_resolver_error_message_unwraps_a_key_error_without_rewording_it() -> None:
    """`_resolver_error_message`, the whole of finding 10's fix, tested at
    its three inputs.

    The resolver is the component that knows which model names exist, so
    this function's job is to render its message, never to paraphrase it:
    a single-argument `KeyError` is unwrapped (that argument IS the
    message), a `ValueError` — `AgentSession.set_model`'s other documented
    "no such name" shape — passes through untouched, and a `KeyError`
    carrying anything else has no single message to unwrap, so its own
    `__str__` (the args tuple's repr) is the whole of what the raiser said.
    """
    assert (
        commands._resolver_error_message(KeyError("unknown model 'x'; configured models: a, b"))
        == "unknown model 'x'; configured models: a, b"
    )
    assert commands._resolver_error_message(ValueError("model 'x' is not usable")) == (
        "model 'x' is not usable"
    )
    two_args = KeyError("x", "y")
    assert commands._resolver_error_message(two_args) == str(two_args)


# ── schema validation (INVALID_PARAMS before the handler ever runs) ─────


async def test_set_model_missing_name_is_invalid_params(handler, session):
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "set_model", "params": {}})
    (response,) = await _drain(handler)
    assert response["error"]["code"] == dialect.INVALID_PARAMS
    assert "name" in response["error"]["message"]


async def test_set_model_unexpected_param_is_invalid_params(handler, session):
    await handler._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "set_model",
            "params": {"name": "m2", "bogus": True},
        }
    )
    (response,) = await _drain(handler)
    assert response["error"]["code"] == dialect.INVALID_PARAMS


# ── D-1: the turn safety guard ───────────────────────────────────────────


async def test_set_model_refuses_while_a_turn_is_in_flight(monkeypatch, handler, session):
    """A turn holds `turn_lock` (a real `asyncio.Lock`, standing in for a
    turn genuinely in flight) — the verb must refuse with
    `TURN_STILL_RUNNING` rather than racing it, and must not touch the
    model. Patches `commands.turn_safety_guard`'s bound timeout down from
    the real 5s `DEFAULT_SWAP_TIMEOUT_S` to keep this test fast — same
    judgment `test_agent_session_runtime.py`'s own swap-timeout test
    docstring makes ("a real 5s ... would be a legitimate but slow way to
    pin the same behaviour"); `_handle_set_model` resolves
    `turn_safety_guard` as a module GLOBAL at call time, so patching
    `commands.turn_safety_guard` reaches it without touching the handler's
    own source. The guard's OWN timeout/release correctness is
    `test_rpc_tier_b_scaffolding.py`'s job, not re-proven here — this test
    is only that `set_model` actually calls it."""
    real_guard = commands.turn_safety_guard

    def _fast_guard(sess, *, timeout: float = 0.05):
        return real_guard(sess, timeout=timeout)

    monkeypatch.setattr(commands, "turn_safety_guard", _fast_guard)

    await session.turn_lock.acquire()
    try:
        response = await _set_model(handler, "m2")
    finally:
        session.turn_lock.release()

    assert "result" not in response
    assert response["error"]["code"] == dialect.TURN_STILL_RUNNING
    # Refused before the guard body ever ran: no switch attempted.
    assert session.get_model()["id"] == "m1"


async def test_set_model_releases_the_lock_after_success(handler, session):
    session.session_log = _LogWithModelChange()
    assert not session.turn_lock.locked()

    await _set_model(handler, "m2")

    assert not session.turn_lock.locked()


async def test_set_model_releases_the_lock_even_when_the_name_is_unknown(handler, session):
    """The guard's release-on-exception path (`turn_safety_guard`'s
    ``finally``) must run even though `_handle_set_model` raises `RPCError`
    from INSIDE the `async with` body — a second `set_model` call right
    after a rejected one must not itself see `TURN_STILL_RUNNING`."""
    session.session_log = _LogWithModelChange()

    await _set_model(handler, "no-such-model")
    assert not session.turn_lock.locked()

    response = await _set_model(handler, "m2", msg_id=2)
    assert response["result"]["model"]["id"] == "m2"
