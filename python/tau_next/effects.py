"""The effects: what performing one job means, each ending in what to feed.

No effect touches the session except to read it. The driver feeds.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import cast

from tau_next import trace
from tau_next._core import (
    Author,
    Body,
    Draft,
    Fed,
    Hook,
    Job,
    Outcome,
    Part,
    RunCall,
    Session,
    Stop,
)
from tau_next.handles import CallDraft, Cursor, NodeHandle, Subject
from tau_next.protocols import (
    Clock,
    CursorHook,
    Extension,
    Finish,
    Provider,
    SampleError,
    Tool,
    ToolHook,
)

HOOK_NAMES: dict[Hook, str] = {
    Hook.PreStep: "pre_step",
    Hook.PreInput: "pre_input",
    Hook.PreSample: "pre_sample",
    Hook.PreReply: "pre_reply",
    Hook.PostReply: "post_reply",
    Hook.PostResult: "post_result",
    Hook.PostStep: "post_step",
}


def subject_for(session: Session, cursor: int, job: Job.Hooks) -> Subject:
    """The subject of a `Hooks` job: the draft, the node at `at`, or the ending."""
    if job.point in (Hook.PostReply, Hook.PostResult):
        return Subject(node=session.node(session.at(cursor)))
    return Subject(draft=job.draft, ending=job.ending)


async def run_hooks(
    job: Job.Hooks,
    subject: Subject,
    extensions: Sequence[Extension],
    cursor_for: Callable[[Extension], Cursor],
) -> Fed:
    """Every extension's hook for the point, in order. The first string halts;
    an exception fails; otherwise the subject's draft comes back."""
    name = HOOK_NAMES[job.point]
    for ext in extensions:
        hook = cast("CursorHook | None", getattr(ext, name, None))
        if hook is None:
            continue
        cursor = cursor_for(ext)
        try:
            with trace.span("hook", f"{name} {ext.name}"):
                stop = await hook(cursor, subject)
        except Exception as e:
            return Fed.HookFailed(str(e))
        finally:
            cursor.retire()
        if stop is not None:
            return Fed.Hooks(stop, None)
    return Fed.Hooks(None, subject.draft)


async def wait(clock: Clock, until: int) -> Fed:
    await clock.sleep_until(until)
    return Fed.Waited()


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
        self, provider: Provider, session: Session, cursor: int, tools: Sequence[Tool]
    ) -> None:
        # Read at dispatch: I1, the request is `walk(at)` and `config_at(at)` as `at` stands.
        with trace.span("driver", "request"):
            at = session.at(cursor)
            self._walk = session.walk(at)
            self._config = session.config_at(at)
        self._provider = provider
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
        async for item in self._provider.sample(self._walk, self._config, self._tools):
            if finish is not None:
                raise SampleError("provider yielded after its finish")
            if isinstance(item, Finish):
                finish = item
            else:
                self._parts.append(item)
        if finish is None:
            raise SampleError("provider ended without a finish")
        return finish

    def _reply(self, stop: Stop, finish_reason: str | None = None) -> Draft:
        return Draft(Body.Assistant(self._parts, stop, finish_reason), Author.Provider(), [])

    async def run(self) -> Fed:
        self._task = asyncio.ensure_future(self._stream())
        if self._cancelled:
            self._task.cancel()
        with trace.span("sample", "provider"):
            await _finish(self._task)
        if self._task.cancelled():
            if not self._parts:
                return Fed.Sampled(None, None)
            return Fed.Sampled(self._reply(Stop.Interrupted), None)
        error = self._task.exception()
        if error is None:
            finish = self._task.result()
            return Fed.Sampled(self._reply(finish.stop, finish.reason), None)
        if not isinstance(error, SampleError):
            raise error
        return Fed.Sampled(self._reply(Stop.Error) if self._parts else None, str(error))


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

    async def run(self) -> Fed:
        outcome, by = await self._outcome()
        return Fed.CallDone(self.id, outcome, by)

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
