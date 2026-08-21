"""Phase 4 — T3 (bounded outbound queue + drain coupling) and P3 (extension-
requested shutdown), white-box.

Reference: docs/REMOTE-CONTROL.md §2 G3/G4/G5, §4[1] T3, §4[7] P3, §9 R-T3.

These drive `RPCHandler`/`AgentSession` directly (the Python API), the same
idiom `test_rpc.py` already uses for its C3 tests — see
`tau-coding-agent/tests/test_rpc_conformance.py` for the black-box/subprocess
half of R-T3 and the abort-reachability demonstration (driving a real `tau
--mode rpc` process with a peer that stops reading, per that requirement's
own wording).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from unittest.mock import patch

import pytest

from tau_llm.streaming import TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_agent_core.rpc import DEFAULT_OUTPUT_QUEUE_EVENT_BOUND, RPCHandler
from tau_agent_core.session_log import InMemorySessionLog


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


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    """The minimal ``stream_simple`` return shape (mirrors test_rpc.py)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __aiter__(self):
        async def _gen():
            yield TextDeltaEvent(delta=self._text, partial=_assistant(self._text))

        return _gen()

    async def result(self):
        return _assistant(self._text)

    def abort(self) -> None:
        pass


class _Recorder:
    """Stand-in for the real stdout handle: records parsed JSON, not bytes
    (mirrors test_rpc.py's own recorder).

    Backed by a REAL OS pipe, not an in-memory buffer: `_write_stdout` now
    moves bytes through `_connect_stdout_writer` (`loop.connect_write_
    pipe`), which requires an actual file descriptor — the writer-side fix
    for T6 blocker 2 (phase-4 review), mirroring the fix `_read_stdin`
    already had for the reader. An in-memory double with no `fileno()`
    cannot stand in for stdout any more, exactly as test_rpc.py's own
    `_FakeStdin` note already states for stdin's reader-side equivalent. A
    background thread drains the read end and parses each complete line as
    JSON into `.lines`, so callers assert on it exactly as before the
    writer swap; `close()`/`join()` let a caller shut the pipe down and
    wait for that thread to catch up once the writer is done with it.
    """

    def __init__(self) -> None:
        read_fd, write_fd = os.pipe()
        self._write_file = os.fdopen(write_fd, "w")
        self.lines: list[dict] = []
        self._read_file = os.fdopen(read_fd, "r")
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    @property
    def buffer(self):  # what `_connect_stdout_writer` actually wires up to
        return self._write_file.buffer

    def _drain(self) -> None:
        for line in self._read_file:
            line = line.rstrip("\n")
            if line:
                self.lines.append(json.loads(line))

    def close(self) -> None:
        self._write_file.close()

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout=timeout)


@pytest.fixture
def real_session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])


@pytest.fixture
def real_handler(real_session: AgentSession) -> RPCHandler:
    return RPCHandler(real_session)


async def _drain_until(handler: RPCHandler, predicate, limit: int = 50, timeout: float = 5.0):
    for _ in range(limit):
        item = await asyncio.wait_for(handler._output_queue.get(), timeout=timeout)
        if predicate(item):
            return item
    raise AssertionError("predicate never matched within the item limit")


# ── T3: the bound itself ─────────────────────────────────────────────────────


