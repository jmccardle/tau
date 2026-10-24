"""The ``annotate`` hook and :func:`entry_blocks` (docs/ENTRY-ANNOTATIONS.md).

An extension's ``annotate`` handler returns JSON for each message entry τ
writes; it lands at ``ext[<extension name>]`` in the write that records the
entry, and never reaches the model. A failed handler still gets its entry
written, with ``{"error": …}`` in place of the value, and the turn then ends
in error with no tool call left unanswered.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent, ErrorEvent, TextDeltaEvent
from tau_llm.tools import ToolDefinition
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_loop import ABORTED_TOOL_RESULT
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree, entries_to_messages
from tau_agent_core.entry_annotations import AnnotationError, entry_blocks, json_value
from tau_agent_core.extensions.runner import ExtensionHandlers, ExtensionRunner
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission
from tau_agent_core.tools.base import AgentTool

_TS = 1_700_000_000_000


class _Stream:
    """A canned stream_simple result."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event

    async def result(self) -> Any:
        return next(e.final for e in self._events if isinstance(e, DoneEvent))

    def abort(self) -> None:
        pass


def _assistant(content: list[Any], stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=_TS,
        usage=Usage(),
    )


def _calls(*ids: str) -> AssistantMessage:
    """An assistant calling the unregistered tool ``write`` once per id: each errors fast."""
    return _assistant(
        [ToolCall(id=i, name="write", arguments={"path": f"{i}.py"}) for i in ids],
        stop_reason="toolUse",
    )


def _script(*finals: AssistantMessage, wire: list[list[Any]] | None = None):
    """A fake stream_simple answering each request with the next final message."""
    queue = list(finals)

    async def fake(model, context, options=None):
        if wire is not None:
            wire.append(list(context.get("messages", [])))
        final = queue.pop(0)
        events: list[Any] = []
        if final.content and isinstance(final.content[0], TextContent):
            events.append(TextDeltaEvent(delta=final.content[0].text, partial=final))
        events.append(DoneEvent(final=final, usage=Usage()))
        return _Stream(events)

    return fake


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session(*extensions, **kwargs: Any) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(), model=_model(), extensions=list(extensions), **kwargs
    )


def _message_entries(session: AgentSession) -> list[dict[str, Any]]:
    return [
        e for e in session.session_log.entries() if e.get("type") in ("message", "customMessage")
    ]


def _ext_name(session: AgentSession, index: int = 0) -> str:
    return session._extension_runner._extensions[index].name


def _assert_every_call_answered(session: AgentSession) -> None:
    """The path folds, and every tool call on it has exactly one result."""
    path = ConversationTree(session.session_log.entries(), session.cursor.leaf).path()
    messages = entries_to_messages(path)
    calls = [
        block["id"]
        for m in messages
        if m.get("role") == "assistant"
        for block in m["content"]
        if block.get("type") == "toolCall"
    ]
    results = [m["tool_call_id"] for m in messages if m.get("role") == "toolResult"]
    assert sorted(calls) == sorted(results)


# ── entry_blocks ─────────────────────────────────────────────────────────────


def test_str_content_is_block_zero() -> None:
    blocks = entry_blocks("user", {"role": "user", "content": "héllo"})
    assert blocks == [
        {
            "block": 0,
            "kind": "user",
            "text": "héllo",
            "content_hash": hashlib.sha256("héllo".encode()).hexdigest(),
        }
    ]


def test_custom_str_content_is_block_zero() -> None:
    (block,) = entry_blocks("custom", {"role": "custom", "content": "steer", "details": None})
    assert (block["block"], block["kind"], block["text"]) == (0, "custom", "steer")


def test_image_yields_no_block_and_keeps_indices() -> None:
    message = {
        "role": "user",
        "content": [
            {"type": "image", "data": "AAAA", "mime_type": "image/png"},
            {"type": "text", "text": "what is this"},
        ],
    }
    (block,) = entry_blocks("user", message)
    assert block["block"] == 1


