"""Trace one full τ agent turn, as executed, from the ``tau -p`` head down to the wire.

What it measures: it runs ``tau_coding_agent.cli.main`` (the ``tau`` console entry)
with ``-p --mode json`` against a scripted model and records, on one global
sequence counter:

- (a) the call tree of every function in ``tau_llm`` / ``tau_agent_core`` /
  ``tau_coding_agent`` / ``tau_jmfts`` (``sys.monitoring``), per asyncio task or
  thread, with every ``raise`` and every handled exception inside τ code;
- (b) every extension hook firing, from a test extension that subscribes to every
  hook in ``ExtensionRunner.HOOK_EVENTS``/``LIFECYCLE_EVENTS``, every ``AgentEvent``
  type, and every string channel the session emits on (found by AST scan);
- (c) every ``EventBus.emit``/``emit_channel`` with its subscriber count;
- (d) every session-log write (``Session._append_at_now``/``finalize`` and the
  in-memory log's), with type, id and parent id;
- (e) cursor turn-lock acquire/release, ``AbortSignal.abort``, cursor and
  ``LoopPosition`` state transitions, and task creation.

The model is the real ``OpenAICompletionsProvider`` with its HTTP client swapped
for an ``httpx.MockTransport`` that serves scripted SSE, so nothing leaves the
process. τ's home, ``HOME`` and the working directory are fresh temporary
directories, so the user's ``~/.tau`` is never read or written.

Usage:
    python scripts/arch/trace_turn.py [--variant NAME|all] [--out DIR]

Outputs (per variant, under ``<out>/<variant>/``): ``trace.json`` (raw records),
``calltree.txt`` (indented, collapsed), ``sequence.mmd`` (mermaid sequenceDiagram,
the subset ``scripts/arch/seq_svg.py`` renders), ``outcome.json`` (how the turn
ended: exit code, stderr, entries, terminal events), ``head_stdout.jsonl`` and
``session_log.jsonl``. With ``--variant all``: ``summary.json``/``summary.md``
over every variant, and ``errors.json``/``errors.md`` (every try/except, raise
and early return in the functions the base trace ran, marked ran/read).
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import dis
import functools
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_PACKAGES = ("tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts")
MOCK_BASE_URL = "mock://scripted-model/v1"
MODEL_KEY = "trace-model"
PROMPT = "Read note.txt and run echo hi, then tell me what you found."
IGNORED_EXCEPTIONS = ("StopIteration", "StopAsyncIteration", "GeneratorExit")

VARIANTS: dict[str, str] = {
    "base": "reasoning, a remark, read + bash in one message, then a final answer",
    "tool-raises": "an extension tool whose execute raises RuntimeError",
    "http-error": "HTTP 503 (retry-after 10ms) then HTTP 400 before any output",
    "stream-error-frame": "an SSE error frame after reasoning and text deltas",
    "stream-generic": "the response body raises RuntimeError mid-stream",
    "hook-raises-tool_call": "the extension's tool_call handler raises",
    "hook-raises-entry_written": "the extension's entry_written handler raises",
    "hook-raises-notify": "the extension's message_end notify handler raises",
    "abort-in-tool": "SIGINT while bash runs sleep 5",
    "bad-args": "the read call's arguments are not valid JSON",
    "unknown-tool": "the second call names a tool that does not exist",
}


@dataclass
class TraceState:
    """Every record of one run, ordered by one sequence counter."""

    variant: str
    roots: tuple[str, ...]
    seq: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    calls: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    states: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    stacks: dict[str, list[int]] = field(default_factory=dict)
    frames: dict[int, int] = field(default_factory=dict)
    task_labels: dict[int, str] = field(default_factory=dict)
    code_info: dict[Any, tuple[str, str, int] | None] = field(default_factory=dict)
    lock_labels: dict[int, str] = field(default_factory=dict)
    task_counter: int = 0
    anomalies: int = 0

    def label_task(self, task: Any) -> str:
        """Give ``task`` a stable ``T<n>:<name>`` label; caller holds ``lock``."""
        self.task_counter += 1
        label = f"T{self.task_counter}:{task.get_name()}"
        self.task_labels[id(task)] = label
        return label

    def next_seq(self) -> int:
        """Return the next global sequence number; caller holds ``lock``."""
        self.seq += 1
        return self.seq


STATE: TraceState | None = None


def trace_state() -> TraceState:
    """The running trace's state; raises when no trace is running."""
    if STATE is None:
        raise RuntimeError("trace_turn: no trace is running (STATE is None)")
    return STATE


def trace_task_key(state: TraceState) -> str:
    """Name the asyncio task (or thread) the current code runs on."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is None:
        return f"thread:{threading.current_thread().name}"
    label = state.task_labels.get(id(task))
    return label if label is not None else state.label_task(task)


def trace_top_call(state: TraceState, key: str | None = None) -> int | None:
    """The innermost τ call on the current task's stack, or None."""
    stack = state.stacks.get(key or trace_task_key(state))
    return stack[-1] if stack else None


def trace_code_info(state: TraceState, code: Any) -> tuple[str, str, int] | None:
    """``(module-relative path, qualname, first line)`` for τ code, else None (cached)."""
    info = state.code_info.get(code, False)
    if info is not False:
        return info  # type: ignore[return-value]
    filename = code.co_filename
    result = None
    for root in state.roots:
        if filename.startswith(root):
            result = (os.path.relpath(filename, REPO_ROOT), code.co_qualname, code.co_firstlineno)
            break
    state.code_info[code] = result
    return result


_LINE_TABLES: dict[Any, list[tuple[int, int]]] = {}


def trace_offset_line(code: Any, offset: int) -> int:
    """The first source line at or after bytecode ``offset`` (a handler target has none of its own)."""
    table = _LINE_TABLES.get(code)
    if table is None:
        table = [
            (ins.offset, ins.positions.lineno)
            for ins in dis.get_instructions(code)
            if ins.positions is not None and ins.positions.lineno is not None
        ]
        _LINE_TABLES[code] = table
    for at, line in table:
        if at >= offset:
            return line
    return code.co_firstlineno


