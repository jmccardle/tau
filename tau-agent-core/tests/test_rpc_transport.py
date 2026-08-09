"""Transport-hygiene tests for tau_agent_core.rpc.RPCHandler.

Covers requirement IDs T1, T2, T5, T6, P1, P4 (docs/REMOTE-CONTROL.md
section 4, blocks [1] and [7]) plus R-T5 (section 9):

- T1: LF-only framing on stdin, CRLF tolerance, no str.splitlines() (which
  also breaks on U+2028/U+2029 — legal characters inside a JSON string).
- T2 / R-T5: stdout takeover — sys.stdout is claimed exclusively for the
  protocol writer, and a stray print() from a tool/subscriber must not
  corrupt the JSON-RPC stream.
- T5: FIFO write ordering is the protocol's only ordering guarantee.
- T6: a write failure (broken pipe) propagates and terminates rather than
  being swallowed.
- P1 / P4: SIGTERM -> exit code 143 (flush skipped), SIGHUP -> 129 (flush
  performed), stdin EOF is a clean (non-signal) shutdown.

These tests exercise real stdin/stdout via OS pipes rather than mocking
sys.stdin/sys.stdout with in-memory buffers, because the behaviour under
test — framing on raw bytes, exclusive claim of the real fd, FIFO ordering
of concurrent writes, broken-pipe propagation, signal-triggered shutdown —
is only real on an actual pipe.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import io
import json
import os
import signal
import sys
import threading
from types import SimpleNamespace
from typing import BinaryIO

import pytest

from tau_agent_core import rpc
from tau_agent_core.rpc import RPCHandler, capabilities, dialect, transport


# =============================================================================
# Helpers
# =============================================================================


class _FakeStdin:
    """Stand-in for `sys.stdin` exposing only the `.buffer` attribute that
    `RPCHandler._read_stdin` actually uses."""

    def __init__(self, buffer: BinaryIO) -> None:
        self.buffer = buffer


def _stdin_pipe() -> tuple[_FakeStdin, BinaryIO]:
    """A stdin fake backed by a real OS pipe, plus the writer end.

    Closing the returned writer signals EOF to the reader, exactly like a
    real peer closing its stdout.
    """
    read_fd, write_fd = os.pipe()
    reader = _FakeStdin(os.fdopen(read_fd, "rb"))
    writer = os.fdopen(write_fd, "wb")
    return reader, writer


def _stdin_from_bytes(data: bytes) -> _FakeStdin:
    """A stdin fake pre-loaded with `data`, already at EOF."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, data)
    os.close(write_fd)
    return _FakeStdin(os.fdopen(read_fd, "rb"))


def _stdin_fed_by_thread(data: bytes) -> tuple[_FakeStdin, threading.Thread]:
    """A stdin fake fed by a background thread, for payloads larger than a
    pipe buffer.

    `_stdin_from_bytes` writes its whole payload BEFORE anything reads, so
    anything past the OS pipe buffer (64 KiB on Linux) deadlocks the test
    process in `os.write`. Feeding from a thread lets the reader drain while
    the write proceeds; closing the write end at the end of the thread is
    still the EOF the reader shuts down on.
    """
    read_fd, write_fd = os.pipe()

    def _feed() -> None:
        with os.fdopen(write_fd, "wb") as out:
            out.write(data)

    thread = threading.Thread(target=_feed, daemon=True)
    thread.start()
    return _FakeStdin(os.fdopen(read_fd, "rb")), thread


def _mock_session() -> SimpleNamespace:
    """A minimal stand-in for AgentSession sufficient for RPCHandler."""

    async def _prompt(text: str, images: object | None) -> list[dict]:
        return [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]

    return SimpleNamespace(
        _model=SimpleNamespace(id="mock-model"),
        _tools=[],
        messages=[],
        is_streaming=False,
        is_aborted=False,
        shutdown_requested=False,
        subscribe=lambda fn: None,
        abort=lambda: None,
        prompt=_prompt,
    )


@pytest.fixture(autouse=True)
def _reset_stdout_takeover():
    """Guard against one failing test leaking the stdout claim into others."""
    yield
    while rpc.is_stdout_taken_over():
        rpc._release_stdout()


# =============================================================================
# T1 — framing
# =============================================================================


class TestFraming:
    async def test_lf_only_tolerates_trailing_cr(self, monkeypatch):
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(
            sys,
            "stdin",
            _stdin_from_bytes(b'{"jsonrpc":"2.0","id":1,"method":"get_session_info"}\r\n'),
        )
        await handler._read_stdin()
        assert received == ['{"jsonrpc":"2.0","id":1,"method":"get_session_info"}']

    async def test_bare_lf_without_cr_is_unaffected(self, monkeypatch):
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(b'{"a":1}\n{"a":2}\n'))
        await handler._read_stdin()
        assert received == ['{"a":1}', '{"a":2}']

    async def test_does_not_split_on_u2028_inside_json_string(self, monkeypatch):
        """U+2028 (LINE SEPARATOR) is a legal, unescaped character inside a
        JSON string. Framing must split only on LF, never on it -- this is
        exactly the bug str.splitlines() would introduce."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "send_prompt",
            "params": {"text": "line one\u2028line two\u2029line three"},
        }
        # ensure_ascii=False so the LINE/PARAGRAPH SEPARATOR is emitted as a
        # literal UTF-8 character, not escaped as the six-char "\u2028" --
        # the raw-byte case that would actually break str.splitlines().
        line = json.dumps(payload, ensure_ascii=False)
        assert "\u2028" in line and "\u2029" in line
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes((line + "\n").encode("utf-8")))
        await handler._read_stdin()
        # Exactly one line -- U+2028/U+2029 did not fragment it.
        assert received == [line]
        # And it round-trips: the separators survive inside the string value.
        parsed = json.loads(received[0])
        assert parsed["params"]["text"] == "line one\u2028line two\u2029line three"

    async def test_final_line_without_trailing_newline_is_still_processed(self, monkeypatch):
        """A last line with no trailing newline at EOF must not be silently
        dropped."""
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(b'{"a":1}\n{"a":2}'))  # no trailing LF
        await handler._read_stdin()
        assert received == ['{"a":1}', '{"a":2}']

    async def test_invalid_json_line_produces_error_not_exception(self, monkeypatch):
        handler = RPCHandler(_mock_session())
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(b"not json at all\n"))
        await handler._read_stdin()
        item = handler._output_queue.get_nowait()
        assert "error" in item

    def test_read_path_never_uses_splitlines(self):
        """Regression guard: str.splitlines() also breaks on U+2028/U+2029
        and must never appear in the actual read-path code. Checked at the
        bytecode level (`co_names`) rather than by scanning source text,
        since the docstrings here legitimately *talk about*
        `str.splitlines()` to explain why it's avoided."""
        for fn in (rpc.RPCHandler._read_stdin, rpc.RPCHandler._handle_line):
            assert "splitlines" not in fn.__code__.co_names, fn.__qualname__


