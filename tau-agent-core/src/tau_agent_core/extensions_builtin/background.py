"""``background`` — backgrounding as a property conferred on any tool.

Reference: docs/BACKGROUND-TOOLS.md. §1 says why this is an extension and not a
core tool; §6 is where a result is delivered (the cursor table); §7 is the ``bg``
tool; §8 is thread safety.

Load it like any file extension and name the tools in config::

    "extensions": {"background": {"tools": ["bash", "grep"]}}

Four moves, each on τ's public surface:

* :func:`backgroundable` — wrap a built-in by name, or an extension's own tool
  definition, into a ``register_tool`` dict whose schema has one added boolean.
  ``background`` absent or false delegates straight through.
* :class:`BackgroundRegistry` — the supervised job set. Each job records the
  cursor that issued it and keeps its own abort signal, so a turn's abort does
  not kill it; :meth:`BackgroundRegistry.cancel_all` cancels *and waits*.
* :func:`register_backgrounding` — the entry point: wraps the named tools,
  routes each result to its issuing cursor (:class:`CursorDelivery`), and
  registers the model-facing ``bg`` tool (status, wait, cancel).
* :func:`completion_text` — the utterance a result is delivered as, labelled
  whenever it lands anywhere but the branch that issued it.

The model elects backgrounding per call; nothing promotes a tool on a timer.
:data:`BACKGROUND_PARAM`'s description is the whole policy.

**Loop affinity.** Every registry method runs on one event loop and raises from
any other, so a job's state is never touched by two threads (§8).

**Fail-Early.** :func:`backgroundable` raises on a tool that is neither a known
built-in nor a supplied definition, on a definition missing ``execute``, and on a
schema that already declares the added parameter. :func:`register_backgrounding`
raises when its config names no tools. A result is never dropped: if no cursor
can take it, it is parked, reported, and returned by ``bg``.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tau_llm.abort import AbortSignal
from tau_agent_core.suspension import TurnSuspended
from tau_agent_core.tau_home import resolve_tau_home

BACKGROUND_KEY = "background"

BACKGROUND_PARAM: dict[str, Any] = {
    "type": "boolean",
    "description": (
        "Background it when you can keep going without the answer; block on it "
        "when your next step depends on it. Backgrounding returns a handle "
        "immediately and the result arrives later, in this turn or a new one."
    ),
}

HANDLE_PREFIX = "bg"
BG_TOOL_NAME = "bg"
WAIT_DEFAULT_S = 30.0
WAIT_MAX_S = 600.0

RUNNING = "running"
QUEUED = "queued"
DELIVERED = "delivered"
HELD_OVER = "held_over"
PARKED = "parked"
CLAIMED = "claimed"
CANCELLED = "cancelled"

ROUTE_ORIGIN = "origin"
ROUTE_MOVED = "moved"
ROUTE_OWNER = "owner"
ROUTE_CROSSED = "crossed"
ROUTE_PARK = "park"


@dataclass
class BackgroundCall:
    """One backgrounded call: its provenance, its state, and its result once it has one.

    ``args`` excludes the backgrounding flag: the flag is this module's, and
    echoing it back would suggest the tool saw it.

    Attributes:
        origin_cursor_id: The id of the cursor whose turn issued the call.
        origin_leaf: That cursor's leaf when the call started: an entry on the
            branch holding the call.
        state: One of the module's state constants; see docs/BACKGROUND-TOOLS.md §6.
        delivered_to: The id of the cursor the result was submitted to, if any.
        note: Why the job is in its state, when that is not obvious.
        signal: The job's own abort signal, tripped only by a cancel.
        origin: The issuing :class:`~tau_agent_core.cursor.Cursor`; runtime only.
    """

    handle: str
    tool: str
    args: dict[str, Any]
    started_at_ms: int
    origin_session_id: str
    origin_cursor_id: str | None = None
    origin_leaf: str | None = None
    state: str = RUNNING
    result: Any = None
    finished_at_ms: int | None = None
    delivered_to: str | None = None
    note: str = ""
    waiters: int = 0
    signal: AbortSignal = field(default_factory=AbortSignal, repr=False, compare=False)
    origin: Any = field(default=None, repr=False, compare=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)

    def correlation(self) -> dict[str, Any]:
        """The provenance as a JSON-safe ``correlation`` payload.

        Returns:
            Handle, tool, arguments, start time, and the originating session,
            cursor and leaf.
        """
        return {
            "background_handle": self.handle,
            "tool": self.tool,
            "args": self.args,
            "started_at_ms": self.started_at_ms,
            "origin_session_id": self.origin_session_id,
            "origin_cursor_id": self.origin_cursor_id,
            "origin_leaf": self.origin_leaf,
        }


def _raise(exc: BaseException) -> None:
    """Re-raise ``exc`` — the default error sink.

    Raises:
        BaseException: always, the argument unchanged.
    """
    raise exc


def _no_cursor() -> Any:
    """The issuing cursor when the registry is used without a session: none."""
    return None


@dataclass
class BackgroundRegistry:
    """Every backgrounded call of one session, running or finished.

    Args:
        session_id: Returns the id of the tree the head is on now; read once per
            call at start.
        cursor: Returns the cursor the code running now acts on; read once per call
            at start, inside the issuing turn. ``lambda: api.context.cursor`` in a
            real session.
        on_error: Receives every exception the delivery path raises. Defaults to
            re-raising, because a dropped completion is not an available default.
    """

    session_id: Callable[[], str]
    cursor: Callable[[], Any] = _no_cursor
    on_error: Callable[[BaseException], None] = field(default=_raise)
    _jobs: dict[str, BackgroundCall] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    _counter: int = 0
    _loop: asyncio.AbstractEventLoop | None = None

    def _check_loop(self, op: str) -> None:
        """Bind the registry to the running loop, or raise if called from another.

        Raises:
            RuntimeError: no running loop (a plain thread), or a different live loop.
        """
        loop = asyncio.get_running_loop()
        bound = self._loop
        if bound is None or bound.is_closed() or not bound.is_running():
            self._loop = loop
        elif bound is not loop:
            raise RuntimeError(
                f"BackgroundRegistry.{op}: called from a different event loop than "
                "the session's; job state is loop-confined (docs/BACKGROUND-TOOLS.md §8)"
            )

    def outstanding(self) -> list[BackgroundCall]:
        """Every call still running, in start order."""
        return [c for c in self._jobs.values() if c.state == RUNNING]

    def jobs(self) -> list[BackgroundCall]:
        """Every call this registry started, in start order, whatever its state."""
        return list(self._jobs.values())

    def get(self, handle: str) -> BackgroundCall:
        """The call ``handle`` names.

        Raises:
            KeyError: no call has that handle; the message lists the known ones.
        """
        if handle not in self._jobs:
            known = ", ".join(self._jobs) or "none"
            raise KeyError(f"no background job {handle!r} (known: {known})")
        return self._jobs[handle]

    def _next_handle(self, tool: str) -> str:
        """Mint ``bg-<tool>-<n>``, unique within this registry."""
        self._counter += 1
        return f"{HANDLE_PREFIX}-{tool}-{self._counter}"

    def start(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        run: Callable[[BackgroundCall], Any],
        deliver: Callable[[BackgroundCall, Any], Any],
    ) -> BackgroundCall:
        """Spawn ``run`` as a supervised task and return its record at once.

        Args:
            tool: The wrapped tool's name.
            args: The model's arguments, with the backgrounding flag removed.
            run: Called with the record; performs the real work and honours
                ``call.signal``.
            deliver: Called with ``(call, result)`` once the work finishes, unless a
                ``bg`` wait claimed the result. Its exceptions reach ``on_error``.

        Returns:
            The :class:`BackgroundCall`.
        """
        self._check_loop("start")
        origin = self.cursor()
        handle = self._next_handle(tool)
        call = BackgroundCall(
            handle=handle,
            tool=tool,
            args=args,
            started_at_ms=int(time.time() * 1000),
            origin_session_id=self.session_id(),
            origin_cursor_id=getattr(origin, "id", None),
            origin_leaf=getattr(origin, "leaf", None),
            origin=origin,
        )
        self._jobs[handle] = call
        self._tasks[handle] = asyncio.create_task(self._supervise(call, run, deliver), name=handle)
        return call

    async def _supervise(
        self,
        call: BackgroundCall,
        run: Callable[[BackgroundCall], Any],
        deliver: Callable[[BackgroundCall, Any], Any],
    ) -> None:
        """Run one call, then deliver it unless a waiter claimed it.

        A raising tool is delivered as a failed result, as a blocking call would
        show it. Clears ``DRIVING_SUBMISSION_DEPTH``: a completion is not
        self-submission (docs/BACKGROUND-TOOLS.md §5).
        """
        from tau_agent_core.submission import DRIVING_SUBMISSION_DEPTH

        token = DRIVING_SUBMISSION_DEPTH.set(None)
        try:
            try:
                result = await _maybe_await(run(call))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — delivered as the call's failed result
                result = {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "is_error": True,
                }
            call.result = result
            call.finished_at_ms = int(time.time() * 1000)
            call.done.set()
            if call.waiters:
                call.state = CLAIMED
                call.note = "returned to a bg wait"
                return
            call.state = QUEUED
            await _maybe_await(deliver(call, result))
            if call.state == QUEUED:
                call.state = DELIVERED
        except asyncio.CancelledError:
            if call.state == RUNNING:
                call.state = CANCELLED
            call.done.set()
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced through on_error, not swallowed
            call.state = PARKED
            call.note = f"delivery raised {type(exc).__name__}: {exc}"
            self.on_error(exc)
        finally:
            self._tasks.pop(call.handle, None)
            DRIVING_SUBMISSION_DEPTH.reset(token)

    async def wait(
        self, handle: str, timeout_s: float, signal: AbortSignal | None = None
    ) -> BackgroundCall:
        """Wait up to ``timeout_s`` for ``handle`` to finish; a result it finishes with is claimed.

        Claimed means the result is returned here and not delivered as a message.

        Raises:
            KeyError: unknown handle.
        """
        self._check_loop("wait")
        call = self.get(handle)
        if call.done.is_set():
            return call
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        call.waiters += 1
        try:
            while not call.done.is_set():
                remaining = deadline - loop.time()
                if remaining <= 0 or (signal is not None and signal.is_aborted()):
                    break
                try:
                    await asyncio.wait_for(call.done.wait(), min(remaining, 0.1))
                except asyncio.TimeoutError:
                    pass
        finally:
            call.waiters -= 1
        return call

    async def cancel(self, handle: str) -> BackgroundCall:
        """Cancel ``handle`` if it is still running, and wait for it to unwind.

        A call whose work has finished is left alone: its delivery is under way.

        Raises:
            KeyError: unknown handle.
        """
        self._check_loop("cancel")
        call = self.get(handle)
        task = self._tasks.get(handle)
        if call.state != RUNNING or task is None:
            return call
        call.signal.abort()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._mark_cancelled(call)
        return call

    def _mark_cancelled(self, call: BackgroundCall) -> None:
        """Record a cancel, including one that landed before the task first ran."""
        if call.state == RUNNING:
            call.state = CANCELLED
        call.done.set()

    async def cancel_all(self) -> int:
        """Cancel every task, delivering or not, and wait for each to unwind.

        Returns:
            How many tasks were cancelled.
        """
        tasks = list(self._tasks.items())
        for handle, task in tasks:
            self._jobs[handle].signal.abort()
            task.cancel()
        for _, task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except TurnSuspended:
                raise
            except BaseException:  # noqa: BLE001 — already reported through on_error
                pass
        for handle, _ in tasks:
            self._mark_cancelled(self._jobs[handle])
        self._tasks.clear()
        return len(tasks)


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, else return it as-is."""
    if inspect.isawaitable(value):
        return await value
    return value


