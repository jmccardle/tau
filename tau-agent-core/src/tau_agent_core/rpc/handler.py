"""RPCHandler: lifecycle, dispatch, and the verb implementations.

Blocks [3]/[4] (dispatch + handlers) plus lifecycle, per
docs/REMOTE-CONTROL.md section 3/4. Split out of the former rpc.py
(docs/REMOTE-CONTROL.md section 7.3, requirement X1) — the wire envelope
lives in dialect.py, and framing/stdout takeover/signal handling live in
transport.py; `RPCHandler` composes both.

Reference: docs/PHASE-6-SUBPHASE-1.md
Reference: SUBPHASE-0.0.md AgentSession interface
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TextIO

from tau_agent_core.event_projection import MessageDeltaProjector
from tau_agent_core.prompt_cache import PromptCacheObserver
from tau_agent_core.rpc import commands, dialect, transport, wire_events
from tau_llm.docs import agent_facing

if TYPE_CHECKING:
    from tau_agent_core.agent_session import AgentSession
    from tau_agent_core.agent_session_runtime import AgentSessionRuntime
    from tau_agent_core.events import AgentEvent


DEFAULT_OUTPUT_QUEUE_EVENT_BOUND = 64

_EVENT_CREDIT_POLL_INTERVAL_S = 0.1

_BACKGROUND_TASK_GRACE_S = 1.0

_BACKGROUND_TASK_CANCEL_TIMEOUT_S = 5.0

_SHUTDOWN_FLUSH_NO_PROGRESS_TIMEOUT_S = 5.0


class _EventCredits:
    """A bounded counter of "room to enqueue one more event item" (T3/G4).

    Deliberately NOT `asyncio.Semaphore`. That primitive has no public,
    non-suspending "is a credit free right now" check, and the abort-poll
    `_acquire_event_credit` needs (racing the wait against
    `AgentSession.is_aborted` on a short timer) has no way to reach it other
    than `asyncio.wait_for(semaphore.acquire(), timeout=...)` — which wraps
    its argument in a `Task` via `ensure_future` UNCONDITIONALLY, even when
    the semaphore already has room and `acquire()` would otherwise return
    without truly suspending at all. That forces one extra event-loop
    round-trip on EVERY single acquisition, not just a genuinely-stalled
    one — measured during development (a revert-and-observe check on the
    ordering test below false-passed because of it: the artificial delay was
    enough, by itself, to let a since-fixed reordering bug hide). A
    from-scratch counter avoids the whole class of problem: `try_acquire` is
    synchronous, and the ONLY caller that ever sees a real suspension is one
    that actually has to wait.
    """

    def __init__(self, bound: int) -> None:
        self._available = bound
        self._waiters: list[asyncio.Future[None]] = []

    def try_acquire(self) -> bool:
        """Non-suspending: take a credit if one is free right now."""
        if self._available <= 0:
            return False
        self._available -= 1
        return True

    def release(self) -> None:
        """Return a credit, waking the longest-waiting `wait()` caller (if
        any) by handing it directly to them rather than merely incrementing
        the counter for someone to race for — irrelevant for correctness on
        a single-threaded event loop, but keeps FIFO fairness among waiters
        explicit rather than incidental."""
        self._available += 1
        while self._waiters and self._available > 0:
            waiter = self._waiters.pop(0)
            if waiter.cancelled():
                continue
            self._available -= 1
            waiter.set_result(None)
            return

    def add_waiter(self) -> "asyncio.Future[None]":
        """Register for the next `release()`. The caller owns the returned
        `Future` — awaiting it directly (not through `wait_for`, so no
        implicit `Task` wrapping) is `_acquire_event_credit`'s job; on
        cancellation (e.g. abandoning the wait) call `remove_waiter` so a
        credit released later is not handed to a `Future` nobody still
        awaits."""
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        return waiter

    def remove_waiter(self, waiter: "asyncio.Future[None]") -> None:
        with contextlib.suppress(ValueError):
            self._waiters.remove(waiter)


@agent_facing(topic="rpc")
class RPCHandler:
    """JSON-RPC 2.0 server over stdin/stdout.

    Implements the RPC protocol for τ-agent-core. External tools,
    custom UIs, and CI/CD pipelines can connect via this handler.

    Dispatch (block [3]) is table-driven — see `tau_agent_core.rpc.commands
    .COMMAND_TABLE` for the verbs and their tiers/schemas/notes. This class
    owns lifecycle, framing glue, and the JSON-RPC envelope only; it no longer
    hardcodes verb names or behaviour.

    Reference: docs/REMOTE-CONTROL.md §3/§4/§6
    Reference: docs/PHASE-6-SUBPHASE-1.md
    Reference: SUBPHASE-0.0.md AgentSession interface
    """

    def __init__(
        self,
        session: "AgentSession",
        *,
        runtime: "AgentSessionRuntime | None" = None,
        output_queue_event_bound: int = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND,
    ) -> None:
        """Initialize the RPC handler.

        Args:
            session: The AgentSession to manage.
            runtime: The session-lifecycle layer (phase 3, H1) backing
                ``new_session``/``fork``/``switch_session``. Optional —
                ``None`` is a legitimate construction for a handler that
                never needs those three verbs (most of this module's own
                test suite), and every OTHER verb works identically without
                one. A host that calls one of the three verbs against a
                handler built with ``runtime=None`` gets a clear
                ``RuntimeError`` (``commands._require_runtime``), never a
                silent no-op — production wiring (``tau_coding_agent
                .rpc_mode.run_rpc``) always supplies one.
            output_queue_event_bound: T3's bound (see
                ``DEFAULT_OUTPUT_QUEUE_EVENT_BOUND`` module constant for the
                default and its rationale). A constructor param rather than
                a bare module constant so a test can drive the bound with a
                handful of items instead of manufacturing hundreds.
        """
        self._session = session  # type: ignore[assignment]
        self._runtime = runtime
        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._stdin_task = None  # type: asyncio.Task | None
        self._stdout_task = None  # type: asyncio.Task | None
        self._drain_progress = 0
        self._output_queue: asyncio.Queue = asyncio.Queue()
        self._event_credits = _EventCredits(output_queue_event_bound)
        self._running = False
        self._shutting_down = False
        # Real stdout handle for this run (see module-level `_take_over_stdout`).
        self._real_stdout: TextIO | None = None
        self._exit_signal: str | None = None
        self.exit_code: int | None = None
        self._registered_signals: list[signal.Signals] = []
        self._stopped_event = asyncio.Event()
        self._shutdown_signal = asyncio.Event()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self.compaction_in_flight: str | None = None
        self._compaction_aborter: "Callable[[], None] | None" = None
        self._delta_projector = MessageDeltaProjector()
        self._prompt_cache = PromptCacheObserver()
        self._session.subscribe(self._forward_event)

    @property
    def session(self) -> "AgentSession":
        """The `AgentSession` this handler dispatches against — the table
        handlers in `commands.py` reach it through here rather than a private
        attribute."""
        return self._session

    @property
    def runtime(self) -> "AgentSessionRuntime | None":
        """The session-lifecycle layer ``new_session``/``fork``/
        ``switch_session`` dispatch against — ``None`` if this handler was
        constructed without one (see ``__init__``)."""
        return self._runtime

    @property
    def output_is_deliverable(self) -> bool:
        """Whether an item put on `_output_queue` from right now can still
        reach the host (finding 3, Tier B review).

        `False` means the answer is a definite no: `run()` started a writer
        for this handler and that writer has since finished — drained and
        exited on a clean shutdown, cancelled on SIGTERM (P1) or by the
        post-EOF flush deadline, or dead of a broken pipe (T6). Nothing
        else will ever dequeue `_output_queue` again, so a `put_nowait`
        past that point is not a slow delivery, it is a silent drop. A
        caller that has an outcome to report — `commands._handle_compact`'s
        `_complete` is the one today — checks this and says so on stderr
        (T4) instead, exactly as the cancellation arm beside it already
        does: an outcome nobody can read is not an outcome, and Fail Early
        forbids pretending otherwise.

        `True` when `_stdout_task` is None, and that is not a hedge: it
        means no writer was ever started for this handler at all — every
        white-box test in this package's own suite, and any embedded caller
        driving `RPCHandler` without `run()` — in which case
        `_output_queue` belongs to whoever constructed the handler and
        enqueueing is the only correct thing to do. "Not yet started" and
        "already gone" are genuinely different answers, and this property
        must not conflate them; conflating them would silence every
        in-process completion instead of the one that has nowhere to go.

        Deliberately NOT the writer's own loop condition (`_running or the
        queue is non-empty`): between `_running` going False and the writer
        task actually completing, the writer is still draining, and an item
        enqueued in that gap is still written. This property is about the
        state after that, which `Task.done()` is the exact witness for.
        """
        return self._stdout_task is None or not self._stdout_task.done()

    def bind_compaction_aborter(self, cancel: Callable[[], None]) -> None:
        """Make the in-flight compaction reachable by `abort` (finding 5).

        Called by `commands._handle_compact` from inside `_acknowledge`, i.e.
        at the instant the acknowledgement carrying `compaction_id` is
        enqueued. Not earlier, and the timing is load-bearing rather than
        incidental: before that instant the compaction is still waiting on
        D-1's `turn_lock`, the dispatch coroutine is still awaiting its own
        acknowledgement future, and cancelling the task there would resolve
        that future by CANCELLING it — wedging the `compact` request instead
        of answering it. It is also the instant the host first learns the
        compaction exists, so there is nothing it could have aborted before.
        """
        self._compaction_aborter = cancel

    def abort_compaction(self) -> str | None:
        """Deliver `abort`'s signal to the in-flight compaction, if any.

        Returns the `compaction_id` the signal was delivered to, or `None`
        when no compaction was in flight. `commands._handle_abort` puts that
        value on its response so a host knows to expect a `compaction_end`
        with `cancelled: true` for it.

        A SIGNAL, exactly like the `AgentSession.abort()` beside it: this
        returns before the compaction has unwound, and whether it actually
        stopped is reported by that `compaction_end`, never here (see
        `commands.ABORT_RESULT_SCHEMA`'s notes on why this verb reports no
        cursor either — same reason, same shape).

        The aborter is cleared here as well as in
        `commands._handle_compact`'s `finally`, so a second `abort` against
        the same compaction reports `None` rather than claiming a second
        delivery: `abort` stays idempotent, and its answer stays true.
        """
        cancel = self._compaction_aborter
        if cancel is None:
            return None
        compaction_id = self.compaction_in_flight
        self._compaction_aborter = None
        cancel()
        return compaction_id

    def release_compaction(self) -> None:
        """Free the single-flight slot and drop `abort`'s handle on it.

        The one place both halves of the in-flight compaction's state are
        cleared, called from `commands._handle_compact`'s `finally` so the
        slot cannot be freed while `abort` still holds a live handle on a
        task that has already finished.
        """
        self.compaction_in_flight = None
        self._compaction_aborter = None

    def track_background_task(self, task: "asyncio.Task[Any]") -> None:
        """Hold a strong reference to a background task until it finishes.

        Two callers today, both C3's dual completion:
        `commands._submit_and_acknowledge` (the post-admission turn a
        `submit`/`prompt` response has already returned for) and
        `commands._handle_compact` (the summarization call a `compact`
        acknowledgement has already returned for — Blocker 1, Tier B review:
        that call is bounded only by the provider, so running it inline on
        the dispatch path stopped `transport._read_stdin` from PARSING the
        next line, `abort` included, for its whole duration).

        Without this, nothing holds either task alive once the handler
        function's local variable goes out of scope, and asyncio is explicit
        that it may then be garbage-collected before it completes.
        `run()`'s teardown reaps whatever is still tracked here
        (`_cancel_background_tasks`), so a task registered through this
        method can never outlive the process — which is the other half of
        why a background verb must register here rather than fire off a
        bare `create_task`.
        """
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._observe_shutdown_after_background_task)

    def _observe_shutdown_after_background_task(self, task: "asyncio.Task[Any]") -> None:
        """Finding 4 (phase-4 review), P3: wake `_read_stdin` if a just-
        finished background turn leaves `shutdown_requested` True.

        Event-driven, not a poll: this callback runs exactly once per
        background task, the moment asyncio marks it done — it does not run
        on any schedule and does not re-check anything later. It exists
        because the ordinary P3 checkpoint (`transport._read_stdin`, right
        after a dispatched line) only fires for a command dispatched
        SYNCHRONOUSLY through the reader loop; `submit`/`prompt` returns
        their acceptance response at ADMISSION and let the turn run on in
        `task` here (C3), so a hook that calls `ctx.shutdown()` from inside
        that turn (e.g. `api.on("turn_end", ...)`) sets the flag well after
        the reader has already moved on — most often to sit parked in
        `reader.readline()` with no further line to dispatch and nothing
        left to run this check again. Setting `_shutdown_signal` here is
        what `_read_stdin` races that `readline()` against.

        Deliberately does NOT cancel `_stdin_task` directly (the more
        obvious-looking fix): at the moment this callback fires, the reader
        may well be in the middle of dispatching some OTHER, unrelated
        in-flight command (nothing stops a host from sending `get_state`
        while a background turn is still running) — cancelling the task
        outright would inject `CancelledError` into that unrelated
        dispatch. Signalling an `Event` that `_read_stdin` only actually
        races against while genuinely idle in `readline()` has no such
        side effect: if a command IS in flight, the existing after-dispatch
        check already covers this same flag on that command's own next
        pass through the loop.
        """
        if self._session.shutdown_requested:
            self._shutdown_signal.set()

    async def _cancel_background_tasks(self) -> None:
        """Reap every still-running background turn (C3) at shutdown.

        Blocker 1, phase-4 review: `_background_tasks` was previously never
        cancelled or awaited by `run()` — a turn still in flight when the
        host disconnects was simply orphaned, kept alive only by this set's
        strong reference (see `track_background_task`), outliving `run()`
        itself. Called from `run()`'s `finally`, strictly AFTER
        `self._shutting_down` is set — see that flag and `_acquire_event_
        credit`'s docstring for why the order matters.

        Two phases, not one immediate `Task.cancel()`, because the two
        failure modes a stalled turn can be in need OPPOSITE handling:

        1. **Credit-starved** (this fix's own motivating case): the task is
           suspended inside `_acquire_event_credit`, which now polls
           `self._shutting_down` every `_EVENT_CREDIT_POLL_INTERVAL_S` and
           will return `False` — letting `agent_end` still enqueue,
           uncredited, exactly as an abort does — ALL BY ITSELF, with no
           cancellation needed. Sending `Task.cancel()` while it is
           SPECIFICALLY suspended in that wait does not help this case; it
           actively defeats it — cancelling the current `await` raises
           `CancelledError` there instead of letting the escape's own
           synchronous check run, which propagates out of `_forward_event`
           BEFORE the `agent_end` item's `put_nowait` — silently dropping
           it, the exact T3 violation Fail Early forbids. So phase 1 waits,
           without cancelling, for `_BACKGROUND_TASK_GRACE_S` — comfortably
           more than one poll interval — and the ordinary credit-starved
           case is expected to resolve entirely within it.
        2. **Stuck on anything else** (a tool call, a provider request) —
           the ordinary reason cancellation exists, and the reason this
           method still sends it: those tasks do not poll `_shutting_down`
           at all and will run indefinitely without it. Phase 2 only
           reaches tasks still alive after phase 1's grace period, and
           cancels them for real.

        Bounded, not indefinite, either way (Fail Early: no best-effort
        shutdown that quietly gives up). A task still alive after BOTH
        phases means something is wedged that neither escape reaches —
        loud, not swallowed.
        """
        tasks = [t for t in self._background_tasks if not t.done()]
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=_BACKGROUND_TASK_GRACE_S)
        if not pending:
            return
        for task in pending:
            task.cancel()
        _done2, still_pending = await asyncio.wait(
            pending, timeout=_BACKGROUND_TASK_CANCEL_TIMEOUT_S
        )
        if still_pending:
            raise RuntimeError(
                f"{len(still_pending)} background turn task(s) did not unwind within "
                f"{_BACKGROUND_TASK_GRACE_S + _BACKGROUND_TASK_CANCEL_TIMEOUT_S:g}s of "
                "run() teardown cancelling them"
            )

    async def _forward_event(self, event: "AgentEvent") -> None:
        """Forward one `AgentEvent` to the output queue as zero or more
        "event" notifications.

        Bound as the single, permanent `session.subscribe` handler (see
        `__init__`). Routes through `wire_events.project_event` (E1/E2/E4) —
        a `message_update` may project into zero, one, or several wire events
        (see that function's docstring); every other event type projects to
        exactly one.

        This is also the single feed of the handler's `PromptCacheObserver`, and
        the reason it is safe to feed here without naming a prefix: this
        subscription is one conversation's, so a sub-agent's completions never
        reach it. The observer answers only on `agent_end`, where the answer
        becomes `WireEvent.cache_notice`.

        **T3/G4 — this is the backpressure mechanism, not a separate "pace
        hook".** `EventBus.emit` (`events.py`) calls each subscribed handler
        and, when it returns a coroutine, `await`s it before moving to the
        next handler or returning to ITS OWN caller — `AgentLoop._emit`
        (bound to `AgentSession._emit_stamped`), awaited at every emission
        site in `AgentLoop.run`. Making this method `async` and genuinely
        suspending inside it (`_acquire_event_credit`, below) therefore
        suspends that entire call chain, all the way back to the turn's own
        `while` loop — the loop stalls exactly where G4 asks it to, using
        the awaited-handler contract `EventBus`/`AgentLoop` already had
        (docs/REMOTE-CONTROL.md §10, resolved: the bus is NOT fire-and-forget
        in the scheduling sense that open question assumed — see the doc for
        the correction and where "fire-and-forget" DOES apply instead).

        Never uses `put_nowait` on a full queue (the OLD shape, before T3):
        `_output_queue` itself has no capacity limit (see `__init__`), so the
        `put_nowait` calls below cannot raise `QueueFull` — the bound is
        enforced entirely by `_acquire_event_credit` BEFORE each one. This is
        deliberate: a `put_nowait` that could fail here would be caught by
        `EventBus.emit`'s own `try/except` and routed to
        `_surface_handler_error` — printed to stderr and the event silently
        dropped, exactly the Fail-Early violation T3's queue-bounding work
        must not introduce (phase-3 review, finding 1).

        Finding 2 (phase-3 review): an `agent_end` item additionally carries a
        private `_cursor_log` key — a direct reference to `self._session
        .session_log` AS IT IS RIGHT NOW, i.e. the log THIS event's turn
        actually ran against. `_stamp_agent_end_cursor` reads the cursor VALUE
        off that captured object later, at dequeue time (still — see its own
        docstring for why dequeue-time is right for the VALUE), but must no
        longer resolve WHICH log via `self._session.session_log` at that
        point: `new_session`/`fork`/`switch_session` (phase 3) can replace
        that attribute with an unrelated session's log while this item is
        still sitting in the queue, and reading it live at dequeue time then
        stamps the WRONG session's cursor onto an already-emitted, already-
        persisted `agent_end` — see `_stamp_agent_end_cursor` for the full
        argument. `_cursor_log` is popped off (never serialized) before the
        item reaches `json.dumps`; every other event type does not get one
        and `_stamp_agent_end_cursor` is a no-op without it.
        """
        cache_notice = self._prompt_cache.feed_event(event)
        for params in wire_events.project_event(
            self._delta_projector, event, cache_notice=cache_notice
        ):
            item: dict[str, Any] = {
                "jsonrpc": "2.0",
                "method": "event",
                "params": params,
            }
            if params.get("type") == "agent_end":
                item["_cursor_log"] = self._session.session_log
            credited = await self._acquire_event_credit()
            if credited:
                item["_credited"] = True
            self._output_queue.put_nowait(item)

    async def _acquire_event_credit(self) -> bool:
        """Wait for room to enqueue one more event-derived item (T3/G4).

        Returns `True` once a credit is held — the caller must set
        `item["_credited"] = True` so `transport._write_stdout` releases it
        back on dequeue — or `False` if the wait was abandoned because the
        CURRENT turn's abort signal fired (`AgentSession.is_aborted`), or
        because `run()` is tearing down (`self._shutting_down`), while
        genuinely stalled here. `False` is not "give up" — the caller still
        enqueues the item unconditionally (Fail Early: an event is never
        dropped) — it only means this one specific item does not count
        against the bound.

        **Blocker 1, phase-4 review — the `_shutting_down` escape.** Without
        it, a turn stalled here when the host stops reading has no way back:
        `AgentLoop.run`'s `except BaseException` bracket emits `agent_end`
        on its way out (see that method), which re-enters this same wait —
        and the ONE cancellation already delivered to get INTO that bracket
        has already been consumed, so nothing wakes this wait a second time.
        `is_aborted` alone does not cover it: nothing calls `abort()` just
        because the peer went away, and a real subprocess with a
        credit-starved turn that dies this way cannot be killed at all —
        `asyncio.run()`'s shutdown (`asyncio.runners._cancel_all_tasks`)
        joins this exact task forever. `self._shutting_down` is checked
        rather than `not self._running`, deliberately: `_running` is False
        for the lifetime of any `RPCHandler` this method's OWN caller never
        ran through `run()` (most of this module's white-box test suite,
        and the credit-starvation repro this fix was verified against) —
        gating on it would silently disable backpressure for all of them.
        `_shutting_down` instead defaults False everywhere and becomes True
        for exactly one reason: `run()`'s teardown has begun and nothing
        will ever again call `_write_stdout`'s dequeue-time `release()` for
        real (see `_cancel_background_tasks`).

        **Why this needs its own poll loop at all, not just `await self
        ._event_credits.add_waiter()` straight through:** an `AbortSignal`
        is a plain polled flag (`tau_llm.abort.AbortSignal` — no async wakeup
        primitive), checked today only inside `AgentLoop`'s own
        turn-start/per-received-line points (`f1e762e`'s commit message). A
        turn that is stalled INSIDE an `_emit` call — which is exactly what
        a full queue now does to it — is not currently AT any of those
        points, so it would not notice `abort()` at all until a host resumes
        reading and frees a credit. That would make `abort`
        dispatchable-but-ineffective against a genuinely backlogged turn:
        the RPC call succeeds (see `_handle_abort` — a synchronous signal,
        never blocked by this), but the turn producing the backlog never
        actually stops, which is exactly backwards for "a host whose whole
        problem is that τ is producing faster than it can read is precisely
        the host that needs to abort" (docs/REMOTE-CONTROL.md §4[1], the
        abort-reachability constraint). Racing the waiter `Future` against a
        short timer — rather than one unbounded wait — gives THIS wait its
        own abort checkpoint, so the aborting turn's last few items
        (`turn_end`, `agent_end`) can still reach the queue and let it wind
        down, instead of staying wedged until a host that has already given
        up on reading resumes doing so. The process-boundary guarantee
        (SIGTERM, G5, P1/P2) is unconditional and does not depend on any of
        this; this is what makes the SOFTER, in-band `abort` verb actually
        work too, not just be reachable.

        Checks `is_aborted` BEFORE the first wait, not only on a timeout —
        an already-aborted turn's remaining items should not each pay one
        poll interval of latency winding down. Checks `try_acquire()` first
        too, so the ordinary (uncontended) case never touches the waiter
        machinery or a timer at all — see `_EventCredits`'s own docstring
        for why that matters beyond tidiness.
        """
        if self._session.is_aborted or self._shutting_down:
            return False
        if self._event_credits.try_acquire():
            return True
        waiter = self._event_credits.add_waiter()
        try:
            while True:
                done, _pending = await asyncio.wait({waiter}, timeout=_EVENT_CREDIT_POLL_INTERVAL_S)
                if waiter in done:
                    return True
                if self._session.is_aborted or self._shutting_down:
                    return False
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                self._event_credits.release()
            raise
        finally:
            if not waiter.done():
                self._event_credits.remove_waiter(waiter)

    def prepare_outbound(self, item: dict[str, Any]) -> None:
        """Last-moment fixups on a queued item, applied just before it is framed.

        Called by the writer (`transport._write_stdout`) at DEQUEUE time. The
        transport calls it without knowing what it does — block [1] frames and
        orders bytes, it does not know an `agent_end` from a `turn_start`
        (§7.3 X1's separation, which a socket transport would otherwise have
        to carry). Everything wire-shaped lives on this side of that line.

        Today it does exactly one thing: E5's cursor (see below).
        """
        self._stamp_agent_end_cursor(item)

    def _stamp_agent_end_cursor(self, item: dict[str, Any]) -> None:
        """Stamp the CAPTURED log's current cursor onto an outbound `agent_end`
        (E5/F3).

        WHY THE CURSOR *VALUE* IS READ AT DEQUEUE TIME rather than when the
        event is projected: `agent_end` is emitted by `AgentLoop
        ._emit_agent_end`, which runs strictly BEFORE `AgentSession
        ._run_one_turn` persists the turn (`_persist_loop_messages` runs only
        after `await loop.run(...)` returns). A cursor read at projection time
        is therefore always the PRE-persistence tip — the exact stale-tip
        failure F3 exists to prevent, and the one `abort`'s old `cursor` field
        had.

        WHY THAT READ IS ORDERED, not a race: between the `put_nowait` in
        `_forward_event` and `_persist_loop_messages` there is no `await`, so
        this coroutine cannot be suspended in that window and the writer task
        cannot dequeue until persistence has already happened. That is a real
        guarantee, but an UNSTATED one — it lives in `agent_session.py`, not
        here, and an `await` introduced between those two points would silently
        reintroduce the stale cursor. `test_agent_end_wire_event_carries_the_
        post_persistence_cursor` pins it: it asserts the cursor on the wire is
        the post-turn tip, and fails if that ordering is ever broken.

        STILL TRUE UNDER T3's BACKPRESSURE, and worth spelling out because
        T3 makes a backlogged writer routine rather than rare (phase-4
        scope): `_forward_event` now awaits `_acquire_event_credit()` BEFORE
        the `put_nowait` this paragraph is about, and that wait CAN
        genuinely suspend (a full queue). The item is provably still not
        visible to the writer at that point, though — `put_nowait` has not
        run yet, so there is nothing IN the queue for the writer to dequeue
        early no matter how long the wait takes or how many other already-
        queued items the writer drains meanwhile. Once the wait resolves,
        `put_nowait` and everything after it up through `_persist_loop_
        messages` is exactly the same unbroken, non-suspending stretch this
        paragraph already described — the ONE await T3 adds sits strictly
        BEFORE the item exists in the queue, never between its existing and
        persistence. `test_agent_end_wire_event_carries_the_post_persistence
        _cursor_under_backpressure` drives this with the credit pool
        actually exhausted and pins it the same way.

        WHY THE LOG *OBJECT* IS NOT RE-RESOLVED HERE (Finding 2, phase-3
        review): the paragraphs above establish that dequeue time is the
        right moment to read `.cursor`'s VALUE, but they say nothing about
        WHICH log to read it from — the original code read `self._session
        .session_log`, live, at that same dequeue-time. Phase 3 added
        `new_session`/`fork`/`switch_session`, which can REPLACE that
        attribute with an unrelated session's (fresh, likely near-empty) log
        while this `agent_end` item is still sitting in the output queue (a
        slow/unresponsive peer, or simply a swap landing in the same tick a
        turn finishes) — at that point `self._session.session_log` is no
        longer the log this turn ran against, and stamping ITS cursor is
        exactly the wrong-session failure E5/F3 exist to prevent, just
        arrived at via a different route than the pre-persistence one two
        paragraphs up. `_forward_event` closes over the correct log at THE
        MOMENT THIS EVENT WAS ACTUALLY EMITTED (`item["_cursor_log"]`, popped
        here) — before persistence, but still the SAME object persistence
        writes into, and immune to any swap that happens after. Reading from
        it keeps BOTH properties at once: post-persistence (still a dequeue-
        time VALUE read) and from the right log (fixed at enqueue time,
        immune to any swap that follows). A `params`-less or non-`agent_end`
        item never gets `_cursor_log` set (`_forward_event`) and is a no-op
        here, same as before.
        """
        log = item.pop("_cursor_log", None)
        if log is None:
            return
        params = item.get("params")
        if isinstance(params, dict) and params.get("type") == "agent_end":
            params["cursor"] = log.cursor

    async def run(self) -> None:
        """Run the RPC server until stdin closes or a shutdown signal fires.

        Reads JSON-RPC requests from stdin and writes responses/events to
        stdout (see module docstring for framing). Claims stdout exclusively
        for the duration of the call (T2) and installs SIGTERM/SIGHUP
        handlers (P1) so a host can request shutdown out-of-band.

        Setup (stdout takeover, signal registration, task creation) lives
        inside the outer `try` specifically so the `finally` still runs —
        stdout gets released and `_running`/`_stopped_event` still get
        reset — even if a setup step itself raises (e.g. signal
        registration failing on a platform where it was expected to work,
        see `_register_signal_handlers`). Without that, a mid-setup
        exception would wedge the handler: stdout hijacked forever, and
        every later `run()` call raising "already running".

        Shutdown ordering is deliberate, and easy to get backwards:

        - We race the reader and writer with `asyncio.wait(...,
          return_when=FIRST_COMPLETED)` instead of gathering them
          unconditionally. Under normal operation the writer never
          finishes before the reader — its loop condition is "`_running`
          is still True, or the queue still has items", and only the
          reader ever flips `_running` off — so this reduces to "wait for
          the reader" exactly as before, avoiding the deadlock a blind
          `gather()` would cause (the writer blocked on `queue.get()`
          forever after EOF, because nothing else ever ends its loop).
          What the race adds: if the WRITER finishes first, that can only
          mean it raised (T6: a broken pipe — the peer is gone). In that
          case there is nothing left to read for, so we cancel the reader
          too instead of continuing to parse, execute, and queue requests
          into a sink nobody drains, then re-raise the write failure.
        - Once the reader is done normally, the background tasks (C3's
          post-acknowledgement turns and compactions) are reaped BEFORE the
          writer is drained, not after — finding 3, Tier B review. Reaping
          them last meant anything they completed with during
          `_cancel_background_tasks`'s no-cancel grace period was enqueued
          onto a queue whose writer had already exited: silently lost, on a
          run that reported rc 0 and an empty stderr. See the call site for
          the measured case (a `compact` whose result was durably written
          and never announced) and for why the `finally`'s reap stays.
        - Then we let the writer drain whatever is queued (P4: a clean
          shutdown flushes pending output) — unless the shutdown reason was
          SIGTERM, in which case
          we cancel the writer outright instead: SIGTERM means the host is
          already impatient and does not want to wait on us (P1). That
          drain is bounded by a no-progress deadline
          (`_flush_stdout_with_deadline`), because EOF says only that the
          host closed the end it writes to, not that it is still reading —
          and an unbounded wait on a peer that stopped reading is a τ
          process no clean-shutdown trigger can end.
        - A `CancelledError` raised at the `asyncio.wait(...)` call itself
          (as opposed to one carried by the reader/writer tasks it's
          waiting on) means `run()`'s own task was cancelled from the
          outside — e.g. a supervising `TaskGroup` or `gather()` tearing
          down — not `_on_signal`/`stop()` cancelling the reader
          internally. Those two must not be conflated: swallowing this one
          the same way as the internal case would report a normal, clean
          completion for a task that was actually cancelled, the standard
          asyncio footgun. So it stops both tasks and re-raises instead.
        - Cleanup (removing signal handlers, releasing the stdout claim) is
          unconditional — it runs whether the reader/writer finished
          cleanly, were cancelled, or raised — via the outer `finally`. A
          write failure (T6: broken pipe) is deliberately NOT caught here;
          it propagates out of `run()` after cleanup still runs.
        """
        if self._running:
            raise RuntimeError("RPCHandler.run() is already running")
        try:
            self._running = True
            self._shutting_down = False
            self._exit_signal = None
            self.exit_code = None
            self._stopped_event.clear()
            self._real_stdout = transport._take_over_stdout()
            self._register_signal_handlers()
            self._stdin_task = asyncio.create_task(self._read_stdin())
            self._stdout_task = asyncio.create_task(self._write_stdout())

            try:
                done, _pending = await asyncio.wait(
                    {self._stdin_task, self._stdout_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                self._stdin_task.cancel()
                self._stdout_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._stdin_task
                with contextlib.suppress(asyncio.CancelledError):
                    await self._stdout_task
                raise

            if self._stdin_task not in done:
                self._stdin_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._stdin_task
                self._running = False
                await self._stdout_task  # re-raises the write failure (T6)
                return

            with contextlib.suppress(asyncio.CancelledError):
                await self._stdin_task

            self._shutting_down = True
            await self._cancel_background_tasks()

            self._running = False
            if self._exit_signal == "SIGTERM":
                self._stdout_task.cancel()
                pending = self._output_queue.qsize()
                if pending:
                    print(
                        f"tau rpc: SIGTERM discarded {pending} queued item(s) "
                        "without writing them (P1: SIGTERM skips the flush). "
                        "The protocol stream is truncated.",
                        file=sys.stderr,
                        flush=True,
                    )
            else:
                await self._flush_stdout_with_deadline()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stdout_task
        finally:
            self._running = False
            self._shutting_down = True
            await self._cancel_background_tasks()
            self._unregister_signal_handlers()
            transport._release_stdout()
            self._stopped_event.set()

    async def _flush_stdout_with_deadline(self) -> None:
        """Let the writer flush its backlog after EOF (P4) — but not forever.

        Called from `run()` once the reader has ended for any reason other
        than SIGTERM (which means the host is already impatient and cancels
        the writer outright instead). `_running` is False by now, so
        `_write_stdout`'s loop condition reduces to "the queue is non-empty":
        it will dequeue, write and drain every remaining item and then exit on
        its own. That is P4's "a clean shutdown flushes pending output", and
        for a host that is still reading it is exactly right.

        The failure it guards is a host that closes stdin and stops reading.
        EOF only reports that the host closed the end it WRITES to; nothing in
        it says the host is still draining stdout. With a backlog queued, the
        writer parks in `await writer.drain()` on a full pipe and `run()`'s
        `await self._stdout_task` waits on that forever — a τ process that
        cannot be shut down by the documented clean-shutdown trigger, the same
        class as phase-4's two merge blockers (SIGTERM/SIGHUP against a
        non-reading peer) reached by a third route. R-T3's subprocess test
        measured it at >15s and a forced kill, once that test was fixed to
        build a real backlog first (phase-4 review, finding 3).

        The deadline is on lack of PROGRESS, not on elapsed flush time: each
        drained item bumps `_drain_progress`, and only a whole
        `_SHUTDOWN_FLUSH_NO_PROGRESS_TIMEOUT_S` window in which that counter
        does not move ends the flush. A host reading slowly, or one with a
        genuinely large backlog to collect, is never cut off for being slow —
        only for being absent. Giving up is announced on stderr (T4's log
        channel), because a truncated protocol stream the host cannot explain
        is worse than a noisy one it can.

        Returns without re-raising: the caller does the `await
        self._stdout_task` that surfaces a write failure (T6) or absorbs the
        `CancelledError` this method may have caused.
        """
        assert self._stdout_task is not None
        while True:
            marker = self._drain_progress
            done, _pending = await asyncio.wait(
                {self._stdout_task}, timeout=_SHUTDOWN_FLUSH_NO_PROGRESS_TIMEOUT_S
            )
            if done:
                return
            if self._drain_progress != marker:
                continue
            print(
                "tau rpc: giving up on flushing "
                f"{self._output_queue.qsize()} queued item(s) after EOF -- "
                f"the peer accepted nothing for {_SHUTDOWN_FLUSH_NO_PROGRESS_TIMEOUT_S}s. "
                "Exiting anyway; the protocol stream is truncated.",
                file=sys.stderr,
                flush=True,
            )
            self._stdout_task.cancel()
            return

    async def stop(self) -> None:
        """Request a clean (non-signal) shutdown, and wait for it to finish.

        Cancels the stdin reader; `run()`'s own shutdown path (drain the
        writer, remove signal handlers, release stdout) takes it from
        there — identical to what happens on stdin EOF. A no-op if `run()`
        was never started.
        """
        if self._stdin_task is None:
            return
        if not self._stdin_task.done():
            self._stdin_task.cancel()
        await self._stopped_event.wait()

    _register_signal_handlers = transport._register_signal_handlers
    _unregister_signal_handlers = transport._unregister_signal_handlers
    _on_signal = transport._on_signal
    _read_stdin = transport._read_stdin

    async def _handle_line(self, line: str) -> None:
        """Parse and dispatch one LF-delimited JSON-RPC line."""
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            await self._send_error(None, dialect.PARSE_ERROR, f"Parse error: {e}")
            return
        await self._handle_request(request)

    _connect_stdout_writer = transport._connect_stdout_writer
    _write_stdout = transport._write_stdout
    _write_line = transport._write_line

    async def _handle_request(self, request: dict) -> None:
        """Route a JSON-RPC request through `commands.COMMAND_TABLE` (block [3]).

        C2: every failure mode gets a distinct standard JSON-RPC code — a
        malformed request (`INVALID_REQUEST`), a method the table does not
        have or has declined (`METHOD_NOT_FOUND`), params that fail the
        method's `params_schema` (`INVALID_PARAMS`), a handler that raised
        something it did not raise on purpose (`INTERNAL_ERROR`), or a
        handler that raised `commands.RPCError` on purpose (its own code —
        today, exactly one case: `SUBMISSION_REJECTED`, C3). Never a bare
        string in a generic `-32603`, and never silence.

        D2: every success response echoes the method it answered as
        `result["method"]`.

        Args:
            request: The parsed JSON-RPC request dict.
        """
        is_dict_request = isinstance(request, dict)
        method_field = request.get("method") if is_dict_request else None
        if not is_dict_request or not isinstance(method_field, str) or not method_field:
            msg_id = request.get("id") if is_dict_request else None
            await self._send_error(
                msg_id,
                dialect.INVALID_REQUEST,
                "Invalid Request: 'method' must be a non-empty string",
            )
            return

        method: str = request["method"]
        msg_id = request.get("id")
        params = request.get("params") or {}

        entry = commands.COMMAND_TABLE.get(method)
        if entry is None or entry.handler is None:
            reason = f" (declined: {entry.declined_because})" if entry is not None else ""
            await self._send_error(
                msg_id,
                dialect.METHOD_NOT_FOUND,
                f"Method not found: {method}{reason}",
                data={"method": method},
            )
            return

        violation = commands.validate_params(entry.params_schema, params)
        if violation is not None:
            await self._send_error(
                msg_id,
                dialect.INVALID_PARAMS,
                f"Invalid params for {method!r}: {violation}",
                data={"method": method},
            )
            return

        try:
            result = await entry.handler(self, msg_id, params)
        except commands.RPCError as exc:
            await self._send_error(
                msg_id, exc.code, exc.message, data={"method": method, **(exc.data or {})}
            )
            return
        except Exception as e:  # noqa: BLE001 - a handler's own failure, never a crash
            await self._send_error(msg_id, dialect.INTERNAL_ERROR, str(e), data={"method": method})
            return

        if result is None:
            return
        await self._send_response(msg_id, {**result, "method": method})

    async def _send_response(self, id: int | None, result: dict) -> None:
        """Send a JSON-RPC success response.

        Args:
            id: The request ID to match.
            result: The response result dict.
        """
        await self._output_queue.put(
            {
                "jsonrpc": "2.0",
                "id": id,
                "result": result,
            }
        )

    async def _send_error(
        self, id: int | None, code: int, message: str, data: dict[str, Any] | None = None
    ) -> None:
        """Send a JSON-RPC error response.

        Args:
            id: The request ID to match, or None for notifications / unparseable lines.
            code: A JSON-RPC error code — see `tau_agent_core.rpc.dialect` for the
                standard set (C2) plus `SUBMISSION_REJECTED`.
            message: A human-readable error message.
            data: Optional structured detail (C2/D2: always includes `method`
                where a method was identified at all).
        """
        error: dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        await self._output_queue.put(
            {
                "jsonrpc": "2.0",
                "id": id,
                "error": error,
            }
        )