def _record(sink: list[str]):
    async def _handler(line: str) -> None:
        sink.append(line)

    return _handler


# =============================================================================
# T7 — the inbound request-line bound (review finding 9)
# =============================================================================
#
# The defect these pin, reproduced against a real child before the fix:
#
#     get_state ok    -> {'jsonrpc': '2.0', 'id': 1, 'result': {...}}
#     100KB prompt    -> NO RESPONSE: stdout closed
#     rc: 1
#
# `_read_stdin` built a bare `asyncio.StreamReader()`, inherited the stdlib's
# 64 KiB limit, and `readline()`'s `ValueError` went uncaught out of
# `handler.run()`. One oversized `prompt` -- a pasted file, a stack trace, a
# diff -- and the process was gone, with no JSON-RPC error and no T4 stderr
# line to explain it.
#
# The bound is exercised at TWO sizes on purpose. Most tests here shrink
# `MAX_REQUEST_LINE_BYTES` to a few hundred bytes (`small_bound`) so the
# MECHANISM -- the boundary, the resync, the error shape, one error per line
# -- is driven exhaustively without pushing megabytes through a pipe.
# `test_a_100_kib_line_the_stdlib_default_would_have_killed_is_dispatched`
# then runs at the REAL shipped bound, so a change that quietly reverted the
# number to the stdlib's own is caught by something no monkeypatched test
# could see. `test_rpc_line_limit.py` (tau-coding-agent) drives the shipped
# number end to end against a real child.


_SMALL_BOUND = 512


@pytest.fixture
def small_bound(monkeypatch):
    """Shrink T7's bound for one test. Patched on the `transport` module
    itself, which is where `_read_stdin` reads it from on every call --
    binding it any other way would test a copy rather than the constant."""
    monkeypatch.setattr(transport, "MAX_REQUEST_LINE_BYTES", _SMALL_BOUND)
    return _SMALL_BOUND


def _request_line_of(size: int) -> str:
    """A syntactically real request line of exactly `size` bytes (ASCII, so
    bytes and characters coincide)."""
    skeleton = '{"jsonrpc":"2.0","id":1,"method":"get_state","params":{"pad":""}}'
    assert size >= len(skeleton), f"a request line cannot be shorter than {len(skeleton)}"
    line = skeleton.replace('"pad":""', '"pad":"' + "x" * (size - len(skeleton)) + '"')
    assert len(line) == size
    return line


def _drain_queue(handler: RPCHandler) -> list[dict]:
    items: list[dict] = []
    while not handler._output_queue.empty():
        items.append(handler._output_queue.get_nowait())
    return items


