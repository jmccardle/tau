"""``ext_kit.background`` — backgrounding as a property conferred on any tool.

Reference: docs/BACKGROUND-TOOLS.md, whose §6 holds the one open question and §7
what this deliberately does not do.

Backgrounding is not a feature a tool implements. It is a wrapper a tool is handed
to, so ``bash``, ``web_search`` and an extension's own ``recall`` all acquire it
the same way and none of them carries code for it.

The model elects it per call. Nothing here promotes a tool to background on a time
threshold: a threshold is a limit that decides for the model on evidence the model
did not supply, and the judgement it replaces — "can I keep going without this
answer?" — is one only the caller of the tool holds. So the judgement lives in the
added parameter's description, and :data:`BACKGROUND_PARAM` is the whole policy.

Three moves, each on τ's public surface:

* :func:`backgroundable` — wrap a built-in by name, or an extension's own tool
  definition, and return a ``register_tool`` dict whose schema has one added
  boolean. ``background: false`` or absent delegates straight through, so a wrapped
  tool is indistinguishable from the original until the model asks for it.
* :class:`BackgroundRegistry` — the supervised task set. Every outstanding call is
  cancellable, and :meth:`BackgroundRegistry.cancel_all` cancels *and waits*, so a
  session shutdown leaves no orphan.
* :func:`register_backgrounding` — the extension entry point: read the tool names
  out of ``api.config``, register a wrapped tool for each, and bind the lifecycle
  hooks that make the epoch check below work.

**Delivery.** A completed call reaches the model through
``api.submit(..., multitask_strategy="steer")``, which is one call for both of the
cases that matter. With a turn in flight, τ's loop drains the steering queue
immediately before its next provider call, after the current tool results have been
appended — so the completion lands right after the tool result that is already
there. With no turn in flight, the same submission takes the turn slot and runs an
ordinary turn: the wake-up. ``source``/``submitter`` are stamped by τ and cannot be
forged by this module, and ``correlation`` carries the handle, the tool, the
arguments and the start time, so the result is attributable on the wire.

**The epoch, and why a completion can refuse to deliver.** A backgrounded call
outlives the turn that started it, and may outlive its whole session. Delivering
into a session that did not ask for it would attribute the work to a transcript
that never contained the originating call. There is no public way to ask an
``ExtensionAPI`` whether its session is still the current one, so this module keeps
a counter it controls: :class:`BackgroundRegistry` bumps it on every
``session_start``/``session_shutdown``, records the value when a call starts, and
compares on completion. A mismatch RAISES
:class:`BackgroundSessionGoneError` through the registry's error sink rather than
delivering or dropping. What *should* happen at a session boundary — wait, cancel,
or deliver with attribution — is an open product question; until it is answered the
only honest behaviour is to refuse loudly, which is what this does.

**Submission depth.** ``DRIVING_SUBMISSION_DEPTH`` is a ``ContextVar``, so a task
spawned inside a tool's ``execute`` inherits the depth of the turn that spawned it
and ``next_submission_depth`` climbs one per completion that triggers another call.
At ``MAX_SUBMISSION_DEPTH`` submissions raise and the session stops. A backgrounded
call is not self-submission — the work was already running when the turn that reads
it began — so :meth:`BackgroundRegistry.start` clears the variable in the spawned
task's own context.

**Fail-Early.** :func:`backgroundable` raises on a tool that is neither a known
built-in nor a supplied definition, on a definition missing ``execute``, and on a
schema that already declares the added parameter. :func:`register_backgrounding`
raises when its config names no tools.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Depends on steer's built-in resolver rather than a second copy of the name table.
from ext_kit.steer import _resolve_builtin

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


class BackgroundSessionGoneError(RuntimeError):
    """A backgrounded call completed after its originating session was replaced.

    Raised by :meth:`BackgroundRegistry.start`'s completion path, never by the tool
    call that started the work. Carries the handle and the two epochs so the error
    names which call was orphaned rather than only that one was.
    """

    def __init__(self, handle: str, tool: str, started_epoch: int, current_epoch: int) -> None:
        """Build the error.

        Args:
            handle: The orphaned call's handle.
            tool: The wrapped tool's name.
            started_epoch: The registry epoch when the call started.
            current_epoch: The registry epoch at completion.
        """
        super().__init__(
            f"background call {handle!r} ({tool}) completed in epoch {current_epoch} "
            f"but was started in epoch {started_epoch}: its originating session is "
            "gone, so the result has no transcript to attach to and was NOT "
            "delivered. What should happen at a session boundary is undecided; "
            "this refuses rather than misattributing the work."
        )
        self.handle = handle
        self.tool = tool
        self.started_epoch = started_epoch
        self.current_epoch = current_epoch


@dataclass
class BackgroundCall:
    """One outstanding backgrounded call's provenance.

    ``args`` is the arguments the model supplied, minus the backgrounding flag
    itself — the flag is this module's, not the wrapped tool's, and echoing it back
    would suggest the tool saw it.
    """

    handle: str
    tool: str
    args: dict[str, Any]
    started_at_ms: int
    epoch: int

    def correlation(self) -> dict[str, Any]:
        """The ``api.submit`` correlation payload for this call.

        Returns:
            A JSON-safe dict naming the handle, tool, arguments and start time.
        """
        return {
            "background_handle": self.handle,
            "tool": self.tool,
            "args": self.args,
            "started_at_ms": self.started_at_ms,
        }


@dataclass
class BackgroundRegistry:
    """The supervised set of outstanding backgrounded calls.

    One registry per extension. It owns the handles, the tasks, the epoch counter
    that decides whether a completion may be delivered, and the error sink that a
    refused completion is reported through.

    ``on_error`` receives every exception the completion path raises, including
    :class:`BackgroundSessionGoneError`. It defaults to :func:`_raise`, which
    re-raises: a dropped completion is the failure this class exists to prevent, so
    silence is not an available default. An extension with a notify surface should
    pass ``api.ui.notify``-backed sink instead.
    """

    on_error: Callable[[BaseException], None] = field(default=lambda exc: _raise(exc))
    epoch: int = 0
    _tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    _calls: dict[str, BackgroundCall] = field(default_factory=dict)
    _counter: int = 0

    def bump_epoch(self) -> int:
        """Advance the epoch, invalidating every call started before now.

        Bound to ``session_start`` and ``session_shutdown`` by
        :func:`register_backgrounding`. Outstanding tasks are NOT cancelled here —
        cancelling is :meth:`cancel_all`'s job and a shutdown calls both, in that
        order, so a task that is already finishing still reports through the error
        sink rather than vanishing.

        Returns:
            The new epoch.
        """
        self.epoch += 1
        return self.epoch

    def outstanding(self) -> list[BackgroundCall]:
        """Every call that has not yet completed, in start order.

        Returns:
            The outstanding calls' provenance records.
        """
        return list(self._calls.values())

    def _next_handle(self, tool: str) -> str:
        """Mint a handle unique within this registry.

        Args:
            tool: The wrapped tool's name, which the handle embeds so a handle read
                aloud in a transcript says what it refers to.

        Returns:
            A handle of the form ``bg-<tool>-<n>``.
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

        The task is created with ``DRIVING_SUBMISSION_DEPTH`` cleared in its own
        context, so a completion that submits does not inherit the spawning turn's
        depth and a long chain of backgrounded calls cannot walk into
        ``MAX_SUBMISSION_DEPTH``.

        Args:
            tool: The wrapped tool's name.
            args: The model's arguments, with the backgrounding flag removed.
            run: Zero-argument awaitable factory performing the real work.
            deliver: Called with ``(call, result)`` on success, in the task, to hand
                the result to the session. Its exceptions reach ``on_error``.

        Returns:
            The :class:`BackgroundCall` describing the started work.
        """
        handle = self._next_handle(tool)
        call = BackgroundCall(
            handle=handle,
            tool=tool,
            args=args,
            started_at_ms=int(time.time() * 1000),
            epoch=self.epoch,
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
        """Run one backgrounded call, then deliver it or refuse loudly.

        Args:
            call: The provenance record minted by :meth:`start`.
            run: The work.
            deliver: The delivery callable.
        """
        from tau_agent_core.submission import DRIVING_SUBMISSION_DEPTH

        token = DRIVING_SUBMISSION_DEPTH.set(None)
        try:
            result = await _maybe_await(run())
        except asyncio.CancelledError:
            self._forget(call.handle)
            raise
        except BaseException as exc:  # noqa: BLE001 — surfaced through on_error, not swallowed
            self._forget(call.handle)
            DRIVING_SUBMISSION_DEPTH.reset(token)
            self.on_error(exc)
            return
        try:
            if call.epoch != self.epoch:
                raise BackgroundSessionGoneError(call.handle, call.tool, call.epoch, self.epoch)
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

        Cancel-then-await, the discipline ``AgentSession._cancel_task_registry``
        uses, because a cancel that is not awaited leaves the loop shutting down
        while the task is still touching the session.

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
    """Copy ``parameters`` with the backgrounding boolean added.

    The copy is deep because the original belongs to the wrapped tool, which is a
    shared built-in instance in the ``name=`` case.

    Args:
        parameters: The wrapped tool's JSON Schema.

    Returns:
        A new schema carrying :data:`BACKGROUND_KEY`.

    Raises:
        ValueError: if the tool already declares a parameter of that name, which
            would make the flag ambiguous between this wrapper and the tool.
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


