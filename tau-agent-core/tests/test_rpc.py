"""RPC mode — the JSON-RPC 2.0 handler over stdin/stdout (``tau_agent_core.rpc``).

Rewritten for unit 2A (docs/REMOTE-CONTROL.md §3/§4/§6/§10): the six legacy
verbs (``send_prompt``, ``send_tool_result``, ``abort``, ``get_commands``,
``get_tools``, ``get_session_info``) are gone, replaced by the declarative
table in ``tau_agent_core.rpc.commands`` — ``submit``, ``prompt``, ``abort``,
``get_state``, ``get_messages``, ``get_commands``, ``get_tools``, plus the
declined ``send_tool_result``. See the module docstring there for the tier
rationale and what is deliberately NOT in the table yet (``new_session`` /
``fork`` / ``switch_session`` — phase 3).

The wire-format and pure-transport tests (framing, stdin→stdout pump
ordering, broken-pipe handling, ``stop()``) are unchanged in spirit from the
pre-2A file — they do not touch verb names — and are kept here with updated
example payloads. Everything under "dispatch" and "handlers" below is new:
the table's completeness, the JSON-RPC error taxonomy (C2), the ``result
.method`` echo (D2), and ``submit``/``prompt``'s dual completion (C3) — an
immediate acceptance response enqueued from inside ``Submission``'s
``on_admitted`` callback, strictly before any event the resulting turn emits,
then the turn's own outcome reported later via the ordinary ``AgentEvent``
stream (never on the response itself).

Reference: docs/REMOTE-CONTROL.md; docs/PHASE-6-SUBPHASE-1.md (transport).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any, BinaryIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tau_llm.streaming import TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.commands import FRONTEND_COMMANDS
from tau_agent_core.flows import Performed, Ready, View
from tau_agent_core.events import AgentEvent
from tau_agent_core.rpc import (
    RPCEvent,
    RPCHandler,
    RPCRequest,
    RPCResponse,
    commands,
    dialect,
    transport,
    wire_events,
)
from tau_agent_core.session import SessionState
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import SubmissionResult

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000


class _FakeStdin:
    """Stand-in for ``sys.stdin`` exposing only the ``.buffer`` that
    ``RPCHandler._read_stdin`` actually uses.

    The reader runs on ``loop.connect_read_pipe``, which needs a real file
    descriptor — a ``StringIO`` cannot stand in for stdin any more. (See
    ``test_rpc_transport.py``, which owns the transport-level tests and the
    same plumbing; these two files test the handler from opposite ends.)
    """

    def __init__(self, buffer: BinaryIO) -> None:
        self.buffer = buffer


def _stdin_from_bytes(data: bytes) -> _FakeStdin:
    """A stdin fake pre-loaded with ``data``, already at EOF."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, data)
    os.close(write_fd)
    return _FakeStdin(os.fdopen(read_fd, "rb"))


def _stdin_held_open() -> tuple[_FakeStdin, BinaryIO]:
    """A stdin fake that never reaches EOF until the returned writer closes."""
    read_fd, write_fd = os.pipe()
    return _FakeStdin(os.fdopen(read_fd, "rb")), os.fdopen(write_fd, "wb")


def _session(**overrides: Any) -> MagicMock:
    """A mock ``AgentSession`` exposing exactly what the command table reads."""
    session = MagicMock()
    session.state = SessionState(session_id="sess-1", status="idle")
    session.is_streaming = False
    session.is_aborted = False
    session.shutdown_requested = False
    session.get_model.return_value = {"id": "gpt-4o", "provider": "openai", "context_window": 8192}
    session.get_usage.return_value = None
    session.messages = []
    session.session_log = MagicMock()
    session.session_log.cursor = "leaf-1"
    session.is_addressable = True
    session.tools = []
    session.subscribe.return_value = MagicMock()
    session.abort = MagicMock()
    session.submit = AsyncMock(
        return_value=SubmissionResult(accepted=True, submission_id="s-1", messages=[])
    )
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


@pytest.fixture
def session() -> MagicMock:
    return _session()


@pytest.fixture
def handler(session: MagicMock) -> RPCHandler:
    return RPCHandler(session)


async def _drain(handler: RPCHandler) -> list[dict]:
    """Everything currently queued for stdout, in order."""
    out = []
    while not handler._output_queue.empty():
        out.append(await handler._output_queue.get())
    return out


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
        timestamp=_TS,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    """The minimal ``stream_simple`` return shape (mirrors test_submit_admission.py)."""

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


@pytest.fixture
def real_session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])


@pytest.fixture
def real_handler(real_session: AgentSession) -> RPCHandler:
    return RPCHandler(real_session)


async def _drain_until(handler: RPCHandler, predicate, limit: int = 50, timeout: float = 5.0):
    """Pop items off the output queue until one matches `predicate`; return it."""
    for _ in range(limit):
        item = await asyncio.wait_for(handler._output_queue.get(), timeout=timeout)
        if predicate(item):
            return item
    raise AssertionError("predicate never matched within the item limit")


@pytest.mark.parametrize(
    "message",
    [
        RPCRequest(id=1, method="prompt", params={"text": "hi"}),
        RPCRequest(id=None, method="abort", params=None),
        RPCResponse(id=1, result={"status": "done"}),
        RPCResponse(id=1, error={"code": -32603, "message": "boom"}),
        RPCEvent(params={"type": "turn_start"}),
    ],
    ids=["request", "notification", "response-ok", "response-err", "event"],
)
def test_every_wire_type_round_trips(message):
    restored = type(message).from_json_line(message.to_json_line())
    assert restored == message


@pytest.mark.parametrize(
    "message",
    [
        RPCRequest(id=1, method="prompt", params={"text": "line one\nline two"}),
        RPCResponse(id=1, result={"text": "a\nb"}),
        RPCEvent(params={"text": "a\nb"}),
    ],
    ids=["request", "response", "event"],
)
def test_a_wire_line_never_contains_a_raw_newline(message):
    """LF framing is the whole protocol — an unescaped newline splits one message
    into two unparseable halves. Embedded newlines must survive as ``\\n``."""
    line = message.to_json_line()
    assert "\n" not in line
    assert type(message).from_json_line(line) == message


def test_is_error_discriminates_the_two_response_shapes():
    assert RPCResponse(id=1, error={"code": -1, "message": "x"}).is_error() is True
    assert RPCResponse(id=1, result={"ok": True}).is_error() is False


# ── transport: stdin → dispatch → stdout ────────────────────────────────────


