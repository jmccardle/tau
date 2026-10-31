"""The effects: what performing one job means, each ending in what to answer.

No effect touches the session except to read it. The driver answers.
"""

import asyncio
import json
import math
import sys
import traceback
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import aclosing
from typing import cast

from tau_next import trace
from tau_next._config import ConfigError, model_name
from tau_next._config_layers import ModelCatalog
from tau_next._core import (
    Answer,
    Author,
    Draft,
    Job,
    Outcome,
    Part,
    RunCall,
    SampleEnd,
    Session,
)
from tau_next.events import CallArgs, CallNamed, Delta, TextDelta, ThinkingDelta, ToolProgress
from tau_next.handles import Cursor, NodeHandle, Subject
from tau_next.protocols import (
    CallDraft,
    CursorHook,
    Extension,
    Finish,
    Progress,
    ProgressTool,
    Provider,
    SampleError,
    Tool,
    ToolHook,
)
from tau_next.tools import FunctionTool, ToolError
from tau_next.values import Context


async def run_hooks(
    job: Job.Hooks,
    subject: Subject,
    extensions: Sequence[Extension],
    cursor_for: Callable[[Extension], Cursor],
    failed: Callable[[str, str, str], None],
) -> Answer:
    """Every extension's hook for the point, in order. The first string halts; an exception, or
    a hook that breaks its contract (returns what is not a `str` or `None`, leaves a `draft`
    that is not a `Draft`), fails the step with `"Type: msg"` and its traceback on stderr;
    otherwise the subject's draft comes back. A failure is also reported as
    `failed(point, extension name, "Type: msg")`, once, before the answer."""
    name = job.point.name
    for ext in extensions:
        hook = cast("CursorHook | None", getattr(ext, name, None))
        if hook is None:
            continue
        cursor = cursor_for(ext)
        try:
            with trace.span("hook", f"{name} {ext.name}"):
                stop = await hook(cursor, subject)
            _check_cursor_hook(name, ext, stop, subject)
        except (Exception, asyncio.CancelledError) as e:
            if not _hook_failed(e):
                raise
            error = _describe(e)
            failed(name, ext.name, error)
            return Answer.HookFailed(error)
        finally:
            cursor.retire()
        if stop is not None:
            return Answer.Hooks(stop, None)
    return Answer.Hooks(None, subject.draft)


def _hook_failed(error: BaseException) -> bool:
    """Whether `error` out of a hook is the hook's failure. A `CancelledError` is one unless
    the driver is cancelling this task (`stop`, `abort`): a hook that cancels something and
    awaits it leaks one out, and the cursor must not be left busy with nothing recorded."""
    if not isinstance(error, asyncio.CancelledError):
        return True
    task = asyncio.current_task()
    return task is None or task.cancelling() == 0


def _describe(error: BaseException) -> str:
    """`"Type: msg"` for a failed hook, with its traceback on stderr (it has absolute paths, so
    never in the log)."""
    traceback.print_exception(error, file=sys.stderr)
    return f"{type(error).__name__}: {error}"


def _check_cursor_hook(point: str, ext: Extension, stop: object, subject: Subject) -> None:
    if stop is not None and not isinstance(stop, str):
        raise TypeError(f"{point} of extension {ext.name!r} returned {stop!r}, not a str or None")
    if subject.draft is not None and not isinstance(subject.draft, Draft):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(
            f"{point} of extension {ext.name!r} left {subject.draft!r} as the draft, not a Draft"
        )