def test_assistant_blocks_thinking_text_and_tool_call() -> None:
    message = _assistant(
        [
            {"type": "thinking", "thinking": "hmm"},  # type: ignore[list-item]
            TextContent(text="ok"),
            ToolCall(id="c1", name="read", arguments={"z": "ü", "a": [1, 2]}),
        ],
        stop_reason="toolUse",
    ).model_dump()
    blocks = entry_blocks("assistant", message)
    assert [(b["block"], b["kind"]) for b in blocks] == [
        (0, "thinking"),
        (1, "assistant_text"),
        (2, "tool_call"),
    ]
    assert blocks[0]["text"] == "hmm"
    assert blocks[2]["text"] == '{"z": "ü", "a": [1, 2]}'


def test_tool_result_text_and_details() -> None:
    message = {
        "role": "toolResult",
        "tool_call_id": "c1",
        "tool_name": "read",
        "content": [{"type": "text", "text": "body"}],
        "details": {"b": 1, "a": "é"},
        "is_error": False,
        "timestamp": _TS,
    }
    blocks = entry_blocks("toolResult", message)
    assert [(b["block"], b["kind"], b["text"]) for b in blocks] == [
        (0, "tool_result", "body"),
        ("details", "details", '{"b": 1, "a": "é"}'),
    ]
    assert blocks[1]["content_hash"] == hashlib.sha256(blocks[1]["text"].encode()).hexdigest()


def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown entry kind"):
        entry_blocks("system", {"role": "system", "content": "x"})


# ── registration ─────────────────────────────────────────────────────────────


def test_second_annotate_handler_raises() -> None:
    bucket = ExtensionHandlers(path="my_pkg.shadow")
    bucket.on("annotate", lambda e, ctx: None)
    with pytest.raises(ValueError, match="second 'annotate' handler"):
        bucket.on("annotate", lambda e, ctx: None)


def test_second_annotate_handler_raises_at_load() -> None:
    def ext(api):
        api.on("annotate", lambda e, ctx: None)
        api.on("annotate", lambda e, ctx: None)

    with pytest.raises(ValueError, match="second 'annotate' handler"):
        _session(ext)


# ── where ext is written ─────────────────────────────────────────────────────


def _recording(ext_log: list[str] | None = None):
    """An annotator storing the kind and each block's ``(block, kind, text)``."""

    def annotate(event, ctx):
        if ext_log is not None:
            ext_log.append(event["kind"])
        return {
            "kind": event["kind"],
            "blocks": [[b["block"], b["kind"], b["text"]] for b in event["blocks"]],
        }

    return annotate


async def test_every_message_write_site_is_annotated() -> None:
    """Prompt, steer, assistant, tool result, both turn-end nodes and send_message."""
    seen: list[str] = []
    holder: dict[str, AgentSession] = {}

    def ext(api):
        api.on("annotate", _recording(seen))
        api.on(
            "turn_end",
            lambda e, ctx: (
                {"message": {"customType": "note", "content": "t0"}}
                if e["turn_index"] == 0
                else None
            ),
        )
        api.on("user_turn_end", lambda e, ctx: {"message": {"customType": "done", "content": "u"}})

    finals = [_calls("c1"), _assistant([TextContent(text="fin")])]

    async def fake(model, context, options=None):
        final = finals.pop(0)
        if finals:
            holder["s"].cursor.steer_queue.append(
                {"role": "user", "content": [{"type": "text", "text": "steer!"}], "timestamp": _TS}
            )
        return _Stream([DoneEvent(final=final, usage=Usage())])

    session = _session(ext)
    holder["s"] = session
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("go")
    await session._append_custom_message({"customType": "aside", "content": "later"})

    name = _ext_name(session)
    written = [e for e in _message_entries(session) if e["message"].get("role") != "system"]
    got = [(e["message"]["role"], e["ext"][name]["blocks"]) for e in written]
    assert got == [
        ("user", [[0, "user", "go"]]),
        ("assistant", [[0, "tool_call", '{"path": "c1.py"}']]),
        ("toolResult", [[0, "tool_result", written[2]["message"]["content"][0]["text"]]]),
        ("custom", [[0, "custom", "t0"]]),
        ("user", [[0, "user", "steer!"]]),
        ("assistant", [[0, "assistant_text", "fin"]]),
        ("custom", [[0, "custom", "u"]]),
        ("custom", [[0, "custom", "later"]]),
    ]
    assert all(e["ext"][name]["kind"] == e["message"]["role"] for e in written)
    assert seen == [role for role, _ in got]