async def test_backpressure_bounds_the_event_queue_and_stalls_the_producer(real_handler):
    """R-T3 (quantitative half — see test_rpc_conformance.py for the
    subprocess/qualitative half). With nothing draining `_output_queue`, at
    most `DEFAULT_OUTPUT_QUEUE_EVENT_BOUND` event items may be outstanding —
    the next `_forward_event` call BLOCKS (never raises, never silently
    proceeds past the bound) until a slot is released.

    Revert the credit gate (make `_forward_event` a plain `put_nowait` again)
    and this fails at ``assert not task.done()`` — nothing would stall.
    """
    bound = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND
    for i in range(bound):
        await real_handler._forward_event(AgentEvent(type="turn_start", timestamp=i, turn_index=i))
    assert real_handler._output_queue.qsize() == bound

    task = asyncio.create_task(
        real_handler._forward_event(AgentEvent(type="turn_start", timestamp=999, turn_index=999))
    )
    try:
        await asyncio.sleep(0.25)
        assert not task.done(), "the (bound+1)th event did not stall — the queue is not bounded"
        assert real_handler._output_queue.qsize() == bound, "grew past the bound"

        # Draining one item, exactly as the writer would, frees one slot.
        real_handler._output_queue.get_nowait()
        real_handler._event_credits.release()
        await asyncio.wait_for(task, timeout=1.0)
        assert real_handler._output_queue.qsize() == bound
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_forward_event_never_drops_an_event_when_credits_are_exhausted(
    real_handler, real_session
):
    """Phase-3 review, Finding 1: a `put_nowait` onto a queue that could
    raise `QueueFull` would be caught by `EventBus.emit`'s own
    `try/except`, routed to `_surface_handler_error`, and the event would be
    gone — silently, on exactly the path G3/G4 exist to protect. Prove the
    new shape cannot do that: drive the REAL bus, register an error
    listener, exhaust every credit, and confirm the event still arrives —
    merely late, never lost, and never surfaced as a handler failure.

    Revert `_acquire_event_credit`'s use of `asyncio.Queue(maxsize=...)`
    semantics (i.e. give `_output_queue` a literal `maxsize` again and drop
    the credit gate) and this fails: `errors` would gain an entry (or the
    event would vanish outright).
    """
    errors: list[tuple[BaseException, str]] = []
    real_session._events.on_error(lambda exc, channel: errors.append((exc, channel)))

    bound = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND
    for _ in range(bound):
        real_handler._event_credits.try_acquire()

    event = AgentEvent(type="turn_start", timestamp=1, turn_index=0)
    task = asyncio.create_task(real_session._events.emit(event))
    try:
        await asyncio.sleep(0.25)
        assert not task.done()  # genuinely stalled, not an immediate silent drop
        assert errors == []

        for _ in range(bound):
            real_handler._event_credits.release()
        await asyncio.wait_for(task, timeout=1.0)
        assert errors == []

        item = real_handler._output_queue.get_nowait()
        assert item["params"]["type"] == "turn_start"
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# ── the three `put_nowait` sites (phase-4 spec) ──────────────────────────────


async def test_admission_ack_wins_the_race_against_agent_start_for_the_one_free_credit(
    real_handler, real_session
):
    """C3 under T3: `commands._on_admitted`'s synchronous, UNCREDITED
    `put_nowait` must win the race onto the wire even when `agent_start`
    is free to enqueue immediately behind it — not just in the ordinary
    (idle-queue) case `test_prompt_acknowledges_before_any_turn_event_
    reaches_the_wire` (test_rpc.py) drives.

    **Finding 6 (phase-4 review) — why this replaced the credit-exhausted
    version.** That version's docstring named its own mutation as "revert
    `_on_admitted` to `await handler._output_queue.put(...)`" — not
    actually expressible: `_on_admitted` is a synchronous callback (see its
    own docstring in commands.py for why it MUST stay that way) and cannot
    `await` at all. The closest expressible form of "the ack's enqueue is
    no longer unconditionally synchronous" is deferring it by one
    event-loop turn (`loop.call_soon(put_nowait, ...)`) instead of calling
    `put_nowait` inline — exactly the reordering `_submit_and_acknowledge`'s
    own docstring measured against the ORIGINAL bug this whole mechanism
    fixes. Applied against the credit-EXHAUSTED setup, that mutation
    changed nothing: `agent_start` cannot enqueue AT ALL without first
    winning a >=100ms poll wait for a credit (`_acquire_event_credit`), so
    the deferred-but-still-near-instant ack had no competitor to lose to —
    "with every credit taken there is, by construction, no competitor for
    the ack to lose to." Left exactly ONE credit free instead, so
    `agent_start`'s own `_forward_event` can ALSO complete synchronously
    (`_event_credits.try_acquire()` succeeds without suspending) — a real
    race the mutation can actually flip.

    Demonstrated (phase-4 review, by hand): reverting `_on_admitted`'s
    `handler._output_queue.put_nowait(...)` call to
    `asyncio.get_running_loop().call_soon(handler._output_queue.put_nowait,
    ...)` makes THIS test fail (`first` is `agent_start`'s wire item, not
    the ack) while leaving the real implementation unchanged.
    """
    bound = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND
    for _ in range(bound - 1):
        real_handler._event_credits.try_acquire()
    # Exactly one credit remains -- enough that agent_start is NOT
    # structurally blocked out (unlike the credit-exhausted setup this
    # replaced), so the ack's ordering guarantee is actually exercised
    # rather than trivially true because the competitor can't run yet.

    gate = asyncio.Event()

    async def _gated_stream_simple(model, context, options=None):
        await gate.wait()
        return _Stream("hi there")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
        await real_handler._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hello"}}
        )
        first = await asyncio.wait_for(real_handler._output_queue.get(), timeout=5.0)
        assert first.get("result", {}).get("accepted") is True, (
            "the admission ack lost the race for the one free credit "
            f"agent_start could also take: {first!r} arrived first"
        )

        second = await asyncio.wait_for(real_handler._output_queue.get(), timeout=5.0)
        assert second["params"]["type"] == "agent_start"

        # Hand every outstanding credit back, as the writer normally would
        # on dequeue, and let the turn actually finish.
        for _ in range(bound):
            real_handler._event_credits.release()
        gate.set()
        agent_end = await _drain_until(
            real_handler,
            lambda item: item.get("method") == "event" and item["params"]["type"] == "agent_end",
        )
        assert agent_end["params"]["is_error"] is False