class _Recorder:
    """A stdout double that records complete lines.

    Backed by a REAL OS pipe, not an in-memory buffer: `_write_stdout` now
    moves bytes through `_connect_stdout_writer`
    (`loop.connect_write_pipe`), which needs an actual file descriptor —
    the writer-side fix for T6 blocker 2 (phase-4 review), mirroring the
    fix `_read_stdin` already had for the reader (see this file's own
    `_FakeStdin` docstring for that precedent). An in-memory `io.StringIO`
    cannot stand in for stdout any more, for the identical reason it
    already could not stand in for stdin.

    Previously had a `jitter` knob: sleeping inside `write()` for
    even-numbered ids, to make an ordering bug observable when writes were
    racing across `ThreadPoolExecutor` threads (the pre-fix mechanism). That
    thread pool is gone — `write()` on this class is never on the hot path
    at all now, the pipe transport writes directly to the fd — so there is
    nothing left for jitter to perturb; `test_output_reaches_stdout_in_
    queue_order` below still pins the ordering guarantee itself, and
    `test_rpc_transport.py::TestWriterContract::test_fifo_order_preserved_
    under_load` is the higher-volume version of the same property against a
    real pipe.

    A background thread drains the read end and parses each complete line
    as JSON into `.lines`, so callers assert on it exactly as before the
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


async def test_run_reads_a_request_writes_a_response_and_stops_at_eof(handler, monkeypatch):
    """The whole loop, end to end — what ``--mode rpc`` will run when it lands.

    ``sys.stdout`` is monkeypatched *before* ``run()`` because ``run()`` now
    claims stdout (T2) — ``_take_over_stdout()`` captures whatever ``sys.stdout``
    is at that moment as the protocol's private write handle, so the recorder
    below is what the writer ends up writing to.
    """
    request = {"jsonrpc": "2.0", "id": 7, "method": "get_state"}
    recorder = _Recorder()
    monkeypatch.setattr("sys.stdin", _stdin_from_bytes((json.dumps(request) + "\n").encode()))
    monkeypatch.setattr("sys.stdout", recorder)

    task = asyncio.create_task(handler.run())
    done, pending = await asyncio.wait([task], timeout=5.0)
    for still_running in pending:
        still_running.cancel()
    assert task in done, "run() did not return after stdin reached EOF"
    recorder.close()
    recorder.join()

    assert recorder.lines == [
        {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {
                "session_id": "sess-1",
                "status": "idle",
                "is_streaming": False,
                "model": {"id": "gpt-4o", "provider": "openai", "context_window": 8192},
                "usage": None,
                "message_count": 0,
                "cursor": "leaf-1",
                "addressable": True,
                "method": "get_state",
            },
        }
    ]
    assert handler._running is False  # run() clears the flag on the way out


async def test_output_reaches_stdout_in_queue_order(handler, monkeypatch):
    """Regression: ``_write_stdout`` must await each write.

    The output queue is FIFO and is the only ordering guarantee the protocol
    has (T6).
    """
    recorder = _Recorder()
    handler._real_stdout = recorder
    handler._running = True
    for i in range(20):
        await handler._send_response(i, {"n": i})

    pump = asyncio.create_task(handler._write_stdout())
    handler._running = False
    await asyncio.wait_for(pump, timeout=5.0)
    recorder.close()
    recorder.join()

    assert [line["id"] for line in recorder.lines] == list(range(20))


def _closed_read_end_pipe() -> Any:
    """stdout after the peer has gone away: a real pipe whose read end is
    already closed, so the very first write/drain hits a genuine OSError.

    Replaces the old `_BrokenPipe(io.StringIO)` double, whose `write()`
    raised synchronously — `_write_stdout` no longer calls `write()`
    itself at all (see `_Recorder`'s docstring above), so an in-memory
    double raising from a method nothing calls anymore proved nothing.
    """
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    return os.fdopen(write_fd, "w")


async def test_a_failed_write_takes_run_down_instead_of_being_swallowed(handler, monkeypatch):
    """A broken pipe must surface, and must not wait for stdin to notice."""
    stdin, stdin_writer = _stdin_held_open()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", _closed_read_end_pipe())
    await handler._send_response(1, {"n": 1})

    task = asyncio.create_task(handler.run())
    try:
        done, pending = await asyncio.wait([task], timeout=5.0)
        for still_running in pending:
            still_running.cancel()
        assert task in done, "run() ignored a dead stdout and kept waiting on stdin"
        with pytest.raises(ConnectionResetError):
            await task
    finally:
        stdin_writer.close()

    assert handler._running is False


async def test_an_unserializable_payload_raises_rather_than_vanishing(handler, monkeypatch):
    """The other way a write dies: nothing to write."""
    recorder = _Recorder()
    handler._real_stdout = recorder
    handler._running = True
    await handler._output_queue.put({"jsonrpc": "2.0", "id": 1, "result": {"x": object()}})

    pump = asyncio.create_task(handler._write_stdout())
    try:
        with pytest.raises(TypeError):
            await asyncio.wait_for(pump, timeout=5.0)
    finally:
        recorder.close()
        recorder.join()


async def test_invalid_json_is_reported_and_the_stream_keeps_going(handler, monkeypatch):
    """One bad line must not kill the session — the next request still runs."""
    stdin = "{not json\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "abort"}) + "\n"
    monkeypatch.setattr("sys.stdin", _stdin_from_bytes(stdin.encode()))
    handler._running = True

    await asyncio.wait_for(handler._read_stdin(), timeout=5.0)

    first, second = await _drain(handler)
    assert first["error"]["code"] == dialect.PARSE_ERROR
    assert first["error"]["message"].startswith("Parse error")
    assert first["id"] is None  # the id is unknowable on an unparseable line
    assert second["result"] == {"status": "aborted", "compaction_id": None, "method": "abort"}


async def test_blank_lines_are_skipped(handler, monkeypatch):
    monkeypatch.setattr("sys.stdin", _stdin_from_bytes(b"\n   \n\n"))
    handler._running = True

    await asyncio.wait_for(handler._read_stdin(), timeout=5.0)

    assert await _drain(handler) == []


async def test_stop_cancels_both_pumps(handler, monkeypatch):
    """``stop()`` ends both pumps and lets ``run()`` return."""
    stdin, stdin_writer = _stdin_held_open()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", _Recorder())

    task = asyncio.create_task(handler.run())
    try:
        for _ in range(500):  # wait for run() to install both pumps
            if handler._stdin_task is not None and handler._stdout_task is not None:
                break
            await asyncio.sleep(0.01)

        await asyncio.wait_for(handler.stop(), timeout=5.0)
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        stdin_writer.close()

    assert handler._running is False
    assert handler._stdin_task.done() and handler._stdout_task.done()


# ── the command table ────────────────────────────────────────────────────────


def test_the_table_has_exactly_the_2a_2c_phase3_and_tier_b_verbs():
    """Tier A + the Tier C differentiator + `get_capabilities` (2C) + phase
    3's runtime-host trio (`new_session`/`fork`/`switch_session`, H1) + the
    declined verbs (2A's `send_tool_result`, 2C's six Tier D verbs) + Tier
    B's nine verbs (`compact`, `get_last_assistant_text`, `get_models`,
    `get_session_name`, `get_session_stats`, `list_sessions`,
    `set_auto_compaction`, `set_model`, `set_session_name` — nine, not six:
    B5 delivered `get_session_name` alongside `set_session_name`,
    docs/RPC-TIER-B.md §3; `get_models` was added by finding 7 of the Tier B
    review, which found `set_model`'s config-NAME param undiscoverable from
    the wire; and `list_sessions` by finding 8, which found the same of
    `switch_session`'s session-id param) — plus protocol 1.4's
    `complete_path`, which makes it ten — plus the flow loop's two Tier C
    reads, `next_step` and `enumerate_domain`, which are what let a host
    drive a gesture it has no table for (the capability/flow model,
    2026-09-03) — plus 0.9.8's eleven, which put the SESSION TREE (five
    mutations) and the EXTENSION SYSTEM (three mutations) on the wire along
    with the three reads that make their arguments discoverable — plus the
    extension-config pair, `get_extension_config` and `set_extension_config`,
    which put the `CONFIG_SCHEMA` declaration and the slice it describes on the
    wire — no more, no less."""
    assert set(commands.COMMAND_TABLE) == {
        "get_extension_config",
        "set_extension_config",
        "submit",
        "prompt",
        "abort",
        "get_state",
        "get_messages",
        "get_commands",
        "get_tools",
        "get_capabilities",
        "new_session",
        "fork",
        "switch_session",
        "send_tool_result",
        "cycle_model",
        "cycle_thinking_level",
        "set_steering_mode",
        "set_follow_up_mode",
        "export_html",
        "bash",
        "compact",
        "complete_path",
        "get_last_assistant_text",
        "get_models",
        "get_session_name",
        "get_session_stats",
        "list_sessions",
        "set_auto_compaction",
        "set_model",
        "set_session_name",
        "next_step",
        "enumerate_domain",
        "navigate",
        "summarize_and_navigate",
        "elide_span",
        "commit_branch",
        "paste_subtree",
        "enable_extension",
        "disable_extension",
        "reload_extension",
        "complete_message_id",
        "list_managed_extensions",
        "get_extension_state",
    }


def test_every_entry_has_a_handler_xor_a_declined_reason():
    for name, entry in commands.COMMAND_TABLE.items():
        assert (entry.handler is None) != (entry.declined_because is None), name


def test_command_entry_rejects_both_handler_and_declined_because():
    async def _h(handler, msg_id, params):
        return {}

    with pytest.raises(ValueError, match="exactly one"):
        commands.CommandEntry(
            name="x",
            tier="A",
            since="2A",
            notes="",
            params_schema=commands.NO_PARAMS_SCHEMA,
            handler=_h,
            declined_because="nope",
        )


def test_command_entry_rejects_neither_handler_nor_declined_because():
    with pytest.raises(ValueError, match="exactly one"):
        commands.CommandEntry(
            name="x", tier="A", since="2A", notes="", params_schema=commands.NO_PARAMS_SCHEMA
        )


def test_command_decorator_refuses_a_duplicate_name():
    async def _h(handler, msg_id, params):
        return {}

    commands.command(
        "x-test-dup",
        tier="A",
        since="2A",
        notes="",
        params_schema=commands.NO_PARAMS_SCHEMA,
        result_schema=commands.NO_PARAMS_SCHEMA,
    )(_h)
    try:
        with pytest.raises(ValueError, match="duplicate"):
            commands.command(
                "x-test-dup",
                tier="A",
                since="2A",
                notes="",
                params_schema=commands.NO_PARAMS_SCHEMA,
                result_schema=commands.NO_PARAMS_SCHEMA,
            )(_h)
    finally:
        del commands.COMMAND_TABLE["x-test-dup"]


def test_declined_reuses_the_no_params_schema_by_default():
    commands.decline("x-test-decline", tier="D", since="2A", notes="", declined_because="because")
    try:
        assert commands.COMMAND_TABLE["x-test-decline"].params_schema is commands.NO_PARAMS_SCHEMA
    finally:
        del commands.COMMAND_TABLE["x-test-decline"]


# ── validate_params ──────────────────────────────────────────────────────────


def test_validate_params_accepts_a_conforming_payload():
    assert commands.validate_params(commands.PROMPT_PARAMS_SCHEMA, {"text": "hi"}) is None


def test_validate_params_reports_a_missing_required_field():
    violation = commands.validate_params(commands.SUBMIT_PARAMS_SCHEMA, {"text": "hi"})
    assert violation is not None and "source" in violation


def test_validate_params_reports_an_unexpected_field():
    violation = commands.validate_params(commands.PROMPT_PARAMS_SCHEMA, {"text": "hi", "bogus": 1})
    assert violation is not None and "bogus" in violation


def test_validate_params_reports_a_wrong_type():
    violation = commands.validate_params(commands.PROMPT_PARAMS_SCHEMA, {"text": 5})
    assert violation is not None and "text" in violation


def test_validate_params_reports_a_bad_enum_value():
    violation = commands.validate_params(
        commands.PROMPT_PARAMS_SCHEMA, {"text": "hi", "source": "not-a-real-source"}
    )
    assert violation is not None and "source" in violation


def test_validate_params_reports_a_non_object_payload():
    violation = commands.validate_params(commands.NO_PARAMS_SCHEMA, ["nope"])  # type: ignore[arg-type]
    assert violation is not None and "object" in violation


def test_validate_params_enforces_minimum():
    violation = commands.validate_params(
        commands.SUBMIT_PARAMS_SCHEMA,
        {"text": "t", "source": "rpc", "submitter": "s", "submission_id": "id", "depth": -1},
    )
    assert violation is not None and "depth" in violation


def _entry(schema: dict) -> commands.CommandEntry:
    async def _noop(handler, msg_id, params):  # pragma: no cover - never called
        return {}

    return commands.CommandEntry(
        name="probe",
        tier="A",
        since="test",
        notes="",
        params_schema=schema,
        handler=_noop,
        result_schema=commands.NO_PARAMS_SCHEMA,
    )


@pytest.mark.parametrize(
    "schema, expected",
    [
        ({"type": "string"}, "must be 'object'"),
        ({"type": "object", "oneOf": []}, "unsupported schema keyword"),
        (
            {"type": "object", "properties": {"a": {"type": "array", "pattern": "x"}}},
            "unsupported keyword",
        ),
        (
            {"type": "object", "properties": {"a": {"type": "objekt"}}},
            "unsupported type",
        ),
        (
            {"type": "object", "properties": {}, "required": ["ghost"]},
            "absent from `properties`",
        ),
        (
            {"type": "object", "properties": {"a": {"type": "object", "items": {}}}},
            "without type 'array'",
        ),
        (
            {"type": "object", "properties": {"a": {"type": "array", "items": {}}}},
            "not an object schema",
        ),
        (
            {
                "type": "object",
                "properties": {"a": {"type": "array", "items": {"type": "object", "oneOf": []}}},
            },
            "unsupported schema keyword",
        ),
    ],
    ids=[
        "non-object-root",
        "unknown-root-keyword",
        "unknown-prop-keyword",
        "typo-type",
        "required-not-declared",
        "items-on-a-non-array",
        "items-that-is-not-an-object-schema",
        "items-whose-own-schema-is-unsupported",
    ],
)
def test_unsupported_schema_vocabulary_is_rejected_at_construction(schema, expected):
    with pytest.raises(ValueError, match=expected):
        _entry(schema)


def test_every_shipped_schema_survives_its_own_guard():
    for name, entry in commands.COMMAND_TABLE.items():
        commands._assert_supported_schema(entry.params_schema, name)


def test_matches_type_raises_on_a_type_it_does_not_implement():
    with pytest.raises(ValueError, match="unsupported JSON Schema type"):
        commands._matches_type("x", "objekt")


# ── dispatch: the JSON-RPC error taxonomy (C2) ───────────────────────────────


DISPATCH_CASES = [
    ("abort", {}),
    ("get_state", {}),
    ("get_messages", {}),
    ("get_commands", {}),
    ("get_tools", {}),
    ("get_capabilities", {}),
    ("submit", {"text": "hi", "source": "rpc", "submitter": "t", "submission_id": "s-1"}),
    ("prompt", {"text": "hi"}),
]


@pytest.mark.parametrize("method,params", DISPATCH_CASES, ids=[c[0] for c in DISPATCH_CASES])
async def test_every_table_verb_dispatches_to_a_result(handler, method, params):
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

    (response,) = await _drain(handler)
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    assert "result" in response and "error" not in response


@pytest.mark.parametrize("method,params", DISPATCH_CASES, ids=[c[0] for c in DISPATCH_CASES])
async def test_every_response_echoes_its_method(handler, method, params):
    """D2: `result.method` names the verb, for every verb in the table."""
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

    (response,) = await _drain(handler)
    assert response["result"]["method"] == method


@pytest.mark.parametrize("method", ["nope", "get_prompt", "get_state "])
async def test_an_unknown_method_gets_32601_and_preserves_the_id(handler, method):
    await handler._handle_request({"jsonrpc": "2.0", "id": 9, "method": method})

    (response,) = await _drain(handler)
    assert response["id"] == 9
    assert response["error"]["code"] == dialect.METHOD_NOT_FOUND
    assert method in response["error"]["message"]
    assert response["error"]["data"]["method"] == method


async def test_send_tool_result_is_declined_not_missing(handler):
    """A declined verb is still `METHOD_NOT_FOUND` on the wire (C2) — the
    capability document, not a bare call, is how a host learns WHY (C1)."""
    await handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "send_tool_result", "params": {}}
    )

    (response,) = await _drain(handler)
    assert response["error"]["code"] == dialect.METHOD_NOT_FOUND
    assert "declined" in response["error"]["message"]
    assert "AgentLoop executes tool calls itself" in response["error"]["message"]


@pytest.mark.parametrize(
    "method",
    [
        "cycle_model",
        "cycle_thinking_level",
        "set_steering_mode",
        "set_follow_up_mode",
        "export_html",
        "bash",
    ],
)
async def test_the_2c_tier_d_verbs_are_declined_not_missing(handler, method):
    """C1: every Tier D verb §4[3] names is declined WITH a reason on the
    wire (METHOD_NOT_FOUND carrying `declined_because`), same shape as
    `send_tool_result` — not simply absent from the table."""
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})

    (response,) = await _drain(handler)
    assert response["error"]["code"] == dialect.METHOD_NOT_FOUND
    assert "declined" in response["error"]["message"]
    entry = commands.COMMAND_TABLE[method]
    assert entry.declined_because in response["error"]["message"]


@pytest.mark.parametrize(
    "request_obj",
    [{}, {"method": ""}, {"method": 5}, {"method": None}, "not-a-dict", None, 42],
    ids=["no-method", "empty-method", "int-method", "null-method", "string", "none", "int"],
)
async def test_a_malformed_request_gets_32600(handler, request_obj):
    await handler._handle_request(request_obj)  # type: ignore[arg-type]

    (response,) = await _drain(handler)
    assert response["error"]["code"] == dialect.INVALID_REQUEST


async def test_bad_params_get_32602_and_name_the_violation(handler):
    await handler._handle_request({"jsonrpc": "2.0", "id": 4, "method": "prompt", "params": {}})

    (response,) = await _drain(handler)
    assert response["error"]["code"] == dialect.INVALID_PARAMS
    assert "text" in response["error"]["message"]
    assert response["error"]["data"]["method"] == "prompt"


async def test_extra_params_are_also_32602(handler):
    await handler._handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "abort", "params": {"bogus": True}}
    )

    (response,) = await _drain(handler)
    assert response["error"]["code"] == dialect.INVALID_PARAMS


@pytest.mark.parametrize("method", ["submit", "prompt"])
async def test_multitask_strategy_fork_is_rejected_not_silently_no_opd(handler, method, session):
    """S3 (phase-2 review): 'fork' is a real, published enum value — a host
    naming it must get an informative -32602, never `{"accepted": true}`
    followed by permanent silence (its events reach the 'branch_event'
    channel, which RPCHandler does not forward)."""
    params: dict[str, Any] = {"text": "go", "multitask_strategy": "fork"}
    if method == "submit":
        params.update(source="rpc", submitter="t", submission_id="s-1")

    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

    (response,) = await _drain(handler)
    assert "result" not in response
    assert response["error"]["code"] == dialect.INVALID_PARAMS
    assert "fork" in response["error"]["message"]
    assert "open_lane" in response["error"]["message"]
    assert response["error"]["data"]["multitask_strategy"] == "fork"
    # Never admitted: AgentSession.submit() must not have been reached at all.
    session.submit.assert_not_called()


async def test_multitask_strategy_steer_is_unaffected_by_the_fork_decline(handler, session):
    """The one strategy S3 explicitly leaves alone: 'steer' lands in the
    in-flight turn's own observable stream, so it is not rejected."""
    await handler._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "prompt",
            "params": {"text": "go", "multitask_strategy": "steer"},
        }
    )

    (response,) = await _drain(handler)
    assert "error" not in response
    assert response["result"]["accepted"] is True
    session.submit.assert_called_once()


