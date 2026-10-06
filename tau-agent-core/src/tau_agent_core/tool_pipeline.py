"""The one path a tool call takes: validate, ``tool_call`` hook, execute, ``tool_result`` hook.

:class:`~tau_agent_core.agent_loop.AgentLoop` runs a model-issued call through
these functions, and :func:`run_nested_call` runs a call a tool or extension
makes through the same ones, so a permission gate written as a ``tool_call``
hook covers both with no new code (docs/CODE-MODE.md §5.1).

Nested calls are experimental (experiments/tool-exposure/RESULTS.md). A nested
call is not written to the tree as a message, because the model never issued
it; it is recorded on its parent's result as ``details["nested_calls"]``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter
from tau_llm.docs import agent_facing
from tau_llm.tools import validate_tool_arguments
from tau_llm.types import ToolCall

from tau_agent_core.agent_loop_types import PreparedToolCall
from tau_agent_core.events import AgentEvent
from tau_agent_core.tool_output import capture_tool_output, suspend_capture
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolError, ToolHalt

if TYPE_CHECKING:
    from tau_agent_core.extensions.runner import ExtensionRunner

__all__ = [
    "BlockedCall",
    "CURRENT_TOOL_CALL",
    "ErrorCall",
    "NestedCallError",
    "NestedToolResult",
    "ToolCallBlocked",
    "ToolCallFrame",
    "apply_tool_result_hooks",
    "execute_prepared_call",
    "prepare_tool_call",
    "run_nested_call",
]

MAX_NESTING_DEPTH = 8
"""A call this many levels below a model-issued call raises instead of running."""

MAX_RECORDED_NESTED_CALLS = 256
"""Entries kept in one parent's ``nested_calls``; later ones are only counted."""

MAX_RECORDED_ARGS_BYTES = 32 * 1024
"""JSON bytes of arguments kept across one parent's ``nested_calls``."""


@agent_facing(topic="agent-loop")
class BlockedCall:
    """A tool call that was blocked (e.g., argument validation failed).

    ``blocked_by_extension`` names the extension that VETOED the call via a
    ``tool_call`` hook (S50, anchor G11); it is ``None`` for a block that is NOT an
    extension veto (argument-validation failure, fail-closed handler throw) — those
    stay a generic errored result rather than the "⛔ blocked by <ext>" render.
    """

    def __init__(
        self, call: PreparedToolCall, error: str, blocked_by_extension: str | None = None
    ) -> None:
        self.call = call
        self.error = error
        self.blocked_by_extension = blocked_by_extension


@agent_facing(topic="agent-loop")
class ErrorCall:
    """A tool call whose preparation raised ``exception``."""

    def __init__(self, call: PreparedToolCall, exception: Exception) -> None:
        self.call = call
        self.exception = exception

    def result(self) -> AgentToolResult:
        """The call's result, by :meth:`AgentToolResult.from_exception`."""
        return AgentToolResult.from_exception(self.call.name, self.exception, self.call.id)


@dataclass
class ToolCallFrame:
    """The tool call running in this context, and the nested calls it has made.

    Attributes:
        tool_call_id: The running call's id; a nested call's id extends it.
        tool_name: The running tool.
        depth: 0 for a model-issued call, one more per nesting level.
        nested: Recorded nested calls, at most :data:`MAX_RECORDED_NESTED_CALLS`.
        omitted: Nested calls made past that cap.
        args_bytes: JSON bytes of arguments recorded so far.
    """

    tool_call_id: str
    tool_name: str
    depth: int
    nested: list[dict[str, Any]] = field(default_factory=list)
    omitted: int = 0
    args_bytes: int = 0
    _children: int = 0

    def next_child_id(self) -> str:
        """``<this id>/<n>``, numbering this call's nested calls from 1."""
        self._children += 1
        return f"{self.tool_call_id}/{self._children}"

    def record(self, entry: dict[str, Any], args: dict[str, Any]) -> None:
        """Keep ``entry`` within the caps; its arguments only while the byte budget lasts."""
        if len(self.nested) >= MAX_RECORDED_NESTED_CALLS:
            self.omitted += 1
            return
        encoded = json.dumps(args, default=str, ensure_ascii=False)
        if self.args_bytes + len(encoded) <= MAX_RECORDED_ARGS_BYTES:
            self.args_bytes += len(encoded)
            entry["args"] = args
        else:
            entry["args_omitted_bytes"] = len(encoded)
        self.nested.append(entry)

    def as_details(self) -> dict[str, Any]:
        """The ``nested_calls`` (and ``nested_calls_omitted``) keys for the result's details."""
        out: dict[str, Any] = {}
        if self.nested:
            out["nested_calls"] = self.nested
        if self.omitted:
            out["nested_calls_omitted"] = self.omitted
        return out


