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
from pathlib import Path
from typing import Any, TextIO, cast

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.cursor import TURN_CURSOR, Cursor, TurnFrame
from tau_agent_core.extension_locks import request_at
from tau_agent_core.extension_types import form_headless_value, validate_form_spec
from tau_agent_core.flows import Performed, Ready, UnknownFlowError
from tau_agent_core.projections import (
    attachment_expansion,
    browse_rows,
    command_vocabulary,
    domain_listing,
    extension_state,
    flow_next_step,
    model_catalog,
    path_completion,
    request_payload,
)
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog
from tau_agent_core.session_log import SessionLog, is_incomplete
from tau_agent_core.submission import Submission

from tau_coding_agent import __version__
from tau_coding_agent.serve import protocol as p

QUEUE_BOUND = 20_000
"""Frames a client may fall behind by before the daemon drops it (§5, backpressure)."""

REPLAY_BOUND = 50_000
"""Events a session keeps for a reconnecting client; older resumes get a snapshot."""

SLOW_CLIENT_CLOSE = 4000
"""The WebSocket close code for a dropped slow client; it reconnects with ``since``."""

SWITCHING = ("fork", "switch_session")
"""Mutations that move a client onto another session; a ``submit`` answers them as ``Ready``.

The daemon cannot move a client, so the client performs them: ``fork`` by
:class:`~protocol.ForkSession` then ``attach``, ``switch_session`` by ``attach``.
"""


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