async def test_continue_conversation_is_annotated() -> None:
    def ext(api):
        api.on("annotate", _recording())

    session = _session(ext)
    script = _script(_assistant([TextContent(text="one")]), _assistant([TextContent(text="two")]))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=script):
        await session.prompt("go")
        await session.continue_conversation()
    last = _message_entries(session)[-1]
    assert last["ext"] == {
        _ext_name(session): {"kind": "assistant", "blocks": [[0, "assistant_text", "two"]]}
    }


async def test_no_handler_writes_no_ext_key() -> None:
    session = _session()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_calls("c1"), _assistant([TextContent(text="x")])),
    ):
        await session.prompt("go")
    assert all("ext" not in e for e in session.session_log.entries())


async def test_handler_returning_none_writes_no_ext_key() -> None:
    def ext(api):
        api.on("annotate", lambda e, ctx: None)

    session = _session(ext)
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_assistant([TextContent(text="x")])),
    ):
        await session.prompt("go")
    assert all("ext" not in e for e in session.session_log.entries())


async def test_store_history_false_never_calls_annotate() -> None:
    calls: list[str] = []

    def ext(api):
        api.on("annotate", lambda e, ctx: calls.append(e["kind"]) or {"x": 1})

    session = _session(ext)
    sub = Submission(
        text="go",
        source="interactive",
        submitter="human",
        submission_id="s1",
        store_history=False,
    )
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_assistant([TextContent(text="x")])),
    ):
        await session.submit(sub)
    assert calls == []


async def test_handler_mutation_does_not_reach_the_entry() -> None:
    """A nested mutation: a shallow copy of the event would let this through."""

    def ext(api):
        def annotate(event, ctx):
            for block in event["message"]["content"]:
                if block.get("type") == "text":
                    block["text"] = "TAMPERED"
            event["blocks"][0]["text"] = "TAMPERED"
            return None

        api.on("annotate", annotate)

    def second(api):
        api.on("annotate", lambda e, ctx: {"seen": e["blocks"][0]["text"]})

    session = _session(ext, second)
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_assistant([TextContent(text="x")])),
    ):
        await session.prompt("go")
    entries = _message_entries(session)
    assert "TAMPERED" not in json.dumps(entries)
    user = next(e for e in entries if e["message"]["role"] == "user")
    assert user["ext"] == {_ext_name(session, 1): {"seen": "go"}}


async def test_ext_is_not_model_input() -> None:
    wire: list[list[Any]] = []

    def ext(api):
        api.on("annotate", lambda e, ctx: {"secret": "ANNOTATION-ONLY"})

    session = _session(ext)
    script = _script(_calls("c1"), _assistant([TextContent(text="x")]), wire=wire)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=script):
        await session.prompt("go")

    assert any("ext" in e for e in session.session_log.entries())
    path = ConversationTree(session.session_log.entries(), session.cursor.leaf).path()
    assert "ANNOTATION-ONLY" not in json.dumps(entries_to_messages(path))
    assert "ANNOTATION-ONLY" not in json.dumps(wire, default=str)


# ── a failed handler ─────────────────────────────────────────────────────────


def _failing_on(kinds: set[str], agent_ends: list[Any] | None = None, value: Any = None):
    """An extension whose handler raises for ``kinds`` and returns ``value`` otherwise."""

    def ext(api):
        def annotate(event, ctx):
            if event["kind"] in kinds:
                raise RuntimeError(f"boom on {event['kind']}")
            return value

        api.on("annotate", annotate)
        if agent_ends is not None:
            api.on("agent_end", lambda e: agent_ends.append(e))

    return ext


