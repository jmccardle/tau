"""R-T1 — the protocol conformance suite, driving a REAL ``tau --mode rpc``
subprocess over real pipes.

Reference: docs/REMOTE-CONTROL.md §9 R-T1, §2 G1 ("the wire is the product...
a second implementation should be possible from this document plus the
generated reference"), §4[1] T1/T6, §4[3] C2/C3, §4[4] E5, §4[8] K2.

This is deliberately NOT `test_rpc.py`/`test_rpc_transport.py`: those import
``RPCHandler`` and call its methods directly, which pins the Python API, not
the wire. Every test here spawns ``python -m tau_coding_agent.cli --mode
rpc`` (the same module ``main()`` — see ``examples/ext_kit/spawn.py`` for the
precedent of driving τ this way rather than depending on the installed
``tau`` console script being on PATH) and talks to it exclusively through its
stdin/stdout pipes, one LF-delimited JSON object at a time — precisely what a
non-Python host would do.

**No live LLM, no network.** A real turn (the dual-completion / cursor tests)
needs a model to answer, so this file stands up a real fake OpenAI-compatible
HTTP server on loopback (stdlib ``http.server``, the exact pattern
``test_delegate.py`` already uses to drive a *different* τ subprocess
honestly) and points the child's ``~/.tau/config.json`` at it via a temp
``$HOME``. ``config.TAU_DIR = Path.home() / ".tau"`` is resolved fresh by the
*child process*, so redirecting its env's ``HOME`` is real isolation, not a
monkeypatch that only fools the parent (MEMORY.md: monkeypatching a
from-import binding in-process is how the TUI suite silently kept hitting a
live server for months — that specific mistake cannot happen here because no
in-process patching is involved at all).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tau_agent_core.rpc import DEFAULT_OUTPUT_QUEUE_EVENT_BOUND, dialect
from tau_agent_core.rpc import commands as rpc_commands
from tau_coding_agent.session_store import Session, rpc_tmp_dirname, session_dir_for_cwd


# ── fake OpenAI-compatible provider (real HTTP server the child talks to) ────


class _State:
    """Mutable per-test knobs the handler reads on every request.

    A module-scoped server is reused across every test in this file (spawning
    a thread per test would work too, but this is cheaper and the tests in
    this module run sequentially, never concurrently with each other).
    """

    def __init__(self) -> None:
        self.delay_s = 0.0
        self.reply_text = "ok"
        # R-T3: when set, overrides `reply_text` with N separate SSE chunks
        # (one `data:` block per string) sent in ONE HTTP write — a peer
        # backpressure test needs many small `message_update` events, not
        # one big reply, to exceed the (item-count) bound (see _sse_body).
        self.chunks: list[str] | None = None
        # Finding 3 (phase-4 review): set the instant the handler thread's
        # OWN `self.wfile.write(body)` call returns — i.e. the OS has
        # accepted every byte of the response body. A blocking socket write
        # against a peer that has stopped reading does not return until the
        # peer resumes, so "still unset after N seconds of the child not
        # reading" is server-side, black-box proof that τ genuinely stopped
        # pulling bytes off this connection — not an inference from counting
        # wire events, which a large-enough single write can satisfy even
        # with an unbounded queue (see the test this backs).
        self.write_complete = threading.Event()
        # Finding 3 (Tier B review): a PARENT-CONTROLLED release, instead of
        # `delay_s`'s wall-clock guess, for the one test that needs a
        # provider call to finish at a specific moment relative to the
        # CHILD's shutdown. When `gate` is not None the handler thread sets
        # `gate_reached` (server-side proof the child's request really did
        # arrive — a test that released a gate nobody was waiting on would
        # otherwise pass while measuring nothing) and then blocks until the
        # parent sets `gate`. `delay_s` cannot express this: the child's
        # teardown starts when the parent closes stdin, and only the parent
        # knows when that happened.
        self.gate: threading.Event | None = None
        self.gate_reached = threading.Event()


def _sse_body(reply_text: str, chunks: list[str] | None = None) -> bytes:
    pieces = chunks if chunks is not None else [reply_text]
    body = "".join(
        'data: {"id":"cmpl-1","choices":[{"index":0,'
        '"delta":{"role":"assistant","content":' + json.dumps(piece) + "},"
        '"finish_reason":null}]}\n\n'
        for piece in pieces
    )
    body += (
        'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
        "data: [DONE]\n\n"
    )
    return body.encode("utf-8")


def _make_fake_openai_handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)  # drain the request body
            if state.gate is not None:
                # Finding 3 (Tier B review): hold this response until the
                # parent says so — see `_State.gate`.
                state.gate_reached.set()
                state.gate.wait(timeout=60)
            if state.delay_s:
                time.sleep(state.delay_s)
            if not self.path.endswith("/chat/completions"):
                self.send_response(404)
                self.end_headers()
                return
            body = _sse_body(state.reply_text, state.chunks)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            state.write_complete.set()

        def log_message(self, *_args: Any) -> None:  # silence per-request stderr logging
            pass

    return _Handler


@pytest.fixture(scope="module")
def fake_state() -> _State:
    return _State()


@pytest.fixture(autouse=True)
def _reset_fake_state(fake_state: _State):
    """Every test starts from the same knobs, regardless of run order."""
    fake_state.delay_s = 0.0
    fake_state.reply_text = "ok"
    fake_state.chunks = None
    fake_state.write_complete.clear()
    fake_state.gate = None
    fake_state.gate_reached.clear()
    yield


@pytest.fixture(scope="module")
def fake_provider_url(fake_state: _State):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_fake_openai_handler(fake_state))
    # Finding 1's repro needs a request the child gives up on well before the
    # fake server would ever respond (see delay_s below) — the CHILD process
    # exits/gets killed long before that sleep ends, which would otherwise
    # leave a non-daemon per-request handler thread alive and block THIS
    # process's own exit at the end of the test session.
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def fake_home(tmp_path: Path, fake_provider_url: str) -> Path:
    """A temp ``$HOME`` whose ``~/.tau/config.json`` names one model, "fake",
    pointing at the loopback fake provider above — no models map lookup
    ambiguity, no ``--model`` flag needed on the child's argv. Isolation is
    by construction: this directory never had a real ``~/.tau`` in it, so
    there is no live JMFTS/config to accidentally reach (the MEMORY.md
    regression this suite is built not to repeat)."""
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    config = {
        "models": {
            "fake": {
                "backend": "openai",
                "model": "fake-model",
                "base_url": fake_provider_url,
                "api_key": "x",
                "tools": [],
            }
        },
        "default_model": "fake",
    }
    (tau_dir / "config.json").write_text(json.dumps(config))
    return tmp_path


@pytest.fixture
def fake_home_two_models(tmp_path: Path, fake_provider_url: str) -> Path:
    """Same shape as ``fake_home``, but with a SECOND config model name,
    "fake-alt" -- needed only by the ``set_model`` persistence test below,
    which must switch to a name that is not already the session's current
    one. Both keys point at the same loopback fake provider: the test never
    sends a turn through "fake-alt", it only switches to it and reads back
    what got persisted, so a second real backend is not needed."""
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    config = {
        "models": {
            "fake": {
                "backend": "openai",
                "model": "fake-model",
                "base_url": fake_provider_url,
                "api_key": "x",
                "tools": [],
            },
            "fake-alt": {
                "backend": "openai",
                "model": "fake-model-alt",
                "base_url": fake_provider_url,
                "api_key": "x",
                "tools": [],
            },
        },
        "default_model": "fake",
    }
    (tau_dir / "config.json").write_text(json.dumps(config))
    return tmp_path


# ── the subprocess itself ────────────────────────────────────────────────────


async def _spawn(
    fake_home: Path, extra_args: list[str] | None = None
) -> asyncio.subprocess.Process:
    """Start ``python -m tau_coding_agent.cli --mode rpc`` with its own
    isolated ``$HOME``. ``-m`` (not the installed ``tau`` console script) so
    the suite does not depend on the venv's bin/ being on PATH — the same
    choice ``examples/ext_kit/spawn.py`` makes for its own child, and it hits
    the identical ``if __name__ == "__main__": sys.exit(main())`` entry the
    console script's generated wrapper calls."""
    env = _child_env(fake_home)
    argv = [sys.executable, "-m", "tau_coding_agent.cli", "--mode", "rpc", *(extra_args or [])]
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


def _child_env(fake_home: Path) -> dict[str, str]:
    """The environment EVERY child in this file is spawned with.

    ``$TMPDIR`` is redirected as well as ``$HOME`` (unit S): ``--mode rpc``
    stores its sessions under ``<tmp>/.tau-<uid>/sessions`` rather than the user's
    ``~/.tau/sessions``, so a child without this leaves a real session in the
    DEVELOPER's own ``/tmp/.tau-<uid>`` on every run — the same pollution in a
    different directory. One helper rather than a dict literal per spawn,
    because that is exactly how the second spawn site in this file missed it.
    """
    child_tmp = fake_home / "tmp"
    child_tmp.mkdir(exist_ok=True)
    return {**os.environ, "HOME": str(fake_home), "TMPDIR": str(child_tmp)}


def rpc_session_base(fake_home: Path) -> Path:
    """Where a child spawned by :func:`_spawn` stores its sessions.

    Unit S: ``--mode rpc``'s DEFAULT base is ``<tmp>/.tau-<uid>/sessions``, NOT
    ``~/.tau/sessions`` — one 0-message session per spawn would otherwise take
    over ``tau -c`` for whoever is working in the same directory. ``_spawn``
    points the child's ``$TMPDIR`` at ``<fake_home>/tmp``, so this is that
    child's real, durable session base; ``test_rpc_session_dir_isolation.py``
    is what proves the user's own list stays clean.
    """
    return fake_home / "tmp" / rpc_tmp_dirname() / "sessions"