def _resolve_target(
    name: str, definition: dict[str, Any] | None, tool: Any
) -> tuple[Callable[..., Any], str, dict[str, Any], str]:
    """Resolve what a wrapped call delegates to, and the schema it advertises.

    Three sources, in precedence order: an explicit extension-shape ``definition``,
    an explicit built-in-shape ``tool`` instance, else the built-in registry. The
    two shapes differ in their execute signature, so each is adapted here rather
    than at the call site.

    Args:
        name: The tool name the wrapper registers under.
        definition: An extension-shape ``register_tool`` dict, whose ``execute``
            takes ``(tool_call_id, params, signal, on_update, ctx)``.
        tool: A built-in-shape instance, whose ``execute`` takes keyword
            ``tool_call_id``/``args``/``signal``/``on_update``.

    Returns:
        ``(delegate, description, parameters, label)`` where ``delegate`` has the
        extension signature.

    Raises:
        ValueError: if both explicit sources are given, if ``definition`` lacks a
            required key, or if ``name`` is not a known built-in and neither
            explicit source is supplied.
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

    original = tool if tool is not None else _resolve_builtin(name)

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
    label: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Build a ``register_tool`` definition for ``name`` with backgrounding conferred.

    The returned tool shadows the original of the same name — τ resolves extension
    tools last, a documented precedence with a fixed winner — so registering it is
    what confers the property. With ``background`` absent or false the call is
    delegated unchanged, which is why conferring this on a tool costs nothing until
    the model elects it.

    Args:
        name: The tool name to register under, and the built-in to resolve when
            neither ``definition`` nor ``tool`` is given.
        registry: The supervised task set the backgrounded call is started in.
        submit: Called as ``submit(call, result)`` from the completed task to hand
            the result to the session — normally a closure over
            ``api.submit(..., multitask_strategy="steer")``.
        definition: An extension-shape tool definition to wrap instead of a built-in.
        tool: A built-in-shape instance to wrap instead of resolving ``name``.
        label: UI label; defaults to the wrapped tool's.
        description: Description sent to the model; defaults to the wrapped tool's.
            The backgrounding judgement is NOT appended here — it belongs to the
            added parameter, so a tool's own description stays about the tool.

    Returns:
        A dict for :meth:`ExtensionAPI.register_tool`.

    Raises:
        ValueError: per :func:`_resolve_target` and :func:`_schema_with_background`.
    """
    delegate, base_description, base_parameters, base_label = _resolve_target(
        name, definition, tool
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


def completion_text(call: BackgroundCall, result: Any) -> str:
    """The utterance a completed backgrounded call is delivered as.

    Names the handle and the tool so the model can tie the result to the call it
    elected, and carries the result's own text. The structured provenance rides
    ``correlation`` rather than this string.

    A tool that reports failure by RETURNING ``is_error`` rather than raising is
    delivered as "failed", not "finished". Both paths exist — the loop's own
    ``AgentToolResult`` carries ``is_error`` — and a blocking call would show the
    model an error result, so a backgrounded one that read as success would make
    backgrounding change the meaning of the answer.

    Args:
        call: The completed call's provenance.
        result: Whatever the wrapped tool returned.

    Returns:
        The message text.
    """
    verb = "failed" if isinstance(result, dict) and result.get("is_error") else "finished"
    return f"[background {call.handle}] {call.tool} {verb}:\n{_result_text(result)}"


def _result_text(result: Any) -> str:
    """Flatten a tool result's text blocks into one string.

    Args:
        result: A result dict in the ``{"content": [...]}`` shape, or anything else.

    Returns:
        The concatenated text blocks, or ``str(result)`` when it is not that shape.
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
    """Confer backgrounding on every named tool and bind the lifecycle hooks.

    The extension entry point. ``names`` defaults to ``api.config["tools"]``, so an
    operator chooses which tools acquire the property in
    ``~/.tau/config.json`` under ``"extensions": {"<stem>": {"tools": [...]}}``
    without editing code.

    Args:
        api: The bound ``ExtensionAPI``.
        names: Built-in tool names to wrap. Defaults to the config's ``tools``.
        definitions: Extension-shape definitions to wrap, keyed by tool name, for
            tools that are not built-ins. Merged with ``names``.

    Returns:
        The registry, so a caller can inspect or cancel outstanding work.

    Raises:
        ValueError: if no tool is named by either source — an extension that
            confers a property on nothing is a configuration mistake, not a no-op.
    """
    resolved = list(names) if names is not None else list(api.config.get("tools", []))
    extras = dict(definitions or {})
    if not resolved and not extras:
        raise ValueError(
            "register_backgrounding: no tools named. Set "
            '"extensions": {"<this extension>": {"tools": ["bash", ...]}} in '
            "~/.tau/config.json, or pass names=/definitions=."
        )

    registry = BackgroundRegistry()

    async def _submit(call: BackgroundCall, result: Any) -> None:
        await api.submit(
            completion_text(call, result),
            multitask_strategy="steer",
            correlation=call.correlation(),
        )

    for tool_name in resolved:
        api.register_tool(backgroundable(tool_name, registry, _submit))
    for tool_name, defn in extras.items():
        api.register_tool(backgroundable(tool_name, registry, _submit, definition=defn))

    api.on("session_start", lambda *_: registry.bump_epoch())

    async def _on_shutdown(*_: Any) -> None:
        registry.bump_epoch()
        await registry.cancel_all()

    api.on("session_shutdown", _on_shutdown)
    return registry


__all__ = [
    "BACKGROUND_KEY",
    "BACKGROUND_PARAM",
    "HANDLE_PREFIX",
    "BackgroundCall",
    "BackgroundRegistry",
    "BackgroundSessionGoneError",
    "backgroundable",
    "completion_text",
    "register_backgrounding",
]
