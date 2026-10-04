"""A connection that closes before sending a request leaves nothing in serve.log."""

from __future__ import annotations

import asyncio
import logging

from websockets.asyncio.server import serve

from tau_coding_agent.serve.cli import DroppedBeforeRequest


async def _connect_and(send: bytes, caplog) -> list[logging.LogRecord]:
    """Open a raw connection to a real websockets server, send ``send``, close it."""

    async def _handler(ws) -> None:
        await ws.close()

    logger = logging.getLogger("websockets.server")
    flt = DroppedBeforeRequest()
    logger.addFilter(flt)
    try:
        with caplog.at_level(logging.ERROR, logger="websockets.server"):
            async with serve(_handler, "127.0.0.1", 0) as server:
                port = next(iter(server.sockets)).getsockname()[1]
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                if send:
                    writer.write(send)
                    await writer.drain()
                writer.close()
                await writer.wait_closed()
                await asyncio.sleep(0.2)
    finally:
        logger.removeFilter(flt)
    return [r for r in caplog.records if r.getMessage() == "opening handshake failed"]


async def test_a_preconnect_that_sends_nothing_is_not_logged(caplog):
    assert await _connect_and(b"", caplog) == []


async def test_a_malformed_request_is_still_logged(caplog):
    records = await _connect_and(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n", caplog)
    assert len(records) == 1
