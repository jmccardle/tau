"""B3-a — the TUI renders from a persistent bus subscription, not one awaited stream.

docs/SUBMISSION-LIFECYCLE.md, end of "Phasing": *"backends.py:200 stream_chat is
single-stream by construction ... so a fork today is unobservable."* That was a
SIGNATURE gap, not a rendering one — one awaited call, one buffer, one exchange, so a second concurrent
agent and a turn the TUI never initiated had nowhere to go.

What these pin:

* a single ordinary typed turn renders exactly as it did before — one user bubble,
  the answer promoted out, the summary stamped from real usage;
* two submissions with different ``submission_id``s do not interleave into one
  another's transcript;
* a turn from a NON-interactive source is rendered — badged with its origin, not
  filtered out (the spec's Jupyter rule, which it warns is easy to get backwards);
* a sub-agent's turn — a submission on an owned cursor (docs/CURSORS.md §6) —
  renders too, attributed to the agent;
* Esc still aborts the turn that is generating, and only it.

B3-b continues in the same file, because it is the same subject: having made a
foreign stream *renderable*, make it *unmistakable*. Its additions pin that a stream's
origin survives every stage of the render — the bubble is typed by its source, the
streaming steps and the promoted answer wear the stream's badge and its CSS class,
and a strip above the footer says out loud that something the user did not type is
running. Plus the negative half, which is just as load-bearing: an ordinary typed
turn is left completely unbadged, and a source this build has never heard of
renders generically rather than being dropped.

Driven through the real app via ``App.run_test()`` against a real ``TauBackend``
(and therefore a real ``AgentSession`` and a real ``RenderRouter``) whose agent
loop is scripted — the same idiom as ``test_tui_submission_source``: everything
``submit()`` does runs for real, only the model round-trip is canned.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from tau_agent_core.events import AgentEvent
from tau_agent_core.submission import Submission
from tau_coding_agent.app import TauApp
from tau_coding_agent.backends import TauBackend
from tau_coding_agent.chat_widgets import ExchangeBox, MessageBox
from tau_coding_agent import editor_widgets, transcript

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000


class _Submit:
    """Duck-typed Input.Submitted — on_input_submitted only reads ``.value``."""

    def __init__(self, value: str) -> None:
        self.value = value


def _script(backend: TauBackend, gate: asyncio.Event | None = None) -> None:
    """Replace the agent loop with a scripted emit, keeping REAL admission.

    ``submit()`` — the turn lock, the provenance stamp, the ``submission_start`` /
    ``submission_end`` span the router brackets a stream with — runs for real. When
    ``gate`` is given the turn blocks on it, which is how a test observes two streams
    live at once.
    """
    session = backend.agent_session

    async def fake_run_one_turn(
        text, images, context, queued=None, strip_ref_text=None, persist=True
    ):
        if gate is not None:
            await gate.wait()
        answer = f"answer to {text}"
        await session._emit_stamped(AgentEvent(type="turn_start", timestamp=_TS, turn_index=0))
        await session._emit_stamped(
            AgentEvent(
                type="message_update",
                timestamp=_TS,
                message={"role": "assistant", "content": [{"type": "text", "text": answer}]},
            )
        )
        await session._emit_stamped(
            AgentEvent(
                type="message_end",
                timestamp=_TS,
                message={
                    "role": "assistant",
                    "content": [{"type": "text", "text": answer}],
                    "usage": {"input_tokens": 80, "output_tokens": 7, "total_tokens": 87},
                },
            )
        )
        return [{"role": "assistant", "content": [{"type": "text", "text": answer}]}]

    session._run_one_turn = fake_run_one_turn  # type: ignore[method-assign]


@pytest.fixture
def scripted(make_app):
    """A TauApp wired to a real TauBackend with a scripted loop, plus its gate."""
    gate = asyncio.Event()
    gate.set()  # ungated by default; a test that wants to block clears it
    holder: dict[str, TauBackend] = {}

    def _create(_cfg):
        backend = TauBackend(
            {
                "backend": "openai",
                "model": "m",
                "base_url": "http://x/v1",
                "api_key": "not-needed",
                "tools": [],
            }
        )
        _script(backend, gate)
        holder["backend"] = backend
        return backend

    return make_app(create_backend=_create), holder, gate


async def _until(pilot, predicate, tries: int = 200) -> None:
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause()
    raise AssertionError("condition never became true")


class _OwnedCursor:
    """A cursor as the router reads it: owned, so its submission is a sub-agent's."""

    owner = object()


