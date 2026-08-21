"""``multitask_strategy="steer"`` — docs/SUBMISSION-LIFECYCLE.md phase 4.

Steer's contract is one sentence and every test here is an argument about where
its clauses land: *deliver the new content after the current turn's tool calls
complete, but BEFORE the next LLM call*. Not at the turn edge (that is
``enqueue``), not by aborting (that is ``rollback``), and never by parking the
loop on a wait for input — the spec's "Deliberately not adopted" section refuses
that shape by name.

So the assertions are mostly about the REQUEST PAYLOAD: ``_Scripted.calls``
records the message list handed to every ``stream_simple`` call, and "the steer
worked" means the steered text is in call N+1 and absent from call N. "The
transcript records it coherently" is the other half — the steered utterance is a
real user node on the active path, in the order the model saw it, not a hidden
channel into the context.

The concurrency cases (a steer racing a rollback, two steers for one turn, a
steer with no turn in flight, same-task reentrancy) exist because steer sits next
to ``_turn_lock`` / ``_current_turn_token`` / ``_turn_task`` / ``_pre_turn_leaf``,
all four hardened in e05ffdf after a review found two real defects there. None of
those guards is weakened: a steer that finds a turn in flight takes no slot at
all, records no pre-turn leaf and bumps no turn token, and a submission from the
in-flight turn's own asyncio task still raises.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission
from tau_agent_core.tools.base import AgentTool, ToolDefinition


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


def _sub(text: str, submission_id: str, **overrides: Any) -> Submission:
    fields: dict[str, Any] = {
        "text": text,
        "source": "interactive",
        "submitter": "human",
        "submission_id": submission_id,
    }
    fields.update(overrides)
    return Submission(**fields)


def _assistant(text: str, tool_calls: list[ToolCall] | None = None) -> AssistantMessage:
    content: list[Any] = [TextContent(text=text)]
    content.extend(tool_calls or [])
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="toolUse" if tool_calls else "stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    """The minimal ``stream_simple`` return shape.

    Ends with a ``DoneEvent``: ``AgentLoop._stream_response`` returns
    ``event.final`` when it sees one and otherwise reconstructs a text-only
    message from the accumulated deltas — which would silently drop the tool
    calls half these tests depend on.
    """

    def __init__(self, message: AssistantMessage) -> None:
        self._message = message

    def __aiter__(self):
        async def _gen():
            text = "".join(
                block.text for block in self._message.content if isinstance(block, TextContent)
            )
            yield TextDeltaEvent(delta=text, partial=self._message)
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return _gen()

    async def result(self) -> AssistantMessage:
        return self._message

    def abort(self) -> None:
        pass


class _Scripted:
    """A ``stream_simple`` stand-in that replays a script and records every payload.

    ``calls[n]`` is the wire message list of the n-th provider call — the only
    place "the steered content reached the model" can honestly be asserted, as
    opposed to "no exception was raised". ``gate``, when set, holds the FIRST call
    open so a test can submit a steer while a turn is genuinely mid-flight.
    """

    def __init__(self, script: list[AssistantMessage]) -> None:
        self._script = script
        self.calls: list[list[Any]] = []
        self.gate: asyncio.Event | None = None

    async def stream(self, model: Any, context: Any, options: Any = None) -> _Stream:
        self.calls.append(list(context["messages"]))
        if self.gate is not None and len(self.calls) == 1:
            await self.gate.wait()
        index = min(len(self.calls) - 1, len(self._script) - 1)
        return _Stream(self._script[index])


def _texts(messages: list[Any]) -> list[str]:
    """Every text-ish string in a message list, flattened, in order."""
    out: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
        else:
            content = getattr(message, "content", None)
        if isinstance(content, str):
            out.append(content)
            continue
        for block in content or []:
            if isinstance(block, str):
                out.append(block)
                continue
            if isinstance(block, dict):
                text = block.get("text")
            else:
                text = getattr(block, "text", None)
            if isinstance(text, str):
                out.append(text)
    return out


def _joined(messages: list[Any]) -> str:
    return "\n".join(_texts(messages))


def _session(
    tools: list[AgentTool] | None = None, extensions: list[Any] | None = None
) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        tools=tools or [],
        extensions=extensions or [],
    )


def _ping_tool(on_call: Any = None) -> AgentTool:
    """A tool whose only jobs are to take time and to be visibly present."""

    async def _execute(**kwargs: Any) -> str:
        if on_call is not None:
            await on_call()
        return "TOOL-RAN"

    return AgentTool(
        definition=ToolDefinition(
            name="ping",
            label="Ping",
            description="ping",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=_execute,
        )
    )


def _tool_call(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="ping", arguments={})


class TestDeliveryPoint:
    async def test_steer_reaches_the_model_on_the_next_call_not_this_one(self):
        """THE test: call 2's payload carries it, call 1's does not.

        Turn 1 makes a tool call; the steer is submitted while that tool is
        executing — after the LLM call that produced it, before the one that
        consumes its result. Steer's definition is that the content lands in the
        second payload, behind the ``toolResult``.
        """
        released = asyncio.Event()
        steered = asyncio.Event()

        async def _during_tool() -> None:
            released.set()
            await asyncio.wait_for(steered.wait(), timeout=2.0)

        session = _session(tools=[_ping_tool(on_call=_during_tool)])
        provider = _Scripted(
            [_assistant("calling the tool", [_tool_call("tc-1")]), _assistant("done")]
        )

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
            turn = asyncio.create_task(
                session.submit(_sub("do the thing", "a-1", multitask_strategy="enqueue"))
            )
            await asyncio.wait_for(released.wait(), timeout=2.0)

            result = await asyncio.wait_for(
                session.submit(_sub("actually, use ripgrep", "s-1", multitask_strategy="steer")),
                timeout=2.0,
            )
            steered.set()
            turn_result = await asyncio.wait_for(turn, timeout=2.0)

        # Admitted, ran no turn of its own, never blocked, never raised.
        assert result.accepted is True
        assert result.messages == []
        assert result.rejection_reason is None

        assert len(provider.calls) == 2, "steering must not buy an extra LLM call here"
        assert "actually, use ripgrep" not in _joined(provider.calls[0])
        second = _texts(provider.calls[1])
        assert "actually, use ripgrep" in second
        # ...and it lands AFTER the tool result — the "after the current turn's
        # tool calls" half of the sentence.
        assert second.index("TOOL-RAN") < second.index("actually, use ripgrep")

        # The transcript is coherent: a real user node on the active path, in the
        # position the model saw it, and in this submission's returned messages.
        active = _texts(
            ConversationTree(
                session._session_log.entries(), session._session_log.cursor
            ).context_for()
        )
        assert active.index("do the thing") < active.index("actually, use ripgrep")
        assert "actually, use ripgrep" in _joined(turn_result.messages)
        assert session._pending_steer_messages == [], "the queue is drained, not copied"

    async def test_two_steers_for_one_turn_arrive_together_in_order(self):
        """Both are delivered before the SAME next call, in submission order.

        τ divergence from pi, deliberate: pi's ``PendingMessageQueue`` defaults to
        ``one-at-a-time``, so its second steering message waits for a further turn
        boundary. τ's spec sentence is "deliver … before the next LLM call" with no
        per-message rationing, and τ has no ``steeringMode`` knob that would make
        the other choice reachable — shipping one unused would be the worse half of
        the trade.
        """
        released = asyncio.Event()
        steered = asyncio.Event()

        async def _during_tool() -> None:
            released.set()
            await asyncio.wait_for(steered.wait(), timeout=2.0)

        session = _session(tools=[_ping_tool(on_call=_during_tool)])
        provider = _Scripted([_assistant("calling", [_tool_call("tc-1")]), _assistant("done")])

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
            turn = asyncio.create_task(
                session.submit(_sub("go", "a-1", multitask_strategy="enqueue"))
            )
            await asyncio.wait_for(released.wait(), timeout=2.0)
            first = await session.submit(_sub("first steer", "s-1", multitask_strategy="steer"))
            second = await session.submit(_sub("second steer", "s-2", multitask_strategy="steer"))
            steered.set()
            await asyncio.wait_for(turn, timeout=2.0)

        assert (first.accepted, second.accepted) == (True, True)
        assert len(provider.calls) == 2
        payload = _texts(provider.calls[1])
        assert payload.index("first steer") < payload.index("second steer")

    async def test_steer_after_the_last_tool_call_buys_one_more_llm_call(self):
        """ "There is no next LLM call" is not the answer — steering creates one.

        pi's inner loop runs while ``hasMoreToolCalls || pendingMessages.length >
        0`` (agent-loop.ts:173), so content landing during a turn the model just
        ended with plain text keeps the loop alive for exactly one more call.
        Stranding it instead would make ``accepted=True`` a claim the harness does
        not honour.
        """
        session = _session()
        provider = _Scripted([_assistant("all done"), _assistant("ok, ripgrep it is")])
        provider.gate = asyncio.Event()

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
            turn = asyncio.create_task(
                session.submit(_sub("go", "a-1", multitask_strategy="enqueue"))
            )
            # Wait until the provider is inside its (gated) first call, so the
            # steer genuinely arrives during a turn whose output has no tool calls.
            while not provider.calls:
                await asyncio.sleep(0)
            await session.submit(_sub("one more thing", "s-1", multitask_strategy="steer"))
            provider.gate.set()
            result = await asyncio.wait_for(turn, timeout=2.0)

        assert len(provider.calls) == 2, "the text-only turn must not end the loop"
        assert "one more thing" in _texts(provider.calls[1])
        assert "ok, ripgrep it is" in _joined(result.messages)

    async def test_max_turns_leaves_an_undeliverable_steer_on_the_queue(self):
        """The cap owns the decision; the content is neither sent nor lost.

        The loop PEEKS the queue to decide whether a text-only turn continues and
        only drains inside the iteration that delivers — so a ``max_turns`` exit
        cannot strand content in a local the caller never sees. It stays queued for
        the next turn (see the next test).
        """
        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), tools=[], max_turns=1
        )
        provider = _Scripted([_assistant("all done")])
        provider.gate = asyncio.Event()

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
            turn = asyncio.create_task(
                session.submit(_sub("go", "a-1", multitask_strategy="enqueue"))
            )
            while not provider.calls:
                await asyncio.sleep(0)
            await session.submit(_sub("too late", "s-1", multitask_strategy="steer"))
            provider.gate.set()
            await asyncio.wait_for(turn, timeout=2.0)

        assert len(provider.calls) == 1, "max_turns=1 must not be exceeded by a steer"
        assert len(session._pending_steer_messages) == 1, "and the content is not dropped"


class TestNoTurnInFlight:
    async def test_steer_with_nothing_running_takes_the_slot_and_runs(self):
        """pi's split: ``streamingBehavior`` is consulted only ``if (isStreaming)``.

        With no turn running, "before the next LLM call" is the call this
        submission is about to make. Running it is the contract, not a degradation
        to ``enqueue`` — the difference between the two would only be observable if
        there were something to wait for, and there is not.
        """
        session = _session()
        provider = _Scripted([_assistant("answered")])

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
            result = await session.submit(_sub("go", "s-1", multitask_strategy="steer"))

        assert result.accepted is True
        assert result.messages, "a steer with nothing to steer runs its own turn"
        assert len(provider.calls) == 1
        assert "go" in _texts(provider.calls[0])
        assert session._pending_steer_messages == []
        assert session._turn_lock.locked() is False
        assert session.is_streaming is False

        active = _joined(
            ConversationTree(
                session._session_log.entries(), session._session_log.cursor
            ).context_for()
        )
        assert "go" in active and "answered" in active

    async def test_a_queued_steer_is_delivered_at_the_start_of_the_next_turn(self):
        """Queued while idle: still "before the next LLM call", just a later one.

        Reached through the ``_queue_message`` seam — what an in-turn hook uses —
        so this also pins that the two doors land in the same place.
        """
        session = _session()
        session._queue_message("read the config first", deliver_as="steer")
        provider = _Scripted([_assistant("done")])

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
            await session.submit(_sub("go", "a-1", multitask_strategy="enqueue"))

        payload = _texts(provider.calls[0])
        assert payload.index("go") < payload.index("read the config first")
        assert session._pending_steer_messages == []


class TestAbortAndRollback:
    async def test_rollback_discards_content_steered_at_the_turn_it_aborts(self):
        """pi's ``abort()`` clears the steering queue; a rollback must too.

        The queue is drained by whichever loop runs NEXT, and after a rollback that
        is the replacement turn this very submission starts — so carrying the steer
        over would inject an utterance aimed at a turn the submitter explicitly
        rolled back past.
        """
        session = _session()
        provider = _Scripted([_assistant("aborted-ish"), _assistant("replacement")])
        provider.gate = asyncio.Event()

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
            turn = asyncio.create_task(
                session.submit(_sub("original", "a-1", multitask_strategy="enqueue"))
            )
            while not provider.calls:
                await asyncio.sleep(0)
            await session.submit(_sub("steered", "s-1", multitask_strategy="steer"))
            assert len(session._pending_steer_messages) == 1

            rollback = asyncio.create_task(
                session.submit(_sub("scratch that", "r-1", multitask_strategy="rollback"))
            )
            await asyncio.sleep(0)
            provider.gate.set()
            await asyncio.wait_for(turn, timeout=2.0)
            rollback_result = await asyncio.wait_for(rollback, timeout=2.0)

        assert rollback_result.accepted is True
        for call in provider.calls:
            assert "steered" not in _joined(call)
        assert session._pending_steer_messages == []

    async def test_abort_clears_the_steering_queue_and_only_that_one(self):
        """``AgentSession.abort()`` → pi's ``clearSteeringQueue()``.

        The followUp/nextTurn queues are deliberately untouched: pre-existing
        behaviour with their own drain points, and phase 4 is not the place to
        change them.
        """
        session = _session()
        session._queue_message("steer me", deliver_as="steer")
        session._queue_message("follow me", deliver_as="followUp")
        session._queue_message("next me", deliver_as="nextTurn")

        session.abort()

        assert session._pending_steer_messages == []
        assert session._pending_follow_up_messages == ["follow me"]
        assert session._pending_next_turn_messages == ["next me"]


class TestReentrancy:
    async def test_a_steer_from_the_in_flight_turns_own_task_still_raises(self):
        """The e05ffdf guard is NOT weakened to let steer through.

        A steer from another task takes no lock and cannot deadlock, so exempting
        it here would look free. Refused: the guard is about the ADMISSION door,
        and a hook steering its own turn through it gains an unbounded loop
        (steer → the loop takes another turn → the hook fires again → steer) that
        decision 3's depth cap cannot see, because a queued steer publishes no new
        driving depth. The seam for that case is
        ``ctx.send_user_message(deliver_as="steer")`` — it starts no turn and
        claims no admission (test_deferral_queue.py).
        """
        holder: dict[str, AgentSession] = {}

        def _ext(api: Any) -> None:
            async def _on_input(event: dict[str, Any], ctx: Any) -> None:
                await holder["session"].submit(_sub("mid-turn", "s-1", multitask_strategy="steer"))

            api.on("input", _on_input)

        session = _session(extensions=[_ext])
        holder["session"] = session
        surfaced: list[Any] = []
        session._extension_runner.on_error(lambda err: surfaced.append(err))
        provider = _Scripted([_assistant("done")])

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
            # The `input` hook is dispatched from the turn's own task (unlike a
            # tool, which parallel execution hands to a gather-created one), so
            # this is the exact same-task shape test_submit_reentrant.py pins for
            # the other strategies. emit_input is Fail-Closed: the handler's
            # exception is SURFACED and dispatch continues, so the outer turn
            # still completes — what matters is that it completes AT ALL.
            await asyncio.wait_for(
                session.submit(_sub("go", "a-1", multitask_strategy="enqueue")), timeout=2.0
            )

        assert len(surfaced) == 1
        assert "reentrant self-submission" in surfaced[0].error
        assert session._pending_steer_messages == [], "a refused steer queues nothing"
        assert "mid-turn" not in _joined(provider.calls[0])


class TestInputPipeline:
    async def test_a_queued_steer_still_runs_the_input_hook_chain(self):
        """A steer is not the one input source that skips the transform pipeline.

        docs/SUBMISSION-LIFECYCLE.md's whole premise is that every submitter gets
        the same parsing/transform chain; pi emits ``input`` (and dispatches
        extension commands) BEFORE it looks at ``streamingBehavior`` for exactly
        this reason. The hook also sees the STEER's own provenance, not the
        in-flight turn's.
        """
        released = asyncio.Event()
        steered = asyncio.Event()

        async def _during_tool() -> None:
            released.set()
            await asyncio.wait_for(steered.wait(), timeout=2.0)

        seen: list[tuple[str, str]] = []

        def _ext(api: Any) -> None:
            def _on_input(event: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
                seen.append((event["source"], event["submitter"]))
                return {"prompt": event["prompt"].upper()}

            api.on("input", _on_input)

        session = _session(tools=[_ping_tool(on_call=_during_tool)], extensions=[_ext])
        provider = _Scripted([_assistant("calling", [_tool_call("tc-1")]), _assistant("done")])

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
            turn = asyncio.create_task(
                session.submit(_sub("go", "a-1", multitask_strategy="enqueue"))
            )
            await asyncio.wait_for(released.wait(), timeout=2.0)
            await session.submit(
                _sub("quietly", "s-1", multitask_strategy="steer", source="bus", submitter="nats")
            )
            steered.set()
            await asyncio.wait_for(turn, timeout=2.0)

        assert ("bus", "nats") in seen, "the hook sees the steer's own provenance"
        assert "QUIETLY" in _texts(provider.calls[1])