def trace_install_monitoring(state: TraceState) -> None:
    """Register ``sys.monitoring`` callbacks that build the per-task τ call stacks."""
    mon = sys.monitoring
    tool = mon.PROFILER_ID
    mon.use_tool_id(tool, "trace_turn")
    events = mon.events

    def on_start(code: Any, offset: int) -> Any:
        """A τ frame starts: push a call record."""
        info = trace_code_info(state, code)
        if info is None:
            return mon.DISABLE
        frame = sys._getframe(1)
        with state.lock:
            key = trace_task_key(state)
            stack = state.stacks.setdefault(key, [])
            call_id = len(state.calls)
            state.calls.append(
                {
                    "id": call_id,
                    "seq": state.next_seq(),
                    "task": key,
                    "depth": len(stack),
                    "parent": stack[-1] if stack else None,
                    "file": info[0],
                    "line": info[2],
                    "name": info[1],
                    "end_seq": None,
                    "outcome": None,
                    "suspensions": 0,
                }
            )
            stack.append(call_id)
            state.frames[id(frame)] = call_id
        return None

    def on_resume(code: Any, offset: int, *rest: Any) -> Any:
        """A suspended τ frame resumes: push it back on its task's stack."""
        if trace_code_info(state, code) is None:
            return mon.DISABLE if not rest else None
        frame = sys._getframe(1)
        with state.lock:
            call_id = state.frames.get(id(frame))
            if call_id is None:
                state.anomalies += 1
                return None
            state.stacks.setdefault(trace_task_key(state), []).append(call_id)
        return None

    def pop(code: Any, final: bool, outcome: str) -> None:
        """Pop the current τ frame; ``final`` when it will not run again."""
        frame = sys._getframe(2)
        with state.lock:
            call_id = state.frames.get(id(frame))
            stack = state.stacks.get(trace_task_key(state), [])
            if call_id is None or not stack:
                state.anomalies += 1
                return
            if stack[-1] == call_id:
                stack.pop()
            elif call_id in stack:
                state.anomalies += 1
                stack.remove(call_id)
            if final:
                state.frames.pop(id(frame), None)
                record = state.calls[call_id]
                record["end_seq"] = state.next_seq()
                record["outcome"] = outcome
            else:
                state.calls[call_id]["suspensions"] += 1

    def on_return(code: Any, offset: int, retval: Any) -> Any:
        """A τ frame returns."""
        if trace_code_info(state, code) is None:
            return mon.DISABLE
        pop(code, True, "return")
        return None

    def on_yield(code: Any, offset: int, retval: Any) -> Any:
        """A τ frame suspends (await or yield)."""
        if trace_code_info(state, code) is None:
            return mon.DISABLE
        pop(code, False, "yield")
        return None

    def on_unwind(code: Any, offset: int, exc: BaseException) -> Any:
        """A τ frame exits on an exception."""
        if trace_code_info(state, code) is None:
            return None
        pop(code, True, f"raise {type(exc).__name__}")
        return None

    def exception_record(kind: str, code: Any, offset: int, exc: BaseException) -> None:
        """Record a raise or a handled exception inside τ code."""
        info = trace_code_info(state, code)
        if info is None or type(exc).__name__ in IGNORED_EXCEPTIONS:
            return
        with state.lock:
            key = trace_task_key(state)
            state.exceptions.append(
                {
                    "seq": state.next_seq(),
                    "kind": kind,
                    "task": key,
                    "call": trace_top_call(state, key),
                    "file": info[0],
                    "line": trace_offset_line(code, offset),
                    "function": info[1],
                    "type": type(exc).__name__,
                    "mro": [k.__name__ for k in type(exc).__mro__],
                    "message": str(exc)[:240],
                }
            )

    mon.register_callback(tool, events.PY_START, on_start)
    mon.register_callback(tool, events.PY_RESUME, on_resume)
    mon.register_callback(tool, events.PY_THROW, on_resume)
    mon.register_callback(tool, events.PY_RETURN, on_return)
    mon.register_callback(tool, events.PY_YIELD, on_yield)
    mon.register_callback(tool, events.PY_UNWIND, on_unwind)
    mon.register_callback(tool, events.RAISE, lambda c, o, e: exception_record("raise", c, o, e))
    mon.register_callback(
        tool, events.EXCEPTION_HANDLED, lambda c, o, e: exception_record("handled", c, o, e)
    )
    mon.set_events(
        tool,
        events.PY_START
        | events.PY_RESUME
        | events.PY_THROW
        | events.PY_RETURN
        | events.PY_YIELD
        | events.PY_UNWIND
        | events.RAISE
        | events.EXCEPTION_HANDLED,
    )


def trace_uninstall_monitoring() -> None:
    """Stop monitoring and free the tool id."""
    mon = sys.monitoring
    mon.set_events(mon.PROFILER_ID, 0)
    mon.free_tool_id(mon.PROFILER_ID)


def trace_record(state: TraceState, bucket: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    """Append one record to ``bucket`` with seq, task and current τ call."""
    with state.lock:
        key = trace_task_key(state)
        record = {"seq": state.next_seq(), "task": key, "call": trace_top_call(state, key)}
        record.update(fields)
        bucket.append(record)
    return record


def trace_install_wrappers(state: TraceState) -> None:
    """Wrap the bus, the logs, locks, abort, cursor/position state and task creation."""
    from tau_agent_core.continuation import LoopPosition
    from tau_agent_core.cursor import Cursor
    from tau_agent_core.events import EventBus
    from tau_agent_core.session_log import InMemorySessionLog
    from tau_coding_agent.session_store import Session
    from tau_llm.abort import AbortSignal

    original_emit = EventBus.emit
    original_channel = EventBus.emit_channel

    async def emit(self: Any, event: Any) -> None:
        """Record an AgentEvent emission, then emit it."""
        listeners = getattr(self, "_listeners", {})
        trace_record(
            state,
            state.events,
            kind="event",
            type=event.type,
            cursor_id=event.cursor_id,
            submission_id=getattr(event, "submission_id", None),
            tool_name=event.tool_name,
            is_error=event.is_error,
            error=event.error,
            end_reason=getattr(event, "end_reason", None),
            subscribers=len(listeners.get(event.type, [])) + len(listeners.get("all", [])),
        )
        if (
            state.variant == "abort-in-tool"
            and event.type == "tool_execution_start"
            and event.tool_name == "bash"
        ):
            asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGINT)
        await original_emit(self, event)

    async def emit_channel(self: Any, channel: str, *args: Any, **kwargs: Any) -> None:
        """Record a string-channel emission, then emit it."""
        listeners = getattr(self, "_listeners", {})
        trace_record(
            state,
            state.events,
            kind="channel",
            type=channel,
            arg_types=[type(a).__name__ for a in args],
            kwargs=sorted(kwargs),
            subscribers=len(listeners.get(channel, [])),
        )
        await original_channel(self, channel, *args, **kwargs)

    EventBus.emit = emit  # type: ignore[method-assign]
    EventBus.emit_channel = emit_channel  # type: ignore[method-assign]

    def wrap_append(cls: Any, name: str, store: str) -> None:
        """Record every entry ``cls.name`` writes."""
        original = getattr(cls, name)

        @functools.wraps(original)
        def append_now(self: Any, parent_id: Any, entry_type: str, payload: dict) -> str:
            entry_id = original(self, parent_id, entry_type, payload)
            message = payload.get("message") if isinstance(payload, dict) else None
            trace_record(
                state,
                state.entries,
                op="append",
                store=store,
                entry_type=entry_type,
                id=entry_id,
                parent=parent_id,
                role=message.get("role") if isinstance(message, dict) else None,
                status=payload.get("status"),
                custom_type=payload.get("customType"),
                has_ext="ext" in payload,
            )
            return entry_id

        setattr(cls, name, append_now)

    def wrap_finalize(cls: Any, store: str) -> None:
        """Record every ``cls.finalize``."""
        original = cls.finalize

        @functools.wraps(original)
        async def finalize(self: Any, entry_id: str, payload: dict) -> None:
            await original(self, entry_id, payload)
            message = payload.get("message") if isinstance(payload, dict) else None
            trace_record(
                state,
                state.entries,
                op="finalize",
                store=store,
                entry_type=None,
                id=entry_id,
                parent=None,
                role=message.get("role") if isinstance(message, dict) else None,
                stop_reason=message.get("stop_reason") if isinstance(message, dict) else None,
                has_ext="ext" in payload,
            )

        cls.finalize = finalize

    wrap_append(Session, "_append_at_now", "file")
    wrap_finalize(Session, "file")
    wrap_append(InMemorySessionLog, "append_at_now", "memory")
    wrap_finalize(InMemorySessionLog, "memory")

    watched_cursor = {"is_streaming", "turn_token", "pre_turn_leaf", "abort_signal", "_leaf"}
    original_setattr = Cursor.__setattr__

    def cursor_setattr(self: Any, name: str, value: Any) -> None:
        """Record watched cursor attribute changes; label the turn lock."""
        if name == "turn_lock":
            state.lock_labels[id(value)] = f"Cursor[{getattr(self, 'id', '?')}].turn_lock"
        old = getattr(self, name, "<unset>") if name in watched_cursor else None
        original_setattr(self, name, value)
        if name in watched_cursor and old is not value and old != value:
            shown = f"AbortSignal@{id(value) % 9973}" if name == "abort_signal" else value
            trace_record(
                state,
                state.states,
                what=f"Cursor[{getattr(self, 'id', '?')}].{name}",
                value=repr(shown)[:80],
            )

    Cursor.__setattr__ = cursor_setattr  # type: ignore[method-assign]

    original_position_setattr = LoopPosition.__setattr__

    def position_setattr(self: Any, name: str, value: Any) -> None:
        """Record ``LoopPosition.then``/``pending_abort`` transitions."""
        old = getattr(self, name, "<unset>")
        original_position_setattr(self, name, value)
        if name in ("then", "pending_abort") and old != value:
            trace_record(state, state.states, what=f"LoopPosition.{name}", value=repr(value)[:80])

    LoopPosition.__setattr__ = position_setattr  # type: ignore[method-assign]

    original_abort = AbortSignal.abort

    def abort(self: Any, reason: Any = "user") -> None:
        """Record an abort, then abort."""
        trace_record(
            state, state.states, what=f"AbortSignal@{id(self) % 9973}.abort", value=repr(reason)
        )
        original_abort(self, reason)

    AbortSignal.abort = abort  # type: ignore[method-assign]

    original_acquire = asyncio.Lock.acquire
    original_release = asyncio.Lock.release

    async def acquire(self: asyncio.Lock) -> bool:
        """Record a labelled lock's wait and grant."""
        label = state.lock_labels.get(id(self))
        if label is not None:
            trace_record(
                state, state.states, what=label, value="acquire-wait", locked=self.locked()
            )
        result = await original_acquire(self)
        if label is not None:
            trace_record(state, state.states, what=label, value="acquired")
        return result

    def release(self: asyncio.Lock) -> None:
        """Record a labelled lock's release."""
        label = state.lock_labels.get(id(self))
        original_release(self)
        if label is not None:
            trace_record(state, state.states, what=label, value="released")

    asyncio.Lock.acquire = acquire  # type: ignore[method-assign]
    asyncio.Lock.release = release  # type: ignore[method-assign]

    original_create_task = asyncio.BaseEventLoop.create_task

    def create_task(self: Any, coro: Any, **kwargs: Any) -> Any:
        """Record which τ call created a task and what it runs."""
        task = original_create_task(self, coro, **kwargs)
        with state.lock:
            creator = trace_task_key(state)
            label = state.label_task(task)
            state.tasks.append(
                {
                    "seq": state.next_seq(),
                    "task": creator,
                    "call": trace_top_call(state, creator),
                    "new_task": label,
                    "coro": getattr(coro, "__qualname__", type(coro).__name__),
                }
            )
        return task

    asyncio.BaseEventLoop.create_task = create_task  # type: ignore[method-assign]