async def _until(predicate, timeout: float = 5.0) -> None:
    """Wait for `predicate()` while the reader task runs. Used only where the
    stdin pipe is deliberately held OPEN -- the point of those tests is what
    happens before EOF, so they cannot simply await the reader to completion."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached within the timeout")


class TestRequestLineBound:
    async def test_a_line_of_exactly_the_bound_is_dispatched(self, monkeypatch, small_bound):
        """The boundary is where the constant says it is: `MAX_REQUEST_LINE_
        BYTES` bytes of line content is ACCEPTED (the LF is framing and does
        not count against it)."""
        line = _request_line_of(small_bound)
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(line.encode() + b"\n"))

        await handler._read_stdin()

        assert received == [line]
        assert _drain_queue(handler) == []

    async def test_one_byte_past_the_bound_is_refused_and_never_parsed(
        self, monkeypatch, small_bound
    ):
        """...and refused is refused: the line does not reach `_handle_line`
        at all, so nothing downstream ever sees the bytes the bound exists to
        not handle."""
        line = _request_line_of(small_bound + 1)
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(line.encode() + b"\n"))

        await handler._read_stdin()

        assert received == []
        (item,) = _drain_queue(handler)
        assert item["error"]["code"] == dialect.REQUEST_TOO_LARGE
        assert item["id"] is None, "the id lives in bytes that were deliberately never parsed"
        assert item["error"]["data"] == {
            "max_request_line_bytes": small_bound,
            "observed_bytes": small_bound + 1,
            "line_complete": True,
        }

    async def test_the_process_survives_and_the_next_request_is_served(
        self, monkeypatch, small_bound
    ):
        """The whole point (T7): one enormous line must not cost the host its
        connection. The oversized line here ENDS in valid JSON, so a resync
        that merely cleared its buffer and read on would dispatch that tail as
        a request -- `received` naming only the real follow-up is what rules
        that out."""
        oversized = "A" * (small_bound + 10) + '{"tail":"not a request"}'
        good = _request_line_of(120)
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(
            sys, "stdin", _stdin_from_bytes(oversized.encode() + b"\n" + good.encode() + b"\n")
        )

        await handler._read_stdin()

        assert received == [good]
        errors = _drain_queue(handler)
        assert len(errors) == 1, "exactly one refusal for one over-long line"
        assert errors[0]["error"]["code"] == dialect.REQUEST_TOO_LARGE

    async def test_an_oversized_line_arriving_in_pieces_is_refused_exactly_once(
        self, monkeypatch, small_bound
    ):
        """A host does not write its line in one syscall, and the reader does
        not read it in one either. Refusing per CHUNK past the bound instead
        of per LINE would answer one request with a burst of errors."""
        stdin_fake, writer = _stdin_pipe()
        monkeypatch.setattr(sys, "stdin", stdin_fake)
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        task = asyncio.create_task(handler._read_stdin())
        try:
            for _ in range(20):  # 20 * 100 bytes, well past a 512-byte bound
                writer.write(b"B" * 100)
                writer.flush()
                await asyncio.sleep(0)
            writer.write(b"\n" + _request_line_of(120).encode() + b"\n")
            writer.flush()
            await _until(lambda: len(received) == 1)
        finally:
            writer.close()
            await asyncio.wait_for(task, timeout=5.0)

        assert received == [_request_line_of(120)]
        errors = _drain_queue(handler)
        assert len(errors) == 1, f"expected one refusal, got {len(errors)}: {errors}"
        assert errors[0]["error"]["data"]["line_complete"] is False
        assert errors[0]["error"]["data"]["observed_bytes"] > small_bound

    async def test_an_endless_line_is_refused_while_it_is_still_arriving(
        self, monkeypatch, small_bound
    ):
        """The hostile case: a peer that sends and sends and never sends an
        LF. The refusal must land WHILE the pipe is still open and the peer
        still writing -- that is what makes the reader's memory flat rather
        than "one line's worth of whatever the peer feels like sending", and
        it is what gives the host a chance to stop.

        Deliberately asserted before any EOF: a refusal that only arrived once
        the peer gave up would satisfy every other test in this class and
        still be an unbounded buffer.
        """
        stdin_fake, writer = _stdin_pipe()
        monkeypatch.setattr(sys, "stdin", stdin_fake)
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        task = asyncio.create_task(handler._read_stdin())
        try:
            writer.write(b"C" * (small_bound + 1))  # no LF, and none coming yet
            writer.flush()
            await _until(lambda: not handler._output_queue.empty())
            refusal = handler._output_queue.get_nowait()
            assert refusal["error"]["code"] == dialect.REQUEST_TOO_LARGE
            assert refusal["error"]["data"]["line_complete"] is False

            # ...and the peer keeps going, for a good while longer than the
            # bound, without earning a second error or a second byte of
            # retained buffer.
            for _ in range(10):
                writer.write(b"C" * small_bound)
                writer.flush()
                await asyncio.sleep(0)
            assert handler._output_queue.empty(), "a refused line must be refused once"

            # Finally an LF: the connection resynchronizes on it and the very
            # next line is served as though nothing had happened.
            good = _request_line_of(120)
            writer.write(b"\n" + good.encode() + b"\n")
            writer.flush()
            await _until(lambda: received == [good])
        finally:
            writer.close()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_the_refusal_is_announced_on_stderr_too(self, monkeypatch, small_bound, capsys):
        """T4: stderr is a documented log channel. An operator whose host went
        quiet should be able to see why without holding the protocol stream."""
        handler = RPCHandler(_mock_session())
        handler._handle_line = _record([])  # type: ignore[method-assign]
        monkeypatch.setattr(
            sys, "stdin", _stdin_from_bytes(("D" * (small_bound + 1)).encode() + b"\n")
        )

        await handler._read_stdin()

        err = capsys.readouterr().err
        assert "tau rpc: Request line refused" in err
        assert str(small_bound) in err

    async def test_a_final_unterminated_line_under_the_bound_is_still_dispatched(
        self, monkeypatch, small_bound
    ):
        """The bound must not have quietly cost T1's other promise: a last
        line the peer never terminated is a request, not a fragment to drop."""
        line = _request_line_of(small_bound)
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(line.encode()))  # no LF, then EOF

        await handler._read_stdin()

        assert received == [line]

    async def test_a_final_unterminated_line_over_the_bound_is_refused_not_dispatched(
        self, monkeypatch, small_bound
    ):
        """...and the previous test's tolerance is not a hole in the bound."""
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(b"E" * (_SMALL_BOUND + 1)))

        await handler._read_stdin()

        assert received == []
        (item,) = _drain_queue(handler)
        assert item["error"]["code"] == dialect.REQUEST_TOO_LARGE

    async def test_a_100_kib_line_the_stdlib_default_would_have_killed_is_dispatched(
        self, monkeypatch
    ):
        """The reproduction itself, at the SHIPPED bound -- no fixture, no
        patched constant. 100 KB is comfortably past the stdlib
        `StreamReader`'s 64 KiB default, which is what used to raise
        `ValueError: Separator is found, but chunk is longer than limit` out
        of `handler.run()` and end the process.

        Reverting `MAX_REQUEST_LINE_BYTES` to the stdlib's own default is a
        mutation only this test can see: every other test in this class
        patches the number away.
        """
        line = _request_line_of(100 * 1024)
        assert len(line) > 64 * 1024
        assert len(line) < transport.MAX_REQUEST_LINE_BYTES
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        stdin_fake, feeder = _stdin_fed_by_thread(line.encode() + b"\n")
        monkeypatch.setattr(sys, "stdin", stdin_fake)

        await handler._read_stdin()
        feeder.join(timeout=5.0)

        assert received == [line]
        assert _drain_queue(handler) == []

    def test_the_advertised_limit_is_the_readers_own_constant(self):
        """K1/T7: `get_capabilities` publishes the bound so a host can size
        its requests instead of discovering the ceiling by hitting it. The
        published number is READ OFF the reader's constant, so the document
        cannot promise 8 MiB while the reader refuses at 64 KiB -- which is
        precisely the drift that makes an advertised limit worse than none.
        """
        doc = capabilities.build_capabilities()
        assert doc["limits"]["max_request_line_bytes"] == transport.MAX_REQUEST_LINE_BYTES

    def test_the_advertised_limit_tracks_the_constant_rather_than_copying_it(self, monkeypatch):
        """The test above passes just as well against a hand-copied literal.
        This one does not: move the constant and the document must move with
        it."""
        monkeypatch.setattr(transport, "MAX_REQUEST_LINE_BYTES", 4242)
        assert capabilities.build_capabilities()["limits"]["max_request_line_bytes"] == 4242

    def test_the_generated_protocol_doc_states_the_bound_and_the_refusal(self):
        """K3: the published reference is the other half of "discoverable".

        Asserted on `protocol_doc.render()` rather than on the checked-in
        `docs/RPC-PROTOCOL.md`, because that file is regenerated once at the
        end of the round -- this pins that the generator HAS the material,
        which is what makes the regeneration produce it.
        """
        from tau_agent_core.rpc import protocol_doc

        rendered = protocol_doc.render()
        assert "max_request_line_bytes" in rendered
        assert str(transport.MAX_REQUEST_LINE_BYTES) in rendered
        # The error TABLE's own row, not merely the name somewhere in the
        # page: the Limits section names `REQUEST_TOO_LARGE` in prose too, so
        # a bare substring check passes even when the error table has lost
        # the code entirely (demonstrated -- it survived that mutation).
        assert f"| `{dialect.REQUEST_TOO_LARGE}` | `REQUEST_TOO_LARGE` |" in rendered

    def test_the_error_code_is_its_own_and_not_an_overloaded_one(self):
        """C2: every failure mode gets a distinct code. A refusal at the
        framing layer is neither a parse failure (the bytes were never given
        to `json.loads`) nor an invalid Request object (no such judgement was
        formed), and it is certainly not an unplanned handler crash."""
        assert dialect.REQUEST_TOO_LARGE == -32003
        assert dialect.REQUEST_TOO_LARGE not in {
            dialect.PARSE_ERROR,
            dialect.INVALID_REQUEST,
            dialect.METHOD_NOT_FOUND,
            dialect.INVALID_PARAMS,
            dialect.INTERNAL_ERROR,
            dialect.SUBMISSION_REJECTED,
            dialect.COMMAND_NOT_SUPPORTED,
            dialect.TURN_STILL_RUNNING,
        }
        assert -32099 <= dialect.REQUEST_TOO_LARGE <= -32000, (
            "JSON-RPC 2.0 reserves -32000..-32099 for implementation-defined "
            "server errors; a code outside it is not ours to define"
        )


