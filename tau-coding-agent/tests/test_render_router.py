"""The multi-stream render seam — ``TurnStream`` + ``RenderRouter`` (B3-a).

docs/SUBMISSION-LIFECYCLE.md, end of "Phasing". This file pins a demultiplexer
that turns ONE session's whole bus into per-stream render events, so two concurrent
turns — a sub-agent's on its own cursor included — and a turn no frontend
initiated are all representable.

Driven against a real ``AgentSession`` where the wiring is what matters
(``subscribe_render``), and against hand-built events where a specific shape is
(orphans, sub-agent streams, interleaving).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from tau_agent_core.events import AgentEvent
from tau_agent_core.submission import Submission
from tau_coding_agent.backends import (
    DEFAULT_STREAM,
    RenderRouter,
    TauBackend,
    TurnStream,
    prompt_tokens,
)

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000


def _backend() -> TauBackend:
    """A real TauBackend (and therefore a real AgentSession) against no network."""
    return TauBackend(
        {
            "backend": "openai",
            "model": "m",
            "base_url": "http://x/v1",
            "api_key": "not-needed",
            "tools": [],
        }
    )


def _stub_turn(backend: TauBackend) -> None:
    """Replace the agent loop with a scripted emit, keeping REAL admission.

    Same idiom as ``test_tui_submission_source``: ``submit()`` runs for real — the
    turn lock, the provenance stamp, and (since B3-a) the ``submission_start`` /
    ``submission_end`` span the router brackets a stream with — and only the model
    round-trip below it is scripted.
    """
    session = backend.agent_session

    async def fake_run_one_turn(
        text, images, context, queued=None, strip_ref_text=None, persist=True
    ):
        await session._emit_stamped(AgentEvent(type="turn_start", timestamp=_TS, turn_index=0))
        await session._emit_stamped(
            AgentEvent(
                type="message_update",
                timestamp=_TS,
                message={"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            )
        )
        await session._emit_stamped(
            AgentEvent(
                type="message_end",
                timestamp=_TS,
                message={
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 60, "output_tokens": 5, "total_tokens": 65},
                },
            )
        )
        return [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]

    session._run_one_turn = fake_run_one_turn  # type: ignore[method-assign]


def _text_event(stream: str, text: str) -> AgentEvent:
    """A ``message_update`` carrying the full accumulated text, as the loop sends it."""
    return AgentEvent(
        type="message_update",
        timestamp=_TS,
        message={"role": "assistant", "content": [{"type": "text", "text": text}]},
        submission_id=stream,
    )


class TestPromptTokens:
    def test_the_prompt_is_the_total_minus_what_the_model_generated(self):
        assert prompt_tokens({"total_tokens": 512, "output_tokens": 12}) == 500

    def test_it_agrees_with_the_field_sum_on_a_transcript_written_today(self):
        """The two readings are equal once the providers stopped double-counting
        the cached span inside input_tokens — which is what makes the subtraction
        safe to use everywhere rather than only on reload."""
        usage = {
            "input_tokens": 400,
            "cache_read_tokens": 100,
            "output_tokens": 12,
            "total_tokens": 512,
        }
        fields = (
            usage["input_tokens"] + usage["cache_read_tokens"] + usage.get("cache_write_tokens", 0)
        )
        assert prompt_tokens(usage) == fields == 500

    def test_a_legacy_transcript_reads_its_real_size_not_double(self):
        """Written before the provider fix: input_tokens 1404 CONTAINS the 1384
        cached, so the field sum says 2788 for a 1404-token prompt. The server's
        own total, 1620, still knows the truth. Real numbers from a session on
        disk (~/.tau/sessions/…claudish-to-english…)."""
        legacy = {
            "input_tokens": 1404,
            "cache_read_tokens": 1384,
            "output_tokens": 216,
            "total_tokens": 1620,
        }
        assert prompt_tokens(legacy) == 1404

    def test_a_server_that_reported_no_total_falls_to_the_field_sum(self):
        """Usage defaults total_tokens to 0, so 0 means "nothing reported" — not a
        zero-token prompt. Fail-Early: use the fields that ARE there, don't
        report 0 - output as a negative size."""
        assert prompt_tokens({"input_tokens": 300, "output_tokens": 20}) == 300

    def test_a_total_below_output_is_a_contradiction_not_a_small_prompt(self):
        assert prompt_tokens({"input_tokens": 300, "output_tokens": 50, "total_tokens": 10}) == 300

    def test_an_empty_usage_is_zero_not_an_error(self):
        assert prompt_tokens({}) == 0


class TestTurnStream:
    def test_text_deltas_are_the_suffix_beyond_what_this_stream_saw(self):
        """The loop re-sends the whole accumulated partial text every update."""
        stream = TurnStream()
        assert [e["delta"] for e in stream.feed(_text_event("x", "Hel"))] == ["Hel"]
        assert [e["delta"] for e in stream.feed(_text_event("x", "Hello"))] == ["lo"]
        assert stream.feed(_text_event("x", "Hello")) == []  # no actual change
        assert stream.text == "Hello"

    def test_turn_start_resets_the_accumulator_so_turns_do_not_concatenate(self):
        stream = TurnStream()
        stream.feed(_text_event("x", "first"))
        stream.feed(AgentEvent(type="turn_start", timestamp=_TS, turn_index=1, submission_id="x"))
        out = stream.feed(_text_event("x", "second"))
        assert [e["delta"] for e in out] == ["second"]

    def test_every_emitted_event_carries_its_stream(self):
        stream = TurnStream("stream-7")
        out = stream.feed(_text_event("stream-7", "hi"))
        assert out == [{"kind": "text_delta", "delta": "hi", "stream": "stream-7"}]

    def test_default_stream_is_the_single_implicit_one(self):
        assert TurnStream().stream_id == DEFAULT_STREAM

    def test_tool_result_is_matched_onto_the_harvested_call(self):
        stream = TurnStream()
        stream.feed(
            AgentEvent(
                type="message_end",
                timestamp=_TS,
                message={
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "c1", "name": "ls", "arguments": {}}],
                    "usage": {"total_tokens": 11},
                },
                submission_id="x",
            )
        )
        out = stream.feed(
            AgentEvent(
                type="tool_execution_end",
                timestamp=_TS,
                tool_call_id="c1",
                tool_name="ls",
                result="a.py",
                submission_id="x",
            )
        )
        assert out[0]["kind"] == "tool_result" and out[0]["result"] == "a.py"
        assert stream.tool_calls[0]["result"] == "a.py"
        assert stream.usage_totals["total_tokens"] == 11


class TestRenderRouterStreams:
    async def test_two_submissions_never_interleave_into_one_stream(self):
        """The defect this task exists to fix. Two turns streaming at once used to
        be one buffer with one exchange; now each delta names the turn it belongs
        to and a renderer can keep them apart."""
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        a = Submission(text="A", source="interactive", submitter="human", submission_id="a")
        b = Submission(text="B", source="bus", submitter="nats", submission_id="b")

        await router.on_submission_start(submission=a, text="A")
        await router.on_submission_start(submission=b, text="B")
        await router.on_agent_event(_text_event("a", "alpha"))
        await router.on_agent_event(_text_event("b", "beta"))
        await router.on_agent_event(_text_event("a", "alphaX"))
        await router.on_submission_end(submission=b, side_usage={})
        await router.on_submission_end(submission=a, side_usage={})

        by_stream: dict[str, list[str]] = {}
        for event in seen:
            if event["kind"] == "text_delta":
                by_stream.setdefault(event["stream"], []).append(event["delta"])
        assert by_stream == {"a": ["alpha", "X"], "b": ["beta"]}

    async def test_a_non_interactive_stream_is_rendered_not_dropped(self):
        """Jupyter's rule, stated in the spec and easy to get backwards: a frontend
        filters on "is this mine?" to decide HOW to render, and still renders the
        rest. So the router carries provenance and drops nothing."""
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        sub = Submission(
            text="run the nightly",
            source="timer",
            submitter="cron:nightly",
            submission_id="t1",
            correlation={"cron_id": "nightly"},
        )

        await router.on_submission_start(submission=sub, text="run the nightly")
        await router.on_agent_event(_text_event("t1", "working"))
        await router.on_submission_end(submission=sub, side_usage={})

        start = seen[0]
        assert start == {
            "kind": "stream_start",
            "stream": "t1",
            "source": "timer",
            "submitter": "cron:nightly",
            "correlation": {"cron_id": "nightly"},
            "text": "run the nightly",
        }
        assert any(e["kind"] == "text_delta" and e["delta"] == "working" for e in seen)
        end = seen[-1]
        assert end["kind"] == "stream_end" and end["source"] == "timer"
        assert end["submitter"] == "cron:nightly"

    async def test_stream_end_reports_loop_output_plus_the_side_usage_delta(self):
        """``output`` folds in the side-usage delta; ``context`` does not. A side
        call (auto-compaction, ``ctx.complete()``) generates real tokens, so they
        are added — but its 6000-token prompt is a DIFFERENT conversation, so
        adding it to this stream's context would report a size the stream never had."""
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        sub = Submission(text="x", source="interactive", submitter="human", submission_id="s")

        await router.on_submission_start(submission=sub, text="x")
        await router.on_agent_event(
            AgentEvent(
                type="message_end",
                timestamp=_TS,
                message={
                    "role": "assistant",
                    "content": [],
                    "usage": {
                        "input_tokens": 300,
                        "cache_read_tokens": 100,
                        "output_tokens": 40,
                        "total_tokens": 440,
                    },
                },
                submission_id="s",
            )
        )
        await router.on_submission_end(
            submission=sub, side_usage={"input_tokens": 6000, "output_tokens": 2}
        )

        assert seen[-1] == {
            "kind": "stream_end",
            "stream": "s",
            "source": "interactive",
            "submitter": "human",
            "context": 400,
            "output": 42,
            "seconds": None,
            "cache_notice": None,
            "extra": {},
        }

    async def test_stream_end_context_is_the_last_prompt_not_the_sum_of_prompts(self):
        """Two completions in one tool-bearing turn. The second prompt CONTAINS the
        first, so context is 900 — not 1400. Summing them is the overcount that made
        every turn's badge read as the whole preceding conversation."""
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        sub = Submission(text="x", source="interactive", submitter="human", submission_id="s")

        await router.on_submission_start(submission=sub, text="x")
        for prompt, out in ((500, 20), (900, 35)):
            await router.on_agent_event(
                AgentEvent(
                    type="message_end",
                    timestamp=_TS,
                    message={
                        "role": "assistant",
                        "content": [],
                        "usage": {"input_tokens": prompt, "output_tokens": out},
                    },
                    submission_id="s",
                )
            )
        await router.on_submission_end(submission=sub)

        assert seen[-1]["context"] == 900
        assert seen[-1]["output"] == 55

    async def test_an_unstamped_event_is_reported_not_swallowed(self):
        """``continue_conversation()`` and a bare ``compact()`` emit agent_start /
        agent_end with no submission to stamp them. A renderer that dropped those
        in silence would be indistinguishable from one that had stopped working."""
        orphans: list[str] = []
        router = RenderRouter(lambda _e: None, on_orphan=orphans.append)

        await router.on_agent_event(AgentEvent(type="agent_start", timestamp=0))

        assert len(orphans) == 1 and "no submission_id" in orphans[0]

    async def test_an_event_after_its_stream_closed_is_reported_not_swallowed(self):
        orphans: list[str] = []
        router = RenderRouter(lambda _e: None, on_orphan=orphans.append)
        sub = Submission(text="x", source="interactive", submitter="human", submission_id="s")

        await router.on_submission_start(submission=sub, text="x")
        await router.on_submission_end(submission=sub, side_usage={})
        await router.on_agent_event(_text_event("s", "late"))

        assert len(orphans) == 1 and "is not open" in orphans[0]

    async def test_close_all_finishes_streams_a_teardown_abandoned(self):
        """A backend swapped mid-turn must not leave an exchange on "Working…"."""
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        sub = Submission(text="x", source="interactive", submitter="human", submission_id="s")

        await router.on_submission_start(submission=sub, text="x")
        assert router.open_streams == ["s"]
        await router.close_all()

        assert router.open_streams == []
        assert seen[-1]["kind"] == "stream_end" and seen[-1]["stream"] == "s"

    async def test_an_async_handler_is_awaited(self):
        """A Textual renderer mounts widgets, so the handler must be allowed to be
        a coroutine — a fire-and-forget call would drop the mount."""
        seen: list[dict] = []

        async def handler(event: dict) -> None:
            seen.append(event)

        router = RenderRouter(handler)
        sub = Submission(text="x", source="interactive", submitter="human", submission_id="s")
        await router.on_submission_start(submission=sub, text="x")

        assert seen and seen[0]["kind"] == "stream_start"


