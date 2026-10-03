"""The TUI's side of ``tau --connect`` (docs/TAU-SERVE.md §7.1).

The daemon owns the session; this module gives the TUI the objects it already
works with, built over a replica of the daemon's tree:

- :class:`RemoteConnection`: the link, re-established after a drop.
- :class:`ReplicaSession`: a read-only ``ConversationSession`` over the replica.
- :class:`RemoteCursor`: the head cursor's position, as the daemon last said.
- :class:`RemoteCatalog`: the daemon's session listing, for the sidebar and picker.
- :class:`RemoteBackend`: the ``Backend`` the TUI drives, each call a request.

A client never writes the tree: :class:`ReplicaSession` refuses ``append_at``,
so a TUI path that still writes locally fails with a message naming it.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from tau_agent_core.agent_session import ExtensionCommandResult
from tau_agent_core.capabilities import Argument, Domain
from tau_agent_core.compaction import CompactionDetails, CompactionResult
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.events import AgentEvent
from tau_agent_core.extension_locks import ExtensionRequest, request_at
from tau_agent_core.flows import DomainValue, DomainValues, FlowStep, Performed, Ready, View
from tau_agent_core.sdk import ExtensionInfo, ExtensionLoadError, LoadExtensionsResult
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog, SessionInfo
from tau_agent_core.session_log import config_at, default_leaf, session_name
from tau_agent_core.submission import Submission, SubmissionResult

from tau_coding_agent.backends import Backend, RenderHandler, RenderRouter
from tau_coding_agent.serve import protocol as p
from tau_coding_agent.serve.client import Address, Replica, ServeClient, ServeError

Listener = Callable[[dict[str, Any]], Awaitable[None] | None]
StatusHandler = Callable[[str, str], None]

RECONNECT_MAX_S = 5.0
"""The longest wait between reconnect attempts after the daemon goes away."""


class RemoteUnsupportedError(RuntimeError):
    """An operation this head cannot perform under ``--connect``, named."""


class RemoteConnection:
    """One TUI's link to a daemon: requests, per-session listeners, and reconnects.

    Attributes:
        address: Where the daemon listens.
        cwd: The directory new sessions get, a path on the daemon's machine.
    """

    def __init__(self, address: Address, *, cwd: str, token: str | None = None) -> None:
        self.address = address
        self.cwd = cwd
        self._token = token
        self._client: ServeClient | None = None
        self._listeners: dict[str, list[Listener]] = {}
        self._attached: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._on_status: StatusHandler | None = None
        self._ready = asyncio.Event()
        self._started = threading.Event()
        self._start_error: BaseException | None = None

    @property
    def is_local(self) -> bool:
        """Whether the daemon is on this machine (a unix socket or a loopback host)."""
        if self.address.path is not None:
            return True
        host = self.address.host or ""
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    async def start(self, on_status: StatusHandler | None = None) -> None:
        """Connect, and keep reconnecting whenever the link drops.

        Raises:
            ServeError: the daemon refused the hello.
            OSError: nothing answered at the address.
        """
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._on_status = on_status
        try:
            self._client = await self._connect()
        except BaseException as exc:
            self._start_error = exc
            raise
        finally:
            self._started.set()
        self._ready.set()
        self._supervisor = asyncio.create_task(self._supervise())

    async def _connect(self, replicas: dict[str, Replica] | None = None) -> ServeClient:
        client = await ServeClient.connect(
            self.address, client="tui", on_event=self._dispatch, token=self._token
        )
        client.replicas.update(replicas or {})
        return client

    async def _supervise(self) -> None:
        """Wait for the link to drop, then reconnect and re-attach, for good."""
        while True:
            assert self._client is not None
            await self._client.closed.wait()
            reason = self._client.close_reason
            held = dict(self._client.replicas)
            self._ready.clear()
            self._status("warning", f"daemon connection lost ({reason}); reconnecting")
            delay = 0.5
            while True:
                try:
                    self._client = await self._connect(held)
                    break
                except (OSError, ServeError):
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RECONNECT_MAX_S)
            for session_id in list(self._attached):
                try:
                    await self._client.attach(session_id)
                except (ServeError, ConnectionError) as exc:
                    self._status("error", f"re-attach to {session_id[:8]} failed: {exc}")
            self._ready.set()
            self._status("information", f"reconnected to {self.address}")

    def _status(self, severity: str, text: str) -> None:
        if self._on_status is not None:
            self._on_status(severity, text)

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        for listener in list(self._listeners.get(frame["session_id"], [])):
            result = listener(frame)
            if asyncio.iscoroutine(result):
                await result

    def listen(self, session_id: str, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` with every event of ``session_id``; returns the unsubscribe."""
        self._listeners.setdefault(session_id, []).append(listener)

        def unsubscribe() -> None:
            listeners = self._listeners.get(session_id, [])
            if listener in listeners:
                listeners.remove(listener)

        return unsubscribe

    async def request(self, message: Any) -> Any:
        """Send a request once the link is up; see :meth:`ServeClient.request`."""
        await self._ready.wait()
        assert self._client is not None
        return await self._client.request(message)

    def request_blocking(self, message: Any, timeout: float = 30.0) -> Any:
        """:meth:`request` from a worker thread, for the TUI's synchronous listing calls.

        Waits for :meth:`start` to finish first, since a TUI lists sessions as it mounts.

        Raises:
            RuntimeError: called on the event loop's own thread, where it would
                deadlock, or the connection never started.
        """
        if not self._started.wait(timeout):
            raise RuntimeError(f"no connection to {self.address} after {timeout:.0f}s")
        if self._start_error is not None or self._loop is None:
            raise RuntimeError(f"no connection to {self.address}: {self._start_error}")
        if threading.get_ident() == self._loop_thread:
            raise RuntimeError(
                f"{type(message).__name__} needs a round trip to the daemon and was "
                "called on the event loop; call it from a worker thread"
            )
        return asyncio.run_coroutine_threadsafe(self.request(message), self._loop).result(timeout)

    async def attach(self, session_id: str) -> Replica:
        """Attach to a session and keep it attached across reconnects."""
        await self._ready.wait()
        assert self._client is not None
        replica = await self._client.attach(session_id)
        self._attached.add(replica.session_id)
        return replica

    async def detach(self, session_id: str) -> None:
        """Stop receiving a session's events, and stop re-attaching it after a reconnect."""
        self._attached.discard(session_id)
        await self.request(p.Detach(session_id=session_id))

    def replica(self, session_id: str) -> Replica:
        """The current replica of an attached session (a new object after a snapshot)."""
        assert self._client is not None
        return self._client.replicas[session_id]

    async def close(self) -> None:
        """Stop reconnecting and close the link."""
        if self._supervisor is not None:
            self._supervisor.cancel()
        if self._client is not None:
            await self._client.close()


