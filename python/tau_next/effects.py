"""The effects: what performing one job means, each ending in what to answer.

No effect touches the session except to read it. The driver answers.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import cast

from tau_next import trace
from tau_next._core import (
    Answer,
    Author,
    Job,
    Outcome,
    Part,
    RunCall,
    SampleEnd,
    Session,
)
from tau_next.config import ConfigError, model_name
from tau_next.config_layers import ModelCatalog
from tau_next.handles import CallDraft, Cursor, NodeHandle, Subject
from tau_next.protocols import (
    CursorHook,
    Extension,
    Finish,
    Provider,
    SampleError,
    Tool,
    ToolHook,
)


async def run_hooks(
    job: Job.Hooks,
    subject: Subject,
    extensions: Sequence[Extension],
    cursor_for: Callable[[Extension], Cursor],
) -> Answer:
    """Every extension's hook for the point, in order. The first string halts;
    an exception fails; otherwise the subject's draft comes back."""
    name = job.point.name
    for ext in extensions:
        hook = cast("CursorHook | None", getattr(ext, name, None))
        if hook is None:
            continue
        cursor = cursor_for(ext)
        try:
            with trace.span("hook", f"{name} {ext.name}"):
                stop = await hook(cursor, subject)
        except Exception as e:
            return Answer.HookFailed(str(e))
        finally:
            cursor.retire()
        if stop is not None:
            return Answer.Hooks(stop, None)
    return Answer.Hooks(None, subject.draft)


async def _finish[T](task: asyncio.Future[T]) -> None:
    """Wait for `task`, cancelling it first if we are cancelled."""
    try:
        await asyncio.wait([task])
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait([task])


class SampleRun:
    """One sample: streams the provider into parts; `cancel` stops it."""

    def __init__(
        self,
        provider: Provider,
        catalog: ModelCatalog,
        session: Session,
        cursor: int,
        tools: Sequence[Tool],
    ) -> None:
        # Read at dispatch: I1, the request is `walk(at)` and `config_at(at)` as `at` stands.
        with trace.span("driver", "request"):
            at = session.cursor(cursor).at
            self._walk = session.tree.walk(at)
            self._config = session.tree.config_at(at)
        self._provider = provider
        self._catalog = catalog
        self._tools = tools
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
        async for item in self._provider.sample(self._walk, model, self._tools):
            if finish is not None:
                raise SampleError("provider yielded after its finish")
            if isinstance(item, Finish):
                finish = item
            else:
                self._parts.append(item)
        if finish is None:
            raise SampleError("provider ended without a finish")
        return finish

    async def run(self) -> Answer:
        """What happened, for core to record: the parts so far and how the sample ended."""
        self._task = asyncio.ensure_future(self._stream())
        if self._cancelled:
            self._task.cancel()
        with trace.span("sample", "provider"):
            await _finish(self._task)
        if self._task.cancelled():
            return Answer.Sampled(self._parts, SampleEnd.Cancelled())
        error = self._task.exception()
        if error is None:
            finish = self._task.result()
            return Answer.Sampled(
                self._parts, SampleEnd.Finished(finish.stop, finish.reason, finish.meta)
            )
        if not isinstance(error, SampleError):
            raise error
        return Answer.Sampled(self._parts, SampleEnd.Failed(str(error), error.meta))


class _HookError(Exception):
    """A tool hook raised: the call is `Err("hook error")`, blamed on `author`."""

    def __init__(self, author: Author) -> None:
        super().__init__("hook error")
        self.author = author


class CallRun:
    """One call of a batch: `pre_tool` hooks, the tool, `pre_result` hooks.

    `abort` cancels the tool; a hook that is running finishes (Q34).
    """

    def __init__(
        self,
        call: RunCall,
        node: Callable[[Extension], NodeHandle],
        tools: Mapping[str, Tool],
        extensions: Sequence[Extension],
    ) -> None:
        self.id = call.id
        self._tool = call.tool
        self._args_raw = call.args_raw
        self._node = node
        self._tools = tools
        self._extensions = extensions
        self._task: asyncio.Future[Outcome] | None = None
        self._aborted = False

    def abort(self) -> None:
        self._aborted = True
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
            answered = await self._hooks("pre_tool", draft, stop_at_outcome=True)
            if isinstance(answered, Author.Extension):
                by = answered
            if draft.outcome is None:
                if self._aborted:
                    return Outcome.Interrupted(), Author.Tools()
                draft.outcome = await self._run(draft.args_raw)
                if isinstance(draft.outcome, Outcome.Interrupted):
                    return draft.outcome, Author.Tools()
            edited = await self._hooks("pre_result", draft, stop_at_outcome=False)
            if isinstance(edited, Author.Extension):
                by = edited
        except _HookError as e:
            return Outcome.Err("hook error"), e.author
        assert draft.outcome is not None
        return draft.outcome, by

    async def _hooks(self, name: str, draft: CallDraft, *, stop_at_outcome: bool) -> Author | None:
        """Run each extension's tool hook. Returns the last extension that changed
        the outcome. A string makes the outcome `Err(string)` and ends the hooks."""
        changed: Author | None = None
        for ext in self._extensions:
            hook = cast("ToolHook | None", getattr(ext, name, None))
            if hook is None:
                continue
            author = Author.Extension(ext.name)
            before = draft.outcome
            handle = self._node(ext)
            try:
                with trace.span("hook", f"{name} {ext.name}"):
                    stop = await hook(handle, draft)
            except Exception as e:
                raise _HookError(author) from e
            finally:
                handle.retire()
            if stop is not None:
                draft.outcome = Outcome.Err(stop)
                return author
            if draft.outcome != before:
                changed = author
                if stop_at_outcome:
                    return changed
        return changed

    async def _run(self, args_raw: str) -> Outcome:
        tool = self._tools.get(self._tool)
        if tool is None:
            return Outcome.Err(f"unknown tool: {self._tool}")
        self._task = asyncio.ensure_future(tool.call(args_raw))
        if self._aborted:
            self._task.cancel()
        with trace.span("tool", self._tool):
            await _finish(self._task)
        if self._task.cancelled():
            return Outcome.Interrupted()
        error = self._task.exception()
        if error is not None:
            return Outcome.Err(str(error))
        outcome = self._task.result()
        if not isinstance(outcome, Outcome.Ok | Outcome.Err):
            raise TypeError(f"tool {self._tool!r} answered {outcome!r}, not an Ok or Err")
        return outcome