def _schema_with_background(parameters: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy ``parameters`` with the backgrounding boolean added.

    Raises:
        ValueError: if the tool already declares that parameter.
    """
    schema = copy.deepcopy(parameters)
    properties = schema.setdefault("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(
            f"backgroundable: tool schema 'properties' must be an object, got {properties!r}"
        )
    if BACKGROUND_KEY in properties:
        raise ValueError(
            f"backgroundable: tool already declares a {BACKGROUND_KEY!r} parameter, so "
            "conferring backgrounding on it would make that name mean two things"
        )
    properties[BACKGROUND_KEY] = dict(BACKGROUND_PARAM)
    return schema


def _resolve_builtin(name: str, cwd: str) -> Any:
    """The built-in tool ``name``, constructed for ``cwd``.

    Raises:
        ValueError: if ``name`` is not a built-in.
    """
    from tau_agent_core.tools import create_all_tools

    builtins = {tool.name: tool for tool in create_all_tools(cwd)}
    if name not in builtins:
        known = ", ".join(sorted(builtins))
        raise ValueError(f"backgroundable: unknown built-in tool {name!r} (known: {known})")
    return builtins[name]


def _resolve_target(
    name: str, definition: dict[str, Any] | None, tool: Any, cwd: str | None
) -> tuple[Callable[..., Any], str, dict[str, Any], str]:
    """Resolve what a wrapped call delegates to, and the schema it advertises.

    Precedence: an extension-shape ``definition``, else a built-in-shape ``tool``
    instance, else the built-in named ``name`` constructed for ``cwd``.

    Returns:
        ``(delegate, description, parameters, label)``, ``delegate`` having the
        extension signature.

    Raises:
        ValueError: if both explicit sources are given, if ``definition`` lacks a
            required key, if a built-in must be resolved without a ``cwd``, or if
            ``name`` is not a built-in.
    """
    if definition is not None and tool is not None:
        raise ValueError(
            "backgroundable: pass definition= (an extension tool) or tool= (a "
            "built-in instance), not both — they are two different execute signatures"
        )

    if definition is not None:
        for key in ("description", "parameters", "execute"):
            if key not in definition:
                raise ValueError(f"backgroundable: definition is missing required key {key!r}")
        execute = definition["execute"]
        if not callable(execute):
            raise ValueError("backgroundable: definition['execute'] must be callable")
        return (
            execute,
            definition["description"],
            definition["parameters"],
            definition.get("label", name),
        )

    if tool is None:
        if cwd is None:
            raise ValueError(f"backgroundable: resolving built-in {name!r} needs cwd=")
        tool = _resolve_builtin(name, cwd)
    original = tool

    async def _delegate(
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Callable[..., Any] | None = None,
        ctx: Any = None,
    ) -> Any:
        return await _maybe_await(
            original.execute(
                tool_call_id=tool_call_id,
                args=params,
                signal=signal,
                on_update=on_update,
            )
        )

    return (
        _delegate,
        original.description,
        original.parameters,
        getattr(original, "label", name),
    )


def backgroundable(
    name: str,
    registry: BackgroundRegistry,
    submit: Callable[[BackgroundCall, Any], Any],
    *,
    definition: dict[str, Any] | None = None,
    tool: Any = None,
    cwd: str | None = None,
    label: str | None = None,
    description: str | None = None,
    control_tool: str | None = None,
) -> dict[str, Any]:
    """Build a ``register_tool`` definition for ``name`` with backgrounding conferred.

    The result shadows the built-in of the same name, because τ resolves extension
    tools last. An extension's tool cannot be shadowed (``register_tool`` raises on
    a duplicate), so for one of those pass its ``definition``.

    A backgrounded call runs under its job's own signal and with no ``on_update``:
    the turn that issued it may be over, and its signal and its tool-call stream
    belong to that turn (docs/BACKGROUND-TOOLS.md §8).

    Args:
        name: The tool name to register under, and the built-in to resolve when
            neither ``definition`` nor ``tool`` is given.
        registry: The supervised job set the call is started in.
        submit: Called as ``submit(call, result)`` from the completed task.
        definition: An extension-shape tool definition to wrap.
        tool: A built-in-shape instance to wrap.
        cwd: The working directory for a built-in resolved by ``name``.
        label: UI label; defaults to the wrapped tool's.
        description: Description sent to the model; defaults to the wrapped tool's.
        control_tool: The name of the tool that checks, waits on and cancels a job,
            named in the started message; ``None`` names none.

    Returns:
        A dict for :meth:`ExtensionAPI.register_tool`.

    Raises:
        ValueError: per :func:`_resolve_target` and :func:`_schema_with_background`.
    """
    delegate, base_description, base_parameters, base_label = _resolve_target(
        name, definition, tool, cwd
    )
    parameters = _schema_with_background(base_parameters)
    control = f" {control_tool} checks, waits on or cancels it." if control_tool else ""

    async def _execute(
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Callable[..., Any] | None = None,
        ctx: Any = None,
    ) -> Any:
        elected = bool(params.get(BACKGROUND_KEY, False))
        forwarded = {k: v for k, v in params.items() if k != BACKGROUND_KEY}
        if not elected:
            return await _maybe_await(delegate(tool_call_id, forwarded, signal, on_update, ctx))

        def _run(call: BackgroundCall) -> Any:
            return delegate(tool_call_id, forwarded, call.signal, None, ctx)

        call = registry.start(tool=name, args=forwarded, run=_run, deliver=submit)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Started {name} in the background as {call.handle}. "
                        f"Keep going; the result will arrive as a message.{control}"
                    ),
                }
            ],
            "details": call.correlation(),
        }

    return {
        "name": name,
        "label": label if label is not None else base_label,
        "description": description if description is not None else base_description,
        "parameters": parameters,
        "execute": _execute,
        "execution_mode": "sequential",
    }


def completion_text(
    call: BackgroundCall,
    result: Any,
    *,
    route: str = ROUTE_ORIGIN,
    held_over: bool = False,
) -> str:
    """The utterance a completed call is delivered as.

    A result returned with ``is_error`` reads "failed", as a blocking call would
    show it. A result landing anywhere but the branch that issued the call says
    where it came from, because the conversation reading it never contained the
    call (docs/BACKGROUND-TOOLS.md §6).

    Args:
        call: The completed call's record.
        result: Whatever the wrapped tool returned.
        route: Where it is going: one of the module's ``ROUTE_*`` constants.
        held_over: Whether the turn it was first queued into was stopped first.

    Returns:
        The message text.
    """
    verb = "failed" if isinstance(result, dict) and result.get("is_error") else "finished"
    body = _result_text(result)
    held = " (held over: the turn it first reached was stopped before reading it)"
    held = held if held_over else ""
    args = json.dumps(call.args, sort_keys=True, default=str)
    if route == ROUTE_ORIGIN:
        return f"[background {call.handle}] {call.tool} {verb}{held}:\n{body}"
    if route == ROUTE_MOVED:
        return (
            f"[background {call.handle}] {call.tool} {verb}{held}. It was started on a "
            f"branch this conversation has since left (at entry {call.origin_leaf}) with "
            f"arguments {args}:\n{body}"
        )
    if route == ROUTE_OWNER:
        label = getattr(call.origin, "label", "") or "no label"
        return (
            f"[background {call.handle}, from closed cursor {call.origin_cursor_id} "
            f"({label})] {call.tool} {verb}{held}. A cursor you own started it with "
            f"arguments {args} and has finished since:\n{body}"
        )
    if route == ROUTE_CROSSED:
        started = datetime.fromtimestamp(call.started_at_ms / 1000, tz=timezone.utc)
        return (
            f"[background {call.handle}, from an earlier session] {call.tool} {verb}{held}. "
            f"It was started in session {call.origin_session_id} at "
            f"{started.isoformat(timespec='seconds')} with arguments {args}, and that "
            f"conversation has ended since:\n{body}"
        )
    raise ValueError(f"completion_text: unknown route {route!r}")


def _result_text(result: Any) -> str:
    """Flatten a tool result's text blocks into one string, or ``str(result)``."""
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p)
    return str(result)


