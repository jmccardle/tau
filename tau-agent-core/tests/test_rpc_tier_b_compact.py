"""B2 — RPC Tier B verb `compact` (docs/RPC-TIER-B.md §3, table row B2),
as reshaped by Blocker 1 of the Tier B review into C3's dual completion.

A new file per unit (docs/RPC-TIER-B.md §3 "B1-B6": "a new test file, so no
unit contends on a test file with any other"). Covers, against the real
handler function (``commands._handle_compact``), not a hand-simulated
re-implementation of it:

- the params/result schemas pass ``CommandEntry.__post_init__``'s import-time
  vocabulary check (already proven by ``commands.py`` importing at all — see
  ``test_compact_registered_in_command_table_as_tier_b`` for the direct
  assertion) and ``validate_params`` behaves on both;
- C3: the RESPONSE is an acknowledgement ({accepted, compaction_id}), and the
  dispatch coroutine returns while the compaction is still running — the
  whole point of Blocker 1 (the unbounded provider call no longer sits on
  ``transport._read_stdin``'s single serial reader). The subprocess proof
  that a pipelined ``abort`` really is answered meanwhile lives in
  ``tau-coding-agent/tests/test_rpc_conformance.py``; what THIS file can
  prove in-process is the property that makes it possible;
- the ``compaction_end`` notification: correlation (compaction_id +
  request_id), ``performed=false`` — ``AgentSession.compact()`` returning
  ``None`` (an empty conversation) reported as a real outcome, with
  ``cursor`` present and unchanged; ``performed=true`` — a real compaction
  (mocked LLM, same pattern ``test_compaction_engine.py`` uses) with the full
  result plus the NEW cursor (E5); and ``is_error`` for a compaction that
  raised, where ``performed`` is absent rather than fabricated as ``false``;
- every notification this file produces validated against the published
  ``COMPACTION_END_PARAMS_SCHEMA``;
- ``custom_instructions`` threading, at the dispatch layer (no LLM needed);
- the `details is None` defensive branch, which the real compaction pipeline
  never actually produces (``compaction.py``'s one `CompactionResult(...)`
  call site always fills `details`) but the field's own type
  (`CompactionDetails | None`) allows, so it is exercised directly rather
  than left as an article of faith;
- D-1: the handler genuinely holds ``turn_lock`` for the duration of the
  ``AgentSession.compact()`` call (not just "some context manager runs"), and
  a refusal from the shared guard still reaches the CALLER as `RPCError`
  even though the guard is now taken inside the background task;
- single-flight: a second ``compact`` while one is in flight is refused
  immediately with TURN_STILL_RUNNING (D-1's vocabulary), without waiting out
  the guard it would otherwise be stuck behind for the whole provider call;
- shutdown: the background task is tracked, so ``RPCHandler
  ._cancel_background_tasks`` (phase-4's unkillable-process defence) reaps it;
- shutdown, the completion contract (findings 3 and 4 of the Tier B review,
  D-5 as amended): a compaction reports its outcome exactly ONE of three ways
  and never none of them — ``compaction_end`` on the wire, the whole outcome
  on stderr when the writer is provably gone, or the cancellation line on
  stderr when it was reaped before finishing. All three are pinned here,
  including the two claims that previously survived mutation (the cancel arm
  emits NO notification, and it DOES print);
- finding 5: a host's ``abort`` REACHES this compaction — it did not before,
  and said otherwise. The task is cancelled, ``abort``'s own response names
  the ``compaction_id``, and the ``compaction_end`` that follows carries
  ``cancelled: true`` with no ``performed``. Distinct from the shutdown reap
  above, which still reports on stderr and emits nothing;
- finding 6 / D-7: this verb APPENDS a ``compaction`` entry, so it refuses an
  unpersisted session (``require_durable_session``) exactly as ``set_model``
  and ``set_session_name`` do — and ``set_auto_compaction``, which appends
  nothing, still answers on that same session. Both halves are pinned, so
  "restoring consistency" in either direction fails here.

Reference: docs/RPC-TIER-B.md §2 D-1, D-5 (incl. the finding-3 amendment) and
D-7, §3 B2 table row, §6; docs/REMOTE-CONTROL.md §4[1] T3/T4/T6, §4[3] C3,
P1/P4.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionDetails, CompactionResult, CompactionSettings
from tau_agent_core.rpc import RPCHandler
from tau_agent_core.rpc import commands
from tau_agent_core.rpc.commands import RPCError
from tau_agent_core.rpc.dialect import SESSION_NOT_PERSISTED, TURN_STILL_RUNNING
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model


def _model(context_window: int = 128000) -> Model:
    return Model(
        id="m",
        name="m",
        api="openai-completions",
        provider="openai",
        base_url="http://127.0.0.1:1/v1",
        context_window=context_window,
        max_tokens=4096,
    )


class _PersistedLog(InMemorySessionLog):
    """An ``InMemorySessionLog`` that DECLARES a durable location.

    Finding 6 / D-7: ``compact`` appends a ``compaction`` entry, so it now
    takes ``require_durable_session`` and refuses a session whose log
    declares no location — which a bare ``InMemorySessionLog`` is (it has
    neither ``path`` nor ``root_doc_id``; ``test_rpc_tier_b_scaffolding.py``
    pins exactly that). Every test in this file that exercises the verb's
    SUCCESS path therefore needs a log that answers the durability question
    the way a real file-backed ``Session`` does, which is the same reason —
    and the same shape — ``test_rpc_tier_b_set_model.py``'s
    ``_LogWithModelChange`` carries a ``path``.

    The path is never opened: ``require_durable_session`` asks what the log
    DECLARES, not what exists on disk, and the entries still live in memory
    where this file's assertions can read them.

    The refusal side is exercised against the un-subclassed
    ``InMemorySessionLog`` and against a ``path=None`` log, in the D-7
    section below.
    """

    path = Path("/tmp/does-not-need-to-exist.jsonl")


def _msg(role: str, text: str, **extra: object) -> dict:
    """A bare message dict for InMemorySessionLog.append_message (mirrors
    test_compaction_engine.py's local helper of the same name — no cross-
    test-file import, per test_rpc_tier_b_scaffolding.py's stated precedent)."""
    msg: dict = {"role": role, "content": [{"type": "text", "text": text}]}
    msg.update(extra)
    return msg


def _fake_complete_simple(text: str):
    """A monkeypatch replacement for compaction.complete_simple that succeeds
    with a fixed summary (mirrors test_compaction_engine.py's `_fake_complete`,
    trimmed to what this file needs)."""
    from tau_llm.types import AssistantMessage, TextContent, Usage

    async def _impl(model, context, options=None):
        return AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model="m",
            stop_reason="stop",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            timestamp=0,
        )

    return _impl


@pytest.fixture
def empty_session() -> AgentSession:
    """No messages at all — AgentSession.compact() returns None with no LLM
    call, the same "no product bug, this is the documented no-op" shape
    test_agent_session.py's TestCompact.test_compact_completes_without_error
    and test_compaction_engine.py's test_compact_is_a_noop_on_an_empty_session
    already cover at the AgentSession layer; here it is exercised through the
    RPC handler."""
    return AgentSession(session_log=_PersistedLog(), model=_model(), tools=[])