class _OwnedCursor:
    """The one attribute the router reads off a cursor: who owns it."""

    def __init__(self, owner: object | None) -> None:
        self.owner = owner


class TestRenderRouterSubAgents:
    """A sub-agent's turn is a submission on its own cursor (docs/CURSORS.md §6)."""

    async def test_a_sub_agent_opens_its_own_stream_attributed_to_the_agent(self):
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        sub = Submission(
            text="explore the tests", source="agent", submitter="fork:explore", submission_id="b1"
        )

        await router.on_submission_start(
            submission=sub, text="explore the tests", cursor=_OwnedCursor(owner=object())
        )
        await router.on_agent_event(_text_event("b1", "branching"))

        assert seen[0]["kind"] == "stream_start"
        assert (seen[0]["stream"], seen[0]["source"], seen[0]["submitter"]) == (
            "b1",
            "agent",
            "fork:explore",
        )
        assert seen[1] == {"kind": "text_delta", "delta": "branching", "stream": "b1"}

    async def test_a_sub_agent_and_the_head_turn_are_separate_streams(self):
        """The concurrency a ``fork`` actually produces: the head's turn is untouched
        and a second agent runs beside it, on the same bus."""
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        head = Submission(text="main", source="interactive", submitter="human", submission_id="m")
        branch = Submission(text="go", source="agent", submitter="fork:l", submission_id="b")

        await router.on_submission_start(submission=head, text="main")
        await router.on_agent_event(_text_event("m", "primary"))
        await router.on_submission_start(
            submission=branch, text="go", cursor=_OwnedCursor(owner=object())
        )
        await router.on_agent_event(_text_event("b", "forked"))
        await router.on_agent_event(_text_event("m", "primaryX"))

        streams = {e["stream"] for e in seen if e["kind"] == "text_delta"}
        assert streams == {"m", "b"}
        assert router.open_streams == ["m", "b"]


