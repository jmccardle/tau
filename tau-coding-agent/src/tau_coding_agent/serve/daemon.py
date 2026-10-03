"""``tau serve``: one process that owns sessions and serves them to clients (docs/TAU-SERVE.md §6).

The daemon is the single writer (§3). It loads a session the first time a client
attaches to it, keeps running its turns whether or not anyone is attached, and
pushes every log write and bus event to the clients that are.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any, TextIO, cast

from tau_agent_core.cursor import TURN_CURSOR, Cursor, TurnFrame
from tau_agent_core.extension_types import form_headless_value, validate_form_spec
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog
from tau_agent_core.session_log import SessionLog, is_incomplete
from tau_agent_core.submission import Submission

from tau_coding_agent.serve import protocol as p

QUEUE_BOUND = 20_000
"""Frames a client may fall behind by before the daemon drops it (§5, backpressure)."""

REPLAY_BOUND = 50_000
"""Events a session keeps for a reconnecting client; older resumes get a snapshot."""

SLOW_CLIENT_CLOSE = 4000
"""The WebSocket close code for a dropped slow client; it reconnects with ``since``."""


class RequestError(Exception):
    """A request that fails with a protocol :class:`~tau_coding_agent.serve.protocol.Error`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def watch_writes(log: SessionLog, listener: Callable[[str, dict[str, Any]], None]) -> None:
    """Call ``listener(kind, entry)`` after every write to ``log``.

    ``kind`` is ``entry_open``, ``entry_append`` or ``entry_final``. The instance's
    own ``append_at`` and ``finalize`` are replaced by wrappers, so every writer
    is seen whichever path it took, and the store's class is unchanged for the
    ``isinstance`` checks forking makes.
    """
    append_at = log.append_at
    finalize = log.finalize

    def _entry(entry_id: str) -> dict[str, Any]:
        for entry in reversed(log.entries()):
            if entry["id"] == entry_id:
                return entry
        raise RuntimeError(f"watch_writes: entry {entry_id!r} vanished after its write")

    async def watched_append_at(
        parent_id: str | None, entry_type: str, payload: dict[str, Any]
    ) -> str:
        entry_id = await append_at(parent_id, entry_type, payload)
        entry = _entry(entry_id)
        listener("entry_open" if is_incomplete(entry) else "entry_append", entry)
        return entry_id

    async def watched_finalize(entry_id: str, payload: dict[str, Any]) -> None:
        await finalize(entry_id, payload)
        listener("entry_final", _entry(entry_id))

    log.append_at = watched_append_at  # type: ignore[method-assign]
    log.finalize = watched_finalize  # type: ignore[method-assign]


def dispatched_to_wire(command: Any) -> dict[str, Any] | None:
    """A dispatched command arm as ``{"arm": name, ...fields}``, or ``None``."""
    if command is None:
        return None
    is_record = dataclasses.is_dataclass(command) and not isinstance(command, type)
    fields = dataclasses.asdict(command) if is_record else {}
    return {"arm": type(command).__name__, **json.loads(json.dumps(fields, default=str))}


class Client:
    """One WebSocket connection: an ordered outbound queue and the sessions it watches."""

    def __init__(self, ws: Any, number: int) -> None:
        self.ws = ws
        self.id = f"c{number}"
        self.name = "?"
        self.attached: set[str] = set()
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=QUEUE_BOUND)
        self.dropped = False

    def push(self, frame: dict[str, Any]) -> bool:
        """Queue ``frame``; ``False`` when the client is too far behind and is being dropped."""
        if self.dropped:
            return False
        try:
            self.queue.put_nowait(json.dumps(frame))
        except asyncio.QueueFull:
            self.dropped = True
            return False
        return True

    async def send_loop(self) -> None:
        """Write queued frames in order until the queue yields ``None`` or the socket closes."""
        while True:
            frame = await self.queue.get()
            if frame is None:
                return
            await self.ws.send(frame)