async def test_agent_end_cursor_stays_correct_under_genuine_backpressure():
    """Site 3 (phase-4 spec): confirm the phase-3 cursor fix
    (`RPCHandler._stamp_agent_end_cursor`'s `_cursor_log` captured at
    enqueue time) holds when `_forward_event`'s OWN wait for a credit is
    what suspends the turn, not merely when a peer is slow to READ an
    already-enqueued item (the scenario the original,
    `test_agent_end_wire_event_carries_the_post_persistence_cursor`, drives).
    A bound of 1 forces the real writer to repeatedly drain-then-release
    for this turn to finish at all, which is exactly what "backlog is
    routine, not rare" (T3) means in practice.
    """
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
    handler = RPCHandler(session, output_queue_event_bound=1)

    pre_turn_cursor = session.session_log.cursor

    async def _fast_stream_simple(model, context, options=None):
        return _Stream("hi there")

    recorder = _Recorder()
    handler._real_stdout = recorder
    handler._running = True
    pump = asyncio.create_task(handler._write_stdout())

    def _has_agent_end() -> bool:
        return any(
            line.get("method") == "event" and line["params"].get("type") == "agent_end"
            for line in recorder.lines
        )

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fast_stream_simple):
        await handler._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hello"}}
        )
        for _ in range(500):
            if _has_agent_end():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("agent_end never reached the recorder")

    handler._running = False
    await asyncio.wait_for(pump, timeout=5.0)
    recorder.close()
    recorder.join()

    (agent_end,) = [
        line
        for line in recorder.lines
        if line.get("method") == "event" and line["params"].get("type") == "agent_end"
    ]
    post_turn_cursor = session.session_log.cursor
    assert post_turn_cursor != pre_turn_cursor
    assert agent_end["params"]["cursor"] == post_turn_cursor


# ── abort reachability under a stalled loop ──────────────────────────────────