CURRENT_TOOL_CALL: ContextVar[ToolCallFrame | None] = ContextVar(
    "tau_current_tool_call", default=None
)
"""The tool call executing in this context; ``None`` outside one."""


@agent_facing(topic="tools")
class NestedCallError(ToolError):
    """A nested tool call failed; the message is the text the tool reported.

    A :class:`ToolError`, so a tool that lets it propagate reports the same text
    to the model as its own failure.

    Attributes:
        result: The failed call's result, or ``None`` if it never ran.
    """

    def __init__(self, message: str, result: AgentToolResult | None = None) -> None:
        super().__init__(message)
        self.result = result


@agent_facing(topic="tools")
class ToolCallBlocked(NestedCallError):
    """A ``tool_call`` hook vetoed a nested call.

    Attributes:
        blocked_by: The extension that vetoed it.
    """

    def __init__(self, message: str, blocked_by: str) -> None:
        super().__init__(message)
        self.blocked_by = blocked_by


@agent_facing(topic="tools")
@dataclass(frozen=True)
class NestedToolResult:
    """What a nested tool call returns to its caller.

    Attributes:
        tool_call_id: The nested call's id, ``<parent id>/<n>`` inside a tool.
        tool_name: The tool that ran.
        value: The structured value (JSON-compatible, shaped by the tool's
            ``output_schema``), or ``None`` for a text-only tool or when a
            ``tool_result`` hook rewrote the content without rewriting it.
        content: The content blocks the model would have read.
        details: The tool's details, after hooks.
    """

    tool_call_id: str
    tool_name: str
    value: Any
    content: list[dict[str, Any]]
    details: dict[str, Any] | None

    @property
    def text(self) -> str:
        """The text blocks of :attr:`content`, joined by newlines."""
        return "\n".join(str(b.get("text", "")) for b in self.content if b.get("type") == "text")

    def as_type(self, tp: Any) -> Any:
        """:attr:`value` validated as ``tp`` (a ``TypedDict``, dataclass or model).

        Raises:
            ValueError: the tool returned no structured value.
            pydantic.ValidationError: the value does not fit ``tp``.
        """
        if self.value is None:
            raise ValueError(f"{self.tool_name} returned no structured value; read .text instead")
        return TypeAdapter(tp).validate_python(self.value)


def _blocked(tool_call: ToolCall, error: str, extension: str | None = None) -> BlockedCall:
    """A :class:`BlockedCall` for ``tool_call`` with no arguments kept."""
    return BlockedCall(
        call=PreparedToolCall(id=tool_call.id, name=tool_call.name, arguments={}),
        error=error,
        blocked_by_extension=extension,
    )


@agent_facing(topic="agent-loop")
async def prepare_tool_call(
    tools: Mapping[str, AgentTool],
    dispatcher: ExtensionRunner | None,
    tool_call: ToolCall,
    *,
    parent_tool_call_id: str | None = None,
) -> PreparedToolCall | BlockedCall | ErrorCall:
    """Validate ``tool_call``'s arguments and run the ``tool_call`` hook.

    A nested call's hook event carries ``parent_tool_call_id``; a model-issued
    call's event has no such key.

    Returns:
        The prepared call, a :class:`BlockedCall` (validation failed, a handler
        vetoed, or a handler raised: fail-closed), or an :class:`ErrorCall`.
    """
    try:
        call_name = tool_call.name
        call_args = tool_call.arguments
        if call_name in tools:
            validate_tool_arguments(tools[call_name], call_args)
        input_args = call_args if isinstance(call_args, dict) else {}

        if dispatcher is not None and dispatcher.has_handlers("tool_call"):
            event: dict[str, Any] = {
                "type": "tool_call",
                "tool_call_id": tool_call.id,
                "tool_name": call_name,
                "input": input_args,
            }
            if parent_tool_call_id is not None:
                event["parent_tool_call_id"] = parent_tool_call_id
            try:
                hook_result = await dispatcher.emit_tool_call(event)
            except Exception as hook_err:
                return _blocked(tool_call, f"Extension failed, blocking execution: {hook_err}")
            if hook_result and hook_result.get("block"):
                return _blocked(
                    tool_call,
                    hook_result.get("reason") or "Tool execution was blocked",
                    hook_result.get("extension"),
                )
            input_args = event["input"]

        return PreparedToolCall(id=tool_call.id, name=call_name, arguments=input_args)
    except ValueError as e:
        return _blocked(tool_call, str(e))
    except Exception as e:
        return ErrorCall(
            call=PreparedToolCall(id=tool_call.id, name=tool_call.name, arguments={}),
            exception=e,
        )


