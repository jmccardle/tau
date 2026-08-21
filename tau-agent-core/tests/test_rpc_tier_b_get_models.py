"""RPC Tier B — `get_models` (finding 7 of the Tier B review).

`set_model` takes a config model NAME and, until this verb, nothing on the
command table enumerated them: `get_state` and `get_session_stats` publish
only the ACTIVE model's `{id, provider, context_window}`, so a host's only
route to a valid `name` was reading the child's `~/.tau/config.json` out of
band — which defeats G1 (docs/REMOTE-CONTROL.md: "a second implementation
should be possible from this document plus the generated reference").

What this file pins, beyond "the list comes back":

* the round trip that IS the finding — a name read off `get_models` is a
  name `set_model` accepts, and `get_state` then reports the model
  `get_models` advertised for it. A verb that listed names `set_model`
  rejects would satisfy a shape test and still leave G1 broken.
* the three refusals. No resolver bound, a resolver that cannot be
  enumerated, and a config entry that does not BUILD each raise
  (INTERNAL_ERROR) rather than resolving into a shorter or empty list —
  Fail-Early, and the distinction this verb's notes draw between "this
  child has no configured models" (a real, empty answer) and "nobody here
  can answer that".
* read-ness: no D-1 `turn_safety_guard` (it answers with a turn in flight)
  and no `cursor` (E5 rule 2).

A new file per unit (docs/RPC-TIER-B.md §3 B1-B6 bullet: "a new file, so no
unit contends on a test file with any other").

A real `AgentSession` (not a `MagicMock`) for the same reason
`test_rpc_tier_b_set_model.py` gives: this verb reads the real
`_model_resolver` binding and the real `turn_lock`.

Reference: docs/RPC-TIER-B.md §6 "Every test must be able to fail"; the Tier
B review, finding 7.
"""

from __future__ import annotations

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.rpc import commands, dialect
from tau_agent_core.rpc.handler import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model


def _model(name: str, provider: str = "openai", context_window: int = 8192) -> Model:
    return Model(
        id=f"{name}-id",
        provider=provider,
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name=name,
        context_window=context_window,
        max_tokens=256,
    )


#: Deliberately NOT in sorted order, and with three distinguishing traits per
#: entry (id != config key, differing providers, differing context windows):
#: a verb that echoed the config key as the id, or the ACTIVE model's
#: provider/window for every row, would pass against a uniform fixture.
_MODELS: dict[str, Model] = {
    "zeta": _model("zeta", "anthropic", 200000),
    "alpha": _model("alpha", "openai", 8192),
    "mid": _model("mid", "gemini", 100000),
}


class _Resolver:
    """The shape `tau_coding_agent.backends.ConfigModelResolver` has — callable,
    plus the `model_names()` the RPC layer probes for (`commands
    ._MODEL_CATALOG_ATTR`).

    Test-local rather than an import of the real one: `tau-agent-core` does not
    depend on `tau-coding-agent` (no test in this package imports it), and the
    coupling that matters — that the SHIPPED resolver still answers the probe —
    is pinned on the other side of the boundary, in
    `tau-coding-agent/tests/test_model_resolver_wiring.py`.
    """

    def __init__(self, models: dict[str, Model] | None = None) -> None:
        self._models = _MODELS if models is None else models

    def model_names(self) -> list[str]:
        return sorted(self._models)

    def __call__(self, name: str) -> Model:
        try:
            return self._models[name]
        except KeyError:
            raise KeyError(
                f"unknown model {name!r}; configured models: {', '.join(sorted(self._models))}"
            ) from None


class _DurableLog(InMemorySessionLog):
    """`set_model`'s (D-2) preconditions, satisfied by the smallest thing that
    can: a `path` so `require_durable_session` sees a declared, set location,
    and `append_model_change` so `require_log_appender` does. Used by ONE test
    here — the wire round trip — because that test needs `set_model` to
    succeed for a reason other than the name it was given; every refusal
    `set_model` itself owns is `test_rpc_tier_b_set_model.py`'s subject.
    """

    path = "/tmp/get-models-round-trip.jsonl"

    def append_model_change(self, model: str, backend: str) -> str:
        return "model-change"


@pytest.fixture
def session() -> AgentSession:
    s = AgentSession(session_log=InMemorySessionLog(), model=_MODELS["alpha"], tools=[])
    s.set_model_resolver(_Resolver())
    return s