# =============================================================================
# T7's sibling — a request line that is not UTF-8 (finding 9, blockers #4)
# =============================================================================
#
# Found while fixing the line bound, verified against a real child, and NOT
# fixed by that unit (a different input class, mid-round, in a shared tree):
#
#     printf '\xff\xfe\n{"jsonrpc":"2.0","id":1,"method":"get_state"}\n' | tau --mode rpc
#     rc: 1 ; stdout: b'' ; UnicodeDecodeError: 'utf-8' codec can't decode
#     byte 0xff in position 0: invalid start byte
#
# Two hostile bytes: no JSON-RPC error, no T4 stderr line, and the well-formed
# request BEHIND them lost with the process. Structurally the same availability
# defect T7 fixed one framing rule over, so it gets the same answer -- refuse
# the line, keep the connection -- with `PARSE_ERROR` rather than a new code
# (see `_refuse_undecodable_request` for why the two cases differ on that).


_UNDECODABLE = b"\xff\xfe"


class TestUndecodableRequestLine:
    async def test_an_undecodable_line_is_refused_and_the_next_request_is_served(self, monkeypatch):
        """The reproduction, line for line. The follow-up request is the
        load-bearing assertion: before the fix there was no reader left to
        serve it, so a test that only checked for an error could pass against
        a process that then died anyway."""
        good = _request_line_of(120)
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(
            sys, "stdin", _stdin_from_bytes(_UNDECODABLE + b"\n" + good.encode() + b"\n")
        )

        await handler._read_stdin()

        assert received == [good]
        (item,) = _drain_queue(handler)
        assert item["error"]["code"] == dialect.PARSE_ERROR
        assert item["id"] is None, "the id is inside the bytes that could not be read"

    async def test_the_refusal_names_where_the_bytes_went_wrong(self, monkeypatch):
        """`data` carries the byte offset, which is what turns "your input is
        corrupt" into a place to look. The offending bytes themselves are not
        echoed -- they are by definition not encodable into this response."""
        line = b'{"jsonrpc":"2.0","id":1,"method":"get_' + _UNDECODABLE + b'"}'
        handler = RPCHandler(_mock_session())
        handler._handle_line = _record([])  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(line + b"\n"))

        await handler._read_stdin()

        (item,) = _drain_queue(handler)
        assert item["error"]["data"] == {
            "encoding": "utf-8",
            "reason": "invalid start byte",
            "byte_offset": line.index(_UNDECODABLE),
            "offending_bytes": len(line),
        }

    async def test_each_bad_line_earns_its_own_refusal_and_no_others(self, monkeypatch):
        """One error per LINE: two corrupt lines are two answers, and a good
        line between them is still dispatched rather than swept up in a
        resync."""
        good = _request_line_of(120)
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(
            sys,
            "stdin",
            _stdin_from_bytes(_UNDECODABLE + b"\n" + good.encode() + b"\n" + _UNDECODABLE + b"\n"),
        )

        await handler._read_stdin()

        assert received == [good]
        errors = _drain_queue(handler)
        assert [e["error"]["code"] for e in errors] == [dialect.PARSE_ERROR] * 2

    async def test_the_refusal_is_announced_on_stderr_too(self, monkeypatch, capsys):
        """T4, for the same reason the over-long refusal has one."""
        handler = RPCHandler(_mock_session())
        handler._handle_line = _record([])  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(_UNDECODABLE + b"\n"))

        await handler._read_stdin()

        err = capsys.readouterr().err
        assert "tau rpc: Request line refused" in err
        assert "not valid UTF-8" in err

    async def test_a_final_unterminated_undecodable_line_is_refused_not_fatal(self, monkeypatch):
        """T1 dispatches a last line the peer never terminated, which routes it
        through the decode by a different path (the EOF branch supplies the LF).
        The refusal has to cover that path too, or the defect survives at EOF."""
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(_UNDECODABLE))  # no LF, then EOF

        await handler._read_stdin()

        assert received == []
        (item,) = _drain_queue(handler)
        assert item["error"]["code"] == dialect.PARSE_ERROR

    async def test_legal_non_ascii_utf8_is_not_swept_up_by_the_refusal(self, monkeypatch):
        """The other half: refusing undecodable bytes must not become refusing
        interesting ones. Emoji, an astral-plane character and a raw U+2028 --
        legal, unescaped, inside a JSON string -- go through byte-for-byte,
        which is also T1's no-`splitlines` promise restated at the decode."""
        line = '{"jsonrpc":"2.0","id":1,"method":"prompt","params":{"text":" 🜂 é 𝄞"}}'
        handler = RPCHandler(_mock_session())
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(line.encode("utf-8") + b"\n"))

        await handler._read_stdin()

        assert received == [line]
        assert _drain_queue(handler) == []


# =============================================================================
# P3 — extension-requested shutdown, checked after each command
# =============================================================================


class TestExtensionRequestedShutdown:
    async def test_stops_the_reader_after_the_triggering_line_not_before(self, monkeypatch):
        """docs/REMOTE-CONTROL.md §4[7] P3: 'checked after each command
        rather than polled' — the check must run once `_handle_line`
        returns, and the SECOND line must never even be parsed once
        `shutdown_requested` flips true handling the first.

        Revert the check in `_read_stdin` and this fails: `received` would
        gain a second entry.
        """
        session = _mock_session()
        handler = RPCHandler(session)
        received: list[str] = []

        async def _handle_line(line: str) -> None:
            received.append(line)
            session.shutdown_requested = True  # an extension's ctx.shutdown(), simulated

        handler._handle_line = _handle_line  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(b'{"a":1}\n{"a":2}\n'))
        await handler._read_stdin()
        assert received == ['{"a":1}']

    async def test_a_blank_line_is_not_a_command_and_does_not_trigger_the_check(self, monkeypatch):
        """A skipped blank line must not itself be treated as 'a command
        handled' — only an actually-dispatched line counts."""
        session = _mock_session()
        handler = RPCHandler(session)
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(b'\n{"a":1}\n\n{"a":2}\n'))
        await handler._read_stdin()
        assert received == ['{"a":1}', '{"a":2}']

    async def test_never_requested_reads_to_eof_as_usual(self, monkeypatch):
        session = _mock_session()
        handler = RPCHandler(session)
        received: list[str] = []
        handler._handle_line = _record(received)  # type: ignore[method-assign]
        monkeypatch.setattr(sys, "stdin", _stdin_from_bytes(b'{"a":1}\n{"a":2}\n'))
        await handler._read_stdin()
        assert received == ['{"a":1}', '{"a":2}']