class ServeUI:
    """The extension UI delegate a served session uses (docs/TAU-SERVE.md §6.4).

    A form goes to every attached client and the first :class:`~protocol.Answer`
    wins. With no client attached, each field takes its declared default.
    """

    def __init__(self, host: SessionHost) -> None:
        self._host = host
        self._forms: dict[str, asyncio.Future[dict[str, Any] | None]] = {}

    async def form(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        title, fields = validate_form_spec(spec)
        if not self._host.clients:
            self._host.daemon.log(f"{self._host.tag} form {title!r}: no client, defaults taken")
            return {field["name"]: form_headless_value(field) for field in fields}
        request_id = uuid.uuid4().hex[:8]
        future: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        self._forms[request_id] = future
        self._host.publish("request", {"request_id": request_id, "spec": spec})
        try:
            return await future
        finally:
            del self._forms[request_id]
            self._host.publish("request_closed", {"request_id": request_id})

    def answer(self, request_id: str, value: dict[str, Any] | None) -> None:
        """Resolve a form; a second answer to the same form is refused."""
        future = self._forms.get(request_id)
        if future is None or future.done():
            raise RequestError("not_found", f"no open form {request_id!r}")
        future.set_result(value)

    def notify(self, message: str, level: str = "info") -> None:
        self._host.publish("ui", {"op": "notify", "message": message, "level": level})

    def set_status(self, key: str, text: str | None) -> None:
        self._host.publish("ui", {"op": "status", "key": key, "text": text})

    def panel(self, key: str, spec: dict[str, Any] | None) -> None:
        self._host.publish("ui", {"op": "panel", "key": key, "spec": spec})


class SessionHost:
    """One loaded session: its backend, its clients, and its numbered event history."""

    def __init__(self, daemon: Daemon, log: ConversationSession, backend: Any) -> None:
        self.daemon = daemon
        self.log = log
        self.backend = backend
        self.agent_session = backend.agent_session
        self.session_id = log.id
        self.tag = f"session {log.id[:8]}"
        self.epoch = uuid.uuid4().hex[:12]
        self.seq = 0
        self.history: collections.deque[dict[str, Any]] = collections.deque(maxlen=REPLAY_BOUND)
        self.clients: set[Client] = set()
        self.ui = ServeUI(self)
        self._cursors_seen: list[dict[str, Any]] = []
        self._tool_started: dict[str, float] = {}
        self._turn_tokens: dict[str, int] = {}
        self._open_entries: dict[str, str] = {}

    async def start(self, cwd: str) -> None:
        """Bind the session's cursor, wire every event source, then load extensions."""
        self.backend.bind_cursor(Cursor.newest(self.log))
        session = self.agent_session
        watch_writes(self.log, self._on_write)
        session.subscribe(self._on_agent_event)
        for channel in ("submission_start", "submission_end", "custom_message"):
            session.subscribe_channel(channel, self._channel(channel))
        session.subscribe_channel("cursor_open", self._on_cursor_open)
        session.subscribe_channel("cursor_close", self._on_cursor_close)
        session.set_ui_delegate(self.ui)
        result = await self.backend.load_extensions(
            None, discover=True, extensions_config=self.daemon.config.get("extensions_config")
        )
        for error in result.errors:
            self.daemon.log(f"{self.tag} extension {error.path} failed: {error.error}")
        await self.backend.emit_session_start("startup")
        await session.start()
        self.daemon.log(f"{self.tag} loaded: {cwd} (model {session.get_model()['id']})")
        for entry in self.log.entries():
            if is_incomplete(entry):
                self.daemon.log(f"{self.tag} {entry['type']} entry {entry['id']} left incomplete")

    async def close(self) -> None:
        """Fire ``session_shutdown`` for the session's extensions."""
        await self.backend.emit_session_shutdown("quit")

    def cursor(self, cursor_id: str) -> Cursor:
        """The live cursor ``cursor_id`` names.

        Raises:
            RequestError: no such cursor on this session.
        """
        for cursor in self.agent_session.cursors:
            if cursor.id == cursor_id:
                return cast(Cursor, cursor)
        raise RequestError("not_found", f"{self.tag} has no cursor {cursor_id!r}")

    def cursor_model(self, cursor: Cursor) -> str:
        """The model id ``cursor``'s next turn calls."""
        if cursor.frame is not None and cursor.frame.model is not None:
            return str(cursor.frame.model.id)
        return str(self.agent_session.get_model()["id"])

    def cursor_states(self) -> list[dict[str, Any]]:
        """Every live cursor as a :class:`~protocol.CursorState` dict, oldest first."""
        return [
            p.to_wire(
                p.CursorState(
                    cursor_id=c.id,
                    leaf=c.leaf,
                    label=c.label,
                    owner_id=c.owner.id if c.owner is not None else None,
                    busy=c.busy,
                    model=self.cursor_model(c),
                )
            )
            for c in self.agent_session.cursors
        ]

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        """Number an event, keep it for replay, and push it to every attached client."""
        self.seq += 1
        frame = p.to_wire(
            p.Event(
                session_id=self.session_id,
                epoch=self.epoch,
                seq=self.seq,
                kind=cast(Any, kind),
                data=data,
            )
        )
        self.history.append(frame)
        for client in list(self.clients):
            if not client.push(frame):
                self.daemon.drop(client)
        if kind != "cursors":
            states = self.cursor_states()
            if states != self._cursors_seen:
                self._cursors_seen = states
                self.publish("cursors", {"cursors": states})

    def attach(self, client: Client, epoch: str | None, since: int | None) -> dict[str, Any]:
        """Register ``client`` and return its :class:`~protocol.Attached`, queueing any replay.

        Synchronous on purpose: no event can be published between the snapshot
        (or replay) and the client joining, so it misses none and sees none twice.
        """
        oldest = self.history[0]["seq"] if self.history else self.seq + 1
        replay = epoch == self.epoch and since is not None and since + 1 >= oldest
        attached = p.Attached(
            session_id=self.session_id,
            epoch=self.epoch,
            seq=since if replay and since is not None else self.seq,
            entries=None if replay else self.log.entries(),
            cursors=[p.CursorState(**c) for c in self.cursor_states()],
            head_cursor_id=self.agent_session.cursor.id,
            cwd=str(self.log.header.get("cwd", "")),
            models=sorted(self.daemon.config.get("models", {})),
            commands=[list(c) for c in self.agent_session.get_extension_commands()],
        )
        self.clients.add(client)
        client.attached.add(self.session_id)
        self.daemon.log(f"{self.tag} client {client.id} ({client.name}) attached")
        return p.to_wire(attached)

    def replay(self, client: Client, since: int) -> None:
        """Queue every kept event after ``since`` for ``client``."""
        for frame in self.history:
            if frame["seq"] > since and not client.push(frame):
                self.daemon.drop(client)
                return

    def detach(self, client: Client) -> None:
        """Stop pushing to ``client``; the session runs on."""
        if client in self.clients:
            self.clients.discard(client)
            client.attached.discard(self.session_id)
            self.daemon.log(f"{self.tag} client {client.id} detached")

    def _on_write(self, kind: str, entry: dict[str, Any]) -> None:
        if kind == "entry_open":
            self._open_entries[entry["id"]] = entry.get("type", "?")
        elif kind == "entry_final":
            self._open_entries.pop(entry["id"], None)
        self.publish(kind, {"entry": entry})

    def _on_agent_event(self, event: Any) -> None:
        data = event.model_dump(mode="json")
        self.publish("agent_event", data)
        if event.type == "tool_execution_start" and event.tool_call_id:
            self._tool_started[event.tool_call_id] = time.monotonic()
        elif event.type == "tool_execution_end" and event.tool_call_id:
            started = self._tool_started.pop(event.tool_call_id, None)
            took = f" {time.monotonic() - started:.1f}s" if started is not None else ""
            failed = " (error)" if event.is_error else ""
            self.daemon.log(
                f"{self.tag} cursor {event.cursor_id} tool {event.tool_name}{took}{failed}"
            )
        elif event.type == "message_end" and event.submission_id and event.message:
            usage = event.message.get("usage") or {}
            total = usage.get("total_tokens") or 0
            self._turn_tokens[event.submission_id] = (
                self._turn_tokens.get(event.submission_id, 0) + total
            )

    def _channel(self, name: str) -> Callable[..., None]:
        def handler(**payload: Any) -> None:
            if name == "submission_start":
                submission = payload["submission"]
                cursor = payload.get("cursor")
                data = {
                    "submission": dataclasses.asdict(submission),
                    "text": payload.get("text", ""),
                    "images": payload.get("images"),
                    "cursor_id": cursor.id if cursor is not None else None,
                    "owner_id": cursor.owner.id
                    if cursor is not None and cursor.owner is not None
                    else None,
                }
                preview = " ".join(str(payload.get("text", "")).split())[:60]
                model = self.cursor_model(cursor) if cursor is not None else "?"
                self.daemon.log(
                    f"{self.tag} cursor {data['cursor_id']} turn started ({model}): {preview!r}"
                )
            elif name == "submission_end":
                submission = payload["submission"]
                data = {
                    "submission": dataclasses.asdict(submission),
                    "side_usage": payload.get("side_usage"),
                }
                tokens = self._turn_tokens.pop(submission.submission_id, 0)
                self.daemon.log(
                    f"{self.tag} turn {submission.submission_id[:8]} ended: {tokens} tokens"
                )
                for entry_id, kind in self._open_entries.items():
                    self.daemon.log(f"{self.tag} {kind} entry {entry_id} left incomplete")
                self._open_entries.clear()
            else:
                data = dict(payload)
            self.publish(
                "channel", {"name": name, "payload": json.loads(json.dumps(data, default=str))}
            )

        return handler

    def _on_cursor_open(self, *, cursor: Cursor) -> None:
        owner = f", owner {cursor.owner.id}" if cursor.owner is not None else ""
        self.daemon.log(
            f"{self.tag} cursor {cursor.id} opened at {cursor.leaf} ({cursor.label!r}{owner})"
        )
        self.publish("cursors", {"cursors": self.cursor_states()})
        self._cursors_seen = self.cursor_states()

    def _on_cursor_close(self, *, cursor: Cursor) -> None:
        self.daemon.log(f"{self.tag} cursor {cursor.id} closed")
        self.publish("cursors", {"cursors": self.cursor_states()})
        self._cursors_seen = self.cursor_states()


class Daemon:
    """The server: config, the session catalog, loaded sessions, and connected clients.

    Attributes:
        config: The daemon's own ``~/.tau/config.json``; a client's config never applies.
        token: The ``serve.token`` every hello must carry, or ``None`` for none.
    """

    def __init__(
        self,
        config: dict[str, Any],
        catalog: SessionCatalog,
        *,
        out: TextIO | None = None,
    ) -> None:
        self.config = config
        self.catalog = catalog
        serve_config = config.get("serve") or {}
        token = serve_config.get("token")
        self.token: str | None = str(token) if token else None
        self.hosts: dict[str, SessionHost] = {}
        self.clients: set[Client] = set()
        self._loading: dict[str, asyncio.Lock] = {}
        self._out = out if out is not None else sys.stdout
        self._numbers = 0

    def log(self, line: str) -> None:
        """One foreground log line, timestamped (docs/TAU-SERVE.md §6.1)."""
        stamp = time.strftime("%H:%M:%S")
        print(f"{stamp} {line}", file=self._out, flush=True)

    def drop(self, client: Client) -> None:
        """Disconnect a client that fell :data:`QUEUE_BOUND` frames behind; it resumes by ``since``."""
        if client not in self.clients:
            return
        self.log(f"client {client.id} ({client.name}) too slow, dropped")
        self._forget(client)
        asyncio.get_running_loop().create_task(
            client.ws.close(SLOW_CLIENT_CLOSE, "too slow; reconnect with since")
        )

    def _forget(self, client: Client) -> None:
        self.clients.discard(client)
        client.dropped = True
        for session_id in list(client.attached):
            host = self.hosts.get(session_id)
            if host is not None:
                host.detach(client)
        try:
            client.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def handle(self, ws: Any) -> None:
        """Serve one connection: hello, then requests until it closes."""
        self._numbers += 1
        client = Client(ws, self._numbers)
        sender = asyncio.create_task(client.send_loop())
        tasks: set[asyncio.Task[None]] = set()
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError as exc:
                    client.push(self._error(-1, "bad_request", f"not JSON: {exc}"))
                    continue
                try:
                    request_id, request = p.parse_request(frame if isinstance(frame, dict) else {})
                except ValueError as exc:
                    rid = frame.get("id", -1) if isinstance(frame, dict) else -1
                    client.push(
                        self._error(rid if isinstance(rid, int) else -1, "bad_request", str(exc))
                    )
                    continue
                if client not in self.clients:
                    if not isinstance(request, p.Hello):
                        client.push(self._error(request_id, "bad_request", "send hello first"))
                        continue
                    if not self._hello(client, request_id, request):
                        break
                    continue
                task = asyncio.create_task(self._serve(client, request_id, request))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            if client in self.clients:
                self.log(f"client {client.id} ({client.name}) closed")
            self._forget(client)
            for task in tasks:
                task.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    def _hello(self, client: Client, request_id: int, hello: p.Hello) -> bool:
        if hello.protocol != p.PROTOCOL_VERSION:
            client.push(
                self._error(
                    request_id,
                    "protocol_mismatch",
                    f"daemon speaks {p.PROTOCOL_VERSION}, client {hello.protocol}",
                )
            )
            return False
        if self.token is not None and hello.token != self.token:
            client.push(
                self._error(request_id, "unauthorized", "token missing or wrong; set TAUD_TOKEN")
            )
            self.log(f"client {client.id} ({hello.client}) refused: bad token")
            return False
        client.name = hello.client
        self.clients.add(client)
        self.log(f"client {client.id} ({client.name}) connected")
        client.push(self._ok(request_id, {"protocol": p.PROTOCOL_VERSION, "client_id": client.id}))
        return True

    @staticmethod
    def _ok(request_id: int, result: Any) -> dict[str, Any]:
        return p.to_wire(p.Response(id=request_id, ok=True, result=result))

    @staticmethod
    def _error(request_id: int, code: str, message: str) -> dict[str, Any]:
        return p.to_wire(
            p.Response(id=request_id, ok=False, error=p.Error(code=code, message=message))  # type: ignore[arg-type]
        )

    async def _serve(self, client: Client, request_id: int, request: Any) -> None:
        try:
            if isinstance(request, p.Attach):
                await self._attach(client, request_id, request)
                return
            result = await self._dispatch(client, request)
        except RequestError as exc:
            client.push(self._error(request_id, exc.code, str(exc)))
        except Exception as exc:
            self.log(f"client {client.id} request {type(request).__name__} failed: {exc!r}")
            client.push(self._error(request_id, "failed", f"{type(exc).__name__}: {exc}"))
        else:
            client.push(self._ok(request_id, result))

    async def _attach(self, client: Client, request_id: int, request: p.Attach) -> None:
        host = await self.load(request.session_id)
        attached = host.attach(client, request.epoch, request.since)
        client.push(self._ok(request_id, attached))
        if attached["entries"] is None and request.since is not None:
            host.replay(client, request.since)

    async def load(self, ref: str) -> SessionHost:
        """The host for session ``ref`` (an id or unambiguous prefix), loading it if needed.

        Raises:
            RequestError: no session matches ``ref``.
        """
        for session_id, host in self.hosts.items():
            if session_id == ref or session_id.startswith(ref):
                return host
        try:
            log = await asyncio.to_thread(self.catalog.resolve_ref, ref)
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise RequestError("not_found", str(exc)) from exc
        lock = self._loading.setdefault(log.id, asyncio.Lock())
        async with lock:
            if log.id in self.hosts:
                return self.hosts[log.id]
            host = await self._host_for(log, model=None)
            self.hosts[log.id] = host
            return host

    async def _host_for(self, log: ConversationSession, model: str | None) -> SessionHost:
        """Build a backend for ``log`` from the daemon's config, the way ``tau -p --session`` does."""
        from tau_coding_agent.backends import create_backend, make_model_resolver
        from tau_coding_agent.cli import CLIArgs
        from tau_coding_agent.headless import resolve_model_config

        cwd = str(log.header.get("cwd") or os.getcwd())
        if not os.path.isdir(cwd):
            raise RequestError("not_found", f"session cwd {cwd!r} does not exist on this machine")
        prior = log.config
        _, model_config = resolve_model_config(
            self.config,
            CLIArgs(model=model),
            fallback_model=prior.get("model"),
            prior_config=prior,
        )
        if self.config.get("system_prompt"):
            model_config["system_prompt"] = self.config["system_prompt"]
        model_config["cwd"] = cwd
        backend: Any = create_backend(model_config)
        backend.agent_session.set_model_resolver(make_model_resolver(self.config.get("models", {})))
        host = SessionHost(self, log, backend)
        await host.start(cwd)
        return host

    async def _dispatch(self, client: Client, request: Any) -> Any:
        if isinstance(request, p.ListSessions):
            return await self._list_sessions()
        if isinstance(request, p.CreateSession):
            return await self._create_session(request)
        if isinstance(request, p.Hello):
            raise RequestError("bad_request", "already said hello")
        host = await self.load(request.session_id)
        session = host.agent_session
        if isinstance(request, p.Detach):
            host.detach(client)
            return None
        if isinstance(request, p.Submit):
            return await self._submit(host, request)
        if isinstance(request, p.Abort):
            session.abort(host.cursor(request.cursor_id))
            return None
        if isinstance(request, p.OpenCursor):
            owner = host.cursor(request.owner_id) if request.owner_id else None
            try:
                cursor = await session.open_cursor(request.leaf, owner=owner, label=request.label)
            except ValueError as exc:
                raise RequestError("not_found", str(exc)) from exc
            return {"cursor_id": cursor.id}
        if isinstance(request, p.CloseCursor):
            try:
                await session.close_cursor(host.cursor(request.cursor_id))
            except RuntimeError as exc:
                raise RequestError("busy", str(exc)) from exc
            except ValueError as exc:
                raise RequestError("bad_request", str(exc)) from exc
            return None
        if isinstance(request, p.MoveCursor):
            cursor = host.cursor(request.cursor_id)
            if cursor.busy:
                raise RequestError("busy", f"cursor {cursor.id} is running a turn")
            try:
                cursor.move(request.leaf)
            except ValueError as exc:
                raise RequestError("not_found", str(exc)) from exc
            host.publish("cursors", {"cursors": host.cursor_states()})
            return None
        if isinstance(request, p.SetModel):
            return self._set_model(host, request)
        if isinstance(request, p.Answer):
            host.ui.answer(request.request_id, request.value)
            return None
        if isinstance(request, p.AnswerRequest):
            token = TURN_CURSOR.set(host.cursor(request.cursor_id))
            try:
                outcome = await session.answer_request(
                    request.request_id, request.action, request.values
                )
            except ValueError as exc:
                raise RequestError("bad_request", str(exc)) from exc
            finally:
                TURN_CURSOR.reset(token)
            return {"handled": outcome.handled, "output": outcome.output_text}
        if isinstance(request, p.Compare):
            return await self._compare(host, request)
        raise RequestError("bad_request", f"unhandled request {type(request).__name__}")

    async def _list_sessions(self) -> dict[str, Any]:
        infos = await asyncio.to_thread(self.catalog.list, None)
        rows = [
            p.to_wire(
                p.SessionRow(
                    id=info.id,
                    cwd=info.cwd,
                    name=info.name,
                    modified=info.modified.isoformat(),
                    message_count=info.message_count,
                    first_message=info.first_message,
                    loaded=info.id in self.hosts,
                )
            )
            for info in infos
            if info.error is None
        ]
        return {"sessions": rows}

    async def _create_session(self, request: p.CreateSession) -> dict[str, Any]:
        from tau_coding_agent.backends import create_backend
        from tau_coding_agent.cli import CLIArgs
        from tau_coding_agent.headless import resolve_model_config

        cwd = os.path.abspath(os.path.expanduser(request.cwd))
        if not os.path.isdir(cwd):
            raise RequestError("not_found", f"cwd {cwd!r} does not exist on the daemon's machine")
        model_name, model_config = resolve_model_config(self.config, CLIArgs(model=request.model))
        if self.config.get("system_prompt"):
            model_config["system_prompt"] = self.config["system_prompt"]
        model_config["cwd"] = cwd
        system_prompt = create_backend(model_config).system_prompt or None
        log = self.catalog.create(
            cwd,
            model_name,
            str(model_config.get("backend", "")),
            system_prompt=system_prompt,
            name=request.name,
        )
        self.log(f"session {log.id[:8]} created in {cwd}")
        return {"session_id": log.id}

    async def _submit(self, host: SessionHost, request: p.Submit) -> dict[str, Any]:
        cursor = host.cursor(request.cursor_id)
        strategy = (
            "followUp" if request.multitask_strategy == "follow_up" else request.multitask_strategy
        )
        submission = Submission(
            text=request.text,
            source="interactive",
            submitter="human",
            submission_id=uuid.uuid4().hex,
            multitask_strategy=strategy,  # type: ignore[arg-type]
            expand_commands=request.expand_commands,
            allow_user_input=True,
        )
        result = await host.agent_session.submit(submission, cursor=cursor)
        return p.to_wire(
            p.SubmitResult(
                accepted=result.accepted,
                submission_id=result.submission_id,
                reason=result.rejection_reason,
                command=dispatched_to_wire(result.command),
            )
        )

    def _set_model(self, host: SessionHost, request: p.SetModel) -> dict[str, Any]:
        cursor = host.cursor(request.cursor_id)
        session = host.agent_session
        if cursor is session.cursor:
            session.set_model(request.model)
        else:
            resolver = session.model_resolver
            if resolver is None:
                raise RequestError("failed", "the session has no model resolver")
            tools = tuple(t.name for t in session.tools)
            frame = cursor.frame
            cursor.frame = TurnFrame(
                tools=frame.tools if frame is not None else tools,
                model=resolver(request.model),
                system_prompt=frame.system_prompt if frame is not None else None,
                max_turns=frame.max_turns if frame is not None else None,
                hooks=frame.hooks if frame is not None else True,
            )
        host.publish("cursors", {"cursors": host.cursor_states()})
        return {"model": host.cursor_model(cursor)}

    async def _compare(self, host: SessionHost, request: p.Compare) -> dict[str, Any]:
        raise RequestError("bad_request", "compare is not built yet (docs/TAU-SERVE.md §8, M5)")

    async def shutdown(self) -> None:
        """Fire ``session_shutdown`` on every loaded session."""
        for host in list(self.hosts.values()):
            await host.close()