class ReplicaSession:
    """A ``ConversationSession`` over a replica: every read local, every write refused."""

    def __init__(self, remote: RemoteConnection, session_id: str) -> None:
        self._remote = remote
        self._session_id = session_id

    @property
    def replica(self) -> Replica:
        """The replica as it stands now."""
        return self._remote.replica(self._session_id)

    @property
    def id(self) -> str:
        return self._session_id

    def entries(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.replica.entries)

    async def append_at(
        self, parent_id: str | None, entry_type: str, payload: dict[str, Any]
    ) -> str:
        raise RemoteUnsupportedError(
            f"this head writes nothing under --connect; a {entry_type!r} entry must be "
            "written by the daemon (docs/TAU-SERVE.md §3)"
        )

    async def finalize(self, entry_id: str, payload: dict[str, Any]) -> None:
        raise RemoteUnsupportedError("this head writes nothing under --connect")

    @property
    def header(self) -> dict[str, Any]:
        return {"type": "session", "id": self._session_id, "cwd": self.replica.cwd}

    @property
    def messages(self) -> list[dict[str, Any]]:
        entries = self.replica.entries
        path = ConversationTree(entries, default_leaf(entries)).path()
        return [e["message"] for e in path if e.get("type") == "message"]

    @property
    def context(self) -> list[dict[str, Any]]:
        entries = self.entries()
        return ConversationTree(entries, default_leaf(entries)).context_for()

    @property
    def config(self) -> dict[str, Any]:
        entries = self.replica.entries
        return config_at(entries, default_leaf(entries))

    @property
    def model(self) -> str:
        return str(self.config.get("model", ""))

    @property
    def backend(self) -> str:
        return str(self.config.get("backend", ""))

    def display_title(self) -> str:
        name = session_name(self.replica.entries)
        if name:
            return name
        for message in self.messages:
            if message.get("role") == "user":
                content = message.get("content")
                text = (
                    content
                    if isinstance(content, str)
                    else " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))
                )
                text = text.replace("\n", " ").strip()
                if text:
                    return text[:50] + ("..." if len(text) > 50 else "")
        return f"Session ({self.model})"