async def test_a_failing_handler_becomes_32603_not_a_crash(handler, session):
    """A handler raising something it did not raise on purpose must not take
    the RPC loop down with it, and must not be confused with a declared
    rejection (C3's `SUBMISSION_REJECTED`)."""
    session.abort.side_effect = RuntimeError("boom")

    await handler._handle_request({"jsonrpc": "2.0", "id": 3, "method": "abort"})

    (response,) = await _drain(handler)
    assert response["id"] == 3
    assert response["error"]["code"] == dialect.INTERNAL_ERROR
    assert response["error"]["message"] == "boom"
    assert response["error"]["data"]["method"] == "abort"


# ── individual handlers (mocked session) ─────────────────────────────────────


async def test_abort_delegates_and_is_idempotent(handler, session):
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "abort"})
    await handler._handle_request({"jsonrpc": "2.0", "id": 2, "method": "abort"})

    responses = await _drain(handler)
    assert [r["result"]["status"] for r in responses] == ["aborted", "aborted"]
    assert all("cursor" not in r["result"] for r in responses)
    assert session.abort.call_count == 2

    assert all(r["result"]["compaction_id"] is None for r in responses)
    assert commands.validate_params(commands.ABORT_RESULT_SCHEMA, responses[0]["result"]) is None
    assert commands.validate_params(commands.ABORT_RESULT_SCHEMA, {"status": "aborted"}) is not None