def _delivers(text: str, call: BackgroundCall) -> bool:
    """Whether ``text`` is a delivery of ``call``, whatever its route.

    The closing ``]`` or ``,`` is part of the match, or ``bg-x-1`` would match
    ``bg-x-10``.
    """
    return f"[background {call.handle}]" in text or f"[background {call.handle}," in text


def _queued_text(message: Any) -> str:
    """The text of a queued ``UserMessage`` (or a dict of one)."""
    content = message.get("content") if isinstance(message, dict) else message.content
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _on_path(cursor: Any, call: BackgroundCall) -> bool:
    """Whether a user message delivering ``call`` lies on ``cursor``'s path."""
    by_id = {str(e["id"]): e for e in cursor.entries() if e.get("id") is not None}
    current, seen = cursor.leaf, set()
    while current is not None and current in by_id and current not in seen:
        seen.add(current)
        entry = by_id[current]
        message = entry.get("message")
        if entry.get("type") == "message" and isinstance(message, dict):
            if message.get("role") == "user" and _delivers(_queued_text(message), call):
                return True
        parent = entry.get("parentId")
        current = str(parent) if parent is not None else None
    return False


def _leaf_on_path(cursor: Any, leaf: str | None) -> bool:
    """Whether entry ``leaf`` is ``cursor``'s leaf or one of its ancestors."""
    if leaf is None:
        return True
    by_id = {str(e["id"]): e for e in cursor.entries() if e.get("id") is not None}
    current, seen = cursor.leaf, set()
    while current is not None and current in by_id and current not in seen:
        if current == leaf:
            return True
        seen.add(current)
        parent = by_id[current].get("parentId")
        current = str(parent) if parent is not None else None
    return False