class RemoteCursor:
    """The read surface of a daemon cursor: its leaf and busy state, as last pushed.

    ``cursor_id`` ``None`` follows the session's head cursor, which a daemon restart
    replaces with a new one.
    """

    def __init__(self, session: ReplicaSession, cursor_id: str | None = None) -> None:
        self.log = session
        self._cursor_id = cursor_id
        self.owner = None
        self.label = "remote"
        self.frame = None

    @property
    def id(self) -> str:
        return self._cursor_id or self.log.replica.head_cursor_id

    def _state(self) -> dict[str, Any]:
        state = self.log.replica.cursors.get(self.id)
        if state is None:
            raise RemoteUnsupportedError(f"cursor {self.id} is not live on the daemon")
        return state

    @property
    def leaf(self) -> str | None:
        leaf = self._state()["leaf"]
        return str(leaf) if leaf is not None else None

    @property
    def busy(self) -> bool:
        return bool(self._state()["busy"])

    @property
    def session_id(self) -> str:
        return self.log.id

    def entries(self) -> list[dict[str, Any]]:
        return self.log.entries()

    def tree(self) -> ConversationTree:
        return ConversationTree(self.log.entries(), self.leaf)

    def context(self) -> list[dict[str, Any]]:
        return self.tree().context_for()

    def move(self, target: str | None) -> None:
        raise RemoteUnsupportedError("move the cursor through the backend's navigate_tree")


class RemoteCatalog(SessionCatalog):
    """The daemon's sessions, listed for the sidebar and the picker.

    Only :meth:`list` is served: creating, loading and forking go through the
    daemon's own requests in the TUI's remote paths, so the other members refuse.
    """

    def __init__(self, remote: RemoteConnection) -> None:
        self._remote = remote

    def list(self, cwd: str | None = None) -> list[SessionInfo]:
        """Every daemon session, or those in ``cwd``; blocks, so call it from a worker thread."""
        rows = self._remote.request_blocking(p.ListSessions())["sessions"]
        infos = []
        for row in rows:
            if cwd is not None and row["cwd"] != cwd:
                continue
            modified = datetime.fromisoformat(row["modified"])
            infos.append(
                SessionInfo(
                    ref=row["id"],
                    id=row["id"],
                    cwd=row["cwd"],
                    name=row["name"],
                    created=modified,
                    modified=modified,
                    message_count=row["message_count"],
                    first_message=row["first_message"],
                    last_message="",
                    parent=None,
                )
            )
        return infos

    def _refuse(self, what: str) -> RemoteUnsupportedError:
        return RemoteUnsupportedError(f"{what} goes through the daemon under --connect")

    def create(self, cwd: str, model: str, backend: str, **_: Any) -> ConversationSession:
        raise self._refuse("creating a session")

    def create_ephemeral(self, cwd: str, model: str, backend: str, **_: Any) -> ConversationSession:
        raise self._refuse("an unpersisted session")

    def load(self, ref: str) -> ConversationSession:
        raise self._refuse("loading a session")

    def fork(
        self, source: ConversationSession, cwd: str, *, at: str | None = None
    ) -> ConversationSession:
        raise self._refuse("forking a session")