# =============================================================================
# T2 / R-T5 — stdout takeover
# =============================================================================


class TestStdoutTakeover:
    def test_take_over_redirects_stdout_to_stderr(self, monkeypatch):
        fake_out, fake_err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_out)
        monkeypatch.setattr(sys, "stderr", fake_err)

        assert rpc.is_stdout_taken_over() is False
        real = rpc._take_over_stdout()
        assert real is fake_out
        assert sys.stdout is fake_err
        assert rpc.is_stdout_taken_over() is True

        rpc._release_stdout()
        assert sys.stdout is fake_out
        assert rpc.is_stdout_taken_over() is False

    def test_take_over_is_reference_counted_for_nesting(self, monkeypatch):
        fake_out, fake_err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_out)
        monkeypatch.setattr(sys, "stderr", fake_err)

        first = rpc._take_over_stdout()
        second = rpc._take_over_stdout()  # nested claim (2nd RPCHandler / double-start)
        assert first is second is fake_out
        assert sys.stdout is fake_err

        rpc._release_stdout()  # one claim released...
        assert sys.stdout is fake_err  # ...stdout stays claimed
        assert rpc.is_stdout_taken_over() is True

        rpc._release_stdout()  # last claim released
        assert sys.stdout is fake_out
        assert rpc.is_stdout_taken_over() is False

    def test_release_without_take_over_raises(self):
        """An unbalanced release is always a caller bug (Fail Early) -- it
        must not be absorbed, or a double release could quietly release
        someone else's still-active claim."""
        assert rpc.is_stdout_taken_over() is False
        with pytest.raises(RuntimeError):
            rpc._release_stdout()
        assert rpc.is_stdout_taken_over() is False

    def test_release_restores_and_clears_the_real_stdout_handle(self, monkeypatch):
        """After the last release, the module no longer holds a stale
        handle to a prior run's stdout."""
        fake_out, fake_err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_out)
        monkeypatch.setattr(sys, "stderr", fake_err)

        rpc._take_over_stdout()
        rpc._release_stdout()
        assert rpc._real_stdout is None

    async def test_print_during_prompt_does_not_corrupt_protocol_stream(self, monkeypatch):
        """R-T5: a subscriber/tool that prints must not corrupt the stream —
        the client must still be able to parse every line."""
        stdin_fake, stdin_writer = _stdin_pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()
        stdout_writer = os.fdopen(stdout_write_fd, "w")

        monkeypatch.setattr(sys, "stdin", stdin_fake)
        monkeypatch.setattr(sys, "stdout", stdout_writer)

        session = _mock_session()

        async def noisy_prompt(text: str, images: object | None) -> list[dict]:
            # Simulate a rogue tool/extension writing straight to stdout.
            print("I AM A ROGUE TOOL WRITING DIRECTLY TO STDOUT")
            print("so is this one", file=sys.stdout)
            return [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]

        session.prompt = noisy_prompt
        handler = RPCHandler(session)

        run_task = asyncio.create_task(handler.run())
        try:
            await asyncio.sleep(0.05)  # let run() take over stdout
            request = (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "send_prompt",
                        "params": {"text": "hi"},
                    }
                )
                + "\n"
            )
            stdin_writer.write(request.encode("utf-8"))
            stdin_writer.flush()
            await asyncio.sleep(0.1)
            stdin_writer.close()  # EOF -> clean shutdown, flushes the queue
            await asyncio.wait_for(run_task, timeout=5)
        finally:
            with contextlib.suppress(OSError):
                stdin_writer.close()

        stdout_writer.close()
        with os.fdopen(stdout_read_fd, "r") as reader:
            raw = reader.read()

        lines = [line for line in raw.split("\n") if line]
        assert lines, "expected at least one protocol line on the real stdout"
        for line in lines:
            parsed = json.loads(line)  # must not raise: every line is valid JSON
            assert parsed["jsonrpc"] == "2.0"
        assert "ROGUE" not in raw
        assert "so is this one" not in raw


# =============================================================================
# T5 / T6 — FIFO ordering and write-failure propagation
# =============================================================================


class TestWriterContract:
    async def test_fifo_order_preserved_under_load(self):
        handler = RPCHandler(_mock_session())
        read_fd, write_fd = os.pipe()
        handler._real_stdout = os.fdopen(write_fd, "w")  # type: ignore[assignment]
        reader = os.fdopen(read_fd, "r")

        n = 500
        for i in range(n):
            handler._output_queue.put_nowait({"seq": i})
        handler._running = False

        try:
            await handler._write_stdout()
        finally:
            handler._real_stdout.close()

        try:
            lines = [line for line in reader.read().split("\n") if line]
        finally:
            reader.close()

        assert len(lines) == n
        seqs = [json.loads(line)["seq"] for line in lines]
        assert seqs == list(range(n))

    async def test_write_failure_propagates_and_is_not_swallowed(self):
        handler = RPCHandler(_mock_session())
        # A real fd is required now (`_connect_stdout_writer` needs one to
        # set up the pipe transport at all — see TestWriterContract's own
        # docstring precedent below); the injected failure happens one
        # layer up, at `_write_line`, before this fd is ever actually
        # written to.
        read_fd, write_fd = os.pipe()
        handler._real_stdout = os.fdopen(write_fd, "w")  # type: ignore[assignment]
        handler._output_queue.put_nowait({"seq": 0})
        handler._running = False

        def _boom(writer: object, data: str) -> None:
            raise BrokenPipeError("peer gone")

        handler._write_line = _boom  # type: ignore[method-assign]

        try:
            with pytest.raises(BrokenPipeError):
                await handler._write_stdout()
        finally:
            handler._real_stdout.close()
            os.close(read_fd)

    async def test_write_stdout_does_not_catch_arbitrary_exceptions(self):
        """Same contract as above, generalized: _write_stdout must not have
        a broad except that swallows write errors of any kind."""
        handler = RPCHandler(_mock_session())
        read_fd, write_fd = os.pipe()
        handler._real_stdout = os.fdopen(write_fd, "w")  # type: ignore[assignment]
        handler._output_queue.put_nowait({"seq": 0})
        handler._running = False

        def _boom(writer: object, data: str) -> None:
            raise RuntimeError("disk full, or whatever")

        handler._write_line = _boom  # type: ignore[method-assign]

        try:
            with pytest.raises(RuntimeError, match="disk full"):
                await handler._write_stdout()
        finally:
            handler._real_stdout.close()
            os.close(read_fd)