def test_prepare_outbound_stamps_the_cursor_only_onto_agent_end(handler, session):
    """B1's dequeue-time cursor stamp: `RPCHandler.prepare_outbound` reads the
    CAPTURED log's cursor (`_cursor_log`, Finding 2 — see
    `_stamp_agent_end_cursor`'s docstring) and sets it on an `agent_end`
    event's params — and touches nothing else (an `abort` response never
    gets a `params` dict at all; a `turn_start` event is not the completion
    E5 cares about, and never carries `_cursor_log` in the first place —
    only `_forward_event` attaches one, and only for `agent_end`)."""
    captured_log = MagicMock()
    captured_log.cursor = "post-turn-cursor"

    agent_end_item = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": "agent_end"},
        "_cursor_log": captured_log,
    }
    handler.prepare_outbound(agent_end_item)
    assert agent_end_item["params"]["cursor"] == "post-turn-cursor"
    assert "_cursor_log" not in agent_end_item  # popped, never serialized

    turn_start_item = {"jsonrpc": "2.0", "method": "event", "params": {"type": "turn_start"}}
    handler.prepare_outbound(turn_start_item)
    assert "cursor" not in turn_start_item["params"]

    response_item = {"jsonrpc": "2.0", "id": 1, "result": {"status": "aborted"}}
    handler.prepare_outbound(response_item)  # must not raise
    assert "params" not in response_item


def test_the_transport_does_not_know_what_an_agent_end_is():
    """X1 (§7.3): block [1] frames and orders bytes. It must not read event
    types or reach into the session — otherwise a socket transport inherits
    the wire's semantics along with its framing. The cursor stamp lives on
    `RPCHandler.prepare_outbound`, which the writer calls blind."""
    source = Path(transport.__file__).read_text()
    for leaked in ("agent_end", "session_log", "AgentEvent"):
        assert leaked not in source, (
            f"{leaked!r} appears in rpc/transport.py — event semantics have "
            "leaked back into block [1]. Put it behind RPCHandler.prepare_outbound()."
        )


async def test_get_state_aggregates_the_session(handler, session):
    session.is_streaming = True
    session.messages = [{"role": "user"}, {"role": "assistant"}]
    session.get_usage.return_value = {"input_tokens": 3}

    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_state"})

    (response,) = await _drain(handler)
    assert response["result"] == {
        "session_id": "sess-1",
        "status": "idle",
        "is_streaming": True,
        "model": {"id": "gpt-4o", "provider": "openai", "context_window": 8192},
        "usage": {"input_tokens": 3},
        "message_count": 2,
        "cursor": "leaf-1",
        "addressable": True,
        "method": "get_state",
    }


async def test_addressable_is_false_on_a_log_that_declares_no_location(real_handler):
    """`InMemorySessionLog` has neither `path` nor `root_doc_id` — the "declares
    nothing" arm of `_declared_durable_locations`."""
    await real_handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_state"})

    (response,) = await _drain(real_handler)
    assert response["result"]["addressable"] is False


async def test_addressable_is_false_on_a_declared_location_that_is_none(real_session):
    """The other arm, and the one that matters for `--no-session`: the file
    store's ephemeral `Session` DOES declare `path`, and sets it to None.

    Asserted separately from the test above because the two arms reach
    `session_log_is_addressable` through different branches — `bool(declared)`
    vs `any(value is not None ...)` — and a rewrite that collapses them to
    `hasattr` alone passes the first and fails this one.
    """

    class _EphemeralLog(InMemorySessionLog):
        path = None

    real_session.session_log = _EphemeralLog()
    handler = RPCHandler(real_session)
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_state"})

    (response,) = await _drain(handler)
    assert response["result"]["addressable"] is False


async def test_addressable_is_true_on_a_declared_location_that_is_set(real_session):
    """The positive case, so the field is a function of the log rather than a
    constant. `path` is the file store's attribute name; any non-None value in
    `_DURABLE_LOCATION_ATTRS` is what "persisted" means at this layer."""

    class _PersistedLog(InMemorySessionLog):
        path = "/somewhere/real.jsonl"

    real_session.session_log = _PersistedLog()
    handler = RPCHandler(real_session)
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_state"})

    (response,) = await _drain(handler)
    assert response["result"]["addressable"] is True


async def test_get_messages_pulls_the_terminal_array(handler, session):
    session.messages = [{"role": "user"}, {"role": "assistant"}]

    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_messages"})

    (response,) = await _drain(handler)
    assert response["result"]["messages"] == session.messages


