"""B3 — the Tier B `get_session_stats` verb (docs/RPC-TIER-B.md D-3).

Read-only, no D-1 guard: `get_session_stats` computes, entirely in the RPC
layer, the shape a host needs to decide WHETHER and WHEN to compact —
`estimate_context_tokens(session.messages)`, the model's `context_window`
and the resulting headroom, the session's EFFECTIVE `CompactionSettings`
(which is also how a host discovers auto-compaction is off, §1.1), the
newest compaction log entry (or `None`), and `get_usage()` for cost. None of
that is a re-shaping of `get_state` (D-3's ship condition) — `get_state`
returns none of it.

A new file per unit (docs/RPC-TIER-B.md §3 B1-B6 bullet: "a new file, so no
unit contends on a test file with any other").

Reference: docs/RPC-TIER-B.md §1, §1.1, D-3.
"""

from __future__ import annotations

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.rpc import commands
from tau_agent_core.rpc.commands import _last_compaction_state
from tau_agent_core.rpc.handler import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model

# TREE-BROWSER-AS-EDITOR.md §8/§11.3: ``append_compaction`` now requires the
# summary's provenance as keyword-only arguments with no defaults. These tests are
# about something else, so they name plausible values once here rather than at every
# call — the point of the required keywords is that a REAL caller cannot skip them.
_PROV = {
    "summarizer_model_id": "test-summarizer",
    "summary_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    "covered_entries": 1,
    "covered_tokens": 50,
    "agent_spec_id": None,
}


def _model(context_window: int = 8192) -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=context_window,
        max_tokens=256,
    )


def _session(**kwargs) -> AgentSession:
    kwargs.setdefault("model", _model())
    return AgentSession(session_log=InMemorySessionLog(), tools=[], **kwargs)


def _rpc_mode_session() -> AgentSession:
    """Mirrors `backends.py:885`'s RPC-mode construction — the ONE place
    `compaction_settings=CompactionSettings(enabled=False)` is passed —
    so a test against this fixture stands in for the real "over RPC" case
    the verb's `notes` claim (§1.1: auto-compaction is hard-disabled)."""
    return _session(compaction_settings=CompactionSettings(enabled=False))


def _user_message(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_message_with_usage(total_tokens: int, text: str = "hi") -> dict:
    """An assistant message the estimator will ANCHOR on.

    `estimate_context_tokens` anchors on the last assistant message that
    `_get_assistant_usage` accepts (compaction.py): role=="assistant", a
    `stop_reason` that is neither "aborted" nor "error", and a non-empty
    `usage` dict. `calculate_context_tokens` then prefers the provider's
    `total_tokens`. Both conditions are spelled out here so the anchor is
    established by the estimator's real rule, not by a fixture that happens
    to work."""
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "stop",
        "usage": {
            "input_tokens": total_tokens - 1,
            "output_tokens": 1,
            "total_tokens": total_tokens,
        },
    }


@pytest.fixture
def session() -> AgentSession:
    return _session()


@pytest.fixture
def handler(session: AgentSession) -> RPCHandler:
    return RPCHandler(session)


async def _call(handler: RPCHandler, method: str = "get_session_stats") -> dict:
    """Dispatch `method` and return the wire `result` dict (no `method` key
    stripped — callers that care assert on the fields they need)."""
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": method})
    return (await handler._output_queue.get())["result"]


# ── shape ────────────────────────────────────────────────────────────────


async def test_result_has_every_required_field(handler: RPCHandler) -> None:
    """Every key GET_SESSION_STATS_RESULT_SCHEMA['required'] names is
    present on a session that has done nothing at all — the empty-session
    floor, not just the populated case."""
    result = await _call(handler)
    schema = commands.COMMAND_TABLE["get_session_stats"].result_schema
    assert schema is not None
    for field in schema["required"]:
        assert field in result, f"missing required field {field!r}"


async def test_get_session_stats_takes_no_params(handler: RPCHandler) -> None:
    """params_schema is NO_PARAMS_SCHEMA (read-only, D-3) — an unexpected
    param is rejected exactly like any other verb's schema violation."""
    entry = commands.COMMAND_TABLE["get_session_stats"]
    assert entry.params_schema is commands.NO_PARAMS_SCHEMA


# ── it is genuinely more than get_state (D-3's ship condition) ────────────


async def test_fields_absent_from_get_state(handler: RPCHandler) -> None:
    """D-3: 'must be genuinely more than a re-shaping of get_state, or it
    does not ship.' Demonstrated, not asserted: every one of these keys is
    checked absent from get_state's own result on the identical session."""
    stats = await _call(handler)
    state = await _call(handler, "get_state")
    for field in ("context", "context_window", "context_headroom", "compaction_settings"):
        assert field in stats
        assert field not in state