@dataclasses.dataclass(frozen=True)
class SessionScope:
    """The ``runtime`` :func:`~tau_agent_core.flows.enumerate_domain` reads: a catalog and a cwd."""

    catalog: SessionCatalog
    cwd: str


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
        self._forms: dict[str, tuple[asyncio.Future[dict[str, Any] | None], dict[str, Any]]] = {}

    async def form(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        title, fields = validate_form_spec(spec)
        if not self._host.clients:
            self._host.daemon.log(f"{self._host.tag} form {title!r}: no client, defaults taken")
            return {field["name"]: form_headless_value(field) for field in fields}
        request_id = uuid.uuid4().hex[:8]
        future: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        self._forms[request_id] = (future, spec)
        self._host.publish("request", p.to_wire(p.RequestEventData(request_id, spec)))
        try:
            return await future
        finally:
            del self._forms[request_id]
            self._host.publish("request_closed", p.to_wire(p.RequestClosedEventData(request_id)))

    def open_forms(self) -> list[p.RequestEventData]:
        """Every form waiting for an answer, oldest first."""
        return [p.RequestEventData(rid, spec) for rid, (_, spec) in self._forms.items()]

    def answer(self, request_id: str, value: dict[str, Any] | None) -> None:
        """Resolve a form; a second answer to the same form is refused."""
        future, _ = self._forms.get(request_id, (None, None))
        if future is None or future.done():
            raise RequestError("not_found", f"no open form {request_id!r}")
        future.set_result(value)

    def notify(self, message: str, level: str = "info") -> None:
        self._host.publish("ui", p.to_wire(p.UiNotify(message=message, level=level)))

    def set_status(self, key: str, text: str | None) -> None:
        self._host.publish("ui", p.to_wire(p.UiStatus(key=key, text=text)))

    def panel(self, key: str, spec: dict[str, Any] | None) -> None:
        self._host.publish("ui", p.to_wire(p.UiPanel(key=key, spec=spec)))


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
        self._cursors_seen: list[p.CursorState] = []
        self._tool_started: dict[str, float] = {}
        self._turn_tokens: dict[str, int] = {}
        self._open_entries: dict[str, tuple[str, str | None]] = {}
        self._submission_cursor: dict[str, str | None] = {}
        self._requests: dict[str, p.ExtensionRequest | None] = {}
        self.attachment_reports: dict[str, dict[str, Any]] = {}

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

    def cursor_states(self) -> list[p.CursorState]:
        """Every live cursor, oldest first."""
        cursors = list(self.agent_session.cursors)
        states = [
            p.CursorState(
                cursor_id=c.id,
                leaf=c.leaf,
                label=c.label,
                owner_id=c.owner.id if c.owner is not None else None,
                busy=c.busy,
                model=self.cursor_model(c),
                request=self._request_at(c.leaf),
            )
            for c in cursors
        ]
        self._requests = {c.leaf: self._requests[c.leaf] for c in cursors if c.leaf is not None}
        return states

    def _request_at(self, leaf: str | None) -> p.ExtensionRequest | None:
        """The extension request at ``leaf``, projected, read once per leaf.

        An entry never changes once finished and a leaf is always finished, so
        the answer at a leaf is fixed; this runs after every event.
        """
        if leaf is None:
            return None
        if leaf not in self._requests:
            found = request_at(self.log.entries(), leaf)
            self._requests[leaf] = (
                None if found is None else p.ExtensionRequest(**request_payload(found))
            )
        return self._requests[leaf]

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
            self.sync_cursors()

    def sync_cursors(self) -> None:
        """Publish the cursor set if it changed since it was last published."""
        if self.cursor_states() != self._cursors_seen:
            self.publish_cursors()

    def publish_cursors(self) -> None:
        """Publish the whole cursor set now, and remember it as published."""
        self._cursors_seen = self.cursor_states()
        self.publish("cursors", p.to_wire(p.CursorsEventData(self._cursors_seen)))

    def attach(self, client: Client, epoch: str | None, since: int | None) -> p.Attached:
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
            cursors=self.cursor_states(),
            head_cursor_id=self.agent_session.cursor.id,
            cwd=self.cwd,
            models=self.models(),
            surface=self.surface(),
            requests=self.ui.open_forms(),
        )
        self.clients.add(client)
        client.attached.add(self.session_id)
        self.daemon.log(f"{self.tag} client {client.id} ({client.name}) attached")
        return attached

    def surface(self) -> p.Surface:
        """The session's command vocabulary and extension state, read now."""
        session = self.agent_session
        state = extension_state(session.get_extension_state())
        return p.Surface(
            commands=[p.CommandInfo(**c) for c in command_vocabulary(session)],
            command_args={
                name: session.get_extension_command_args(name)
                for name, _ in session.get_extension_commands()
            },
            shortcuts=[list(s) for s in session.get_extension_shortcuts()],
            extensions=[list(e) for e in session.list_managed_extensions()],
            loaded=[p.ExtensionInfo(**info) for info in state["extensions"]],
            load_errors=[[err["path"], err["error"]] for err in state["errors"]],
        )

    def models(self) -> list[p.ModelRecord]:
        """Every model the session's resolver accepts, as ``get_models`` lists them."""
        return [
            p.ModelRecord(name=m["name"], model=p.ModelSpec(**m["model"]))
            for m in model_catalog(self.agent_session.model_resolver)
        ]

    @property
    def cwd(self) -> str:
        """The session's directory on this machine."""
        return str(self.log.header.get("cwd", ""))

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
            cursor = TURN_CURSOR.get()
            self._open_entries[entry["id"]] = (
                entry.get("type", "?"),
                cursor.id if cursor is not None else None,
            )
        elif kind == "entry_final":
            self._open_entries.pop(entry["id"], None)
        self.publish(kind, p.to_wire(p.EntryEventData(entry=entry)))

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
            data: Any
            if name == "submission_start":
                submission = payload["submission"]
                cursor = payload.get("cursor")
                cursor_id = cursor.id if cursor is not None else None
                report = self.attachment_reports.get(submission.submission_id)
                data = p.SubmissionStartChannel(
                    p.SubmissionStartPayload(
                        submission=p.SubmissionInfo(**dataclasses.asdict(submission)),
                        text=payload.get("text", ""),
                        images=payload.get("images"),
                        cursor_id=cursor_id,
                        owner_id=cursor.owner.id
                        if cursor is not None and cursor.owner is not None
                        else None,
                        attachments=p.AttachmentReport(**report) if report is not None else None,
                    )
                )
                self._submission_cursor[submission.submission_id] = cursor_id
                preview = " ".join(str(payload.get("text", "")).split())[:60]
                model = self.cursor_model(cursor) if cursor is not None else "?"
                self.daemon.log(
                    f"{self.tag} cursor {cursor_id} turn started ({model}): {preview!r}"
                )
            elif name == "submission_end":
                submission = payload["submission"]
                data = p.SubmissionEndChannel(
                    p.SubmissionEndPayload(
                        submission=p.SubmissionInfo(**dataclasses.asdict(submission)),
                        side_usage=payload.get("side_usage"),
                    )
                )
                tokens = self._turn_tokens.pop(submission.submission_id, 0)
                self.daemon.log(
                    f"{self.tag} turn {submission.submission_id[:8]} ended: {tokens} tokens"
                )
                ended = self._submission_cursor.pop(submission.submission_id, None)
                for entry_id, (kind, cursor_id) in list(self._open_entries.items()):
                    if cursor_id == ended:
                        self.daemon.log(f"{self.tag} {kind} entry {entry_id} left incomplete")
                        del self._open_entries[entry_id]
            else:
                data = p.CustomMessageChannel(p.CustomMessagePayload(**payload))
            self.publish("channel", p.json_safe(p.to_wire(data)))

        return handler

    def _on_cursor_open(self, *, cursor: Cursor) -> None:
        owner = f", owner {cursor.owner.id}" if cursor.owner is not None else ""
        self.daemon.log(
            f"{self.tag} cursor {cursor.id} opened at {cursor.leaf} ({cursor.label!r}{owner})"
        )
        self.publish_cursors()

    def _on_cursor_close(self, *, cursor: Cursor) -> None:
        self.daemon.log(f"{self.tag} cursor {cursor.id} closed")
        self.publish_cursors()


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
        self.cwd = os.getcwd()

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
        hello_result = p.HelloResult(
            protocol=p.PROTOCOL_VERSION,
            client_id=client.id,
            pid=os.getpid(),
            version=__version__,
            cwd=self.cwd,
        )
        client.push(self._ok(request_id, p.to_wire(hello_result)))
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
            result = p.result_to_wire(request, await self._dispatch(client, request))
            host = self.hosts.get(getattr(request, "session_id", ""))
            if host is not None:
                host.sync_cursors()
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
        client.push(self._ok(request_id, p.result_to_wire(request, attached)))
        if attached.entries is None and request.since is not None:
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
            return p.CursorOpened(cursor.id)
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
            host.publish_cursors()
            return None
        if isinstance(request, p.SetModel):
            return await self._set_model(host, request)
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
            return p.RequestAnswered(handled=outcome.handled, output=outcome.output_text())
        if isinstance(request, p.Perform):
            return await self._perform(client, host, request)
        if isinstance(request, p.PerformReady):
            return await self._perform_ready_request(host, request)
        if isinstance(request, p.Describe):
            return host.surface()
        if isinstance(request, p.Compare):
            return await self._compare(host, request)
        if isinstance(request, p.EndCompare):
            return await self._end_compare(host, request)
        if isinstance(request, p.NextStep):
            try:
                return p.NextStepResult(
                    **flow_next_step(request.flow, request.bound, request.leaf, session.vocabulary)
                )
            except UnknownFlowError as exc:
                raise RequestError("not_found", str(exc.args[0])) from exc
        if isinstance(request, p.EnumerateDomain):
            if request.domain not in session.vocabulary.domains:
                raise RequestError("not_found", f"no domain {request.domain!r}")
            try:
                listing = domain_listing(
                    request.domain,
                    session=session,
                    runtime=SessionScope(self.catalog, host.cwd),
                    scope=request.scope,
                    cursor=request.leaf,
                    query=request.query,
                    limit=request.limit,
                )
                return p.DomainListing(
                    domain=listing["domain"],
                    values=[p.DomainChoice(**v) for v in listing["values"]],
                    total=listing["total"],
                )
            except KeyError as exc:
                raise RequestError("not_found", str(exc.args[0])) from exc
            except ValueError as exc:
                raise RequestError("bad_request", str(exc)) from exc
        if isinstance(request, p.CompletePath):
            found = path_completion(request.text, request.offset, Path(host.cwd))["completion"]
            if found is None:
                return p.CompletePathResult(None)
            matches = [p.PathMatch(**m) for m in found.pop("matches")]
            return p.CompletePathResult(p.AttachmentCompletion(matches=matches, **found))
        if isinstance(request, p.GetTree):
            cursor = host.cursor(request.cursor_id)
            nodes = [p.TreeRow(**row) for row in browse_rows(cursor.tree())]
            return p.TreeResult(nodes=nodes, leaf=cursor.leaf, count=len(nodes))
        if isinstance(request, p.ForkSession):
            return self._fork_session(host, request.at)
        raise RequestError("bad_request", f"unhandled request {type(request).__name__}")

    async def _list_sessions(self) -> p.SessionList:
        infos = await asyncio.to_thread(self.catalog.list, None)
        rows = [
            p.SessionRow(
                id=info.id,
                cwd=info.cwd,
                name=info.name,
                modified=info.modified.isoformat(),
                message_count=info.message_count,
                first_message=info.first_message,
                loaded=info.id in self.hosts,
            )
            for info in infos
            if info.error is None
        ]
        return p.SessionList(rows)

    async def _create_session(self, request: p.CreateSession) -> p.SessionCreated:
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
        return p.SessionCreated(log.id)

    async def _submit(self, host: SessionHost, request: p.Submit) -> p.SubmitResult:
        cursor = host.cursor(request.cursor_id)
        text, images = request.text, request.images
        submission_id = request.submission_id or uuid.uuid4().hex
        if request.expand_attachments:
            text, images, report = attachment_expansion(text, images, Path(host.cwd))
            host.attachment_reports[submission_id] = report
        submission = Submission(
            text=text,
            images=images,
            source="interactive",
            submitter="human",
            submission_id=submission_id,
            multitask_strategy=request.multitask_strategy,
            expand_commands=request.expand_commands,
            allow_user_input=True,
        )
        try:
            result = await host.agent_session.submit(submission, cursor=cursor)
        finally:
            host.attachment_reports.pop(submission_id, None)
        command = result.command
        if isinstance(command, Ready) and command.mutation not in SWITCHING:
            token = TURN_CURSOR.set(cursor)
            try:
                command = await self._perform_ready(host, cursor, command)
            finally:
                TURN_CURSOR.reset(token)
        return p.SubmitResult(
            accepted=result.accepted,
            submission_id=result.submission_id,
            reason=result.rejection_reason,
            command=p.command_to_wire(command),
        )

    async def _perform_ready_request(self, host: SessionHost, request: p.PerformReady) -> Any:
        """Perform a client's ``Ready`` at its cursor; a :data:`SWITCHING` one comes back as is.

        Raises:
            RequestError: no such flow, or a mutation that is not the flow's.
        """
        cursor = host.cursor(request.cursor_id)
        ready = request.ready
        flow = host.agent_session.vocabulary.flow(ready.flow)
        if flow is None:
            raise RequestError("not_found", f"no flow {ready.flow!r}")
        if flow.mutation != ready.mutation:
            raise RequestError(
                "bad_request",
                f"/{ready.flow} performs {flow.mutation!r}, not {ready.mutation!r}",
            )
        if ready.mutation in SWITCHING:
            return ready
        token = TURN_CURSOR.set(cursor)
        try:
            performed = await self._perform_ready(host, cursor, ready)
        finally:
            TURN_CURSOR.reset(token)
        host.sync_cursors()
        return performed

    async def _perform_ready(self, host: SessionHost, cursor: Cursor, ready: Ready) -> Performed:
        """Perform a command whose arguments are all bound, at ``cursor``, as a local head would.

        Raises:
            RequestError: a mutation the backend lacks, or one that answers no ``Performed``.
        """
        if ready.mutation == "set_model" and cursor is not host.agent_session.cursor:
            answer = await self._set_model(
                host,
                p.SetModel(
                    session_id=host.session_id, cursor_id=cursor.id, model=ready.arguments["name"]
                ),
            )
            return Performed(
                flow=ready.flow,
                mutation="set_model",
                data={"model": {"id": answer.model, "name": ready.arguments["name"]}},
            )
        if ready.mutation == "compare":
            return await self._start_compare(
                host, list(ready.arguments["models"]), str(ready.arguments["text"]), None
            )
        if ready.flow in host.agent_session.vocabulary.extension_flows:
            bound = " ".join(str(value) for value in ready.arguments.values())
            outcome = await host.backend.run_extension_command(ready.flow, bound)
            return Performed(
                flow=ready.flow,
                mutation=ready.mutation,
                data={"handled": outcome.handled, "output": outcome.output_text()},
            )
        action = getattr(host.backend, ready.mutation, None)
        if action is None:
            raise RequestError(
                "not_found", f"/{ready.flow} performs {ready.mutation!r}, which the daemon lacks"
            )
        result = action(**ready.arguments)
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, Performed):
            raise RequestError(
                "failed", f"/{ready.flow} answered {type(result).__name__}, not a Performed"
            )
        return result

    async def _set_model(self, host: SessionHost, request: p.SetModel) -> p.ModelSet:
        cursor = host.cursor(request.cursor_id)
        session = host.agent_session
        if cursor is session.cursor:
            await host.backend.set_model(request.model)
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
        host.publish_cursors()
        return p.ModelSet(host.cursor_model(cursor))

    async def _perform(self, client: Client, host: SessionHost, request: p.Perform) -> Any:
        """Run a :data:`~protocol.PERFORMABLE` backend operation for a client, at its cursor.

        The backend acts on ``AgentSession._turn_cursor()``, which is
        :data:`~tau_agent_core.cursor.TURN_CURSOR` while it is set.
        """
        if request.method not in p.PERFORMABLE:
            raise RequestError("bad_request", f"{request.method!r} is not performable")
        method = getattr(host.backend, request.method, None)
        if method is None:
            raise RequestError("not_found", f"the session backend has no {request.method!r}")
        cursor = host.cursor(request.cursor_id)
        self.log(f"{host.tag} client {client.id} performs {request.method} at cursor {cursor.id}")
        token = TURN_CURSOR.set(cursor)
        try:
            result = method(**request.arguments)
            if asyncio.iscoroutine(result):
                result = await result
        except (ValueError, KeyError, TypeError) as exc:
            raise RequestError("bad_request", f"{request.method}: {exc}") from exc
        finally:
            TURN_CURSOR.reset(token)
        host.sync_cursors()
        return p.perform_answer(result)

    def _fork_session(self, host: SessionHost, at: str | None) -> p.SessionForked:
        """Copy ``host``'s session, whole or up to ``at``, into a new one in its cwd.

        Raises:
            RequestError: ``at`` names no entry, or the copy would carry an entry a
                turn is still writing.
        """
        entries = host.log.entries()
        if at is not None and not any(e["id"] == at for e in entries):
            raise RequestError("not_found", f"{host.tag} has no entry {at!r}")
        copied = ConversationTree(entries, at).path() if at is not None else entries
        writing = [e["id"] for e in copied if is_incomplete(e)]
        if writing:
            raise RequestError(
                "busy", f"entry {writing[0]} is still being written; fork when its turn ends"
            )
        forked = self.catalog.fork(host.log, host.cwd, at=at)
        self.log(f"{host.tag} forked into session {forked.id[:8]}" + (f" at {at}" if at else ""))
        return p.SessionForked(forked.id)

    async def _compare(self, host: SessionHost, request: p.Compare) -> p.CompareStarted:
        performed = await self._start_compare(host, request.models, request.text, request.leaf)
        data = performed.data
        return p.CompareStarted(
            comparison_id=data["comparison_id"],
            cursors=[p.CompareCursor(**c) for c in data["cursors"]],
            message=data["message"],
        )

    async def _start_compare(
        self, host: SessionHost, models: list[str], text: str, leaf: str | None
    ) -> Performed:
        """Open a comparison on ``host`` (docs/TAU-SERVE.md §8) and log it.

        Raises:
            RequestError: an unknown model, an empty list or prompt, or a leaf
                that names no entry; nothing was opened.
        """
        try:
            performed: Performed = await host.backend.compare(models, text, at=leaf)
        except KeyError as exc:
            raise RequestError("not_found", str(exc.args[0] if exc.args else exc)) from exc
        except ValueError as exc:
            raise RequestError("bad_request", str(exc)) from exc
        comparison = host.backend.comparisons[performed.data["comparison_id"]]
        self.log(
            f"{host.tag} compare {comparison.id} at {comparison.leaf}: "
            + ", ".join(f"{c.id} {m}" for c, m in zip(comparison.cursors, comparison.models))
        )
        for cursor, turn in zip(comparison.cursors, comparison.turns):
            turn.add_done_callback(self._compare_turn_done(host, cursor.id))
        return performed

    def _compare_turn_done(self, host: SessionHost, cursor_id: str) -> Callable[..., None]:
        def done(turn: asyncio.Task[Any]) -> None:
            if turn.cancelled():
                self.log(f"{host.tag} cursor {cursor_id} compare turn cancelled")
            elif turn.exception() is not None:
                self.log(f"{host.tag} cursor {cursor_id} compare turn failed: {turn.exception()!r}")

        return done

    async def _end_compare(self, host: SessionHost, request: p.EndCompare) -> p.CompareEnded:
        try:
            leaf = await host.backend.end_compare(request.comparison_id, request.keep)
        except KeyError as exc:
            raise RequestError("not_found", str(exc.args[0] if exc.args else exc)) from exc
        except RuntimeError as exc:
            raise RequestError("busy", str(exc)) from exc
        kept = f"kept {request.keep}" if request.keep is not None else "kept none"
        self.log(f"{host.tag} compare {request.comparison_id} ended, {kept}; head at {leaf}")
        host.publish_cursors()
        return p.CompareEnded(leaf)

    async def shutdown(self) -> None:
        """Fire ``session_shutdown`` on every loaded session."""
        for host in list(self.hosts.values()):
            await host.close()
