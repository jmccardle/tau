"""``api.request`` and ``api.subscriber_count``: the emitter owns the outcome (#98).

docs/EXT-REQUEST.md. Every timeout here is short; a sync subscriber that
outlives one is a daemon thread, waited for only where its warning is checked.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.policies import (
    DeadlineExpired,
    Failed,
    PausableDeadline,
    RequestReply,
    Returned,
    runs_on_behalf_of,
)
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.suspension import TurnSuspended

REQUESTER = "test_ext_request._requester"
CHANNEL = f"ext:{REQUESTER}:ask"
TIMEOUT = 0.2

_apis: dict[str, Any] = {}


def _requester(api: Any) -> None:
    _apis["requester"] = api


def _subscriber(name: str, handler: Any, policy: str | None = None) -> Any:
    def factory(api: Any) -> None:
        _apis[name] = api
        api.on(CHANNEL, handler, policy=policy)

    factory.__qualname__ = f"_sub_{name}"
    return factory


def _session(*extensions: Any) -> AgentSession:
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    return AgentSession(
        session_log=InMemorySessionLog(), model=model, extensions=[_requester, *extensions]
    )


def _name(session: AgentSession, index: int) -> str:
    return session._extension_runner._extensions[index].name


async def _request(payload: Any = None, timeout_s: float = TIMEOUT) -> list[RequestReply]:
    replies: list[RequestReply] = await _apis["requester"].request(
        "ask", payload, timeout_s=timeout_s
    )
    return replies


@pytest.fixture(autouse=True)
def _reset() -> None:
    _apis.clear()


async def test_no_subscribers_gives_an_empty_list() -> None:
    _session()
    assert await _request() == []
    assert _apis["requester"].subscriber_count("ask") == 0


async def test_a_fast_subscriber_returns_its_return_value_not_the_payload() -> None:
    async def answer(payload: dict[str, Any]) -> str:
        payload["mutated"] = True
        return "route-b"

    session = _session(_subscriber("fast", answer))
    payload: dict[str, Any] = {}
    assert await _request(payload) == [
        RequestReply(_name(session, 1), Returned("route-b"), "returned")
    ]


async def test_a_slow_subscriber_expires_at_the_timeout() -> None:
    async def slow(payload: Any) -> None:
        await asyncio.sleep(10)

    _session(_subscriber("slow", slow))
    started = time.monotonic()
    [reply] = await _request()
    assert time.monotonic() - started < TIMEOUT + 0.2
    assert reply.status == "expired" and not reply.abandoned
    assert isinstance(reply.outcome, Failed)
    assert isinstance(reply.outcome.error, DeadlineExpired)


async def test_a_raising_subscriber_keeps_its_exception() -> None:
    def boom(payload: Any) -> None:
        raise LookupError("no route for this")

    _session(_subscriber("boom", boom))
    [reply] = await _request()
    assert reply.status == "raised"
    assert isinstance(reply.outcome, Failed)
    assert type(reply.outcome.error) is LookupError
    assert str(reply.outcome.error) == "no route for this"


async def test_a_subscribers_own_deadline_expiring_is_raised_not_expired() -> None:
    async def nested(payload: Any) -> None:
        async def inner() -> None:
            await asyncio.sleep(10)

        await PausableDeadline(0.02).run(inner)

    _session(_subscriber("nested", nested))
    [reply] = await _request(timeout_s=1)
    assert reply.status == "raised"
    assert isinstance(reply.outcome, Failed) and isinstance(reply.outcome.error, DeadlineExpired)


async def test_a_sync_subscriber_is_preempted_and_abandoned() -> None:
    released = threading.Event()

    def blocks(payload: Any) -> str:
        released.wait(2)
        return "too late"

    session = _session(_subscriber("blocks", blocks))
    started = time.monotonic()
    [reply] = await _request()
    assert time.monotonic() - started < TIMEOUT + 0.2
    assert reply.status == "expired" and reply.abandoned
    released.set()
    deadline = time.monotonic() + 3
    while not session.extension_warnings():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    [warning] = session.extension_warnings()
    assert (warning.source, warning.key) == (_name(session, 1), f"policy:{CHANNEL}")
    assert "abandoned on its worker thread finished" in warning.message


@pytest.mark.parametrize("cause", ["timeout", "error"])
async def test_an_advisory_subscriber_cannot_downgrade_the_requested_failure(cause: str) -> None:
    async def advisory(payload: Any) -> None:
        if cause == "timeout":
            await asyncio.sleep(10)
        raise RuntimeError("advisory broke")

    session = _session(_subscriber("advisory", advisory, policy="advisory"))
    [reply] = await _request()
    assert isinstance(reply.outcome, Failed)
    assert reply.status == ("expired" if cause == "timeout" else "raised")
    assert session.extension_warnings() == []


async def test_subscribers_run_concurrently_in_order_bounded_by_one_timeout() -> None:
    async def slow(payload: Any) -> None:
        await asyncio.sleep(10)

    async def fast(payload: Any) -> str:
        return "fast"

    session = _session(
        _subscriber("a", slow),
        _subscriber("b", fast),
        _subscriber("c", slow),
        _subscriber("d", slow),
    )
    started = time.monotonic()
    replies = await _request()
    assert time.monotonic() - started < 2 * TIMEOUT
    assert [r.extension for r in replies] == [_name(session, i) for i in (1, 2, 3, 4)]
    assert [r.status for r in replies] == ["expired", "returned", "expired", "expired"]


async def test_cancelling_the_request_cancels_every_subscriber_and_abandons_threads() -> None:
    cancelled: list[str] = []
    started = asyncio.Event()
    released = threading.Event()

    async def waits(payload: Any) -> None:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append("async")
            raise

    def blocks(payload: Any) -> None:
        released.wait(2)

    session = _session(_subscriber("waits", waits), _subscriber("blocks", blocks))
    before = asyncio.all_tasks()
    request = asyncio.create_task(_request(timeout_s=5))
    await started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert cancelled == ["async"]
    assert asyncio.all_tasks() - before == set()
    released.set()
    deadline = time.monotonic() + 3
    while not session.extension_warnings():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    assert "abandoned" in session.extension_warnings()[0].message


async def test_a_subscribers_turn_suspended_leaves_unchanged_and_cancels_the_rest() -> None:
    cancelled: list[str] = []

    async def suspends(payload: Any) -> None:
        await asyncio.sleep(0.01)
        raise TurnSuspended()

    async def waits(payload: Any) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append("waits")
            raise

    _session(_subscriber("waits", waits), _subscriber("suspends", suspends))
    with pytest.raises(TurnSuspended):
        await _request(timeout_s=5)
    assert cancelled == ["waits"]


async def test_an_enclosing_deadline_caps_the_request() -> None:
    async def slow(payload: Any) -> None:
        await asyncio.sleep(10)

    _session(_subscriber("slow", slow))
    outer = PausableDeadline(0.05)
    started = time.monotonic()
    with pytest.raises(DeadlineExpired) as raised:
        await outer.run(lambda: _request(timeout_s=5))
    assert raised.value.deadline is outer
    assert time.monotonic() - started < 1


async def test_a_subscriber_runs_on_behalf_of_the_requester() -> None:
    owner = asyncio.current_task()
    assert owner is not None
    seen: list[bool] = []

    async def check(payload: Any) -> None:
        seen.append(runs_on_behalf_of(owner))

    def check_sync(payload: Any) -> None:
        seen.append(runs_on_behalf_of(owner))

    _session(_subscriber("a", check), _subscriber("b", check_sync))
    await _request()
    assert seen == [True, True]


async def test_subscriber_count_is_complete_at_session_start() -> None:
    counts: list[int] = []

    def counter(api: Any) -> None:
        _apis["requester"] = api
        api.on("session_start", lambda event, ctx: counts.append(api.subscriber_count("ask")))

    counter.__qualname__ = "_requester"
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=model,
        extensions=[
            counter,
            _subscriber("one", lambda payload: None),
            _subscriber("two", lambda payload: None, policy="gating"),
        ],
    )
    await session.emit_session_start()
    assert counts == [2]
    with pytest.raises(ValueError, match="topic"):
        _apis["requester"].subscriber_count(" ")


async def test_bad_arguments_are_refused() -> None:
    _session()
    with pytest.raises(ValueError, match="timeout_s"):
        await _request(timeout_s=0)
    with pytest.raises(ValueError, match="topic"):
        await _apis["requester"].request("", None, timeout_s=1)


async def test_emit_still_runs_the_subscribers_own_policy() -> None:
    async def advisory(payload: Any) -> None:
        raise RuntimeError("advisory broke")

    session = _session(_subscriber("advisory", advisory, policy="advisory"))
    await _apis["requester"].emit("ask", None)
    [warning] = session.extension_warnings()
    assert warning.message == f"{CHANNEL} handler proceeded: RuntimeError: advisory broke"