@pytest.fixture
def handler(session: AgentSession) -> RPCHandler:
    return RPCHandler(session)


async def _call(handler: RPCHandler, method: str = "get_models", **params) -> dict:
    """Dispatch and return the whole wire response (envelope included), so a
    test can assert on `error` as readily as on `result`."""
    request: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        request["params"] = params
    await handler._handle_request(request)
    return await handler._output_queue.get()


# ── table wiring ─────────────────────────────────────────────────────────


def test_get_models_is_a_tier_b_read_with_schemas() -> None:
    entry = commands.COMMAND_TABLE["get_models"]
    assert entry.tier == "B"
    assert entry.since == "tier-b"
    assert entry.handler is not None
    assert entry.declined_because is None
    assert entry.result_schema is not None
    assert entry.result_schema["required"] == ["models"]
    # A read takes no params at all (the same NO_PARAMS_SCHEMA object every
    # other read on the table uses, so an unexpected param is refused by
    # `validate_params` before the handler runs).
    assert entry.params_schema is commands.NO_PARAMS_SCHEMA


async def test_an_unexpected_param_is_refused(handler: RPCHandler) -> None:
    response = await _call(handler, provider="openai")
    assert "result" not in response
    assert response["error"]["code"] == dialect.INVALID_PARAMS


# ── the catalogue ────────────────────────────────────────────────────────


async def test_lists_every_configured_name_sorted_with_get_state_s_projection(
    handler: RPCHandler,
) -> None:
    """The whole result, asserted as one literal: names sorted, and each
    `model` the {id, provider, context_window} projection `get_state`
    publishes — read off the RESOLVED model, so per-entry differences
    survive."""
    response = await _call(handler)

    assert "error" not in response
    assert response["result"]["models"] == [
        {
            "name": "alpha",
            "model": {"id": "alpha-id", "provider": "openai", "context_window": 8192},
        },
        {"name": "mid", "model": {"id": "mid-id", "provider": "gemini", "context_window": 100000}},
        {
            "name": "zeta",
            "model": {"id": "zeta-id", "provider": "anthropic", "context_window": 200000},
        },
    ]


async def test_the_listed_projection_has_exactly_get_state_s_fields(handler: RPCHandler) -> None:
    """Same KEY SET as `get_state`'s `model`, asserted against a live
    `get_state` response rather than a hand-copied field list — a host is
    told these are the same shape, and a drift on either side is the
    regression."""
    listed = (await _call(handler))["result"]["models"]
    state = await _call(handler, method="get_state")

    active_projection = state["result"]["model"]
    assert set(active_projection) == {"id", "provider", "context_window"}
    for entry in listed:
        assert set(entry) == {"name", "model"}
        assert set(entry["model"]) == set(active_projection)


async def test_a_listed_name_is_one_set_model_accepts(session, handler) -> None:
    """The G1 round trip finding 7 is about, driven entirely over the wire:
    `get_models` for a name, `set_model` with that exact string, `get_state`
    for the result — no out-of-band read of ~/.tau/config.json anywhere in
    the loop, which is the whole claim.

    The durable-log fake is `set_model`'s precondition (D-2 /
    `require_durable_session`), not this verb's; without it the third call
    would refuse for a reason that has nothing to do with the name."""
    session.session_log = _DurableLog()

    listed = (await _call(handler))["result"]["models"]
    target = next(entry for entry in listed if entry["name"] == "zeta")

    switched = await _call(handler, method="set_model", name=target["name"])
    assert "error" not in switched, switched
    assert switched["result"]["model"] == target["model"]

    state = await _call(handler, method="get_state")
    assert state["result"]["model"] == target["model"]


async def test_an_empty_config_lists_nothing_and_is_not_an_error(session, handler) -> None:
    """ "This child has no configured models" is a real answer with a real
    (empty) list — the refusals below are for the case where nobody can be
    asked at all. Conflating the two is what would make a host stop."""
    session.set_model_resolver(_Resolver({}))

    response = await _call(handler)

    assert "error" not in response
    assert response["result"]["models"] == []