def _wrap_raw(call: PreparedToolCall, raw: Any) -> AgentToolResult:
    """Normalize what a tool's ``execute`` returned into an :class:`AgentToolResult`."""
    if isinstance(raw, AgentToolResult):
        raw.tool_name = call.name
        raw.tool_call_id = call.id
        return raw
    if isinstance(raw, dict):
        content = raw.get("content", "")
        details = raw.get("details")
        return AgentToolResult(
            tool_name=call.name,
            tool_call_id=call.id,
            content=content
            if isinstance(content, list)
            else [{"type": "text", "text": str(content)}],
            details=details if isinstance(details, dict) else None,
            value=raw.get("value"),
            is_error=raw.get("is_error", False),
            terminate=raw.get("terminate", False),
        )
    content_list = raw if isinstance(raw, list) else [{"type": "text", "text": str(raw)}]
    return AgentToolResult(tool_name=call.name, tool_call_id=call.id, content=content_list)


@agent_facing(topic="agent-loop")
async def execute_prepared_call(
    tools: Mapping[str, AgentTool], call: PreparedToolCall, signal: Any
) -> AgentToolResult:
    """Run a prepared call; any exception becomes an error result.

    While the tool runs, :data:`CURRENT_TOOL_CALL` names it and its prints are
    captured. Nested calls it made land in ``details["nested_calls"]`` and its
    output in ``details["captured_output"]``.
    """
    tool = tools.get(call.name)
    if tool is None:
        return AgentToolResult.from_error(call.name, f"Unknown tool: {call.name}", call.id)
    parent = CURRENT_TOOL_CALL.get()
    frame = ToolCallFrame(call.id, call.name, depth=0 if parent is None else parent.depth + 1)
    token = CURRENT_TOOL_CALL.set(frame)
    try:
        with capture_tool_output() as captured:
            try:
                raw = await tool.execute(tool_call_id=call.id, args=call.arguments, signal=signal)
                result = _wrap_raw(call, raw)
            except Exception as e:
                result = AgentToolResult.from_exception(call.name, e, call.id)
    finally:
        CURRENT_TOOL_CALL.reset(token)
    extra = frame.as_details()
    printed = captured.as_details()
    if printed is not None:
        extra["captured_output"] = printed
    if extra:
        result.details = {**(result.details or {}), **extra}
    return result


@agent_facing(topic="agent-loop")
async def apply_tool_result_hooks(
    dispatcher: ExtensionRunner | None,
    result: AgentToolResult,
    input_args: dict[str, Any] | None = None,
    *,
    parent_tool_call_id: str | None = None,
) -> AgentToolResult:
    """Apply the ``tool_result`` mutating hook to ``result``.

    A patched-to-``None`` field keeps its original value, so a handler clears
    ``details`` by patching ``{}``. The event carries ``value`` when the tool
    returned one, and a handler that rewrites ``content`` without rewriting
    ``value`` drops it: a value that no longer matches the text it came with
    would let a redaction leak to a nested caller.
    """
    if dispatcher is None or not dispatcher.has_handlers("tool_result"):
        return result
    event: dict[str, Any] = {
        "type": "tool_result",
        "tool_name": result.tool_name,
        "tool_call_id": result.tool_call_id,
        "input": input_args if input_args is not None else {},
        "content": result.content,
        "details": result.details,
        "is_error": result.is_error,
    }
    if result.value is not None:
        event["value"] = result.value
    if parent_tool_call_id is not None:
        event["parent_tool_call_id"] = parent_tool_call_id
    patch = await dispatcher.emit_tool_result(event)
    if patch is not None:
        patched = set(patch.get("patched", ()))
        if patch.get("content") is not None:
            result.content = patch["content"]
        if patch.get("details") is not None:
            result.details = patch["details"]
        if patch.get("is_error") is not None:
            result.is_error = patch["is_error"]
        if "value" in patched:
            result.value = patch.get("value")
        elif "content" in patched:
            result.value = None
    return result


Emit = Callable[[AgentEvent], Awaitable[None]]


async def _emit_uncaptured(emit: Emit, event: AgentEvent) -> None:
    """Emit outside the calling tool's output capture, so a head's writer is not swallowed."""
    with suspend_capture():
        await emit(event)