async def test_get_commands_enumerates_builtins_and_extension_commands(handler, session):
    # §6 "One thing to keep dynamic": enumerated per call, never a table.
    session.get_extension_commands.return_value = [("notes", "jot something down")]

    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_commands"})

    (response,) = await _drain(handler)
    listed = {c["name"]: c for c in response["result"]["commands"]}
    assert set(listed) == set(FRONTEND_COMMANDS) | {"notes"}
    assert listed["compact"]["origin"] == "builtin"
    assert listed["notes"]["origin"] == "extension"
    assert listed["notes"]["description"] == "jot something down"
    assert all(c["description"] for c in listed.values())


async def test_get_commands_does_not_advertise_a_shadowed_extension_command(handler, session):
    session.get_extension_commands.return_value = [("compact", "the extension's own compact")]

    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_commands"})

    (response,) = await _drain(handler)
    listed = [c for c in response["result"]["commands"] if c["name"] == "compact"]
    assert len(listed) == 1
    assert listed[0]["origin"] == "builtin"
    assert listed[0]["description"] == FRONTEND_COMMANDS["compact"]


async def test_get_tools_reports_name_description_and_schema(handler, session):
    from tau_agent_core.tools.base import AgentTool, ToolDefinition

    session.tools = [
        AgentTool(
            definition=ToolDefinition(
                name="bash",
                label="Bash",
                description="Execute bash commands",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}},
                execute=lambda ctx: "done",
            )
        )
    ]

    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_tools"})

    (response,) = await _drain(handler)
    (tool,) = response["result"]["tools"]
    assert tool["name"] == "bash"
    assert tool["description"] == "Execute bash commands"
    assert tool["parameters"]["properties"]["command"] == {"type": "string"}


async def test_get_tools_on_a_session_with_none(handler):
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_tools"})
    (response,) = await _drain(handler)
    assert response["result"]["tools"] == []


async def test_get_tools_over_a_real_session_with_builtin_tools():
    """get_tools works on a session built the way production builds one (B1)."""
    from tau_agent_core.sdk import create_agent_session

    names = ["read", "write", "edit", "bash", "ls", "grep", "find"]
    real_session = create_agent_session(model="gpt-4o", tools=names)
    tools = (await commands._handle_get_tools(RPCHandler(real_session), None, {}))["tools"]

    assert {t["name"] for t in tools} == set(names)
    for tool in tools:
        assert isinstance(tool["description"], str) and tool["description"]
        assert isinstance(tool["parameters"], dict) and tool["parameters"]


async def test_a_core_performed_commands_output_rides_the_acceptance_response(handler, session):
    """A `Performed` already ran (an extension-registered command) and produced
    text — any host can render a string, so it rides the ONE response this
    submission will ever get (no turn ran, so there is no `agent_end` to carry it
    instead)."""
    session.submit = AsyncMock(
        return_value=SubmissionResult(
            accepted=True,
            submission_id="s-1",
            command=Performed(flow=None, mutation="ledger", data={"output": "42"}),
        )
    )

    await handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "/ledger week"}}
    )

    (response,) = await _drain(handler)
    assert response["result"]["accepted"] is True
    assert response["result"]["command"] == {
        "name": "ledger",
        "output": "42",
    }


async def test_a_view_comes_back_as_a_success_carrying_its_reason(handler, session):
    """A `View` is a surface the core decided WHAT it is and did not open.

    It used to be COMMAND_NOT_SUPPORTED. It is a SUCCESS response now, because the
    tree payload has to land somewhere: putting it in an error and moving it later
    is two breaks instead of one. Not a silent no-op — `unavailable_because` is a
    sentence a host prints, the idiom the table's seven `declined_because` entries
    already use."""
    session.submit = AsyncMock(
        return_value=SubmissionResult(
            accepted=True,
            submission_id="s-1",
            command=View(name="tree", unavailable_because="τ projects no tree state yet"),
        )
    )

    await handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "/tree"}}
    )

    (response,) = await _drain(handler)
    assert "error" not in response
    assert response["result"]["accepted"] is True
    assert response["result"]["view"] == {
        "name": "tree",
        "state": None,
        "unavailable_because": "τ projects no tree state yet",
    }


async def test_a_ready_flow_errors_and_names_the_verb_to_call_instead(handler, session):
    """The two arms only a head performs still refuse, each with its own reason.

    `tau_agent_core.commands`'s own module docstring is explicit that a frontend
    which cannot perform an arm "must raise UnsupportedCommandError rather than
    return silently" — the wire's version is COMMAND_NOT_SUPPORTED, and it now says
    which verb does the same job."""
    session.submit = AsyncMock(
        return_value=SubmissionResult(
            accepted=True,
            submission_id="s-1",
            command=Ready(flow="compact", mutation="compact", arguments={}),
        )
    )

    await handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "/compact"}}
    )

    (response,) = await _drain(handler)
    assert "result" not in response
    assert response["error"]["code"] == dialect.COMMAND_NOT_SUPPORTED
    assert "'compact'" in response["error"]["message"]
    assert response["error"]["data"]["command"] == "compact"


async def test_get_capabilities_matches_the_capabilities_module(handler):
    """The handler is a one-line pass-through to `capabilities
    .build_capabilities()` — verified by literal equality rather than
    re-deriving the shape here, so this test cannot itself drift from what
    `test_rpc_capabilities.py` pins down about that function's content."""
    from tau_agent_core.rpc import capabilities

    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"})

    (response,) = await _drain(handler)
    expected = capabilities.build_capabilities()
    expected["method"] = "get_capabilities"
    assert response["result"] == expected


# ── the permanent event-forwarding subscription ─────────────────────────────


def test_construction_subscribes_once_for_the_whole_handler_lifetime(session):
    handler = RPCHandler(session)
    session.subscribe.assert_called_once_with(handler._forward_event)


async def test_forward_event_enqueues_a_notification(handler):
    event = AgentEvent(type="turn_start", timestamp=1, turn_index=0)
    await handler._forward_event(event)

    assert handler._output_queue.qsize() == 1
    item = handler._output_queue.get_nowait()
    assert item["method"] == "event"
    assert item["params"]["type"] == "turn_start"
    assert "id" not in item


async def test_forward_event_resets_the_projector_on_turn_start(handler):
    """E1: the projector is per-handler but reset per turn (see
    RPCHandler.__init__'s comment on `_delta_projector` for why turn_start,
    specifically, is the correct reset point for this subscription)."""
    await handler._forward_event(
        AgentEvent(
            type="message_update",
            timestamp=1,
            message={"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
        )
    )
    await handler._forward_event(AgentEvent(type="turn_start", timestamp=2, turn_index=1))
    await handler._forward_event(
        AgentEvent(
            type="message_update",
            timestamp=3,
            message={"role": "assistant", "content": [{"type": "text", "text": "Hi"}]},
        )
    )

    items = []
    while not handler._output_queue.empty():
        items.append(handler._output_queue.get_nowait())

    deltas = [i["params"]["delta"] for i in items if i["params"]["type"] == "message_update"]
    assert deltas == ["Hello", "Hi"]


async def test_forward_event_carries_provenance_when_a_submission_drove_it(handler):
    """E4: the quad rides every wire event a submission drove."""
    event = AgentEvent(
        type="turn_start",
        timestamp=1,
        turn_index=0,
        submission_id="sub-1",
        source="rpc",
        submitter="t",
        correlation={"bus_subject": "x"},
    )
    await handler._forward_event(event)
    (item,) = [handler._output_queue.get_nowait() for _ in range(handler._output_queue.qsize())]
    assert item["params"]["submission_id"] == "sub-1"
    assert item["params"]["source"] == "rpc"
    assert item["params"]["submitter"] == "t"
    assert item["params"]["correlation"] == {"bus_subject": "x"}


async def test_forward_event_carries_null_provenance_when_no_submission_drove_it(handler):
    """E4: 'null when none did' — never a fabricated id."""
    event = AgentEvent(type="turn_start", timestamp=1, turn_index=0)
    await handler._forward_event(event)
    (item,) = [handler._output_queue.get_nowait() for _ in range(handler._output_queue.qsize())]
    assert item["params"]["submission_id"] is None
    assert item["params"]["source"] is None
    assert item["params"]["submitter"] is None
    assert item["params"]["correlation"] is None


async def test_forward_event_drops_non_diffable_block_deltas(handler):
    """E1/G3: a toolCall block change produces no wire event (see
    rpc/wire_events.py's module docstring)."""
    event = AgentEvent(
        type="message_update",
        timestamp=1,
        message={
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {}}],
        },
    )
    await handler._forward_event(event)
    assert handler._output_queue.qsize() == 0


