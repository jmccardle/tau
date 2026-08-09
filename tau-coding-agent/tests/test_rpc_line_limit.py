"""T7 — an over-long request line is refused, not fatal, against a REAL child.

Tier B review, finding 9. ``transport._read_stdin`` built a bare
``asyncio.StreamReader()`` and inherited the stdlib's 64 KiB line limit, which
``readline()`` enforces by raising ``ValueError: Separator is found, but chunk
is longer than limit``. Nothing caught it. Reproduced against a real child
before the fix::

    get_state ok -> {'jsonrpc': '2.0', 'id': 1, 'result': {…}}
    100KB prompt -> NO RESPONSE: stdout closed
    rc: 1

One oversized ``prompt`` — a pasted file, a stack trace, a diff, the single
most obvious thing a host sends at size — and the process was gone: no
JSON-RPC error, no T4 stderr line, exit 1, and every later request on that
connection lost with it.

**Why this file exists separately from the unit tests.**
``tau-agent-core/tests/test_rpc_transport.py::TestRequestLineBound`` drives the
mechanism exhaustively (the boundary, the resync, one error per line, the
error shape) with the bound monkeypatched down to a few hundred bytes. What it
cannot show is the part that actually failed: a real ``tau --mode rpc``
process, with a real pipe and a real ``handler.run()`` above the reader,
STAYING ALIVE and answering the next request. That needs a child, at the
shipped bound, and the sizes here are read from the child's own
``get_capabilities`` rather than hardcoded — so the number a host is told and
the number it is held to are proven to be the same number.

**And its sibling defect, at the bottom of the file.** Finding 9 also
*measured* — and deliberately did not fix, being mid-round in a shared tree —
an identical availability failure one framing rule over: a request line that is
not valid UTF-8 killed the child the same way, out of the same reader, with the
same silence. It is fixed and pinned here rather than in a file of its own,
because it is the same question ("what does the framing layer do with bytes it
will not accept?") answered against the same child by the same scaffolding; the
file keeps its name so nobody has to re-find T7's tests.

**No LLM and no HTTP server.** Nothing here runs a turn. The oversized line is
refused at the framing layer, before anything looks at ``method``, so the
child needs a model config that RESOLVES, not one that answers (the
``127.0.0.1:1`` trick ``test_rpc_session_dir_isolation.py`` and
``test_store_factory.py`` already use). ``$HOME`` and ``$TMPDIR`` are both
redirected, per unit S — a child spawned without ``$TMPDIR`` leaves a real
session in the developer's own ``/tmp/.tau``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tau_agent_core.rpc import dialect

# Port 1 is always connection-refused: a model entry that resolves without
# anything ever answering it.
UNREACHABLE_BASE_URL = "http://127.0.0.1:1/v1"

#: Comfortably past the stdlib `StreamReader`'s 64 KiB default (which is what
#: used to kill the child), comfortably under the shipped bound, and small
#: enough to push through a pipe without the test noticing.
_PAST_THE_OLD_DEFAULT_BYTES = 1024 * 1024


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    tau_dir = home / ".tau"
    tau_dir.mkdir(parents=True)
    (tau_dir / "config.json").write_text(
        json.dumps(
            {
                "models": {
                    "fake": {
                        "backend": "openai",
                        "model": "fake-model",
                        "base_url": UNREACHABLE_BASE_URL,
                        "api_key": "x",
                        "tools": [],
                    }
                },
                "default_model": "fake",
            }
        )
    )
    (home / "tmp").mkdir()
    return home


async def _spawn(fake_home: Path) -> asyncio.subprocess.Process:
    """``python -m tau_coding_agent.cli --mode rpc`` with its own ``$HOME``
    and ``$TMPDIR`` — the same entry point and the same isolation
    ``test_rpc_conformance.py`` uses, so this file's children behave like
    every other child in the suite."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tau_coding_agent.cli",
        "--mode",
        "rpc",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "HOME": str(fake_home), "TMPDIR": str(fake_home / "tmp")},
    )


async def _send(proc: asyncio.subprocess.Process, obj: dict[str, Any]) -> None:
    await _send_raw(proc, (json.dumps(obj) + "\n").encode("utf-8"))