class TestWriterCancellationLeavesNoThreadBehind:
    async def test_cancelling_a_stalled_write_does_not_leave_a_blocked_thread_behind(self):
        """Blocker 2 (phase-4 review): the OLD writer ran its blocking
        `write()`/`flush()` on a `ThreadPoolExecutor` thread
        (`loop.run_in_executor`) — cancelling the *task* awaiting it
        detaches without stopping the *thread*, which stays parked in the
        `write()` syscall against a peer that never drains the pipe.
        VERBATIM the hazard `_read_stdin`'s own docstring documents and
        fixed for the reader (`loop.connect_read_pipe`) — the writer was
        simply left behind, and `asyncio.run()`'s own shutdown
        (`loop.shutdown_default_executor()`, called by every real `tau
        --mode rpc` process on the way out) then joins that thread
        forever — measured directly (phase-4 review): a task awaiting
        `run_in_executor` around a genuinely-blocked `os.write()` returns
        from `.cancel()` in ~0ms, but a SUBSEQUENT `executor.shutdown(wait
        =True)` — the exact call `shutdown_default_executor()` makes —
        then hangs indefinitely on the still-running thread.

        The NEW writer (`_connect_stdout_writer`/`loop.connect_write_pipe`)
        has no such thread at all: cancelling a task stuck on `writer
        .drain()` is a complete, genuine asyncio-level cancellation, and
        `shutdown_default_executor()` has nothing left to wait for.

        Revert the writer to the old `loop.run_in_executor(None, self
        ._write_line, line)` shape (keeping this test's own harness
        unchanged) and this fails: `shutdown_default_executor()` times out.
        """
        handler = RPCHandler(_mock_session())
        read_fd, write_fd = os.pipe()
        handler._real_stdout = os.fdopen(write_fd, "w")  # type: ignore[assignment]
        # Nobody ever reads read_fd. One oversized item guarantees `drain()`
        # (or, under the reverted code, the executor-thread `write()`)
        # genuinely blocks — comfortably more than a 64 KiB pipe buffer.
        handler._output_queue.put_nowait({"seq": 0, "pad": "x" * 500_000})
        handler._running = True

        task = asyncio.create_task(handler._write_stdout())
        try:
            await asyncio.sleep(0.3)
            assert not task.done(), "the write did not even stall -- nothing to cancel"

            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=8.0)
            assert task.done(), "cancelling the stalled write task did not complete"

            # The primary assertion: the fixed writer never touches
            # `loop.run_in_executor` at all, so the loop's default
            # executor is never even CREATED (it is created lazily, on
            # first use) -- a direct, structural proof no thread pool was
            # involved. Under the reverted (`run_in_executor`-based)
            # writer this is non-None, because writing the FIRST item
            # (before the oversized one ever stalls) already created it.
            loop = asyncio.get_event_loop()
            executor = loop._default_executor  # type: ignore[attr-defined]
            assert executor is None, (
                "a default ThreadPoolExecutor was created -- the writer used "
                "run_in_executor somewhere, reintroducing the uncancellable-"
                "thread hazard"
            )

            # Secondary, behavioural check for when a REVERTED writer DOES
            # create one: `asyncio.run()`'s own teardown makes exactly this
            # call (`Runner.close()` -> `shutdown_default_executor()` ->
            # `executor.shutdown(wait=True)`) -- if a thread is still
            # parked in a blocking write(), it hangs. Run on a throwaway
            # helper thread (not the executor's own pool -- shutdown()
            # joining its own calling thread would deadlock trivially and
            # prove nothing) so the bound below can actually observe a
            # timeout rather than being blocked by the same hang itself.
            if executor is not None:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as helper:
                    shutdown_future = helper.submit(executor.shutdown, True)
                    await asyncio.wait_for(asyncio.wrap_future(shutdown_future), timeout=3.0)
        finally:
            handler._real_stdout.close()
            os.close(read_fd)


# =============================================================================
# P1 / P4 — signals and shutdown
# =============================================================================