def _multi_turn_session(settings: CompactionSettings) -> AgentSession:
    """Three real log entries (two turns) — enough for `prepare_compaction`
    to find a cut point once `keep_recent_tokens` forces one (same recipe as
    test_compaction_engine.py's module-local `_session`)."""
    log = _PersistedLog()
    log.append_message(_msg("user", "old question"))
    log.append_message(_msg("assistant", "old answer", stop_reason="stop"))
    log.append_message(_msg("user", "current"))
    return AgentSession(
        session_log=log, model=_model(), api_key="sk-test", compaction_settings=settings
    )


@pytest.fixture
def empty_handler(empty_session: AgentSession) -> RPCHandler:
    return RPCHandler(empty_session)


# ── driving the dual completion ───────────────────────────────────────────
#
# Every behavioural test below goes through these two helpers rather than
# reading `_output_queue` inline, because C3 splits one call into two
# observable things (a response, then a notification) and a test that
# forgets to await the background half would pass vacuously.


def _drain_queue(handler: RPCHandler) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while not handler._output_queue.empty():
        items.append(handler._output_queue.get_nowait())
    return items


async def _settle(handler: RPCHandler) -> None:
    """Wait for the handler's background compaction task to finish."""
    tasks = list(handler._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    # Let asyncio run the done-callbacks that reap `_background_tasks`.
    await asyncio.sleep(0)


async def _settle_cancelled(handler: RPCHandler) -> None:
    """Wait for a background compaction that ends by CANCELLATION.

    Separate from `_settle` because `asyncio.gather` on a cancelled task
    re-raises the `CancelledError` into this test coroutine, which pytest-
    asyncio reports as an errored test rather than the cancellation the test
    is asserting.

    The `wait_for` is what makes "abort did not cancel it" EXPRESSIBLE as a
    failure: every caller below gates its compaction on an event nobody ever
    sets, so a handler that stopped cancelling would hang the run forever
    instead of failing it (finding 5's own mutation target)."""
    tasks = list(handler._background_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5.0)
    await asyncio.sleep(0)


async def _compact(
    handler: RPCHandler, params: dict[str, Any] | None = None, msg_id: int | None = 7
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Dispatch `compact`, let the background compaction finish, and return
    ``(acknowledgement_result, compaction_end_params)``.

    Asserts the C3 shape on the way through: the handler itself returns
    `None` (it sent its own response), exactly one response and exactly one
    `compaction_end` notification are enqueued, and the notification
    validates against the published schema.
    """
    assert await commands._handle_compact(handler, msg_id, params or {}) is None
    await _settle(handler)
    items = _drain_queue(handler)
    responses = [i for i in items if "id" in i]
    notifications = [i for i in items if i.get("method") == commands.COMPACTION_END_METHOD]
    assert len(responses) == 1, f"expected exactly one response, got {items}"
    assert len(notifications) == 1, f"expected exactly one compaction_end, got {items}"
    assert responses[0]["id"] == msg_id
    params_out = notifications[0]["params"]
    violation = commands.validate_params(commands.COMPACTION_END_PARAMS_SCHEMA, params_out)
    assert violation is None, f"compaction_end violates its own schema: {violation}"
    return responses[0]["result"], params_out


def _gated_compact(started: asyncio.Event, gate: asyncio.Event, result: Any = None):
    """A `session.compact` double that parks until `gate` is set — the
    in-process stand-in for the unbounded provider call Blocker 1 is about."""

    async def _impl(custom_instructions: str | None = None) -> Any:
        started.set()
        await gate.wait()
        return result

    return _impl


# ── schema ───────────────────────────────────────────────────────────────


def test_compact_registered_in_command_table_as_tier_b() -> None:
    entry = commands.COMMAND_TABLE["compact"]
    assert entry.tier == "B"
    assert entry.handler is not None
    assert entry.declined_because is None
    assert entry.result_schema is not None


def test_compact_params_schema_accepts_empty_and_custom_instructions() -> None:
    assert commands.validate_params(commands.COMPACT_PARAMS_SCHEMA, {}) is None
    assert (
        commands.validate_params(
            commands.COMPACT_PARAMS_SCHEMA, {"custom_instructions": "focus on the bug"}
        )
        is None
    )


def test_compact_params_schema_rejects_wrong_type_and_unknown_keys() -> None:
    violation = commands.validate_params(commands.COMPACT_PARAMS_SCHEMA, {"custom_instructions": 5})
    assert violation is not None and "custom_instructions" in violation

    violation = commands.validate_params(commands.COMPACT_PARAMS_SCHEMA, {"bogus": "x"})
    assert violation is not None and "bogus" in violation


def test_compact_result_schema_is_the_acknowledgement_not_the_outcome() -> None:
    """C3: what `compact` RETURNS is an ack; the outcome has its own schema.
    Pins the two apart so a future edit cannot quietly move a
    CompactionResult field back onto the response without this failing."""
    result_schema = commands.COMMAND_TABLE["compact"].result_schema
    assert result_schema is not None
    assert set(result_schema["properties"]) == {"accepted", "compaction_id"}
    assert result_schema["required"] == ["accepted", "compaction_id"]
    assert "performed" in commands.COMPACTION_END_PARAMS_SCHEMA["properties"]
    assert "summary" in commands.COMPACTION_END_PARAMS_SCHEMA["properties"]


# ── C3: the response comes back while the compaction is still running ──────


async def test_compact_acknowledges_before_the_compaction_finishes(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 1, at the level this in-process file can reach it: the
    dispatch coroutine RETURNS — freeing `transport._read_stdin` to parse the
    next line — while `AgentSession.compact()` is still parked inside the
    provider call.

    MUTATION TARGET: move `_acknowledge()` in `_drive` to AFTER `await
    session.compact(...)` (or restore the pre-Blocker-1 inline shape
    entirely) and this goes red — `asyncio.TimeoutError`, because the
    dispatch call cannot return until `gate` is set, which happens well after
    the budget below. The `wait_for` is what makes that mutation EXPRESSIBLE
    as a failure rather than a hung test run."""
    started = asyncio.Event()
    gate = asyncio.Event()
    monkeypatch.setattr(empty_session, "compact", _gated_compact(started, gate))

    assert await asyncio.wait_for(commands._handle_compact(empty_handler, 3, {}), 2.0) is None

    # Acknowledged already, and the compaction genuinely has not finished.
    await asyncio.wait_for(started.wait(), timeout=1.0)
    ack = _drain_queue(empty_handler)
    assert len(ack) == 1
    assert ack[0]["id"] == 3
    assert ack[0]["result"]["accepted"] is True
    assert isinstance(ack[0]["result"]["compaction_id"], str)
    assert ack[0]["result"]["method"] == "compact"
    assert empty_handler.compaction_in_flight == ack[0]["result"]["compaction_id"]

    gate.set()
    await _settle(empty_handler)
    assert empty_handler.compaction_in_flight is None
    end = _drain_queue(empty_handler)
    assert len(end) == 1
    assert end[0]["method"] == commands.COMPACTION_END_METHOD
    assert end[0]["params"]["compaction_id"] == ack[0]["result"]["compaction_id"]
    assert end[0]["params"]["request_id"] == 3


async def test_compaction_end_correlates_to_the_request_that_started_it(
    empty_session: AgentSession, empty_handler: RPCHandler
) -> None:
    """MUTATION TARGET: drop `"request_id": msg_id` from `_complete`'s
    payload and this goes red on the schema check inside `_compact`
    (request_id is `required`) before it even reaches the assertions."""
    ack, end = await _compact(empty_handler, msg_id=42)
    assert end["compaction_id"] == ack["compaction_id"]
    assert end["request_id"] == 42


# ── performed=False (the None outcome, D-3-sibling honesty) ────────────────


async def test_compact_on_empty_session_reports_performed_false(
    empty_session: AgentSession, empty_handler: RPCHandler
) -> None:
    cursor_before = empty_session.session_log.cursor
    _ack, end = await _compact(empty_handler)
    assert end["performed"] is False
    assert end["cursor"] == cursor_before
    assert end["is_error"] is False
    assert end["error"] is None
    # No CompactionResult field leaks into the false shape.
    assert set(end) == {
        "compaction_id",
        "request_id",
        "is_error",
        "error",
        "cancelled",
        "performed",
        "cursor",
    }


async def test_compact_under_the_shipped_settings_reports_performed_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DEFAULT-settings path of this verb, which neither layer covered: B2's
    performed=true test forces a cut with ``keep_recent_tokens=1`` and B7's
    conformance test used an empty session, so nothing exercised what a host
    actually gets when it calls `compact` on an ordinary conversation.

    With the shipped ``keep_recent_tokens`` (20000) the cut on a three-entry
    session keeps everything, so the honest answer is performed=false: no
    completion spent, no entry written, the cursor unmoved. Before the
    compaction.py fix this branch published performed=true with
    ``tokens_saved`` ≈ ``tokens_before`` (measured over the wire: 4953 of 5003)
    while every message stayed in the context.

    MUTATION TARGET: delete ``prepare_compaction``'s both-lists-empty guard —
    `_boom` fires (a completion is spent) and the run errors inside the
    background task rather than reporting performed=false.
    """

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("a compaction that removes nothing must not spend a completion")

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _boom)
    session = _multi_turn_session(CompactionSettings())  # the SHIPPED settings
    handler = RPCHandler(session)
    cursor_before = session.session_log.cursor
    entries_before = list(session.session_log.entries())

    _ack, end = await _compact(handler)

    assert end["is_error"] is False, end["error"]
    assert end["performed"] is False
    assert "tokens_saved" not in end
    assert end["cursor"] == cursor_before
    assert session.session_log.entries() == entries_before


# ── performed=True (real compaction, mocked LLM) ────────────────────────────


async def test_compact_performed_true_reports_full_result_and_new_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple", _fake_complete_simple("## Goal\nrecap")
    )
    session = _multi_turn_session(CompactionSettings(keep_recent_tokens=1))
    handler = RPCHandler(session)
    cursor_before = session.session_log.cursor

    _ack, end = await _compact(handler, {"custom_instructions": "be terse"})

    assert end["is_error"] is False
    assert end["performed"] is True
    assert "recap" in end["summary"]
    assert isinstance(end["tokens_before"], int) and end["tokens_before"] > 0
    assert isinstance(end["tokens_saved"], int)
    assert isinstance(end["compacted_entry_ids"], list) and end["compacted_entry_ids"]
    assert end["read_files"] == []
    assert end["modified_files"] == []
    assert isinstance(end["usage"], dict)
    assert end["cursor"] != cursor_before
    assert end["cursor"] == session.session_log.cursor
    # The mutation actually landed on the session (not just reported).
    assert any(
        "[[Compaction summary:" in m["content"][0]["text"]
        for m in session.messages
        if m.get("content")
    )


# ── a compaction that raises ───────────────────────────────────────────────


async def test_compact_that_raises_reports_is_error_and_omits_performed(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-Early on the wire: a CompactionError (summary generation failed,
    nothing written) is REPORTED, not dropped, and `performed` is absent
    rather than fabricated as `false` — "it raised" and "there was nothing to
    compact" are different outcomes.

    MUTATION TARGET: add `"performed": False` to `_complete`'s is_error
    payload and the `"performed" not in end` assertion goes red."""

    async def _boom(custom_instructions: str | None = None) -> CompactionResult:
        raise RuntimeError("summarizer said no")

    monkeypatch.setattr(empty_session, "compact", _boom)

    _ack, end = await _compact(empty_handler)
    assert end["is_error"] is True
    assert "summarizer said no" in end["error"]
    assert "performed" not in end
    assert end["cursor"] == empty_session.session_log.cursor
    # The single-flight slot is released even on the failure path.
    assert empty_handler.compaction_in_flight is None


# ── custom_instructions threading (dispatch layer, no LLM needed) ─────────


async def test_compact_threads_custom_instructions_to_agent_session(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: change the handler to call
    `session.compact(custom_instructions=None)` unconditionally (drop the
    params read) and this goes red — `captured["value"]` stays `None`."""
    captured: dict[str, object] = {}

    async def _spy_compact(custom_instructions: str | None = None) -> None:
        captured["value"] = custom_instructions
        return None

    monkeypatch.setattr(empty_session, "compact", _spy_compact)

    await _compact(empty_handler, {"custom_instructions": "focus on X"})
    assert captured["value"] == "focus on X"

    await _compact(empty_handler, {})
    assert captured["value"] is None


# ── the `details is None` defensive branch ─────────────────────────────────


async def test_compact_details_none_reports_empty_file_lists(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: replace `details.read_files if details is not None
    else []` with a bare `details.read_files` and this reports `is_error`
    with an AttributeError instead of passing — proving the guard is
    load-bearing, not decorative, for a `CompactionResult` whose `details`
    field is `None` (a shape `CompactionResult`'s own type allows even though
    the real compaction pipeline in compaction.py never produces it)."""

    async def _spy_compact(custom_instructions: str | None = None) -> CompactionResult:
        return CompactionResult(
            summary="s",
            first_kept_entry_id="e1",
            tokens_before=10,
            details=None,
            compacted_entry_ids=["e0"],
            tokens_saved=3,
            usage={"total_tokens": 7},
        )

    monkeypatch.setattr(empty_session, "compact", _spy_compact)

    _ack, end = await _compact(empty_handler)
    assert end["is_error"] is False
    assert end["performed"] is True
    assert end["read_files"] == []
    assert end["modified_files"] == []


async def test_compact_details_present_reports_its_file_lists(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _spy_compact(custom_instructions: str | None = None) -> CompactionResult:
        return CompactionResult(
            summary="s",
            first_kept_entry_id="e1",
            tokens_before=10,
            details=CompactionDetails(read_files=["a.py"], modified_files=["b.py"]),
            compacted_entry_ids=["e0"],
            tokens_saved=3,
            usage={"total_tokens": 7},
        )

    monkeypatch.setattr(empty_session, "compact", _spy_compact)

    _ack, end = await _compact(empty_handler)
    assert end["read_files"] == ["a.py"]
    assert end["modified_files"] == ["b.py"]


# ── D-1: turn_safety_guard wiring ───────────────────────────────────────────


async def test_compact_holds_turn_lock_for_the_duration_of_the_call(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: delete the `async with turn_safety_guard(session):`
    wrapper in `_drive` (call `session.compact(...)` directly) and
    `observed["locked"]` becomes `False` — the lock is never actually held
    while `compact()` runs, even though the handler still "works" by every
    other test in this file, which is exactly why D-1 needs its own,
    lock-observing assertion rather than relying on the guard's own
    (already-covered, in test_rpc_tier_b_scaffolding.py) unit tests."""
    observed: dict[str, bool] = {}
    real_compact = AgentSession.compact

    async def _spy_compact(self: AgentSession, custom_instructions: str | None = None):
        observed["locked"] = self.turn_lock.locked()
        return await real_compact(self, custom_instructions=custom_instructions)

    monkeypatch.setattr(AgentSession, "compact", _spy_compact)

    assert not empty_session.turn_lock.locked()
    await _compact(empty_handler)
    assert observed["locked"] is True
    assert not empty_session.turn_lock.locked()


async def test_compact_propagates_turn_still_running_from_the_guard(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's refusal still reaches the CALLER as-is — not swallowed,
    not converted into a `performed=False` outcome, and NOT relegated to the
    compaction_end notification — even though the guard is now taken inside
    the background task: `_drive` routes a pre-acknowledgement failure back
    through the `acknowledged` future, so this call's own response is the
    refusal, exactly as before Blocker 1.

    MUTATION TARGET: in `_drive`'s `except Exception` branch, drop the `if
    not acknowledged.done()` arm (always `_complete(...)` instead) and this
    goes red — no RPCError is ever raised and the awaiting dispatch coroutine
    hangs until the `finally`'s `acknowledged.cancel()` turns it into a
    CancelledError."""

    @asynccontextmanager
    async def _always_refuses(session, *, timeout: float = 5.0):
        raise RPCError(TURN_STILL_RUNNING, "turn is in flight (test double)")
        yield  # pragma: no cover - unreachable; keeps this an async CM shape

    monkeypatch.setattr(commands, "turn_safety_guard", _always_refuses)

    with pytest.raises(RPCError) as excinfo:
        await commands._handle_compact(empty_handler, None, {})
    assert excinfo.value.code == TURN_STILL_RUNNING
    # Nothing was enqueued, and the single-flight slot was released.
    assert _drain_queue(empty_handler) == []
    assert empty_handler.compaction_in_flight is None


# ── single-flight (constraint 3: two concurrent compactions are impossible) ─


async def test_second_compact_while_one_is_in_flight_is_refused_immediately(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second `compact` arriving while one is still running is refused with
    TURN_STILL_RUNNING (D-1's vocabulary for "no, a thing is running") —
    IMMEDIATELY, not after waiting out the turn_lock the first compaction
    holds for as long as the provider takes.

    The elapsed-time assertion is the load-bearing half: without the
    single-flight check the second call would still fail, but only after
    `DEFAULT_SWAP_TIMEOUT_S` on the serial reader.

    MUTATION TARGET: delete the `if in_flight is not None: raise` block and
    this goes red twice over — the elapsed assertion trips (the guard wait
    runs for the full timeout) and, with the gate opened, a second
    compaction would run concurrently with the first."""
    started = asyncio.Event()
    gate = asyncio.Event()
    monkeypatch.setattr(empty_session, "compact", _gated_compact(started, gate))

    await commands._handle_compact(empty_handler, 1, {})
    await asyncio.wait_for(started.wait(), timeout=1.0)

    loop = asyncio.get_running_loop()
    before = loop.time()
    with pytest.raises(RPCError) as excinfo:
        await commands._handle_compact(empty_handler, 2, {})
    elapsed = loop.time() - before

    assert excinfo.value.code == TURN_STILL_RUNNING
    assert excinfo.value.data is not None
    assert excinfo.value.data["compaction_id"] == empty_handler.compaction_in_flight
    assert elapsed < 0.5, (
        "the second compact waited on the turn_lock the first one holds "
        f"({elapsed:.2f}s) instead of being refused outright"
    )

    gate.set()
    await _settle(empty_handler)
    # Exactly one compaction ran, and the slot is free again afterwards.
    items = _drain_queue(empty_handler)
    assert [i for i in items if i.get("method") == commands.COMPACTION_END_METHOD].__len__() == 1
    assert empty_handler.compaction_in_flight is None


# ── D-7: durability (finding 6 — the tier's third answer, retired) ─────────


class _UnpersistedLog(_PersistedLog):
    """The shape a real ``new_session {"persist": false}`` produces: a
    ``ConversationSession`` that HAS every appender and a ``path``
    attribute, whose ``path`` is ``None`` and whose ``_persist_*`` are
    therefore no-ops (``session_store.py:585,593``). That is the exact
    session finding 6 measured ``compact`` running to completion on."""

    path = None


async def test_compact_refuses_an_unpersisted_session(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-7 rule 1, the case finding 6 measured over the wire: on one
    ``new_session {"persist": false}`` session, ``set_model`` and
    ``set_session_name`` refused with -32603 while ``compact`` appended a
    ``compaction`` entry and reported a cursor for it — an entry lost on
    exit, i.e. the exact "cursor for a write that leaves no durable record"
    ``require_durable_session``'s own docstring rejects.

    The refusal is TOTAL and it is CHEAP: asserted here by the compaction
    never starting at all (no provider call, no acknowledgement, nothing
    enqueued, the single-flight slot untouched), because the guard runs
    before any of that.

    MUTATION TARGET: delete the ``require_durable_session(session,
    verb="compact")`` line from ``_handle_compact`` — every assertion below
    goes red at once (no exception is raised and the compaction runs)."""
    empty_session.session_log = _UnpersistedLog()
    started = asyncio.Event()
    gate = asyncio.Event()
    gate.set()
    monkeypatch.setattr(empty_session, "compact", _gated_compact(started, gate))

    with pytest.raises(RPCError) as excinfo:
        await commands._handle_compact(empty_handler, 5, {})

    assert excinfo.value.code == SESSION_NOT_PERSISTED
    assert "compact:" in str(excinfo.value)
    assert "unpersisted" in str(excinfo.value)
    assert not started.is_set(), "the compaction ran anyway — the refusal was not total"
    assert _drain_queue(empty_handler) == []
    assert empty_handler.compaction_in_flight is None
    assert empty_handler._background_tasks == set()


async def test_compact_refuses_a_log_that_declares_no_location_at_all(
    empty_session: AgentSession, empty_handler: RPCHandler
) -> None:
    """The other half of ``require_durable_session``: the SDK's bare
    ``InMemorySessionLog`` declares neither ``path`` nor ``root_doc_id``, and
    an undeclared location is refused rather than assumed durable (that
    asymmetry is ``_DURABLE_LOCATION_ATTRS``' whole point).

    Kept separate from the test above because the two messages are
    different facts a host has to act on differently: "you are on an
    unpersisted session, move" vs "this store does not answer the question".

    MUTATION TARGET: the same deleted line; also reddened by making
    ``_DURABLE_LOCATION_ATTRS`` empty."""
    empty_session.session_log = InMemorySessionLog()

    with pytest.raises(RPCError) as excinfo:
        await commands._handle_compact(empty_handler, 5, {})

    assert excinfo.value.code == SESSION_NOT_PERSISTED
    assert "declares no durable location" in str(excinfo.value)
    assert empty_handler.compaction_in_flight is None


async def test_compact_and_set_auto_compaction_disagree_on_purpose(
    empty_session: AgentSession, empty_handler: RPCHandler
) -> None:
    """D-7 rule 2 is not an oversight, so it is pinned as deliberately as
    rule 1: ``set_auto_compaction`` appends NOTHING, so on the very session
    ``compact`` refuses it still answers, cursor and all.

    Finding 6's complaint was three answers with no stated rule; the fix is
    ONE rule that still produces two behaviours, and a test that asserts
    only the refusal would let a future edit "restore consistency" by
    guarding the setter too — which would deny a working capability over a
    promise that verb never made.

    MUTATION TARGET: add ``require_durable_session`` to
    ``_handle_set_auto_compaction`` and the second half goes red."""
    empty_session.session_log = _UnpersistedLog()

    with pytest.raises(RPCError):
        await commands._handle_compact(empty_handler, 1, {})

    result = await commands._handle_set_auto_compaction(empty_handler, 2, {"enabled": True})
    assert result["enabled"] is True
    assert result["cursor"] == empty_session.session_log.cursor


# ── finding 5: abort reaches an in-flight compaction ───────────────────────


async def test_abort_cancels_an_in_flight_compaction_and_says_which(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5: ``abort`` used to answer ``{"status": "aborted"}`` at
    +0.00s while the compaction ran to completion and rewrote the tree at
    +20.01s — a verb reporting success for something it did not do.

    Three claims, all of which the pre-fix code failed:

    1. the compaction's task is actually cancelled;
    2. ``abort``'s own response NAMES it, so a host can correlate the
       completion that follows to the abort that caused it;
    3. that completion arrives, carrying ``cancelled: true`` and NO
       ``performed`` — a compaction stopped part-way neither performed nor
       found nothing to do.

    The gate is never opened, so the compaction can only end by
    cancellation: a test that opened it would be unable to tell "abort
    stopped it" from "it finished on its own".

    MUTATION TARGET: make ``_handle_abort`` return ``{"status": "aborted",
    "compaction_id": None}`` without calling ``handler.abort_compaction()``
    — the pre-fix behaviour exactly — and all three go red."""
    started = asyncio.Event()
    gate = asyncio.Event()  # never set
    monkeypatch.setattr(empty_session, "compact", _gated_compact(started, gate))

    await commands._handle_compact(empty_handler, 1, {})
    await asyncio.wait_for(started.wait(), timeout=1.0)
    (ack,) = _drain_queue(empty_handler)
    compaction_id = ack["result"]["compaction_id"]
    (task,) = list(empty_handler._background_tasks)

    aborted = await commands._handle_abort(empty_handler, 2, {})
    assert aborted == {"status": "aborted", "compaction_id": compaction_id}

    await _settle_cancelled(empty_handler)
    assert task.cancelled()

    (end,) = _drain_queue(empty_handler)
    assert end["method"] == commands.COMPACTION_END_METHOD
    params = end["params"]
    assert commands.validate_params(commands.COMPACTION_END_PARAMS_SCHEMA, params) is None
    assert params["compaction_id"] == compaction_id
    assert params["request_id"] == 1
    assert params["cancelled"] is True
    assert params["is_error"] is False
    assert params["error"] is None
    assert "performed" not in params
    assert params["cursor"] == empty_session.session_log.cursor
    # The slot is free again, so a host can compact once more — and so is
    # D-1's lock, which the cancellation unwound through
    # `turn_safety_guard`'s `finally`. A session left permanently locked by
    # an abort would refuse every mutator from here on with
    # TURN_STILL_RUNNING, which is a worse outcome than the bug being fixed.
    assert empty_handler.compaction_in_flight is None
    assert not empty_session.turn_lock.locked()


async def test_an_aborted_compaction_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim ``cancelled: true`` makes about the log, asserted against a
    REAL ``AgentSession.compact`` rather than a double: the summary is
    generated before the ``compaction`` entry is appended, so cancelling
    inside the provider call leaves the log exactly as it was.

    Without this, "nothing was written" would be an article of faith in a
    comment, and a future change that appended before summarizing would
    turn ``cancelled: true`` into the same class of lie finding 5 is about.

    Also the EVENT bracket, which only the real method emits: ``compact``'s
    ``agent_start`` went out before the abort, and its ``finally``'s
    ``agent_end`` still goes out after it. A host that saw the open and
    never the close would believe work was still running on a session that
    has none — a second way to be told nothing true.

    MUTATION TARGET: move ``session_log.append_compaction`` in
    ``agent_session._perform_compaction`` ahead of ``run_compaction`` — the
    entry-list assertion goes red."""
    provider_reached = asyncio.Event()

    async def _never_answers(model, context, options=None):
        provider_reached.set()
        await asyncio.Event().wait()  # the unbounded provider call, forever
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _never_answers)
    session = _multi_turn_session(CompactionSettings(keep_recent_tokens=1))
    handler = RPCHandler(session)
    entries_before = list(session.session_log.entries())
    cursor_before = session.session_log.cursor

    await commands._handle_compact(handler, 1, {})
    await asyncio.wait_for(provider_reached.wait(), timeout=5.0)
    items = _drain_queue(handler)  # the ack + compact()'s own agent_start

    await commands._handle_abort(handler, 2, {})
    await _settle_cancelled(handler)
    items += _drain_queue(handler)

    end = [i for i in items if i.get("method") == commands.COMPACTION_END_METHOD]
    assert len(end) == 1 and end[0]["params"]["cancelled"] is True
    assert session.session_log.entries() == entries_before
    assert session.session_log.cursor == cursor_before

    lifecycle = [
        i["params"]["type"]
        for i in items
        if i.get("method") == "event" and i["params"]["type"] in ("agent_start", "agent_end")
    ]
    assert lifecycle == [
        "agent_start",
        "agent_end",
    ], f"compact's lifecycle bracket did not close on cancellation: {lifecycle}"


async def test_abort_with_no_compaction_running_reports_null(
    empty_session: AgentSession, empty_handler: RPCHandler
) -> None:
    """``compaction_id: null`` is a real answer, not a placeholder: it is how
    a host knows NOT to wait for a compaction_end. Also the ordinary case —
    most aborts are aimed at a turn.

    MUTATION TARGET: have ``abort_compaction`` return
    ``self.compaction_in_flight`` unconditionally (i.e. without the
    ``cancel is None`` arm) and this goes red only if a compaction ever
    ran; the following test covers the harder half."""
    assert await commands._handle_abort(empty_handler, 1, {}) == {
        "status": "aborted",
        "compaction_id": None,
    }


async def test_a_second_abort_does_not_claim_it_stopped_the_same_compaction_twice(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``abort`` is documented as idempotent, and idempotent must not mean
    "keeps claiming credit". The second call reports ``compaction_id: null``
    because there is nothing left to signal — the handle was dropped by the
    first one.

    MUTATION TARGET: delete ``self._compaction_aborter = None`` from
    ``RPCHandler.abort_compaction`` — the second abort then re-cancels an
    already-cancelled task and reports the same id, and this goes red."""
    started = asyncio.Event()
    gate = asyncio.Event()  # never set
    monkeypatch.setattr(empty_session, "compact", _gated_compact(started, gate))

    await commands._handle_compact(empty_handler, 1, {})
    await asyncio.wait_for(started.wait(), timeout=1.0)

    first = await commands._handle_abort(empty_handler, 2, {})
    second = await commands._handle_abort(empty_handler, 3, {})

    assert isinstance(first["compaction_id"], str)
    assert second["compaction_id"] is None
    await _settle_cancelled(empty_handler)


async def test_abort_cannot_reach_a_compaction_that_has_not_been_acknowledged(
    empty_session: AgentSession, empty_handler: RPCHandler
) -> None:
    """Why ``bind_compaction_aborter`` is called from ``_acknowledge`` and
    not from the top of ``_drive``.

    Before the acknowledgement the compaction is still parked on D-1's
    ``turn_lock`` and the dispatch coroutine is still awaiting its own
    ``acknowledged`` future. Cancelling the task there would resolve that
    future by CANCELLING it (the ``finally``'s ``acknowledged.cancel()``),
    turning a ``compact`` request into a ``CancelledError`` escaping the
    dispatcher — a wedged request instead of an answered one.

    So: with a turn holding the lock, ``abort`` finds nothing to signal, and
    the ``compact`` request still ends the way D-1 says it does
    (TURN_STILL_RUNNING once the bounded wait expires).

    A COMPLETED compaction runs first, deliberately. That is what makes
    ``release_compaction`` clearing the aborter — not just the id —
    load-bearing: with a stale handle left behind, ``abort`` here would
    cancel the finished task (a no-op) while reporting the SECOND
    compaction's id, i.e. claim to have stopped a compaction that then runs
    on. That is finding 5's own defect, one round later.

    MUTATION TARGET: move the ``handler.bind_compaction_aborter(...)`` call
    out of ``_acknowledge`` and to the first line of ``_drive`` — the
    ``compaction_id is None`` assertion goes red, and the ``RPCError``
    becomes a ``CancelledError``. Or delete
    ``self._compaction_aborter = None`` from ``release_compaction`` — the
    same assertion goes red, reporting the parked compaction's id."""
    await _compact(empty_handler)  # one compaction, run to completion first

    await empty_session.turn_lock.acquire()  # a turn is in flight
    try:
        compact_call = asyncio.ensure_future(commands._handle_compact(empty_handler, 1, {}))
        await asyncio.sleep(0)  # let _drive start and park on the guard

        aborted = await commands._handle_abort(empty_handler, 2, {})
        assert aborted["compaction_id"] is None, (
            "abort reached a compaction that had not been acknowledged yet — "
            "cancelling there wedges the compact request instead of answering it"
        )

        with pytest.raises(RPCError) as excinfo:
            await asyncio.wait_for(compact_call, timeout=commands.DEFAULT_SWAP_TIMEOUT_S + 5.0)
        assert excinfo.value.code == TURN_STILL_RUNNING
    finally:
        empty_session.turn_lock.release()


async def test_every_other_outcome_reports_cancelled_false(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cancelled`` is `required` and always present — absence is not this
    tier's way of saying anything (E5 rule 3, applied to the field finding 5
    added). Asserted on all three non-cancelled outcomes.

    MUTATION TARGET: drop ``"cancelled": False`` from either
    ``_compaction_outcome`` branch or from the ``is_error`` payload — the
    schema check inside ``_compact`` fails first (``cancelled`` is
    required), before these assertions are even reached."""
    _ack, end = await _compact(empty_handler)  # performed=False
    assert end["cancelled"] is False

    async def _boom(custom_instructions: str | None = None) -> CompactionResult:
        raise RuntimeError("summarizer said no")

    monkeypatch.setattr(empty_session, "compact", _boom)
    _ack, end = await _compact(empty_handler)  # is_error
    assert end["cancelled"] is False

    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple", _fake_complete_simple("## Goal\nrecap")
    )
    performed_session = _multi_turn_session(CompactionSettings(keep_recent_tokens=1))
    _ack, end = await _compact(RPCHandler(performed_session))  # performed=True
    assert end["cancelled"] is False


# ── shutdown (constraint 4: a tracked task, reaped by run()'s teardown) ─────


async def test_a_running_compaction_is_reaped_by_shutdown(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 4 fixed three unkillable-process routes; a background task that
    outlives `run()` is exactly the hazard `track_background_task` /
    `_cancel_background_tasks` exist to manage. A compaction parked in the
    provider call is cancellable and IS cancelled by that teardown.

    MUTATION TARGET: drop the `handler.track_background_task(...)` wrapper in
    `_handle_compact` (bare `asyncio.create_task(_drive())`) and this goes
    red — `_background_tasks` is empty, so `_cancel_background_tasks` has
    nothing to reap and the never-gated compaction is still pending at the
    end."""
    started = asyncio.Event()
    gate = asyncio.Event()  # never set: this compaction only ends by cancellation
    monkeypatch.setattr(empty_session, "compact", _gated_compact(started, gate))

    await commands._handle_compact(empty_handler, 1, {})
    await asyncio.wait_for(started.wait(), timeout=1.0)
    tracked = list(empty_handler._background_tasks)
    assert len(tracked) == 1

    empty_handler._shutting_down = True  # what run()'s teardown sets first
    await empty_handler._cancel_background_tasks()

    assert tracked[0].done()
    assert tracked[0].cancelled()
    assert empty_handler.compaction_in_flight is None


async def test_a_reaped_compaction_emits_no_compaction_end(
    empty_session: AgentSession, empty_handler: RPCHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-5: a compaction cancelled by shutdown emits NO `compaction_end`.

    Finding 4 (Tier B review), mutation V1: inserting
    `_complete({"is_error": True, "error": "cancelled", "cursor": None})` as
    the first statement of `_drive`'s `except asyncio.CancelledError` arm —
    i.e. emitting the notification D-5 says must never be emitted — survived
    the whole suite. `test_a_running_compaction_is_reaped_by_shutdown` above
    asserts the task is cancelled and the slot cleared; it never looks at the
    output queue, so nothing held the claim.

    The nothing-was-emitted half of an assertion is only as good as the proof
    that the observation works at all, so this also asserts the
    ACKNOWLEDGEMENT is sitting in the same drained queue: a `_drain_queue`
    that returned `[]` because of some unrelated breakage would fail here
    rather than pass vacuously.

    MUTATION TARGET: add any `_complete(...)` call to that arm, or replace
    the arm's `raise` with a fall-through into the success `_complete` — both
    go red on the first assertion."""
    started = asyncio.Event()
    gate = asyncio.Event()  # never set: this compaction only ends by cancellation
    monkeypatch.setattr(empty_session, "compact", _gated_compact(started, gate))

    await commands._handle_compact(empty_handler, 11, {})
    await asyncio.wait_for(started.wait(), timeout=1.0)

    empty_handler._shutting_down = True
    await empty_handler._cancel_background_tasks()

    items = _drain_queue(empty_handler)
    assert [i for i in items if i.get("method") == commands.COMPACTION_END_METHOD] == [], (
        "a compaction that was cancelled before it finished emitted a "
        "compaction_end -- D-5 forbids it: nothing was generated and nothing "
        "was written, and an is_error notification for it is indistinguishable "
        "on the wire from a real CompactionError"
    )
    acks = [i for i in items if "id" in i]
    assert len(acks) == 1 and acks[0]["id"] == 11, (
        f"the acknowledgement should still be here; got {items}"
    )


async def test_a_reaped_compaction_says_so_on_stderr(
    empty_session: AgentSession,
    empty_handler: RPCHandler,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T4: the stderr line is the ONLY report a reaped compaction makes, so
    it is load-bearing, not decoration.

    Finding 4 (Tier B review), mutation V2 — deleting that `print` entirely
    survived the whole suite. That is the more consequential half of the two:
    the code's own comment argues stderr is the honest substitute BECAUSE no
    notification may be delivered, and with the print gone a host gets
    nothing at all from either channel.

    Asserts the compaction_id is in the line, not merely that something was
    printed: correlation is the whole point of a report about one of
    potentially many background tasks.

    MUTATION TARGET: delete the `print(...)` in `_drive`'s `except
    asyncio.CancelledError` arm, or drop the `{compaction_id!r}` from it."""
    started = asyncio.Event()
    gate = asyncio.Event()  # never set
    monkeypatch.setattr(empty_session, "compact", _gated_compact(started, gate))

    await commands._handle_compact(empty_handler, 12, {})
    await asyncio.wait_for(started.wait(), timeout=1.0)
    compaction_id = empty_handler.compaction_in_flight
    assert compaction_id is not None

    empty_handler._shutting_down = True
    await empty_handler._cancel_background_tasks()

    err = capsys.readouterr().err
    assert compaction_id in err, (
        f"the cancelled compaction's id is not in stderr; stderr was {err!r}"
    )
    assert "was cancelled" in err
    assert "no compaction_end will follow" in err


# ── shutdown, finding 3: a completion in the teardown window reports SOMEWHERE ─


class _FakeStdin:
    """Stand-in for `sys.stdin` exposing only the `.buffer` attribute
    `transport._read_stdin` uses (the same shape `test_rpc_transport.py`
    defines for itself; duplicated rather than imported across test files,
    the precedent `_msg` above already sets in this module)."""

    def __init__(self, buffer: BinaryIO) -> None:
        self.buffer = buffer


class _Recorder:
    """A stdout double backed by a REAL OS pipe, recording complete lines.

    `_write_stdout` moves bytes through `loop.connect_write_pipe`, which
    needs an actual file descriptor — an in-memory buffer cannot stand in
    (see `test_rpc.py::_Recorder`, of which this is the trimmed copy this
    module needs)."""

    def __init__(self) -> None:
        read_fd, write_fd = os.pipe()
        self._write_file = os.fdopen(write_fd, "w")
        self._read_file = os.fdopen(read_fd, "r")
        self.lines: list[dict[str, Any]] = []
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    @property
    def buffer(self) -> Any:  # what `_connect_stdout_writer` wires up to
        return self._write_file.buffer

    def _drain(self) -> None:
        for line in self._read_file:
            line = line.rstrip("\n")
            if line:
                self.lines.append(json.loads(line))

    def close(self) -> None:
        self._write_file.close()

    def join(self) -> None:
        self._thread.join(timeout=5)
        self._read_file.close()

    def methods(self) -> list[str]:
        return [str(item.get("method")) for item in self.lines if "method" in item]


async def test_a_compaction_finishing_inside_the_reap_window_still_reaches_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 3 (Tier B review): the hole in D-5's completion contract.

    `run()`'s teardown used to drain and exit the writer BEFORE reaping the
    background tasks, and `_cancel_background_tasks`'s phase 1 waits
    `_BACKGROUND_TASK_GRACE_S` without cancelling. A compaction finishing in
    that window called `put_nowait` on a queue nobody would ever read: rc 0,
    empty stderr, no compaction_end -- and, against a real session store, a
    `compaction` entry durably written for the next process to find
    unannounced. Measured against a real `--mode rpc` child before the fix.

    Deterministic by construction, not by clock: `_cancel_background_tasks`
    is wrapped so that opening the compaction's gate IS the first thing the
    reap does. The compaction therefore always completes inside the reap
    window -- the exact interval the defect was about -- on every machine
    and under any load, instead of being aimed at it with a `sleep`.

    Drives the real `run()` over real pipes, because the property is a
    property of ITS teardown ordering and nothing smaller reproduces it.

    MUTATION TARGET: move `self._shutting_down = True; await
    self._cancel_background_tasks()` back below the writer drain (i.e. leave
    it only in `run()`'s `finally`) and this goes red -- the gate then opens
    after the writer has already exited, so the notification never reaches
    the pipe (it goes to stderr instead, which is the honest fallback, not
    the delivery this test is about)."""
    read_fd, write_fd = os.pipe()
    stdin_fake = _FakeStdin(os.fdopen(read_fd, "rb", buffering=0))
    stdin_writer = os.fdopen(write_fd, "wb", buffering=0)
    recorder = _Recorder()
    monkeypatch.setattr("sys.stdin", stdin_fake)
    monkeypatch.setattr("sys.stdout", recorder)

    session = AgentSession(session_log=_PersistedLog(), model=_model(), tools=[])
    started = asyncio.Event()
    gate = asyncio.Event()
    monkeypatch.setattr(session, "compact", _gated_compact(started, gate))
    handler = RPCHandler(session)

    reap = handler._cancel_background_tasks

    async def _reap_with_the_compaction_finishing_inside_it() -> None:
        gate.set()
        await reap()

    monkeypatch.setattr(
        handler, "_cancel_background_tasks", _reap_with_the_compaction_finishing_inside_it
    )

    run_task = asyncio.create_task(handler.run())
    try:
        await asyncio.sleep(0.05)  # let run() take over stdout and start reading
        stdin_writer.write(
            (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "compact"}) + "\n").encode()
        )
        stdin_writer.flush()
        await asyncio.wait_for(started.wait(), timeout=5.0)
        assert not gate.is_set(), "the gate opened before the reap -- the setup is not honest"

        stdin_writer.close()  # EOF: the clean shutdown trigger (T1/P4)
        await asyncio.wait_for(run_task, timeout=20.0)
    finally:
        with contextlib.suppress(OSError):
            stdin_writer.close()
        run_task.cancel()
        recorder.close()
        recorder.join()

    assert commands.COMPACTION_END_METHOD in recorder.methods(), (
        "the compaction finished inside run()'s background-task grace window "
        "and its compaction_end never reached stdout -- the host was told "
        f"nothing. Lines written: {recorder.lines}"
    )


async def test_sigterm_says_what_it_discarded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """P1 (SIGTERM skips the flush) is unchanged; what finding 3 adds is that
    discarding queued output now SAYS SO.

    The reap ahead of the drain can leave a background task's last items -- a
    `compaction_end` among them -- queued microseconds before the writer is
    cancelled, and "the host was told nothing about a mutation that landed"
    is the defect this fix exists to remove whatever the shutdown trigger.
    Every other truncation route already announced itself: the post-EOF flush
    prints when the peer stops reading, and a broken pipe propagates out of
    `run()`. This was the one with no report at all.

    Deterministic without racing the writer: its queue never yields, so the
    item provably has not been written when the SIGTERM branch runs. The
    signal itself is not raised for real -- `_exit_signal` is set at the end
    of the reap, immediately before `run()` reads it -- because sending a
    process-wide SIGTERM from inside a test to exercise one `if` would be a
    far bigger hammer than the branch deserves.

    MUTATION TARGET: delete the `if pending:` report on `run()`'s SIGTERM
    branch."""

    class _QueueThatNeverYields(asyncio.Queue):
        """Nothing dequeues, so anything put here is provably still queued."""

        async def get(self) -> Any:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")  # pragma: no cover

    read_fd, write_fd = os.pipe()
    stdin_fake = _FakeStdin(os.fdopen(read_fd, "rb", buffering=0))
    stdin_writer = os.fdopen(write_fd, "wb", buffering=0)
    recorder = _Recorder()
    monkeypatch.setattr("sys.stdin", stdin_fake)
    monkeypatch.setattr("sys.stdout", recorder)

    session = AgentSession(session_log=_PersistedLog(), model=_model(), tools=[])
    started = asyncio.Event()
    gate = asyncio.Event()
    monkeypatch.setattr(session, "compact", _gated_compact(started, gate))
    handler = RPCHandler(session)
    handler._output_queue = _QueueThatNeverYields()

    reap = handler._cancel_background_tasks

    async def _reap_then_sigterm() -> None:
        gate.set()
        await reap()
        handler._exit_signal = "SIGTERM"  # P1's branch, without a real signal

    monkeypatch.setattr(handler, "_cancel_background_tasks", _reap_then_sigterm)

    run_task = asyncio.create_task(handler.run())
    try:
        await asyncio.sleep(0.05)
        stdin_writer.write(
            (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "compact"}) + "\n").encode()
        )
        stdin_writer.flush()
        await asyncio.wait_for(started.wait(), timeout=5.0)
        stdin_writer.close()
        await asyncio.wait_for(run_task, timeout=20.0)
    finally:
        with contextlib.suppress(OSError):
            stdin_writer.close()
        run_task.cancel()
        recorder.close()
        recorder.join()

    err = capsys.readouterr().err
    assert "SIGTERM discarded" in err, f"SIGTERM threw output away silently; stderr: {err!r}"
    # The count, not just the fact: "some output was lost" a host cannot size
    # is barely a report at all. Two items: the acknowledgement and the
    # compaction_end the reap let land.
    assert "discarded 2 queued item(s)" in err, err


async def test_the_writer_does_not_exit_with_items_still_queued(
    empty_handler: RPCHandler,
) -> None:
    """Finding 3's second half, in `transport._write_stdout`.

    Its `except asyncio.TimeoutError: if not self._running: break` used to
    contradict the `while` condition directly above it, which writes while
    "`_running`, OR the queue is non-empty". `wait_for` timing out proves
    only that the queue was empty for the last 0.5s; a `put_nowait` landing
    in the same event-loop iteration as the timer loses the race (the getter
    it wakes is cancelled, and the item stays queued), and the handler then
    ran with items waiting. Reaping before the drain made it reachable: with
    a completion offset of exactly 0.5s into the reap, three real children
    out of three lost both the compaction's `agent_end` and its
    `compaction_end`.

    The lost wakeup is reproduced structurally rather than by racing a
    timer: a queue whose FIRST `get()` never resolves puts `_write_stdout`
    into precisely the state that race produces -- TimeoutError raised,
    item still queued -- on every run.

    MUTATION TARGET: restore `if not self._running: break` and this goes red
    (nothing is written at all)."""

    class _QueueThatLosesOneWakeup(asyncio.Queue):
        """`get()` hangs the first time, exactly as an already-cancelled
        getter does when `put_nowait`'s wakeup lost the race with the
        `wait_for` timer. The item is NOT consumed -- `qsize`/`empty` keep
        reporting it, which is the whole point."""

        def __init__(self) -> None:
            super().__init__()
            self._swallowed = False

        async def get(self) -> Any:
            if not self._swallowed:
                self._swallowed = True
                await asyncio.sleep(3600)
            return await super().get()

    recorder = _Recorder()
    empty_handler._real_stdout = recorder  # type: ignore[assignment]
    empty_handler._running = False  # run() has already cleared it
    empty_handler._output_queue = _QueueThatLosesOneWakeup()
    empty_handler._output_queue.put_nowait(
        {
            "jsonrpc": "2.0",
            "method": commands.COMPACTION_END_METHOD,
            "params": {"compaction_id": "c", "request_id": 1, "is_error": False, "cursor": None},
        }
    )

    try:
        await asyncio.wait_for(empty_handler._write_stdout(), timeout=10.0)
    finally:
        recorder.close()
        recorder.join()

    assert recorder.methods() == [commands.COMPACTION_END_METHOD], (
        "the writer exited while an item was still queued, throwing it away; "
        f"it wrote {recorder.lines}"
    )
    assert empty_handler._output_queue.empty()


async def test_output_is_deliverable_separates_never_started_from_already_gone(
    empty_handler: RPCHandler,
) -> None:
    """The predicate `_complete` branches on (finding 3).

    `_stdout_task is None` means no writer was EVER started -- every
    white-box test in this file, and any embedded caller driving
    `RPCHandler` without `run()` -- where `_output_queue` belongs to
    whoever built the handler and enqueueing is the only correct thing to
    do. Conflating that with "the writer has finished" would silence every
    in-process completion instead of the one with nowhere to go, which is
    why this asserts all three states rather than just the False one.

    MUTATION TARGET: `return self._stdout_task is not None and not
    self._stdout_task.done()` (the None case flipped) -- red on the first
    assertion, and it would also take out most of this file."""
    assert empty_handler._stdout_task is None
    assert empty_handler.output_is_deliverable is True

    writer = asyncio.create_task(asyncio.sleep(30))
    empty_handler._stdout_task = writer
    assert empty_handler.output_is_deliverable is True

    writer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await writer
    assert empty_handler.output_is_deliverable is False


async def test_an_outcome_the_writer_can_no_longer_carry_goes_to_stderr(
    empty_session: AgentSession,
    empty_handler: RPCHandler,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding 3, the paths where delivery is impossible rather than late:
    a broken pipe (T6), or SIGTERM cancelling the writer outright (P1).
    Reaping before the drain cannot help there -- the writer is already
    gone -- so the outcome goes to stderr (T4) with its whole payload,
    exactly as the cancellation arm reports itself, and is never enqueued
    onto a queue nobody will read.

    MUTATION TARGET: delete the `if not handler.output_is_deliverable:`
    branch in `_complete` and this goes red twice -- stderr is silent and
    the notification is enqueued into the void."""
    dead_writer = asyncio.create_task(asyncio.sleep(0))
    await dead_writer
    empty_handler._stdout_task = dead_writer
    assert empty_handler.output_is_deliverable is False

    assert await commands._handle_compact(empty_handler, 13, {}) is None
    await _settle(empty_handler)

    items = _drain_queue(empty_handler)
    assert [i for i in items if i.get("method") == commands.COMPACTION_END_METHOD] == [], (
        "the outcome was enqueued onto a queue whose writer has already exited"
    )
    err = capsys.readouterr().err
    assert "no compaction_end could be delivered" in err
    # The OUTCOME, not just the fact that one happened: this compaction ran
    # on an empty session, so `performed` is False and `cursor` is None.
    assert "'performed': False" in err, f"stderr does not carry the outcome: {err!r}"
    assert "'request_id': 13" in err