def _fork(submission_id: str, label: str) -> Submission:
    """The submission ``AgentSession.spawn`` admits for a sub-agent labelled ``label``."""
    return Submission(
        text=label, source="agent", submitter=f"fork:{label}", submission_id=submission_id
    )


def _top_level(display: transcript.ChatDisplay) -> list:
    """The display's top-level TRANSCRIPT boxes, in order.

    ``ChatPlaceholder`` is filtered out because it is chrome, not transcript: it
    is composed once and hidden for the whole life of a non-empty chat, so it is
    child 0 forever and would shift every index here by one while carrying no
    message. What this helper is asked about is what the user's conversation
    rendered as.
    """
    return [c for c in display.children if not isinstance(c, transcript.ChatPlaceholder)]


_NOT_A_BUBBLE = {"assistant", "pending", "toolCall", "toolResult", "system", "custom"}


def _user_boxes(display: transcript.ChatDisplay) -> list[MessageBox]:
    """Every submission bubble, whatever source opened its stream."""
    return [b for b in display.query(MessageBox) if b.role not in _NOT_A_BUBBLE]


async def test_one_ordinary_turn_renders_exactly_as_before(scripted, wait_for_workers_settled):
    """One user bubble, then the answer promoted out below the (unwrapped, no-tool)
    exchange, with the real token count on it. The user bubble now arrives via
    ``stream_start`` rather than being drawn by ``on_input_submitted``, which is the
    change; that it is indistinguishable from before is the requirement."""
    app, _holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()

        await app.on_input_submitted(_Submit("hello"))
        await wait_for_workers_settled(app)
        await pilot.pause()

        display = app.query_one(transcript.ChatDisplay)
        top = _top_level(display)
        assert isinstance(top[0], MessageBox) and top[0].role == "user"
        assert top[0].content_text == "hello"
        # No badge: a human typed this at this frontend.
        assert top[0].border_subtitle in (None, "")
        assert isinstance(top[1], MessageBox) and top[1].role == "assistant"
        assert top[1].content_text == "answer to hello"
        # A no-tool span is unwrapped entirely — no ExchangeBox left behind.
        assert not list(display.query(ExchangeBox))
        # Real usage, on the answer's subtitle (the unwrapped-span path).
        assert "80 ctx · 7 out" in (top[1].border_subtitle or "")
        assert app.is_generating is False


async def test_a_dispatched_command_still_renders_no_user_turn(scripted):
    """``submit()`` emits no span for a command, so nothing opens a stream — which is
    what replaces ``on_input_submitted``'s peek-then-render dance."""
    app, _holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()

        await app.on_input_submitted(_Submit("/extensions"))
        await pilot.pause()

        display = app.query_one(transcript.ChatDisplay)
        assert _user_boxes(display) == []
        assert app.is_generating is False


async def test_two_streams_do_not_interleave_into_one_transcript(scripted):
    """The defect. Two turns streaming at once used to share one ``_exchange``,
    one ``_active_box`` and one tool-route table, so their deltas landed in
    whichever box was current and each finalized the other's exchange."""
    app, _holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()

        a = {"kind": "stream_start", "stream": "a", "source": "interactive", "submitter": "human"}
        b = {"kind": "stream_start", "stream": "b", "source": "bus", "submitter": "nats"}
        await app._on_render_event({**a, "text": "first"})
        await app._on_render_event({**b, "text": "second"})
        for stream in ("a", "b"):
            await app._on_render_event({"kind": "turn_start", "stream": stream, "turn_index": 0})
        # Interleaved deltas, the shape two concurrent streams actually produce.
        await app._on_render_event({"kind": "text_delta", "stream": "a", "delta": "AAA"})
        await app._on_render_event({"kind": "text_delta", "stream": "b", "delta": "BBB"})
        await app._on_render_event({"kind": "text_delta", "stream": "a", "delta": "aaa"})
        await pilot.pause()
        await app._on_render_event(
            {"kind": "stream_end", "stream": "a", "context": 90, "output": 3, "extra": {}}
        )
        await app._on_render_event(
            {"kind": "stream_end", "stream": "b", "context": 90, "output": 5, "extra": {}}
        )
        await pilot.pause()

        display = app.query_one(transcript.ChatDisplay)
        answers = [b.content_text for b in display.query(MessageBox) if b.role == "assistant"]
        assert "AAAaaa" in answers, answers
        assert "BBB" in answers, answers
        # Neither stream's text leaked into the other's box.
        assert not any("BBB" in text and "AAA" in text for text in answers)
        # Two user bubbles, in submission order, each with its own origin.
        users = _user_boxes(display)
        assert [u.content_text for u in users] == ["first", "second"]