def submission_result_from_wire(result: dict[str, Any], submission_id: str) -> SubmissionResult:
    """A :class:`~protocol.SubmitResult` as the core's ``SubmissionResult``."""
    return SubmissionResult(
        accepted=result["accepted"],
        submission_id=result.get("submission_id") or submission_id,
        rejection_reason=result.get("reason"),
        command=dispatched_from_wire(result.get("command")),
    )


def flow_step_from_wire(fields: dict[str, Any]) -> FlowStep:
    """A ``FlowStep`` as JSON back into the record, its argument and domain included."""
    argument = dict(fields["argument"])
    domain = dict(fields["domain"])
    if domain["values"] is not None:
        domain["values"] = tuple(domain["values"])
    return FlowStep(
        flow=fields["flow"],
        argument=Argument(**argument),
        domain=Domain(**domain),
        cursor=fields["cursor"],
        bound=dict(fields["bound"]),
    )


def dispatched_from_wire(command: dict[str, Any] | None) -> Any:
    """Rebuild a dispatched arm the TUI can act on from the daemon's ``{"arm": ...}``.

    ``Performed`` and ``View`` come back whole. A ``FlowStep`` is a command
    still missing an argument, which the TUI asks for and steps through the
    daemon. A ``Ready`` comes back only for a mutation that moves the client to
    another session (``daemon.SWITCHING``), which the TUI performs itself.

    Raises:
        RemoteUnsupportedError: an arm this head does not know.
    """
    if command is None:
        return None
    arm = command.get("arm")
    fields = {k: v for k, v in command.items() if k != "arm"}
    if arm == "Performed":
        return Performed(**fields)
    if arm == "View":
        return View(**fields)
    if arm == "FlowStep":
        return flow_step_from_wire(fields)
    if arm == "Ready":
        return Ready(**fields)
    raise RemoteUnsupportedError(f"the daemon answered a command with {arm!r}")


def value_from_wire(answer: dict[str, Any]) -> Any:
    """A :class:`~protocol.Perform` answer as the value the local backend would return."""
    kind = answer["kind"]
    if kind == "value":
        return answer["value"]
    fields = answer["fields"]
    if kind == "Performed":
        return Performed(**fields)
    if kind == "ExtensionCommandResult":
        return ExtensionCommandResult(**fields)
    if kind == "SubmissionResult":
        fields = dict(fields)
        fields["command"] = None
        if fields["lock"] is not None:
            fields["lock"] = ExtensionRequest(**fields["lock"])
        return SubmissionResult(**fields)
    if kind == "CompactionResult":
        fields = dict(fields)
        if fields["details"] is not None:
            fields["details"] = CompactionDetails(**fields["details"])
        return CompactionResult(**fields)
    raise RemoteUnsupportedError(f"the daemon answered with a {kind!r} this head cannot read")