class CursorDelivery:
    """Deliver each result to the cursor that issued it (docs/BACKGROUND-TOOLS.md §6).

    The steer queue is not a store of record: ``abort()`` and ``rollback`` clear it,
    and a turn can end between a steer's busy check and its append. So a queued
    result is followed until the target cursor is idle, and then confirmed on its
    path, re-submitted, held over to its next turn, or re-routed.

    Args:
        api: The bound ``ExtensionAPI``.
        notify: Reports a result that was held over or parked.
    """

    def __init__(self, api: Any, notify: Callable[[str, str], None]) -> None:
        """Bind to ``api``."""
        self._api = api
        self._notify = notify

    async def __call__(self, call: BackgroundCall, result: Any) -> None:
        """Deliver ``result``; returns once it is on a path, held over, or parked."""
        await self._route(call, result, held_over=False)

    def _live(self, cursor: Any) -> bool:
        """Whether ``cursor`` is still open on the session."""
        return cursor is not None and any(c is cursor for c in self._api.context.cursors)

    def target(self, call: BackgroundCall) -> tuple[Any, str]:
        """The cursor the result goes to, and the route; ``None`` is the head's cursor.

        Raises:
            ValueError: the call recorded no issuing cursor.
        """
        origin = call.origin
        if origin is None:
            raise ValueError(f"{call.handle}: no issuing cursor was recorded")
        if self._live(origin):
            moved = not _leaf_on_path(origin, call.origin_leaf)
            return origin, ROUTE_MOVED if moved else ROUTE_ORIGIN
        owner = origin.owner
        while owner is not None:
            if self._live(owner):
                return owner, ROUTE_OWNER
            owner = owner.owner
        if origin.session_id != self._api.context.session_id:
            return None, ROUTE_CROSSED
        return None, ROUTE_PARK

    def _park(self, call: BackgroundCall, reason: str) -> None:
        """Keep the result in the registry, and say so."""
        call.state = PARKED
        call.note = reason
        self._notify(
            f"background {call.handle}: {call.tool} finished but was not delivered: "
            f"{reason}. {BG_TOOL_NAME} returns it (action=wait, handle={call.handle}).",
            "warning",
        )

    async def _route(self, call: BackgroundCall, result: Any, *, held_over: bool) -> None:
        """Submit to :meth:`target`, then follow the submission until it is settled."""
        target, route = self.target(call)
        if route == ROUTE_PARK:
            self._park(call, f"cursor {call.origin_cursor_id} closed and no live cursor owns it")
            return
        text = completion_text(call, result, route=route, held_over=held_over)
        outcome = await self._api.submit(
            text,
            multitask_strategy="steer",
            correlation={
                **call.correlation(),
                "route": route,
                "held_over": held_over,
                "crossed_session": route == ROUTE_CROSSED,
            },
            cursor=target,
        )
        # Outside any turn of a live cursor, acting_cursor is the head's: where submit(cursor=None) went.
        dest = target if target is not None else self._api.context.cursor
        call.delivered_to = dest.id
        if not outcome.accepted:
            self._park(call, f"submission refused: {outcome.rejection_reason}")
            return
        if outcome.messages:
            call.state = DELIVERED
            return
        await self._settle(call, result, dest, route=route, held_over=held_over)

    async def _settle(
        self, call: BackgroundCall, result: Any, dest: Any, *, route: str, held_over: bool
    ) -> None:
        """Wait out ``dest``'s turn, then confirm, re-submit, hold over or re-route."""
        while True:
            async with dest.turn_lock:
                pass
            # A FIFO waiter (a rollback) takes the lock on the next loop pass.
            await asyncio.sleep(0)
            if not dest.busy:
                break
        stranded = [m for m in dest.steer_queue if _delivers(_queued_text(m), call)]
        if stranded:
            for message in stranded:
                dest.steer_queue.remove(message)
            call.note = "its turn ended before the steer was appended; re-submitted"
            await self._route(call, result, held_over=held_over)
            return
        if _on_path(dest, call):
            call.state = DELIVERED
            return
        if self._live(dest):
            dest.next_turn_queue.append(completion_text(call, result, route=route, held_over=True))
            call.state = HELD_OVER
            call.note = f"the turn on cursor {dest.id} was stopped first; rides its next prompt"
            self._notify(f"background {call.handle}: {call.note}", "info")
            return
        await self._route(call, result, held_over=True)