class TestSubscribeRenderWiring:
    """The seam itself, against a real session: one attach, every turn rendered."""

    async def test_a_real_turn_produces_a_bracketed_stream(self):
        backend = _backend()
        _stub_turn(backend)
        seen: list[dict] = []
        backend.subscribe_render(seen.append)

        await backend.submit_turn(
            Submission(text="hi", source="interactive", submitter="human", submission_id="s1"),
            [],
        )

        kinds = [e["kind"] for e in seen]
        assert kinds[0] == "stream_start" and kinds[-1] == "stream_end"
        assert all(e["stream"] == "s1" for e in seen)
        assert "text_delta" in kinds
        assert seen[-1]["context"] == 60 and seen[-1]["output"] == 5

    async def test_a_second_turn_on_the_same_subscription_gets_its_own_stream(self):
        """The point of a PERSISTENT subscription: no re-attach per turn."""
        backend = _backend()
        _stub_turn(backend)
        seen: list[dict] = []
        backend.subscribe_render(seen.append)

        await backend.submit_turn(
            Submission(text="a", source="interactive", submitter="human", submission_id="s1"), []
        )
        await backend.submit_turn(
            Submission(text="b", source="bus", submitter="nats", submission_id="s2"), []
        )

        streams = [e["stream"] for e in seen if e["kind"] == "stream_start"]
        assert streams == ["s1", "s2"]
        sources = [e["source"] for e in seen if e["kind"] == "stream_start"]
        assert sources == ["interactive", "bus"]

    async def test_detach_stops_the_renderer(self):
        backend = _backend()
        _stub_turn(backend)
        seen: list[dict] = []
        router = backend.subscribe_render(seen.append)

        router.detach()
        await backend.submit_turn(
            Submission(text="a", source="interactive", submitter="human", submission_id="s1"), []
        )

        assert seen == []

    async def test_a_sub_agent_whose_turn_raises_still_closes_its_stream(self):
        """A sub-agent whose provider call fails emits ``agent_start`` and then
        nothing from the loop, so ``submission_end`` — emitted from a ``finally`` —
        is the bracket that closes its stream. A leaked stream is a permanently
        "Working…" exchange."""
        backend = _backend()
        session = backend.agent_session
        await session.cursor.append_message(
            {"role": "user", "content": [{"type": "text", "text": "shared prefix"}]}
        )
        seen: list[dict] = []
        router = backend.subscribe_render(seen.append)

        async def _boom(model, context, options=None):
            raise RuntimeError("the provider dropped the connection")

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_boom):
            result = await session._extension_api.context.spawn_branch(
                session.cursor.leaf, "explore", tools=[]
            )

        assert result.ok is False, "a failing sub-agent is contained, not raised"
        assert router.open_streams == [], "the stream must not be leaked"
        assert seen[0]["kind"] == "stream_start" and seen[-1]["kind"] == "stream_end"
        assert seen[0]["submitter"] == "fork:explore"

    async def test_a_cancelled_sub_agent_still_closes_its_stream(self):
        """``abort()`` cancels every forked task, and ``CancelledError`` is not an
        ``Exception`` — the containment handler never sees it. The stream still closes."""
        backend = _backend()
        session = backend.agent_session
        await session.cursor.append_message(
            {"role": "user", "content": [{"type": "text", "text": "shared prefix"}]}
        )
        seen: list[dict] = []
        router = backend.subscribe_render(seen.append)
        streaming = asyncio.Event()

        async def _hang(model, context, options=None):
            streaming.set()
            await asyncio.sleep(3600)

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_hang):
            task = asyncio.get_running_loop().create_task(
                session._extension_api.context.spawn_branch(
                    session.cursor.leaf, "explore", tools=[]
                )
            )
            await streaming.wait()
            assert len(router.open_streams) == 1

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert router.open_streams == []
        assert seen[-1]["kind"] == "stream_end"

    async def test_submit_turn_returns_the_result_verbatim(self):
        """No streaming plumbing, but the typed in-band answer is still the answer."""
        backend = _backend()
        _stub_turn(backend)
        result = await backend.submit_turn(
            Submission(text="hi", source="interactive", submitter="human", submission_id="s1"), []
        )
        assert result.accepted is True
        assert result.submission_id == "s1"
        assert result.messages