class RemoteBackend(Backend):
    """The ``Backend`` the TUI drives under ``--connect``: the daemon's session, by request.

    It has no ``agent_session``, so the TUI's paths that need one in-process skip
    it, and every operation it does offer is a protocol request.
    """

    def __init__(self, remote: RemoteConnection, session_id: str) -> None:
        super().__init__({"model": ""})
        self._remote = remote
        self._session_id = session_id
        self._delegate: Any = None
        self._ui_unsub = remote.listen(session_id, self._on_ui_event)

    @property
    def replica(self) -> Replica:
        return self._remote.replica(self._session_id)

    @property
    def session_id(self) -> str:
        """The daemon session this backend drives."""
        return self._session_id

    @property
    def pending_request(self) -> ExtensionRequest | None:
        """The extension request at the head cursor, read from the replica (EXTENSION-LOCKS §2)."""
        replica = self.replica
        state = replica.cursors.get(replica.head_cursor_id)
        return request_at(replica.entries, state["leaf"] if state is not None else None)

    def _head(self) -> str:
        return self.replica.head_cursor_id

    async def _perform(self, method: str, **arguments: Any) -> Any:
        answer = await self._remote.request(
            p.Perform(
                session_id=self._session_id,
                cursor_id=self._head(),
                method=method,
                arguments=arguments,
            )
        )
        return value_from_wire(answer)

    async def next_step(
        self, flow: str, bound: dict[str, Any] | None, cursor: str | None
    ) -> FlowStep | Ready:
        """The daemon's ``next_step`` for ``flow``, which knows the session's extension flows."""
        answer = await self._remote.request(
            p.NextStep(session_id=self._session_id, flow=flow, bound=bound, leaf=cursor)
        )
        if answer["status"] == "ready":
            return Ready(**answer["ready"])
        return flow_step_from_wire(answer["step"])

    async def enumerate_domain(
        self,
        domain: str,
        *,
        scope: str | None = None,
        cursor: str | None = None,
        query: str = "",
        limit: int = 50,
    ) -> DomainValues:
        """The daemon's values for ``domain``, read against the session's own objects."""
        answer = await self._remote.request(
            p.EnumerateDomain(
                session_id=self._session_id,
                domain=domain,
                scope=scope,  # type: ignore[arg-type]
                leaf=cursor,
                query=query,
                limit=limit,
            )
        )
        return DomainValues(
            domain=answer["domain"],
            values=tuple(DomainValue(value=v["value"], label=v["label"]) for v in answer["values"]),
            total=answer["total"],
        )

    async def fork(self, at: str | None) -> str:
        """Fork the session on the daemon, at entry ``at`` or whole; returns the new session's id."""
        answer = await self._remote.request(p.ForkSession(session_id=self._session_id, at=at))
        return str(answer["session_id"])

    def is_extension_flow(self, name: str) -> bool:
        """Whether ``name`` is a flow an extension of the daemon's session declared."""
        return any(
            c["name"] == name and c["origin"] == "extension" and c["flow"]
            for c in self.replica.surface["commands"]
        )

    def extension_summary(self) -> tuple[list[ExtensionInfo], list[ExtensionLoadError]]:
        """What the session's extensions registered and which failed to load, as last described."""
        surface = self.replica.surface
        infos = [
            ExtensionInfo(**{**info, "subjects": tuple(info["subjects"])})
            for info in surface["loaded"]
        ]
        errors = [
            ExtensionLoadError(path=path, error=error) for path, error in surface["load_errors"]
        ]
        return infos, errors

    async def chat(self, messages: list[dict]) -> tuple[str, dict, list[dict]]:
        raise RemoteUnsupportedError("chat() is headless-only; the TUI uses submit_turn")

    async def stream_chat(self, messages, callback, on_event=None, on_json_event=None):  # type: ignore[no-untyped-def]
        raise RemoteUnsupportedError("stream_chat() is headless-only; the TUI uses submit_turn")

    async def stream_submission(
        self, submission, context, callback, on_event=None, on_json_event=None
    ):  # type: ignore[no-untyped-def]
        raise RemoteUnsupportedError("stream_submission() is headless-only")

    async def submit_turn(
        self, submission: Submission, context: list[dict] | None
    ) -> SubmissionResult:
        """Send the submission to the head cursor; ``context`` is the daemon's, so it is unused."""
        strategy = (
            "follow_up"
            if submission.multitask_strategy == "followUp"
            else submission.multitask_strategy
        )
        result = await self._remote.request(
            p.Submit(
                session_id=self._session_id,
                cursor_id=self._head(),
                text=submission.text,
                multitask_strategy=strategy,  # type: ignore[arg-type]
                expand_commands=submission.expand_commands,
                submission_id=submission.submission_id,
                images=submission.images,
            )
        )
        return submission_result_from_wire(result, submission.submission_id)

    async def submit_command(self, submission: Submission) -> SubmissionResult:
        return await self.submit_turn(submission, None)

    def subscribe_render(
        self, handler: RenderHandler, *, on_orphan: Callable[[str], None] | None = None
    ) -> RenderRouter:
        """Feed the daemon's events into a :class:`RenderRouter`, as a local bus would."""
        router = RenderRouter(handler, on_orphan=on_orphan)

        async def feed(frame: dict[str, Any]) -> None:
            kind = frame["kind"]
            data = frame["data"]
            if kind == "agent_event":
                await router.on_agent_event(AgentEvent.model_validate(data))
            elif kind == "channel":
                name = data["name"]
                payload = data["payload"]
                if name == "submission_start":
                    owner = object() if payload.get("owner_id") else None
                    await router.on_submission_start(
                        submission=Submission(**payload["submission"]),
                        text=payload.get("text", ""),
                        images=payload.get("images"),
                        cursor=SimpleNamespace(id=payload.get("cursor_id"), owner=owner),
                    )
                elif name == "submission_end":
                    await router.on_submission_end(
                        submission=Submission(**payload["submission"]),
                        side_usage=payload.get("side_usage"),
                    )
                elif name == "custom_message":
                    await router.on_custom_message(
                        entry_id=payload["entry_id"], message=payload["message"]
                    )

        router.bind_detach(self._remote.listen(self._session_id, feed))
        return router

    def abort(self) -> None:
        asyncio.get_running_loop().create_task(
            self._remote.request(p.Abort(session_id=self._session_id, cursor_id=self._head()))
        )

    async def load_extensions(self, *_: Any, **__: Any) -> LoadExtensionsResult:
        """Nothing to load here: the daemon loaded the session's extensions itself."""
        return LoadExtensionsResult()

    def set_ui_delegate(self, delegate: Any) -> None:
        """Show the session's extension notices and forms through ``delegate``."""
        self._delegate = delegate

    async def _on_ui_event(self, frame: dict[str, Any]) -> None:
        delegate = self._delegate
        if delegate is None:
            return
        data = frame["data"]
        if frame["kind"] == "ui":
            if data["op"] == "notify":
                delegate.notify(data["message"], data.get("level", "info"))
            elif data["op"] == "status":
                delegate.set_status(data["key"], data.get("text"))
            elif data["op"] == "panel":
                delegate.panel(data["key"], data.get("spec"))
        elif frame["kind"] == "request":
            asyncio.get_running_loop().create_task(self._answer_form(data))

    async def _answer_form(self, data: dict[str, Any]) -> None:
        answers = await self._delegate.form(data["spec"])
        try:
            await self._remote.request(
                p.Answer(session_id=self._session_id, request_id=data["request_id"], value=answers)
            )
        except ServeError as exc:
            if exc.code != "not_found":
                raise
            self._delegate.notify("That form was answered from another client", "warning")

    def get_extension_commands(self) -> list[tuple[str, str]]:
        return [
            (str(c["name"]), str(c["description"]))
            for c in self.replica.surface["commands"]
            if c["origin"] == "extension" and not c["hidden"]
        ]

    def get_extension_command_args(self, name: str) -> str | None:
        value = self.replica.surface["command_args"].get(name)
        return str(value) if value is not None else None

    def get_extension_shortcuts(self) -> list[tuple[str, str, str, str]]:
        return [
            (str(a), str(b), str(c), str(d)) for a, b, c, d in self.replica.surface["shortcuts"]
        ]

    def list_managed_extensions(self) -> list[tuple[str, bool]]:
        return [(str(path), bool(enabled)) for path, enabled in self.replica.surface["extensions"]]

    async def _refresh_surface(self) -> None:
        surface = await self._remote.request(p.Describe(session_id=self._session_id))
        self.replica.surface = dict(surface)

    async def set_model(self, name: str) -> Performed:
        answer = await self._remote.request(
            p.SetModel(session_id=self._session_id, cursor_id=self._head(), model=name)
        )
        return Performed(
            flow="model",
            mutation="set_model",
            data={"model": {"id": answer["model"], "name": name}},
        )

    async def end_compare(self, comparison_id: str, keep: str | None) -> str | None:
        """End a daemon comparison, as ``TauBackend.end_compare`` does in-process.

        Raises:
            KeyError: the daemon has no such comparison (another client ended it).
            RuntimeError: the kept turn or the head is still running.
        """
        try:
            answer = await self._remote.request(
                p.EndCompare(session_id=self._session_id, comparison_id=comparison_id, keep=keep)
            )
        except ServeError as exc:
            if exc.code == "not_found":
                raise KeyError(str(exc)) from exc
            if exc.code == "busy":
                raise RuntimeError(str(exc)) from exc
            raise
        leaf = answer["leaf"]
        return str(leaf) if leaf is not None else None

    async def compact(self, custom_instructions: str | None = None) -> Any:
        return await self._perform("compact", custom_instructions=custom_instructions)

    async def set_auto_compaction(self, enabled: bool) -> Performed:
        result: Performed = await self._perform("set_auto_compaction", enabled=enabled)
        return result

    async def set_session_name(self, name: str) -> Performed:
        result: Performed = await self._perform("set_session_name", name=name)
        return result

    async def enable_extension(self, path: str) -> Performed:
        result: Performed = await self._perform("enable_extension", path=path)
        await self._refresh_surface()
        return result

    async def disable_extension(self, path: str) -> Performed:
        result: Performed = await self._perform("disable_extension", path=path)
        await self._refresh_surface()
        return result

    async def reload_extension(self, path: str) -> Performed:
        result: Performed = await self._perform("reload_extension", path=path)
        await self._refresh_surface()
        return result

    async def run_extension_command(self, name: str, args: str = "") -> ExtensionCommandResult:
        result: ExtensionCommandResult = await self._perform(
            "run_extension_command", name=name, args=args
        )
        return result

    async def answer_request(
        self, request_id: str, action: str, values: dict[str, Any] | None = None
    ) -> ExtensionCommandResult:
        result: ExtensionCommandResult = await self._perform(
            "answer_request", request_id=request_id, action=action, values=values
        )
        return result

    async def navigate_tree(self, target_id: str | None, **options: Any) -> list[dict]:
        result: list[dict] = await self._perform("navigate_tree", target_id=target_id, **options)
        return result

    async def elide_span(self, anchor_id: str, first_kept_id: str) -> list[dict]:
        result: list[dict] = await self._perform(
            "elide_span", anchor_id=anchor_id, first_kept_id=first_kept_id
        )
        return result

    async def commit_branch(self, ids: list[str], *, drop_context: bool) -> list[dict]:
        result: list[dict] = await self._perform(
            "commit_branch", ids=list(ids), drop_context=drop_context
        )
        return result

    async def paste_subtree(self, source_id: str, target_id: str) -> list[str]:
        result: list[str] = await self._perform(
            "paste_subtree", source_id=source_id, target_id=target_id
        )
        return result

    async def rollback_turn(self, text: str) -> SubmissionResult:
        result: SubmissionResult = await self._perform("rollback_turn", text=text)
        return result

    def close(self) -> None:
        """Stop listening for this session's UI events."""
        self._ui_unsub()