async def test_forward_event_agent_end_carries_a_count_not_the_messages(handler):
    """E2: agent_end announces completion with counts, never the array."""
    event = AgentEvent(
        type="agent_end",
        timestamp=1,
        messages=[{"role": "user"}, {"role": "assistant"}],
    )
    await handler._forward_event(event)
    (item,) = [handler._output_queue.get_nowait() for _ in range(handler._output_queue.qsize())]
    assert item["params"]["message_count"] == 2
    assert "messages" not in item["params"]


def _cache_less_completion(timestamp: int, submission_id: str | None = "s1") -> AgentEvent:
    return AgentEvent(
        type="message_end",
        timestamp=timestamp,
        submission_id=submission_id,
        message={
            "role": "assistant",
            "content": [],
            "usage": {
                "cache_read_tokens": 0,
                "cache_reported": True,
                "output_tokens": 10,
                "total_tokens": 30_010,
            },
        },
    )


async def _drain(handler) -> list[dict]:
    return [handler._output_queue.get_nowait() for _ in range(handler._output_queue.qsize())]


async def test_agent_end_carries_the_prompt_cache_notice(handler):
    """The one head-agnostic delivery: an RPC host reads the notice off a field
    rather than needing τ's own renderer (docs/PROMPT-CACHING.md §7)."""
    await handler._forward_event(AgentEvent(type="agent_start", timestamp=1, submission_id="s1"))
    await handler._forward_event(_cache_less_completion(2))
    await handler._forward_event(_cache_less_completion(3))
    await handler._forward_event(
        AgentEvent(type="agent_end", timestamp=4, submission_id="s1", messages=[])
    )

    items = await _drain(handler)
    agent_end = [i for i in items if i["params"]["type"] == "agent_end"][-1]
    assert "2 calls" in agent_end["params"]["cache_notice"]


async def test_a_healthy_turn_leaves_the_notice_null(handler):
    warm = _cache_less_completion(3)
    warm.message["usage"]["cache_read_tokens"] = 29_000

    await handler._forward_event(AgentEvent(type="agent_start", timestamp=1, submission_id="s1"))
    await handler._forward_event(_cache_less_completion(2))
    await handler._forward_event(warm)
    await handler._forward_event(
        AgentEvent(type="agent_end", timestamp=4, submission_id="s1", messages=[])
    )

    items = await _drain(handler)
    agent_end = [i for i in items if i["params"]["type"] == "agent_end"][-1]
    assert agent_end["params"]["cache_notice"] is None


async def test_every_other_event_type_leaves_the_notice_null(handler):
    await handler._forward_event(AgentEvent(type="agent_start", timestamp=1, submission_id="s1"))
    await handler._forward_event(_cache_less_completion(2))
    items = await _drain(handler)
    assert [i["params"]["cache_notice"] for i in items] == [None] * len(items)


def _truncated_completion(dropped: int | None = None) -> AgentEvent:
    usage: dict = {"total_tokens": 4100, "output_tokens": 4096}
    if dropped is not None:
        usage["extra"] = {"dropped_partial_tool_calls": dropped}
    return AgentEvent(
        type="message_end",
        timestamp=2,
        submission_id="s1",
        message={
            "role": "assistant",
            "content": [],
            "usage": usage,
            "stop_reason": "length",
        },
    )


async def test_message_end_carries_the_completions_stop_reason(handler):
    """Without this a host renders a truncated prefix as a finished answer: the
    stop reason rode inside ``message``, which the wire excludes
    (docs/TRUNCATED-TOOL-CALLS.md §3)."""
    await handler._forward_event(_truncated_completion())
    (item,) = await _drain(handler)
    assert item["params"]["stop_reason"] == "length"


async def test_message_end_carries_the_calls_the_cut_lost(handler):
    await handler._forward_event(_truncated_completion(dropped=2))
    (item,) = await _drain(handler)
    assert item["params"]["dropped_tool_calls"] == 2


async def test_dropping_nothing_leaves_the_count_null_not_zero(handler):
    """0 and "not reported" are different statements, and the provider writes the
    key only when something was lost."""
    await handler._forward_event(_truncated_completion())
    (item,) = await _drain(handler)
    assert item["params"]["dropped_tool_calls"] is None


async def test_the_content_only_duplicate_message_end_carries_neither(handler):
    """The agent loop emits a second ``message_end`` per tool-bearing turn with
    role and content only. It is not a second completion and must not read as
    one that stopped for an unknown reason."""
    await handler._forward_event(
        AgentEvent(
            type="message_end",
            timestamp=3,
            submission_id="s1",
            message={"role": "assistant", "content": []},
        )
    )
    (item,) = await _drain(handler)
    assert item["params"]["stop_reason"] is None
    assert item["params"]["dropped_tool_calls"] is None


async def test_every_other_event_type_leaves_the_stop_reason_null(handler):
    await handler._forward_event(AgentEvent(type="agent_start", timestamp=1, submission_id="s1"))
    await handler._forward_event(
        AgentEvent(type="agent_end", timestamp=4, submission_id="s1", messages=[])
    )
    items = await _drain(handler)
    assert [i["params"]["stop_reason"] for i in items] == [None] * len(items)


# ── background task tracking ─────────────────────────────────────────────────


async def test_track_background_task_holds_and_then_reaps_the_task(handler):
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _work():
        started.set()
        await finish.wait()

    task = asyncio.create_task(_work())
    handler.track_background_task(task)
    await started.wait()

    assert task in handler._background_tasks
    finish.set()
    await task
    await asyncio.sleep(0)  # let the done-callback run
    assert task not in handler._background_tasks


# ── submit / prompt: C3's dual completion, against a real AgentSession ──────


async def test_prompt_acknowledges_before_any_turn_event_reaches_the_wire(real_handler):
    """The core of C3: the acceptance response must be first onto the wire,
    strictly before the turn's own `agent_start` — see
    `commands._submit_and_acknowledge`'s docstring for why this is not
    automatic and how it is guaranteed (a synchronous enqueue from inside
    `on_admitted`, not a Future another task later turns into a response)."""
    gate = asyncio.Event()

    async def _gated_stream_simple(model, context, options=None):
        await gate.wait()
        return _Stream("hi there")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
        await real_handler._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hello"}}
        )
        first = await asyncio.wait_for(real_handler._output_queue.get(), timeout=5.0)

        assert first == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "accepted": True,
                "submission_id": first["result"]["submission_id"],
                "rejection_reason": None,
                "method": "prompt",
            },
        }
        assert "messages" not in first["result"]  # C3: never the turn's messages

        gate.set()
        agent_end = await _drain_until(
            real_handler,
            lambda item: item.get("method") == "event" and item["params"]["type"] == "agent_end",
        )
        assert agent_end["params"]["is_error"] is False