async def test_failed_tool_result_is_written_then_turn_errors() -> None:
    agent_ends: list[Any] = []
    session = _session(
        _failing_on({"toolResult"}, agent_ends, value={"ok": True}),
        tool_execution_mode="sequential",
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_calls("c1", "c2"))):
        with pytest.raises(AnnotationError, match="boom on toolResult"):
            await session.prompt("go")

    name = _ext_name(session)
    results = [e for e in _message_entries(session) if e["message"]["role"] == "toolResult"]
    assert [r["message"]["tool_call_id"] for r in results] == ["c1", "c2"]
    assert results[0]["ext"] == {name: {"error": "RuntimeError: boom on toolResult"}}
    assert results[1]["message"]["content"][0]["text"] == ABORTED_TOOL_RESULT
    assert results[1]["ext"] == {name: {"error": "RuntimeError: boom on toolResult"}}
    assert [(e.is_error, e.end_reason) for e in agent_ends] == [(True, "error")]
    _assert_every_call_answered(session)


async def test_no_reentry_one_abort_and_turn_error_is_annotated() -> None:
    """Failures written during the abort are recorded and never abort again."""
    agent_ends: list[Any] = []
    session = _session(
        _failing_on({"toolResult", "custom"}, agent_ends), tool_execution_mode="sequential"
    )
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(_calls("c1", "c2", "c3"))
    ):
        with pytest.raises(AnnotationError, match="boom on toolResult"):
            await session.prompt("go")

    name = _ext_name(session)
    assert len(agent_ends) == 1
    errored = [e for e in _message_entries(session) if e.get("ext", {}).get(name, {}).get("error")]
    roles = [e["message"]["role"] for e in errored]
    assert roles == ["toolResult", "toolResult", "toolResult", "custom"]
    assert errored[-1]["customType"] == "turn_error"
    _assert_every_call_answered(session)


async def test_failed_assistant_is_finalized_and_its_calls_answered() -> None:
    ran: list[str] = []
    agent_ends: list[Any] = []
    session = _session(_failing_on({"assistant"}, agent_ends))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_calls("c1", "c2"))):
        with patch(
            "tau_agent_core.agent_loop.AgentLoop._execute_tool_calls",
            side_effect=lambda *a, **k: ran.append("ran"),
        ):
            with pytest.raises(AnnotationError, match="boom on assistant"):
                await session.prompt("go")

    name = _ext_name(session)
    entries = _message_entries(session)
    (assistant,) = [e for e in entries if e["message"]["role"] == "assistant"]
    assert "status" not in assistant
    assert assistant["ext"] == {name: {"error": "RuntimeError: boom on assistant"}}
    results = [e for e in entries if e["message"]["role"] == "toolResult"]
    assert [r["message"]["content"][0]["text"] for r in results] == [ABORTED_TOOL_RESULT] * 2
    assert ran == []
    assert [(e.is_error, e.end_reason) for e in agent_ends] == [(True, "error")]
    _assert_every_call_answered(session)


async def test_failed_user_prompt_is_written_and_no_request_is_sent() -> None:
    requests: list[Any] = []
    agent_ends: list[Any] = []
    session = _session(_failing_on({"user"}, agent_ends))

    async def never(model, context, options=None):
        requests.append(context)
        raise AssertionError("no request after a failed input annotation")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=never):
        with pytest.raises(AnnotationError, match="boom on user"):
            await session.prompt("go")

    name = _ext_name(session)
    (user,) = [e for e in _message_entries(session) if e["message"]["role"] == "user"]
    assert user["ext"] == {name: {"error": "RuntimeError: boom on user"}}
    assert requests == []
    assert [(e.is_error, e.end_reason) for e in agent_ends] == [(True, "error")]


async def test_non_json_value_is_a_failed_handler() -> None:
    def ext(api):
        api.on("annotate", lambda e, ctx: {"tags": {"a", "b"}} if e["kind"] == "user" else None)

    session = _session(ext)
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_assistant([TextContent(text="x")])),
    ):
        with pytest.raises(AnnotationError, match="TypeError"):
            await session.prompt("go")
    (user,) = [e for e in _message_entries(session) if e["message"]["role"] == "user"]
    assert user["ext"][_ext_name(session)]["error"].startswith("TypeError: $.tags is a set")