# ── context / headroom ──────────────────────────────────────────────────


async def test_empty_session_has_zero_context_tokens_and_full_headroom(
    handler: RPCHandler,
) -> None:
    result = await _call(handler)
    assert result["context"]["tokens"] == 0
    assert result["context"]["last_usage_index"] is None
    assert result["context_window"] == 8192
    assert result["context_headroom"] == 8192


async def test_appending_messages_raises_context_tokens_and_lowers_headroom(
    session: AgentSession, handler: RPCHandler
) -> None:
    """MUTATION TARGET 1: delete the `- estimate.tokens` in
    `context_headroom`'s computation (leave it as plain `context_window`).
    This test goes red — `after["context_headroom"] == before["context_headroom"]`
    fails — because headroom would stop tracking context growth at all."""
    before = await _call(handler)
    session.session_log.append_message(_user_message("x" * 400))
    after = await _call(handler)
    assert after["context"]["tokens"] > before["context"]["tokens"]
    assert after["context_headroom"] < before["context_headroom"]
    assert after["context_headroom"] == after["context_window"] - after["context"]["tokens"]


async def test_context_window_tracks_the_active_model(handler: RPCHandler) -> None:
    small_session = _session(model=_model(context_window=111))
    small_handler = RPCHandler(small_session)
    result = await _call(small_handler)
    assert result["context_window"] == 111
    assert result["context_headroom"] == 111


async def test_context_headroom_is_negative_on_an_over_budget_session() -> None:
    """GET_SESSION_STATS_RESULT_SCHEMA's ONE Fail-Early claim about
    `context_headroom`: 'Can be negative: an honest over-budget number,
    never clamped to zero.' Nothing else in this file drives a session past
    its window, so without this test the claim is prose only.

    The over-budget is PROVIDER-REPORTED, not heuristic: the assistant's
    own `usage.total_tokens` (9000) already exceeds the model's 8192
    window, which is the case a real host hits — the provider says the
    conversation no longer fits, and the host has to see how badly.

    MUTATION TARGET 5: replace `context_window - estimate.tokens` with
    `max(0, context_window - estimate.tokens)`. This test goes red twice
    over (`< 0` and the exact-arithmetic assertion); every other test in
    this file stays green, because every other session in it is under
    budget and `max(0, ...)` is the identity there."""
    session = _session(model=_model(context_window=8192))
    session.session_log.append_message(_user_message("hello"))
    session.session_log.append_message(_assistant_message_with_usage(9000))
    session.session_log.append_message(_user_message("y" * 400))

    result = await _call(RPCHandler(session))

    assert result["context_window"] == 8192
    assert result["context"]["tokens"] == 9100
    assert result["context_headroom"] < 0
    assert result["context_headroom"] == -908
    assert result["context_headroom"] == result["context_window"] - result["context"]["tokens"]


async def test_context_projects_the_usage_anchor_and_the_trailing_estimate() -> None:
    """The `context` description's four-field contract: 'usage_tokens is the
    anchored provider-reported count up to the last assistant Usage,
    trailing_tokens the heuristic estimate for messages after it,
    last_usage_index that message's index' — and `tokens` their sum.

    Every other test in this file uses a session with NO assistant Usage,
    where usage_tokens==0 and trailing_tokens==tokens, so the three fields
    are indistinguishable from each other and from constants. Here they are
    pairwise distinct (500 / 100 / index 1), which is what makes a
    mis-wiring visible.

    MUTATION TARGET 6: in the handler's returned `context` dict, swap the
    `usage_tokens` and `trailing_tokens` values (500 and 100 change places).
    MUTATION TARGET 7: hardcode `"last_usage_index": None`. Both redden
    this test; the empty-session and no-usage tests cannot see either one."""
    session = _session(model=_model(context_window=8192))
    session.session_log.append_message(_user_message("hello"))
    session.session_log.append_message(_assistant_message_with_usage(500))
    session.session_log.append_message(_user_message("y" * 400))

    result = await _call(RPCHandler(session))

    context = result["context"]
    assert context["last_usage_index"] == 1, "the anchor is the assistant message, index 1 of 3"
    assert context["usage_tokens"] == 500, "the provider's own total_tokens, not an estimate"
    assert context["trailing_tokens"] == 100, "400 chars after the anchor, at ~4 chars/token"
    assert context["tokens"] == 600
    assert context["tokens"] == context["usage_tokens"] + context["trailing_tokens"]
    assert result["context_headroom"] == 8192 - 600