async def test_abort_lets_a_credit_starved_turn_wind_down_without_waiting_for_room(
    real_handler, real_session
):
    """G5: a turn genuinely stalled on a full queue must still notice
    `abort()` and let its remaining events reach the queue, rather than
    staying wedged until a host that has already stopped reading resumes
    doing so — see `_acquire_event_credit`'s docstring for the full
    argument. `_handle_abort` itself is synchronous and never touches the
    credit pool (`commands._handle_abort` — a plain signal), so this is
    about the STALLED PRODUCER noticing the signal, not about `abort`'s own
    dispatch (that half is `test_rpc_conformance.py`'s
    `test_backpressure_does_not_wedge_abort_behind_a_stalled_turn`).

    Revert the `is_aborted` poll in `_acquire_event_credit` back to a bare
    wait on the credit (no abort escape hatch) and this fails: the task
    never completes (the `wait_for` below times out).
    """
    bound = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND
    for _ in range(bound):
        real_handler._event_credits.try_acquire()

    task = asyncio.create_task(
        real_handler._forward_event(AgentEvent(type="turn_start", timestamp=1, turn_index=0))
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # genuinely stalled: no credit available

    real_session.abort()
    await asyncio.wait_for(task, timeout=1.0)  # noticed within one poll interval

    item = real_handler._output_queue.get_nowait()
    assert item["params"]["type"] == "turn_start"
    assert "_credited" not in item  # bypassed the bound; did not consume one


async def test_acquire_event_credit_does_not_wait_at_all_once_already_aborted(
    real_handler, real_session
):
    """The cheap half of the same mechanism: an ALREADY-aborted turn's
    remaining items should not each pay one poll interval winding down."""
    bound = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND
    for _ in range(bound):
        real_handler._event_credits.try_acquire()
    real_session.abort()

    credited = await asyncio.wait_for(real_handler._acquire_event_credit(), timeout=0.05)
    assert credited is False


# ── blocker 1 (phase-4 review): a credit-starved turn cannot be killed ──────


async def test_shutting_down_lets_a_credit_starved_turn_wind_down_without_waiting_for_room(
    real_handler, real_session
):
    """Symmetric to the abort escape above, for the OTHER reason a stalled
    wait must give up: `run()` tearing down rather than an in-band `abort`.

    Deliberately `self._shutting_down`, not `not self._running` (the
    reviewer's own sketch) — see `_acquire_event_credit`'s docstring for
    why: `_running` is False for the lifetime of `real_handler` (this
    fixture never calls `run()`), so gating on it would make EVERY test in
    this file — including the two directly above — stop exercising
    backpressure at all instead of exercising this one specific case.

    Revert the `self._shutting_down` half of the escape (leave the
    `is_aborted` check alone) and this fails: the task never completes
    (`wait_for` below times out).
    """
    bound = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND
    for _ in range(bound):
        real_handler._event_credits.try_acquire()

    task = asyncio.create_task(
        real_handler._forward_event(AgentEvent(type="turn_start", timestamp=1, turn_index=0))
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # genuinely stalled: no credit available

    real_handler._shutting_down = True
    await asyncio.wait_for(task, timeout=1.0)  # noticed within one poll interval

    item = real_handler._output_queue.get_nowait()
    assert item["params"]["type"] == "turn_start"
    assert "_credited" not in item  # bypassed the bound; did not consume one


async def test_cancel_background_tasks_completes_a_turn_re_stalled_in_agent_end(
    real_session,
):
    """The reviewer's own repro (phase-4 review, blocker 1), reproduced
    white-box, driven through the ACTUAL shutdown entry point
    (`_cancel_background_tasks`) rather than an isolated `task.cancel()`:

    A turn is cancelled exactly ONCE, mid-stream. `AgentLoop.run`'s
    `except BaseException` bracket catches that cancellation on its way to
    emitting `agent_end` — and that emit re-enters `_acquire_event_credit`
    with credits still exhausted. No SECOND cancellation is ever delivered
    (the one this task received was already consumed by the bracket), so
    before this fix that second wait had no way out at all: `is_aborted` is
    false (nothing called `abort()`), and nothing else ever woke it — the
    task could never reach `done()`, which is exactly "a τ process whose
    peer stops reading cannot be killed" (SIGTERM's hang traces to
    precisely this: `run()` never awaits `_background_tasks`, so this task
    outlives `run()` and `asyncio.run()`'s own shutdown machinery
    (`_cancel_all_tasks`) joins it forever).

    Revert either half of the fix — the `_shutting_down` escape in
    `_acquire_event_credit`, or `run()`/`_cancel_background_tasks` setting
    it before reaping — and this fails: `task.done()` is still False after
    the bound below.
    """
    bound = 4
    handler = RPCHandler(real_session, output_queue_event_bound=bound)
    n_chunks = bound * 3

    class _ManyChunks:
        def __init__(self, n: int) -> None:
            self._n = n

        def __aiter__(self):
            async def _gen():
                acc = ""
                for _ in range(self._n):
                    acc += "x"
                    yield TextDeltaEvent(delta="x", partial=_assistant(acc))

            return _gen()

        async def result(self):
            return _assistant("x" * self._n)

        def abort(self) -> None:
            pass

    async def _stream_simple(model, context, options=None):
        return _ManyChunks(n_chunks)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_simple):
        await handler._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "go"}}
        )
        await asyncio.sleep(0.3)
        assert handler._background_tasks, "turn already finished -- nothing left to stall"
        (task,) = list(handler._background_tasks)
        assert not task.done()

        task.cancel()
        await asyncio.sleep(0.2)
        assert not task.done(), (
            "cancelling once already resolved the turn -- the re-stall in "
            "_emit_agent_end this test targets never happened"
        )

        # run()'s own finally: flip the flag, THEN reap — see both
        # docstrings for why that order is load-bearing.
        handler._shutting_down = True
        await asyncio.wait_for(handler._cancel_background_tasks(), timeout=5.0)

        assert task.done()

    agent_end_items = [
        item
        for item in list(handler._output_queue._queue)  # type: ignore[attr-defined]
        if item.get("params", {}).get("type") == "agent_end"
    ]
    assert agent_end_items, "agent_end was dropped instead of unwinding uncredited"


# ── P3: extension-requested shutdown ────────────────────────────────────────


def test_agent_session_shutdown_requested_reflects_the_shared_extension_context():
    """`AgentSession.shutdown_requested` reads the ONE `ExtensionContext`
    every bound extension shares — the same object `ctx.abort()`'s signal
    rebind touches (agent_session.py). False until an extension calls
    `ctx.shutdown()`, true and sticky after."""
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
    assert session.shutdown_requested is False
    session._extension_api.context.shutdown()
    assert session.shutdown_requested is True


def test_extension_context_shutdown_is_additive_over_the_legacy_session_manager_path():
    """`shutdown()`'s pre-existing `session_manager.shutdown()` pass-through
    (extension_types.py, predates P3) must still fire — P3 adds a flag, it
    does not replace that call."""
    from unittest.mock import MagicMock

    from tau_agent_core.extension_types import ExtensionContext

    manager = MagicMock()
    ctx = ExtensionContext(session_manager=manager)
    assert ctx.shutdown_requested is False
    ctx.shutdown()
    assert ctx.shutdown_requested is True
    manager.shutdown.assert_called_once()