async def test_other_extensions_values_are_kept_beside_an_error() -> None:
    def good(api):
        api.on("annotate", lambda e, ctx: {"fine": e["kind"]})

    session = _session(good, _failing_on({"assistant"}))
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_assistant([TextContent(text="x")])),
    ):
        with pytest.raises(AnnotationError):
            await session.prompt("go")
    (assistant,) = [e for e in _message_entries(session) if e["message"]["role"] == "assistant"]
    assert assistant["ext"] == {
        _ext_name(session, 0): {"fine": "assistant"},
        _ext_name(session, 1): {"error": "RuntimeError: boom on assistant"},
    }


async def test_failed_send_message_is_written_then_raises() -> None:
    session = _session(_failing_on({"custom"}))
    with pytest.raises(AnnotationError):
        await session._append_custom_message({"customType": "aside", "content": "x"})
    (entry,) = [e for e in _message_entries(session) if e["type"] == "customMessage"]
    assert entry["ext"] == {_ext_name(session): {"error": "RuntimeError: boom on custom"}}


def _tool(name: str, delay: float = 0.0) -> AgentTool:
    async def _execute(**kw: Any) -> str:
        await asyncio.sleep(delay)
        return f"{name} ok"

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name,
            description=f"the {name} tool",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=_execute,
        )
    )


def _named_calls(*names: str) -> AssistantMessage:
    return _assistant(
        [ToolCall(id=f"c-{n}", name=n, arguments={}) for n in names], stop_reason="toolUse"
    )


async def test_failed_tool_result_in_a_parallel_batch_writes_each_result_once() -> None:
    """The fast call's failed write lands while the slow call is still running."""
    agent_ends: list[Any] = []
    order: list[str] = []

    def ext(api):
        def annotate(event, ctx):
            if event["kind"] == "toolResult":
                order.append(event["message"]["tool_name"])
                if event["message"]["tool_name"] == "fast":
                    raise RuntimeError("boom on fast")
            return None

        api.on("annotate", annotate)
        api.on("agent_end", lambda e: agent_ends.append(e))

    session = _session(ext, tools=[_tool("fast"), _tool("slow", delay=0.05)])
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(_named_calls("fast", "slow"))
    ):
        with pytest.raises(AnnotationError, match="boom on fast"):
            await session.prompt("go")

    results = [e for e in _message_entries(session) if e["message"]["role"] == "toolResult"]
    assert [r["message"]["tool_call_id"] for r in results] == ["c-fast", "c-slow"]
    assert [r["message"]["content"][0]["text"] for r in results] == ["fast ok", "slow ok"]
    assert order == ["fast", "slow"]
    assert "ext" not in results[1]
    assert len(agent_ends) == 1
    _assert_every_call_answered(session)


# ── blocks that cannot be derived ────────────────────────────────────────────


async def test_non_json_details_still_writes_the_result_then_errors() -> None:
    """A ``tool_result`` hook puts a datetime in ``details``; entry_blocks cannot render it."""
    agent_ends: list[Any] = []

    def patcher(api):
        api.on("tool_result", lambda e, ctx: {"details": {"when": datetime(2026, 10, 24)}})

    def annotator(api):
        api.on("annotate", lambda e, ctx: {"ok": 1})
        api.on("agent_end", lambda e: agent_ends.append(e))

    session = _session(patcher, annotator, tools=[_tool("probe")])
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(_named_calls("probe"))
    ):
        with pytest.raises(AnnotationError, match="could not be derived.*TypeError"):
            await session.prompt("go")

    (result,) = [e for e in _message_entries(session) if e["message"]["role"] == "toolResult"]
    assert result["ext"][_ext_name(session, 1)]["error"].startswith("TypeError: ")
    assert [(e.is_error, e.end_reason) for e in agent_ends] == [(True, "error")]
    _assert_every_call_answered(session)


async def test_underivable_blocks_record_an_error_for_every_annotator() -> None:
    runner = ExtensionRunner()
    for path in ("a.one", "b.two"):
        runner.register_extension(path).on("annotate", lambda e, ctx: {"never": "called"})
    runner.register_extension("c.none").on("turn_end", lambda e, ctx: None)
    message = {"role": "custom", "customType": "t", "content": [{"type": "weird"}]}

    ext, failure = await runner.emit_annotate("custom", message)

    detail = "ValueError: entry_blocks: a 'custom' message has a 'weird' block"
    assert ext == {"a.one": {"error": detail}, "b.two": {"error": detail}}
    assert failure is not None and failure.extension is None and failure.error == detail