async def _send_raw(proc: asyncio.subprocess.Process, raw: bytes) -> None:
    """Write bytes to the child's stdin, bounded.

    ``drain()`` is awaited under a timeout on purpose: before the fix, a child
    that died mid-line left this parent blocked on a pipe nobody was reading
    from any more, and the failure showed up as a hung test rather than as the
    defect it is. A dead child must fail this suite in seconds, with a message
    that names the write that killed it.
    """
    assert proc.stdin is not None
    proc.stdin.write(raw)
    try:
        await asyncio.wait_for(proc.stdin.drain(), timeout=60.0)
    except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError) as exc:
        raise AssertionError(
            f"the child stopped reading stdin after {len(raw)} bytes ({exc!r}) — "
            "T7's whole point is that an over-long line does not do this"
        ) from exc


def _stdout_buffer(proc: asyncio.subprocess.Process) -> bytearray:
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
        if not chunk:
            line = bytes(buf)
            buf.clear()
            return line
        buf += chunk


async def _read_line(proc: asyncio.subprocess.Process, timeout: float) -> bytes:
    """One LF-terminated line off stdout, with NO length cap.

    ``StreamReader.readline()`` raises past 64 KiB, and every test below
    reads a ``get_capabilities`` response, which is larger than that — see
    ``test_rpc_conformance.py::_read_line`` for the measurement and for why
    a bigger ``limit=`` at spawn is the wrong lever. It is a nice
    demonstration of T7's own lesson landing on the other side of the wire:
    a host that lets somebody else's `readline` decide how long a line may
    be has a fatal input class it did not choose.
    """
    return await asyncio.wait_for(_read_one_line(proc), timeout=timeout)


async def _recv(proc: asyncio.subprocess.Process, timeout: float = 30.0) -> dict[str, Any]:
    assert proc.stdout is not None
    raw = await _read_line(proc, timeout=timeout)
    if not raw:
        assert proc.stderr is not None
        err = (await proc.stderr.read()).decode("utf-8", errors="replace")
        raise AssertionError(f"the child's stdout closed before it answered; stderr:\n{err}")
    return json.loads(raw.decode("utf-8"))


async def _recv_response(
    proc: asyncio.subprocess.Process, expected_id: Any, timeout: float = 30.0
) -> dict[str, Any]:
    item = await _recv(proc, timeout=timeout)
    assert item.get("id") == expected_id, f"expected a response with id={expected_id!r}, got {item}"
    return item


async def _capabilities(proc: asyncio.subprocess.Process, msg_id: int) -> dict[str, Any]:
    await _send(proc, {"jsonrpc": "2.0", "id": msg_id, "method": "get_capabilities"})
    return (await _recv_response(proc, msg_id))["result"]


def _oversized_prompt_line(limit_bytes: int) -> bytes:
    """One `prompt` request, one byte over the child's advertised bound.

    Real JSON, and the verb a host actually sends at size — the refusal must
    not depend on the line being garbage. One byte over rather than wildly
    over, so a bound that is off by a chunk boundary cannot pass this by
    accident.
    """
    skeleton = '{"jsonrpc":"2.0","id":99,"method":"prompt","params":{"text":""}}'
    padding = "x" * (limit_bytes + 1 - len(skeleton))
    line = skeleton.replace('"text":""', '"text":"' + padding + '"')
    assert len(line) == limit_bytes + 1
    return line.encode("ascii") + b"\n"


async def _shutdown(proc: asyncio.subprocess.Process) -> int:
    """Close stdin (EOF is the clean shutdown trigger, T1/P4) and wait for the
    exit code, draining stdout so nothing blocks on a full pipe."""
    assert proc.stdin is not None and proc.stdout is not None
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.close()

    async def _drain() -> None:
        with contextlib.suppress(asyncio.CancelledError, ValueError):
            # Chunks, not lines: a drain that can raise on a long response is
            # a drain that silently stops (see `_read_line`).
            while await proc.stdout.read(64 * 1024):  # type: ignore[union-attr]
                pass

    drainer = asyncio.create_task(_drain())
    try:
        return await asyncio.wait_for(proc.wait(), timeout=30.0)
    except asyncio.TimeoutError:  # pragma: no cover - a wedged child fails loudly
        proc.kill()
        await proc.wait()
        raise AssertionError("the child did not exit within 30s of stdin EOF")
    finally:
        drainer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drainer


