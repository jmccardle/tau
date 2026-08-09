"""The multi-lane render seam — ``TurnStream`` + ``RenderRouter`` (B3-a).

docs/SUBMISSION-LIFECYCLE.md, end of "Phasing": *"backends.py:200 stream_chat is
single-stream by construction, and nothing yet subscribes to the branch_event
channel, so a fork today is unobservable."* This file pins the replacement — a
demultiplexer that turns ONE session's whole bus into per-lane render events, so
two concurrent turns and a turn no frontend initiated are both representable.

Driven against a real ``AgentSession`` where the wiring is what matters
(``subscribe_render``), and against hand-built events where a specific shape is
(orphans, branch lanes, interleaving).
"""

from __future__ import annotations

import asyncio

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_agent_core.submission import Submission
from tau_coding_agent.backends import DEFAULT_LANE, RenderRouter, TauBackend, TurnStream


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
    ``submission_end`` span the router brackets a lane with — and only the model
    round-trip below it is scripted.
    """
    session = backend.agent_session

    async def fake_run_one_turn(
        text, images, context, queued=None, strip_ref_text=None, persist=True
    ):
        await session._emit_stamped(AgentEvent(type="turn_start", timestamp=0, turn_index=0))
        await session._emit_stamped(
            AgentEvent(
                type="message_update",
                timestamp=0,
                message={"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            )
        )
        await session._emit_stamped(
            AgentEvent(
                type="message_end",
                timestamp=0,
                message={
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"total_tokens": 5},
                },
            )
        )
        return [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]

    session._run_one_turn = fake_run_one_turn  # type: ignore[method-assign]


def _text_event(lane: str, text: str) -> AgentEvent:
    """A ``message_update`` carrying the full accumulated text, as the loop sends it."""
    return AgentEvent(
        type="message_update",
        timestamp=0,
        message={"role": "assistant", "content": [{"type": "text", "text": text}]},
        submission_id=lane,
    )


# ---------------------------------------------------------------------------
# TurnStream — the normalizer, extracted so it can exist once per lane.
# ---------------------------------------------------------------------------


class TestTurnStream:
    def test_text_deltas_are_the_suffix_beyond_what_this_lane_saw(self):
        """The loop re-sends the whole accumulated partial text every update."""
        stream = TurnStream()
        assert [e["delta"] for e in stream.feed(_text_event("x", "Hel"))] == ["Hel"]
        assert [e["delta"] for e in stream.feed(_text_event("x", "Hello"))] == ["lo"]
        assert stream.feed(_text_event("x", "Hello")) == []  # no actual change
        assert stream.text == "Hello"

    def test_turn_start_resets_the_accumulator_so_turns_do_not_concatenate(self):
        stream = TurnStream()
        stream.feed(_text_event("x", "first"))
        stream.feed(AgentEvent(type="turn_start", timestamp=0, turn_index=1, submission_id="x"))
        out = stream.feed(_text_event("x", "second"))
        assert [e["delta"] for e in out] == ["second"]

    def test_every_emitted_event_carries_its_lane(self):
        stream = TurnStream("lane-7")
        out = stream.feed(_text_event("lane-7", "hi"))
        assert out == [{"kind": "text_delta", "delta": "hi", "lane": "lane-7"}]

    def test_default_lane_is_the_single_implicit_one(self):
        assert TurnStream().lane == DEFAULT_LANE

    def test_tool_result_is_matched_onto_the_harvested_call(self):
        stream = TurnStream()
        stream.feed(
            AgentEvent(
                type="message_end",
                timestamp=0,
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
                timestamp=0,
                tool_call_id="c1",
                tool_name="ls",
                result="a.py",
                submission_id="x",
            )
        )
        assert out[0]["kind"] == "tool_result" and out[0]["result"] == "a.py"
        assert stream.tool_calls[0]["result"] == "a.py"
        assert stream.usage_totals["total_tokens"] == 11


# ---------------------------------------------------------------------------
# RenderRouter — the demultiplexer.
# ---------------------------------------------------------------------------


class TestRenderRouterLanes:
    async def test_two_submissions_never_interleave_into_one_lane(self):
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

        by_lane: dict[str, list[str]] = {}
        for event in seen:
            if event["kind"] == "text_delta":
                by_lane.setdefault(event["lane"], []).append(event["delta"])
        assert by_lane == {"a": ["alpha", "X"], "b": ["beta"]}

    async def test_a_non_interactive_lane_is_rendered_not_dropped(self):
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
            "kind": "lane_start",
            "lane": "t1",
            "source": "timer",
            "submitter": "cron:nightly",
            "correlation": {"cron_id": "nightly"},
            "text": "run the nightly",
        }
        assert any(e["kind"] == "text_delta" and e["delta"] == "working" for e in seen)
        end = seen[-1]
        assert end["kind"] == "lane_end" and end["source"] == "timer"
        assert end["submitter"] == "cron:nightly"

    async def test_lane_end_reports_loop_tokens_plus_the_side_usage_delta(self):
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        sub = Submission(text="x", source="interactive", submitter="human", submission_id="s")

        await router.on_submission_start(submission=sub, text="x")
        await router.on_agent_event(
            AgentEvent(
                type="message_end",
                timestamp=0,
                message={"role": "assistant", "content": [], "usage": {"total_tokens": 40}},
                submission_id="s",
            )
        )
        await router.on_submission_end(submission=sub, side_usage={"total_tokens": 2})

        assert seen[-1] == {
            "kind": "lane_end",
            "lane": "s",
            "source": "interactive",
            "submitter": "human",
            "tokens": 42,
            "extra": {},
        }

    async def test_an_unstamped_event_is_reported_not_swallowed(self):
        """``continue_conversation()`` and a bare ``compact()`` emit agent_start /
        agent_end with no submission to stamp them. A renderer that dropped those
        in silence would be indistinguishable from one that had stopped working."""
        orphans: list[str] = []
        router = RenderRouter(lambda _e: None, on_orphan=orphans.append)

        await router.on_agent_event(AgentEvent(type="agent_start", timestamp=0))

        assert len(orphans) == 1 and "no submission_id" in orphans[0]

    async def test_an_event_after_its_lane_closed_is_reported_not_swallowed(self):
        orphans: list[str] = []
        router = RenderRouter(lambda _e: None, on_orphan=orphans.append)
        sub = Submission(text="x", source="interactive", submitter="human", submission_id="s")

        await router.on_submission_start(submission=sub, text="x")
        await router.on_submission_end(submission=sub, side_usage={})
        await router.on_agent_event(_text_event("s", "late"))

        assert len(orphans) == 1 and "is not open" in orphans[0]

    async def test_close_all_finishes_lanes_a_teardown_abandoned(self):
        """A backend swapped mid-turn must not leave an exchange on "Working…"."""
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        sub = Submission(text="x", source="interactive", submitter="human", submission_id="s")

        await router.on_submission_start(submission=sub, text="x")
        assert router.open_lanes == ["s"]
        await router.close_all()

        assert router.open_lanes == []
        assert seen[-1]["kind"] == "lane_end" and seen[-1]["lane"] == "s"

    async def test_an_async_handler_is_awaited(self):
        """A Textual renderer mounts widgets, so the handler must be allowed to be
        a coroutine — a fire-and-forget call would drop the mount."""
        seen: list[dict] = []

        async def handler(event: dict) -> None:
            seen.append(event)

        router = RenderRouter(handler)
        sub = Submission(text="x", source="interactive", submitter="human", submission_id="s")
        await router.on_submission_start(submission=sub, text="x")

        assert seen and seen[0]["kind"] == "lane_start"


class TestRenderRouterBranches:
    """A fork's sub-agent is a second lane — the thing that was unobservable."""

    async def test_a_branch_opens_its_own_lane_attributed_to_the_agent(self):
        seen: list[dict] = []
        router = RenderRouter(seen.append)

        await router.on_branch_event(
            lane="lane-2",
            label="explore the tests",
            event=_text_event("whatever", "branching"),
        )

        assert seen[0] == {
            "kind": "lane_start",
            "lane": "branch:lane-2",
            "source": "agent",
            "submitter": "fork:explore the tests",
            "correlation": {"branch_lane": "lane-2", "branch_label": "explore the tests"},
            "text": "explore the tests",
        }
        assert seen[1] == {"kind": "text_delta", "delta": "branching", "lane": "branch:lane-2"}

    async def test_the_branch_lane_does_not_borrow_the_sub_sessions_provenance(self):
        """The forked session's own ``prompt()`` stamps interactive/human, which
        would be a lie on the primary transcript — nobody typed it."""
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        event = _text_event("sub-submission", "x")
        event.source = "interactive"
        event.submitter = "human"

        await router.on_branch_event(lane="lane-2", label="l", event=event)

        assert seen[0]["source"] == "agent"
        assert seen[0]["submitter"] == "fork:l"

    async def test_a_branch_lane_closes_on_its_terminal_branch_end(self):
        seen: list[dict] = []
        router = RenderRouter(seen.append)

        await router.on_branch_event(lane="lane-2", label="l", event=_text_event("s", "hi"))
        await router.on_branch_event(
            lane="lane-2", label="l", event=AgentEvent(type="agent_end", timestamp=0)
        )
        await router.on_branch_end(lane="lane-2", label="l", error=None)

        assert router.open_lanes == []
        assert seen[-1]["kind"] == "lane_end" and seen[-1]["lane"] == "branch:lane-2"

    async def test_agent_end_alone_does_not_close_a_branch_lane(self):
        """The bracket is ``branch_end``, not ``agent_end``, because
        ``AgentLoop.run`` emits ``agent_end`` AFTER its while loop rather than from
        a ``finally``. Closing on it would mean a branch that raised or was
        cancelled never closed at all — so exactly ONE close arrives, and it
        arrives however the branch ended."""
        seen: list[dict] = []
        router = RenderRouter(seen.append)

        await router.on_branch_event(lane="lane-2", label="l", event=_text_event("s", "hi"))
        await router.on_branch_event(
            lane="lane-2", label="l", event=AgentEvent(type="agent_end", timestamp=0)
        )

        assert router.open_lanes == ["branch:lane-2"]
        assert [e["kind"] for e in seen].count("lane_end") == 0

    async def test_a_branch_end_for_a_lane_that_never_opened_is_reported_not_swallowed(self):
        """A branch that failed before emitting anything rendered nothing, so there
        is no lane to close — but silence there is indistinguishable from a renderer
        that stopped working, so it is reported."""
        orphans: list[str] = []
        router = RenderRouter(lambda _e: None, on_orphan=orphans.append)

        await router.on_branch_end(lane="lane-2", label="l", error="boom")

        assert len(orphans) == 1
        assert "branch:lane-2" in orphans[0] and "boom" in orphans[0]

    async def test_a_branch_and_the_primary_turn_are_separate_lanes(self):
        """The concurrency a ``fork`` submission actually produces: the in-flight
        turn is untouched and a second agent runs beside it."""
        seen: list[dict] = []
        router = RenderRouter(seen.append)
        sub = Submission(text="main", source="interactive", submitter="human", submission_id="m")

        await router.on_submission_start(submission=sub, text="main")
        await router.on_agent_event(_text_event("m", "primary"))
        await router.on_branch_event(lane="lane-2", label="l", event=_text_event("s", "forked"))
        await router.on_agent_event(_text_event("m", "primaryX"))

        lanes = {e["lane"] for e in seen if e["kind"] == "text_delta"}
        assert lanes == {"m", "branch:lane-2"}
        assert router.open_lanes == ["m", "branch:lane-2"]