def trace_sse(payload: dict[str, Any]) -> bytes:
    """One SSE ``data:`` frame."""
    return f"data: {json.dumps(payload)}\n\n".encode()


def trace_chunk(delta: dict[str, Any] | None = None, finish: str | None = None) -> bytes:
    """One OpenAI ``chat.completion.chunk`` frame."""
    return trace_sse(
        {
            "id": "chatcmpl-trace",
            "object": "chat.completion.chunk",
            "model": MODEL_KEY,
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        }
    )


def trace_usage(prompt: int, completion: int) -> bytes:
    """The trailing usage-only chunk ``stream_options.include_usage`` asks for."""
    return trace_sse(
        {
            "id": "chatcmpl-trace",
            "object": "chat.completion.chunk",
            "choices": [],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }
    )


def trace_tool_delta(index: int, call_id: str | None, name: str | None, args: str) -> bytes:
    """A streamed tool-call delta; ``call_id``/``name`` only on a call's first delta."""
    call: dict[str, Any] = {"index": index, "function": {"arguments": args}}
    if call_id is not None:
        call["id"] = call_id
        call["type"] = "function"
        call["function"]["name"] = name
    return trace_chunk({"tool_calls": [call]})


def trace_first_response(variant: str, tool_names: list[str]) -> list[bytes]:
    """The scripted first completion: reasoning, a remark, two tool calls."""
    read_args = '{"path": note.txt]' if variant == "bad-args" else '{"path": "note.txt"}'
    second_name, second_args = "bash", '{"command": "echo hi"}'
    if variant == "abort-in-tool":
        second_args = '{"command": "sleep 5"}'
    elif variant == "unknown-tool":
        second_name, second_args = "no_such_tool", '{"x": 1}'
    elif variant == "tool-raises":
        matches = [n for n in tool_names if "trace_fail" in n]
        if len(matches) != 1:
            raise RuntimeError(f"trace_turn: expected one trace_fail tool, got {tool_names}")
        second_name, second_args = matches[0], "{}"
    head = [
        trace_chunk({"role": "assistant", "reasoning_content": "The user wants the note "}),
        trace_chunk({"reasoning_content": "read and a shell check."}),
        trace_chunk({"content": "Reading the note "}),
    ]
    return [
        *head,
        trace_chunk({"content": "and running the command."}),
        trace_tool_delta(0, "call_read_1", "read", ""),
        trace_tool_delta(0, None, None, read_args),
        trace_tool_delta(1, "call_two_1", second_name, second_args),
        trace_chunk(finish="tool_calls"),
        trace_usage(812, 64),
        b"data: [DONE]\n\n",
    ]


def trace_final_response() -> list[bytes]:
    """The scripted second completion: a final text answer and ``stop``."""
    return [
        trace_chunk({"role": "assistant", "content": "The note says hello "}),
        trace_chunk({"content": "and the shell printed hi."}),
        trace_chunk(finish="stop"),
        trace_usage(1020, 18),
        b"data: [DONE]\n\n",
    ]


def trace_make_handler(state: TraceState) -> Any:
    """The ``httpx.MockTransport`` handler serving the variant's scripted responses."""
    import httpx

    async def body(frames: list[bytes], fail_after: int | None, frame: bytes | None) -> Any:
        """Yield SSE frames one per scheduler pass; optionally fail mid-body."""
        for index, chunk in enumerate(frames):
            if fail_after is not None and index == fail_after:
                if frame is not None:
                    yield frame
                    return
                raise RuntimeError("scripted transport failure mid-body")
            yield chunk
            await asyncio.sleep(0)

    async def handler(request: httpx.Request) -> httpx.Response:
        """Answer one /chat/completions request from the script."""
        payload = json.loads(request.content)
        index = len(state.requests)
        tool_names = [t["function"]["name"] for t in payload.get("tools") or []]
        trace_record(
            state,
            state.requests,
            index=index,
            url=str(request.url),
            roles=[m.get("role") for m in payload.get("messages", [])],
            message_keys=[sorted(m) for m in payload.get("messages", [])],
            tools=tool_names,
            stream=payload.get("stream"),
            stream_options=payload.get("stream_options"),
            max_tokens_field=[k for k in payload if "max" in k and "tokens" in k],
        )
        sse = {"content-type": "text/event-stream"}
        variant = state.variant
        if variant == "http-error":
            if index == 0:
                return httpx.Response(
                    503,
                    headers={"retry-after-ms": "10"},
                    json={"error": {"message": "scripted overload"}},
                )
            if index == 1:
                return httpx.Response(
                    400, json={"error": {"message": "scripted: prompt exceeds context"}}
                )
        if index == 0:
            frames = trace_first_response(variant, tool_names)
            if variant == "stream-error-frame":
                error = trace_sse({"error": {"message": "scripted overload", "type": "overloaded"}})
                return httpx.Response(200, headers=sse, content=body(frames, 3, error))
            if variant == "stream-generic":
                return httpx.Response(200, headers=sse, content=body(frames, 3, None))
            return httpx.Response(200, headers=sse, content=body(frames, None, None))
        if index == 1:
            return httpx.Response(
                200, headers=sse, content=body(trace_final_response(), None, None)
            )
        raise RuntimeError(f"trace_turn: request {index} is beyond the script")

    return handler