async def test_an_oversized_prompt_is_refused_and_the_child_keeps_serving(fake_home: Path):
    """THE PROOF, and the reproduction line for line: a working `get_state`,
    then one oversized `prompt`, then the SAME `get_state` again.

    Before the fix the second `get_state` had nothing to answer it — the
    process was already gone with `rc: 1`. Now the oversized line earns a
    `REQUEST_TOO_LARGE` and nothing else changes: same process, same session,
    next request served, clean exit 0 at EOF.

    The size is derived from the child's OWN advertised limit rather than
    hardcoded, so this also pins the advertisement to the enforcement — a
    document promising one number while the reader applies another is worse
    than no document at all.
    """
    proc = await _spawn(fake_home)
    try:
        caps = await _capabilities(proc, 1)
        limit = caps["limits"]["max_request_line_bytes"]
        assert isinstance(limit, int) and limit > 64 * 1024, (
            "the advertised bound is at or below the stdlib default this "
            f"finding is about: {limit}"
        )

        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "get_state"})
        before = await _recv_response(proc, 2)
        assert "session_id" in before["result"]

        await _send_raw(proc, _oversized_prompt_line(limit))
        refusal = await _recv(proc)
        assert refusal["error"]["code"] == dialect.REQUEST_TOO_LARGE
        assert refusal["id"] is None, "the request's id was inside the bytes never parsed"
        assert refusal["error"]["data"]["max_request_line_bytes"] == limit
        assert refusal["error"]["data"]["observed_bytes"] == limit + 1
        assert refusal["error"]["data"]["line_complete"] is True

        # ...and the connection is untouched: same child, same session.
        await _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "get_state"})
        after = await _recv_response(proc, 3)
        assert after["result"]["session_id"] == before["result"]["session_id"]

        assert await _shutdown(proc) == 0, "a refused line must not change the exit code"
        assert proc.stderr is not None
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
        assert "Request line refused" in stderr, (
            "T4: the refusal must also reach the documented log channel, so an "
            f"operator can see it without holding the protocol stream. stderr:\n{stderr}"
        )
    finally:
        if proc.returncode is None:  # pragma: no cover - only on an assertion above
            proc.kill()
            await proc.wait()


async def test_a_megabyte_line_is_read_and_dispatched(fake_home: Path):
    """The other half of the fix: the bound was RAISED, not merely enforced.

    A 1 MiB request line is sixteen times the old ceiling and is framed,
    parsed and dispatched normally. The assertion is that the child ANSWERED
    THIS id — not that the request succeeded: the padding rides in an
    unexpected `params` member, so the answer is a schema violation
    (`INVALID_PARAMS`), which is exactly the proof wanted here. A verb that
    reports what is wrong with a 1 MiB request is a verb that read all 1 MiB
    of it.
    """
    proc = await _spawn(fake_home)
    try:
        skeleton = '{"jsonrpc":"2.0","id":7,"method":"get_state","params":{"pad":""}}'
        padding = "x" * (_PAST_THE_OLD_DEFAULT_BYTES - len(skeleton))
        line = skeleton.replace('"pad":""', '"pad":"' + padding + '"')
        assert len(line) == _PAST_THE_OLD_DEFAULT_BYTES > 64 * 1024

        await _send_raw(proc, line.encode("ascii") + b"\n")
        answer = await _recv_response(proc, 7)
        assert answer["error"]["code"] != dialect.REQUEST_TOO_LARGE, (
            "a 1 MiB line is well under the shipped bound and must not be refused"
        )
        assert answer["error"]["code"] == dialect.INVALID_PARAMS

        assert await _shutdown(proc) == 0
    finally:
        if proc.returncode is None:  # pragma: no cover - only on an assertion above
            proc.kill()
            await proc.wait()