class TestSignalsAndShutdown:
    async def test_on_signal_sigterm_sets_exit_code_143(self):
        handler = RPCHandler(_mock_session())
        handler._stdin_task = asyncio.get_running_loop().create_future()  # not done
        handler._on_signal("SIGTERM")
        assert handler.exit_code == 143
        assert handler._exit_signal == "SIGTERM"
        handler._stdin_task.cancel()

    async def test_on_signal_sighup_sets_exit_code_129(self):
        handler = RPCHandler(_mock_session())
        handler._stdin_task = asyncio.get_running_loop().create_future()
        handler._on_signal("SIGHUP")
        assert handler.exit_code == 129
        assert handler._exit_signal == "SIGHUP"
        handler._stdin_task.cancel()

    async def test_double_run_raises_runtime_error(self):
        handler = RPCHandler(_mock_session())
        handler._running = True
        with pytest.raises(RuntimeError):
            await handler.run()
        handler._running = False  # don't leak state into other tests

    async def test_stop_before_run_is_a_no_op(self):
        handler = RPCHandler(_mock_session())
        await asyncio.wait_for(handler.stop(), timeout=1)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
    async def test_stdin_eof_is_a_clean_shutdown(self, monkeypatch):
        stdin_fake, stdin_writer = _stdin_pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()
        stdout_writer = os.fdopen(stdout_write_fd, "w")
        monkeypatch.setattr(sys, "stdin", stdin_fake)
        monkeypatch.setattr(sys, "stdout", stdout_writer)

        handler = RPCHandler(_mock_session())
        run_task = asyncio.create_task(handler.run())
        await asyncio.sleep(0.05)
        stdin_writer.close()  # EOF
        await asyncio.wait_for(run_task, timeout=5)

        assert handler.exit_code is None
        assert handler._exit_signal is None
        assert handler._running is False
        assert rpc.is_stdout_taken_over() is False

        stdout_writer.close()
        os.close(stdout_read_fd)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
    async def test_sigterm_end_to_end_sets_exit_code_and_stops(self, monkeypatch):
        stdin_fake, stdin_writer = _stdin_pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()
        stdout_writer = os.fdopen(stdout_write_fd, "w")
        monkeypatch.setattr(sys, "stdin", stdin_fake)
        monkeypatch.setattr(sys, "stdout", stdout_writer)

        handler = RPCHandler(_mock_session())
        run_task = asyncio.create_task(handler.run())
        try:
            await asyncio.sleep(0.05)  # let run() install its signal handlers
            # If registration silently failed, os.kill below would hard-kill
            # the pytest process itself instead of failing this test -- see
            # test_signal_registration_failure_propagates_not_swallowed.
            assert handler._registered_signals, "signal registration did not happen"
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(run_task, timeout=5)
        finally:
            with contextlib.suppress(OSError):
                stdin_writer.close()

        assert handler.exit_code == 143
        assert handler._exit_signal == "SIGTERM"
        assert rpc.is_stdout_taken_over() is False

        stdout_writer.close()
        os.close(stdout_read_fd)

    @pytest.mark.skipif(
        not hasattr(signal, "SIGHUP"), reason="SIGHUP does not exist on this platform"
    )
    async def test_sighup_end_to_end_sets_exit_code_and_flushes(self, monkeypatch):
        stdin_fake, stdin_writer = _stdin_pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()
        stdout_writer = os.fdopen(stdout_write_fd, "w")
        monkeypatch.setattr(sys, "stdin", stdin_fake)
        monkeypatch.setattr(sys, "stdout", stdout_writer)

        handler = RPCHandler(_mock_session())
        run_task = asyncio.create_task(handler.run())
        try:
            await asyncio.sleep(0.05)
            # Queue a response before the signal fires, to prove SIGHUP
            # flushes pending output rather than dropping it.
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "get_session_info"}) + "\n"
            stdin_writer.write(request.encode("utf-8"))
            stdin_writer.flush()
            await asyncio.sleep(0.1)
            assert handler._registered_signals, "signal registration did not happen"
            os.kill(os.getpid(), signal.SIGHUP)
            await asyncio.wait_for(run_task, timeout=5)
        finally:
            with contextlib.suppress(OSError):
                stdin_writer.close()

        assert handler.exit_code == 129
        assert handler._exit_signal == "SIGHUP"

        stdout_writer.close()
        with os.fdopen(stdout_read_fd, "r") as reader:
            raw = reader.read()
        lines = [line for line in raw.split("\n") if line]
        # SIGHUP flushes: the queued get_session_info response must have
        # made it out before shutdown, not been dropped.
        assert any(json.loads(line).get("id") == 1 for line in lines)


# =============================================================================
# P1 / P4 — the SIGTERM-skips-flush vs SIGHUP-drains distinction itself
# =============================================================================
#
# The tests above prove exit codes and that *something* gets written on
# SIGHUP. Neither proves the actual substance of P1/P4: that SIGTERM
# specifically abandons whatever is still queued while SIGHUP specifically
# waits for it. These pin the queued-vs-written outcome directly, using a
# deterministic gate (not timing) so a future refactor that collapses the
# two branches together fails here rather than staying green.


class TestSignalFlushBehavior:
    async def _run_with_gated_writer(
        self, monkeypatch, signal_name: str, *, release_gate: bool
    ) -> tuple[RPCHandler, int]:
        """Force a deterministic stall on the FIRST write, then signal.

        Before the writer swap (T6 blocker 2, phase-4 review) this gated
        `_write_line` — which ran in a `ThreadPoolExecutor` thread — via a
        `threading.Event`, released only once the test wanted the write to
        proceed. That mechanism is gone along with the thread it gated: the
        writer now runs entirely on the event loop
        (`_connect_stdout_writer`/`loop.connect_write_pipe`), and blocking
        that thread with a synchronous `Event.wait()` would freeze the
        whole loop, not just the write.

        The REPLACEMENT stall is the real one T3/T6 are actually about: item
        0 carries enough padding to exceed the pipe's kernel buffer (64 KiB,
        the Linux default) — writing it fills the pipe, and
        `writer.drain()` genuinely suspends until something reads, exactly
        like a real peer that has stopped reading. No monkeypatched gate
        needed for the STALL itself; `_write_line` is still wrapped, but
        only to COUNT calls (the test's actual observable).
        """
        stdin_fake, stdin_writer = _stdin_pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()
        stdout_writer = os.fdopen(stdout_write_fd, "w")
        monkeypatch.setattr(sys, "stdin", stdin_fake)
        monkeypatch.setattr(sys, "stdout", stdout_writer)

        handler = RPCHandler(_mock_session())
        n = 10
        # Comfortably more than a 64 KiB pipe buffer, so writing it alone
        # blocks `drain()` with nobody reading the other end.
        handler._output_queue.put_nowait({"seq": 0, "pad": "x" * 200_000})
        for i in range(1, n):
            handler._output_queue.put_nowait({"seq": i})

        call_count = [0]
        real_write_line = RPCHandler._write_line

        def _counting_write_line(writer: object, data: str) -> None:
            call_count[0] += 1
            real_write_line(handler, writer, data)

        handler._write_line = _counting_write_line  # type: ignore[method-assign]

        def _drain_reader() -> None:
            with os.fdopen(stdout_read_fd, "r") as f:
                while f.read(65536):
                    pass

        reader_thread: threading.Thread | None = None

        run_task = asyncio.create_task(handler.run())
        try:
            # Let the writer reach — and, with nobody reading yet, stay
            # stuck on — item 0's oversized `drain()` before signalling.
            for _ in range(200):
                if call_count[0] >= 1:
                    break
                await asyncio.sleep(0.01)
            assert call_count[0] == 1, "writer did not even reach the first item"
            await asyncio.sleep(0.05)  # let it genuinely settle into the blocked drain()
            assert call_count[0] == 1, "writer proceeded past item 0 with nobody reading"
            handler._on_signal(signal_name)
            if release_gate:
                # SIGHUP drains: something has to actually read the pipe
                # NOW for the blocked first write — and everything queued
                # behind it — to ever complete. Started only after the
                # stall above is established, same as the old gate's
                # `Event.set()` timing.
                reader_thread = threading.Thread(target=_drain_reader, daemon=True)
                reader_thread.start()
            await asyncio.wait_for(run_task, timeout=5)
        finally:
            with contextlib.suppress(OSError):
                stdin_writer.close()
            with contextlib.suppress(OSError):
                stdout_writer.close()
            if not release_gate:
                with contextlib.suppress(OSError):
                    os.close(stdout_read_fd)
            if reader_thread is not None:
                reader_thread.join(timeout=5)

        return handler, call_count[0]

    async def test_sigterm_cancels_writer_leaving_queue_unflushed(self, monkeypatch):
        handler, written = await self._run_with_gated_writer(
            monkeypatch, "SIGTERM", release_gate=False
        )
        assert handler.exit_code == 143
        # The writer was cancelled with item 0 still in flight and the
        # rest of the queue untouched -- SIGTERM does not flush (P1).
        assert written == 1
        assert handler._output_queue.qsize() == 9

    async def test_sighup_drains_writer_flushing_the_full_queue(self, monkeypatch):
        handler, written = await self._run_with_gated_writer(
            monkeypatch, "SIGHUP", release_gate=True
        )
        assert handler.exit_code == 129
        # SIGHUP lets the writer drain everything already queued (P4).
        assert written == 10
        assert handler._output_queue.empty()


