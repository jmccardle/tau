"""τ logs a rejected handshake itself; a connection that sends nothing leaves no trace."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import InvalidHandshake

from tau_coding_agent.serve.http import build_connection_class, build_process_request

NO_KEY = (
    b"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
    b"Sec-WebSocket-Version: 13\r\n\r\n"
)


async def _connect_and(
    send: bytes, caplog, *, half_close: bool = False, process_request=None
) -> tuple[list[str], str, list[logging.LogRecord]]:
    """Send ``send`` raw to a real websockets server with τ's hooks, read its answer, close.

    Args:
        send: The raw bytes; empty for a preconnect.
        half_close: Send EOF before reading, for a request cut off mid-way.
        process_request: Overrides τ's ``process_request`` hook.

    Returns:
        τ's log lines, the client's ``host:port``, and websockets' WARNING+ records.
    """

    async def _handler(ws) -> None:
        await ws.close()

    lines: list[str] = []
    hook = process_request or build_process_request({}, lines.append)
    with caplog.at_level(logging.WARNING, logger="websockets"):
        async with serve(
            _handler,
            "127.0.0.1",
            0,
            process_request=hook,
            create_connection=build_connection_class(lines.append),
        ) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            host, client_port = writer.get_extra_info("sockname")[:2]
            if send:
                writer.write(send)
                await writer.drain()
                if half_close:
                    writer.write_eof()
                await asyncio.wait_for(reader.read(), 5)
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.2)
    records = [r for r in caplog.records if r.name.startswith("websockets")]
    return lines, f"{host}:{client_port}", records


def _assert_one_rejection(lines: list[str], peer: str) -> str:
    assert len(lines) == 1, lines
    prefix = f"handshake rejected from {peer}: "
    assert lines[0].startswith(prefix)
    reason = lines[0][len(prefix) :]
    assert reason.strip()
    return reason


async def test_a_preconnect_that_sends_nothing_is_not_logged(caplog):
    lines, _, records = await _connect_and(b"", caplog)
    assert lines == []
    assert records == []


async def test_a_plain_get_is_answered_by_process_request_and_is_not_a_rejection(caplog):
    lines, _, records = await _connect_and(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n", caplog)
    assert lines == []
    assert records == []


@pytest.mark.parametrize(
    "send", [NO_KEY, b"not http at all\r\n\r\n"], ids=["no-key-upgrade", "unparseable"]
)
async def test_a_rejected_handshake_is_one_tau_line_naming_peer_and_reason(caplog, send):
    lines, peer, records = await _connect_and(send, caplog)
    _assert_one_rejection(lines, peer)
    assert records == []


@pytest.mark.parametrize(
    "send", [b"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websock", b"GET / HT"], ids=["headers", "line"]
)
async def test_a_request_cut_off_mid_way_is_a_rejection_not_a_preconnect(caplog, send):
    lines, peer, records = await _connect_and(send, caplog, half_close=True)
    reason = _assert_one_rejection(lines, peer)
    assert "closed" in reason
    assert records == []


async def test_a_process_request_bug_reaches_websockets_error_log_not_tau(caplog):
    bug = RuntimeError("process_request bug")

    def _raising(connection, request):
        raise bug

    lines, _, records = await _connect_and(NO_KEY, caplog, process_request=_raising)
    assert lines == []
    errors = [r for r in records if r.levelno >= logging.ERROR]
    assert [r.exc_info[1] for r in errors if r.exc_info] == [bug]


async def test_an_invalid_handshake_that_is_not_the_protocols_own_is_raised(monkeypatch):
    stray = InvalidHandshake("stray")

    async def _raise(self, *args, **kwargs):
        raise stray

    monkeypatch.setattr(ServerConnection, "handshake", _raise)
    lines: list[str] = []
    cls = build_connection_class(lines.append)
    connection = cls.__new__(cls)
    connection.protocol = SimpleNamespace(handshake_exc=None)
    with pytest.raises(InvalidHandshake) as raised:
        await connection.handshake()
    assert raised.value is stray
    assert lines == []