# ── json_value ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ({"x": float("nan")}, ValueError),
        ([float("inf")], ValueError),
        ({1: "a"}, TypeError),
        ({"t": (1, 2)}, TypeError),
        (b"bytes", TypeError),
    ],
)
def test_json_value_rejects(value: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        json_value(value)


def test_json_value_copies() -> None:
    value = {"a": [1, 2.5, None, True, {"b": "c"}]}
    out = json_value(value)
    assert out == value and out is not value and out["a"] is not value["a"]


# ── failures at the remaining sites ──────────────────────────────────────────


async def test_failure_on_the_stream_error_finalize_does_not_replace_the_stream_error() -> None:
    agent_ends: list[Any] = []
    session = _session(_failing_on({"assistant"}, agent_ends))
    partial = _assistant([TextContent(text="part")])

    async def fake(model, context, options=None):
        return _Stream(
            [TextDeltaEvent(delta="part", partial=partial), ErrorEvent(message="upstream 503")]
        )

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        with pytest.raises(Exception, match="upstream 503") as raised:
            await session.prompt("go")
    assert not isinstance(raised.value, AnnotationError)

    (assistant,) = [e for e in _message_entries(session) if e["message"]["role"] == "assistant"]
    assert assistant["message"]["stop_reason"] == "error"
    assert assistant["ext"] == {_ext_name(session): {"error": "RuntimeError: boom on assistant"}}
    assert len(agent_ends) == 1


async def test_user_turn_end_failure_is_written_then_prompt_raises() -> None:
    def ext(api):
        def annotate(event, ctx):
            if event["message"].get("customType") == "done":
                raise RuntimeError("boom on done")
            return None

        api.on("annotate", annotate)
        api.on("user_turn_end", lambda e, ctx: {"message": {"customType": "done", "content": "u"}})

    session = _session(ext)
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_assistant([TextContent(text="x")])),
    ):
        with pytest.raises(AnnotationError, match="boom on done"):
            await session.prompt("go")
    (done,) = [e for e in _message_entries(session) if e.get("customType") == "done"]
    assert done["ext"] == {_ext_name(session): {"error": "RuntimeError: boom on done"}}


async def test_turn_end_node_failure_in_a_tool_turn_stops_before_the_next_turn() -> None:
    turn_starts: list[int] = []
    agent_ends: list[Any] = []
    requests: list[Any] = []

    def ext(api):
        def annotate(event, ctx):
            if event["message"].get("customType") == "note":
                raise RuntimeError("boom on note")
            return None

        api.on("annotate", annotate)
        api.on("turn_end", lambda e, ctx: {"message": {"customType": "note", "content": "t"}})
        api.on("turn_start", lambda e: turn_starts.append(e.turn_index))
        api.on("agent_end", lambda e: agent_ends.append(e))

    session = _session(ext)
    script = _script(_calls("c1"), _assistant([TextContent(text="x")]), wire=requests)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=script):
        with pytest.raises(AnnotationError, match="boom on note"):
            await session.prompt("go")

    assert len(requests) == 1
    assert turn_starts == [0]
    assert [(e.is_error, e.end_reason) for e in agent_ends] == [(True, "error")]
    (note,) = [e for e in _message_entries(session) if e.get("customType") == "note"]
    assert note["ext"] == {_ext_name(session): {"error": "RuntimeError: boom on note"}}
    _assert_every_call_answered(session)


@pytest.mark.parametrize("annotated", [False, True])
async def test_custom_node_with_an_unknown_block_fails_the_same_with_or_without_annotate(
    annotated: bool,
) -> None:
    """Acceptance does not depend on an unrelated extension being loaded."""
    extensions = [_failing_on(set(), value={"ok": 1})] if annotated else []
    session = _session(*extensions)
    before = list(session.session_log.entries())
    with pytest.raises(ValueError, match=r"content\[0\] is not a text or image block"):
        await session._append_custom_message(
            {"customType": "aside", "content": [{"type": "weird"}]}
        )
    assert session.session_log.entries() == before
