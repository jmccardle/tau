"""``background`` — backgrounding as a property conferred on any tool.

Reference: docs/BACKGROUND-TOOLS.md. §1 says why this is an extension and not a
core tool; §6 is the session-boundary rule; §7 is what it does not do.

Load it like any file extension and name the tools in config::

    "extensions": {"background": {"tools": ["bash", "grep"]}}

Three moves, each on τ's public surface:

* :func:`backgroundable` — wrap a built-in by name, or an extension's own tool
  definition, into a ``register_tool`` dict whose schema has one added boolean.
  ``background`` absent or false delegates straight through.
* :class:`BackgroundRegistry` — the supervised task set. Every call records the
  session it started in, and :meth:`BackgroundRegistry.cancel_all` cancels *and
  waits*, so a shutdown leaves no orphan.
* :func:`register_backgrounding` — the entry point :func:`register` calls.

The model elects backgrounding per call; nothing promotes a tool on a timer.
:data:`BACKGROUND_PARAM`'s description is the whole policy.

**Delivery** is ``api.submit(..., multitask_strategy="steer")``: mid-turn it lands
after the current tool result, and with no turn in flight it runs a turn, which is
the wake-up. **A result outlives its session**: if the session id at delivery
differs from the one at start, the result is delivered into the current session
anyway, and its text and ``correlation`` say which session, tool, arguments and
start time it came from.

**Submission depth.** :meth:`BackgroundRegistry.start` clears
``DRIVING_SUBMISSION_DEPTH`` in the spawned task, because a completion is not
self-submission and inheriting the turn's depth would let a chain of eleven kill
the session.

**Fail-Early.** :func:`backgroundable` raises on a tool that is neither a known
built-in nor a supplied definition, on a definition missing ``execute``, and on a
schema that already declares the added parameter. :func:`register_backgrounding`
raises when its config names no tools.
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


@dataclass
class BackgroundCall:
    """One backgrounded call's provenance.

    ``args`` excludes the backgrounding flag: the flag is this module's, and
    echoing it back would suggest the tool saw it.
    """

    handle: str
    tool: str
    args: dict[str, Any]
    started_at_ms: int
    origin_session_id: str

    def correlation(self) -> dict[str, Any]:
        """The provenance as a JSON-safe ``correlation`` payload.

        Returns:
            Handle, tool, arguments, start time and originating session id.
        """
        return {
            "background_handle": self.handle,
            "tool": self.tool,
            "args": self.args,
            "started_at_ms": self.started_at_ms,
            "origin_session_id": self.origin_session_id,
        }


@dataclass
class BackgroundRegistry:
    """The supervised set of outstanding backgrounded calls, one per extension.

    Args:
        session_id: Returns the id of the conversation the session is on now; read
            once per call at start. ``lambda: api.context.session_id`` in a real
            session.
        on_error: Receives every exception the completion path raises. Defaults to
            re-raising, because a dropped completion is not an available default.
    """

    session_id: Callable[[], str]
    on_error: Callable[[BaseException], None] = field(default=lambda exc: _raise(exc))
    _tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    _calls: dict[str, BackgroundCall] = field(default_factory=dict)
    _counter: int = 0

    def outstanding(self) -> list[BackgroundCall]:
        """Every call that has not yet completed, in start order.

        Returns:
            The outstanding calls' provenance records.
        """
        return list(self._calls.values())

    def _next_handle(self, tool: str) -> str:
        """Mint ``bg-<tool>-<n>``, unique within this registry.

        Args:
            tool: The wrapped tool's name, so a handle read aloud says what it is.

        Returns:
            The handle.
        """
        self._counter += 1
        return f"{HANDLE_PREFIX}-{tool}-{self._counter}"

    def start(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        run: Callable[[], Any],
        deliver: Callable[[BackgroundCall, Any], Any],
    ) -> BackgroundCall:
        """Spawn ``run`` as a supervised task and return its provenance at once.

        Args:
            tool: The wrapped tool's name.
            args: The model's arguments, with the backgrounding flag removed.
            run: Zero-argument awaitable factory performing the real work.
            deliver: Called with ``(call, result)`` on success, in the task. Its
                exceptions reach ``on_error``.

        Returns:
            The :class:`BackgroundCall` describing the started work.
        """
        handle = self._next_handle(tool)
        call = BackgroundCall(
            handle=handle,
            tool=tool,
            args=args,
            started_at_ms=int(time.time() * 1000),
            origin_session_id=self.session_id(),
        )
        self._calls[handle] = call
        self._tasks[handle] = asyncio.create_task(self._supervise(call, run, deliver), name=handle)
        return call

    async def _supervise(
        self,
        call: BackgroundCall,
        run: Callable[[], Any],
        deliver: Callable[[BackgroundCall, Any], Any],
    ) -> None:
        """Run one backgrounded call, then deliver it; failures go to ``on_error``.

        Args:
            call: The provenance record minted by :meth:`start`.
            run: The work.
            deliver: The delivery callable.
        """
        from tau_agent_core.submission import DRIVING_SUBMISSION_DEPTH

        token = DRIVING_SUBMISSION_DEPTH.set(None)
        try:
            result = await _maybe_await(run())
            await _maybe_await(deliver(call, result))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — surfaced through on_error, not swallowed
            self.on_error(exc)
        finally:
            self._forget(call.handle)
            DRIVING_SUBMISSION_DEPTH.reset(token)

    def _forget(self, handle: str) -> None:
        """Drop a finished call's records.

        Args:
            handle: The call's handle.
        """
        self._calls.pop(handle, None)
        self._tasks.pop(handle, None)

    async def cancel_all(self) -> int:
        """Cancel every outstanding task and wait for each to unwind.

        Returns:
            How many tasks were cancelled.
        """
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException:  # noqa: BLE001 — already reported through on_error
                pass
        self._tasks.clear()
        self._calls.clear()
        return len(tasks)


def _raise(exc: BaseException) -> None:
    """Re-raise ``exc`` — the default error sink.

    Args:
        exc: The exception the completion path produced.

    Raises:
        BaseException: always, the argument unchanged.
    """
    raise exc


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, else return it as-is.

    Args:
        value: A value or an awaitable.

    Returns:
        The resolved value.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def _schema_with_background(parameters: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy ``parameters`` with the backgrounding boolean added.

    Args:
        parameters: The wrapped tool's JSON Schema, which may be a shared
            built-in's dict and so is never mutated.

    Returns:
        A new schema carrying :data:`BACKGROUND_KEY`.

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

    Args:
        name: A built-in tool name.
        cwd: The working directory the tool resolves paths against.

    Returns:
        The tool instance.

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

    Args:
        name: The tool name the wrapper registers under.
        definition: A ``register_tool`` dict; ``execute`` takes ``(tool_call_id,
            params, signal, on_update, ctx)``.
        tool: A built-in instance; ``execute`` takes keyword ``tool_call_id``/
            ``args``/``signal``/``on_update``.
        cwd: Where a built-in resolved by name works; required only in that case.

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
) -> dict[str, Any]:
    """Build a ``register_tool`` definition for ``name`` with backgrounding conferred.

    The result shadows the built-in of the same name, because τ resolves extension
    tools last. An extension's tool cannot be shadowed (``register_tool`` raises on
    a duplicate), so for one of those pass its ``definition``.

    Args:
        name: The tool name to register under, and the built-in to resolve when
            neither ``definition`` nor ``tool`` is given.
        registry: The supervised task set the call is started in.
        submit: Called as ``submit(call, result)`` from the completed task.
        definition: An extension-shape tool definition to wrap.
        tool: A built-in-shape instance to wrap.
        cwd: The working directory for a built-in resolved by ``name``.
        label: UI label; defaults to the wrapped tool's.
        description: Description sent to the model; defaults to the wrapped tool's.
            The backgrounding judgement lives on the added parameter, not here.

    Returns:
        A dict for :meth:`ExtensionAPI.register_tool`.

    Raises:
        ValueError: per :func:`_resolve_target` and :func:`_schema_with_background`.
    """
    delegate, base_description, base_parameters, base_label = _resolve_target(
        name, definition, tool, cwd
    )
    parameters = _schema_with_background(base_parameters)

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

        def _run() -> Any:
            return delegate(tool_call_id, forwarded, signal, on_update, ctx)

        call = registry.start(tool=name, args=forwarded, run=_run, deliver=submit)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Started {name} in the background as {call.handle}. "
                        "Keep going; the result will arrive as a message."
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


def completion_text(call: BackgroundCall, result: Any, *, current_session_id: str) -> str:
    """The utterance a completed call is delivered as.

    A result returned with ``is_error`` reads "failed", as a blocking call would
    show it. A result crossing into a different session says where it came from,
    because the conversation reading it never contained the call.

    Args:
        call: The completed call's provenance.
        result: Whatever the wrapped tool returned.
        current_session_id: The session the result is being delivered into.

    Returns:
        The message text.
    """
    verb = "failed" if isinstance(result, dict) and result.get("is_error") else "finished"
    body = _result_text(result)
    if current_session_id == call.origin_session_id:
        return f"[background {call.handle}] {call.tool} {verb}:\n{body}"
    started = datetime.fromtimestamp(call.started_at_ms / 1000, tz=timezone.utc)
    return (
        f"[background {call.handle}, from an earlier session] {call.tool} {verb}. "
        f"It was started in session {call.origin_session_id} at "
        f"{started.isoformat(timespec='seconds')} with arguments "
        f"{json.dumps(call.args, sort_keys=True, default=str)}, and that "
        f"conversation has ended since:\n{body}"
    )


def _result_text(result: Any) -> str:
    """Flatten a tool result's text blocks into one string.

    Args:
        result: A ``{"content": [...]}`` result, or anything else.

    Returns:
        The concatenated text blocks, or ``str(result)`` for any other shape.
    """
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


def register_backgrounding(
    api: Any,
    *,
    names: list[str] | None = None,
    definitions: dict[str, dict[str, Any]] | None = None,
) -> BackgroundRegistry:
    """Confer backgrounding on every named tool and cancel outstanding work at shutdown.

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
            "~/.tau/config.json, or pass names=/definitions=."
        )

    registry = BackgroundRegistry(session_id=lambda: api.context.session_id)

    async def _submit(call: BackgroundCall, result: Any) -> None:
        current = api.context.session_id
        await api.submit(
            completion_text(call, result, current_session_id=current),
            multitask_strategy="steer",
            correlation={
                **call.correlation(),
                "crossed_session": current != call.origin_session_id,
            },
        )

    cwd = api.context.cwd
    for tool_name in resolved:
        api.register_tool(backgroundable(tool_name, registry, _submit, cwd=cwd))
    for tool_name, defn in extras.items():
        api.register_tool(backgroundable(tool_name, registry, _submit, definition=defn))

    async def _on_shutdown(*_: Any) -> None:
        await registry.cancel_all()

    api.on("session_shutdown", _on_shutdown)
    return registry


def register(api: Any) -> None:
    """File-extension entry point: wrap the tools named in this extension's config.

    Args:
        api: The bound ``ExtensionAPI``.
    """
    register_backgrounding(api)


__all__ = [
    "BACKGROUND_KEY",
    "BACKGROUND_PARAM",
    "HANDLE_PREFIX",
    "BackgroundCall",
    "BackgroundRegistry",
    "backgroundable",
    "completion_text",
    "register",
    "register_backgrounding",
]