async def test_a_tool_result_folds_into_its_own_streams_box(scripted):
    """Two streams with the SAME tool_call_id: a shared route table would fold one
    stream's result into the other's box."""
    app, _holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()

        for stream, source in (("a", "interactive"), ("b", "bus")):
            await app._on_render_event(
                {
                    "kind": "stream_start",
                    "stream": stream,
                    "source": source,
                    "submitter": "human" if source == "interactive" else "nats",
                    "text": stream,
                }
            )
            await app._on_render_event({"kind": "turn_start", "stream": stream, "turn_index": 0})
            await pilot.pause()
            await app._on_render_event(
                {"kind": "tool_call", "stream": stream, "id": "c1", "name": "ls", "arguments": {}}
            )
            await pilot.pause()
        await app._on_render_event(
            {"kind": "tool_result", "stream": "a", "id": "c1", "name": "ls", "result": "A-RESULT"}
        )
        await pilot.pause()

        display = app.query_one(transcript.ChatDisplay)
        stream_a = display.active_step("a")
        stream_b = display.active_step("b")
        assert stream_a is not None and stream_b is not None
        assert stream_a.tool_boxes["c1"].has_result is True
        assert stream_b.tool_boxes["c1"].has_result is False


async def test_a_bus_submission_is_rendered_not_dropped(scripted):
    """A turn the TUI never initiated. Before B3-a this had no representation at
    all: nothing was awaiting it, so nothing drew it."""
    app, holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        backend = holder["backend"]

        await backend.agent_session.submit(
            Submission(
                text="run the nightly",
                source="timer",
                submitter="cron:nightly",
                submission_id="t1",
                multitask_strategy="enqueue",
            )
        )
        await pilot.pause()

        display = app.query_one(transcript.ChatDisplay)
        users = _user_boxes(display)
        assert [u.content_text for u in users] == ["run the nightly"]
        assert users[0].role == "timer"
        assert users[0].border_subtitle == "timer · cron:nightly"
        answers = [b.content_text for b in display.query(MessageBox) if b.role == "assistant"]
        assert "answer to run the nightly" in answers


async def test_a_forked_branch_gets_its_own_labelled_stream(scripted):
    """A sub-agent's turn is an ordinary submission on the same bus, so it gets a
    stream like any other — attributed to the agent (``agent · fork:<label>``), not
    to the human who typed the head's turn."""
    app, holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        events = holder["backend"].agent_session._events

        await events.emit_channel(
            "submission_start",
            submission=_fork("sub-2", "explore"),
            text="explore",
            cursor=_OwnedCursor(),
        )
        await events.emit(
            AgentEvent(type="turn_start", timestamp=_TS, turn_index=0, submission_id="sub-2")
        )
        await events.emit(
            AgentEvent(
                type="message_update",
                timestamp=_TS,
                message={"role": "assistant", "content": [{"type": "text", "text": "forked"}]},
                submission_id="sub-2",
            )
        )
        await pilot.pause()

        display = app.query_one(transcript.ChatDisplay)
        users = _user_boxes(display)
        assert users[0].role == "agent"
        assert users[0].border_subtitle == "agent · fork:explore"
        exchange = display.query(ExchangeBox).first()
        assert "agent · fork:explore" in exchange.title
        step = display.active_step("sub-2")
        assert step is not None and step.content_text == "forked"


async def test_a_failed_fork_closes_its_stream_instead_of_hanging_on_working(scripted):
    """A sub-agent whose first provider call fails must not leave an ExchangeBox
    titled "agent · fork:… · Working…" and a "⑂ 1 other stream" strip behind.

    ``submission_end`` is emitted from a ``finally``, so it closes the stream however
    the turn ended. Driven through the real ``ctx.spawn_branch`` and the real
    agent loop, with only the provider call patched to fail.
    """
    app, holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        session = holder["backend"].agent_session
        del session._run_one_turn  # the sub-agent runs the real loop, not the script
        await session.cursor.append_message(
            {"role": "user", "content": [{"type": "text", "text": "shared prefix"}]}
        )

        async def _boom(model, context, options=None):
            raise RuntimeError("the provider dropped the connection")

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_boom):
            result = await session._extension_api.context.spawn_branch(
                session.cursor.leaf, "explore", tools=[]
            )
        await pilot.pause()

        assert result.ok is False, "the failure is contained, as it always was"
        display = app.query_one(transcript.ChatDisplay)
        assert display._streams == {}, "the stream's render state must not be leaked"
        titles = [e.title for e in display.query(ExchangeBox)]
        assert not any("Working…" in t for t in titles), titles
        assert app.query_one(editor_widgets.StreamStrip).streams == {}
        assert app.query_one(editor_widgets.StreamStrip).display is False