async def test_a_session_with_no_assistant_usage_is_estimated_end_to_end() -> None:
    """The `context` description's parenthetical: last_usage_index is 'null
    if no assistant Usage exists yet, in which case tokens==trailing_tokens
    and the whole list was heuristically estimated'.

    `test_empty_session_has_zero_context_tokens_and_full_headroom` covers
    this only degenerately — on an empty session tokens, trailing_tokens
    and usage_tokens are all 0, so tokens==trailing_tokens holds for any
    wiring at all. This session has real content and still no anchor, so
    the equality has to be earned: 100 == 100 while usage_tokens stays 0.

    MUTATION TARGET 8: `"usage_tokens": estimate.usage_tokens` →
    `estimate.usage_tokens or estimate.tokens` (the falsy-fallback shape
    this repo treats as an anti-pattern). It is invisible on the empty
    session (0 or 0) and on the anchored session (500 is truthy); only
    here does it turn the honest 0 into 100."""
    session = _session()
    session.session_log.append_message(_user_message("z" * 400))

    result = await _call(RPCHandler(session))

    context = result["context"]
    assert context["last_usage_index"] is None
    assert context["usage_tokens"] == 0, "no anchor means nothing is provider-reported"
    assert context["tokens"] == 100
    assert context["tokens"] == context["trailing_tokens"]


# ── compaction_settings — also §1.1's "how a host discovers auto-compaction
# is off" ────────────────────────────────────────────────────────────────


async def test_compaction_settings_reflects_the_bound_session_settings(
    handler: RPCHandler,
) -> None:
    result = await _call(handler)
    assert result["compaction_settings"] == {
        "enabled": True,
        "reserve_tokens": 16384,
        "keep_recent_tokens": 20000,
    }


async def test_rpc_mode_settings_reveal_auto_compaction_is_off() -> None:
    """§1.1: 'In RPC mode this is also how a host discovers that
    auto-compaction is off.' `_rpc_mode_session()` mirrors backends.py:885
    exactly — this is the literal shape a real RPC host would read.

    MUTATION TARGET 2: hardcode `"enabled": True` in the handler instead of
    reading `settings.enabled`. This test goes red (expects `False`); the
    default-session test above stays green (it also expects `True`, by
    coincidence) — which is exactly why this second, differently-configured
    fixture exists: only it can catch a hardcoded constant."""
    rpc_session = _rpc_mode_session()
    rpc_handler = RPCHandler(rpc_session)
    result = await _call(rpc_handler)
    assert result["compaction_settings"]["enabled"] is False


async def test_compaction_settings_tracks_set_auto_compaction_not_a_construction_constant() -> None:
    """Finding 10 of the Tier B review: this verb's notes asserted
    "compaction_settings.enabled is False on every session reachable through
    this verb today" — untrue the moment `set_auto_compaction`, shipped in
    the SAME tier, is called. §1.1's premise is about the value an RPC
    session is CONSTRUCTED with, not an invariant this verb may promise, and
    the two units wrote from that premise without reconciling.

    Drives both verbs through one handler, in the order a host would: read
    the starting value, flip it over the wire, read it back. The middle step
    is what the prose denied was possible.
    """
    rpc_handler = RPCHandler(_rpc_mode_session())
    assert (await _call(rpc_handler))["compaction_settings"]["enabled"] is False

    await rpc_handler._handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "set_auto_compaction", "params": {"enabled": True}}
    )
    setter = await rpc_handler._output_queue.get()
    assert setter["result"]["enabled"] is True

    assert (await _call(rpc_handler))["compaction_settings"]["enabled"] is True


async def test_custom_compaction_settings_are_not_the_defaults(handler: RPCHandler) -> None:
    custom = CompactionSettings(enabled=False, reserve_tokens=111, keep_recent_tokens=222)
    custom_session = _session(compaction_settings=custom)
    custom_handler = RPCHandler(custom_session)
    result = await _call(custom_handler)
    assert result["compaction_settings"] == {
        "enabled": False,
        "reserve_tokens": 111,
        "keep_recent_tokens": 222,
    }


# ── last_compaction ────────────────────────────────────────────────────


async def test_last_compaction_is_null_before_any_compaction(handler: RPCHandler) -> None:
    result = await _call(handler)
    assert result["last_compaction"] is None