# =============================================================================
# T6 — a broken pipe stops run() entirely, not just the writer
# =============================================================================


class TestBrokenPipeStopsRun:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
    async def test_broken_pipe_stops_the_reader_instead_of_running_forever(self, monkeypatch):
        """T6 end-to-end: once the writer dies (the peer's read end of
        stdout is gone), run() must stop entirely rather than continuing to
        read, execute, and queue requests into a sink nobody drains."""
        stdin_fake, stdin_writer = _stdin_pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()
        os.close(stdout_read_fd)  # the peer is already gone
        stdout_writer = os.fdopen(stdout_write_fd, "w")
        monkeypatch.setattr(sys, "stdin", stdin_fake)
        monkeypatch.setattr(sys, "stdout", stdout_writer)

        handler = RPCHandler(_mock_session())
        run_task = asyncio.create_task(handler.run())
        try:
            await asyncio.sleep(0.05)
            request = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "get_session_info"}) + "\n"
            stdin_writer.write(request.encode("utf-8"))
            stdin_writer.flush()

            # Poll for run_task to finish *on its own* -- do NOT cancel it
            # ourselves (e.g. via asyncio.wait_for's timeout-driven cancel).
            # That distinction matters: run()'s CancelledError handling
            # cannot tell "the reader was cancelled internally" apart from
            # "something external cancelled run()'s own task" (that's a
            # separate defect, see TestExternalCancellation), so an
            # externally injected cancellation from a test timeout would
            # unwind run() and produce a result *regardless* of whether the
            # T6 fix under test is present -- silently passing this test
            # even with the fix reverted. Polling without cancelling makes
            # "run() just kept running" observable as its own failure.
            for _ in range(20):
                if run_task.done():
                    break
                await asyncio.sleep(0.05)

            assert run_task.done(), (
                "run() did not stop on its own after the writer died -- it "
                "kept the reader running (in production: kept parsing, "
                "executing, and queueing requests) behind a dead peer"
            )
            # `ConnectionResetError`, not `BrokenPipeError`, since the writer
            # swap (T6 blocker 2, phase-4 review): `loop.connect_write_pipe`
            # detects the read end being ALREADY closed via the selector
            # (POLLHUP) at connect time, before any `os.write()` EPIPE is
            # ever attempted, and asyncio's pipe transport reports that as
            # `ConnectionResetError` (verified directly against a real
            # closed-read-end pipe; both are `OSError`/`ConnectionError`
            # subclasses — T5's actual contract is "a write failure
            # propagates", not a specific errno).
            with pytest.raises(ConnectionResetError):
                run_task.result()
        finally:
            with contextlib.suppress(OSError):
                stdin_writer.close()
            with contextlib.suppress(OSError):
                stdout_writer.close()
            if not run_task.done():
                run_task.cancel()
                with contextlib.suppress(BaseException):
                    await run_task

        # run() actually stopped -- it did not park the reader and keep
        # queueing requests behind the dead writer.
        assert handler._running is False
        assert rpc.is_stdout_taken_over() is False


# =============================================================================
# Fail Early: signal-registration failures and outer cancellation
# =============================================================================


class TestFailEarlyOnRegistrationFailure:
    async def test_signal_registration_failure_propagates_not_swallowed(self, monkeypatch):
        """A registration failure that is not a genuine platform limitation
        (NotImplementedError) must not be silently absorbed -- the
        alternative is a handler that is signal-deaf with no warning, for a
        signal whose entire purpose is graceful shutdown."""
        stdin_fake, stdin_writer = _stdin_pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()
        stdout_writer = os.fdopen(stdout_write_fd, "w")
        monkeypatch.setattr(sys, "stdin", stdin_fake)
        monkeypatch.setattr(sys, "stdout", stdout_writer)

        loop = asyncio.get_event_loop()

        def _boom(sig, callback, *args):
            raise RuntimeError("simulated registration failure")

        monkeypatch.setattr(loop, "add_signal_handler", _boom)

        handler = RPCHandler(_mock_session())
        try:
            # Bounded so that if the fix under test regresses (registration
            # failure silently swallowed again), this fails cleanly via
            # TimeoutError instead of hanging forever reading a stdin that
            # nothing will ever close or signal -- a hang here previously
            # took the whole verification run down with it rather than
            # reporting one failing test.
            with pytest.raises(RuntimeError, match="simulated registration failure"):
                await asyncio.wait_for(handler.run(), timeout=5)
        finally:
            with contextlib.suppress(OSError):
                stdin_writer.close()
            with contextlib.suppress(OSError):
                stdout_writer.close()
            with contextlib.suppress(OSError):
                os.close(stdout_read_fd)

        # Cleanup still ran despite the mid-setup exception (finding: setup
        # must live inside the try/finally).
        assert handler._running is False
        assert rpc.is_stdout_taken_over() is False


class TestExternalCancellation:
    async def test_external_cancellation_of_run_task_propagates(self, monkeypatch):
        """If something external (a supervising TaskGroup/gather) cancels
        the task running `run()`, that must propagate as CancelledError,
        not be swallowed as a normal return -- the standard asyncio footgun
        of eating cancellation meant for the outer task."""
        stdin_fake, stdin_writer = _stdin_pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()
        stdout_writer = os.fdopen(stdout_write_fd, "w")
        monkeypatch.setattr(sys, "stdin", stdin_fake)
        monkeypatch.setattr(sys, "stdout", stdout_writer)

        handler = RPCHandler(_mock_session())
        run_task = asyncio.create_task(handler.run())
        await asyncio.sleep(0.05)
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

        assert handler._running is False
        assert rpc.is_stdout_taken_over() is False

        with contextlib.suppress(OSError):
            stdin_writer.close()
        with contextlib.suppress(OSError):
            stdout_writer.close()
        os.close(stdout_read_fd)