class TestSubscribeRenderWiring:
    """The seam itself, against a real session: one attach, every turn rendered."""

    async def test_a_real_turn_produces_a_bracketed_lane(self):
        backend = _backend()
        _stub_turn(backend)
        seen: list[dict] = []
        backend.subscribe_render(seen.append)

        await backend.submit_turn(
            Submission(text="hi", source="interactive", submitter="human", submission_id="s1"),
            [],
        )

        kinds = [e["kind"] for e in seen]
        assert kinds[0] == "lane_start" and kinds[-1] == "lane_end"
        assert all(e["lane"] == "s1" for e in seen)
        assert "text_delta" in kinds
        assert seen[-1]["tokens"] == 5

    async def test_a_second_turn_on_the_same_subscription_gets_its_own_lane(self):
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

        lanes = [e["lane"] for e in seen if e["kind"] == "lane_start"]
        assert lanes == ["s1", "s2"]
        sources = [e["source"] for e in seen if e["kind"] == "lane_start"]
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

    async def test_a_branch_whose_turn_raises_still_closes_its_lane(self, monkeypatch):
        """The B3 rework defect, end to end through the real ``branch_event`` /
        ``branch_end`` channels. A sub-agent whose first provider call fails (a
        dropped connection, an ``ErrorEvent`` the loop turns into a ``RuntimeError``)
        emits ``agent_start`` and then nothing — ``AgentLoop.run`` emits
        ``agent_end`` after its while loop, not from a ``finally``. Before the fix
        the lane stayed in ``open_lanes`` for the rest of the session: a
        permanently "Working…" exchange and a LaneStrip entry that never cleared.
        """
        backend = _backend()
        session = backend.agent_session
        session.session_log.append_message(
            {"role": "user", "content": [{"type": "text", "text": "shared prefix"}]}
        )
        seen: list[dict] = []
        router = backend.subscribe_render(seen.append)

        async def _boom(self, text, images=None, context=None):
            await self._events.emit(AgentEvent(type="agent_start", timestamp=0))
            raise RuntimeError("the provider dropped the connection")

        monkeypatch.setattr(AgentSession, "prompt", _boom)
        result = await session._extension_api.context.spawn_branch(
            session.session_log.cursor, "explore", tools=[]
        )

        assert result.ok is False, "a failing branch is contained, not raised"
        assert router.open_lanes == [], "the branch lane must not be leaked"
        assert [e["kind"] for e in seen] == ["lane_start", "lane_end"]
        assert seen[-1]["lane"] == "branch:" + result.lane

    async def test_a_cancelled_branch_still_closes_its_lane(self, monkeypatch):
        """``AgentSession.abort()`` (Esc) cancels every forked task, and
        ``CancelledError`` is not an ``Exception`` — ``spawn_branch``'s containment
        handler never sees it. The lane still has to close."""
        backend = _backend()
        session = backend.agent_session
        session.session_log.append_message(
            {"role": "user", "content": [{"type": "text", "text": "shared prefix"}]}
        )
        seen: list[dict] = []
        router = backend.subscribe_render(seen.append)
        streaming = asyncio.Event()

        async def _hang(self, text, images=None, context=None):
            await self._events.emit(AgentEvent(type="agent_start", timestamp=0))
            streaming.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(AgentSession, "prompt", _hang)
        task = asyncio.get_running_loop().create_task(
            session._extension_api.context.spawn_branch(
                session.session_log.cursor, "explore", tools=[]
            )
        )
        await streaming.wait()
        assert len(router.open_lanes) == 1 and router.open_lanes[0].startswith("branch:")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert router.open_lanes == []
        assert seen[-1]["kind"] == "lane_end"

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
