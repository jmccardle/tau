"""Background results reach the cursor that issued them (docs/BACKGROUND-TOOLS.md §6).

Everything here is a real ``AgentSession`` on an ``InMemorySessionLog`` with the
provider faked at ``stream_simple``. A user text ``go:<q>`` makes the fake model
call ``slow`` with ``background=true``; ``slow`` blocks on a per-``q`` gate the test
releases. Any other context gets the reply ``ok``, held on ``script.hold`` when the
test needs the turn to stay busy.

What is pinned:

* delivery to the issuing cursor, idle or busy, never to the head by default;
* the labelled routes: a moved cursor, a closed sub-agent's live owner, a parked
  result when no cursor can take it;
* the three ways the steer queue used to lose a result: ``abort``, ``rollback``,
  and a turn ending between a steer's busy check and its append;
* the ``bg`` tool: status, wait (which claims the result), cancel, errors;
* a stress run of many completions racing an aborting turn: every result lands
  on the head's path exactly once.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any
from unittest.mock import patch

import pytest
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.cursor import TURN_CURSOR
from tau_agent_core.extensions_builtin import background as bg
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission


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


def _reply(text: str, calls: list[ToolCall] | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text), *(calls or [])],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="toolUse" if calls else "stop",
        timestamp=1_700_000_000_000,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    def __init__(self, message: AssistantMessage) -> None:
        self._message = message

    def __aiter__(self):
        async def _gen():
            text = "".join(b.text for b in self._message.content if isinstance(b, TextContent))
            yield TextDeltaEvent(delta=text, partial=self._message)
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return _gen()

    async def result(self) -> AssistantMessage:
        return self._message

    def abort(self) -> None:
        pass


def _role_text(message: Any) -> tuple[str, str]:
    """``(role, text)`` of a message that may be a dict or a pydantic model."""
    role = message.get("role") if isinstance(message, dict) else getattr(message, "role", "")
    content = (
        message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
    )
    if isinstance(content, str):
        return str(role), content
    parts = []
    for block in content or []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return str(role), "".join(parts)


class _Script:
    """The fake model: ``go:<q>`` backgrounds ``slow(q)``; anything else answers ``ok``."""

    def __init__(self) -> None:
        self.hold: asyncio.Event | None = None
        self.calls = 0

    async def stream(self, model: Any, context: Any, options: Any = None) -> _Stream:
        self.calls += 1
        role, text = _role_text(context["messages"][-1])
        if role == "user" and text.startswith("go:"):
            q = text[3:]
            call = ToolCall(
                id=f"call-{q}-{self.calls}",
                name="slow",
                arguments={"q": q, bg.BACKGROUND_KEY: True},
            )
            return _Stream(_reply("starting", [call]))
        if self.hold is not None:
            await self.hold.wait()
        return _Stream(_reply("ok"))


class _Rig:
    """A session with ``slow`` backgrounded, its gates, and its registry."""

    def __init__(self, *extra: Any) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.signals: dict[str, Any] = {}
        self.notes: list[tuple[str, str]] = []
        holder: dict[str, Any] = {}

        async def _slow(tool_call_id, params, signal=None, on_update=None, ctx=None):
            q = params["q"]
            self.signals[q] = signal
            await self.gate(q).wait()
            return {"content": [{"type": "text", "text": f"result-{q}"}]}

        definition = {
            "name": "slow",
            "label": "Slow",
            "description": "the slow tool",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            "execute": _slow,
        }

        def _factory(api: Any) -> None:
            holder["registry"] = bg.register_backgrounding(api, definitions={"slow": definition})
            api.ui.notify = lambda message, level="info", **_: self.notes.append((message, level))

        self.session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model(),
            system_prompt="",
            tools=[],
            api_key="k",
            extensions=[_factory, *extra],
        )
        self.registry: bg.BackgroundRegistry = holder["registry"]
        self.script = _Script()

    def gate(self, q: str) -> asyncio.Event:
        return self.gates.setdefault(q, asyncio.Event())

    def tool(self, name: str) -> Any:
        return self.session._extension_api._registry._tools[name]

    async def submit(self, text: str, cursor: Any = None, strategy: str = "enqueue") -> Any:
        sub = Submission(
            text=text,
            source="interactive",
            submitter="human",
            submission_id=f"s-{text}-{random.random()}",
            multitask_strategy=strategy,  # type: ignore[arg-type]
        )
        return await self.session.submit(sub, cursor=cursor)


def _path_user_texts(cursor: Any) -> list[str]:
    """Every user message's text on ``cursor``'s path, root first."""
    texts = []
    for message in cursor.context():
        role, text = _role_text(message)
        if role == "user":
            texts.append(text)
    return texts


def _count(cursor: Any, handle: str) -> int:
    """How many user messages on ``cursor``'s path deliver ``handle``."""
    marks = (f"[background {handle}]", f"[background {handle},")
    return sum(1 for t in _path_user_texts(cursor) if any(m in t for m in marks))


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    """Poll ``predicate`` on the loop until it holds, or fail."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


def _settled(rig: _Rig) -> bool:
    finals = {bg.DELIVERED, bg.HELD_OVER, bg.PARKED, bg.CLAIMED, bg.CANCELLED}
    return all(c.state in finals for c in rig.registry.jobs()) and not rig.registry._tasks


@pytest.fixture
def rig() -> _Rig:
    return _Rig()


async def _issue(rig: _Rig, q: str, cursor: Any = None) -> Any:
    """Run a ``go:<q>`` turn to completion on ``cursor``; the job is then running."""
    result = await rig.submit(f"go:{q}", cursor=cursor)
    assert result.accepted
    return rig.registry.jobs()[-1]


class TestIssuingCursor:
    async def test_an_idle_issuer_is_woken_and_the_head_is_untouched(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            await head.append_message({"role": "user", "content": "shared"})
            other = await rig.session.open_cursor(head.leaf, label="client-b")
            call = await _issue(rig, "a", cursor=other)
            assert call.origin_cursor_id == other.id
            rig.gate("a").set()
            await _until(lambda: _settled(rig))

        assert call.state == bg.DELIVERED
        assert call.delivered_to == other.id
        assert _count(other, call.handle) == 1
        assert _count(head, call.handle) == 0
        assert _path_user_texts(other)[-1].startswith(f"[background {call.handle}] slow finished")

    async def test_a_busy_issuer_gets_it_as_a_steer_in_the_same_turn(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            await head.append_message({"role": "user", "content": "shared"})
            other = await rig.session.open_cursor(head.leaf, label="client-b")
            rig.script.hold = asyncio.Event()
            turn = asyncio.create_task(rig.submit("go:a", cursor=other))
            await _until(lambda: bool(rig.registry.jobs()) and other.busy)
            call = rig.registry.jobs()[0]
            rig.gate("a").set()
            await _until(lambda: len(other.steer_queue) == 1)
            rig.script.hold.set()
            await turn
            await _until(lambda: _settled(rig))

        assert call.state == bg.DELIVERED
        assert _count(other, call.handle) == 1
        assert _count(head, call.handle) == 0
        messages = (await turn).messages
        assert any(f"[background {call.handle}" in _role_text(m)[1] for m in messages)

    async def test_a_moved_issuer_is_told_the_call_is_not_on_its_branch(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            await head.append_message({"role": "user", "content": "root"})
            root = head.leaf
            call = await _issue(rig, "a")
            head.move(root)
            rig.gate("a").set()
            await _until(lambda: _settled(rig))

        assert call.state == bg.DELIVERED
        text = _path_user_texts(head)[-1]
        assert "has since left" in text and str(call.origin_leaf) in text


class TestClosedIssuer:
    async def test_a_finished_sub_agent_routes_to_its_live_owner_labelled(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            await head.append_message({"role": "user", "content": "root"})
            branch = await rig.session.spawn(head.leaf, "go:s", tools=["slow"], label="scout")
            assert branch.ok
            call = rig.registry.jobs()[0]
            assert call.origin_cursor_id == branch.cursor_id
            assert all(c.id != branch.cursor_id for c in rig.session.cursors)
            rig.gate("s").set()
            await _until(lambda: _settled(rig))

        assert call.state == bg.DELIVERED
        assert call.delivered_to == head.id
        text = _path_user_texts(head)[-1]
        assert f"from closed cursor {branch.cursor_id} (scout)" in text
        assert "result-s" in text

    async def test_a_closed_unowned_cursor_parks_reports_and_bg_returns_it(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            await head.append_message({"role": "user", "content": "root"})
            other = await rig.session.open_cursor(head.leaf, label="client-b")
            call = await _issue(rig, "c", cursor=other)
            await rig.session.close_cursor(other)
            rig.gate("c").set()
            await _until(lambda: _settled(rig))
            assert call.state == bg.PARKED
            assert any(call.handle in note and level == "warning" for note, level in rig.notes)
            assert _count(head, call.handle) == 0

            out = await rig.tool("bg").execute("t", {"action": "wait", "handle": call.handle})

        assert "result-c" in out["content"][0]["text"]
        assert "also delivered as a message: parked" in out["content"][0]["text"]


class TestTheQueueNoLongerLosesIt:
    async def test_abort_while_queued_holds_it_over_to_the_next_prompt(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            call = await _issue(rig, "a")
            rig.script.hold = asyncio.Event()
            turn = asyncio.create_task(rig.submit("hello"))
            await _until(lambda: head.busy)
            rig.gate("a").set()
            await _until(lambda: len(head.steer_queue) == 1)
            rig.session.abort(head)
            assert head.steer_queue == []
            rig.script.hold.set()
            await turn
            await _until(lambda: _settled(rig))
            assert call.state == bg.HELD_OVER
            assert _count(head, call.handle) == 0
            assert len(head.next_turn_queue) == 1

            rig.script.hold = None
            await rig.submit("next")

        assert _count(head, call.handle) == 1
        assert "held over" in [t for t in _path_user_texts(head) if call.handle in t][0]

    async def test_rollback_while_queued_does_not_lose_it(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            call = await _issue(rig, "a")
            rig.script.hold = asyncio.Event()
            turn = asyncio.create_task(rig.submit("hello"))
            await _until(lambda: head.busy)
            rig.gate("a").set()
            await _until(lambda: len(head.steer_queue) == 1)
            rollback = asyncio.create_task(rig.submit("instead", strategy="rollback"))
            await asyncio.sleep(0)
            assert head.steer_queue == []
            rig.script.hold.set()
            await turn
            await rollback
            await _until(lambda: _settled(rig))
            if call.state == bg.HELD_OVER:
                await rig.submit("next")

        assert _count(head, call.handle) == 1

    async def test_a_turn_ending_inside_the_input_hook_does_not_strand_the_steer(self):
        hook_gate = asyncio.Event()

        def _slow_input(api: Any) -> None:
            async def _hook(event: Any, ctx: Any) -> None:
                if "[background" in str(getattr(event, "text", event)):
                    await hook_gate.wait()

            api.on("input", _hook)

        rig = _Rig(_slow_input)
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            call = await _issue(rig, "a")
            rig.script.hold = asyncio.Event()
            turn = asyncio.create_task(rig.submit("hello"))
            await _until(lambda: head.busy)
            rig.gate("a").set()
            for _ in range(20):
                await asyncio.sleep(0)
            rig.script.hold.set()
            await turn
            assert not head.busy
            hook_gate.set()
            await _until(lambda: _settled(rig))

        assert call.state == bg.DELIVERED
        assert head.steer_queue == []
        assert _count(head, call.handle) == 1
        assert "re-submitted" in call.note


class TestTheBgTool:
    async def test_status_lists_every_job_with_its_cursor(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            call = await _issue(rig, "a")
            out = await rig.tool("bg").execute("t", {"action": "status"})
            rig.gate("a").set()
            await _until(lambda: _settled(rig))
        text = out["content"][0]["text"]
        assert call.handle in text and "running" in text and str(call.origin_cursor_id) in text

    async def test_wait_returns_the_result_and_claims_it(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            call = await _issue(rig, "a")
            waiting = asyncio.create_task(
                rig.tool("bg").execute("t", {"action": "wait", "handle": call.handle})
            )
            await _until(lambda: call.waiters == 1)
            rig.gate("a").set()
            out = await waiting
            await _until(lambda: _settled(rig))
        assert call.state == bg.CLAIMED
        assert "result-a" in out["content"][0]["text"]
        assert _count(head, call.handle) == 0

    async def test_wait_times_out_and_the_result_still_arrives_as_a_message(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            call = await _issue(rig, "a")
            out = await rig.tool("bg").execute(
                "t", {"action": "wait", "handle": call.handle, "timeout_s": 0.05}
            )
            assert "still running" in out["content"][0]["text"]
            rig.gate("a").set()
            await _until(lambda: _settled(rig))
        assert call.state == bg.DELIVERED
        assert _count(head, call.handle) == 1

    async def test_cancel_trips_the_jobs_own_signal_and_delivers_nothing(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            head = rig.session.cursor
            call = await _issue(rig, "a")
            await _until(lambda: "a" in rig.signals)
            out = await rig.tool("bg").execute("t", {"action": "cancel", "handle": call.handle})
        assert call.state == bg.CANCELLED
        assert rig.signals["a"].is_aborted()
        assert "cancelled" in out["content"][0]["text"]
        assert _count(head, call.handle) == 0

    async def test_a_turn_abort_does_not_kill_a_backgrounded_job(self, rig: _Rig):
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
            call = await _issue(rig, "a")
            await _until(lambda: "a" in rig.signals)
            rig.session.abort()
            assert not rig.signals["a"].is_aborted()
            rig.gate("a").set()
            await _until(lambda: _settled(rig))
        assert call.state == bg.DELIVERED

    async def test_unknown_handle_and_bad_action_are_tool_errors(self, rig: _Rig):
        bgtool = rig.tool("bg")
        missing = await bgtool.execute("t", {"action": "wait", "handle": "bg-nope-9"})
        bad = await bgtool.execute("t", {"action": "explode"})
        assert missing["is_error"] and "no background job" in missing["content"][0]["text"]
        assert bad["is_error"]


class TestLoopAffinity:
    async def test_the_registry_refuses_a_second_loop(self, rig: _Rig):
        rig.registry._check_loop("start")
        import threading

        errors: list[BaseException] = []

        def _other() -> None:
            async def _go() -> None:
                rig.registry._check_loop("start")

            try:
                asyncio.run(_go())
            except RuntimeError as exc:
                errors.append(exc)

        thread = threading.Thread(target=_other)
        thread.start()
        thread.join()
        assert errors and "different event loop" in str(errors[0])


async def _stress_once(seed: int, jobs: int = 12) -> tuple[_Rig, list[Any]]:
    """Many completions race a turn that is aborted or rolled back; return the rig and jobs."""
    rng = random.Random(seed)
    rig = _Rig()
    head = rig.session.cursor
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
        calls = []
        for i in range(jobs):
            out = await rig.tool("slow").execute(f"c{i}", {"q": f"q{i}", bg.BACKGROUND_KEY: True})
            calls.append(rig.registry.get(out["details"]["background_handle"]))
        rig.script.hold = asyncio.Event()
        turn = asyncio.create_task(rig.submit("hello"))
        await _until(lambda: head.busy)

        async def _complete(i: int) -> None:
            for _ in range(rng.randint(0, 40)):
                await asyncio.sleep(0)
            rig.gate(f"q{i}").set()

        async def _interrupt() -> None:
            for _ in range(rng.randint(0, 30)):
                await asyncio.sleep(0)
            if rng.random() < 0.5:
                rig.session.abort(head)
                for _ in range(rng.randint(0, 5)):
                    await asyncio.sleep(0)
                assert rig.script.hold is not None
                rig.script.hold.set()
                rig.script.hold = None
            else:
                rollback = asyncio.create_task(rig.submit("instead", strategy="rollback"))
                await asyncio.sleep(0)
                assert rig.script.hold is not None
                rig.script.hold.set()
                rig.script.hold = None
                await rollback

        await asyncio.gather(*(_complete(i) for i in range(jobs)), _interrupt())
        await turn
        await _until(lambda: _settled(rig), timeout=10.0)
        if head.next_turn_queue:
            await rig.submit("flush")
    return rig, calls


class TestStress:
    @pytest.mark.parametrize("seed", range(25))
    async def test_every_result_lands_exactly_once_while_the_turn_is_stopped(self, seed: int):
        rig, calls = await _stress_once(seed)
        head = rig.session.cursor
        counts = {c.handle: _count(head, c.handle) for c in calls}
        assert counts == {c.handle: 1 for c in calls}, [(c.handle, c.state, c.note) for c in calls]
        assert head.steer_queue == [] and head.next_turn_queue == []


def test_turn_cursor_is_what_a_spawned_task_inherits():
    """The mechanism the issuing cursor rides on: a task copies the context it was created in."""

    async def _main() -> Any:
        token = TURN_CURSOR.set("cursor-x")  # type: ignore[arg-type]
        try:
            task = asyncio.create_task(asyncio.sleep(0, result=None))
            seen = asyncio.create_task(_read())
        finally:
            TURN_CURSOR.reset(token)
        await task
        return await seen

    async def _read() -> Any:
        await asyncio.sleep(0)
        return TURN_CURSOR.get()

    assert asyncio.run(_main()) == "cursor-x"
