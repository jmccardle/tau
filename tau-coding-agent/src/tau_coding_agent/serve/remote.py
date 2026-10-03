"""The TUI's side of ``tau --connect`` (docs/TAU-SERVE.md §7.1).

The daemon owns the session; this module gives the TUI the objects it already
works with, built over a replica of the daemon's tree:

- :class:`RemoteConnection`: the link, re-established after a drop.
- :class:`ReplicaSession`: a read-only ``ConversationSession`` over the replica.
- :class:`RemoteCursor`: the head cursor's position, as the daemon last said.
- :class:`RemoteCatalog`: the daemon's session listing, for the sidebar and picker.
- :class:`RemoteBackend`: the ``Backend`` the TUI drives, each call a request.
- :class:`WireJoin`: the daemon's bounded events rejoined with its entries, for rendering.

A client never writes the tree: :class:`ReplicaSession` refuses ``append_at``,
so a TUI path that still writes locally fails with a message naming it.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import threading
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from tau_agent_core.agent_session import ExtensionCommandResult
from tau_agent_core.capabilities import Argument, Domain
from tau_agent_core.compaction import CompactionDetails, CompactionResult
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.extension_locks import ExtensionRequest, request_at
from tau_agent_core.flows import DomainValue, DomainValues, FlowStep, Performed, Ready, View
from tau_agent_core.sdk import ExtensionInfo, ExtensionLoadError, LoadExtensionsResult
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog, SessionInfo
from tau_agent_core.session_log import config_at, default_leaf, session_name
from tau_agent_core.submission import Submission, SubmissionResult

from tau_coding_agent.backends import (
    Backend,
    EventDetail,
    RenderHandler,
    RenderRouter,
    result_text,
)
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
        """Every daemon session, or those in ``cwd``; blocks, so call it from a worker thread.

        The listing carries a bounded ``title``, never message text, so ``first_message``
        holds the title and a picker searches that.
        """
        rows = self._remote.request_blocking(p.ListSessions())["sessions"]
        infos = []
        for row in rows:
            if cwd is not None and row["cwd"] != cwd:
                continue
            infos.append(
                SessionInfo(
                    ref=row["session_id"],
                    id=row["session_id"],
                    cwd=row["cwd"],
                    name=row["name"],
                    created=datetime.fromisoformat(row["created"]),
                    modified=datetime.fromisoformat(row["modified"]),
                    message_count=row["message_count"],
                    first_message=row["title"],
                    last_message="",
                    parent=row["parent"],
                    error=row["error"],
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


def submission_result_from_wire(result: dict[str, Any]) -> SubmissionResult:
    """An accepted ``submit`` answer as the core's ``SubmissionResult``."""
    return SubmissionResult(
        accepted=True,
        submission_id=result["submission_id"],
        command=dispatched_from_wire(result["dispatched"]),
    )


def rejection_from_wire(error: ServeError, submission_id: str) -> SubmissionResult:
    """A ``submission_rejected`` refusal as the core's ``SubmissionResult``, its lock included."""
    data = error.data or {}
    lock = data.get("lock")
    return SubmissionResult(
        accepted=False,
        submission_id=data.get("submission_id", submission_id),
        rejection_reason=error.reason,
        lock=ExtensionRequest(
            entry_id=lock["entry_id"],
            extension=lock["extension"],
            sentence=lock["sentence"],
            lock=True,
            ask=lock["ask"],
            release=lock["release"],
        )
        if lock is not None
        else None,
    )