async def test_last_compaction_reflects_the_newest_compaction_entry(
    session: AgentSession, handler: RPCHandler
) -> None:
    first_id = session.session_log.append_message(_user_message("hello"))
    session.session_log.append_compaction(
        summary="first summary", first_kept_id=first_id, tokens_before=100, **_PROV
    )
    second_id = session.session_log.append_message(_user_message("later"))
    session.session_log.append_compaction(
        summary="second summary", first_kept_id=second_id, tokens_before=200, **_PROV
    )

    result = await _call(handler)

    assert result["last_compaction"] is not None
    assert result["last_compaction"]["summary"] == "second summary"
    assert result["last_compaction"]["first_kept_id"] == second_id
    assert result["last_compaction"]["tokens_before"] == 200


async def test_last_compaction_carries_the_entry_id_and_timestamp(
    session: AgentSession, handler: RPCHandler
) -> None:
    """The `last_compaction` description names five fields — '{id, timestamp,
    summary, first_kept_id, tokens_before}'. The tests above assert three of
    them; `id` and `timestamp` are named on the wire contract and asserted
    nowhere, so dropping either would ship silently.

    `id` is checked against the value `append_compaction` RETURNED (the
    entry's real address, which a host uses to find the compaction in the
    log), not merely for presence, and `timestamp` against the entry the
    log actually wrote — so a fabricated id or a `datetime.now()` invented
    at read time fails too.

    MUTATION TARGET 9: delete the `"id": entry.get("id"),` line from
    `_last_compaction_state`. MUTATION TARGET 10: replace
    `entry.get("timestamp")` with `None`. Each reddens this test alone."""
    first_id = session.session_log.append_message(_user_message("hello"))
    compaction_id = session.session_log.append_compaction(
        summary="only summary", first_kept_id=first_id, tokens_before=100, **_PROV
    )
    written = [e for e in session.session_log.entries() if e.get("type") == "compaction"]
    assert len(written) == 1

    result = await _call(handler)

    assert result["last_compaction"]["id"] == compaction_id
    assert result["last_compaction"]["timestamp"] == written[0]["timestamp"]


def test_last_compaction_state_helper_directly() -> None:
    """`_last_compaction_state` in isolation (the handler-level tests above
    exercise it through the wire; this pins its own contract).

    MUTATION TARGET 3: change `reversed(session.session_log.entries())` to
    a forward scan (drop `reversed`). This test goes red — it would then
    return the FIRST compaction entry ("old"), not the newest ("new")."""
    session = _session()
    first_id = session.session_log.append_message(_user_message("a"))
    session.session_log.append_compaction(
        summary="old", first_kept_id=first_id, tokens_before=1, **_PROV
    )
    second_id = session.session_log.append_message(_user_message("b"))
    session.session_log.append_compaction(
        summary="new", first_kept_id=second_id, tokens_before=2, **_PROV
    )

    state = _last_compaction_state(session)

    assert state is not None
    assert state["summary"] == "new"
    assert state["first_kept_id"] == second_id


def test_last_compaction_state_helper_on_an_empty_log() -> None:
    assert _last_compaction_state(_session()) is None


# ── usage ────────────────────────────────────────────────────────────────


async def test_usage_is_null_before_any_completion(handler: RPCHandler) -> None:
    result = await _call(handler)
    assert result["usage"] is None


async def test_usage_reflects_get_usage(session: AgentSession, handler: RPCHandler) -> None:
    """Same precedent test_agent_session_runtime.py:265 uses to seed usage
    without running a real completion: set the private ledger directly."""
    session._last_usage = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    result = await _call(handler)
    assert result["usage"] == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}


# ── D-1: read-only, no turn_safety_guard ───────────────────────────────


async def test_get_session_stats_succeeds_while_the_turn_lock_is_held(
    session: AgentSession, handler: RPCHandler
) -> None:
    """D-1: 'get_session_stats and get_last_assistant_text are reads and
    take no guard.' Proven, not just claimed by the notes string: hold
    turn_lock (as a real in-flight turn would) and confirm the call still
    answers instead of raising TURN_STILL_RUNNING or hanging.

    MUTATION TARGET 4: wrap the handler body in
    `async with turn_safety_guard(session, timeout=0.05): pass`. This test
    goes red on `_call`'s `["result"]` lookup (`KeyError: 'result'`) — the
    lock is already held by this test (standing in for an in-flight turn),
    so the guard's bounded wait raises `RPCError(TURN_STILL_RUNNING)`,
    `_handle_request` turns that into an `error` envelope instead of a
    `result` one, and the request that should have answered instantly
    comes back a structured refusal instead. Verified directly (not just
    asserted here): applying this exact mutation and running this one test
    fails with that KeyError; reverting the mutation turns it green again."""
    await session.turn_lock.acquire()
    try:
        result = await _call(handler)
    finally:
        session.turn_lock.release()
    assert result["context_window"] == 8192