@agent_facing(topic="tools")
async def run_nested_call(
    *,
    tools: Mapping[str, AgentTool],
    dispatcher: ExtensionRunner | None,
    emit: Emit,
    signal: Any,
    name: str,
    args: dict[str, Any],
    parent_tool_call_id: str | None = None,
) -> NestedToolResult:
    """Run one call a tool or extension makes, through the model-issued call's pipeline.

    Inside a running tool, the parent is :data:`CURRENT_TOOL_CALL` and the call
    is recorded on it. Outside one, ``parent_tool_call_id`` may name a parent the
    caller vouches for (a sandbox host running a script's call), and nothing
    records it. ``tool_execution_start``/``_end`` are emitted with
    ``parent_tool_call_id`` set.

    Args:
        tools: The tools a nested call may reach, by name.
        dispatcher: The hook runner, or ``None`` for no hooks.
        emit: Where the ``tool_execution_*`` events go.
        signal: The abort signal the tool receives.
        name: The tool to call.
        args: Its arguments.
        parent_tool_call_id: The parent, when not inside a running tool.

    Returns:
        The tool's structured value, content and details.

    Raises:
        ToolCallBlocked: a ``tool_call`` hook vetoed it.
        ToolHalt: the tool asked to stop the turn.
        NestedCallError: the tool is not reachable from here, its arguments
            failed validation, nesting is too deep, or it reported an error.
        ValueError: ``parent_tool_call_id`` contradicts the running tool.
    """
    frame = CURRENT_TOOL_CALL.get()
    if frame is not None:
        if parent_tool_call_id not in (None, frame.tool_call_id):
            raise ValueError(
                f"parent_tool_call_id {parent_tool_call_id!r} is not the running call "
                f"{frame.tool_call_id!r}; inside a tool the parent is implied"
            )
        parent: str | None = frame.tool_call_id
        if frame.depth + 1 > MAX_NESTING_DEPTH:
            raise NestedCallError(
                f"{name}: nested tool calls are limited to {MAX_NESTING_DEPTH} levels"
            )
        call_id = frame.next_child_id()
    else:
        parent = parent_tool_call_id
        suffix = uuid.uuid4().hex[:12]
        call_id = f"{parent}/{suffix}" if parent else f"ext-{suffix}"
    args = dict(args)
    started = time.monotonic()

    def record(status: str, **extra: Any) -> None:
        if frame is None:
            return
        entry = {
            "tool_call_id": call_id,
            "tool_name": name,
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            **extra,
        }
        frame.record(entry, args)

    if name not in tools:
        record("unavailable")
        raise NestedCallError(f"Tool {name!r} is not available to a nested call")

    def event(kind: str, **fields: Any) -> AgentEvent:
        return AgentEvent(
            type=kind,  # type: ignore[arg-type]
            timestamp=int(time.time() * 1000),
            tool_call_id=call_id,
            tool_name=name,
            parent_tool_call_id=parent,
            **fields,
        )

    await _emit_uncaptured(emit, event("tool_execution_start", args=args))
    prepared = await prepare_tool_call(
        tools,
        dispatcher,
        ToolCall(id=call_id, name=name, arguments=args),
        parent_tool_call_id=parent,
    )
    if isinstance(prepared, BlockedCall):
        blocked_by = prepared.blocked_by_extension
        await _emit_uncaptured(
            emit,
            event(
                "tool_execution_end",
                result=prepared.error,
                is_error=True,
                blocked=blocked_by is not None,
                blocked_by=blocked_by,
            ),
        )
        record("blocked", error=prepared.error, blocked_by=blocked_by)
        if blocked_by is not None:
            raise ToolCallBlocked(prepared.error, blocked_by)
        raise NestedCallError(prepared.error)
    if isinstance(prepared, ErrorCall):
        result = prepared.result()
    else:
        result = await execute_prepared_call(tools, prepared, signal)
        result = await apply_tool_result_hooks(
            dispatcher, result, prepared.arguments, parent_tool_call_id=parent
        )
    await _emit_uncaptured(
        emit,
        event(
            "tool_execution_end",
            result=result.content,
            details=result.details,
            is_error=result.is_error,
        ),
    )
    nested = NestedToolResult(
        tool_call_id=call_id,
        tool_name=name,
        value=result.value,
        content=list(result.content),
        details=result.details,
    )
    if result.is_error:
        record("error", error=nested.text[:200])
        if result.terminate:
            raise ToolHalt(nested.text)
        raise NestedCallError(nested.text, result)
    record("ok")
    return nested