def trace_install_provider(state: TraceState) -> None:
    """Replace the ``openai-completions`` factory with one whose client is a MockTransport."""
    import httpx
    from tau_llm.providers import api_sends_without_key, get_api_factory, register_api

    original = get_api_factory("openai-completions")
    handler = trace_make_handler(state)

    def factory(*, provider_id: str, name: str, base_url: str, api_key: str | None) -> Any:
        """Build the real provider, then give it the scripted transport."""
        provider = original(provider_id=provider_id, name=name, base_url=base_url, api_key=api_key)
        provider._client = httpx.AsyncClient(  # type: ignore[attr-defined]
            base_url=base_url,
            headers={"Content-Type": "application/json"},
            timeout=provider.request_timeout,  # type: ignore[attr-defined]
            transport=httpx.MockTransport(handler),
        )
        return provider

    register_api(
        "openai-completions",
        factory,
        replace=True,
        sends_without_key=api_sends_without_key("openai-completions"),
    )


def trace_channel_names() -> list[str]:
    """Every literal channel name ``tau_agent_core`` passes to ``emit_channel``."""
    names: set[str] = set()
    root = REPO_ROOT / "tau-agent-core" / "src" / "tau_agent_core"
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "emit_channel"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
    return sorted(names)


def trace_hook_names() -> tuple[list[str], list[str]]:
    """The runner-routed hooks and the notify ``AgentEvent`` types, read from the code."""
    from tau_agent_core.events import AgentEvent
    from tau_agent_core.extensions.runner import ExtensionRunner

    hooks = [*ExtensionRunner.HOOK_EVENTS, *ExtensionRunner.LIFECYCLE_EVENTS]
    notify = list(typing.get_args(AgentEvent.model_fields["type"].annotation))
    return hooks, notify


def trace_describe(value: Any) -> Any:
    """A short, JSON-safe description of a handler argument."""
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(str(k) for k in value)}
    return {"type": type(value).__name__}


def trace_register(api: Any) -> None:
    """The test extension: subscribe to every hook, event and channel, and log each firing."""
    state = trace_state()
    hooks, notify = trace_hook_names()
    raise_in = (
        state.variant.removeprefix("hook-raises-") if state.variant.startswith("hook-") else ""
    )

    def hook_handler(name: str) -> Any:
        """A runner hook handler: ``(event, ctx)``; returns None (observes)."""

        def handle(event: Any, ctx: Any) -> None:
            trace_record(
                state,
                state.hooks,
                hook=name,
                route="runner",
                event=trace_describe(event),
                ctx=type(ctx).__name__,
                detail={
                    k: event.get(k)
                    for k in ("tool_name", "kind", "reason", "turn_index", "loop_turns", "entry_id")
                    if isinstance(event, dict) and k in event
                },
            )
            if name == raise_in:
                raise RuntimeError(f"trace: {name} handler raised")

        return handle

    def notify_handler(name: str, route: str = "bus") -> Any:
        """A notify-bus handler: any arguments; returns None."""

        def handle(*args: Any, **kwargs: Any) -> None:
            event = args[0] if args else None
            trace_record(
                state,
                state.hooks,
                hook=getattr(event, "type", name) if name == "all" else name,
                route=route,
                event=trace_describe(event) if event is not None else None,
                kwargs=sorted(kwargs),
                detail={"tool_name": getattr(event, "tool_name", None)},
            )
            if raise_in == "notify" and name == "message_end":
                raise RuntimeError("trace: message_end notify handler raised")

        return handle

    for name in hooks:
        api.on(name, hook_handler(name))
    for name in [*notify, *trace_channel_names()]:
        if name not in hooks:
            api.on(name, notify_handler(name))
    shadowed = {name for name in notify if name in hooks}
    on_all = notify_handler("all", "bus:all")

    def all_handler(event: Any) -> None:
        """Receive, through ``all``, the AgentEvent types a hook of the same name shadows."""
        if getattr(event, "type", None) in shadowed:
            on_all(event)

    api.on("all", all_handler)
    if state.variant == "tool-raises":

        def fail(tool_call_id: str, params: Any, signal: Any, on_update: Any, ctx: Any) -> Any:
            raise RuntimeError("trace_fail: scripted tool failure")

        api.register_tool(
            {
                "name": "trace_fail",
                "description": "Always raises (trace fixture).",
                "parameters": {"type": "object", "properties": {}},
                "execute": fail,
            }
        )


def register(api: Any) -> None:
    """Extension entry point: hand the api to the tracing process's ``trace_register``."""
    main_module = sys.modules.get("__main__")
    target = getattr(main_module, "trace_register", None)
    if target is None:
        raise RuntimeError("trace_turn extension: load it only from scripts/arch/trace_turn.py")
    target(api)


def trace_run_head(state: TraceState, work: Path) -> dict[str, Any]:
    """Run ``tau_coding_agent.cli.main`` for one ``-p --mode json`` turn; capture what it shows."""
    from tau_coding_agent.cli import main as tau_main

    argv = [
        "-p",
        "--mode",
        "json",
        "--model",
        MODEL_KEY,
        "--session-dir",
        str(work / "sessions"),
        "-e",
        str(Path(__file__).resolve()),
        "-ne",
        "--no-context-files",
        "--no-prompt-templates",
        "--no-skills",
        PROMPT,
    ]
    out, err = io.StringIO(), io.StringIO()
    escaped: str | None = None
    code: int | None = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = tau_main(argv)
        except BaseException as exc:  # noqa: BLE001 — the head's own escape is the observation
            escaped = f"{type(exc).__name__}: {exc}"
    return {
        "argv": ["tau", *argv[:-1], "<prompt>"],
        "exit_code": code,
        "escaped": escaped,
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
    }


