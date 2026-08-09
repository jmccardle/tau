"""RPC transport: framing, stdout takeover, and signal shutdown.

Block [1] Transport, per docs/REMOTE-CONTROL.md section 3/4. Split out of the
former rpc.py (docs/REMOTE-CONTROL.md section 7.3, requirement X1: "Block [1]
is the only code that knows about sys.stdin/sys.stdout. The reader/writer
pair takes a stream abstraction; nothing above block [1] names a file
descriptor.").

The six ``RPCHandler`` transport methods below are defined here as plain
functions taking the handler instance as their first argument (still named
``self``, to keep this move a pure relocation with no logic edits), then
composed back onto the class in handler.py via `` _read_stdin =
transport._read_stdin`` and so on — a set of module-level functions that
``RPCHandler`` composes, rather than transport code living in the class body.

Reference: docs/PHASE-6-SUBPHASE-0.md
Reference: docs/SUBPHASE-0.0.md lines 260-340
Reference: docs/tau-coding-agent.md lines 220-280
Reference: docs/IMPLEMENTATION-PLAN.md lines 460-500
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from typing import TYPE_CHECKING, TextIO

from tau_agent_core.rpc import dialect

if TYPE_CHECKING:
    from tau_agent_core.rpc.handler import RPCHandler


# =============================================================================
# T7 — the inbound request-line bound
# =============================================================================
#
# Tier B review, finding 9. `_read_stdin` used to build a bare
# `asyncio.StreamReader()` and inherit the stdlib's 64 KiB `_limit`, which
# `readline()` enforces by RAISING `ValueError: Separator is found, but chunk
# is longer than limit`. Nothing caught it: one `prompt` carrying a pasted
# file, a stack trace or a diff took the whole process down — no JSON-RPC
# error, no T4 stderr line, exit 1, and every later request on that
# connection lost with it. Measured end to end against a real child: a 100 KB
# prompt produced "NO RESPONSE: stdout closed".
#
# The fix is deliberately BOTH halves, because either alone is a defect:
#
# - **The bound is raised**, from 64 KiB to `MAX_REQUEST_LINE_BYTES`. 64 KiB
#   is below what an ordinary host legitimately sends; a limit that a correct
#   host trips over in normal use is not a limit, it is a bug with a number
#   attached.
# - **The bound is enforced, loudly, and survivably.** Fail Early: a silently
#   bigger number is the same defect one order of magnitude later. An
#   over-long line is refused with `REQUEST_TOO_LARGE`, announced on stderr
#   (T4), and DISCARDED THROUGH ITS NEXT LF so the connection resynchronizes
#   on real framing instead of parsing the tail of a rejected request as a
#   fresh one. The process does not die and the next well-formed line is
#   served normally.
#
# And the bound is DISCOVERABLE, in three places, so no host has to learn it
# by failing: `get_capabilities` publishes it as
# `limits.max_request_line_bytes` (K1 — the §10-open-question-4 shape: "an
# advertised max the host is obliged to respect"), `docs/RPC-PROTOCOL.md`
# renders it from that same value, and the refusal itself carries it in
# `error.data`.
#
# WHY 8 MiB. It is far above anything a correct host sends — 8 MiB of prose
# is on the order of two million tokens, an order of magnitude past any
# context window a request could usefully fill — and still small enough that
# the worst case is bounded and boring: the reader holds at most one
# partial line, so a hostile peer streaming an endless line costs
# `MAX_REQUEST_LINE_BYTES` plus one chunk of RSS and no more, forever. It is
# a round number that can be stated in a document, not a tuning parameter.
#
# Measured in BYTES of the line's own content: the terminating LF is framing
# and does not count, a tolerated trailing CR does (it is inside the line as
# far as this reader is concerned). A line of exactly this many bytes is
# accepted; one byte more is refused.
MAX_REQUEST_LINE_BYTES = 8 * 1024 * 1024

#: How many bytes `_read_stdin` pulls off the pipe per read. Framing is done
#: on the buffer this fills (see `_read_stdin`), so this is a syscall-batching
#: number and nothing else — it bounds neither a line nor the reader's memory
#: (`MAX_REQUEST_LINE_BYTES` does both). 64 KiB matches the stdlib
#: `StreamReader`'s own default buffer granularity.
_READ_CHUNK_BYTES = 64 * 1024


# =============================================================================
# stdout takeover (T2)
# =============================================================================
#
# Any print() from a tool, an extension, or a library shares fd 1 with the
# JSON-RPC protocol stream by default, and a single stray line is enough to
# corrupt it for the client. We claim stdout exclusively for the protocol
# writer for as long as an RPCHandler is running: `sys.stdout` is rebound to
# `sys.stderr` so every *other* writer in the process lands there instead,
# and the real stdout is kept as a private handle that only the protocol
# writer (`RPCHandler._write_line`) uses.
#
# This is process-global state (there is exactly one `sys.stdout`), so it is
# reference-counted rather than a plain boolean: nested/concurrent callers —
# two RPCHandler instances in the same process, or a handler whose run() is
# entered while another hasn't stopped yet — each register a claim, and the
# real stdout is only restored once every claim has been released.

_stdout_takeover_depth = 0
_real_stdout: TextIO | None = None


def _take_over_stdout() -> TextIO:
    """Claim stdout exclusively for RPC protocol output.

    Returns a private handle to the real stdout for the caller's exclusive
    use as a protocol writer. Idempotent and reference-counted — see the
    module note above.
    """
    global _stdout_takeover_depth, _real_stdout
    if _stdout_takeover_depth == 0:
        _real_stdout = sys.stdout
        sys.stdout = sys.stderr
    _stdout_takeover_depth += 1
    assert _real_stdout is not None
    return _real_stdout


def _release_stdout() -> None:
    """Release one claim on stdout, restoring it once every taker has.

    Raises if called without a matching `_take_over_stdout()`. An
    unbalanced release is always a caller bug (a double release in some
    `finally`, or a release from a path that never took over) and must
    not be absorbed silently: at depth > 1 it would release *someone
    else's* still-active claim early, and at depth 0 there is nothing
    to release at all — either way the failure is exactly the kind T2
    exists to prevent (stray output sharing fd 1 with the protocol
    stream again), so it should be loud, not swallowed.
    """
    global _stdout_takeover_depth, _real_stdout
    if _stdout_takeover_depth <= 0:
        raise RuntimeError("_release_stdout() called without a matching _take_over_stdout()")
    _stdout_takeover_depth -= 1
    if _stdout_takeover_depth == 0:
        sys.stdout = _real_stdout
        _real_stdout = None


def is_stdout_taken_over() -> bool:
    """True while at least one RPCHandler holds the stdout claim."""
    return _stdout_takeover_depth > 0


# =============================================================================
# Signal shutdown (P1)
# =============================================================================

_SIGNAL_EXIT_CODES: dict[str, int] = {"SIGTERM": 143, "SIGHUP": 129}


def _register_signal_handlers(self: "RPCHandler") -> None:
    """Install SIGTERM/SIGHUP handlers for this run() call.

    SIGTERM -> exit code 143, writer is cancelled rather than drained
    (the host is already impatient). SIGHUP -> exit code 129, writer is
    drained normally. SIGHUP does not exist on Windows and is skipped
    there.

    Registration failure is handled by kind rather than blanket-
    swallowed (Fail Early). `NotImplementedError` means the event loop
    genuinely doesn't support signal handlers on this platform (e.g.
    Windows' `ProactorEventLoop`) — that's a real, permanent platform
    limitation, not a bug, and EOF-triggered shutdown still works, so
    it's skipped silently. Anything else (e.g. `ValueError` from
    `add_signal_handler` being invoked off the main thread, where P1
    was expected to work) is let through: the alternative is a handler
    that is silently signal-deaf, and the actual consequence of that —
    verified, not assumed — is that the *next* SIGTERM falls through to
    the OS default disposition and hard-kills the process with no
    flush, no stdout restore, and no cleanup at all, which is strictly
    worse than surfacing the registration failure up front.
    """
    loop = asyncio.get_event_loop()
    self._registered_signals = []
    names = ["SIGTERM"]
    if hasattr(signal, "SIGHUP"):
        names.append("SIGHUP")
    for name in names:
        sig = getattr(signal, name)
        try:
            loop.add_signal_handler(sig, self._on_signal, name)
        except NotImplementedError:
            continue
        self._registered_signals.append(sig)


def _unregister_signal_handlers(self: "RPCHandler") -> None:
    """Remove any signal handlers installed by `_register_signal_handlers`."""
    loop = asyncio.get_event_loop()
    for sig in self._registered_signals:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(sig)
    self._registered_signals = []


def _on_signal(self: "RPCHandler", sig_name: str) -> None:
    """Record the shutdown reason and stop the reader.

    Runs as an event-loop callback (registered via
    `loop.add_signal_handler`), not a raw OS-level signal handler, so
    it's safe to touch asyncio state directly here.
    """
    self._exit_signal = sig_name
    self.exit_code = _SIGNAL_EXIT_CODES[sig_name]
    if self._stdin_task is not None and not self._stdin_task.done():
        self._stdin_task.cancel()


async def _read_stdin(self: "RPCHandler") -> None:
    """Read JSON-RPC requests from stdin, one LF-delimited line at a time.

    Reads via an `asyncio.StreamReader` wired directly to the stdin
    pipe with `loop.connect_read_pipe`, rather than blocking
    `stdin.buffer.readline()` in a thread-pool executor. That
    distinction is not cosmetic: a worker thread blocked inside a
    blocking syscall does not respond to `Task.cancel()` at all —
    cancelling the *task* awaiting it returns immediately (the
    cancellation detaches from the underlying `concurrent.futures`
    work rather than stopping it), but the thread itself stays parked
    in `readline()` until the peer actually writes or closes its end.
    Nothing then closes it: `asyncio.run()`'s shutdown afterwards joins
    that thread in `loop.shutdown_default_executor()`, hanging forever
    on any signal (SIGTERM/SIGHUP) whose whole point was an out-of-band
    shutdown while the peer's pipe is still open. A `StreamReader` has
    no such thread; cancelling the read actually stops it.

    Framing is raw-bytes LF-only: a line ends at the 0x0A byte and
    nowhere else, which — because 0x0A never appears as a continuation
    or lead byte of a multi-byte UTF-8 sequence — is always a genuine
    line break, never a mid-character split, and (unlike
    `str.splitlines()`) never splits on U+2028/U+2029, which are legal,
    unescaped characters inside a JSON string value.

    A single trailing `\\r` is tolerated (CRLF-framed peers) and
    stripped; nothing else is. A final line with no trailing newline at
    EOF is a request like any other and is dispatched, not dropped.

    **Why the split is done here rather than by `StreamReader.readline()`
    (T7, Tier B review finding 9).** `readline()` enforces the reader's
    `_limit` by RAISING `ValueError` on a longer line, and — worse for a
    protocol — its recovery is ambiguous from the outside: depending on
    whether the LF happened to already be in the buffer when the limit
    was hit, the raising call has EITHER consumed through that LF or not,
    and the caller cannot tell which from the exception. A resync built
    on it therefore either drops the next legitimate request or replays
    the tail of the rejected one. Filling a buffer with `reader.read()`
    and finding the LF here costs a few lines and makes the bound exact:
    a line is refused at `MAX_REQUEST_LINE_BYTES` + 1 bytes, once, and
    the discard runs to that line's own LF and stops. See that constant
    for the bound, the two halves of the fix, and where it is published.

    The refusal path holds no bytes: an over-long line is dropped as it
    arrives rather than assembled and then rejected, so a peer streaming
    an endless line with no LF at all is refused ONCE and then costs a
    flat `MAX_REQUEST_LINE_BYTES` + one chunk of memory no matter how
    long it keeps going. It cannot make progress either way — no further
    request can arrive until it sends an LF — but it cannot grow τ, wedge
    the reader against EOF, or defeat SIGTERM/SIGHUP (P1) while trying.
    """
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    # The bytes of the line currently being assembled, never more than one
    # line's worth (T7): complete lines are dispatched and removed below, and
    # an over-long one is refused and cleared rather than kept.
    pending = bytearray()
    # T7: True while the remains of an already-refused over-long line are
    # still arriving. Everything up to and including that line's LF is
    # discarded, and exactly one error was already sent for it.
    discarding = False
    try:
        while True:
            # Finding 4 (phase-4 review): race the next line against
            # `self._shutdown_signal` (set by `_observe_shutdown_after_
            # background_task`, handler.py) rather than a bare `await
            # reader.readline()`. Without this, an extension that requests
            # shutdown from a hook running INSIDE a background turn (C3) —
            # the common case, since `submit`/`prompt` return their
            # acceptance response at admission and let the turn run on —
            # sets `AgentSession.shutdown_requested` while this coroutine is
            # already parked here waiting for a next line that may never
            # come, and nothing was ever checking the flag again. Not a
            # poll: `self._shutdown_signal` is only ever set once, by that
            # one done-callback, and this `asyncio.wait` is the only place
            # that ever awaits it — it does not re-check anything on a
            # timer. The existing after-dispatch check just below this loop
            # body is UNCHANGED and still the primary path for a shutdown
            # requested synchronously inside a dispatched command's own
            # handling.
            read_task = asyncio.ensure_future(reader.read(_READ_CHUNK_BYTES))
            shutdown_task = asyncio.ensure_future(self._shutdown_signal.wait())
            done, _pending = await asyncio.wait(
                {read_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if read_task not in done:
                # The shutdown signal fired while genuinely idle here (no
                # line in flight to finish parsing/dispatching) -- take the
                # exact same clean-shutdown path as EOF/the after-dispatch
                # check below. The abandoned read() is cancelled rather
                # than left to resolve later against a `transport` this
                # method's own `finally` is about to close.
                read_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await read_task
                break
            if not shutdown_task.done():
                shutdown_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await shutdown_task
            chunk = read_task.result()
            eof = not chunk
            if eof:
                # EOF: a clean shutdown trigger (P4), handled by run(). What
                # is still in `pending` is a final line the peer never
                # terminated — a request like any other (T1), dispatched by
                # giving it the LF the peer did not send.
                #
                # A stream that ends mid-discard needs no case of its own,
                # and deliberately does not get one: `pending` is cleared
                # when a line is refused and nothing is appended to it while
                # `discarding` (the branch below drops those bytes outright),
                # so `discarding` here implies `not pending` and the refused
                # line cannot be resurrected by an EOF. Stated rather than
                # re-tested, because a condition that can never be false is a
                # branch no test can fail on.
                if not pending:
                    break
                pending += b"\n"
            elif discarding:
                offset = chunk.find(b"\n")
                if offset < 0:
                    # Still inside the refused line. Drop the bytes; the
                    # error for them was sent when the bound was crossed.
                    continue
                discarding = False
                chunk = chunk[offset + 1 :]
                pending += chunk
            else:
                pending += chunk

            stop = False
            while True:
                offset = pending.find(b"\n")
                if offset < 0:
                    # An incomplete line. T7: refuse it the moment it crosses
                    # the bound rather than when it finally ends — a peer that
                    # never sends an LF would otherwise be an unbounded
                    # buffer, and telling the host NOW is what lets it stop.
                    if len(pending) > MAX_REQUEST_LINE_BYTES:
                        await _refuse_oversized_request(self, len(pending), line_complete=False)
                        pending.clear()
                        discarding = True
                    break
                if offset > MAX_REQUEST_LINE_BYTES:
                    # A complete line, whole in the buffer, over the bound.
                    # Refused unparsed and dropped through its own LF; the
                    # loop continues on the next line rather than resyncing,
                    # because there is nothing left of this one to resync on.
                    await _refuse_oversized_request(self, offset, line_complete=True)
                    del pending[: offset + 1]
                    continue
                raw = bytes(pending[:offset])
                # The buffer advances BEFORE the decode, unconditionally: an
                # undecodable line is consumed exactly like a decodable one,
                # which is what makes the refusal below a resync rather than a
                # loop on the same bytes.
                del pending[: offset + 1]
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    # T7's sibling defect (Tier B review, finding 9's blockers
                    # #4): two hostile bytes on stdin used to kill the child
                    # here — `UnicodeDecodeError` escaped `_read_stdin`, out of
                    # `handler.run()`, and every request behind them was lost
                    # with the process. Same availability failure as an
                    # over-long line, same answer: refuse the one line, keep
                    # serving the connection.
                    await _refuse_undecodable_request(self, exc, len(raw))
                    continue
                if line.endswith("\r"):
                    line = line[:-1]
                if not line.strip():
                    # A blank or whitespace-only line is framing, not a request:
                    # skip it rather than answering with an "Invalid JSON" error.
                    # Emptiness is tested on the STRIPPED form, but the original
                    # line is what gets parsed — the reader must not strip the
                    # content itself (T1: no splitlines, no eating a U+2028 or a
                    # meaningful character the framing does not own).
                    continue
                await self._handle_line(line)
                # P3 (docs/REMOTE-CONTROL.md §4[7]): "extensions can request
                # shutdown, checked after each command rather than polled" —
                # this IS that check, placed exactly where the spec's wording
                # says: right after a command's own dispatch, not on a separate
                # timer/poll loop, and never for a skipped blank line (that is
                # not a command). `AgentSession.shutdown_requested` reads the one
                # `ExtensionContext` every bound extension shares, so any
                # extension's `ctx.shutdown()` — called from a hook or a tool
                # this command's dispatch just ran — is visible here immediately.
                # Breaking out of this loop takes the exact same clean-shutdown
                # path `run()` already gives stdin EOF (drain the writer, remove
                # signal handlers, release stdout) — no new exit code, no signal.
                # It also abandons any FURTHER lines already sitting in
                # `pending`, which is the same promise as before: once shutdown
                # is requested the next line is never even parsed.
                if self._session.shutdown_requested:
                    stop = True
                    break
            if stop or eof:
                break
    finally:
        transport.close()


async def _refuse_oversized_request(
    self: "RPCHandler", observed_bytes: int, *, line_complete: bool
) -> None:
    """Answer one over-long request line with an error instead of dying (T7).

    Unlike its neighbours this one is NOT composed onto `RPCHandler` in
    handler.py: it is an internal helper of `_read_stdin`, which calls it
    directly as a plain function, and nothing outside this module has any
    business reaching it. The first parameter keeps the file's `self`
    naming anyway, so the handler-taking convention reads the same
    throughout.

    Called by `_read_stdin` at most once per offending line — see
    `MAX_REQUEST_LINE_BYTES` for the bound and why it is both raised and
    enforced, and `dialect.REQUEST_TOO_LARGE` for why that code and not
    `PARSE_ERROR`/`INVALID_REQUEST`.

    `id` is `null` because it is genuinely unknowable: the id lives inside
    bytes this reader deliberately never parsed. That is a real cost of
    refusing at the framing layer, and the honest one — the alternative,
    parsing an over-long line far enough to recover its id, is doing the
    work the bound exists to avoid. A host correlates by the obvious
    means instead: it just sent something enormous, and `data` tells it
    exactly how enormous and what the ceiling is.

    `line_complete` distinguishes the two ways the bound is crossed and is
    reported as such rather than smoothed over: `True` means the whole line
    was already in the buffer, so `observed_bytes` is its real length;
    `False` means it was refused mid-flight, so `observed_bytes` is a lower
    bound — the true length is unknown and stays unknown, because the rest
    of that line is discarded as it arrives rather than counted.

    Announced on stderr as well (T4's documented log channel, the same
    channel `_flush_stdout_with_deadline` uses to report giving up): an
    operator watching a host that silently stopped working should not have
    to hold the JSON-RPC stream to find out why.
    """
    seen = "" if line_complete else "at least "
    message = (
        f"Request line refused: {seen}{observed_bytes} bytes exceeds the "
        f"{MAX_REQUEST_LINE_BYTES}-byte limit on a single request line. The line was "
        "discarded through its next LF; this connection is otherwise unaffected and "
        "the next well-formed line is served normally. The limit is published as "
        "limits.max_request_line_bytes by get_capabilities."
    )
    print(f"tau rpc: {message}", file=sys.stderr, flush=True)
    await self._send_error(
        None,
        dialect.REQUEST_TOO_LARGE,
        message,
        data={
            "max_request_line_bytes": MAX_REQUEST_LINE_BYTES,
            "observed_bytes": observed_bytes,
            "line_complete": line_complete,
        },
    )


async def _refuse_undecodable_request(
    self: "RPCHandler", exc: UnicodeDecodeError, observed_bytes: int
) -> None:
    """Answer one request line that is not UTF-8 with an error instead of
    dying (T7's sibling; Tier B review finding 9, blockers #4).

    Verified before the fix, against a real `tau --mode rpc` child::

        printf '\\xff\\xfe\\n{"jsonrpc":"2.0","id":1,"method":"get_state"}\\n' | tau --mode rpc
        → UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in
          position 0: invalid start byte
        → rc 1, stdout empty, no JSON-RPC error, no stderr report, and the
          well-formed `get_state` behind it never answered.

    Two bytes, and the connection was gone. That is the same availability
    defect T7 fixed one framing rule over, so it gets the same answer: the
    offending LINE is refused and consumed through its own LF, and the next
    line is served normally.

    **`PARSE_ERROR`, not a code of its own** — the opposite call to T7's, for
    the reason stated in `dialect.REQUEST_TOO_LARGE`'s own comment. That code
    exists because an over-long line is never handed to a parser at all, so
    nothing may claim its bytes were or were not valid JSON. Here the claim
    IS available and is exactly PARSE_ERROR's: JSON-RPC 2.0 transmits JSON
    text, and JSON text is UTF-8 (RFC 8259 §8.1), so bytes that are not UTF-8
    are not JSON — "an error occurred on the server while parsing the JSON
    text", which is what -32700 says. Inventing a second code for a case the
    standard one already describes would be a new thing for a host to learn
    in exchange for nothing.

    `id` is `null` for the same honest reason it is null on a T7 refusal: the
    id is inside the bytes that could not be read. A lossy re-decode to fish
    it out would be guessing at a correlation the host can make itself —
    JSON-RPC 2.0 §5 says `id` MUST be null when it cannot be determined, and
    "cannot be determined" is precisely the situation.

    `data` names WHERE, because the byte offset is the one thing that turns
    "your input is corrupt" into a place to look: a host that framed a binary
    blob into its request, or wrote latin-1, learns which byte broke it
    without re-sending anything. The offending bytes themselves are NOT
    echoed — they are by definition not encodable into this JSON response.

    Announced on stderr too (T4), for the same reason the T7 refusal is: an
    operator watching a host that stopped working should not have to hold the
    JSON-RPC stream to find out why.
    """
    message = (
        f"Request line refused: {observed_bytes} bytes that are not valid UTF-8 "
        f"({exc.reason} at byte {exc.start}). JSON-RPC 2.0 carries JSON text, and "
        "JSON text is UTF-8; the line was discarded unparsed. This connection is "
        "otherwise unaffected and the next well-formed line is served normally."
    )
    print(f"tau rpc: {message}", file=sys.stderr, flush=True)
    await self._send_error(
        None,
        dialect.PARSE_ERROR,
        message,
        data={
            "encoding": "utf-8",
            "reason": exc.reason,
            "byte_offset": exc.start,
            "offending_bytes": observed_bytes,
        },
    )


class _WriterClosedProtocol(asyncio.streams.FlowControlMixin):
    """`FlowControlMixin` plus a genuine close-waiter.

    Bare `FlowControlMixin` (the protocol `asyncio`'s own docs use for
    exactly this "wrap a StreamWriter around connect_write_pipe" pattern)
    does not implement `_get_close_waiter`, so `StreamWriter.wait_closed()`
    raises `NotImplementedError` on it — there is no way to learn when the
    transport has ACTUALLY finished closing the fd it owns, only that
    `close()` was requested. `close()` schedules that teardown on the event
    loop rather than performing it inline; without a real way to await it,
    whether the fd is closed by the time the caller's next line runs is a
    race, not a guarantee — measured directly (phase-4 review) as a flaky
    hang in `_write_stdout`'s own tests, where a caller reading the pipe to
    EOF immediately after `_write_stdout()` returned sometimes found the
    duplicated write fd (`_connect_stdout_writer`) still open. Mirrors
    `asyncio.StreamReaderProtocol`'s own `_get_close_waiter`, the stdlib's
    solution to the identical problem on the read side.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(loop=loop)
        self._closed_waiter: asyncio.Future[None] = loop.create_future()

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        if not self._closed_waiter.done():
            if exc is None:
                self._closed_waiter.set_result(None)
            else:
                self._closed_waiter.set_exception(exc)

    def _get_close_waiter(self, stream: asyncio.StreamWriter) -> "asyncio.Future[None]":
        return self._closed_waiter


async def _connect_stdout_writer(
    self: "RPCHandler", loop: asyncio.AbstractEventLoop
) -> tuple[asyncio.StreamWriter, int]:
    """Wire an `asyncio.StreamWriter` directly to the real stdout pipe.

    The writer-side mirror of `_read_stdin`'s `loop.connect_read_pipe` —
    see that method's docstring for the underlying hazard (a worker thread
    blocked in a blocking syscall does not respond to `Task.cancel()` at
    all) and `_write_stdout`'s docstring for why the writer needed the
    identical fix (T6 blocker 2, phase-4 review: it is VERBATIM the same
    hazard, left unfixed on the write side while the reader was already
    corrected).

    Uses the private handle stdout takeover (T2) captured
    (`self._real_stdout`), never `sys.stdout` directly — by the time this
    runs, `sys.stdout` has been rebound to stderr for every other writer in
    the process. `.buffer` is used when present (the normal case: a real
    `sys.stdout`, or any text-mode file object — both expose the
    underlying binary stream `connect_write_pipe` needs, exactly as
    `_read_stdin` uses `sys.stdin.buffer`); the handle itself is used
    directly otherwise, for a caller that already supplies a binary-mode
    file object.

    Connects to a DUPLICATED file descriptor (`os.dup`), not the original
    one: `connect_write_pipe`'s transport closes the fd it was given as
    part of its own teardown (`_write_stdout`'s `finally`), and closing
    the ORIGINAL would leave `self._real_stdout` — which `transport
    ._release_stdout()` hands back to `sys.stdout` right after this
    writer's task ends — pointing at an fd that is no longer valid. The
    duplicate is a distinct kernel fd number backed by the SAME open file
    description (writes through either are equivalent; POSIX `dup(2)`),
    so closing it affects only this writer's own transport.

    Also returns the ORIGINAL fd number so `_write_stdout` can restore its
    blocking mode once the writer is done (see that method's `finally`):
    `connect_write_pipe`'s transport calls `os.set_blocking(fd, False)` on
    the DUPLICATE, but `O_NONBLOCK` is a property of the underlying OPEN
    FILE DESCRIPTION, not the fd number — POSIX `dup(2)` makes the
    duplicate SHARE it with the original. Left unrestored, `self
    ._real_stdout` (handed back to `sys.stdout` on release) silently
    becomes non-blocking too: measured directly (phase-4 review) as a
    livelock, not a hang — `io.TextIOWrapper.close()`'s internal flush,
    hitting `EAGAIN` against a peer that never drained a backed-up pipe,
    retries in a tight loop with no backoff rather than blocking as a
    caller of a normally-blocking file object would expect.

    Raises if called before stdout has been taken over (a missing
    exclusive-stdout handle is exactly the invariant T2 exists to
    enforce — the same failure `_write_line` used to raise directly,
    before the writer swap moved the check here, one layer up).
    """
    if self._real_stdout is None:
        raise RuntimeError(
            "RPCHandler._write_stdout() called without stdout takeover — "
            "run() must call _take_over_stdout() (or a test must set "
            "self._real_stdout) before the writer runs."
        )
    pipe = getattr(self._real_stdout, "buffer", self._real_stdout)
    original_fd = pipe.fileno()
    dup_pipe = os.fdopen(os.dup(original_fd), "wb", buffering=0)
    stream_transport, protocol = await loop.connect_write_pipe(
        lambda: _WriterClosedProtocol(loop), dup_pipe
    )
    return asyncio.StreamWriter(stream_transport, protocol, None, loop), original_fd


async def _write_stdout(self: "RPCHandler") -> None:
    """Write queued responses/events to stdout, one at a time, in order.

    FIFO ordering is the RPC protocol's only ordering guarantee to the
    client (T6), so every write is fully drained — not merely handed to a
    buffer — before the next queued item is even dequeued. Historically
    this was enforced by awaiting `loop.run_in_executor(None, self
    ._write_line, line)` to completion before looping; see
    `_connect_stdout_writer` for why the MECHANISM changed (T6 blocker 2,
    phase-4 review) — the reasoning this paragraph states is unchanged:
    without something serializing writes end to end, N writes racing to
    completion in whatever order the OS happens to service them is exactly
    what a 20-response queue once measured emerging `1,3,5,...,0,15,...`.

    T3 consequence, stated deliberately as a natural effect of the writer
    swap rather than a redesign of T3 itself: `await writer.drain()` below
    is now REAL drain coupling to the OS's own acceptance of the bytes
    (pi's own `waitForRawStdoutBackpressure()`, docs/REMOTE-CONTROL.md
    §4[1]), so an item's event-credit (`_credited`) is released only AFTER
    that drain succeeds — strictly stronger than the previous dequeue-time
    release, which freed a credit the instant an item left `_output_queue`
    regardless of whether the OS had actually accepted anything yet.

    Deliberately does NOT catch write failures (T5/T6). A failed drain
    means the peer's end of the pipe is gone — there is nothing useful to
    do but stop, and swallowing the exception would leave us silently
    producing output nobody can read. Letting it propagate ends this task;
    `run()`'s `await self._stdout_task` re-raises it there.
    """
    loop = asyncio.get_event_loop()
    writer, original_fd = await self._connect_stdout_writer(loop)
    try:
        while self._running or not self._output_queue.empty():
            try:
                item = await asyncio.wait_for(self._output_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                # Finding 3 (Tier B review), second half. The queue clause is
                # load-bearing, not belt-and-braces: without it this `break`
                # CONTRADICTS the `while` condition one line above, which
                # keeps writing precisely while "`_running`, OR the queue is
                # non-empty". `asyncio.wait_for` raising here proves only
                # that the queue was empty for the last 0.5s — it says
                # nothing about right now, and `put_nowait` landing in the
                # same event-loop iteration as the timer loses the race:
                # the getter it woke is cancelled, the item stays queued,
                # and this handler runs with items waiting.
                #
                # Reachable, and measured, once `run()` reaps background
                # tasks before draining the writer (handler.py): a task
                # finishing 0.5s into that reap queued its last two items,
                # `run()` cleared `_running`, and this `break` threw both
                # away — the child exited rc 0 with an empty stderr and the
                # host was never told what had happened. Deciding to stop on
                # `_running` ALONE is what made a queued item droppable; the
                # loop condition was right all along. (Stated without naming
                # what those items MEANT: X1 — block [1] frames and orders
                # bytes, and test_rpc.py greps this file for leaked event
                # vocabulary to keep it that way.)
                if not self._running and self._output_queue.empty():
                    break
                continue
            # T3/G4: this item's event-credit (if it holds one — see
            # `_forward_event`/`_acquire_event_credit`), released once the
            # write below has actually drained (see the docstring above for
            # why post-drain, not dequeue-time). A response/ack item (C3's
            # `_on_admitted`, `_send_response`, `_send_error`) never had one
            # (`_credited` absent, `pop` defaults to False) and this is a
            # no-op for it.
            credited = item.pop("_credited", False)
            # Hand the item back to the layer that knows what it MEANS before framing
            # it (X1: block [1] frames and orders bytes; it does not read event types).
            self.prepare_outbound(item)
            line = json.dumps(item, separators=(",", ":"))
            self._write_line(writer, line)
            # AWAIT is load-bearing — see the class docstring above (T6/T3).
            #
            # Deliberately uncaught. stdout is not logging here, it is the protocol's
            # only outbound channel: if the write fails the peer is gone, and every
            # response after it would be queued into a void. A `except Exception:
            # pass` here made a broken pipe indistinguishable from a delivered
            # message. Let it raise and take run() down with it.
            await writer.drain()
            # The peer accepted this item's bytes. Only observed by `run()`'s
            # post-EOF flush deadline (handler.py,
            # `_SHUTDOWN_FLUSH_NO_PROGRESS_TIMEOUT_S`) to distinguish "the
            # host is reading slowly" from "the host stopped reading".
            self._drain_progress += 1
            if credited:
                self._event_credits.release()
    finally:
        # Teardown of THIS writer's own transport/fd (see
        # `_connect_stdout_writer`'s docstring for why it is a duplicate,
        # not `self._real_stdout`'s own fd). AWAITED via `wait_closed()`
        # (`_WriterClosedProtocol`), not fire-and-forget: `close()` only
        # SCHEDULES the fd's closure on the event loop, and without
        # waiting for it a caller that reads the peer pipe to EOF
        # immediately after this coroutine returns can race that closure
        # and hang — measured directly (phase-4 review) as a flaky test
        # failure once the writer swap landed.
        unwinding = sys.exc_info()[0]
        if unwinding is asyncio.CancelledError:
            # This coroutine is being stopped DELIBERATELY, by one of the two
            # paths that cancel the writer: SIGTERM (P1) and the post-EOF
            # flush deadline (`RPCHandler._flush_stdout_with_deadline`). Both
            # fire for the same underlying reason — the peer is not taking
            # bytes — so `close()` alone is not enough here: it waits for the
            # transport's buffer to reach the OS before it completes, which
            # is precisely the wait that cannot finish, and `wait_closed()`
            # below would then park for its whole timeout and hand the
            # unkillable process straight back. Those bytes are already lost;
            # `abort()` discards them and tears the transport down now.
            # Deliberately NOT done on the broken-pipe path (T5/T6): there
            # `connection_lost` has already run, the transport has dropped
            # its loop reference, and calling `abort()` on it raises an
            # asyncio-internal `AttributeError` that would replace the real
            # failure — measured, as two broken-pipe tests failing.
            writer.transport.abort()
        writer.close()
        if unwinding is None:
            # Nothing is already propagating out of this coroutine — a
            # close failure HERE is new information, and Fail Early says
            # surface it (bounded, not silent) rather than swallow it.
            await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
        else:
            # An exception is already unwinding this coroutine — the SAME
            # connection failure `drain()` raised above (T5/T6), or the
            # `CancelledError` handled just above. Give the transport a
            # moment to finish tearing down so the fd is genuinely gone
            # before returning, but do not let a redundant close-time echo
            # of the failure already in flight REPLACE it, and do not hang
            # on it either.
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
        # See `_connect_stdout_writer`'s docstring: the duplicate fd's
        # transport put the SHARED open file description into non-blocking
        # mode. Restore blocking mode on the ORIGINAL fd now that the
        # duplicate is gone, so `self._real_stdout` — handed back to
        # `sys.stdout` by `transport._release_stdout()` right after this
        # method returns — behaves exactly as a caller of a normal,
        # blocking file object expects. `contextlib.suppress(OSError)`:
        # the original fd may already be invalid (e.g. a genuinely broken
        # pipe, T5/T6) — that failure was already handled above, and an fd
        # that is gone cannot leak a blocking-mode footgun either.
        with contextlib.suppress(OSError):
            os.set_blocking(original_fd, True)


def _write_line(self: "RPCHandler", writer: asyncio.StreamWriter, data: str) -> None:
    """Queue one line on the stdout pipe transport.

    Non-blocking: `StreamWriter.write()` only appends to the transport's
    own internal buffer — the actual OS-level completion, and the point a
    write failure surfaces, is `await writer.drain()` in `_write_stdout`,
    immediately after this call.

    Kept as its own method, same seam it always was: several tests inject
    a failure here (`test_write_failure_propagates_and_is_not_swallowed`
    et al.) to drive T5/T6 without needing a real broken pipe.

    Args:
        writer: The stdout `StreamWriter` `_write_stdout` connected via
            `_connect_stdout_writer`.
        data: The JSON string to write.
    """
    writer.write(data.encode("utf-8") + b"\n")