def _bg_tool(registry: BackgroundRegistry) -> dict[str, Any]:
    """The model-facing ``bg`` tool over ``registry`` (docs/BACKGROUND-TOOLS.md §7)."""

    def _describe(call: BackgroundCall) -> str:
        age = (time.time() * 1000 - call.started_at_ms) / 1000
        where = f"cursor {call.origin_cursor_id}" if call.origin_cursor_id else "no cursor"
        note = f" — {call.note}" if call.note else ""
        return f"{call.handle} {call.tool} {call.state}, started {age:.1f}s ago on {where}{note}"

    def _error(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    def _text(text: str, call: BackgroundCall | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if call is not None:
            out["details"] = {**call.correlation(), "state": call.state}
        return out

    async def _execute(
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Callable[..., Any] | None = None,
        ctx: Any = None,
    ) -> Any:
        action = params.get("action")
        handle = params.get("handle")
        if action == "status" and not handle:
            jobs = registry.jobs()
            return _text("\n".join(_describe(c) for c in jobs) if jobs else "no background jobs")
        if action not in ("status", "wait", "cancel"):
            return _error(f"bg: action must be status, wait or cancel, not {action!r}")
        if not isinstance(handle, str) or not handle:
            return _error(f"bg: action {action!r} needs a handle")
        try:
            call = registry.get(handle)
        except KeyError as exc:
            return _error(f"bg: {exc.args[0]}")
        if action == "status":
            return _text(_describe(call), call)
        if action == "cancel":
            call = await registry.cancel(handle)
            if call.state == CANCELLED:
                return _text(f"{handle} cancelled", call)
            return _text(f"{handle} was not running: {_describe(call)}", call)
        timeout = float(params.get("timeout_s") or WAIT_DEFAULT_S)
        if not 0 < timeout <= WAIT_MAX_S:
            return _error(f"bg: timeout_s must be in (0, {WAIT_MAX_S:g}], got {timeout:g}")
        call = await registry.wait(handle, timeout, signal)
        if not call.done.is_set():
            return _text(f"{handle} still running after {timeout:g}s", call)
        if call.state == CANCELLED:
            return _text(f"{handle} was cancelled", call)
        also = "" if call.state == CLAIMED else f" (also delivered as a message: {call.state})"
        verb = (
            "failed"
            if isinstance(call.result, dict) and call.result.get("is_error")
            else "finished"
        )
        return _text(f"{handle} {call.tool} {verb}{also}:\n{_result_text(call.result)}", call)

    return {
        "name": BG_TOOL_NAME,
        "label": "Background jobs",
        "description": (
            "Check, wait for, or cancel a call you started with background=true. "
            "status lists every job (or one, by handle); wait blocks until the job "
            "finishes or timeout_s passes and returns its result, which then does "
            "not also arrive as a message; cancel stops a running job."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "wait", "cancel"]},
                "handle": {"type": "string", "description": "A handle like bg-bash-1."},
                "timeout_s": {
                    "type": "number",
                    "description": f"wait only; default {WAIT_DEFAULT_S:g}, at most {WAIT_MAX_S:g}.",
                },
            },
            "required": ["action"],
        },
        "execute": _execute,
    }