def _check_tool_hook(point: str, ext: Extension, stop: object, draft: CallDraft) -> None:
    if stop is not None and not isinstance(stop, str):
        raise TypeError(f"{point} of extension {ext.name!r} returned {stop!r}, not a str or None")
    if draft.outcome is None and point == "pre_result":
        raise TypeError(f"{point} of extension {ext.name!r} cleared the outcome it was given")
    if draft.outcome is not None and not isinstance(draft.outcome, Outcome):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(
            f"{point} of extension {ext.name!r} set the outcome to {draft.outcome!r}, "
            "not an Outcome"
        )
    if not isinstance(draft.args_raw, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(
            f"{point} of extension {ext.name!r} set args_raw to {draft.args_raw!r}, not a str"
        )


async def _finish[T](task: asyncio.Future[T]) -> None:
    """Wait for `task`, cancelling it first if we are cancelled."""
    try:
        await asyncio.wait([task])
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait([task])


class _Run:
    """The run of text, thinking, or call arguments whose deltas have arrived but whose part
    has not: its `kind` (`"text"`, `"thinking"`, `"call"`), the deltas' text, and for a call
    the id and tool `CallNamed` gave."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.chunks: list[str] = []
        self.call: CallNamed | None = None

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    def part(self) -> Part | None:
        """The part this run's text would have been; a call that never finished is not one."""
        if not self.text:
            return None
        if self.kind == "thinking":
            return Part.Thinking(self.text)
        return Part.Text(self.text) if self.kind == "text" else None

    def check(self, part: Part) -> None:
        """Raise `SampleError` unless `part` is the part this run was streaming."""
        match part:
            case Part.Text(text=text):
                kind, said = "text", text
            case Part.Thinking(text=text):
                kind, said = "thinking", text
            case Part.Call(id=cid, tool=tool, args_raw=args):
                kind, said = "call", args
                if self.call is not None and (self.call.id, self.call.tool) != (cid, tool):
                    raise SampleError(
                        f"call part {cid!r} {tool!r} does not match the call streamed "
                        f"({self.call.id!r} {self.call.tool!r})"
                    )
            case _:
                raise SampleError(f"provider yielded a part this driver does not know: {part}")
        if kind != self.kind:
            raise SampleError(f"provider yielded a {kind} part while streaming {self.kind}")
        if said != self.text:
            raise SampleError(
                f"the {kind} part {said!r} is not the text streamed for it ({self.text!r})"
            )


def _failed(error: Exception, *, internal: bool = False) -> SampleEnd:
    """The end of a failed sample. The failure goes into `meta` under `error`: `message`, and
    `status` and `retry_after` when the error has them and they are finite (strict JSON has no
    infinity). An `internal` failure (the provider raised something other than `SampleError`)
    has the message `"<Type>: <msg>"` and `internal: true`. If the provider's own `meta` has an
    `error` key, it is kept as `error.provider` beside the driver's detail. With no parts
    nothing is saved and core writes a `sample_error`."""
    meta = getattr(error, "meta", None)
    said: dict[str, object] = {} if meta is None else json.loads(meta)
    message = f"{type(error).__name__}: {error}" if internal else str(error)
    detail: dict[str, object] = {"message": message}
    for key in ("status", "retry_after"):
        value = getattr(error, key, None)
        if value is not None and not (isinstance(value, float) and not math.isfinite(value)):
            detail[key] = value
    if internal:
        detail["internal"] = True
    if "error" in said:
        detail["provider"] = said["error"]
    return SampleEnd.Failed(message, json.dumps({**said, "error": detail}, allow_nan=False))


class SampleRun:
    """One sample: streams the provider into parts and forwards its deltas to `on_delta`;
    `cancel` stops it."""

    def __init__(  # noqa: PLR0913, PLR0917 - what one sample is given
        self,
        providers: Mapping[str, Provider],
        catalog: ModelCatalog,
        session: Session,
        cursor: int,
        tools: Sequence[Tool],
        on_delta: Callable[[Delta], None],
    ) -> None:
        # Read at dispatch: I1, the request is `walk(at)` and `config_at(at)` as `at` stands.
        with trace.span("driver", "request"):
            at = session.cursor(cursor).at
            self._walk = session.tree.walk(at)
            self._config = session.tree.config_at(at)
        self._providers = providers
        self._catalog = catalog
        self._tools = tools
        self._on_delta = on_delta
        self._open: _Run | None = None
        self._parts: list[Part] = []
        self._task: asyncio.Future[Finish] | None = None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        if self._task is not None:
            self._task.cancel()

    async def _stream(self) -> Finish:
        finish: Finish | None = None
        try:
            model = self._catalog.resolve(model_name(self._config))
        except ConfigError as e:
            raise SampleError(str(e)) from e
        provider = self._providers.get(model.provider)
        if provider is None:
            raise SampleError(
                f"model {model.model!r} names provider {model.provider!r}, which is not one of "
                f"{sorted(self._providers)}; `tau config check` reports this"
            )
        if problems := provider.check(model):  # any Exception: an internal failure
            raise SampleError(f"model {model.model!r} ({provider.name}): " + "; ".join(problems))
        async with aclosing(provider.sample(self._walk, model, self._tools)) as stream:
            async for item in stream:
                if finish is not None:
                    raise SampleError("provider yielded after its finish")
                if isinstance(item, Finish):
                    if self._open is not None:
                        raise SampleError(f"provider finished with a {self._open.kind} unfinished")
                    finish = item
                elif isinstance(item, Delta):
                    self._delta(item)
                elif not isinstance(item, Part):  # pyright: ignore[reportUnnecessaryIsInstance]
                    raise SampleError(f"provider yielded {item!r}: not a part, delta or finish")
                else:
                    if self._open is not None:
                        self._open.check(item)
                    self._parts.append(item)
                    self._open = None
        if finish is None:
            raise SampleError("provider ended without a finish")
        return finish

    def _delta(self, delta: Delta) -> None:
        kind = (
            "text"
            if isinstance(delta, TextDelta)
            else "thinking"
            if isinstance(delta, ThinkingDelta)
            else "call"
        )
        if isinstance(delta, CallArgs) and (
            self._open is None or self._open.call is None or self._open.call.index != delta.index
        ):
            raise SampleError(
                f"provider streamed arguments of call {delta.index} with no such call named"
            )
        if self._open is not None and self._open.kind != kind:
            raise SampleError(f"provider streamed {kind} before finishing a {self._open.kind} part")
        if isinstance(delta, CallNamed):
            if self._open is not None:
                raise SampleError("provider named a call before finishing the one streaming")
            self._open = _Run(kind)
            self._open.call = delta
        else:
            self._open = self._open or _Run(kind)
            self._open.chunks.append(delta.text)
        self._on_delta(delta)

    def _unfinished(self) -> list[Part]:
        """The parts so far, and the text or thinking whose deltas arrived without a part."""
        open_ = self._open.part() if self._open is not None else None
        return self._parts if open_ is None else [*self._parts, open_]

    def catch_up(self) -> list[Delta]:
        """The deltas that draw what has streamed so far, for a client that starts watching
        mid-sample: each part not in the tree yet (the reply is saved when the sample ends),
        its calls numbered in order, then the text of the part still open."""
        deltas: list[Delta] = []
        calls = 0
        for part in self._parts:
            match part:
                case Part.Text(text=text) if text:
                    deltas.append(TextDelta(text))
                case Part.Thinking(text=text) if text:
                    deltas.append(ThinkingDelta(text))
                case Part.Call(id=cid, tool=tool, args_raw=args):
                    deltas += [
                        CallNamed(calls, cid, tool),
                        *([CallArgs(calls, args)] if args else []),
                    ]
                    calls += 1
                case _:
                    pass  # an empty text or thinking: nothing was drawn
        run = self._open
        if run is not None and run.text:
            if run.call is not None:
                deltas += [run.call, CallArgs(run.call.index, run.text)]
            else:
                deltas.append(
                    TextDelta(run.text) if run.kind == "text" else ThinkingDelta(run.text)
                )
        elif run is not None and run.call is not None:
            deltas.append(run.call)
        return deltas

    async def run(self) -> Answer:
        """What happened, for core to record: the parts so far and how the sample ended."""
        self._task = asyncio.ensure_future(self._stream())
        if self._cancelled:
            self._task.cancel()
        with trace.span("sample", "provider"):
            await _finish(self._task)
        if self._task.cancelled():
            return Answer.Sampled(self._unfinished(), SampleEnd.Cancelled())
        error = self._task.exception()
        if error is None:  # a sample that finished before the abort reached it stays whole
            finish = self._task.result()
            return Answer.Sampled(
                self._parts, SampleEnd.Finished(finish.stop, finish.reason, finish.meta)
            )
        if self._cancelled:  # a sample that failed after the abort is an abort
            return Answer.Sampled(self._unfinished(), SampleEnd.Cancelled())
        if isinstance(error, SampleError):
            return Answer.Sampled(self._unfinished(), _failed(error))
        if not isinstance(error, Exception):
            raise error
        # Anything else the provider raised fails this sample only; the traceback has absolute
        # paths, so it goes to stderr, never the log.
        traceback.print_exception(error, file=sys.stderr)
        return Answer.Sampled(self._unfinished(), _failed(error, internal=True))


class _HookError(Exception):
    """A tool hook failed: the call is `Err(message)`, blamed on `author`."""

    def __init__(self, author: Author, message: str) -> None:
        super().__init__(message)
        self.author = author


class CallRun:
    """One call of a batch: `pre_tool` hooks, the tool, `pre_result` hooks.

    `abort` cancels the tool; a hook that is running finishes (Q34). A tool that opted in
    (`progress_tools` names it) is given a `Progress` that turns each call into
    `on_progress(ToolProgress(node, call id, text))` on the loop while the tool runs, and does
    nothing before or after.
    """

    def __init__(  # noqa: PLR0913 - what one call needs to run
        self,
        call: RunCall,
        node: Callable[[str], NodeHandle],
        tools: Mapping[str, Tool],
        extensions: Sequence[Extension],
        *,
        context: Context,
        owners: Mapping[str, str],
        batch: int,
        progress_tools: Collection[str],
        on_progress: Callable[[ToolProgress], None],
    ) -> None:
        self.id = call.id
        self._tool = call.tool
        self._args_raw = call.args_raw
        self._node = node
        self._tools = tools
        self._extensions = extensions
        self._context = context
        self._owners = owners
        self._batch = batch
        self._progress_tools = progress_tools
        self._on_progress = on_progress
        self._task: asyncio.Future[Outcome] | None = None
        self._aborted = False
        self._showing = False

    def abort(self) -> None:
        self._aborted = True
        self._showing = False
        if self._task is not None:
            self._task.cancel()

    async def run(self) -> Answer:
        outcome, by = await self._outcome()
        return Answer.CallDone(self.id, outcome, by)

    async def _outcome(self) -> tuple[Outcome, Author]:
        # Outcomes the driver writes itself skip `pre_result`.
        if self._aborted:
            return Outcome.Interrupted(), Author.Tools()
        draft = CallDraft(self.id, self._tool, self._args_raw)
        by: Author = Author.Tools()
        try:
            answered = await self._hooks("pre_tool", draft)
            if answered is not None:
                by = answered
            if draft.outcome is None:
                if self._aborted:
                    return Outcome.Interrupted(), Author.Tools()
                draft.outcome = await self._run(draft.args_raw)
                if isinstance(draft.outcome, Outcome.Interrupted):
                    return draft.outcome, Author.Tools()
            edited = await self._hooks("pre_result", draft)
            if edited is not None:
                by = edited
        except _HookError as e:
            return Outcome.Err(str(e)), e.author
        assert draft.outcome is not None
        return draft.outcome, by

    async def _hooks(self, name: str, draft: CallDraft) -> Author | None:
        """Run each extension's tool hook, every one even after the outcome is set (a later hook
        sees it and may change it). Returns the last extension that changed the outcome. A string
        makes the outcome `Err(string)` and ends the hooks. A hook that raises or breaks its
        contract raises `_HookError`: `"Type: msg"`, its traceback on stderr."""
        changed: Author | None = None
        for ext in self._extensions:
            hook = cast("ToolHook | None", getattr(ext, name, None))
            if hook is None:
                continue
            author = Author.Extension(ext.name)
            before = draft.outcome
            handle = self._node(ext.name)
            try:
                with trace.span("hook", f"{name} {ext.name}"):
                    stop = await hook(handle, draft)
                _check_tool_hook(name, ext, stop, draft)
            except (Exception, asyncio.CancelledError) as e:
                if not _hook_failed(e):
                    raise
                raise _HookError(author, _describe(e)) from e
            finally:
                handle.retire()
            if stop is not None:
                draft.outcome = Outcome.Err(stop)
                return author
            if draft.outcome is None:  # cleared: the tool will produce it, so no one answered
                changed = None
            elif draft.outcome != before:
                changed = author
        return changed

    def _progress(self) -> Progress:
        """The tool's `Progress`: callable from any thread; the event is made on the loop,
        and only if the call is still running there."""
        loop = asyncio.get_running_loop()

        def show(text: str) -> None:
            if self._showing:
                self._on_progress(ToolProgress(self._batch, self.id, text))

        def progress(text: str) -> None:
            if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise TypeError(f"progress takes str, not {type(text).__name__}")
            if not self._showing or not text:
                return
            try:
                loop.call_soon_threadsafe(show, text)
            except RuntimeError:  # the loop is closed: the call is over
                return

        return progress

    def _start(self, tool: Tool, args_raw: str) -> asyncio.Future[Outcome]:
        """Call the tool; raises if the call is not an awaitable (or raises itself)."""
        if isinstance(tool, FunctionTool):
            self._showing = True
            call = self._function_call(tool, args_raw)
        elif self._tool in self._progress_tools:
            self._showing = True
            call = cast("ProgressTool", tool).call(args_raw, progress=self._progress())
        else:
            call = tool.call(args_raw)
        return asyncio.ensure_future(call)

    async def _function_call(self, tool: FunctionTool, args_raw: str) -> Outcome:
        """Call a tool made from a function: it is given the progress, the session `Context`,
        and (if it takes one) a handle bound to the extension that lists it, retired when the
        call is over."""
        owner = self._owners.get(tool.name)
        handle = self._node(owner) if tool.takes_handle and owner is not None else None
        try:
            return await tool.call(
                args_raw, progress=self._progress(), context=self._context, handle=handle
            )
        finally:
            if handle is not None:
                handle.retire()

    async def _run(self, args_raw: str) -> Outcome:
        tool = self._tools.get(self._tool)
        if tool is None:
            return Outcome.Err(f"unknown tool: {self._tool}")
        try:
            self._task = self._start(tool, args_raw)
        except Exception as e:  # a `call` that is not async, or raises before it returns
            self._showing = False
            return Outcome.Err(_describe(e))
        if self._aborted:
            self._task.cancel()
        try:
            with trace.span("tool", self._tool):
                await _finish(self._task)
        finally:
            self._showing = False
        if self._task.cancelled():
            return Outcome.Interrupted()
        error = self._task.exception()
        if error is not None:
            # A ToolError is a message meant for the model; anything else is a bug in the tool.
            return Outcome.Err(str(error) if isinstance(error, ToolError) else _describe(error))
        outcome = self._task.result()
        if not isinstance(outcome, Outcome.Ok | Outcome.Err):
            message = f"TypeError: tool {self._tool!r} answered {outcome!r}, not an Ok or Err"
            print(message, file=sys.stderr)
            return Outcome.Err(message)
        return outcome