async def test_esc_aborts_the_generating_turn_and_leaves_a_foreign_stream_alone(
    scripted, wait_for_workers_settled
):
    app, holder, gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()
        gate.clear()  # the typed turn will block inside its loop

        await app.on_input_submitted(_Submit("hello"))
        await _until(pilot, lambda: app.is_generating)
        backend = holder["backend"]

        # A second, foreign stream is live at the same time.
        await app._on_render_event(
            {
                "kind": "stream_start",
                "stream": "bus-1",
                "source": "bus",
                "submitter": "nats",
                "text": "from the bus",
            }
        )
        await pilot.pause()

        aborted: list[bool] = []
        original_abort = backend.agent_session.abort
        backend.agent_session.abort = lambda: (  # type: ignore[method-assign]
            aborted.append(True),
            original_abort(),
        )[1]

        app.action_cancel_generation()
        assert aborted == [True], "Esc aborts the session's in-flight turn"

        gate.set()
        await wait_for_workers_settled(app)
        await pilot.pause()

        display = app.query_one(transcript.ChatDisplay)
        # The foreign stream is untouched by the cancel: still open, still "Working…".
        assert "bus-1" in display._streams
        titles = [e.title for e in display.query(ExchangeBox)]
        assert any("Working…" in t for t in titles), titles
        assert app.is_generating is False


def test_stream_role_types_a_bubble_by_its_source_and_never_invents_one():
    """Pure. The bubble's role IS the submission source, so ``ROLE_LABELS`` gives
    it a border title of its own; an unlisted source passes through verbatim (and
    ``MessageBox.on_mount`` capitalizes it) rather than being mapped to a known
    one, and a missing source says ``unknown`` rather than borrowing ``user``."""
    assert TauApp._stream_role("bus") == "bus"
    assert TauApp._stream_role("agent") == "agent"
    # Novel source: rendered generically, NOT dropped and NOT relabelled.
    assert TauApp._stream_role("carrier-pigeon") == "carrier-pigeon"
    assert TauApp._stream_role(None) == "unknown"
    assert TauApp._stream_role("   ") == "unknown"


def test_stream_strip_reports_only_foreign_streams_and_collapses_when_idle():
    """Pure. The strip costs zero rows on an ordinary session: this frontend's own
    typed stream (``label=None``) is not something it reports, because the reader is
    already looking at it and the input is already disabled."""
    strip = editor_widgets.StreamStrip()
    assert strip.display is False

    strip.open_stream("mine", None)
    assert strip.streams == {} and strip.display is False

    strip.open_stream("b1", "bus · nats")
    strip.open_stream("f1", "agent · fork:explore")
    assert list(strip.streams) == ["b1", "f1"]
    assert strip.display is True
    rendered = strip.summary
    assert "bus · nats" in rendered and "agent · fork:explore" in rendered
    assert "2 other streams" in rendered

    strip.close_stream("mine")  # never tracked — a no-op, not an error
    strip.close_stream("b1")
    assert "1 other stream" in strip.summary

    strip.close_stream("f1")
    assert strip.display is False and strip.streams == {}


async def test_a_forks_answer_stays_attributed_after_its_exchange_is_unwrapped(scripted):
    """The defect B3-b fixes. A no-tool span is UNWRAPPED at finalize — the
    ExchangeBox that carried the fork's label is removed and the answer is
    promoted to top level, next to the primary transcript. Before this, the box a
    reader was left with said "Assistant" and nothing else, which is precisely
    mistaking a sub-agent's text for the main agent's.

    Driven through the bus channels a sub-agent's submission uses —
    ``submission_start`` with its owned cursor, its stamped events, and
    ``submission_end`` — so the router's stream bracket is exercised too.
    """
    app, holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        events = holder["backend"].agent_session._events
        fork = _fork("sub-9", "explore")

        async def branch(**fields) -> None:
            await events.emit(AgentEvent(timestamp=_TS, submission_id="sub-9", **fields))

        await events.emit_channel(
            "submission_start", submission=fork, text="explore", cursor=_OwnedCursor()
        )
        await branch(type="turn_start", turn_index=0)
        await branch(
            type="message_update",
            message={"role": "assistant", "content": [{"type": "text", "text": "sub-answer"}]},
        )
        await pilot.pause()

        display = app.query_one(transcript.ChatDisplay)
        step = display.active_step("sub-9")
        assert step is not None
        assert step.has_class(transcript.STREAM_FOREIGN_CLASS)
        assert step.border_subtitle == "agent · fork:explore"

        await branch(
            type="message_end",
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "sub-answer"}],
                "usage": {"input_tokens": 90, "output_tokens": 11, "total_tokens": 101},
            },
        )
        await branch(type="agent_end")
        await events.emit_channel("submission_end", submission=fork, side_usage={})
        await pilot.pause()

        # The exchange really is gone (no tools to group) …
        assert not list(display.query(ExchangeBox))
        # … and the answer it left behind still says whose it is.
        answers = [b for b in display.query(MessageBox) if b.role == "assistant"]
        assert [b.content_text for b in answers] == ["sub-answer"]
        assert answers[0].has_class(transcript.STREAM_FOREIGN_CLASS)
        subtitle = answers[0].border_subtitle or ""
        assert subtitle.startswith("agent · fork:explore · ")
        assert "90 ctx · 11 out" in subtitle