def register_backgrounding(
    api: Any,
    *,
    names: list[str] | None = None,
    definitions: dict[str, dict[str, Any]] | None = None,
) -> BackgroundRegistry:
    """Confer backgrounding on every named tool, register ``bg``, and cancel at shutdown.

    ``session_shutdown`` fires on quit, disable and reload, never on a session
    swap, so outstanding work survives ``new_session`` and is delivered labelled.

    Args:
        api: The bound ``ExtensionAPI``.
        names: Built-in tool names to wrap. Defaults to the config's ``tools``.
        definitions: Extension-shape definitions to wrap, keyed by tool name.

    Returns:
        The registry, so a caller can inspect or cancel outstanding work.

    Raises:
        ValueError: if no tool is named by either source.
    """
    resolved = list(names) if names is not None else list(api.config.get("tools", []))
    extras = dict(definitions or {})
    if not resolved and not extras:
        raise ValueError(
            "register_backgrounding: no tools named. Set "
            '"extensions": {"background": {"tools": ["bash", ...]}} in '
            f"{resolve_tau_home() / 'config.json'}, or pass names=/definitions=."
        )

    def _notify(message: str, level: str) -> None:
        api.ui.notify(message, level)

    def _report(exc: BaseException) -> None:
        _notify(f"background delivery failed: {type(exc).__name__}: {exc}", "error")

    registry = BackgroundRegistry(
        session_id=lambda: api.context.session_id,
        cursor=lambda: api.context.cursor,
        on_error=_report,
    )
    deliver = CursorDelivery(api, _notify)

    cwd = api.context.cwd
    for tool_name in resolved:
        api.register_tool(
            backgroundable(tool_name, registry, deliver, cwd=cwd, control_tool=BG_TOOL_NAME)
        )
    for tool_name, defn in extras.items():
        api.register_tool(
            backgroundable(tool_name, registry, deliver, definition=defn, control_tool=BG_TOOL_NAME)
        )
    api.register_tool(_bg_tool(registry))

    async def _on_shutdown(*_: Any) -> None:
        await registry.cancel_all()

    api.on("session_shutdown", _on_shutdown)
    return registry


def register(api: Any) -> None:
    """File-extension entry point: wrap the tools named in this extension's config."""
    register_backgrounding(api)


__all__ = [
    "BACKGROUND_KEY",
    "BACKGROUND_PARAM",
    "BG_TOOL_NAME",
    "HANDLE_PREFIX",
    "BackgroundCall",
    "BackgroundRegistry",
    "CursorDelivery",
    "backgroundable",
    "completion_text",
    "register",
    "register_backgrounding",
]
