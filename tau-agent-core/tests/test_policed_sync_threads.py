"""A sync handler under a deadline runs on a worker thread and returns at the deadline (#97).

docs/STEP-DEBUGGER.md §4, "Built (#97)". Deadlines are short; a handler that
outlives one is a daemon thread the test waits for only when it checks the
late-finish warning.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionAPI, ExtensionContext, ExtensionUI
from tau_agent_core.policies import (
    Bounded,
    DeadlineExpired,
    Failed,
    HandlerCall,
    HandlerPolicyError,
    PausableDeadline,
    PolicySite,
    Returned,
    Unbounded,
    police_thread_affinity,
    runs_on_behalf_of,
)
from tau_agent_core.session_log import InMemorySessionLog

_TS = 1_700_000_000_000


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _text(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=_TS,
        usage=Usage(),
    )


def _script(*finals: AssistantMessage):
    queue = list(finals)

    async def fake(model, context, options=None):
        return _Stream([DoneEvent(final=queue.pop(0), usage=Usage())])

    return fake


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
    return AgentSession(session_log=InMemorySessionLog(), model=model, extensions=list(extensions))


def _call(handler: Any, *args: Any, warnings: list[str] | None = None) -> HandlerCall:
    sink = warnings if warnings is not None else []
    return HandlerCall(handler, args, hook="turn_end", warn=sink.append)


def _site() -> PolicySite:
    return PolicySite("turn_end", "ext", lambda text: None)


async def _until(predicate: Any, limit: float = 3.0) -> None:
    deadline = time.monotonic() + limit
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


# ── the deadline returns on time ────────────────────────────────────────────


async def test_a_blocking_sync_handler_returns_failed_at_the_deadline() -> None:
    def blocks(event: Any) -> str:
        time.sleep(2)
        return "too late"

    started = time.monotonic()
    outcome = await Bounded(0.2, "fail", "fail").run(_call(blocks, {}), _site())
    assert time.monotonic() - started < 0.5
    assert isinstance(outcome, Failed) and isinstance(outcome.error, DeadlineExpired)


async def test_the_late_result_is_dropped_and_the_late_finish_is_warned() -> None:
    warnings: list[str] = []
    released = threading.Event()

    def late(event: Any) -> str:
        released.wait(2)
        return "late value"

    call = _call(late, {}, warnings=warnings)
    outcome = await Bounded(0.05, "fail", "fail").run(call, _site())
    assert isinstance(outcome, Failed) and isinstance(outcome.error, DeadlineExpired)
    assert call.abandoned
    released.set()
    await _until(lambda: warnings)
    [warning] = warnings
    assert warning.startswith("turn_end handler abandoned on its worker thread finished ")
    assert warning.endswith("s late")


async def test_a_late_raise_is_warned_with_its_error() -> None:
    warnings: list[str] = []

    def late(event: Any) -> None:
        time.sleep(0.15)
        raise ValueError("after the fact")

    outcome = await Bounded(0.05, "proceed", "proceed").run(
        _call(late, {}, warnings=warnings), _site()
    )
    assert outcome.record.startswith("DeadlineExpired")  # type: ignore[union-attr]
    await _until(lambda: warnings)
    assert "raised ValueError: after the fact" in warnings[0]


async def test_a_sync_handler_that_finishes_in_time_returns_its_value_and_its_awaitable() -> None:
    async def later() -> str:
        return "awaited"

    assert await Bounded(1, "fail", "fail").run(_call(lambda e: 7, {}), _site()) == Returned(7)
    assert await Bounded(1, "fail", "fail").run(_call(lambda e: later(), {}), _site()) == Returned(
        "awaited"
    )
    raised = await Bounded(1, "fail", "fail").run(_call(lambda e: 1 / 0, {}), _site())
    assert isinstance(raised, Failed) and isinstance(raised.error, ZeroDivisionError)


async def test_the_thread_sees_the_context_and_counts_as_the_awaiting_task() -> None:
    import contextvars

    var: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="unset")
    var.set("inherited")
    owner = asyncio.current_task()
    assert owner is not None
    seen: dict[str, Any] = {}

    def handler(event: Any) -> None:
        seen["var"] = var.get()
        seen["on_behalf"] = runs_on_behalf_of(owner)
        seen["thread"] = threading.get_ident()

    await PausableDeadline(1).run(_call(handler, {}))
    assert seen["var"] == "inherited"
    assert seen["on_behalf"] is True
    assert seen["thread"] != threading.get_ident()


async def test_an_abandoned_thread_no_longer_counts_as_the_turn() -> None:
    owner = asyncio.current_task()
    assert owner is not None
    abandoned = threading.Event()
    seen: list[bool] = []

    def handler(event: Any) -> None:
        abandoned.wait(2)
        seen.append(runs_on_behalf_of(owner))

    with pytest.raises(DeadlineExpired):
        await PausableDeadline(0.05).run(_call(handler, {}))
    abandoned.set()
    await _until(lambda: seen)
    assert seen == [False]


async def test_unbounded_and_an_async_handler_run_inline() -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def sync(event: Any) -> None:
        seen.append(threading.get_ident())

    async def asynchronous(event: Any) -> None:
        seen.append(threading.get_ident())

    await Unbounded().run(_call(sync, {}), _site())
    await Bounded(1, "fail", "fail").run(_call(asynchronous, {}), _site())
    assert seen == [loop_thread, loop_thread]


# ── thread affinity of ctx and api ──────────────────────────────────────────


@pytest.mark.parametrize("cls", [ExtensionContext, ExtensionAPI, ExtensionUI])
def test_every_public_member_has_a_thread_affinity(cls: type) -> None:
    public = {
        name
        for name, member in vars(cls).items()
        if not name.startswith("_") and (callable(member) or isinstance(member, property))
    }
    table = cls.__tau_thread_affinity__  # type: ignore[attr-defined]
    assert public == set(table), (
        f"classify {sorted(public - set(table))} in extension_types' thread-affinity "
        f"table; drop {sorted(set(table) - public)}"
    )


@pytest.mark.parametrize("cls", [ExtensionContext, ExtensionAPI, ExtensionUI])
def test_each_member_is_wrapped_as_classified(cls: type) -> None:
    for name, kind in cls.__tau_thread_affinity__.items():  # type: ignore[attr-defined]
        member = vars(cls)[name]
        fn = member.fget if isinstance(member, property) else member
        wrapped = hasattr(fn, "__wrapped__")
        assert wrapped is (kind != "read"), f"{cls.__name__}.{name} ({kind})"
        if kind != "refuse":
            assert not inspect.iscoroutinefunction(getattr(fn, "__wrapped__", fn)), name


def test_a_coroutine_method_cannot_be_marshalled() -> None:
    class Probe:
        async def go(self) -> None:
            return None

    with pytest.raises(TypeError, match="must be 'refuse'"):
        police_thread_affinity(Probe, {"go": "loop"})
    with pytest.raises(KeyError):
        police_thread_affinity(Probe, {"absent": "loop"})
    with pytest.raises(ValueError):
        police_thread_affinity(Probe, {"go": "sometimes"})  # type: ignore[dict-item]


async def test_each_affinity_from_a_worker_thread() -> None:
    owner = asyncio.current_task()
    assert owner is not None
    loop_thread = threading.get_ident()

    class Probe:
        def free(self) -> int:
            return threading.get_ident()

        def marshalled(self) -> tuple[int, bool]:
            return threading.get_ident(), runs_on_behalf_of(owner)

        @property
        def live(self) -> str:
            return "live"

        async def later(self) -> str:
            return "later"

    police_thread_affinity(
        Probe, {"free": "any", "marshalled": "loop", "live": "refuse", "later": "refuse"}
    )
    probe = Probe()
    seen: dict[str, Any] = {}

    def handler(event: Any) -> None:
        seen["free"] = probe.free()
        seen["marshalled"] = probe.marshalled()
        for name in ("live", "later"):
            try:
                getattr(probe, name) if name == "live" else probe.later()
            except RuntimeError as err:
                seen[name] = str(err)

    await PausableDeadline(1).run(_call(handler, {}))
    assert seen["free"] != loop_thread
    assert seen["marshalled"] == (loop_thread, True)
    assert "Probe.live cannot be called from a sync handler" in seen["live"]
    assert "coroutine method" in seen["later"]
    assert probe.marshalled() == (loop_thread, True)
    assert probe.live == "live" and await probe.later() == "later"


def _thread_probe(results: dict[str, Any]):
    def factory(api: Any) -> None:
        api.register_policy("quick_gate", Bounded(1, "fail", "fail"))

        def on_turn_end(event: Any, ctx: Any) -> None:
            results["thread"] = threading.get_ident()
            results["leaf"] = ctx.leaf_id()
            results["entries"] = len(ctx.entries())
            ctx.ui.notify("from the thread")
            api.set_warning("probe", "set from the thread")
            for label, attempt in (
                ("cursor", lambda: ctx.cursor),
                ("prompt", lambda: ctx.prompt("again")),
                ("emit", lambda: api.emit("topic", {})),
            ):
                try:
                    attempt()
                except RuntimeError as err:
                    results[label] = str(err)

        api.on("turn_end", on_turn_end, policy="quick_gate")

    return factory


async def test_ctx_and_api_from_a_policed_sync_handler() -> None:
    results: dict[str, Any] = {}
    notified: list[int] = []
    session = _session(_thread_probe(results))
    session._extension_api.context.ui.set_record_sink(
        lambda record: notified.append(threading.get_ident())
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await session.prompt("go")
    assert results["thread"] != threading.get_ident()
    assert results["leaf"] is not None and results["entries"] >= 2
    await _until(lambda: session.extension_warnings())
    assert notified and set(notified) == {threading.get_ident()}
    assert [w.message for w in session.extension_warnings()] == ["set from the thread"]
    assert "ExtensionContext.cursor cannot be called" in results["cursor"]
    assert "ExtensionContext.prompt cannot be called" in results["prompt"]
    assert "ExtensionAPI.emit cannot be called" in results["emit"]


async def test_a_sync_handler_calling_prompt_fails_at_once_not_at_the_deadline() -> None:
    def reentrant(api: Any) -> None:
        api.register_policy("slow_gate", Bounded(5, "fail", "fail"))

        def on_turn_end(event: Any, ctx: Any) -> Any:
            return ctx.prompt("again")

        api.on("turn_end", on_turn_end, policy="slow_gate")

    session = _session(reentrant)
    started = time.monotonic()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        with pytest.raises(HandlerPolicyError) as raised:
            await session.prompt("go")
    assert time.monotonic() - started < 2
    assert "ExtensionContext.prompt cannot be called" in raised.value.detail


async def test_an_abandoned_handler_may_not_act_on_the_session_later() -> None:
    released = threading.Event()
    done = threading.Event()
    refused: dict[str, str] = {}
    read: dict[str, Any] = {}

    def stale(api: Any) -> None:
        api.register_policy("tight", Bounded(0.05, "proceed", "proceed"))

        def on_turn_end(event: Any, ctx: Any) -> None:
            released.wait(2)
            for label, attempt in (
                ("set_model", lambda: ctx.set_model("anything")),
                ("shutdown", ctx.shutdown),
                ("set_warning", lambda: api.set_warning("late", "should not land")),
                ("clear_warning", lambda: api.clear_warning("late")),
                ("submit_threadsafe", lambda: api.submit_threadsafe("a late turn")),
                ("notify", lambda: ctx.ui.notify("late")),
            ):
                try:
                    attempt()
                except RuntimeError as err:
                    refused[label] = str(err)
            read.update(
                cwd=ctx.cwd,
                shutdown_requested=ctx.shutdown_requested,
                interactive=ctx.ui.interactive,
                extension_name=api.extension_name,
                config=api.config,
                context=api.context is ctx,
            )
            done.set()

        api.on("turn_end", on_turn_end, policy="tight")

    session = _session(stale)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await session.prompt("go")
    entries = len(session.session_log.entries())
    released.set()
    await _until(done.is_set)
    await _until(lambda: any("abandoned" in w.message for w in session.extension_warnings()))
    assert set(refused) == {
        "set_model",
        "shutdown",
        "set_warning",
        "clear_warning",
        "submit_threadsafe",
        "notify",
    }
    for label, error in refused.items():
        assert "this sync handler was abandoned" in error, label
    assert read["cwd"] == session._extension_api.context.cwd
    assert read["shutdown_requested"] is False and read["context"] is True
    assert session._extension_api.context.shutdown_requested is False
    await asyncio.sleep(0.05)
    assert len(session.session_log.entries()) == entries
    [warning] = session.extension_warnings()
    assert warning.key == "policy:turn_end"
    assert "handler abandoned on its worker thread finished" in warning.message


@pytest.mark.parametrize("mode", ["fail", "proceed"])
async def test_a_sync_stop_iteration_is_carried_as_runtime_error_at_once(mode: str) -> None:
    def stops(event: Any) -> None:
        raise StopIteration("done early")

    started = time.monotonic()
    outcome = await Bounded(1, mode, mode).run(_call(stops, {}), _site())  # type: ignore[arg-type]
    assert time.monotonic() - started < 0.5
    if mode == "fail":
        assert isinstance(outcome, Failed)
        assert type(outcome.error) is RuntimeError
        assert str(outcome.error) == "turn_end handler raised StopIteration"
        assert isinstance(outcome.error.__cause__, StopIteration)
    else:
        assert outcome.record == "RuntimeError: turn_end handler raised StopIteration"  # type: ignore[union-attr]


async def test_a_sync_stop_iteration_at_a_bounded_hook_fails_the_turn_at_once() -> None:
    def stopper(api: Any) -> None:
        api.register_policy("gate", Bounded(1, "fail", "fail"))

        def on_turn_end(event: Any, ctx: Any) -> None:
            raise StopIteration

        api.on("turn_end", on_turn_end, policy="gate")

    session = _session(stopper)
    started = time.monotonic()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        with pytest.raises(HandlerPolicyError) as raised:
            await session.prompt("go")
    assert time.monotonic() - started < 0.5
    assert raised.value.detail == "RuntimeError: turn_end handler raised StopIteration"


async def test_a_policed_sync_handler_blocks_the_turn_no_longer_than_its_deadline() -> None:
    def blocker(api: Any) -> None:
        api.register_policy("fast_gate", Bounded(0.2, "fail", "fail"))
        api.on("turn_end", lambda event, ctx: time.sleep(2), policy="fast_gate")

    session = _session(blocker)
    started = time.monotonic()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        with pytest.raises(HandlerPolicyError) as raised:
            await session.prompt("go")
    assert time.monotonic() - started < 0.5
    assert raised.value.detail == "DeadlineExpired: timed out after 0.2s"


@pytest.mark.parametrize("policy", [None, "human"])
async def test_an_unpoliced_or_unbounded_sync_handler_still_runs_on_the_loop(
    policy: str | None,
) -> None:
    seen: list[int] = []

    def inline(api: Any) -> None:
        def on_turn_end(event: Any, ctx: Any) -> None:
            seen.append(threading.get_ident())
            ctx.cursor

        api.on("turn_end", on_turn_end, policy=policy)

    session = _session(inline)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await session.prompt("go")
    assert seen == [threading.get_ident()]
