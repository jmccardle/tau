"""A client of ``tau serve``: requests, pushed events, and a replica of each attached tree.

Used by ``tau serve --tail`` and by ``tau --connect``'s backend. One
:class:`ServeClient` holds one WebSocket; :meth:`ServeClient.request` sends a
request and awaits its :class:`~tau_coding_agent.serve.protocol.Response`, and
every :class:`~tau_coding_agent.serve.protocol.Event` goes to the handler given
at connect, after the client has applied it to its :class:`Replica`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect, unix_connect

from tau_coding_agent.serve import protocol as p

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True)
class Address:
    """Where a daemon listens: TCP ``host:port``, or a unix socket ``path``."""

    host: str | None
    port: int | None
    path: str | None = None

    @property
    def url(self) -> str:
        """The WebSocket URL a client dials (a unix socket's URL names no host)."""
        if self.path is not None:
            return "ws://localhost/"
        return f"ws://{self.host}:{self.port}/"

    def __str__(self) -> str:
        return f"unix:{self.path}" if self.path is not None else f"{self.host}:{self.port}"


def parse_address(text: str, *, default_host: str = "127.0.0.1") -> Address:
    """Read ``HOST``, ``HOST:PORT``, ``:PORT``, ``ws://HOST:PORT`` or ``unix:/PATH``.

    Raises:
        ValueError: an empty host part with no default, or a port that is not a number.
    """
    if text.startswith("unix:"):
        if not hasattr(asyncio, "open_unix_connection"):
            raise ValueError("unix sockets are not available on this platform")
        return Address(None, None, os.path.expanduser(text[len("unix:") :]))
    for scheme in ("ws://", "http://"):
        if text.startswith(scheme):
            text = text[len(scheme) :]
    text = text.rstrip("/")
    host, sep, port = text.rpartition(":")
    if not sep:
        host, port = text, ""
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if port and not port.isdigit():
        raise ValueError(f"port {port!r} is not a number")
    return Address(host or default_host, int(port) if port else p.DEFAULT_PORT)


@dataclass
class Replica:
    """One attached session as the daemon's log holds it, kept current by entry events.

    Attributes:
        entries: Every entry in append order; a finalize replaces in place.
        cursors: Every live cursor, by id, as :class:`~protocol.CursorState` dicts.
        epoch: The daemon's epoch for this session; a change means re-snapshot.
        seq: The newest event applied.
        surface: The :class:`~protocol.Surface` dict as last read.
    """

    session_id: str
    epoch: str
    seq: int
    entries: list[dict[str, Any]]
    cursors: dict[str, dict[str, Any]]
    head_cursor_id: str
    cwd: str
    models: list[str]
    surface: dict[str, Any]
    _index: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_attached(cls, attached: dict[str, Any]) -> Replica:
        """A replica built from an :class:`~protocol.Attached` snapshot."""
        replica = cls(
            session_id=attached["session_id"],
            epoch=attached["epoch"],
            seq=attached["seq"],
            entries=list(attached["entries"] or []),
            cursors={c["cursor_id"]: c for c in attached["cursors"]},
            head_cursor_id=attached["head_cursor_id"],
            cwd=attached["cwd"],
            models=list(attached["models"]),
            surface=dict(attached["surface"]),
        )
        replica._index = {e["id"]: i for i, e in enumerate(replica.entries)}
        return replica

    def apply(self, event: dict[str, Any]) -> None:
        """Fold one event in: an entry write, or the cursor set.

        Raises:
            ValueError: the event skips a number; the client must reattach.
        """
        if event["seq"] != self.seq + 1:
            raise ValueError(f"event {event['seq']} after {self.seq}: a gap, reattach")
        self.seq = event["seq"]
        kind = event["kind"]
        if kind in ("entry_open", "entry_append", "entry_final"):
            entry = event["data"]["entry"]
            position = self._index.get(entry["id"])
            if position is None:
                self._index[entry["id"]] = len(self.entries)
                self.entries.append(entry)
            else:
                self.entries[position] = entry
        elif kind == "cursors":
            self.cursors = {c["cursor_id"]: c for c in event["data"]["cursors"]}


class ServeError(Exception):
    """A request the daemon refused; ``code`` is the protocol's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class ServeClient:
    """One connection to a daemon, after its hello.

    Construct with :meth:`connect`. Events for attached sessions are applied to
    :attr:`replicas` before the handler sees them, so a handler reads a replica
    that already includes the event.
    """

    def __init__(self, ws: Any, on_event: EventHandler | None) -> None:
        self._ws = ws
        self._on_event = on_event
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._attaching: set[int] = set()
        self.replicas: dict[str, Replica] = {}
        self._reader: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()
        self.close_reason: str | None = None

    @classmethod
    async def connect(
        cls,
        address: Address,
        *,
        client: str,
        on_event: EventHandler | None = None,
        token: str | None = None,
    ) -> ServeClient:
        """Dial ``address``, say hello, and start reading.

        ``token`` defaults to ``TAUD_TOKEN`` from the environment.

        Raises:
            ServeError: the daemon refused the hello (protocol or token).
            OSError: nothing answered at ``address``.
        """
        if address.path is not None:
            ws = await unix_connect(address.path, uri=address.url, max_size=None)
        else:
            ws = await connect(address.url, max_size=None)
        self = cls(ws, on_event)
        self._reader = asyncio.create_task(self._read())
        await self.request(
            p.Hello(
                protocol=p.PROTOCOL_VERSION,
                client=client,
                token=token if token is not None else os.environ.get("TAUD_TOKEN"),
            )
        )
        return self

    async def request(self, message: Any) -> Any:
        """Send a request dataclass and return its result.

        Raises:
            ServeError: the daemon answered with an error.
            ConnectionError: the connection closed before the answer.
        """
        if self.closed.is_set():
            raise ConnectionError(f"connection closed: {self.close_reason}")
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        if isinstance(message, p.Attach):
            self._attaching.add(request_id)
        await self._ws.send(json.dumps({"id": request_id, **p.to_wire(message)}))
        return await future

    async def attach(self, session_id: str) -> Replica:
        """Attach to a session, resuming from a replica already held when the daemon allows."""
        held = self.replicas.get(session_id)
        attached = await self.request(
            p.Attach(
                session_id=session_id,
                epoch=held.epoch if held is not None else None,
                since=held.seq if held is not None else None,
            )
        )
        return self.replicas[attached["session_id"]]

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                frame = json.loads(raw)
                if frame.get("type") == "response":
                    self._resolve(frame)
                elif frame.get("type") == "event":
                    replica = self.replicas.get(frame["session_id"])
                    if replica is not None and frame["epoch"] == replica.epoch:
                        if frame["seq"] <= replica.seq:
                            continue
                        replica.apply(frame)
                    if self._on_event is not None:
                        result = self._on_event(frame)
                        if asyncio.iscoroutine(result):
                            await result
        except Exception as exc:
            self.close_reason = f"{type(exc).__name__}: {exc}"
        finally:
            if self.close_reason is None:
                rcvd = getattr(self._ws, "close_rcvd", None)
                self.close_reason = (
                    f"{rcvd.code} {rcvd.reason}".strip() if rcvd is not None else "closed"
                )
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(f"connection closed: {self.close_reason}"))
            self._pending.clear()

    def _resolve(self, frame: dict[str, Any]) -> None:
        future = self._pending.pop(frame["id"], None)
        attaching = frame["id"] in self._attaching
        self._attaching.discard(frame["id"])
        if future is None or future.done():
            return
        if frame["ok"] and attaching:
            # Before the next frame is read: the events after this answer belong to it.
            attached = frame["result"]
            if attached["entries"] is not None:
                self.replicas[attached["session_id"]] = Replica.from_attached(attached)
        if frame["ok"]:
            future.set_result(frame.get("result"))
        else:
            error = frame.get("error") or {}
            future.set_exception(ServeError(error.get("code", "failed"), error.get("message", "")))

    async def close(self) -> None:
        """Close the connection and wait for the reader to finish."""
        await self._ws.close()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)