async def test_agent_end_wire_event_carries_the_post_persistence_cursor(real_handler, real_session):
    """B1 (phase-2 review): E5/F3 for the event that actually satisfies it.

    `abort` no longer carries a cursor (it is a signal, not a completion —
    see test_abort_delegates_and_is_idempotent). The resulting cursor
    reaches the host on `agent_end` instead, and it must be the cursor AFTER
    this turn's messages were persisted (`_run_one_turn`'s
    `_persist_loop_messages`, which runs strictly after `AgentLoop.run()`
    returns — i.e. after `agent_end` itself fires) — never the value that
    was current at the moment `agent_end` was emitted, which is stale by
    construction (see `RPCHandler._stamp_agent_end_cursor`). This test is
    also what pins the ordering that stamp depends on: there is no `await`
    between `_forward_event`'s enqueue and `_persist_loop_messages`, so the
    writer cannot dequeue early. Introduce one and this fails. Driven
    through the REAL `_write_stdout` (not the raw `_output_queue`, which
    `_forward_event` populates before persistence has happened) because
    that is the one call site late enough to read the post-persistence
    value.
    """
    pre_turn_cursor = real_session.session_log.cursor

    async def _fast_stream_simple(model, context, options=None):
        return _Stream("hi there")

    recorder = _Recorder()
    real_handler._real_stdout = recorder
    real_handler._running = True
    pump = asyncio.create_task(real_handler._write_stdout())

    def _has_agent_end() -> bool:
        return any(
            line.get("method") == "event" and line["params"].get("type") == "agent_end"
            for line in recorder.lines
        )

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fast_stream_simple):
        await real_handler._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hello"}}
        )
        for _ in range(200):
            if _has_agent_end():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("agent_end never reached the recorder")

    real_handler._running = False
    await asyncio.wait_for(pump, timeout=5.0)
    recorder.close()
    recorder.join()

    (agent_end,) = [
        line
        for line in recorder.lines
        if line.get("method") == "event" and line["params"].get("type") == "agent_end"
    ]
    post_turn_cursor = real_session.session_log.cursor
    assert post_turn_cursor != pre_turn_cursor
    assert agent_end["params"]["cursor"] == post_turn_cursor


async def test_agent_end_cursor_survives_a_session_log_swap_before_dequeue(
    real_handler, real_session
):
    """Finding 2 (phase-3 review): `_stamp_agent_end_cursor` used to resolve
    WHICH log to read the cursor from at dequeue time too
    (`self._session.session_log`, live) — correct for reading the cursor's
    VALUE late (post-persistence, see the previous test), wrong once
    `session_log` can be REPLACED between enqueue and dequeue
    (`new_session`/`fork`/`switch_session`, phase 3). The reviewer's wire
    reproduction: a peer that stops reading leaves `agent_end` queued while a
    swap lands, and the response comes back carrying the NEW session's
    cursor (`None` for a fresh session) instead of the turn's real tip.

    Mirrors that by grabbing the raw queued item WITHOUT draining through
    `_write_stdout` (same technique `test_prepare_outbound_stamps_the_cursor
    _only_onto_agent_end` uses) so the swap can land before `prepare_outbound`
    ever runs on it.
    """

    async def _fast_stream_simple(model, context, options=None):
        return _Stream("hi there")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fast_stream_simple):
        await real_handler._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hello"}}
        )
        agent_end_item = await _drain_until(
            real_handler,
            lambda item: item.get("method") == "event" and item["params"]["type"] == "agent_end",
        )

    old_log = real_session.session_log
    old_cursor = old_log.cursor
    assert old_cursor is not None  # sanity: the turn really persisted something

    real_session.session_log = InMemorySessionLog()
    assert real_session.session_log.cursor is None

    real_handler.prepare_outbound(agent_end_item)

    assert agent_end_item["params"]["cursor"] == old_cursor


async def test_get_messages_reflects_the_turn_only_after_it_runs(real_handler):
    async def _fast_stream_simple(model, context, options=None):
        return _Stream("hi there")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fast_stream_simple):
        await real_handler._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hello"}}
        )
        await _drain_until(
            real_handler,
            lambda item: item.get("method") == "event" and item["params"]["type"] == "agent_end",
        )

    await real_handler._handle_request({"jsonrpc": "2.0", "id": 2, "method": "get_messages"})
    response = await _drain_until(real_handler, lambda item: item.get("id") == 2)
    assert [m["role"] for m in response["result"]["messages"]] == ["user", "assistant"]


async def test_a_rejected_submission_errors_on_the_response(real_handler):
    """C3: "A rejected prompt errors on the response." A second submission
    with the default `multitask_strategy="reject"` while a turn is in flight
    gets `SUBMISSION_REJECTED`, not a `result`."""
    gate = asyncio.Event()

    async def _gated_stream_simple(model, context, options=None):
        await gate.wait()
        return _Stream("A's reply")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
        await real_handler._handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "submit",
                "params": {
                    "text": "turn A",
                    "source": "rpc",
                    "submitter": "t",
                    "submission_id": "a-1",
                    "multitask_strategy": "enqueue",
                },
            }
        )
        await asyncio.wait_for(real_handler._output_queue.get(), timeout=5.0)  # A's ack

        await real_handler._handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "submit",
                "params": {
                    "text": "turn B",
                    "source": "rpc",
                    "submitter": "t",
                    "submission_id": "b-1",
                },
            }
        )
        rejection = await _drain_until(real_handler, lambda item: item.get("id") == 2)

        assert "result" not in rejection
        assert rejection["error"]["code"] == dialect.SUBMISSION_REJECTED
        assert rejection["error"]["message"] == "a turn is already in flight"
        assert rejection["error"]["data"] == {"method": "submit", "submission_id": "b-1"}

        gate.set()


async def test_prompt_defaults_provenance_submit_requires_it(real_handler):
    """§10 decision 10: `prompt` accepts bare `text`; `submit` requires the
    full quad (C2: `-32602` when it is missing)."""

    async def _fast_stream_simple(model, context, options=None):
        return _Stream("ok")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fast_stream_simple):
        await real_handler._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hi"}}
        )
        ack = await asyncio.wait_for(real_handler._output_queue.get(), timeout=5.0)
        assert ack["result"]["accepted"] is True
        await _drain_until(
            real_handler,
            lambda item: item.get("method") == "event" and item["params"]["type"] == "agent_end",
        )

    await real_handler._handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "submit", "params": {"text": "hi"}}
    )
    (response,) = [r for r in await _drain(real_handler) if r.get("id") == 2]
    assert response["error"]["code"] == dialect.INVALID_PARAMS


async def test_a_resolved_extension_command_reaches_the_host_end_to_end():
    """B2, against the REAL admission machinery — no mocked `session.submit`.
    A submission that resolves to an extension-registered command via
    `expand_commands` must produce exactly ONE response, carrying the
    command's output, and must NOT hang waiting for a second completion that
    is never coming (no turn ran, so there is no `agent_end`)."""

    def my_ext(api):
        api.register_command("ledger", {"description": "d", "handler": lambda a, c: "42"})

    session = AgentSession(
        session_log=InMemorySessionLog(), model=_model(), tools=[], extensions=[my_ext]
    )
    real_handler = RPCHandler(session)

    await real_handler._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "prompt",
            "params": {"text": "/ledger", "expand_commands": True},
        }
    )

    (response,) = await asyncio.wait_for(_drain(real_handler), timeout=5.0)
    assert response["result"]["accepted"] is True
    assert response["result"]["command"] == {
        "name": "ledger",
        "output": "42",
    }
    assert real_handler._output_queue.empty()


async def test_a_resolved_frontend_command_errors_end_to_end(real_handler):
    """The same path, for a built-in (`/compact`) that needs a screen the
    RPC wire does not have — 2C's own get_commands notes say a host submits
    these as text with expand_commands: true, so this IS the documented
    invocation the review named."""
    await real_handler._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "prompt",
            "params": {"text": "/compact", "expand_commands": True},
        }
    )

    (response,) = await asyncio.wait_for(_drain(real_handler), timeout=5.0)
    assert "result" not in response
    assert response["error"]["code"] == dialect.COMMAND_NOT_SUPPORTED
    assert "/compact" in response["error"]["message"]


# ── RPCError ──────────────────────────────────────────────────────────────


def test_rpc_error_carries_code_message_and_data():
    err = commands.RPCError(dialect.SUBMISSION_REJECTED, "nope", data={"submission_id": "x"})
    assert err.code == dialect.SUBMISSION_REJECTED
    assert err.message == "nope"
    assert err.data == {"submission_id": "x"}
    assert str(err) == "nope"


def test_project_event_non_message_types_are_1to1(real_handler):
    """Every non-message_update event type projects to exactly one payload,
    carrying the trimmed field set (no message/args/result/tool_results)."""
    event = AgentEvent(
        type="tool_execution_end",
        timestamp=123,
        tool_call_id="c1",
        tool_name="bash",
        is_error=False,
    )
    (payload,) = wire_events.project_event(real_handler._delta_projector, event)
    assert payload["type"] == "tool_execution_end"
    assert payload["timestamp"] == 123
    assert payload["tool_call_id"] == "c1"
    assert payload["tool_name"] == "bash"
    assert payload["is_error"] is False
    for excluded in ("message", "args", "result", "tool_results", "messages"):
        assert excluded not in payload