def compaction_from_wire(end: dict[str, Any]) -> CompactionResult | None:
    """A ``compaction_end`` payload as ``backend.compact`` returns it locally.

    Raises:
        RuntimeError: the compaction failed or was cancelled, as it raises locally.
    """
    if end["is_error"]:
        raise RuntimeError(end["error"])
    if end["cancelled"]:
        raise RuntimeError(f"compaction {end['compaction_id']} was cancelled")
    if not end["performed"]:
        return None
    return CompactionResult(
        summary=end["summary"],
        first_kept_entry_id=end["first_kept_entry_id"],
        tokens_before=end["tokens_before"],
        details=CompactionDetails(
            read_files=list(end["read_files"]), modified_files=list(end["modified_files"])
        ),
        compacted_entry_ids=list(end["compacted_entry_ids"]),
        tokens_saved=end["tokens_saved"],
        usage=dict(end["usage"]),
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
        leaf=fields["leaf"],
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


class WireJoin:
    """Rejoins the daemon's ``agent_event`` with the entries a turn writes, for a router.

    ``agent_event`` is RPC's bounded ``WireEvent``: it leaves out a tool's
    arguments and result, and a message's content and usage, because the entry
    events carry them. This reads them back, keyed by tool call id or by the
    ``cursor_id`` an entry event and an ``agent_event`` both name, and hands the
    router what a local bus would (:class:`~tau_coding_agent.backends.EventDetail`):

    - ``tool_execution_start`` takes its arguments from the assistant entry
      finalized before the tool ran.
    - ``tool_execution_end`` takes its result from the toolResult entry, which a
      sequential batch writes after the event (so the event is held) and a
      parallel one before it.
    - ``message_end`` with a ``stop_reason`` takes usage from the assistant entry
      its cursor finalized just before it.
    - ``message_start`` is held: when its cursor's next entry is a user message,
      it was a steer, delivered with that message.
    - ``side_completion_*`` carries no model, text or spend; the box keeps its
      streamed text and says so.

    Every frame passes through :meth:`frame` in the daemon's order, after the
    client applied it to the replica.
    """

    def __init__(self, router: RenderRouter, *, on_orphan: Callable[[str], None] | None) -> None:
        self._router = router
        self._on_orphan = on_orphan
        self._args: dict[str, dict[str, Any]] = {}
        self._finished: dict[str | None, dict[str, Any]] = {}
        self._ended: dict[str, dict[str, Any]] = {}
        self._running: set[str] = set()
        self._results: dict[str, str] = {}
        self._starts: dict[str | None, dict[str, Any]] = {}

    async def frame(self, frame: dict[str, Any]) -> None:
        """Apply one event frame of the session."""
        kind = frame["kind"]
        data = frame["data"]
        if kind in ("entry_open", "entry_append", "entry_final"):
            await self._entry(kind, data["entry"], data.get("cursor_id"))
        elif kind == "agent_event":
            await self._agent_event(data)
        elif kind == "channel":
            await self._channel(data["name"], data["payload"])

    async def _entry(self, kind: str, entry: dict[str, Any], cursor_id: str | None) -> None:
        message = entry.get("message") if entry.get("type") == "message" else None
        if not isinstance(message, dict) or kind == "entry_open":
            return
        role = message.get("role")
        if role == "assistant" and kind == "entry_final":
            self._finished[cursor_id] = message
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    self._args[str(block.get("id"))] = block.get("arguments") or {}
        elif role == "toolResult":
            call_id = str(message.get("tool_call_id"))
            text = result_text(message.get("content"))
            wire = self._ended.pop(call_id, None)
            if wire is None:
                if call_id in self._running:
                    self._results[call_id] = text
            else:
                await self._router.on_wire_event(wire, EventDetail(result=text))
        elif role == "user" and cursor_id in self._starts:
            start = self._starts.pop(cursor_id)
            await self._router.on_wire_event(start, EventDetail(message=message))

    async def _agent_event(self, wire: dict[str, Any]) -> None:
        kind = wire["type"]
        cursor_id = wire.get("cursor_id")
        self._starts.pop(cursor_id, None)
        detail = EventDetail()
        if kind == "message_start":
            self._starts[cursor_id] = wire
            return
        if kind == "tool_execution_start":
            call_id = str(wire.get("tool_call_id"))
            self._running.add(call_id)
            args = self._args.pop(call_id, None)
            if args is None:
                self._orphan(f"tool call {call_id} started with no finalized entry naming it")
            detail = EventDetail(args=args)
        elif kind == "tool_execution_end":
            call_id = str(wire.get("tool_call_id"))
            self._running.discard(call_id)
            result = self._results.pop(call_id, None)
            if result is None:
                self._ended[call_id] = wire
                return
            detail = EventDetail(result=result)
        elif kind == "message_end" and wire.get("stop_reason") is not None:
            message = self._finished.pop(cursor_id, None)
            if message is None:
                self._orphan(f"cursor {cursor_id} ended a completion it finalized no entry for")
            detail = EventDetail(message=message)
        elif kind == "side_completion_end":
            detail = EventDetail(spend_known=False)
        await self._router.on_wire_event(wire, detail)

    async def _channel(self, name: str, payload: dict[str, Any]) -> None:
        router = self._router
        if name == "submission_start":
            owner = object() if payload.get("owner_id") else None
            await router.on_submission_start(
                submission=Submission(**payload["submission"]),
                text=payload.get("text", ""),
                images=payload.get("images"),
                cursor=SimpleNamespace(id=payload.get("cursor_id"), owner=owner),
            )
        elif name == "submission_end":
            submission_id = payload["submission"]["submission_id"]
            for call_id, wire in list(self._ended.items()):
                if wire.get("submission_id") == submission_id:
                    del self._ended[call_id]
                    self._orphan(f"tool call {call_id} ended with no result entry written")
                    await router.on_wire_event(wire, EventDetail())
            await router.on_submission_end(
                submission=Submission(**payload["submission"]),
                side_usage=payload.get("side_usage"),
            )
        elif name == "custom_message":
            await router.on_custom_message(entry_id=payload["entry_id"], message=payload["message"])

    def _orphan(self, reason: str) -> None:
        if self._on_orphan is not None:
            self._on_orphan(reason)


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
        self._ends: dict[str, asyncio.Future[None]] = {}
        self._compacting = 0
        self._compaction_ends: dict[str, dict[str, Any]] = {}
        self._compaction_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ui_unsub = remote.listen(session_id, self._on_event)

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

    async def _rpc(self, verb: str, **params: Any) -> dict[str, Any]:
        """RPC ``verb`` at the head cursor; ``None`` params are left out, as RPC's defaults."""
        given = {k: v for k, v in params.items() if v is not None}
        answer: dict[str, Any] = await self._remote.request(
            p.RpcCall(verb, self._session_id, self._head(), given)
        )
        return answer

    def _performed(self, mutation: str, answer: dict[str, Any]) -> Performed:
        """An RPC mutator's answer as the ``Performed`` the local backend returns for it."""
        return Performed(flow=None, mutation=mutation, data=answer, leaf=answer.get("leaf"))

    async def next_step(
        self, flow: str, bound: dict[str, Any] | None, leaf: str | None
    ) -> FlowStep | Ready:
        """The daemon's ``next_step`` for ``flow``, which knows the session's extension flows."""
        answer = await self._rpc("next_step", flow=flow, bound=bound, leaf=leaf)
        if answer["status"] == "ready":
            return Ready(**answer["ready"])
        return flow_step_from_wire(answer["step"])

    async def enumerate_domain(
        self,
        domain: str,
        *,
        scope: str | None = None,
        leaf: str | None = None,
        query: str = "",
        limit: int = 50,
    ) -> DomainValues:
        """The daemon's values for ``domain``, read against the session's own objects."""
        answer = await self._rpc(
            "enumerate_domain", domain=domain, scope=scope, leaf=leaf, query=query, limit=limit
        )
        return DomainValues(
            domain=answer["domain"],
            values=tuple(DomainValue(value=v["value"], label=v["label"]) for v in answer["values"]),
            total=answer["total"],
        )

    async def fork(self, at: str | None) -> str:
        """Fork the session on the daemon, at entry ``at`` or whole; returns the new session's id."""
        answer = await self._remote.request(
            p.Fork(session_id=self._session_id, cursor_id=self._head(), at=at)
        )
        return str(answer["session"]["session_id"])

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
        """Submit at the head cursor and return when its turn ends, as the local backend does.

        ``context`` is the daemon's, so it is unused. An admitted turn ends with its
        ``submission_end`` channel event; anything else is over when answered.
        """
        sid = submission.submission_id
        ended = self._ends[sid] = asyncio.get_running_loop().create_future()
        try:
            answer = await self._rpc(
                "submit",
                text=submission.text,
                images=submission.images,
                source=submission.source,
                submitter=submission.submitter,
                submission_id=sid,
                multitask_strategy=submission.multitask_strategy,
                expand_commands=submission.expand_commands,
                allow_user_input=submission.allow_user_input,
                store_history=submission.store_history,
                silent=submission.silent,
                correlation=submission.correlation or None,
                depth=submission.depth,
            )
            if answer["admitted"]:
                await ended
        except ServeError as exc:
            if exc.code != "submission_rejected":
                raise
            return rejection_from_wire(exc, sid)
        finally:
            self._ends.pop(sid, None)
        return submission_result_from_wire(answer)

    async def submit_command(self, submission: Submission) -> SubmissionResult:
        return await self.submit_turn(submission, None)

    def subscribe_render(
        self, handler: RenderHandler, *, on_orphan: Callable[[str], None] | None = None
    ) -> RenderRouter:
        """Feed the daemon's events into a :class:`RenderRouter` through a :class:`WireJoin`."""
        router = RenderRouter(handler, on_orphan=on_orphan)
        join = WireJoin(router, on_orphan=on_orphan)
        router.bind_detach(self._remote.listen(self._session_id, join.frame))
        return router

    def abort(self) -> None:
        asyncio.get_running_loop().create_task(self._rpc("abort"))

    async def load_extensions(self, *_: Any, **__: Any) -> LoadExtensionsResult:
        """Nothing to load here: the daemon loaded the session's extensions itself."""
        return LoadExtensionsResult()

    def set_ui_delegate(self, delegate: Any) -> None:
        """Show the session's extension notices and forms through ``delegate``."""
        self._delegate = delegate

    async def _on_event(self, frame: dict[str, Any]) -> None:
        data = frame["data"]
        if frame["kind"] == "channel" and data["name"] == "submission_end":
            ended = self._ends.get(data["payload"]["submission"]["submission_id"])
            if ended is not None and not ended.done():
                ended.set_result(None)
        elif frame["kind"] == "compaction_end":
            waiter = self._compaction_waiters.pop(data["compaction_id"], None)
            if waiter is not None:
                waiter.set_result(data)
            elif self._compacting:
                self._compaction_ends[data["compaction_id"]] = data
        delegate = self._delegate
        if delegate is None:
            return
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
        return self._performed("set_model", await self._rpc("set_model", name=name))

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

    async def compact(self, custom_instructions: str | None = None) -> CompactionResult | None:
        """Compact at the head cursor and return its outcome, read off ``compaction_end``.

        The event can arrive before the answer is read, so ends seen while a
        compaction is pending are kept until claimed.

        Raises:
            RuntimeError: the compaction failed or was cancelled.
        """
        self._compacting += 1
        try:
            answer = await self._rpc("compact", custom_instructions=custom_instructions)
            compaction_id = answer["compaction_id"]
            end = self._compaction_ends.pop(compaction_id, None)
            if end is None:
                waiter = asyncio.get_running_loop().create_future()
                self._compaction_waiters[compaction_id] = waiter
                end = await waiter
        finally:
            self._compacting -= 1
            if not self._compacting:
                self._compaction_ends.clear()
        return compaction_from_wire(end)

    async def set_auto_compaction(self, enabled: bool) -> Performed:
        answer = await self._rpc("set_auto_compaction", enabled=enabled)
        return self._performed("set_auto_compaction", answer)

    async def set_session_name(self, name: str) -> Performed:
        return self._performed("set_session_name", await self._rpc("set_session_name", name=name))

    async def enable_extension(self, path: str) -> Performed:
        answer = await self._rpc("enable_extension", path=path)
        await self._refresh_surface()
        return self._performed("enable_extension", answer)

    async def disable_extension(self, path: str) -> Performed:
        answer = await self._rpc("disable_extension", path=path)
        await self._refresh_surface()
        return self._performed("disable_extension", answer)

    async def reload_extension(self, path: str) -> Performed:
        answer = await self._rpc("reload_extension", path=path)
        await self._refresh_surface()
        return self._performed("reload_extension", answer)

    async def run_extension_command(self, name: str, args: str = "") -> ExtensionCommandResult:
        """Run an extension command by submitting its slash line, as a typed one runs.

        A name the session's surface does not list is not sent: submitted, it
        would reach the model as prose.
        """
        known = {c["name"] for c in self.replica.surface["commands"] if c["origin"] == "extension"}
        if name not in known:
            return ExtensionCommandResult(handled=False)
        line = f"/{name} {args}".rstrip()
        result = await self.submit_turn(
            Submission(
                text=line,
                source="interactive",
                submitter="human",
                submission_id=uuid.uuid4().hex,
                multitask_strategy="reject",
                expand_commands=True,
            ),
            None,
        )
        if not result.accepted:
            raise RuntimeError(result.rejection_reason or f"/{name} was refused")
        if not isinstance(result.command, Performed):
            raise RemoteUnsupportedError(f"/{name} resolved to {type(result.command).__name__}")
        return ExtensionCommandResult(handled=True, output=result.command.data.get("output"))

    async def answer_request(
        self, request_id: str, action: str, values: dict[str, Any] | None = None
    ) -> ExtensionCommandResult:
        answer = await self._rpc(
            "answer_request", request_id=request_id, action=action, values=values
        )
        return ExtensionCommandResult(handled=answer["handled"], output=answer["output"])

    async def navigate_tree(
        self,
        target_id: str,
        *,
        summarize: bool = False,
        custom_instructions: str | None = None,
    ) -> list[dict]:
        if summarize:
            answer = await self._rpc(
                "summarize_and_navigate",
                target_id=target_id,
                custom_instructions=custom_instructions,
            )
        else:
            answer = await self._rpc("navigate", target_id=target_id)
        messages: list[dict] = answer["messages"]
        return messages

    async def elide_span(self, anchor_id: str, first_kept_id: str) -> list[dict]:
        answer = await self._rpc("elide_span", anchor_id=anchor_id, first_kept_id=first_kept_id)
        messages: list[dict] = answer["messages"]
        return messages

    async def commit_branch(self, ids: list[str], *, drop_context: bool) -> list[dict]:
        answer = await self._rpc("commit_branch", ids=list(ids), drop_context=drop_context)
        messages: list[dict] = answer["messages"]
        return messages

    async def paste_subtree(self, source_id: str, target_id: str) -> list[str]:
        answer = await self._rpc("paste_subtree", source_id=source_id, target_id=target_id)
        minted: list[str] = answer["minted_ids"]
        return minted

    async def rollback_turn(self, text: str) -> SubmissionResult:
        """Roll the head cursor's last turn back and send ``text``, as ``TauBackend.rollback_turn``."""
        return await self.submit_turn(
            Submission(
                text=text,
                source="interactive",
                submitter="human",
                submission_id=uuid.uuid4().hex,
                multitask_strategy="rollback",
                allow_user_input=True,
            ),
            None,
        )

    def close(self) -> None:
        """Stop listening for this session's UI events."""
        self._ui_unsub()