def trace_run_variant(variant: str, out: Path) -> None:
    """Run one variant in this process and write its outputs under ``out``."""
    global STATE
    work = Path(tempfile.mkdtemp(prefix="tau-trace-"))
    home = work / "home"
    (home / ".tau").mkdir(parents=True)
    (work / "cwd").mkdir()
    (work / "cwd" / "note.txt").write_text("hello\n", encoding="utf-8")
    os.environ["HOME"] = str(home)
    os.environ["TAU_HOME"] = str(home / ".tau")
    config = {
        "default_model": MODEL_KEY,
        "models": {
            MODEL_KEY: {
                "backend": "openai",
                "model": MODEL_KEY,
                "base_url": MOCK_BASE_URL,
                "reasoning": True,
                "context_window": 32768,
                "max_tokens": 1024,
            }
        },
    }
    (home / ".tau" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    os.chdir(work / "cwd")
    roots = tuple(str(REPO_ROOT / pkg / "src") + os.sep for pkg in SRC_PACKAGES)
    import tau_agent_core

    if not str(Path(tau_agent_core.__file__).resolve()).startswith(roots):
        sys.exit(
            f"trace_turn: tau_agent_core imports from {tau_agent_core.__file__}, not this tree; "
            f"set PYTHONPATH to the four {REPO_ROOT}/<pkg>/src directories"
        )
    STATE = state = TraceState(variant=variant, roots=roots)
    trace_install_provider(state)
    trace_install_wrappers(state)
    trace_install_monitoring(state)
    try:
        head = trace_run_head(state, work)
    finally:
        trace_uninstall_monitoring()
    logs = sorted((work / "sessions").rglob("*.jsonl"))
    session_lines = [ln for p in logs for ln in p.read_text(encoding="utf-8").splitlines()]
    out.mkdir(parents=True, exist_ok=True)
    trace_write_outputs(state, head, session_lines, out)
    shutil.rmtree(work, ignore_errors=True)


def trace_outcome(state: TraceState, head: dict[str, Any], session_lines: list[str]) -> dict:
    """How the turn ended: the head's view, the terminal events and the entries."""
    stdout_records = []
    for line in head["stdout"].splitlines():
        try:
            stdout_records.append(json.loads(line))
        except json.JSONDecodeError:
            stdout_records.append({"unparsed": line[:200]})
    terminal = [
        e
        for e in state.events
        if e["type"] in ("agent_end", "submission_end", "turn_end", "follow_ups_dropped")
    ]
    entries = [json.loads(ln) for ln in session_lines]
    return {
        "variant": state.variant,
        "description": VARIANTS[state.variant],
        "exit_code": head["exit_code"],
        "escaped": head["escaped"],
        "stderr": head["stderr"],
        "stdout_types": [r.get("type") for r in stdout_records],
        "agent_end": [
            {k: e[k] for k in ("seq", "error", "end_reason", "is_error")}
            for e in state.events
            if e["type"] == "agent_end"
        ],
        "terminal_events": [{k: e.get(k) for k in ("seq", "type", "end_reason")} for e in terminal],
        "requests": len(state.requests),
        "session_entries": [
            {
                "type": e.get("type"),
                "id": e.get("id"),
                "parentId": e.get("parentId"),
                "role": (e.get("message") or {}).get("role"),
                "status": e.get("status"),
                "stop_reason": (e.get("message") or {}).get("stop_reason"),
                "is_error": (e.get("message") or {}).get("is_error"),
                "text": trace_entry_text(e),
            }
            for e in entries
        ],
        "hooks_fired": sorted({h["hook"] for h in state.hooks}),
        "handled_exceptions": [
            {k: x[k] for k in ("seq", "type", "file", "line", "function")}
            for x in state.exceptions
            if x["kind"] == "handled"
        ],
    }


def trace_entry_text(entry: dict[str, Any]) -> str:
    """A short text view of a log line's message content."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return json.dumps(entry.get("data") or entry.get("name") or "")[:100]
    content = message.get("content")
    if isinstance(content, str):
        return content[:100]
    parts = []
    for block in content or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "thinking":
            parts.append(f"<thinking {len(block.get('thinking', ''))}ch>")
        elif block.get("type") == "toolCall":
            parts.append(f"<toolCall {block.get('name')}>")
    extra = message.get("error_message") or ""
    return (" ".join(parts) + (f" | error: {extra}" if extra else ""))[:160]


def trace_write_outputs(
    state: TraceState, head: dict[str, Any], session_lines: list[str], out: Path
) -> None:
    """Write trace.json, calltree.txt, sequence.mmd, outcome.json and the raw head/log files."""
    hooks, notify = trace_hook_names()
    raw = {
        "variant": state.variant,
        "description": VARIANTS[state.variant],
        "head": {k: head[k] for k in ("argv", "exit_code", "escaped", "stderr")},
        "subscribed": {"hooks": hooks, "notify": notify, "channels": trace_channel_names()},
        "monitor_anomalies": state.anomalies,
        "calls": state.calls,
        "hooks": state.hooks,
        "events": state.events,
        "entries": state.entries,
        "states": state.states,
        "tasks": state.tasks,
        "exceptions": state.exceptions,
        "requests": state.requests,
    }
    (out / "trace.json").write_text(json.dumps(raw, indent=1, default=str), encoding="utf-8")
    (out / "head_stdout.jsonl").write_text(head["stdout"], encoding="utf-8")
    (out / "session_log.jsonl").write_text("\n".join(session_lines) + "\n", encoding="utf-8")
    outcome = trace_outcome(state, head, session_lines)
    (out / "outcome.json").write_text(json.dumps(outcome, indent=1, default=str), encoding="utf-8")
    (out / "calltree.txt").write_text(trace_calltree(raw), encoding="utf-8")
    (out / "sequence.mmd").write_text(trace_sequence(raw), encoding="utf-8")


def trace_markers(raw: dict[str, Any]) -> list[tuple[int, int | None, str, str]]:
    """Every non-call record as ``(seq, call, task, text)``."""
    markers: list[tuple[int, int | None, str, str]] = []
    for h in raw["hooks"]:
        markers.append((h["seq"], h["call"], h["task"], f"! hook[{h['route']}] {h['hook']}"))
    for e in raw["events"]:
        extra = f" tool={e['tool_name']}" if e.get("tool_name") else ""
        if e.get("error"):
            extra += f" error={e['error'][:60]!r}"
        if e.get("end_reason"):
            extra += f" end_reason={e['end_reason']}"
        markers.append(
            (
                e["seq"],
                e["call"],
                e["task"],
                f"~ {e['kind']} {e['type']}{extra} subs={e['subscribers']}",
            )
        )
    for n in raw["entries"]:
        what = n.get("entry_type") or ""
        role = f"({n['role']})" if n.get("role") else ""
        status = f" status={n['status']}" if n.get("status") else ""
        parent = f" parent={n['parent']}" if n["op"] == "append" else ""
        markers.append(
            (
                n["seq"],
                n["call"],
                n["task"],
                f"+ {n['op']} {what}{role} id={n['id']}{parent}{status}",
            )
        )
    for s in raw["states"]:
        markers.append((s["seq"], s["call"], s["task"], f"= {s['what']} -> {s['value']}"))
    for t in raw["tasks"]:
        markers.append(
            (t["seq"], t["call"], t["task"], f"> new task {t['new_task']} ({t['coro']})")
        )
    for x in raw["exceptions"]:
        markers.append(
            (
                x["seq"],
                x["call"],
                x["task"],
                f"x {x['kind']} {x['type']} at {x['file']}:{x['line']}",
            )
        )
    for r in raw["requests"]:
        markers.append(
            (r["seq"], r["call"], r["task"], f"> HTTP request #{r['index']} roles={r['roles']}")
        )
    return sorted(markers)


def trace_calltree(raw: dict[str, Any]) -> str:
    """Render the call tree, per task, with markers in place and helper runs collapsed."""
    calls = raw["calls"]
    children: dict[int | None, list[tuple[int, str, Any]]] = {}
    roots: dict[str, list[tuple[int, str, Any]]] = {}
    for c in calls:
        item = (c["seq"], "call", c["id"])
        if c["parent"] is None:
            roots.setdefault(c["task"], []).append(item)
        else:
            children.setdefault(c["parent"], []).append(item)
    for seq, call, task, text in trace_markers(raw):
        item = (seq, "mark", text)
        if call is None:
            roots.setdefault(task, []).append(item)
        else:
            children.setdefault(call, []).append(item)
    interesting: dict[int, bool] = {}

    def is_interesting(call_id: int) -> bool:
        """A call is shown when its subtree holds a marker."""
        if call_id not in interesting:
            interesting[call_id] = any(
                kind == "mark" or is_interesting(ref) for _, kind, ref in children.get(call_id, [])
            )
        return interesting[call_id]

    sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))

    def render(items: list[tuple[int, str, Any]], indent: int) -> list[str]:
        """Render sibling items; merge identical consecutive blocks and helper runs."""
        blocks: list[list[str]] = []
        helpers: dict[str, int] = {}

        def flush() -> None:
            if helpers:
                shown = ", ".join(f"{k}×{v}" for k, v in sorted(helpers.items())[:8])
                more = "" if len(helpers) <= 8 else f", +{len(helpers) - 8} more"
                blocks.append(
                    ["  " * indent + f"· {sum(helpers.values())} helper calls: {shown}{more}"]
                )
                helpers.clear()

        for _, kind, ref in sorted(items, key=lambda i: i[0]):
            if kind == "mark":
                flush()
                blocks.append(["  " * indent + ref])
                continue
            c = calls[ref]
            if not is_interesting(ref):
                helpers[c["name"]] = helpers.get(c["name"], 0) + 1
                continue
            flush()
            tail = "" if c["outcome"] in (None, "return") else f"  !! {c['outcome']}"
            head = "  " * indent + f"{c['name']}  [{c['file']}:{c['line']}]{tail}"
            blocks.append([head, *render(children.get(ref, []), indent + 1)])
        flush()
        merged: list[str] = []
        previous: list[str] | None = None
        count = 0
        for block in [*blocks, None]:
            if block is not None and previous is not None and block == previous:
                count += 1
                continue
            if previous is not None:
                if count > 1:
                    merged.append(previous[0] + f"   (×{count} identical)")
                    merged.extend(previous[1:])
                else:
                    merged.extend(previous)
            previous, count = block, 1
        return merged

    lines = [
        f"# call tree — variant {raw['variant']}: {raw['description']}",
        "# markers: ! hook  ~ bus emit  + log write  = state  > task/request  x exception",
        f"# monitor anomalies (unmatched frame pops): {raw['monitor_anomalies']}",
    ]
    order = sorted(roots, key=lambda k: min(i[0] for i in roots[k]))
    for task in order:
        spawned = [t for t in raw["tasks"] if t["new_task"] == task]
        origin = ""
        if spawned:
            parent = spawned[0]["call"]
            where = calls[parent]["name"] if parent is not None else spawned[0]["task"]
            origin = f" (created by {where} on {spawned[0]['task']})"
        lines.append("")
        lines.append(f"## task {task}{origin}")
        lines.extend(render(roots[task], 1))
    return "\n".join(lines) + "\n"


LANDMARKS: dict[str, str] = {
    "cli.py:main": "Head",
    "headless.py:run_print": "Head",
    "headless.py:start_extensions": "Head",
    "backends.py:TauBackend.stream_submission": "Backend",
    "backends.py:TauBackend.abort": "Backend",
    "backends.py:TauBackend.load_extensions": "Backend",
    "agent_session.py:AgentSession.submit": "Session",
    "agent_session.py:AgentSession._drive_turn": "Session",
    "agent_session.py:AgentSession._apply_input_pipeline": "Session",
    "agent_session.py:AgentSession._record_config": "Session",
    "agent_session.py:AgentSession._run_one_turn": "Session",
    "agent_session.py:AgentSession._end_of_prompt_drain": "Session",
    "agent_session.py:AgentSession._run_user_turn_end": "Session",
    "agent_session.py:AgentSession._maybe_auto_compact": "Session",
    "agent_session.py:AgentSession._drain_deferred_ops": "Session",
    "agent_session.py:AgentSession._compact_mid_turn": "Session",
    "agent_session.py:AgentSession._request_settings": "Session",
    "agent_session.py:AgentSession._append_turn_error": "Session",
    "agent_session.py:AgentSession.abort": "Session",
    "agent_session.py:AgentSession.emit_session_start": "Session",
    "agent_session.py:AgentSession.emit_session_shutdown": "Session",
    "agent_session.py:AgentSession._annotation": "Session",
    "agent_session.py:AgentSession._entry_written": "Session",
    "agent_session.py:_CursorWriter.open": "Writer",
    "agent_session.py:_CursorWriter.finalize": "Writer",
    "agent_session.py:_CursorWriter.append": "Writer",
    "agent_session.py:_CursorWriter.close_group": "Writer",
    "cursor.py:Cursor.append": "Cursor",
    "cursor.py:Cursor.open": "Cursor",
    "cursor.py:Cursor.finalize": "Cursor",
    "cursor.py:Cursor.append_config": "Cursor",
    "agent_loop.py:AgentLoop.run": "Loop",
    "agent_loop.py:AgentLoop._stream_response": "Loop",
    "agent_loop.py:AgentLoop._stream_failed": "Loop",
    "agent_loop.py:AgentLoop._execute_tool_calls": "Loop",
    "agent_loop.py:AgentLoop._execute_parallel": "Loop",
    "agent_loop.py:AgentLoop._execute_sequential": "Loop",
    "agent_loop.py:AgentLoop._aborted_batch": "Loop",
    "agent_loop.py:AgentLoop._abort_prepared": "Loop",
    "agent_loop.py:AgentLoop._run_mutating_turn_end": "Loop",
    "agent_loop.py:AgentLoop._deliver_steer": "Loop",
    "agent_loop.py:AgentLoop._record": "Loop",
    "client.py:stream_simple": "Client",
    "retry.py:retrying_stream": "Client",
    "streaming.py:AssistantMessageEventStream._collect": "Client",
    "openai.py:OpenAICompletionsProvider.stream_chat": "Provider",
    "openai.py:OpenAICompletionsProvider._stream_transport": "Provider",
    "openai.py:OpenAICompletionsProvider._build_final_message": "Provider",
    "openai.py:OpenAICompletionsProvider._http_error_from_response": "Provider",
    "tool_pipeline.py:prepare_tool_call": "Pipeline",
    "tool_pipeline.py:execute_prepared_call": "Pipeline",
    "tool_pipeline.py:apply_tool_result_hooks": "Pipeline",
    "read.py:ReadTool.execute": "Tool",
    "bash.py:BashTool.execute": "Tool",
    "base.py:AgentTool.execute": "Tool",
}

PARTICIPANTS = [
    ("Head", "tau -p head (cli.main/run_print)"),
    ("Backend", "TauBackend"),
    ("Session", "AgentSession"),
    ("Writer", "_CursorWriter"),
    ("Cursor", "Cursor"),
    ("Log", "Session log (JSONL file)"),
    ("Loop", "AgentLoop"),
    ("Client", "stream_simple / EventStream"),
    ("Provider", "OpenAICompletionsProvider"),
    ("Wire", "MockTransport (scripted SSE)"),
    ("Pipeline", "tool_pipeline"),
    ("Tool", "Tool.execute"),
    ("Runner", "ExtensionRunner"),
    ("Bus", "EventBus"),
    ("Ext", "trace extension"),
]


def trace_landmark(call: dict[str, Any]) -> str | None:
    """The diagram participant a call belongs to, if it is a landmark."""
    key = f"{Path(call['file']).name}:{call['name']}"
    if key in LANDMARKS:
        return LANDMARKS[key]
    if call["file"].endswith("extensions/runner.py") and call["name"].startswith(
        "ExtensionRunner.emit_"
    ):
        return "Runner"
    if "/tools/" in call["file"] and call["name"].endswith(".execute"):
        return "Tool"
    return None


def trace_sequence(raw: dict[str, Any]) -> str:
    """Render the mermaid sequenceDiagram: landmark calls, hooks, emits, writes, failures."""
    calls = raw["calls"]
    spawned_by = {t["new_task"]: t["call"] for t in raw["tasks"]}

    def owner(call_id: int | None) -> str:
        """The participant of the nearest landmark at or above ``call_id``."""
        while call_id is not None:
            c = calls[call_id]
            part = trace_landmark(c)
            if part is not None:
                return part
            parent = c["parent"]
            call_id = parent if parent is not None else spawned_by.get(c["task"])
        return "Head"

    def caller(call_id: int) -> str:
        """The participant of the nearest landmark strictly above ``call_id``."""
        c = calls[call_id]
        parent = c["parent"] if c["parent"] is not None else spawned_by.get(c["task"])
        return owner(parent)

    steps: list[tuple[int, str]] = []
    for c in calls:
        part = trace_landmark(c)
        if part is None:
            continue
        short = ".".join(c["name"].replace(".<locals>", "").split(".")[-2:])
        src = caller(c["id"])
        line = f"{src}->>{part}: {short}()" if src != part else f"Note right of {part}: {short}()"
        steps.append((c["seq"], line))
        if c["outcome"] and c["outcome"].startswith("raise") and c["end_seq"] is not None:
            if src != part:
                steps.append((c["end_seq"], f"{part}-x{src}: {short} {c['outcome']}"))
            else:
                steps.append((c["end_seq"], f"Note right of {part}: {short} {c['outcome']}"))
    for h in raw["hooks"]:
        if h["route"] == "runner":
            src = owner(h["call"])
            target = "Runner" if src != "Runner" else "Ext"
            if target == "Runner":
                steps.append((h["seq"], f"{src}->>Runner: hook {h['hook']}"))
            steps.append((h["seq"], f"Runner->>Ext: {h['hook']}(event, ctx) #{h['seq']}"))
    for e in raw["events"]:
        extra = f" {e['tool_name']}" if e.get("tool_name") else ""
        if e.get("end_reason"):
            extra += f" end={e['end_reason']}"
        if e.get("error"):
            extra += " error"
        steps.append((e["seq"], f"{owner(e['call'])}->>Bus: {e['type']}{extra}"))
    for n in raw["entries"]:
        what = f"{n.get('entry_type') or ''}{'(' + n['role'] + ')' if n.get('role') else ''}"
        status = " incomplete" if n.get("status") else ""
        steps.append((n["seq"], f"{owner(n['call'])}->>Log: {n['op']} {what} {n['id']}{status}"))

    def under(call_id: int | None, root: int | None) -> bool:
        """Whether ``call_id`` is ``root`` or below it, following task creation."""
        while call_id is not None:
            if call_id == root:
                return True
            c = calls[call_id]
            call_id = c["parent"] if c["parent"] is not None else spawned_by.get(c["task"])
        return False

    for r in raw["requests"]:
        steps.append((r["seq"], f"Provider->>Wire: POST chat/completions #{r['index']}"))
        reads = sum(
            1
            for t in raw["tasks"]
            if t["coro"] in ("async_generator_asend", "AbortSignal.wait")
            and under(t["call"], r["call"])
        )
        steps.append(
            (
                r["seq"],
                f"Note over Provider: iterate_until_aborted: {reads} tasks (1 per line read + 1 wait)",
            )
        )
    for s in raw["states"]:
        if ".turn_lock" in s["what"] and s["value"] in ("acquired", "released"):
            steps.append((s["seq"], f"Note over Cursor: turn_lock {s['value']}"))
        elif s["what"].endswith(".abort"):
            steps.append(
                (s["seq"], f"Note over {owner(s['call'])}: AbortSignal.abort({s['value']})")
            )
    for t in raw["tasks"]:
        if t["coro"] in ("async_generator_asend", "AbortSignal.wait"):
            continue
        if t["coro"] == "async_generator_athrow":
            steps.append((t["seq"], "Note over Client: asyncgen finalizer schedules aclose() task"))
            continue
        steps.append((t["seq"], f"Note over {owner(t['call'])}: new task {t['coro'][:48]}"))
    steps.sort()
    lines = [
        "sequenceDiagram",
        f"%% variant {raw['variant']}: {raw['description']}",
        f"%% generated by scripts/arch/trace_turn.py from {len(calls)} calls",
    ]
    lines += [f"participant {pid} as {label}" for pid, label in PARTICIPANTS]
    previous, count = None, 0
    for _, line in [*steps, (0, None)]:
        if line == previous:
            count += 1
            continue
        if previous is not None:
            lines.append(previous if count == 1 else f"{previous} ×{count}")
        previous, count = line, 1
    return "\n".join(lines) + "\n"


TURN_PATH_MODULES = ("agent_session.py", "agent_loop.py", "tool_pipeline.py", "headless.py")


def trace_handler_action(handler: ast.ExceptHandler) -> str:
    """Classify an ``except`` body by what it does, from its AST."""
    actions: list[str] = []
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            actions.append(
                "re-raise"
                if node.exc is None
                or (isinstance(node.exc, ast.Name) and node.exc.id == handler.name)
                else "convert"
            )
        elif isinstance(node, ast.Call):
            text = ast.unparse(node.func)
            low = text.lower()
            if (
                "print" in low
                or "log" in low
                or "warn" in low
                or "_emit_error" in low
                or "surface" in low
            ):
                actions.append("log/surface")
            if "emit" in low and "_emit_error" not in low:
                actions.append("emit-event")
            if any(w in low for w in ("append", "finalize", "writer", "_record", "close_group")):
                actions.append("write/record")
            if "note_turn_failure" in low:
                actions.append("note-failure")
            if "put" in low:
                actions.append("enqueue")
        elif isinstance(node, (ast.Continue,)):
            actions.append("retry/continue")
        elif isinstance(node, ast.Return):
            actions.append("return-value")
        elif isinstance(node, ast.Assign):
            actions.append("assign")
    if not actions:
        return "swallow (pass)"
    return ", ".join(dict.fromkeys(actions))


def trace_function_nodes(path: Path) -> dict[int, ast.AST]:
    """Function nodes in ``path`` keyed by every line ``co_firstlineno`` may report."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes[node.lineno] = node
            for deco in node.decorator_list:
                nodes.setdefault(deco.lineno, node)
    return nodes


def trace_own_nodes(func: ast.AST) -> list[ast.AST]:
    """The nodes of ``func``'s body, not descending into nested functions or classes."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def trace_handler_names(handler: ast.ExceptHandler) -> set[str] | None:
    """The class names an ``except`` clause names; None for a bare ``except``."""
    if handler.type is None:
        return None
    parts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {p.attr if isinstance(p, ast.Attribute) else ast.unparse(p) for p in parts}


def trace_try_hits(
    file: str, node: ast.Try, handled: dict[tuple[str, int], list[tuple[str, list[str]]]]
) -> dict[int, set[str]]:
    """Which clause of ``node`` caught each handled exception recorded at its first ``except`` line.

    A handler's bytecode target carries no line, so the trace records the first line
    after it: the first ``except`` clause's. The clause that matched is the first
    whose named classes meet the exception's MRO.
    """
    hits: dict[int, set[str]] = {}
    if not node.handlers:
        return hits
    head = node.handlers[0]
    last = head.type.end_lineno if head.type is not None and head.type.end_lineno else head.lineno
    events = [e for line in range(head.lineno, last + 1) for e in handled.get((file, line), [])]
    for variant, mro in events:
        for h in node.handlers:
            names = trace_handler_names(h)
            if names is None or names & set(mro):
                hits.setdefault(h.lineno, set()).add(variant)
                break
    return hits


def trace_static_errors(base: dict[str, Any], variants: list[dict[str, Any]]) -> dict[str, Any]:
    """Every try/except, raise and early return in the functions the base trace ran."""
    handled: dict[tuple[str, int], list[tuple[str, list[str]]]] = {}
    raised: dict[tuple[str, int], set[str]] = {}
    for v in variants:
        for x in v["exceptions"]:
            where = (x["file"], x["line"])
            if x["kind"] == "handled":
                handled.setdefault(where, []).append((v["variant"], x.get("mro", [x["type"]])))
            else:
                raised.setdefault(where, set()).add(v["variant"])
    functions = sorted(
        {
            (c["file"], c["line"], c["name"])
            for c in base["calls"]
            if "__annotate__" not in c["name"]
        }
    )
    cache: dict[str, dict[int, ast.AST]] = {}
    tries: list[dict[str, Any]] = []
    raises: list[dict[str, Any]] = []
    returns: list[dict[str, Any]] = []
    for file, line, name in functions:
        if file not in cache:
            cache[file] = trace_function_nodes(REPO_ROOT / file)
        func = cache[file].get(line)
        if func is None:
            continue
        nodes = trace_own_nodes(func)
        for node in nodes:
            if isinstance(node, ast.Try):
                caught = trace_try_hits(file, node, handled)
                for h in node.handlers:
                    hit = sorted(caught.get(h.lineno, set()))
                    tries.append(
                        {
                            "where": f"{file}:{h.lineno}",
                            "function": name,
                            "catches": ast.unparse(h.type)
                            if h.type is not None
                            else "BaseException (bare)",
                            "action": trace_handler_action(h),
                            "ran": hit,
                        }
                    )
                if node.finalbody:
                    tries.append(
                        {
                            "where": f"{file}:{node.finalbody[0].lineno}",
                            "function": name,
                            "catches": "(finally)",
                            "action": "always runs",
                            "ran": [],
                        }
                    )
            elif isinstance(node, ast.Raise) and node.exc is not None:
                exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                raises.append(
                    {
                        "where": f"{file}:{node.lineno}",
                        "function": name,
                        "raises": ast.unparse(exc)[:60],
                        "ran": sorted(raised.get((file, node.lineno), set())),
                    }
                )
            elif isinstance(node, ast.If) and Path(file).name in TURN_PATH_MODULES:
                for sub in node.body:
                    if isinstance(sub, ast.Return):
                        returns.append(
                            {
                                "where": f"{file}:{sub.lineno}",
                                "function": name,
                                "guard": ast.unparse(node.test)[:90],
                                "returns": ast.unparse(sub.value)[:60] if sub.value else "None",
                            }
                        )

    def ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rows sorted by file and line, one per (where, catches/raises/guard)."""
        unique = {(r["where"], r.get("catches", r.get("raises", r.get("guard")))): r for r in rows}
        return sorted(
            unique.values(), key=lambda r: (r["where"].split(":")[0], int(r["where"].split(":")[1]))
        )

    return {
        "functions": len(functions),
        "try_handlers": ordered(tries),
        "raises": ordered(raises),
        "early_returns": ordered(returns),
    }


def trace_errors_md(errors: dict[str, Any]) -> str:
    """Markdown tables for ``trace_static_errors``."""
    lines = [
        "# Error cases on the base-turn path",
        "",
        f"Functions scanned: {errors['functions']} (every τ function the base trace called).",
        "`ran` lists the variants whose trace recorded a handled exception (or raise) on that line;",
        "empty means read only. Actions are classified from the handler's AST.",
        "",
        "## try/except handlers",
        "",
        "| where | function | catches | action | ran |",
        "|---|---|---|---|---|",
    ]
    for t in errors["try_handlers"]:
        lines.append(
            f"| {t['where']} | `{t['function']}` | `{t['catches']}` | {t['action']} | {', '.join(t['ran']) or 'read'} |"
        )
    lines += [
        "",
        "## raise statements",
        "",
        "| where | function | raises | ran |",
        "|---|---|---|---|",
    ]
    for r in errors["raises"]:
        lines.append(
            f"| {r['where']} | `{r['function']}` | `{r['raises']}` | {', '.join(r['ran']) or 'read'} |"
        )
    lines += [
        "",
        "## guarded returns (turn-path modules)",
        "",
        "| where | function | guard | returns |",
        "|---|---|---|---|",
    ]
    for r in errors["early_returns"]:
        guard = r["guard"].replace("|", "\\|")
        lines.append(f"| {r['where']} | `{r['function']}` | `{guard}` | `{r['returns']}` |")
    return "\n".join(lines) + "\n"


def trace_summary_md(outcomes: list[dict[str, Any]]) -> str:
    """A table of how each variant's turn ended."""
    lines = [
        "# Variant outcomes",
        "",
        "| variant | exit | requests | agent_end | entries written (type/role/stop) | head stderr |",
        "|---|---|---|---|---|---|",
    ]
    for o in outcomes:
        end = "; ".join(f"{a['end_reason']}: {(a['error'] or '')[:50]}" for a in o["agent_end"])
        entries = ", ".join(
            f"{e['type']}/{e['role'] or '-'}/{e['stop_reason'] or ('err' if e['is_error'] else '-')}"
            for e in o["session_entries"]
            if e["type"] != "session"
        )
        err = o["stderr"].strip().replace("\n", " ⏎ ").replace("|", "\\|")[:160]
        lines.append(
            f"| {o['variant']} | {o['exit_code']} | {o['requests']} | {end} | {entries} | {err} |"
        )
    return "\n".join(lines) + "\n"


def trace_all(out: Path) -> int:
    """Run every variant in its own interpreter, then write the summary and error tables."""
    failures = 0
    for name in VARIANTS:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--variant", name, "--out", str(out)],
            check=False,
        )
        if proc.returncode != 0:
            print(f"trace_turn: variant {name} exited {proc.returncode}", file=sys.stderr)
            failures += 1
    if failures:
        return 1
    outcomes = [json.loads((out / v / "outcome.json").read_text()) for v in VARIANTS]
    (out / "summary.json").write_text(json.dumps(outcomes, indent=1), encoding="utf-8")
    (out / "summary.md").write_text(trace_summary_md(outcomes), encoding="utf-8")
    traces = [json.loads((out / v / "trace.json").read_text()) for v in VARIANTS]
    errors = trace_static_errors(traces[0], traces)
    (out / "errors.json").write_text(json.dumps(errors, indent=1), encoding="utf-8")
    (out / "errors.md").write_text(trace_errors_md(errors), encoding="utf-8")
    return 0


def main() -> int:
    """Parse arguments and run one variant or all of them."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--variant", default="all", choices=["all", *VARIANTS])
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "build" / "arch" / "turn")
    args = parser.parse_args()
    if not hasattr(sys, "monitoring"):
        sys.exit("trace_turn: needs Python 3.12+ (sys.monitoring)")
    try:
        import httpx  # noqa: F401
    except ImportError:
        sys.exit("trace_turn: httpx is missing; pip install -e './tau-coding-agent[dev]'")
    out = args.out.resolve()
    if args.variant == "all":
        return trace_all(out)
    trace_run_variant(args.variant, out / args.variant)
    return 0


if __name__ == "__main__":
    sys.exit(main())