async def _send(proc: asyncio.subprocess.Process, obj: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
    await proc.stdin.drain()


async def _send_raw(proc: asyncio.subprocess.Process, raw: bytes) -> None:
    assert proc.stdin is not None
    proc.stdin.write(raw)
    await proc.stdin.drain()


def _stdout_buffer(proc: asyncio.subprocess.Process) -> bytearray:
    """The unconsumed tail of this child's stdout, between `_read_line` calls."""
    buf = getattr(proc, "_tau_stdout_buffer", None)
    if buf is None:
        buf = bytearray()
        proc._tau_stdout_buffer = buf  # type: ignore[attr-defined]
    return buf


async def _read_one_line(proc: asyncio.subprocess.Process) -> bytes:
    buf = _stdout_buffer(proc)
    while True:
        offset = buf.find(b"\n")
        if offset >= 0:
            line = bytes(buf[: offset + 1])
            del buf[: offset + 1]
            return line
        chunk = await proc.stdout.read(64 * 1024)  # type: ignore[union-attr]
        if not chunk:  # EOF: hand back whatever is left, empty included
            line = bytes(buf)
            buf.clear()
            return line
        buf += chunk


async def _read_line(proc: asyncio.subprocess.Process, timeout: float) -> bytes:
    """One LF-terminated line off the child's stdout, with NO length cap.

    ``StreamReader.readline()`` cannot be used for this, and that is a fact
    about the PROTOCOL rather than about this suite: it raises ``ValueError:
    Separator is found, but chunk is longer than limit`` past
    ``asyncio.streams._DEFAULT_LIMIT`` (64 KiB), and ``get_capabilities`` --
    the one verb K2 tells every host to send FIRST -- answers with a document
    larger than that. Measured at this round's integration: 65258 bytes
    before it, 65762 after one added schema description, i.e. τ shipped the
    whole Tier B round 278 bytes from the cliff.

    So a host frames its own lines, exactly as ``transport._read_stdin`` has
    done since T7 and for the mirror-image reason. Deliberately built on
    ``read()`` rather than on a larger ``limit=`` at spawn: the reader's limit
    is also its flow-control high-water mark, and raising it would silently
    let this driver absorb megabytes the backpressure tests need it to refuse
    (R-T3 -- see ``test_backpressure_bounds_the_backlog_and_abort_stays_
    reachable_behind_it``). Chunked reads leave that mark exactly where it
    was: still 64 KiB, still paused when nothing here is reading.

    The timeout bounds the whole line, not each chunk, which is what
    ``wait_for(readline(), timeout)`` used to mean.
    """
    return await asyncio.wait_for(_read_one_line(proc), timeout=timeout)


async def _recv(proc: asyncio.subprocess.Process, timeout: float = 10.0) -> dict[str, Any]:
    assert proc.stdout is not None
    raw = await _read_line(proc, timeout=timeout)
    if not raw:
        # stdout closed early -- surface whatever the child said on stderr
        # (T4's channel) instead of a bare "readline returned nothing".
        assert proc.stderr is not None
        err = (await proc.stderr.read()).decode("utf-8", errors="replace")
        raise AssertionError(f"subprocess stdout closed before a response arrived; stderr:\n{err}")
    return json.loads(raw.decode("utf-8"))


async def _recv_response(
    proc: asyncio.subprocess.Process, expected_id: Any, timeout: float = 10.0
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read lines, skipping NOTIFICATIONS, until the response whose ``id``
    matches ``expected_id`` arrives.

    An in-flight background turn's own ``AgentEvent`` stream can legitimately
    interleave with an unrelated request's response (that interleaving IS
    what proves concurrency, see the dual-completion test below) -- so a
    caller waiting on a specific response must skip past events on the way,
    not assume the very next line is its answer. Returns the response plus
    whatever notifications were skipped getting there, so a caller that also
    wants to observe them (e.g. spot ``agent_end``) does not have to re-read.

    "Notification" is spelled as JSON-RPC itself spells it -- a message with
    no ``id`` -- rather than as ``method == "event"``: ``compact``'s second
    completion is a ``compaction_end`` notification (Blocker 1, Tier B
    review), and a helper that only knew about ``event`` would assert on that
    line as though it were the response it was waiting for.
    """
    skipped: list[dict[str, Any]] = []
    for _ in range(500):
        item = await _recv(proc, timeout=timeout)
        if "id" not in item:
            skipped.append(item)
            continue
        assert item.get("id") == expected_id, f"expected response id={expected_id!r}, got {item}"
        return item, skipped
    raise AssertionError(f"no response with id={expected_id!r} within the line budget")


async def _recv_notification(
    proc: asyncio.subprocess.Process, method: str, timeout: float = 10.0
) -> dict[str, Any]:
    """Read lines until the notification whose ``method`` matches arrives.

    The mirror of ``_recv_response`` for C3's SECOND completion: a host that
    has already collected an acknowledgement waits for the outcome on a
    notification, and everything in between (the turn's own event stream) is
    not an error.
    """
    for _ in range(500):
        item = await _recv(proc, timeout=timeout)
        if item.get("method") == method:
            return item
    raise AssertionError(f"no {method!r} notification within the line budget")


async def _drain_to_eof(proc: asyncio.subprocess.Process) -> None:
    """Keep consuming `proc.stdout` so asyncio's OWN `StreamReader` flow
    control (see the shutdown-matrix section's module note below) never
    pauses the read side and blocks `proc.wait()` from resolving. Discards
    everything -- exit code and timing are what its callers assert on, not
    response content.

    Chunks, not lines: this must not be able to trip over a response longer
    than the stdlib's 64 KiB `readline()` cap (see `_read_line`), which
    `ValueError` was being suppressed into a silent "stopped draining" --
    i.e. into the exact hang this helper exists to prevent."""
    assert proc.stdout is not None
    with contextlib.suppress(asyncio.CancelledError, ValueError):
        while True:
            raw = await proc.stdout.read(64 * 1024)
            if not raw:
                return


async def _shutdown(proc: asyncio.subprocess.Process) -> None:
    """Close stdin (T1/P4: EOF is a clean shutdown trigger) and wait for
    exit; kill as a last resort so a broken test can't wedge the suite.

    Drains `proc.stdout` concurrently with the wait, for the reason the
    shutdown-matrix section's own module note spells out at length: an
    `asyncio.subprocess` `StreamReader` has its OWN 64 KiB high-water mark,
    and once a test has left that much unread the transport stops reading
    the OS pipe — so `proc.wait()` never resolves, no matter how promptly
    the child actually exits. That is a property of this driver, not of τ,
    and every test that ends with a backlog would otherwise time out here
    and blame the child. A test that wants to assert on the non-reading-peer
    shutdown path itself must therefore not route stdout through a
    `StreamReader` at all — see
    `test_stdin_eof_exits_when_the_peer_never_resumes_reading`."""
    if proc.stdin is not None:
        proc.stdin.close()
    drain_task = asyncio.create_task(_drain_to_eof(proc))
    try:
        await asyncio.wait_for(proc.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    finally:
        drain_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain_task


# ── framing, dispatch, ordering (K2, T1, C2, D2, T6) ────────────────────────


async def test_protocol_conformance_capabilities_framing_errors_and_ordering(fake_home):
    """One subprocess, several sequential assertions, to keep this suite's
    wall-clock down (spawning a fresh interpreter per assertion here would
    multiply the dominant cost several-fold for no extra coverage — see the
    module docstring's "one subprocess" tests below for where a fresh spawn
    IS needed, because the assertion is about process-level behaviour)."""
    proc = await _spawn(fake_home)
    try:
        # K2 -- get_capabilities answers correctly as the FIRST thing sent on
        # a fresh connection, no priming/handshake required.
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"})
        caps = await _recv(proc)
        assert caps["id"] == 1
        result = caps["result"]
        assert result["method"] == "get_capabilities"  # D2
        assert result["protocol_version"].split(".")[0].isdigit()
        assert result["dialect"] == "jsonrpc-2.0"
        command_names = {c["name"] for c in result["commands"]}
        assert {"prompt", "submit", "abort", "get_state", "get_messages"} <= command_names
        declined_names = {d["name"] for d in result["declined"]}
        assert "bash" in declined_names  # C1: declined, with a reason, not silently missing

        # T1 -- a single trailing \r is tolerated (CRLF-framed peers).
        await _send_raw(
            proc, (json.dumps({"jsonrpc": "2.0", "id": 2, "method": "get_state"}) + "\r\n").encode()
        )
        cr_resp = await _recv(proc)
        assert cr_resp["id"] == 2
        assert cr_resp["result"]["method"] == "get_state"

        # T1 -- NOT splitlines(): a raw (unescaped) U+2028 LINE SEPARATOR
        # embedded in a string value is legal JSON (RFC 8259 only requires
        # escaping U+0000-U+001F) and must not split this one physical line
        # into two unparseable fragments -- exactly the hazard pi's jsonl.ts
        # refuses Node's `readline` over (docs/REMOTE-CONTROL.md §4[1]).
        # Round-tripped through the request id (a JSON-RPC id may be any
        # JSON value) so a wrong split is unambiguous: it would either time
        # out (the reader choked on half a JSON object) or come back with a
        # mangled id. `ensure_ascii=False` is load-bearing -- the default
        # `json.dumps` would escape U+2028 to a 6-character ASCII sequence,
        # which never puts the actual codepoint on the wire and would test
        # nothing.
        marker_id = "marker end"
        line = (
            json.dumps(
                {"jsonrpc": "2.0", "id": marker_id, "method": "get_state"}, ensure_ascii=False
            )
            + "\n"
        )
        assert " " in line  # guard: the raw codepoint really is on the wire
        await _send_raw(proc, line.encode("utf-8"))
        u2028_resp = await _recv(proc)
        assert u2028_resp["id"] == marker_id
        assert u2028_resp["result"]["method"] == "get_state"

        # C2 -- an unknown method is -32601, never silence, and the id is preserved.
        await _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "no_such_method"})
        unknown = await _recv(proc)
        assert unknown["id"] == 3
        assert unknown["error"]["code"] == -32601
        assert "result" not in unknown

        # C2 -- bad params (a required field missing) is -32602, names the method.
        await _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "submit", "params": {}})
        bad_params = await _recv(proc)
        assert bad_params["id"] == 4
        assert bad_params["error"]["code"] == -32602
        assert bad_params["error"]["data"]["method"] == "submit"

        # T6 -- FIFO is the only ordering guarantee: pipeline N fast,
        # turn-free requests without waiting for a response between sends,
        # and confirm the responses come back in the SAME order they were
        # sent, over the real pipe (the regression transport.py's own
        # comment names: "twenty responses emerged 1,3,5,...,0,15,...").
        ids = list(range(100, 120))
        for i in ids:
            await _send(proc, {"jsonrpc": "2.0", "id": i, "method": "get_state"})
        for expected_id in ids:
            resp = await _recv(proc)
            assert resp["id"] == expected_id, "responses arrived out of FIFO order"
    finally:
        await _shutdown(proc)


# ── prompt's dual completion, concurrency, and E5's cursor (C3, E5, F3) ─────


async def test_prompt_dual_completion_concurrency_and_cursor(fake_home, fake_state):
    """One live turn, with the fake provider gated slow enough to prove the
    server keeps answering OTHER requests while it is in flight -- the
    "concurrent requests" half of ordering, not just pipelined ones.

    Phase-2 review B1: `abort` is called HERE, while the turn is still
    genuinely in flight (`mid_turn["result"]["is_streaming"] is True`, just
    below) -- not after draining for `agent_end`, which is what the old
    version of this test did. Aborting a turn that has already finished is
    degenerate: nothing is in flight, so `abort`'s response and a following
    `get_state` trivially report the same cursor no matter how `abort`
    computes its own. That is exactly the shape that let B1 ship: the
    reviewer's own trace (a 2s-gated turn aborted at 0.5s) showed `abort`'s
    cursor and the cursor after the turn actually finished DISAGREEING, a
    divergence this test cannot see unless it aborts before the turn is
    done.
    """
    fake_state.delay_s = 0.4
    proc = await _spawn(fake_home)
    try:
        # C3 -- prompt's FIRST completion is admission, not the turn's
        # result: no `messages` key, `accepted: true`, arrives fast (well
        # before the gated HTTP call the turn is about to make even starts).
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hi"}})
        accept = await _recv(proc, timeout=5.0)
        assert accept["id"] == 1
        assert accept["result"]["method"] == "prompt"
        assert accept["result"]["accepted"] is True
        assert "messages" not in accept["result"]  # C3: never the turn's messages
        submission_id = accept["result"]["submission_id"]
        assert submission_id

        # Concurrency -- while that turn is still waiting on the (delayed)
        # fake provider, an unrelated get_state must still answer promptly
        # and must observe is_streaming=True: proof the reader/dispatcher is
        # not blocked behind the in-flight turn, AND the precondition for
        # the abort below actually being "genuinely in flight".
        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "get_state"})
        mid_turn, _skipped = await _recv_response(proc, 2, timeout=5.0)
        assert mid_turn["result"]["is_streaming"] is True

        # B1 -- abort THIS in-flight turn. It is a SIGNAL, not a completion:
        # the response must arrive fast (it does not wait for the gated
        # provider call) and must NOT claim a cursor -- at signal time the
        # turn has not unwound or persisted anything yet, so any cursor
        # here would be the stale pre-abort tip E5/F3 rule out.
        await _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "abort"})
        abort_resp, _skipped = await _recv_response(proc, 3, timeout=5.0)
        assert abort_resp["result"]["status"] == "aborted"
        assert "cursor" not in abort_resp["result"]

        # Drain event notifications until the turn's SECOND completion
        # (agent_end -- never on the response itself, C3) shows up. This is
        # the one point the mutation has genuinely happened, and E5/F3 are
        # satisfied HERE: agent_end carries the resulting cursor.
        agent_end = None
        for _ in range(200):
            item = await _recv(proc, timeout=5.0)
            if item.get("method") == "event" and item["params"].get("type") == "agent_end":
                assert item["params"].get("is_error") in (False, None)
                agent_end = item
                break
        assert agent_end is not None, "turn never reached agent_end"
        assert agent_end["params"].get("cursor")

        # E5/F3 -- a separate get_state afterwards must report the SAME
        # cursor agent_end already announced, never a different (later or
        # earlier) tip -- no host may cache "the tip" and no response may
        # invent one either.
        await _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "get_state"})
        state_resp = await _recv(proc, timeout=5.0)
        assert state_resp["result"]["cursor"] == agent_end["params"]["cursor"]

        # E2 -- the turn's text is PULLED via get_messages, never pushed
        # wholesale; confirms the turn actually ran end to end despite the
        # abort request landing mid-flight (the fake provider has no tool
        # calls to interrupt, so the single already-started LLM call still
        # completes -- exactly the reviewer's own trace).
        await _send(proc, {"jsonrpc": "2.0", "id": 5, "method": "get_messages"})
        messages_resp = await _recv(proc, timeout=5.0)
        assert messages_resp["id"] == 5
        blob = json.dumps(messages_resp["result"]["messages"])
        assert fake_state.reply_text in blob
    finally:
        await _shutdown(proc)


# ── P1: signals and exit codes, through the REAL entry point ────────────────


async def test_sigterm_exit_code_143(fake_home):
    """P1, verified through the actual `tau`/`python -m ...cli` process --
    not by constructing an RPCHandler and calling its signal callback
    directly (that is test_rpc_transport.py's job, and it already covers
    the mechanism)."""
    proc = await _spawn(fake_home)
    try:
        # Wait for the server to be genuinely up and reading stdin before
        # signalling it, so this isn't racing process startup.
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"})
        await _recv(proc, timeout=10.0)

        proc.send_signal(signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), timeout=10.0)
        assert proc.returncode == 143
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_sighup_exit_code_129(fake_home):
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"})
        await _recv(proc, timeout=10.0)

        proc.send_signal(signal.SIGHUP)
        await asyncio.wait_for(proc.wait(), timeout=10.0)
        assert proc.returncode == 129
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_stdin_eof_is_a_clean_shutdown(fake_home):
    """P4/T1: closing stdin (no signal at all) exits 0, not some signal code."""
    proc = await _spawn(fake_home)
    await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"})
    await _recv(proc, timeout=10.0)

    assert proc.stdin is not None
    proc.stdin.close()
    await asyncio.wait_for(proc.wait(), timeout=10.0)
    assert proc.returncode == 0


# ── T2: stdout hygiene under a mode flag that requires it ───────────────────


async def test_every_stdout_line_is_valid_json(fake_home, fake_state):
    """T2's observable contract: whatever else is true, nothing on stdout is
    ever a non-JSON line -- the property a stray print() would violate. This
    does not load an extension (out of scope here: extension discovery finds
    nothing under the isolated $HOME anyway), so it is a floor-level check
    that the server's own startup path is clean; R-T5 (a tool/extension
    print() specifically) is a separate obligation this unit does not claim."""
    fake_state.delay_s = 0.1
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"})
        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": {"text": "hi"}})
        seen_agent_end = False
        for _ in range(200):
            item = await _recv(proc, timeout=5.0)  # raises if a line fails json.loads
            assert isinstance(item, dict) and item.get("jsonrpc") == "2.0"
            if item.get("method") == "event" and item["params"].get("type") == "agent_end":
                seen_agent_end = True
                break
        assert seen_agent_end
    finally:
        await _shutdown(proc)


# ── session lifecycle (H1, phase 3): new_session / fork / switch_session ────
#
# rpc_mode.py now builds a real SessionCatalog (defaulting to the file store
# under the isolated $HOME these tests already sandbox — see fake_home) and
# an AgentSessionRuntime over it, so these three verbs are exercised as a
# real client would see them: over the wire, against a real subprocess.


async def test_new_session_resets_state_and_returns_the_addressable_tuple(fake_home):
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hi"}})
        accept, _ = await _recv_response(proc, 1)
        assert accept["result"]["accepted"] is True
        # Wait for the turn to actually finish -- otherwise this test itself
        # would be racing the exact in-flight-turn hazard H4 exists for.
        for _ in range(200):
            item = await _recv(proc)
            if item.get("method") == "event" and item["params"].get("type") == "agent_end":
                break
        else:
            raise AssertionError("agent_end never arrived")

        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "get_state"})
        state_before, _ = await _recv_response(proc, 2)
        assert state_before["result"]["message_count"] > 0

        await _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "new_session"})
        resp, _ = await _recv_response(proc, 3)
        result = resp["result"]
        assert result["method"] == "new_session"  # D2
        assert result["cancelled"] is False
        # F2: the addressable tuple. cursor is NOT None here — a fresh
        # session still carries its own model_change provenance entry
        # (Session._init_state), so "empty" means "no message entries", not
        # "no entries at all".
        assert result["session"]["store"] == "file"
        assert result["session"]["lane"] == "primary"
        assert isinstance(result["session"]["cursor"], str)
        # E5: the resulting cursor, also at top level, matches.
        assert result["cursor"] == result["session"]["cursor"]

        await _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "get_messages"})
        after, _ = await _recv_response(proc, 4)
        assert after["result"]["messages"] == []
    finally:
        await _shutdown(proc)


async def test_fork_creates_a_second_addressable_session(fake_home):
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "new_session"})
        first, _ = await _recv_response(proc, 1)
        first_id = first["result"]["session"]["session_id"]

        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "fork"})
        forked, _ = await _recv_response(proc, 2)
        assert forked["result"]["method"] == "fork"
        assert forked["result"]["cancelled"] is False
        assert forked["result"]["session"]["session_id"] != first_id
    finally:
        await _shutdown(proc)


async def test_switch_session_with_an_unknown_id_is_invalid_params_and_touches_nothing(
    fake_home,
):
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "get_state"})
        before, _ = await _recv_response(proc, 1)

        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "switch_session",
                "params": {"session_id": "no-such-session"},
            },
        )
        resp = await _recv(proc)
        assert resp["id"] == 2
        assert resp["error"]["code"] == dialect.INVALID_PARAMS

        # Fail-Early: the current session is untouched by a switch that failed.
        await _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "get_state"})
        after, _ = await _recv_response(proc, 3)
        assert after["result"]["session_id"] == before["result"]["session_id"]
    finally:
        await _shutdown(proc)


async def test_switch_session_loads_a_forked_session_by_id(fake_home):
    """Forks twice and switches back to the FIRST fork, not the second.

    ``fork`` always persists (``SessionCatalog.fork`` -> ``Session.fork``, a
    real file), which is what makes a fork addressable by id. Since Blocker 2
    ``new_session`` persists by default too, so it could also back this
    fixture now — that is
    ``test_new_session_persists_by_default_and_is_addressable``'s job below,
    and keeping THIS test on ``fork`` keeps the two failure modes separable
    (a regression in one does not redden the other)."""
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "fork"})
        branch_a, _ = await _recv_response(proc, 1)
        branch_a_id = branch_a["result"]["session"]["session_id"]

        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "fork"})
        branch_b, _ = await _recv_response(proc, 2)
        branch_b_id = branch_b["result"]["session"]["session_id"]
        assert branch_b_id != branch_a_id

        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "switch_session",
                "params": {"session_id": branch_a_id},
            },
        )
        switched, _ = await _recv_response(proc, 3)
        assert switched["result"]["method"] == "switch_session"
        assert switched["result"]["cancelled"] is False
        assert switched["result"]["session"]["session_id"] == branch_a_id
    finally:
        await _shutdown(proc)


# ── Finding 1 (phase-3 review): a swap verb must not wedge the channel ──────


async def test_new_session_does_not_wedge_the_channel_when_the_turn_wont_stop(
    fake_home, fake_state
):
    """The reviewer's own wire reproduction: a provider that accepts the
    connection and never sends a line (modeled here with a delay far longer
    than the runtime's swap timeout — the child process never waits that
    long out) leaves a turn genuinely in flight. `new_session` landing on it
    used to await `AgentSession.turn_lock` with no bound at all, and because
    `transport._read_stdin` awaits every request to completion before even
    PARSING the next line, that wedged every later request behind it —
    including `abort`, the host's only other recourse short of killing the
    process:

        prompt ack        : True
        new_session (id=3): NO RESPONSE
        get_state   (id=4): NO RESPONSE
        abort       (id=5): NO RESPONSE
        >>> WEDGED

    All three must now answer: `new_session` refuses with
    `TURN_STILL_RUNNING` instead of hanging, and — the actual point of this
    test — `get_state`/`abort` are not blocked behind it.
    """
    fake_state.delay_s = 20.0  # far longer than AgentSessionRuntime's 5s swap timeout
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hi"}})
        accept, _ = await _recv_response(proc, 1, timeout=5.0)
        assert accept["result"]["accepted"] is True

        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "get_state"})
        mid_turn, _ = await _recv_response(proc, 2, timeout=5.0)
        assert mid_turn["result"]["is_streaming"] is True

        # Pipeline all three -- exactly the reviewer's script. A wedged
        # reader would never even PARSE ids 4/5, let alone answer them.
        await _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "new_session"})
        await _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "get_state"})
        await _send(proc, {"jsonrpc": "2.0", "id": 5, "method": "abort"})

        responses: dict[Any, dict[str, Any]] = {}
        for _ in range(200):
            item = await _recv(proc, timeout=8.0)
            if item.get("method") != "event" and "id" in item:
                responses[item["id"]] = item
            if {3, 4, 5} <= responses.keys():
                break
        else:
            raise AssertionError(f"WEDGED: only got responses for {sorted(responses)}")

        assert responses[3]["error"]["code"] == dialect.TURN_STILL_RUNNING
        # get_state answering AT ALL is the point (a wedged reader would
        # never have parsed this request in the first place); is_streaming
        # is already False here because new_session's OWN abort() signal
        # (AgentSession.abort() flips it unconditionally, before waiting on
        # anything) landed first -- not evidence either way about the wedge.
        assert "is_streaming" in responses[4]["result"]
        assert responses[5]["result"]["status"] == "aborted"
    finally:
        await _shutdown(proc)


# ── R-T3: backpressure against a real process + abort reachability ─────────


async def test_backpressure_bounds_the_backlog_and_abort_stays_reachable_behind_it(
    fake_home, fake_state
):
    """R-T3 (docs/REMOTE-CONTROL.md §9): 'a host that stops reading must
    stall the loop rather than grow τ's memory. Assert bounded queue depth
    under a non-reading peer.' Combined here with the abort-reachability
    demonstration (§4[1], 'a host whose whole problem is that τ is
    producing faster than it can read is precisely the host that needs to
    abort').

    **Finding 3 (phase-4 review) and why this is not the original shape.**
    The original version of this test sent `abort` in the same breath as
    `prompt`, before either of us had read a single byte — so the credit
    bound was never actually reached before the turn unwound. Mutation-
    tested: short-circuiting `RPCHandler._acquire_event_credit` to
    `return True` (disabling the bound entirely) left it passing UNCHANGED
    (`seen_events_before_abort_ack` measured at 2 either way, against a
    threshold of `n_chunks // 2`). Counting wire events proved nothing,
    because nothing had forced the queue to fill before the count was taken.

    This version asserts something the bound actually moves, and does so
    without importing τ internals (`DEFAULT_OUTPUT_QUEUE_EVENT_BOUND`) the
    way the rest of this file's backpressure tests do — a deliberately
    large, black-box chunk/size count stands in for "comfortably more than
    whatever the internal bound is", which is also the correct posture for
    a document-only host that has no way to know the bound's value either
    (G1). The fake provider's OWN blocking `self.wfile.write(body)` call
    (several megabytes, one shot) is the server-side proof: a genuinely
    bounded output queue stalls `AgentLoop.run`'s streaming loop inside
    `_emit` once credits run out, which stops it pulling further bytes off
    the HTTP response, which backs the TCP connection all the way up to
    that blocking write — it CANNOT have returned yet after a few seconds
    of nothing reading the JSON-RPC wire. An unbounded queue (or the credit
    gate disabled) races the whole multi-megabyte reply to completion
    almost immediately regardless of whether anything reads the wire, and
    the write returns and sets `fake_state.write_complete` well within that
    window — this is the assertion that actually fails under the mutation
    (see the unit's own report for the measured before/after).

    `abort` is then sent while the queue is provably still stalled (not
    merely "immediately", as before) and must still be reachable — proof
    the reader was never wedged behind the stalled turn (S1/f1e762e's
    failure mode, extended here to ordinary backpressure, not only the swap
    verbs) — and the number of events that reached the wire ahead of its
    response is bounded by our OWN chunk count, not the internal constant.

    This is deliberately NOT `test_rpc_backpressure.py`'s white-box tests:
    those pin the mechanism; this drives the REAL subprocess end to end,
    per R-T3's own wording ("drive it against a real process with a peer
    that stops reading").
    """
    # Deliberately a large, black-box constant rather than a multiple of
    # DEFAULT_OUTPUT_QUEUE_EVENT_BOUND (see docstring) -- ~50 MB total.
    # Measured directly (phase-4 review fix): a ~6 MB body was NOT enough --
    # this machine's `tcp_rmem`/`tcp_wmem` autotune up to 6 MB / 4 MB, so the
    # kernel alone can absorb ~10 MB with the write() call returning
    # (falsely) instantly, no matter how the credit gate behaves, because
    # none of that capacity requires anything in USER SPACE to have read a
    # byte. ~50 MB is comfortably past that ceiling on any plausible
    # deployment target, so the fake provider's write() blocking is a robust
    # consequence of the child ceasing to read, not a coincidence of this
    # environment's buffer sizing.
    n_chunks = 25000
    fake_state.chunks = [str(i % 10) * 2000 for i in range(n_chunks)]
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "go"}})
        accept = await _recv(proc, timeout=5.0)
        assert accept["result"]["accepted"] is True

        # The peer that "stops reading": genuinely do not read anything for
        # a while. Local, no artificial provider delay -- an unbounded
        # implementation would finish producing (and enqueueing, and the
        # fake provider would finish WRITING) the entire multi-megabyte
        # reply well within this window.
        await asyncio.sleep(2.0)

        assert not fake_state.write_complete.is_set(), (
            "the fake provider finished writing its multi-megabyte response "
            "body while nothing has read anything off the JSON-RPC wire -- "
            "AgentLoop's own consumption of the provider stream was never "
            "stalled, i.e. the outbound queue is not actually bounded"
        )

        # abort must still be reachable while the turn is genuinely,
        # provably (see above) stalled on backpressure -- not merely sent
        # before backpressure had a chance to matter.
        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "abort"})

        seen_events_before_abort_ack = 0
        abort_resp: dict[str, Any] | None = None
        for _ in range(n_chunks + 50):
            item = await _recv(proc, timeout=10.0)
            if item.get("id") == 2:
                abort_resp = item
                break
            assert item.get("method") == "event"
            seen_events_before_abort_ack += 1
        assert abort_resp is not None, "abort's response never arrived -- reader wedged"
        assert abort_resp["result"]["status"] == "aborted"
        assert seen_events_before_abort_ack < n_chunks // 2, (
            f"{seen_events_before_abort_ack} events were enqueued ahead of abort's response "
            f"out of {n_chunks} produced -- the outbound queue is not bounded"
        )
    finally:
        await _shutdown(proc)


# ── P3: extension-requested shutdown, through the real entry point ─────────


async def test_extension_requested_shutdown_ends_the_process_cleanly(tmp_path, fake_home):
    """P3 (docs/REMOTE-CONTROL.md §4[7]): an extension can request shutdown,
    checked after each command. A `/trigger_shutdown` command calls
    `ctx.shutdown()`; the process must then exit cleanly (no signal, exit
    code 0 — `rpc_mode.py`'s `handler.exit_code or 0`) shortly after
    answering that command, with no second command required."""
    ext_path = tmp_path / "shutdown_ext.py"
    ext_path.write_text(
        "def _extension(api):\n"
        "    def _on_trigger(args, ctx):\n"
        "        ctx.shutdown()\n"
        "        return 'shutting down'\n"
        "    api.register_command(\n"
        "        'trigger_shutdown',\n"
        "        {'description': 'test fixture', 'handler': _on_trigger},\n"
        "    )\n"
        "\n"
        "register = _extension\n"
    )
    proc = await _spawn(fake_home, extra_args=["-e", str(ext_path)])
    try:
        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "prompt",
                "params": {"text": "/trigger_shutdown", "expand_commands": True},
            },
        )
        resp, _skipped = await _recv_response(proc, 1, timeout=5.0)
        assert resp["result"]["accepted"] is True

        # No further command is sent. The process must still end on its
        # own, cleanly -- P3's whole point.
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        assert proc.returncode == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_extension_requested_shutdown_from_a_turn_end_hook_ends_the_process(
    tmp_path, fake_home
):
    """Finding 4 (phase-4 review): the test above only exercises P3's
    SYNCHRONOUS path -- a command handler that calls `ctx.shutdown()` and
    returns, resolved inside the very `_handle_line` dispatch that reads
    `shutdown_requested` right afterwards. The most likely REAL producer of
    a shutdown request is different: an extension hook firing DURING the
    background turn `submit`/`prompt` already returned its acceptance
    response for (C3) -- `api.on("turn_end", …)` -- which sets the flag
    well after `_handle_line` has already returned. With no second command
    sent, the reader is parked in `readline()` waiting for one that never
    comes, and nothing was checking the flag again there.

    Verified end to end (phase-4 review) against the pre-fix code: `[ext]
    turn_end -> ctx.shutdown()` reliably appeared on stderr, but the
    process was STILL ALIVE 10s later -- this test's own
    `asyncio.wait_for(proc.wait(), timeout=5.0)` timed out. This pins the
    fix (`RPCHandler._shutdown_signal`, raced against `readline()` in
    `transport._read_stdin`).
    """
    ext_path = tmp_path / "turn_end_shutdown_ext.py"
    ext_path.write_text(
        "import sys\n"
        "def _extension(api):\n"
        "    def _on_turn_end(event, ctx):\n"
        "        print('[ext] turn_end -> ctx.shutdown()', file=sys.stderr)\n"
        "        ctx.shutdown()\n"
        "    api.on('turn_end', _on_turn_end)\n"
        "\n"
        "register = _extension\n"
    )
    proc = await _spawn(fake_home, extra_args=["-e", str(ext_path)])
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "hi"}})
        accept, _skipped = await _recv_response(proc, 1, timeout=5.0)
        assert accept["result"]["accepted"] is True

        # No second command -- the reader is left idle in readline() once
        # the turn (and its turn_end hook) finishes. The process must still
        # end on its own, cleanly.
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        assert proc.returncode == 0

        assert proc.stderr is not None
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
        assert "[ext] turn_end -> ctx.shutdown()" in stderr
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


# ── blockers 1/2 (phase-4 review): the shutdown matrix, non-reading peer ───
#
# The adversarial review's headline finding: "a τ process whose peer stops
# reading cannot be killed" -- the exact opposite of what T3 backpressure set
# out to deliver (G5: kill always works), reproduced against a real
# subprocess for all three shutdown triggers. Two independent defects, fixed
# together here:
#
#   Blocker 1 -- a credit-starved turn's own `agent_end` re-emission (inside
#   `AgentLoop.run`'s `except BaseException` bracket) had no escape once the
#   ONE cancellation that got it there was already spent; `RPCHandler
#   ._background_tasks` was also never reaped by `run()`, so the stalled
#   turn outlived it -- `asyncio.run()`'s OWN shutdown (`asyncio.runners
#   ._cancel_all_tasks`) then joined that orphaned task forever.
#
#   Blocker 2 -- the stdout writer ran its blocking `write()`/`flush()` on
#   a `ThreadPoolExecutor` thread (`loop.run_in_executor`); cancelling the
#   *task* awaiting it detaches without stopping the *thread*, which stays
#   parked in the `write()` syscall against a peer that never drains the
#   pipe. Verbatim the hazard `_read_stdin`'s own docstring documents and
#   fixed for the reader (`loop.connect_read_pipe`) -- the writer was
#   simply left behind.
#
# A note on WHY these tests drain `proc.stdout` concurrently with
# `proc.wait()` rather than never reading it again at all: `asyncio.subprocess
# .Process.stdout` is a `StreamReader` with its own 64 KiB flow-control
# high-water mark (`asyncio.streams._DEFAULT_LIMIT`), independent of
# anything τ's protocol or either blocker's fix controls -- once that much
# unconsumed data is buffered, the transport PAUSES reading from the
# underlying OS pipe, which means the driver stops draining it regardless of
# what τ does on its own side. A raw, non-Python host reading via `read(2)`
# directly would not have this second, driver-side limit; simulating "the
# peer stops reading" here means the TEST temporarily stops calling
# `readline()` (proving the signal/EOF path itself is not wedged behind a
# stalled turn), then resumes -- exactly `test_backpressure_bounds_the_
# backlog_and_abort_stays_reachable_behind_it`'s own established pattern,
# reused here for signals instead of `abort`.


async def test_sigterm_kills_a_stalled_turn_behind_a_non_reading_peer(fake_home, fake_state):
    """Blocker 1 + blocker 2, combined: SIGTERM against a turn genuinely
    stalled on backpressure (nothing has read anything since the admission
    ack) must still exit 143 within a bounded time.

    `n_chunks` comfortably exceeds `DEFAULT_OUTPUT_QUEUE_EVENT_BOUND` so the
    turn is provably credit-starved (not merely slow) by the time SIGTERM
    lands: `AgentLoop.run`'s streaming loop is stalled inside `_emit`,
    genuinely blocked on backpressure -- the exact state
    `_acquire_event_credit`'s `_shutting_down` escape (blocker 1) and the
    non-thread-blocked writer (blocker 2) exist for.

    Mutation-tested (phase-4 review): reverting EITHER blocker's fix alone
    reproduces the hang this pins -- see this unit's own report for the
    measured before/after timings.
    """
    n_chunks = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND * 8
    fake_state.chunks = [str(i % 10) for i in range(n_chunks)]
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "go"}})
        accept = await _recv(proc, timeout=5.0)
        assert accept["result"]["accepted"] is True

        # Genuinely stop reading -- long enough that backpressure has
        # provably stalled the turn (T3's own bound is well under a
        # second's worth of local, unthrottled production).
        await asyncio.sleep(1.0)

        proc.send_signal(signal.SIGTERM)

        drain_task = asyncio.create_task(_drain_to_eof(proc))
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        finally:
            drain_task.cancel()
        assert proc.returncode == 143
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_sighup_drains_and_exits_behind_a_non_reading_peer(fake_home, fake_state):
    """SIGHUP against the same stalled-turn setup: drains (P4) and exits
    129 within a bounded time, once the peer resumes reading.

    Unlike SIGTERM, `run()`'s SIGHUP path never cancels the writer -- it
    lets it drain to completion (P1) -- so this test is NOT independently
    mutation-sensitive to blocker 2 (verified: it also passes, at
    comparable speed, against the pre-fix writer). It is kept anyway as the
    P4 conformance test the requirement asks for, and because it shares
    this file's other real-subprocess proof that the SIGNAL path itself is
    never wedged behind a stalled turn -- see this unit's own report for
    the measured mutation result.
    """
    n_chunks = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND * 8
    fake_state.chunks = [str(i % 10) for i in range(n_chunks)]
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "go"}})
        accept = await _recv(proc, timeout=5.0)
        assert accept["result"]["accepted"] is True

        await asyncio.sleep(1.0)

        proc.send_signal(signal.SIGHUP)

        drain_task = asyncio.create_task(_drain_to_eof(proc))
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        finally:
            drain_task.cancel()
        assert proc.returncode == 129
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_stdin_eof_exits_cleanly_behind_a_non_reading_peer(fake_home, fake_state):
    """stdin EOF against the same stalled-turn setup: exits cleanly (0)
    within a bounded time, once the peer resumes reading.

    Same relationship to blocker 2 as the SIGHUP test above: EOF takes the
    identical clean-drain path (never cancels the writer), so it is not
    independently mutation-sensitive to blocker 2 either -- see this unit's
    own report.
    """
    n_chunks = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND * 8
    fake_state.chunks = [str(i % 10) for i in range(n_chunks)]
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "go"}})
        accept = await _recv(proc, timeout=5.0)
        assert accept["result"]["accepted"] is True

        await asyncio.sleep(1.0)

        assert proc.stdin is not None
        proc.stdin.close()

        drain_task = asyncio.create_task(_drain_to_eof(proc))
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        finally:
            drain_task.cancel()
        assert proc.returncode == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_stdin_eof_exits_when_the_peer_never_resumes_reading(fake_home, fake_state):
    """Blocker 3 (found by finding 3's own fix, phase-4 review): EOF against
    a peer that never resumes reading must ALSO end the process.

    The three tests above all end with `_drain_to_eof` -- the peer stops
    reading long enough to stall the turn, then RESUMES. That is the right
    shape for what they pin (the signal/EOF path is not wedged behind a
    stalled turn) and it is forced on them by the driver, not chosen: an
    `asyncio.subprocess` `StreamReader` pauses its own transport past 64 KiB
    of unread data, so `proc.wait()` cannot resolve while the test is not
    reading, however promptly the child exits. Every "non-reading peer" test
    written through a `StreamReader` therefore silently tests the RESUMING
    peer.

    A real host has no such second limit -- and R-T3's premise is a host
    that genuinely stops. Closing stdin and walking away left τ parked in
    `writer.drain()` on a full pipe with a backlog behind it and `run()`
    waiting on that writer forever: EOF, the documented clean-shutdown
    trigger (P4), could not end the process. Same unkillable-process class
    as this section's blockers 1 and 2, reached by a third route, and
    invisible to all three tests above.

    So stdout here is a RAW `os.pipe()` whose read end is never touched.
    `proc.wait()` is then gated on process exit alone, and the pipe fills
    and stays full exactly as a stopped host's would.

    Measured (phase-4 review): against the pre-fix child this hangs -- still
    alive 25s after EOF, killed. With `RPCHandler._flush_stdout_with_
    deadline` it exits 0 about `_SHUTDOWN_FLUSH_NO_PROGRESS_TIMEOUT_S` after
    EOF, announcing the truncation on stderr (T4).
    """
    n_chunks = DEFAULT_OUTPUT_QUEUE_EVENT_BOUND * 8
    # ~2 KB per event: 512 of them is far past the 64 KiB pipe buffer, so
    # the writer is provably blocked on the OS rather than merely idle.
    fake_state.chunks = [str(i % 10) * 2000 for i in range(n_chunks)]

    read_fd, write_fd = os.pipe()
    env = _child_env(fake_home)
    argv = [sys.executable, "-m", "tau_coding_agent.cli", "--mode", "rpc"]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=write_fd,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    os.close(write_fd)  # the child owns the write end now
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "go"}})

        # Nothing ever reads `read_fd`. Long enough for the turn to fill the
        # pipe and stall.
        await asyncio.sleep(2.0)

        assert proc.stdin is not None
        proc.stdin.close()

        await asyncio.wait_for(proc.wait(), timeout=25.0)
        assert proc.returncode == 0

        assert proc.stderr is not None
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
        assert "giving up on flushing" in stderr, (
            "the process exited but never said it had truncated the stream "
            f"(T4); stderr was:\n{stderr}"
        )
    finally:
        os.close(read_fd)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


# ── B7: Tier B — schema-matches-wire-bytes + set_model persists (RPC-TIER-B ─
# .md §3 "B7 — integration") ──────────────────────────────────────────────
#
# No merged Tier B unit's own test drives either of these, because every one
# of them constructs an ``RPCHandler`` and calls its handler function
# in-process (docs/RPC-TIER-B.md §3's own per-unit scope): that pins the
# Python return VALUE, never the JSON that actually reaches a peer, and none
# of them writes a session to disk and reloads it the way a real host
# restart would. Both gaps are exactly what let the phase review's finding
# #2 through: ``SET_AUTO_COMPACTION_RESULT_SCHEMA`` claimed
# ``additionalProperties: false`` while the dispatcher (handler.py:1009, D2)
# grafts ``result.method`` onto every real response — a fact no in-process
# handler-return-value test can see, because that value is what ``{**result,
# "method": method}`` is built FROM, not what a peer receives.


async def test_tier_b_results_match_their_published_schemas_and_set_model_persists_across_reload(
    fake_home_two_models,
):
    """Drives all seven Tier B verbs (``compact``, ``get_last_assistant_text``,
    ``get_session_name``, ``get_session_stats``, ``set_auto_compaction``,
    ``set_model``, ``set_session_name``) against a real ``tau --mode rpc``
    child and validates each REAL response's ``result`` — ``method``
    included — against the ``result_schema`` the SAME child publishes for
    that verb via ``get_capabilities``. This is the class of check that
    would have failed on finding #2 before the fix
    (``SET_AUTO_COMPACTION_RESULT_SCHEMA``'s stray ``additionalProperties:
    false`` rejecting the very ``method`` key every real response carries),
    and it is mutation-tested against exactly that regression (see the
    unit's own report).

    Separately (D-2, RPC-TIER-B.md §1: the bare ``AgentSession.set_model``
    "does not persist anything" — that gap is what this verb exists to
    close): proves the ``model_change`` entry ``set_model`` appends actually
    survives a reload, not merely that the in-memory response looks right.
    ``fork`` gives a real file on disk — as, since Blocker 2, does the
    startup session this test forks FROM (that session's own durability is
    ``test_the_startup_session_itself_survives_with_both_entries``'s subject,
    below; this one deliberately keeps forking, so the schema conformance it
    checks stays independent of that fix); after ``set_model`` returns, that
    file is re-parsed with
    ``tau_coding_agent.session_store.Session.load`` — the SAME reload code a
    resumed process runs, not a hand-rolled scan of the JSONL — and its
    ``.model`` property (a fold over ``model_change`` entries, independent
    of anything already asserted about the RPC response) must report the
    switched-to name.
    """
    proc = await _spawn(fake_home_two_models)
    session_id: str | None = None
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"})
        caps, _ = await _recv_response(proc, 1)
        schemas = {c["name"]: c["result_schema"] for c in caps["result"]["commands"]}
        tier_b_verbs = {
            "compact",
            "get_last_assistant_text",
            "get_session_name",
            "get_session_stats",
            "set_auto_compaction",
            "set_model",
            "set_session_name",
        }
        assert tier_b_verbs <= set(schemas), "get_capabilities dropped a Tier B verb"

        def _check(name: str, result: dict[str, Any]) -> None:
            violation = rpc_commands.validate_params(schemas[name], result)
            assert violation is None, (
                f"{name}'s real response violates its own published "
                f"result_schema: {violation}\nresult={result}"
            )

        # A real, persisted (file-backed) session to run the seven verbs
        # against -- a fork, kept deliberately distinct from the startup
        # session so this test's subject stays "the published result
        # schemas", not "does startup persist" (Blocker 2's own test).
        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "fork"})
        forked, _ = await _recv_response(proc, 2)
        session_id = forked["result"]["session"]["session_id"]

        await _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "get_session_name"})
        name0, _ = await _recv_response(proc, 3)
        _check("get_session_name", name0["result"])
        assert name0["result"]["name"] is None

        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "set_session_name",
                "params": {"name": "b7-conformance"},
            },
        )
        set_name, _ = await _recv_response(proc, 4)
        _check("set_session_name", set_name["result"])
        assert set_name["result"]["name"] == "b7-conformance"
        assert set_name["result"]["cursor"]

        await _send(proc, {"jsonrpc": "2.0", "id": 5, "method": "get_session_name"})
        name1, _ = await _recv_response(proc, 5)
        _check("get_session_name", name1["result"])
        assert name1["result"]["name"] == "b7-conformance"

        await _send(proc, {"jsonrpc": "2.0", "id": 6, "method": "get_session_stats"})
        stats, _ = await _recv_response(proc, 6)
        _check("get_session_stats", stats["result"])
        assert stats["result"]["compaction_settings"]["enabled"] is False
        assert stats["result"]["usage"] is None
        assert stats["result"]["last_compaction"] is None

        await _send(proc, {"jsonrpc": "2.0", "id": 7, "method": "get_last_assistant_text"})
        last_text, _ = await _recv_response(proc, 7)
        _check("get_last_assistant_text", last_text["result"])
        assert last_text["result"]["text"] is None

        # The verb finding #2 was about, by name -- the assertion that
        # fails first if the stray `additionalProperties: false` ever comes
        # back.
        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "set_auto_compaction",
                "params": {"enabled": True},
            },
        )
        auto_compact, _ = await _recv_response(proc, 8)
        _check("set_auto_compaction", auto_compact["result"])
        assert auto_compact["result"]["enabled"] is True

        # C3 (Blocker 1, Tier B review): compact's RESPONSE is only an
        # acknowledgement -- the outcome rides the compaction_end
        # notification, whose payload is checked against
        # COMPACTION_END_PARAMS_SCHEMA. That constant is imported from this
        # process rather than pulled from the child's get_capabilities
        # because the capability document has no slot for a notification's
        # schema (stated in the verb's own notes); the bytes are still the
        # child's.
        await _send(proc, {"jsonrpc": "2.0", "id": 9, "method": "compact"})
        compacted, _ = await _recv_response(proc, 9)
        _check("compact", compacted["result"])
        assert compacted["result"]["accepted"] is True
        assert compacted["result"]["compaction_id"]
        end = await _recv_notification(proc, "compaction_end")
        violation = rpc_commands.validate_params(
            rpc_commands.COMPACTION_END_PARAMS_SCHEMA, end["params"]
        )
        assert violation is None, f"compaction_end violates its schema: {violation}"
        assert end["params"]["compaction_id"] == compacted["result"]["compaction_id"]
        assert end["params"]["request_id"] == 9
        assert end["params"]["is_error"] is False
        assert end["params"]["performed"] is False  # nothing to compact yet
        assert end["params"]["cursor"]

        await _send(
            proc,
            {"jsonrpc": "2.0", "id": 10, "method": "set_model", "params": {"name": "fake-alt"}},
        )
        switched, _ = await _recv_response(proc, 10)
        _check("set_model", switched["result"])
        assert switched["result"]["cursor"]
    finally:
        await _shutdown(proc)

    assert session_id is not None

    # Reload, for real: parse the on-disk session with the SAME code a
    # resumed process uses (Session.load), not through the RPC wire at all
    # -- proving the model_change entry set_model appended is durable, not
    # merely reflected in the response this same call already returned.
    session_dir = session_dir_for_cwd(
        os.getcwd(), base_dir=rpc_session_base(fake_home_two_models)
    )
    matches = list(session_dir.glob(f"*_{session_id}.jsonl"))
    assert len(matches) == 1, f"expected exactly one session file for {session_id}, found {matches}"
    reloaded = Session.load(matches[0])
    assert reloaded.model == "fake-alt", (
        "set_model's own response claimed success, but the model_change "
        "entry it should have appended (D-2) is not what a fresh reload of "
        f"the session file sees (got {reloaded.model!r})"
    )


# ── Blocker 2 (Tier B review): the STARTUP session must keep what it is told ──
#
# The test above forks first, and says why: until this fix the startup session
# was `create_ephemeral`, so it had no file to reload and the only way to
# check durability at all was to route around it. That routing-around is
# exactly what let the defect through -- on the session every host actually
# starts on, `set_model` and `set_session_name` appended into a list nobody
# ever wrote, and returned a cursor implying otherwise. The verbs looked
# right, the replay showed nothing, and `require_log_appender` passed because
# an ephemeral `Session` has every appender.
#
# So this test forks nothing, switches nothing, and creates nothing: it drives
# the two durability-promising verbs against the session the child chose for
# itself at startup, then reads the disk after that child is gone.


async def test_the_startup_session_itself_survives_with_both_entries(fake_home_two_models):
    """Blocker 2's reproduction, inverted into a regression test.

    Set the name and the model over the wire on the STARTUP session, let the
    child exit, then assert a real session file exists carrying both -- via
    ``Session.load``, the same reload a resumed process runs.

    MUTATION TARGET, both halves of the fix, and they fail this test at
    DIFFERENT lines -- worth stating, because only the second is the original
    bug:

    1. Put ``create_ephemeral`` back in ``rpc_mode.run_rpc`` (in place of
       ``create``): red at ``set_session_name``'s ``["result"]`` (KeyError) --
       the verb now REFUSES on an unpersisted startup session, which is the
       other half of the fix working.
    2. Do that AND drop ``require_durable_session`` from both handlers --
       i.e. restore the exact pre-fix code: red at "the startup session left
       NO file on disk", with every wire response above still passing,
       cursors included. That is the reproduction verbatim, and the reason
       this test needs a subprocess and a disk rather than a handler return
       value: nothing observable on the wire was ever wrong.
    """
    proc = await _spawn(fake_home_two_models)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "get_state"})
        state, _ = await _recv_response(proc, 1)
        startup_id = state["result"]["session_id"]

        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "set_session_name",
                "params": {"name": "my-important-session"},
            },
        )
        named, _ = await _recv_response(proc, 2)
        assert named["result"]["name"] == "my-important-session"
        assert named["result"]["cursor"]

        await _send(
            proc,
            {"jsonrpc": "2.0", "id": 3, "method": "set_model", "params": {"name": "fake-alt"}},
        )
        switched, _ = await _recv_response(proc, 3)
        assert switched["result"]["model"]["id"] == "fake-model-alt"
        assert switched["result"]["cursor"]

        # The session was never forked/switched/replaced: what those two
        # verbs wrote to is still the one the process started on.
        await _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "get_state"})
        after, _ = await _recv_response(proc, 4)
        assert after["result"]["session_id"] == startup_id
    finally:
        await _shutdown(proc)

    session_dir = session_dir_for_cwd(
        os.getcwd(), base_dir=rpc_session_base(fake_home_two_models)
    )
    on_disk = sorted(session_dir.glob("*.jsonl")) if session_dir.is_dir() else []
    assert [p.name for p in on_disk if startup_id in p.name], (
        "the startup session left NO file on disk, so the cursors "
        "set_session_name and set_model just returned promised a durable "
        f"write that never happened (Blocker 2). Files found: {on_disk}"
    )
    matches = [p for p in on_disk if startup_id in p.name]
    assert len(matches) == 1, f"expected one file for {startup_id}, found {matches}"

    reloaded = Session.load(matches[0])
    assert reloaded.id == startup_id
    assert reloaded.name == "my-important-session", (
        "set_session_name's response carried a cursor, but a fresh reload of "
        f"the startup session sees name={reloaded.name!r}"
    )
    assert reloaded.model == "fake-alt", (
        "set_model's response carried a cursor, but a fresh reload of the "
        f"startup session sees model={reloaded.model!r}"
    )


async def test_an_unpersisted_session_refuses_the_durability_promising_verbs(fake_home_two_models):
    """The other half of the fix: ``new_session {"persist": false}`` is still
    reachable, and on THAT session ``set_session_name``/``set_model``/
    ``compact`` refuse instead of returning a cursor for a write that lands
    nowhere (``require_durable_session``). Fail-Early, over the wire, end to
    end.

    ``compact`` joined them at finding 6 (D-7), and this test is where the
    three-way inconsistency that finding measured is now impossible: it
    drives all four Tier B mutators against ONE unpersisted session, so the
    rule -- "the verb that APPENDS refuses; the verb that appends nothing
    answers" -- is visible in one place rather than derivable from none.
    ``set_auto_compaction`` succeeding is asserted with the same weight as
    the three refusals: a future edit that "restores consistency" by
    guarding it too fails here.

    MUTATION TARGET: drop the ``require_durable_session`` call from any of
    the three handlers and that verb answers with a ``result`` carrying a
    cursor -- which is precisely the lie this whole blocker was about; or
    add one to ``_handle_set_auto_compaction`` and the last block goes red.
    """
    proc = await _spawn(fake_home_two_models)
    try:
        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "new_session",
                "params": {"persist": False},
            },
        )
        fresh, _ = await _recv_response(proc, 1)
        assert fresh["result"]["cancelled"] is False

        await _send(
            proc,
            {"jsonrpc": "2.0", "id": 2, "method": "set_session_name", "params": {"name": "nope"}},
        )
        named, _ = await _recv_response(proc, 2)
        assert "result" not in named
        assert named["error"]["code"] == dialect.SESSION_NOT_PERSISTED
        assert "unpersisted" in named["error"]["message"]

        await _send(
            proc,
            {"jsonrpc": "2.0", "id": 3, "method": "set_model", "params": {"name": "fake-alt"}},
        )
        switched, _ = await _recv_response(proc, 3)
        assert "result" not in switched
        assert switched["error"]["code"] == dialect.SESSION_NOT_PERSISTED
        assert "unpersisted" in switched["error"]["message"]

        # D-7 rule 1's third member (finding 6): compact appends a
        # `compaction` entry, so it refuses here too -- with an ERROR
        # response, not an `accepted` acknowledgement followed by a
        # compaction_end carrying a cursor for an entry that dies with the
        # process, which is what it used to do.
        await _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "compact"})
        compacted, _ = await _recv_response(proc, 4)
        assert "result" not in compacted
        assert compacted["error"]["code"] == dialect.SESSION_NOT_PERSISTED
        assert "unpersisted" in compacted["error"]["message"]

        # And the refusal was total: the model did not change either.
        await _send(proc, {"jsonrpc": "2.0", "id": 5, "method": "get_state"})
        state, _ = await _recv_response(proc, 5)
        assert state["result"]["model"]["id"] == "fake-model"

        # D-7 rule 2, on the SAME session: set_auto_compaction appends
        # nothing, so it answers -- cursor and all. The rule produces two
        # behaviours on purpose; both are pinned.
        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "set_auto_compaction",
                "params": {"enabled": True},
            },
        )
        auto, _ = await _recv_response(proc, 6)
        assert "error" not in auto, auto
        assert auto["result"]["enabled"] is True
        # Present, not necessarily non-null: E5 rule 3 requires the KEY on
        # every mutator completion, and on this session it is the live
        # in-memory tip, whatever that happens to be.
        assert "cursor" in auto["result"]
    finally:
        await _shutdown(proc)


async def test_new_session_persists_by_default_and_is_addressable(fake_home):
    """``new_session`` used to hardcode ``persist=False`` (Blocker 2, part b),
    so the ``{store, session_id, cursor}`` tuple it handed back named a
    session ``switch_session`` could never resolve. Default is now true, and
    "addressable" is proven by switching away and back rather than by
    reading the flag we just sent.
    """
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "new_session"})
        fresh, _ = await _recv_response(proc, 1)
        fresh_id = fresh["result"]["session"]["session_id"]

        # Move off it, then resolve it again by id -- impossible for an
        # unpersisted session, which is never written and never listed.
        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "fork"})
        await _recv_response(proc, 2)

        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "switch_session",
                "params": {"session_id": fresh_id},
            },
        )
        switched, _ = await _recv_response(proc, 3)
        assert switched["result"]["cancelled"] is False
        assert switched["result"]["session"]["session_id"] == fresh_id
    finally:
        await _shutdown(proc)


# ── findings 7 & 8 (Tier B review): the ids nothing produced, and the tuple ──
#    that called itself addressable
#
# Finding 8: `switch_session` took a session id and NOTHING on the wire
# enumerated them, so a host could only ever reach a session it had made in
# this process. The three tests below drive `list_sessions` against a real
# child, and the middle one is the finding itself: a session made by an
# EARLIER child, unreachable before this verb existed.
#
# Finding 7: `new_session {"persist": false}` returned an "addressable tuple"
# `switch_session` then answered -32602 for. The last test measures the
# corrected shape end to end.


async def test_list_sessions_names_the_universe_this_child_is_looking_at(fake_home):
    """The child's own startup session is in its listing, and the row's
    ``ref`` says WHICH session directory that is — unit S (D-6/H1b) made
    ``--mode rpc`` default to ``<tmp>/.tau-<uid>/sessions`` while the TUI and
    ``--print`` stay on ``~/.tau/sessions``, so "which list am I reading" is
    a real question with a non-obvious answer.

    Asserted against the real directory this suite's ``rpc_session_base``
    already knows, and against ``$HOME`` in the negative — a verb that
    listed the user's sessions instead would pass every shape check above
    and be wrong about the only thing that matters here.

    The result is also validated against the ``result_schema`` this SAME
    child publishes for the verb, the check
    ``test_tier_b_results_match_their_published_schemas...`` applies to the
    rest of the tier.
    """
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"})
        caps, _ = await _recv_response(proc, 1)
        schema = {c["name"]: c["result_schema"] for c in caps["result"]["commands"]}[
            "list_sessions"
        ]

        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "get_state"})
        state, _ = await _recv_response(proc, 2)
        startup_id = state["result"]["session_id"]

        await _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "list_sessions"})
        listed, _ = await _recv_response(proc, 3)
        result = listed["result"]
        assert rpc_commands.validate_params(schema, result) is None, result

        # The child's own session is one of the rows -- so the listing is
        # never empty in RPC mode, and `get_state` is how a host finds itself
        # in it (the verb deliberately flags no row as "current").
        rows = {row["session_id"]: row for row in result["sessions"]}
        assert startup_id in rows, result

        base = rpc_session_base(fake_home)
        assert Path(rows[startup_id]["ref"]).is_relative_to(base)
        assert not Path(rows[startup_id]["ref"]).is_relative_to(fake_home / ".tau")
        assert result["scope"] == {"store": "file", "cwd": os.getcwd()}
    finally:
        await _shutdown(proc)


async def test_a_session_an_earlier_child_made_is_reachable_by_the_next_one(fake_home):
    """THE finding, end to end and across two processes: before
    ``list_sessions`` a host could only switch to a session it had created
    itself, so a session left by an earlier RPC child (or by the TUI, or by
    ``tau -p``) was unreachable no matter what it contained.

    The first child forks and NAMES the fork — a durable ``session_info``
    entry (D-2/B5) — then exits. The second child, which has never seen that
    id, finds it by name in ``list_sessions``, switches to it by the id the
    listing gave, and reads the name back off the loaded session. Nothing
    reads the session directory out of band anywhere in the loop, which is
    the G1 claim.
    """
    first = await _spawn(fake_home)
    try:
        await _send(first, {"jsonrpc": "2.0", "id": 1, "method": "fork"})
        forked, _ = await _recv_response(first, 1)
        planted_id = forked["result"]["session"]["session_id"]
        assert forked["result"]["session"]["addressable"] is True

        await _send(
            first,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "set_session_name",
                "params": {"name": "left-by-the-first-child"},
            },
        )
        named, _ = await _recv_response(first, 2)
        assert named["result"]["name"] == "left-by-the-first-child"
    finally:
        await _shutdown(first)

    second = await _spawn(fake_home)
    try:
        await _send(second, {"jsonrpc": "2.0", "id": 1, "method": "list_sessions"})
        listed, _ = await _recv_response(second, 1)
        matches = [
            row for row in listed["result"]["sessions"] if row["name"] == "left-by-the-first-child"
        ]
        assert len(matches) == 1, listed["result"]["sessions"]
        assert matches[0]["session_id"] == planted_id
        assert matches[0]["title"] == "left-by-the-first-child"

        await _send(
            second,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "switch_session",
                "params": {"session_id": matches[0]["session_id"]},
            },
        )
        switched, _ = await _recv_response(second, 2)
        assert switched["result"]["cancelled"] is False
        assert switched["result"]["session"]["session_id"] == planted_id
        assert switched["result"]["session"]["addressable"] is True

        await _send(second, {"jsonrpc": "2.0", "id": 3, "method": "get_session_name"})
        name, _ = await _recv_response(second, 3)
        assert name["result"]["name"] == "left-by-the-first-child"
    finally:
        await _shutdown(second)


async def test_an_unpersisted_session_says_it_is_unaddressable_and_the_listing_agrees(fake_home):
    """Finding 7's measured trace, and the corrected shape beside it.

    ``new_session {"persist": false}`` -> a tuple that now says
    ``addressable: false``; ``list_sessions`` does not contain its id;
    ``switch_session`` on that id answers -32602 (the exact pair the finding
    reported, minus the tuple's claim to be addressable). Then the same three
    calls with the default ``persist``, where all three agree the other way —
    so this cannot pass by a handler that hardcodes either answer.
    """
    proc = await _spawn(fake_home)
    try:
        await _send(
            proc,
            {"jsonrpc": "2.0", "id": 1, "method": "new_session", "params": {"persist": False}},
        )
        unpersisted, _ = await _recv_response(proc, 1)
        unpersisted_id = unpersisted["result"]["session"]["session_id"]
        assert unpersisted["result"]["session"]["addressable"] is False

        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "list_sessions"})
        listed, _ = await _recv_response(proc, 2)
        assert unpersisted_id not in {row["session_id"] for row in listed["result"]["sessions"]}

        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "switch_session",
                "params": {"session_id": unpersisted_id},
            },
        )
        refused, _ = await _recv_response(proc, 3)
        assert refused["error"]["code"] == dialect.INVALID_PARAMS
        assert unpersisted_id in refused["error"]["message"]

        # The other half of the same contract, so neither answer can be a
        # constant: a persisted session is addressable AND listed AND
        # switchable.
        await _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "new_session"})
        persisted, _ = await _recv_response(proc, 4)
        persisted_id = persisted["result"]["session"]["session_id"]
        assert persisted["result"]["session"]["addressable"] is True

        await _send(proc, {"jsonrpc": "2.0", "id": 5, "method": "list_sessions"})
        listed_again, _ = await _recv_response(proc, 5)
        assert persisted_id in {row["session_id"] for row in listed_again["result"]["sessions"]}
    finally:
        await _shutdown(proc)


# ── Blocker 1 (Tier B review): a slow compaction must not wedge the reader ──
#
# The regression this file already had -- test_new_session_does_not_wedge_the_
# channel_when_the_turn_wont_stop -- pipelines one BOUNDED verb behind
# another, so it cannot see this: `compact` used to run the summarization LLM
# call INLINE on the dispatch path, and `transport._read_stdin` awaits each
# dispatched line to completion before parsing the next one. Measured before
# the fix, with the provider gated at 20s: compact, get_state and abort were
# all answered at t=20.01s -- the latter two not merely delayed but UNPARSED
# for the whole call.

#: How long the fake provider stalls the compaction's summary call. Long
#: enough that "answered while the compaction runs" and "answered when the
#: compaction ends" cannot be confused for one another under CI jitter, short
#: enough that this test costs seconds rather than the 20s the original
#: reproduction used.
_SLOW_COMPACTION_S = 6.0

#: The budget the three pipelined answers must land inside. Deliberately far
#: from BOTH numbers it discriminates between: > any plausible dispatch cost,
#: << `_SLOW_COMPACTION_S`.
_PIPELINE_BUDGET_S = 2.0

#: The conversation the compaction below needs, expressed the only way this
#: transport allows. The shipped `keep_recent_tokens` is 20000 (~4 chars/token),
#: and a cut that keeps everything removes nothing: `prepare_compaction` reports
#: that as "nothing to compact" (None), the provider is never called, and this
#: test's `outcome_at > _SLOW_COMPACTION_S * 0.8` half -- which exists precisely
#: to refuse "a compaction that did nothing at all" -- fails. So the turns must
#: together clear 20000 tokens with the FIRST turn still on the far side of the
#: cut: four turns of ~10000 tokens puts the cut on turn 3 with turns 1-2 as the
#: summarised prefix. Four medium prompts rather than one big one is now only
#: a convenience: this comment used to justify it with "a single request line
#: past the stdlib's 64 KiB limit kills the child", which stopped being true
#: at T7 (the bound is `transport.MAX_REQUEST_LINE_BYTES`, 8 MiB, and past it
#: is a `REQUEST_TOO_LARGE` refusal rather than a death). The FOUR-turn shape
#: is still required, for the reason above it — the cut must fall past turn 2.
#: See tau-agent-core/tests/test_compaction_engine.py for the same property
#: pinned at the unit layer.
_TURNS_BEFORE_COMPACTION = 4
_TURN_TEXT = "pad " + "x" * 40_000


async def test_a_slow_compaction_does_not_wedge_the_channel(fake_home, fake_state):
    """Pipeline `compact` (gated on a deliberately slow provider), then
    `get_state`, then `get_session_stats`, without reading in between -- and
    show all three answered promptly, with the compaction's own outcome
    arriving much later on its `compaction_end` notification (C3's second
    completion).

    This is the proof a unit test cannot give: it needs a real child whose
    reader is the single serial chokepoint, so that "the next line was not
    even parsed" is observable as latency on an UNRELATED verb rather than
    inferred from an in-process call graph.

    The third verb was `abort` until finding 5, and it moved for a reason
    worth stating: `abort` is no longer UNRELATED to a running compaction --
    it cancels it (that is finding 5's fix), so keeping it here would have
    destroyed the very thing the tail of this test measures, a compaction
    that really did take the provider's whole delay and really did perform.
    The availability property is about the READER, and any verb the reader
    must parse proves it. `abort`'s own version of this test, including that
    it too is answered inside the budget, is
    `test_abort_stops_a_slow_compaction_and_the_host_is_told` below.

    MUTATION TARGET: restore the pre-Blocker-1 `_handle_compact` body --

        async with turn_safety_guard(session):
            result = await session.compact(custom_instructions=...)
            ...return the outcome dict...

    -- and the `elapsed < _PIPELINE_BUDGET_S` assertion goes red with elapsed
    ≈ `_SLOW_COMPACTION_S`, which is exactly the defect: `abort` unanswerable
    for as long as the provider feels like taking (REMOTE-CONTROL.md §4[1]:
    "a host whose whole problem is that τ is producing faster than it can
    read is precisely the host that needs to abort").
    """
    proc = await _spawn(fake_home)
    try:
        # Real turns first, and enough of them. A compaction that never calls
        # the provider cannot be slow, and there are two separate ways to end up
        # with no provider call: an active path holding no message entry at all
        # (agent_session.py's `not any(... type in ("message", ...))` check),
        # and -- the one `_TURN_TEXT` is about -- a cut that keeps everything.
        for msg_id in range(1, _TURNS_BEFORE_COMPACTION + 1):
            await _send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "method": "prompt",
                    "params": {"text": f"{msg_id} {_TURN_TEXT}"},
                },
            )
            _accept, skipped = await _recv_response(proc, msg_id, timeout=30.0)
            if not any(
                i.get("method") == "event" and i["params"].get("type") == "agent_end"
                for i in skipped
            ):
                for _ in range(200):
                    item = await _recv(proc, timeout=30.0)
                    if item.get("method") == "event" and item["params"].get("type") == "agent_end":
                        break
                else:  # pragma: no cover - the turn always ends on the fake provider
                    raise AssertionError(f"turn {msg_id} never reached agent_end")

        fake_state.delay_s = _SLOW_COMPACTION_S

        loop = asyncio.get_running_loop()
        started = loop.time()
        compact_id = _TURNS_BEFORE_COMPACTION + 1
        await _send(proc, {"jsonrpc": "2.0", "id": compact_id, "method": "compact"})
        await _send(proc, {"jsonrpc": "2.0", "id": compact_id + 1, "method": "get_state"})
        await _send(proc, {"jsonrpc": "2.0", "id": compact_id + 2, "method": "get_session_stats"})

        ack, _ = await _recv_response(proc, compact_id, timeout=_SLOW_COMPACTION_S + 10.0)
        state, _ = await _recv_response(proc, compact_id + 1, timeout=_SLOW_COMPACTION_S + 10.0)
        stats, _ = await _recv_response(proc, compact_id + 2, timeout=_SLOW_COMPACTION_S + 10.0)
        elapsed = loop.time() - started

        assert ack["result"]["accepted"] is True
        assert "session_id" in state["result"]
        assert "context" in stats["result"]
        assert elapsed < _PIPELINE_BUDGET_S, (
            f"the three pipelined verbs took {elapsed:.2f}s -- the compaction's "
            f"{_SLOW_COMPACTION_S}s provider call is still holding the serial "
            "reader (Blocker 1)"
        )

        # ...and the compaction really was the slow thing: its outcome shows
        # up only after the provider finally answers. Without this half the
        # test would still pass against a compaction that did nothing at all.
        end = await _recv_notification(proc, "compaction_end", timeout=_SLOW_COMPACTION_S + 15.0)
        outcome_at = loop.time() - started
        assert end["params"]["request_id"] == compact_id
        assert end["params"]["compaction_id"] == ack["result"]["compaction_id"]
        assert outcome_at > _SLOW_COMPACTION_S * 0.8, (
            f"the compaction_end arrived after only {outcome_at:.2f}s -- the "
            f"provider was supposed to stall it for {_SLOW_COMPACTION_S}s, so "
            "this test is not measuring what it claims to"
        )
        # Timing alone once said "slow compaction" about a compaction that
        # removed nothing: the whole conversation sat under keep_recent_tokens,
        # the summariser was handed an empty <conversation>, and the stall being
        # measured was the fake provider answering THAT. State the premise
        # directly instead of trusting the clock for it. (The `tokens_saved`
        # arithmetic is not asserted here on purpose: `estimate_context_tokens`
        # anchors on the last assistant Usage, and this fake provider reports
        # single-digit token counts, so the wire numbers here are the fake's,
        # not the conversation's. That arithmetic is pinned where the estimate
        # is real -- tau-agent-core/tests/test_compaction_engine.py.)
        assert end["params"]["performed"] is True
        assert end["params"]["compacted_entry_ids"], "the cut removed no entry"
    finally:
        fake_state.delay_s = 0.0
        await _shutdown(proc)


async def test_abort_stops_a_slow_compaction_and_the_host_is_told(fake_home, fake_state):
    """Finding 5 of the Tier B review, end to end against a real child.

    The measured defect: `response id=4 at t=+0.00s {'result': {'status':
    'aborted'}}`, then `compaction_end at t=+20.01s ... 'performed': True`.
    `AgentSession.compact` consults no abort flag anywhere, so a host was
    told its abort succeeded while the tree was rewritten anyway -- a verb
    reporting success for something it did not do.

    Four claims, and only a subprocess can carry the last one:

    1. `abort` is still answered promptly (it is a signal, and the
       availability argument REMOTE-CONTROL.md §4[1] makes about it is
       unchanged by making it do more);
    2. its response NAMES the compaction it reached, so the completion that
       follows is correlatable to the abort that caused it;
    3. the `compaction_end` arrives with `cancelled: true`, no `performed`,
       and it arrives FAST -- well inside the provider delay the compaction
       would otherwise have taken, which is what proves the cancellation
       reached the provider call rather than the compaction merely finishing;
    4. nothing was written: `get_session_stats` on the same, REAL,
       file-backed session still reports `last_compaction: null`. That is
       the claim `cancelled: true` makes about the log, checked against the
       store rather than against a comment.

    MUTATION TARGET: drop the `handler.abort_compaction()` call from
    `commands._handle_abort` (returning `{"status": "aborted",
    "compaction_id": None}`) -- claim 2 goes red immediately, and claim 3
    times out because no compaction_end ever comes early; restoring the
    pre-fix behaviour entirely (no `cancelled` field, compaction runs on)
    additionally reddens claim 4.
    """
    proc = await _spawn(fake_home)
    try:
        for msg_id in range(1, _TURNS_BEFORE_COMPACTION + 1):
            await _send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "method": "prompt",
                    "params": {"text": f"{msg_id} {_TURN_TEXT}"},
                },
            )
            _accept, skipped = await _recv_response(proc, msg_id, timeout=30.0)
            if not any(
                i.get("method") == "event" and i["params"].get("type") == "agent_end"
                for i in skipped
            ):
                for _ in range(200):
                    item = await _recv(proc, timeout=30.0)
                    if item.get("method") == "event" and item["params"].get("type") == "agent_end":
                        break
                else:  # pragma: no cover - the turn always ends on the fake provider
                    raise AssertionError(f"turn {msg_id} never reached agent_end")

        fake_state.delay_s = _SLOW_COMPACTION_S

        loop = asyncio.get_running_loop()
        compact_id = _TURNS_BEFORE_COMPACTION + 1
        await _send(proc, {"jsonrpc": "2.0", "id": compact_id, "method": "compact"})
        ack, _ = await _recv_response(proc, compact_id, timeout=30.0)
        assert ack["result"]["accepted"] is True
        compaction_id = ack["result"]["compaction_id"]

        started = loop.time()
        await _send(proc, {"jsonrpc": "2.0", "id": compact_id + 1, "method": "abort"})
        aborted, _ = await _recv_response(proc, compact_id + 1, timeout=_SLOW_COMPACTION_S + 10.0)
        assert aborted["result"]["status"] == "aborted"
        assert aborted["result"]["compaction_id"] == compaction_id, (
            "abort did not name the compaction it signalled, so a host "
            "cannot correlate the compaction_end that follows"
        )

        end = await _recv_notification(
            proc, "compaction_end", timeout=_SLOW_COMPACTION_S + 10.0
        )
        outcome_at = loop.time() - started
        assert end["params"]["compaction_id"] == compaction_id
        assert end["params"]["cancelled"] is True
        assert end["params"]["is_error"] is False
        assert "performed" not in end["params"]
        assert outcome_at < _SLOW_COMPACTION_S * 0.8, (
            f"the compaction_end took {outcome_at:.2f}s after the abort -- "
            f"the provider was stalling for {_SLOW_COMPACTION_S}s, so this "
            "compaction ran to completion and the abort stopped nothing"
        )

        # Claim 4: the store, not a comment. Nothing was appended.
        fake_state.delay_s = 0.0
        await _send(proc, {"jsonrpc": "2.0", "id": compact_id + 2, "method": "get_session_stats"})
        stats, _ = await _recv_response(proc, compact_id + 2, timeout=30.0)
        assert stats["result"]["last_compaction"] is None, (
            "a compaction entry landed for a compaction that was cancelled "
            "before it finished"
        )
    finally:
        fake_state.delay_s = 0.0
        await _shutdown(proc)


#: Where inside `_BACKGROUND_TASK_GRACE_S` (1.0s) the compaction is released.
#: Bounded on BOTH sides and deliberately centred between them:
#:
#: - it must be LATER than one `_write_stdout` poll interval (0.5s), because
#:   before the fix that is when the writer had already drained, exited, and
#:   left the queue unread — release any earlier and the pre-fix code would
#:   deliver too, and this test would prove nothing;
#: - it must be EARLIER than the 1.0s grace, or `_cancel_background_tasks`
#:   reaches phase 2, cancels the compaction, and the run takes the (already
#:   correct, separately pinned) cancellation path instead.
#:
#: 0.75s leaves 250ms of scheduling slack on each side. If a loaded machine
#: overruns it anyway the run lands on the cancellation branch, which this
#: test also asserts — so the failure mode is a weaker proof, never a false
#: red.
_RELEASE_INSIDE_THE_REAP_S = 0.75


async def test_a_compaction_finishing_during_shutdown_is_never_silent(fake_home, fake_state):
    """Finding 3 (Tier B review): D-5's completion contract had a hole, and
    only a real child could show it.

    `run()`'s teardown drained and exited the writer BEFORE reaping the
    background tasks, and `_cancel_background_tasks`'s phase 1 waits
    `_BACKGROUND_TASK_GRACE_S` without cancelling. A compaction finishing in
    that window put its `compaction_end` on a queue nobody would ever read.
    Measured against this exact child, matrix over provider delay and
    stdin-close time: rc 0, EMPTY stderr, no notification -- and a
    `compaction` entry durably written to the session log, so the next
    process to resume that session found a compaction it was never told
    about. Neighbouring timings delivered correctly, so it was a race.

    The assertion is D-5's promise as amended, stated as a partition: a
    compaction reports EXACTLY ONE of three ways and never none of them --
    the notification on the wire, its whole outcome on stderr when the
    writer is provably gone, or the cancellation line on stderr when it was
    reaped before finishing. Before the fix this child reported ZERO of the
    three.

    Timing is parent-controlled, not guessed: the fake provider holds the
    summarization response until this test releases it (`_State.gate`), and
    `gate_reached` is server-side proof the child's request actually arrived
    first -- releasing a gate nobody was waiting on would sail through every
    assertion below while measuring nothing.

    MUTATION TARGET: move `self._shutting_down = True; await
    self._cancel_background_tasks()` in `handler.run()` back below the writer
    drain (leaving it only in the `finally`) -- the run goes back to "none of
    the three" and this goes red.

    Deliberately NOT the mutation target for the fix's other half
    (`transport._write_stdout`'s `if not self._running: break`, which the
    reorder made reachable): with the reap ahead of the drain, `_running` is
    still True for the whole release window, so that break cannot fire here
    at any release offset this test could choose. It is a lost-wakeup race,
    and racing a subprocess against it would be a flaky test pretending to be
    a proof. It is pinned structurally instead, in
    tau-agent-core/tests/test_rpc_tier_b_compact.py::
    test_the_writer_does_not_exit_with_items_still_queued."""
    proc = await _spawn(fake_home)
    gate = threading.Event()
    try:
        for msg_id in range(1, _TURNS_BEFORE_COMPACTION + 1):
            await _send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "method": "prompt",
                    "params": {"text": f"{msg_id} {_TURN_TEXT}"},
                },
            )
            _accept, skipped = await _recv_response(proc, msg_id, timeout=30.0)
            if not any(
                i.get("method") == "event" and i["params"].get("type") == "agent_end"
                for i in skipped
            ):
                for _ in range(200):
                    item = await _recv(proc, timeout=30.0)
                    if item.get("method") == "event" and item["params"].get("type") == "agent_end":
                        break
                else:  # pragma: no cover - the turn always ends on the fake provider
                    raise AssertionError(f"turn {msg_id} never reached agent_end")

        # From here the provider answers only when this test says so.
        fake_state.gate = gate
        compact_id = _TURNS_BEFORE_COMPACTION + 1
        await _send(proc, {"jsonrpc": "2.0", "id": compact_id, "method": "compact"})
        ack, _ = await _recv_response(proc, compact_id, timeout=30.0)
        assert ack["result"]["accepted"] is True
        compaction_id = ack["result"]["compaction_id"]

        # The compaction is genuinely parked in the provider call, not merely
        # admitted -- asserted from the SERVER side, in the child's own
        # request handler.
        assert await asyncio.to_thread(fake_state.gate_reached.wait, 20.0), (
            "the compaction never reached the provider, so there is no "
            "in-flight background task for shutdown to race"
        )

        assert proc.stdin is not None
        proc.stdin.close()  # EOF: teardown starts now (T1/P4)
        await asyncio.sleep(_RELEASE_INSIDE_THE_REAP_S)
        gate.set()  # ...and the compaction finishes INSIDE the reap window

        assert proc.stdout is not None
        stdout_lines: list[dict[str, Any]] = []
        while True:
            raw = await _read_line(proc, timeout=30.0)
            if not raw:
                break
            stdout_lines.append(json.loads(raw.decode("utf-8")))
        assert proc.stderr is not None
        err = (await proc.stderr.read()).decode("utf-8", errors="replace")
        rc = await asyncio.wait_for(proc.wait(), timeout=20.0)
        assert rc == 0, f"clean shutdown should exit 0; stderr:\n{err}"

        delivered = [
            i["params"]
            for i in stdout_lines
            if i.get("method") == rpc_commands.COMPACTION_END_METHOD
        ]
        undeliverable = "no compaction_end could be delivered" in err
        reaped = "was cancelled before it finished" in err

        reports = [bool(delivered), undeliverable, reaped]
        assert sum(reports) == 1, (
            "D-5 (as amended by finding 3): a compaction reports its outcome "
            "exactly one of three ways -- compaction_end on the wire, the "
            "outcome on stderr, or the cancellation line on stderr. This run "
            f"produced {sum(reports)} of them "
            f"(delivered={bool(delivered)}, undeliverable={undeliverable}, "
            f"reaped={reaped}); stderr was:\n{err}"
        )
        if delivered:
            # The strong branch, and the one the fix is FOR: the compaction
            # ran to completion inside the grace window and the host was told
            # over the wire, correlated to the acknowledgement it already has.
            assert len(delivered) == 1
            assert delivered[0]["compaction_id"] == compaction_id
            assert delivered[0]["request_id"] == compact_id
            assert delivered[0]["is_error"] is False
        else:
            # The weak branch (a machine slow enough to overrun the 1.0s
            # grace): nothing was written, and stderr says so, naming which
            # compaction it was.
            assert reaped and compaction_id in err
    finally:
        fake_state.gate = None
        gate.set()  # never leave a server thread parked
        await _shutdown(proc)