def test_project_event_agent_end_carries_count_not_messages(real_handler):
    event = AgentEvent(type="agent_end", timestamp=1, messages=[{"role": "user"}])
    (payload,) = wire_events.project_event(real_handler._delta_projector, event)
    assert payload["message_count"] == 1
    assert "messages" not in payload


def test_project_event_agent_end_carries_the_error_reason(real_handler):
    """B3 (phase-2 review): AgentEvent.error must reach the wire, not just be
    declared on WireEvent's schema. This is the level the schema-only tests
    in test_rpc_event_schema.py cannot catch — WireEvent's own `error` field
    defaults to None, so a `_wire_event()` that never copies `event.error`
    across still constructs successfully and every schema-shape assertion
    there still passes. Only exercising the real projection function against
    a real AgentEvent with `.error` SET proves the value actually crosses,
    which is exactly the gap that let this field go missing for this
    module's entire life (neither projected nor declared excluded)."""
    event = AgentEvent(
        type="agent_end",
        timestamp=1,
        messages=[],
        is_error=True,
        error="RuntimeError: Connection refused",
    )
    (payload,) = wire_events.project_event(real_handler._delta_projector, event)
    assert payload["is_error"] is True
    assert payload["error"] == "RuntimeError: Connection refused"


def test_project_event_normal_agent_end_has_no_error(real_handler):
    """The paired case: a clean close must not fabricate a reason."""
    event = AgentEvent(type="agent_end", timestamp=1, messages=[], is_error=False)
    (payload,) = wire_events.project_event(real_handler._delta_projector, event)
    assert payload["is_error"] is False
    assert payload["error"] is None


def test_project_event_message_update_yields_a_bounded_delta(real_handler):
    projector = real_handler._delta_projector
    first = AgentEvent(
        type="message_update",
        timestamp=1,
        message={"role": "assistant", "content": [{"type": "text", "text": "Hi"}]},
    )
    second = AgentEvent(
        type="message_update",
        timestamp=2,
        message={"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]},
    )
    (p1,) = wire_events.project_event(projector, first)
    (p2,) = wire_events.project_event(projector, second)
    assert p1["delta"] == "Hi"
    assert p1["block_type"] == "text"
    assert p1["replace"] is False
    assert p2["delta"] == " there"  # bounded: the SUFFIX, not the cumulative text


def test_project_event_replace_case_resets_not_appends(real_handler):
    """E1: BlockDelta.replace=True means the receiver must RESET its
    accumulator to `delta`, not append it — the defect the TUI's backends.py
    deliberately ignores (its own comment says so) and the wire must not."""
    projector = real_handler._delta_projector
    first = AgentEvent(
        type="message_update",
        timestamp=1,
        message={"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
    )
    second = AgentEvent(
        type="message_update",
        timestamp=2,
        message={"role": "assistant", "content": [{"type": "text", "text": "Goodbye"}]},
    )
    (p1,) = wire_events.project_event(projector, first)
    (p2,) = wire_events.project_event(projector, second)
    assert p1["replace"] is False
    assert p2["replace"] is True
    assert p2["delta"] == "Goodbye"  # the WHOLE new value, not a suffix of "Hello"


def test_project_event_drops_non_diffable_block_and_returns_nothing(real_handler):
    event = AgentEvent(
        type="message_update",
        timestamp=1,
        message={
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {}}],
        },
    )
    assert wire_events.project_event(real_handler._delta_projector, event) == []


def test_project_event_returns_one_wireevent_per_changed_diffable_block(real_handler):
    """message_update is not 1:1 — a single AgentEvent carrying BOTH a
    thinking and a text change (a ToolCallDeltaEvent-triggered update can, per
    event_projection.py's docstring) projects to two wire events, each tagged
    with its own block_type."""
    projector = real_handler._delta_projector
    event = AgentEvent(
        type="message_update",
        timestamp=1,
        message={
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "pondering"},
                {"type": "text", "text": "Hi"},
            ],
        },
    )
    payloads = wire_events.project_event(projector, event)
    by_kind = {p["block_type"]: p["delta"] for p in payloads}
    assert by_kind == {"thinking": "pondering", "text": "Hi"}


def _apply_wire_delta(acc: str, payload: dict) -> str:
    """The consumer-side reconstruction rule WireEvent's contract demands —
    the wire analogue of test_event_projection.py's `_apply`."""
    return payload["delta"] if payload["replace"] else acc + payload["delta"]


async def test_rt6_concatenated_wire_deltas_reproduce_final_text_many_chunks(handler):
    """A real multi-chunk stream — 40 one-token-ish chunks — not a two-chunk
    smoke test, driven through RPCHandler._forward_event exactly as the agent
    loop would call it (one AgentEvent per provider chunk)."""
    words = (
        "The quick brown fox jumps over the lazy dog while a second sentence "
        "keeps the stream going long enough to matter for this test of "
        "reconstruction across many small chunks arriving one at a time."
    ).split(" ")
    assert len(words) >= 30

    await handler._forward_event(AgentEvent(type="turn_start", timestamp=_TS, turn_index=0))

    acc_text = ""
    running = ""
    for i, word in enumerate(words):
        running = word if i == 0 else running + " " + word
        await handler._forward_event(
            AgentEvent(
                type="message_update",
                timestamp=i + 1,
                message={"role": "assistant", "content": [{"type": "text", "text": running}]},
            )
        )

    payloads = []
    while not handler._output_queue.empty():
        item = handler._output_queue.get_nowait()
        if item["params"]["type"] == "message_update":
            payloads.append(item["params"])

    assert len(payloads) == len(words)  # one bounded delta per chunk, none dropped
    for payload in payloads:
        acc_text = _apply_wire_delta(acc_text, payload)

    assert acc_text == running == " ".join(words)


async def test_rt6_concatenated_wire_deltas_reproduce_final_text_with_a_replace(handler):
    """Same obligation, but the stream includes a provider replace-not-extend
    chunk partway through (the defensive case) — the RPC path must apply
    `replace` correctly, unlike backends.py's TurnStream (see that module's
    own comment, deliberately not fixed by this unit)."""
    await handler._forward_event(AgentEvent(type="turn_start", timestamp=_TS, turn_index=0))

    snapshots = ["I think", "I think the", "Actually, scratch that", "Actually, scratch that."]
    for i, snap in enumerate(snapshots):
        await handler._forward_event(
            AgentEvent(
                type="message_update",
                timestamp=i + 1,
                message={"role": "assistant", "content": [{"type": "text", "text": snap}]},
            )
        )

    payloads = [
        handler._output_queue.get_nowait()["params"] for _ in range(handler._output_queue.qsize())
    ]
    payloads = [p for p in payloads if p["type"] == "message_update"]

    acc = ""
    for payload in payloads:
        acc = _apply_wire_delta(acc, payload)

    assert acc == snapshots[-1]
    assert any(p["replace"] for p in payloads)  # the case actually fired


# ── JSON-RPC 2.0 compliance ─────────────────────────────────────────────────


async def test_responses_and_errors_are_well_formed(handler):
    await handler._send_response(1, {"ok": True})
    await handler._send_error(2, dialect.INTERNAL_ERROR, "boom")
    success, failure = await _drain(handler)

    assert success == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert failure == {"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "boom"}}
    # result and error are mutually exclusive — never both on one response.
    assert "error" not in success and "result" not in failure


async def test_an_error_with_data_carries_it(handler):
    await handler._send_error(1, dialect.METHOD_NOT_FOUND, "nope", data={"method": "nope"})
    (response,) = await _drain(handler)
    assert response["error"]["data"] == {"method": "nope"}


async def test_a_notification_error_carries_a_null_id(handler):
    """An unparseable line has no recoverable id, so the response must say so."""
    await handler._send_error(None, dialect.PARSE_ERROR, "Parse error")
    (response,) = await _drain(handler)
    assert response["id"] is None