async def test_an_ordinary_typed_turn_carries_no_badge_at_all(scripted, wait_for_workers_settled):
    """The quiet half of the rule. A badge on every message the user typed
    themselves is noise, so ``interactive``/``human`` renders exactly as it always
    has — no stream class, no origin subtitle, no strip."""
    app, _holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()

        await app.on_input_submitted(_Submit("hello"))
        await wait_for_workers_settled(app)
        await pilot.pause()

        display = app.query_one(transcript.ChatDisplay)
        boxes = list(display.query(MessageBox))
        assert boxes, "the turn rendered"
        assert not any(b.has_class(transcript.STREAM_FOREIGN_CLASS) for b in boxes)
        bubble = _user_boxes(display)[0]
        assert bubble.role == "user"
        assert bubble.border_subtitle in (None, "")
        # The answer's subtitle is the stats line and ONLY the stats line.
        answer = [b for b in boxes if b.role == "assistant"][0]
        assert (answer.border_subtitle or "").startswith("80 ctx · 7 out")
        assert app.query_one(editor_widgets.StreamStrip).display is False


async def test_a_source_this_build_never_heard_of_still_renders(scripted):
    """Do not filter. A renderer that hides what it does not recognise is the
    failure mode the whole submission lifecycle exists to prevent, so an unknown
    source gets a generic attribution — its own name — and every foreign-stream
    affordance, rather than being dropped or quietly recoloured as a user turn."""
    app, _holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()

        await app._on_render_event(
            {
                "kind": "stream_start",
                "stream": "x1",
                "source": "carrier-pigeon",
                "submitter": "coop-3",
                "text": "a message arrived by bird",
            }
        )
        await app._on_render_event({"kind": "turn_start", "stream": "x1", "turn_index": 0})
        await pilot.pause()
        await app._on_render_event({"kind": "text_delta", "stream": "x1", "delta": "coo"})
        await pilot.pause()

        display = app.query_one(transcript.ChatDisplay)
        bubble = _user_boxes(display)[0]
        assert bubble.content_text == "a message arrived by bird"
        assert bubble.role == "carrier-pigeon"
        assert bubble.border_title == "Carrier-pigeon"
        assert bubble.border_subtitle == "carrier-pigeon · coop-3"
        assert bubble.has_class(transcript.STREAM_FOREIGN_CLASS)
        step = display.active_step("x1")
        assert step is not None and step.has_class(transcript.STREAM_FOREIGN_CLASS)
        assert app.query_one(editor_widgets.StreamStrip).streams == {
            "x1": "carrier-pigeon · coop-3"
        }


async def test_the_strip_announces_a_foreign_stream_for_exactly_as_long_as_it_runs(scripted):
    """Content scrolls; a fork running inside a collapsed exchange three screens up
    is running invisibly. The strip is the ambient half — live while the stream is,
    gone when it ends."""
    app, _holder, _gate = scripted
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        strip = app.query_one(editor_widgets.StreamStrip)
        assert strip.display is False

        await app._on_render_event(
            {
                "kind": "stream_start",
                "stream": "b1",
                "source": "bus",
                "submitter": "nats_bus",
                "text": "deploy the thing",
            }
        )
        await pilot.pause()
        assert strip.display is True
        assert strip.streams == {"b1": "bus · nats_bus"}
        assert "bus · nats_bus" in strip.summary

        await app._on_render_event(
            {"kind": "stream_end", "stream": "b1", "context": 90, "output": 3, "extra": {}}
        )
        await pilot.pause()
        assert strip.streams == {}
        assert strip.display is False
