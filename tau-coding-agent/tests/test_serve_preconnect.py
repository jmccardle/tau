"""τ logs a rejected handshake itself; a connection that sends nothing leaves no trace."""

from __future__ import annotations

import asyncio
import logging

import pytest
from websockets.asyncio.server import serve

from tau_coding_agent.serve.http import build_connection_class


async def _connect_and(send: bytes, caplog) -> tuple[list[str], str, list[logging.LogRecord]]:
    """Send ``send`` raw to a real websockets server, read its answer, and close.

    Returns:
        τ's log lines, the client's ``host:port``, and websockets' WARNING+ records.
    """

    async def _handler(ws) -> None:
        await ws.close()

    lines: list[str] = []
    with caplog.at_level(logging.WARNING, logger="websockets"):
        async with serve(
            _handler, "127.0.0.1", 0, create_connection=build_connection_class(lines.append)
        ) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            host, client_port = writer.get_extra_info("sockname")[:2]
            if send:
                writer.write(send)
                await writer.drain()
                await asyncio.wait_for(reader.read(), 5)
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.2)
    records = [r for r in caplog.records if r.name.startswith("websockets")]
    return lines, f"{host}:{client_port}", records


async def test_a_preconnect_that_sends_nothing_is_not_logged(caplog):
    lines, _, records = await _connect_and(b"", caplog)
    assert lines == []
    assert records == []


@pytest.mark.parametrize(
    "send",
    [b"GET / HTTP/1.1\r\nHost: x\r\n\r\n", b"not http at all\r\n\r\n"],
    ids=["no-upgrade", "unparseable"],
)
async def test_a_rejected_handshake_is_one_tau_line_naming_peer_and_reason(caplog, send):
    lines, peer, records = await _connect_and(send, caplog)
    assert len(lines) == 1
    prefix = f"handshake rejected from {peer}: "
    assert lines[0].startswith(prefix)
    assert lines[0][len(prefix) :].strip()
    assert records == []