async def test_a_hostile_endless_line_is_refused_once_and_the_child_survives(fake_home: Path):
    """The deliberately hostile peer: bytes forever, no LF, ever.

    Three properties, all of them things a serial reader can get wrong:

    1. The refusal arrives WHILE the peer is still writing, not when it
       finally stops — the reader never assembles more than the bound, so
       this is what "flat memory" looks like from the outside.
    2. Megabytes more earn no further errors. One refusal per line, however
       long the line turns out to be.
    3. The first LF resynchronizes: the next request is served normally, on
       the same connection, and the child still exits 0 at EOF.

    This is the case the reader must not be wedged or unbounded by
    (docs/REMOTE-CONTROL.md §4[1]: the reader is the single serial
    chokepoint the whole design is built around).
    """
    proc = await _spawn(fake_home)
    try:
        limit = (await _capabilities(proc, 1))["limits"]["max_request_line_bytes"]

        await _send_raw(proc, b"Z" * (limit + 1))  # no LF, and none coming
        refusal = await _recv(proc)
        assert refusal["error"]["code"] == dialect.REQUEST_TOO_LARGE
        assert refusal["error"]["data"]["line_complete"] is False, (
            "the line has not ended, so its length is a lower bound, and the "
            "wire must say so rather than imply an exact count"
        )
        assert refusal["error"]["data"]["observed_bytes"] > limit

        for _ in range(4):
            await _send_raw(proc, b"Z" * (limit // 4))

        # The first LF ends the refused line; what follows it is a request
        # like any other. If a second refusal had been queued for the same
        # line, THIS read would find it instead of the response.
        request = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "get_state"}).encode()
        await _send_raw(proc, b"\n" + request + b"\n")
        answer = await _recv_response(proc, 2)
        assert "session_id" in answer["result"]

        assert await _shutdown(proc) == 0
    finally:
        if proc.returncode is None:  # pragma: no cover - only on an assertion above
            proc.kill()
            await proc.wait()


# ── finding 9's blockers #4: the same defect, one framing rule over ──────────


async def test_an_undecodable_line_is_refused_and_the_child_keeps_serving(fake_home: Path):
    """The measured reproduction, verbatim, against a real child::

        printf '\\xff\\xfe\\n{"jsonrpc":"2.0","id":1,"method":"get_state"}\\n' | tau --mode rpc
        rc: 1 ; stdout: b'' ; UnicodeDecodeError: 'utf-8' codec can't decode
        byte 0xff in position 0: invalid start byte

    Two bytes, and there was no process left to answer the well-formed request
    behind them. Same three properties T7's own proof asserts, for the same
    reason: a `PARSE_ERROR` on the wire, the SAME session still answering
    afterwards (not a fresh child), and exit 0 at EOF.

    The corrupt line is sent AHEAD of the good one in a single write, so the
    good request is genuinely behind it in the child's buffer — this is the
    "and everything queued behind it was lost too" half of the defect, which a
    test that sent them in two round trips would not exercise.
    """
    proc = await _spawn(fake_home)
    try:
        await _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "get_state"})
        before = await _recv_response(proc, 1)

        request = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "get_state"}).encode()
        await _send_raw(proc, b"\xff\xfe\n" + request + b"\n")

        refusal = await _recv(proc)
        assert refusal["error"]["code"] == dialect.PARSE_ERROR
        assert refusal["id"] is None, "the id was inside the bytes that could not be decoded"
        assert refusal["error"]["data"]["encoding"] == "utf-8"
        assert refusal["error"]["data"]["byte_offset"] == 0

        after = await _recv_response(proc, 2)
        assert after["result"]["session_id"] == before["result"]["session_id"], (
            "the request queued behind the corrupt line must be served by the "
            "same process on the same session"
        )

        assert await _shutdown(proc) == 0, "a refused line must not change the exit code"
        assert proc.stderr is not None
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
        assert "not valid UTF-8" in stderr, f"T4: stderr must say why. stderr:\n{stderr}"
    finally:
        if proc.returncode is None:  # pragma: no cover - only on an assertion above
            proc.kill()
            await proc.wait()