async def test_the_active_model_is_not_flagged_and_needs_no_config_entry(session, handler) -> None:
    """Known gap 1 in the verb's notes, pinned so it stays a stated gap
    rather than a surprise: an ad-hoc startup model (`--model provider/id`,
    no config key) does not appear here at all, and no listed entry claims
    to be the active one. A host reads `get_state` for what is running."""
    session._model = _model("ad-hoc", "openai")

    response = await _call(handler)

    names = [entry["name"] for entry in response["result"]["models"]]
    assert "ad-hoc" not in names
    for entry in response["result"]["models"]:
        assert "active" not in entry


# ── E5 / D-1: it is a read ───────────────────────────────────────────────


async def test_carries_no_cursor(handler: RPCHandler) -> None:
    """E5 rule 2 (commands.py "E5 in Tier B"): a read never carries one."""
    assert "cursor" not in commands.COMMAND_TABLE["get_models"].result_schema["properties"]
    assert "cursor" not in (await _call(handler))["result"]


async def test_answers_while_a_turn_holds_the_turn_lock(session, handler) -> None:
    """D-1 binds the four MUTATING Tier B verbs; this one must NOT take the
    guard. With `turn_lock` held — a turn genuinely in flight — a verb that
    took the guard would sit on it and then answer TURN_STILL_RUNNING.

    No timeout shrinking (the trick `test_rpc_tier_b_set_model.py` needs):
    if this verb ever grew the guard, this test would take the full 5s
    `DEFAULT_SWAP_TIMEOUT_S` and then fail on the code — slow, but the
    failure is unambiguous."""
    await session.turn_lock.acquire()
    try:
        response = await _call(handler)
    finally:
        session.turn_lock.release()

    assert "error" not in response
    assert len(response["result"]["models"]) == 3


# ── the three refusals (Fail-Early) ──────────────────────────────────────


async def test_no_resolver_bound_refuses_rather_than_listing_nothing(session, handler) -> None:
    """A session with no resolver cannot switch models at all (`set_model`
    raises RuntimeError there too), so an empty catalogue would be a host-
    facing lie about a fixable wiring mistake."""
    session._model_resolver = None

    response = await _call(handler)

    assert "result" not in response
    assert response["error"]["code"] == dialect.INTERNAL_ERROR
    assert "no model resolver is bound" in response["error"]["message"]


async def test_a_resolver_that_cannot_enumerate_refuses(session, handler) -> None:
    """The probe is `model_names` and unknown means REFUSE (the asymmetry
    `_DURABLE_LOCATION_ATTRS` documents for the durability guard): a plain
    `Callable[[str], Model]` — which is all `set_model_resolver` requires —
    resolves names fine but can list none, and saying "[]" for that would
    read as "set_model has no valid argument"."""

    def _bare_callable(name: str) -> Model:  # no model_names()
        return _MODELS[name]

    session.set_model_resolver(_bare_callable)

    response = await _call(handler)

    assert "result" not in response
    assert response["error"]["code"] == dialect.INTERNAL_ERROR
    assert commands._MODEL_CATALOG_ATTR in response["error"]["message"]


async def test_an_unbuildable_entry_fails_the_verb_naming_it(session, handler) -> None:
    """A config entry `build_model_from_config` refuses (its own ValueError —
    e.g. an invalid `reasoning_replay`) fails this verb, naming that entry
    and carrying the resolver's own words. Dropping it would hand a host a
    list it would trust as complete."""

    class _OneBadEntry(_Resolver):
        def __call__(self, name: str) -> Model:
            if name == "mid":
                raise ValueError("reasoning_replay must be one of 'all', 'turn', 'off'")
            return super().__call__(name)

    session.set_model_resolver(_OneBadEntry())

    response = await _call(handler)

    assert "result" not in response
    assert response["error"]["code"] == dialect.INTERNAL_ERROR
    assert "'mid'" in response["error"]["message"]
    assert "reasoning_replay must be one of" in response["error"]["message"]


async def test_an_unbuildable_entry_is_not_reported_as_a_partial_list(session, handler) -> None:
    """The refusal above replaces the answer; it does not accompany one.
    Asserted separately because "raises AND returns the other two" is the
    shape a well-meaning fix would take."""

    class _OneBadEntry(_Resolver):
        def __call__(self, name: str) -> Model:
            if name == "alpha":
                raise ValueError("nope")
            return super().__call__(name)

    session.set_model_resolver(_OneBadEntry())

    response = await _call(handler)

    assert "result" not in response
    assert handler._output_queue.empty()
