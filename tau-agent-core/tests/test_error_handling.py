"""Error handling across the agent stack — ``agent_loop.py`` + ``agent_session.py``.

Previously ``test_phase6_subphase3_errors.py`` (56 tests / 833 LOC), structured
as three classes named after the three error SOURCES the original spec named:
``TestProviderErrorHandling``, ``TestToolErrorHandling``, ``TestExtensionErrorHandling``.

Measured alone, that file covered ``agent_loop.py`` at 33% and ``agent_session.py``
at 43%. Almost none of that was its own: every ``AgentToolResult`` /
``ToolBatchResult`` / ``ToolResultMessage`` / ``validate_tool_arguments`` /
``ExtensionContext`` / ``ExtensionUI`` / ``ExtensionAPI`` behaviour it asserted is
independently and more thoroughly tested in ``test_tools_base.py``,
``tau-ai/tests/test_subphase1.py``, ``tau-ai/tests/test_tools.py``,
``test_extension_types.py``, and ``test_ui_delegate.py`` — none of which import
``agent_loop`` or ``agent_session`` at all. Its ``TestExtensionErrorHandling``
"doesn't crash" checks are a strict subset of ``test_extension_error_visibility.py``
(S44/G3/G12), which additionally verifies the failure actually *surfaces* rather
than just vanishing. And two of its "integration" tests were checking nothing:
``test_prompt_handles_error_state`` ran a normal turn on a session with no error
condition at all, and ``test_failing_tool_in_session`` claimed to attach "a
failing tool" but never constructed or registered one — the session it built had
no tools, so the turn it ran could not have exercised a tool error under any
circumstance.

So the load-bearing question — do these tests exercise the three error paths for
real, or only assert that a mock raised what it was told to raise — was mostly
answered "the latter, and sometimes not even that." Two consequences followed
from actually running the paths for real:

* The provider path (``ErrorEvent`` -> ``agent_loop.py:780-799``) had **zero**
  real coverage anywhere in the suite. ``test_agent_loop.py`` imports
  ``ErrorEvent`` but never once constructs one; this file's own provider tests
  built a bare ``ErrorEvent`` and asserted its dataclass fields, never sending it
  through ``stream_simple``/``AgentLoop``. Driving one through the real loop
  here shows the old file's own framing ("provider error -> error event -> error
  message in chat") does not hold: at this layer the ``ErrorEvent`` becomes a
  raised ``RuntimeError`` that propagates, uncaught, out of ``AgentLoop.run``
  and through ``AgentSession.prompt()``/``submit()`` — there is no "error message
  in chat" here. This matches a comment already on record elsewhere
  (``tau-coding-agent/backends.py`` ~line 406: "a provider ``ErrorEvent``, which
  the loop turns into a ``RuntimeError``"), so the raise itself is intentional
  Fail-Early design and stays — but the spec-derived docstring describing this
  suite was simply wrong about the contract, and nothing had ever run the case
  that would have caught it.

  What the raise DID leak was the ``agent_start`` bracket: ``agent_end`` was
  reachable only by falling out of the while loop, so a raised or cancelled run
  never closed it. pi has no such hole because a provider error is a *value*
  there (``agent-loop.ts:342-353`` returns a ``stopReason="error"`` message and
  emits ``turn_end``/``agent_end`` normally) — τ diverged on the raise and
  inherited the emission placement that only works without one. ``AgentLoop.run``
  and ``run_continue`` now close from an ``except`` that re-raises, carrying
  ``is_error``/``error`` so "ended" and "died" are distinguishable.
* The extension path has a fail-CLOSED hook (``tool_call`` — a veto, or a
  raising handler, blocks the call: ``agent_loop.py:1176-1189``,
  ``"Extension failed, blocking execution: …"``) that had never been
  instantiated by any test in the repo (confirmed by grep), sitting right next
  to the fail-OPEN ``tool_result`` hook that S44 already covers. Both are
  exercised below now.

Consolidated from 56 tests to 10 test functions (12 collected cases). Coverage of
THIS FILE ALONE, measured the same way as the old file: agent_loop.py 33% -> 60%
(220 -> 132 statements missed, out of 328 — the ``ErrorEvent`` branch and the
fail-closed/fail-open hook paths account for most of the gain); agent_session.py
43% -> 43% (448 -> 445 missed, out of 783 — a small, honest gain, matching this
file's own small share of that module's behaviour; the module reaches 97% only
under the full suite, unaffected by this change).

Reference: docs/SUBPHASE-0.0.md AgentSession interface (the original spec doc,
``PHASE-6-SUBPHASE-3.md``, no longer exists in ``docs/`` — same situation as the
``test_export.py``/``test_rpc.py`` exemplars this file follows).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, ErrorEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, TextContent, ToolCall as TauToolCall, Usage, UserMessage
from tau_agent_core.agent_loop import AgentLoop
from tau_agent_core.agent_loop_types import AgentLoopConfig
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools.base import AgentTool, ToolDefinition

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _model():
    from tau_ai.types import Model

    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session(**kwargs) -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), **kwargs)


def _assistant_text(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


def _assistant_tool_call(call_id: str, name: str, args: dict) -> AssistantMessage:
    return AssistantMessage(
        content=[TauToolCall(type="toolCall", id=call_id, name=name, arguments=args)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    """A minimal async-iterable ``stream_simple`` result carrying fixed events."""

    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event in self._events:
            yield event

    async def result(self):
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self):
        pass


def _ok_stream_simple(text: str = "ok"):
    """A ``stream_simple`` double that always answers with plain text."""

    async def _fake(model, context, options=None):
        return _Stream(
            [
                TextDeltaEvent(delta=text, partial=_assistant_text(text)),
                DoneEvent(
                    final=_assistant_text(text),
                    usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
                ),
            ]
        )

    return _fake


def _error_stream_simple(message: str):
    """A ``stream_simple`` double whose provider always errors."""

    async def _fake(model, context, options=None):
        return _Stream([ErrorEvent(message=message)])

    return _fake


def _never_completes():
    """A ``stream_simple`` double that hangs until the task is cancelled."""

    async def _fake(model, context, options=None):
        await asyncio.Event().wait()
        raise AssertionError("unreachable — this stream only ends by cancellation")

    return _fake


def _tool_call_then_text(call_id: str, tool_name: str, args: dict, captured_contexts: list):
    """First call: a tool call. Second call: plain text. Records every ``context`` seen.

    ``captured_contexts`` lets a test inspect exactly what the SECOND (post-tool-result)
    LLM call was sent — the only way to verify a tool result actually reached the model,
    as opposed to merely being recorded on an ``AgentEvent``.
    """
    calls = {"n": 0}

    async def _fake(model, context, options=None):
        captured_contexts.append(context)
        calls["n"] += 1
        if calls["n"] == 1:
            final = _assistant_tool_call(call_id, tool_name, args)
            return _Stream([DoneEvent(final=final, usage=Usage())])
        return _Stream(
            [
                TextDeltaEvent(delta="done", partial=_assistant_text("done")),
                DoneEvent(final=_assistant_text("done"), usage=Usage()),
            ]
        )

    return _fake


def _failing_tool(name: str, error_msg: str, ran: list | None = None) -> AgentTool:
    """A real ``AgentTool`` whose ``execute`` raises — not a mock told to raise."""

    async def _execute(**kw):
        if ran is not None:
            ran.append(True)
        raise RuntimeError(error_msg)

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name,
            description=f"the {name} tool",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=_execute,
        )
    )


def _succeeding_tool(name: str, result: str = "tool ok", ran: list | None = None) -> AgentTool:
    async def _execute(**kw):
        if ran is not None:
            ran.append(True)
        return result

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name,
            description=f"the {name} tool",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=_execute,
        )
    )


# ---------------------------------------------------------------------------
# The matrix: three SOURCES, three different contracts
# ---------------------------------------------------------------------------


def _case_provider():
    patcher = patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_error_stream_simple("provider boom")
    )
    return patcher, lambda: _session()


def _case_tool():
    patcher = patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_tool_call_then_text("c1", "failing_tool", {}, []),
    )
    return patcher, lambda: _session(tools=[_failing_tool("failing_tool", "tool boom")])


def _case_extension():
    patcher = patch("tau_agent_core.agent_loop.stream_simple", side_effect=_ok_stream_simple())

    def _bad_ext(api):
        def _raise(event):
            raise ValueError("extension boom")

        api.on("agent_start", _raise)

    return patcher, lambda: _session(extensions=[_bad_ext])


@pytest.mark.parametrize(
    "make_case,expectation",
    [
        (_case_provider, "raises"),
        (_case_tool, "completes"),
        (_case_extension, "completes"),
    ],
    ids=["provider", "tool", "extension"],
)
def test_the_three_error_sources_have_different_contracts(make_case, expectation):
    """The central finding of this file: "error handling" is not one behaviour.

    A provider error is Fail-Early — it raises out of ``prompt()`` uncaught. A
    tool error is caught, wrapped, and fed back to the model — the turn
    completes. An extension observer error is isolated and surfaced (S44) — the
    turn also completes. Each branch below runs the REAL loop/session (a patched
    network boundary, but real tool code / real extension code / real hook
    dispatch), not a mock asserting its own instructions.
    """
    patcher, session_factory = make_case()
    with patcher:
        session = session_factory()
        if expectation == "raises":
            with pytest.raises(RuntimeError, match="provider boom"):
                asyncio.run(session.prompt("hello"))
        else:
            messages = asyncio.run(session.prompt("hello"))
            assert len(messages) > 0


# ---------------------------------------------------------------------------
# Provider error — agent_loop.py's ErrorEvent branch, in depth
# ---------------------------------------------------------------------------


def test_provider_error_event_paints_the_turn_then_raises_with_the_bracket_closed():
    """Drives a real ``ErrorEvent`` through ``AgentLoop._stream_response``.

    Never done anywhere else in the suite: ``test_agent_loop.py`` imports
    ``ErrorEvent`` but never instantiates one. Running it showed the error text IS
    rendered as a message_start/message_end pair (an "Error: …" assistant bubble)
    before the raise — and that ``agent_end`` never fired, because it was only
    reachable by falling out of the while loop. That was the leak
    ``tau-coding-agent/backends.py``'s ``on_branch_end`` docstring describes ("a
    branch whose turn raises... emits no agent_end at all"), and it also left
    ``latency.py``'s bracket open, so the NEXT prompt's ``agent_start`` reported a
    spurious "nested agent_start" anomaly for the rest of the session.

    Now closed from an ``except`` in ``AgentLoop.run``. Still no ``turn_end``: the
    turn was abandoned mid-flight, and one carrying ``tool_results=[]`` would
    assert a turn completed with no tools, which is a claim rather than an
    observation. pi does emit one, but only because a provider error is a value
    there and it has a real final message to attach (agent-loop.ts:342-353).
    """
    events: list[AgentEvent] = []

    async def _emit(e):
        events.append(e)

    loop = AgentLoop(
        config=AgentLoopConfig(model="gpt-4o"),
        emit=_emit,
    )

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_error_stream_simple("Connection refused"),
    ):
        with pytest.raises(RuntimeError, match="Connection refused"):
            asyncio.run(
                loop.run(prompts=[UserMessage(content=[TextContent(text="hi")], timestamp=0)])
            )

    types_seen = [e.type for e in events]
    assert types_seen == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "agent_end",
    ]
    assert events[3].message["content"][0]["text"] == "Error: Connection refused"
    assert "turn_end" not in types_seen

    closing = events[-1]
    assert closing.is_error is True
    assert closing.error == "RuntimeError: Connection refused"


def test_a_normal_close_carries_no_error_so_the_two_are_distinguishable():
    """The other half of the signal: ``ended`` and ``died`` must not look alike.

    An ``agent_end`` that closed a raised loop is the same event type as one that
    closed a finished loop, so ``is_error``/``error`` are the only way a consumer
    can tell them apart.
    """
    events: list[AgentEvent] = []

    async def _emit(e):
        events.append(e)

    loop = AgentLoop(config=AgentLoopConfig(model="gpt-4o"), emit=_emit)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_ok_stream_simple("hi")):
        asyncio.run(loop.run(prompts=[UserMessage(content=[TextContent(text="hi")], timestamp=0)]))

    closing = events[-1]
    assert closing.type == "agent_end"
    assert closing.is_error is False
    assert closing.error is None


def test_a_cancelled_run_still_closes_its_bracket():
    """``abort()`` cancels the task outright, and a cancelled loop used to leave
    ``agent_start`` open exactly like a raised one — the second case
    ``backends.py`` names. ``BaseException`` is caught rather than ``Exception``
    precisely so ``CancelledError`` is covered, and the re-raise keeps the
    cancellation itself intact."""
    events: list[AgentEvent] = []

    async def _emit(e):
        events.append(e)

    loop = AgentLoop(config=AgentLoopConfig(model="gpt-4o"), emit=_emit)

    async def go():
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_never_completes(),
        ):
            task = asyncio.create_task(
                loop.run(prompts=[UserMessage(content=[TextContent(text="hi")], timestamp=0)])
            )
            for _ in range(1000):
                if any(e.type == "turn_start" for e in events):
                    break
                await asyncio.sleep(0)
            else:
                raise AssertionError("the loop never reached its first turn")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(go())

    closing = events[-1]
    assert closing.type == "agent_end"
    assert closing.is_error is True
    assert closing.error == "CancelledError"


def test_provider_error_propagates_uncaught_through_session_prompt():
    """``AgentSession`` adds no error->chat-message conversion of its own.

    This is the specific behaviour the old file's docstring claimed
    ("error event -> error message in chat") and never actually tested.
    Reported, not fixed: no ``src/`` file is touched by this suite.
    """
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_error_stream_simple("upstream 503"),
    ):
        session = _session()
        with pytest.raises(RuntimeError, match="upstream 503"):
            asyncio.run(session.prompt("hello"))
        # The turn never completed, so nothing was persisted either.
        assert session.messages == []


# ---------------------------------------------------------------------------
# Tool error — caught, wrapped, and actually sent back to the model
# ---------------------------------------------------------------------------


def test_tool_error_result_is_the_exact_payload_the_next_llm_call_receives():
    """Closes the "sent to the LLM" half of the claim ``test_agent_loop.py`` left open.

    ``test_agent_loop.py::TestToolErrorHandling`` already proves a failing tool
    produces an ``is_error=True`` ``tool_execution_end`` event (genuine, not a
    mock-only test) but never inspects what the SECOND LLM call was actually
    sent. Captured here via the ``context`` argument ``stream_simple`` receives.
    """
    captured: list = []
    config = AgentLoopConfig(model="gpt-4o")
    loop = AgentLoop(
        config=config,
        tools=[_failing_tool("failing_tool", "disk full")],
    )

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_tool_call_then_text("call_1", "failing_tool", {}, captured),
    ):
        asyncio.run(
            loop.run(prompts=[UserMessage(content=[TextContent(text="do it")], timestamp=0)])
        )

    assert len(captured) == 2  # the tool-call turn, then the follow-up turn
    second_call_messages = captured[1]["messages"]
    # Context messages are a mix of dicts (tool results, already-converted) and
    # pydantic models (the original UserMessage) — only dicts have `.get`.
    tool_result_messages = [
        m for m in second_call_messages if isinstance(m, dict) and m.get("role") == "toolResult"
    ]
    assert len(tool_result_messages) == 1
    result = tool_result_messages[0]
    assert result["is_error"] is True
    assert "disk full" in result["content"][0]["text"]


def test_failing_tool_result_is_persisted_to_the_session_log_as_an_error():
    """Session-level: the error result is not just an event, it lands in the transcript.

    ``test_agent_loop.py`` never checks persistence (it drives the loop directly,
    with no ``AgentSession``); this is the layer the old file's own
    ``test_failing_tool_in_session`` claimed to check but, having no tool
    attached, could not have.
    """
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_tool_call_then_text("call_1", "failing_tool", {}, []),
    ):
        session = _session(tools=[_failing_tool("failing_tool", "permission denied")])
        messages = asyncio.run(session.prompt("please"))

    tool_results = [m for m in messages if m.get("role") == "toolResult"]
    assert len(tool_results) == 1
    assert tool_results[0]["is_error"] is True
    assert "permission denied" in tool_results[0]["content"][0]["text"]
    # And it is durably on the session, not just in the return value.
    persisted_tool_results = [m for m in session.messages if m.get("role") == "toolResult"]
    assert persisted_tool_results == tool_results


# ---------------------------------------------------------------------------
# Extension error — two DIFFERENT hooks, two DIFFERENT failure policies
# ---------------------------------------------------------------------------


def test_a_raising_tool_call_hook_blocks_the_call_fail_closed():
    """The ``tool_call`` veto hook is fail-CLOSED: a raising handler blocks execution.

    ``agent_loop.py``'s ``_prepare_tool_call`` catches the handler's exception and
    returns a ``BlockedCall`` with "Extension failed, blocking execution: …" — a
    code path with, per a repo-wide grep, no test anywhere before this one. It
    sits right next to the fail-OPEN ``tool_result`` hook
    ``test_extension_error_visibility.py`` already covers; the two are opposite
    policies and neither is obvious from reading the event dispatch alone.
    """
    tool_ran: list = []

    def _bad_ext(api):
        def _veto(event, ctx):
            raise RuntimeError("policy engine unavailable")

        api.on("tool_call", _veto)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_tool_call_then_text("call_1", "real_tool", {}, []),
    ):
        session = _session(
            tools=[_succeeding_tool("real_tool", ran=tool_ran)], extensions=[_bad_ext]
        )
        messages = asyncio.run(session.prompt("run it"))

    tool_results = [m for m in messages if m.get("role") == "toolResult"]
    assert len(tool_results) == 1
    assert tool_results[0]["is_error"] is True
    assert "blocking execution" in tool_results[0]["content"][0]["text"]
    assert "policy engine unavailable" in tool_results[0]["content"][0]["text"]
    assert tool_ran == []  # the tool itself never executed


def test_a_raising_tool_result_hook_does_not_discard_the_tool_that_already_succeeded():
    """The ``tool_result`` hook is fail-OPEN: the original result survives.

    ``test_extension_error_visibility.py::test_session_routes_mutating_hook_error_to_tui_delegate``
    already proves the runner surfaces this via ``ctx.ui.notify`` by calling
    ``emit_tool_result`` directly. This is the layer that test does not reach:
    a full turn, through ``_apply_after_hooks``, with a tool that actually ran
    and produced real output — confirming the output is not silently lost
    underneath the exploding hook.
    """

    def _bad_ext(api):
        def _explode(event, ctx):
            raise ValueError("logging sink down")

        api.on("tool_result", _explode)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_tool_call_then_text("call_1", "real_tool", {}, []),
    ):
        session = _session(
            tools=[_succeeding_tool("real_tool", result="the real answer")],
            extensions=[_bad_ext],
        )
        messages = asyncio.run(session.prompt("run it"))

    tool_results = [m for m in messages if m.get("role") == "toolResult"]
    assert len(tool_results) == 1
    assert tool_results[0]["is_error"] is False
    assert tool_results[0]["content"][0]["text"] == "the real answer"


@pytest.mark.usefixtures("fake_llm")
def test_two_independent_bad_extensions_are_isolated_from_each_other_and_the_turn():
    """Multiplicity: two DIFFERENT extensions, raising on two DIFFERENT events, in
    one live turn.

    ``test_event_bus.py``'s ``TestErrorIsolation`` proves the bus isolates
    handlers from each other; ``test_extension_error_visibility.py`` proves ONE
    bad extension surfaces during a real ``prompt()``. Neither exercises TWO
    separately-loaded extensions failing on two different hooks in the same
    turn — this is the one shape the old file's
    ``test_multiple_extension_errors_dont_crash`` covered that nothing else does.
    """

    def _bad_ext_1(api):
        def _raise(event):
            raise ValueError("ext1 boom")

        api.on("agent_start", _raise)

    def _bad_ext_2(api):
        def _raise(event):
            raise ZeroDivisionError("ext2 boom")

        api.on("agent_end", _raise)

    session = _session(extensions=[_bad_ext_1, _bad_ext_2])
    messages = asyncio.run(session.prompt("hello"))
    assert len(messages) > 0
