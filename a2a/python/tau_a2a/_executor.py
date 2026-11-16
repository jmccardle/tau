"""The A2A executor: one A2A task is one send to a τ cursor, followed until it rests or asks.

- A new `contextId` (the server's, or one the client chose) is a cursor opened at the fork point.
- A message with no `taskId` is a `send` on the context's cursor; one task at a time per context.
- `TASK_STATE_INPUT_REQUIRED`: an open question this client may answer. The next message on the
  same task answers it: its first data part as the value, else its text read by the schema.
- `TASK_STATE_AUTH_REQUIRED`: the step failed for a credential (HTTP 401/403, a missing or
  expired key). The next message on the task retries.
- `TASK_STATE_COMPLETED`: the step ended `Done`; the artifact is the turn's last reply.
- `TASK_STATE_FAILED`: it ended `Failed` or `Halted`, or the last reply was refused.
- `TASK_STATE_CANCELED`: `CancelTask` aborts the cursor's step and declines its questions.
- `TASK_STATE_REJECTED`: a new task on a context whose task waits for input or a credential.

Tasks on one context run one after another: a second waits for the first to end. Only a task in an
interrupted state makes a second one rejected, since waiting would wait on a person.
"""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field

from a2a.helpers.proto_helpers import (
    get_data_parts,
    get_message_text,
    new_data_part,
    new_task_from_user_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Role, TaskState
from a2a.utils.errors import InvalidParamsError
from google.protobuf import json_format

from tau import Author, Body, CallNamed, CursorGone, CursorView, Ending, Live, Part, Stop, TextDelta
from tau.commandline import value as parse_value
from tau_a2a._link import Feed, Link
from tau_a2a._store import Store

log = logging.getLogger("tau_a2a")

FORK_EXTENSION = "https://ffwf.net/a2a/ext/tau-fork/v1"
REPLY_ARTIFACT = "reply"
_CREDENTIAL = re.compile(
    r"\b(401|403)\b|unauthori[sz]ed|forbidden|authenticat|api[ _-]?key|credential|expired token",
    re.IGNORECASE,
)


@dataclass
class _Slot:
    """The task a context's cursor is working on."""

    task_id: str
    interrupted: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)


class TauExecutor(AgentExecutor):
    def __init__(self, link: Link, store: Store, fork_at: int) -> None:
        self.link = link
        self.store = store
        self.fork_at = fork_at
        self._slots: dict[int, _Slot] = {}
        self._answered: set[int] = set()

    # -- the A2A calls

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id is not None
        assert context.context_id is not None
        assert context.message is not None
        owner = context.call_context.user.user_name
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        task = context.current_task
        if task is None:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
            cursor = await self._cursor(owner, context)
            if not await self._claim(cursor, context.task_id):
                await updater.reject(
                    self._say(
                        context,
                        "this context has a task waiting for input or a credential: "
                        "answer or cancel it first",
                    )
                )
                return
            self.store.add_task(context.task_id, owner, context.context_id, cursor)
            await updater.start_work()
            feed = await self.link.follow(cursor)  # before the send: its events are in the feed
            try:
                item = await self.link.client.send(cursor, get_message_text(context.message))
                await self._follow(context, updater, cursor, feed, item)
            finally:
                feed.close()
            return
        record = self.store.task(context.task_id, owner)
        if record is None:
            raise InvalidParamsError(message=f"no task {context.task_id}")
        cursor = record.cursor
        slot = self._slots.setdefault(cursor, _Slot(context.task_id))
        slot.interrupted = False
        feed = await self.link.follow(cursor)
        try:
            item = None
            if task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
                problem = await self._answer(cursor, context.message)
                if problem is not None:
                    slot.interrupted = True
                    await updater.requires_input(self._say(context, problem))
                    return
            else:  # auth-required, or a message to a task still working: more input
                text = get_message_text(context.message)
                if text:
                    item = await self.link.client.send(cursor, text)
                else:
                    await self.link.client.step(cursor)
            await updater.start_work()
            await self._follow(context, updater, cursor, feed, item)
        finally:
            feed.close()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id is not None
        assert context.context_id is not None
        record = self.store.task(context.task_id, context.call_context.user.user_name)
        if record is not None:
            await self.link.client.abort(record.cursor)
            self._release(record.cursor, context.task_id)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    # -- contexts and their one task at a time

    async def _cursor(self, owner: str, context: RequestContext) -> int:
        assert context.context_id is not None
        assert context.message is not None
        cursor = self.store.cursor_of(owner, context.context_id)
        fork = self._fork_point(owner, context)
        if cursor is not None:
            if fork is not None:
                raise InvalidParamsError(message="tau-fork opens a new context; this one exists")
            return cursor
        cursor = await self.link.client.open(self.fork_at if fork is None else fork)
        self.store.bind(owner, context.context_id, cursor)
        return cursor

    def _fork_point(self, owner: str, context: RequestContext) -> int | None:
        """With the tau-fork extension requested and `{"at": taskId}` in the message's metadata
        under its URI: the node that task ended on (the caller's own task only)."""
        if FORK_EXTENSION not in context.call_context.requested_extensions:
            return None
        assert context.message is not None
        params = context.metadata.get(FORK_EXTENSION) or json_format.MessageToDict(
            context.message.metadata
        ).get(FORK_EXTENSION)
        at = params.get("at") if isinstance(params, dict) else None
        if not at:
            return None
        record = self.store.task(str(at), owner)
        if record is None or record.end_node is None:
            raise InvalidParamsError(message=f"tau-fork: no ended task {at}")
        return record.end_node

    async def _claim(self, cursor: int, task_id: str) -> bool:
        while (slot := self._slots.get(cursor)) is not None and slot.task_id != task_id:
            if slot.interrupted:
                return False
            await slot.done.wait()
        self._slots[cursor] = _Slot(task_id)
        return True

    def _release(self, cursor: int, task_id: str) -> None:
        slot = self._slots.get(cursor)
        if slot is not None and slot.task_id == task_id:
            del self._slots[cursor]
            slot.done.set()

    # -- following a cursor until it rests or asks

    async def _follow(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        cursor: int,
        feed: Feed,
        item: int | None,
    ) -> None:
        """Hear the cursor until it asks this client something (input-required) or rests. The
        feed was taken before the call that woke the cursor, so it can start with views from
        before it, even a whole earlier step (opening a cursor wakes it). A rest counts only
        after a view that shows this call's effect: the message pending (`send` publishes that
        before it returns), or, with no message (an answer, a retry), the cursor busy."""
        streamed = False
        stepped = False
        async for event in feed:
            if isinstance(event, CursorGone):
                await self._finish(context, updater, cursor, None, closed=True)
                return
            if isinstance(event, Live):
                if isinstance(event.delta, TextDelta):
                    await updater.add_artifact(
                        [new_text_part(event.delta.text)],
                        artifact_id=REPLY_ARTIFACT,
                        name=REPLY_ARTIFACT,
                        append=streamed,
                        last_chunk=False,
                    )
                    streamed = True
                elif isinstance(event.delta, CallNamed):
                    await updater.update_status(
                        TaskState.TASK_STATE_WORKING,
                        self._say(context, f"calling {event.delta.tool}"),
                    )
                continue
            view = event
            if (question := self._question(view)) is not None:
                self._slots[cursor].interrupted = True
                await updater.requires_input(
                    updater.new_agent_message(
                        [
                            new_text_part(question.text),
                            new_data_part(
                                {
                                    "question": question.id,
                                    "extension": question.extension,
                                    "schema": question.schema,
                                }
                            ),
                        ]
                    )
                )
                return
            pending = item is not None and any(i.id == item for i in view.items)
            stepped = stepped or (pending if item is not None else view.busy)
            if stepped and not view.busy and (not pending or not view.auto):
                await self._finish(context, updater, cursor, view)
                return

    def _question(self, view: CursorView):
        for q in view.questions:
            if q.id not in self._answered and q.to in (None, self.link.id):
                return q
        return None

    async def _answer(self, cursor: int, message: Message) -> str | None:
        """Answer the cursor's open question with the message: its first data part as the value,
        else its text read by the question's schema. A problem to say, or None."""
        view = self.link.views.get(cursor)
        question = None if view is None else self._question(view)
        if question is None:
            return None  # answered meanwhile (by a person elsewhere): carry on following
        data = get_data_parts(message.parts)
        try:
            value = data[0] if data else parse_value(get_message_text(message), question.schema)
            await self.link.client.answer(question.id, value)
        except (ValueError, TypeError) as e:
            return f"{question.text} ({e})"
        except LookupError:
            pass  # closed meanwhile
        self._answered.add(question.id)
        return None

    async def _finish(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        cursor: int,
        view: CursorView | None,
        *,
        closed: bool = False,
    ) -> None:
        assert context.task_id is not None
        if closed or view is None:
            self._release(cursor, context.task_id)
            await updater.failed(self._say(context, "the cursor was closed"))
            return
        reply, tools, refused = await self._turn(view.at)
        self.store.ended(context.task_id, view.at)
        meta = {"tau": {"node": view.at, "cursor": cursor, "tools": tools}}
        if reply is not None:
            await updater.add_artifact(
                [new_text_part(reply)],
                artifact_id=REPLY_ARTIFACT,
                name=REPLY_ARTIFACT,
                metadata=meta,
                append=False,
                last_chunk=True,
            )
        match view.ending:
            case Ending.Failed(error=error) if _CREDENTIAL.search(error):
                self._slots[cursor].interrupted = True
                await updater.requires_auth(self._say(context, error))
                return
            case Ending.Failed(error=error):
                outcome = ("failed", error)
            case Ending.Halted(reason=reason):
                outcome = ("failed", f"halted: {reason}")
            case Ending.Aborted():
                outcome = ("canceled", "aborted")
            case None if not view.auto:
                outcome = ("failed", "the message is still pending: auto-step is off")
            case _:
                outcome = ("failed", "the model refused") if refused else ("completed", reply or "")
        self._release(cursor, context.task_id)
        kind, text = outcome
        message = self._say(context, text, meta)
        if kind == "completed":
            await updater.complete(message)
        elif kind == "canceled":
            await updater.cancel(message)
        else:
            await updater.failed(message)

    async def _turn(self, at: int) -> tuple[str | None, list[str], bool]:
        """From this client's last message on the walk to `at`: the last reply's text, the tools
        called on the way (evidence: a claim with no call behind it shows as none), and whether
        the last reply was refused."""
        walk = await self.link.client.walk(at)
        start = 0
        for i, node in enumerate(walk):
            if (
                isinstance(node.body, Body.User)
                and isinstance(node.by, Author.Frontend)
                and node.by.client == self.link.id
            ):
                start = i
        tools: list[str] = []
        reply, refused = None, False
        for node in walk[start:]:
            if isinstance(node.body, Body.Assistant):
                tools += [p.tool for p in node.body.parts if isinstance(p, Part.Call)]
                text = "\n".join(p.text for p in node.body.parts if isinstance(p, Part.Text))
                reply = text.strip() or reply
                refused = node.body.stop == Stop.Refused
        return reply, tools, refused

    @staticmethod
    def _say(context: RequestContext, text: str, metadata: dict | None = None) -> Message:
        assert context.task_id is not None
        assert context.context_id is not None
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_AGENT,
            task_id=context.task_id,
            context_id=context.context_id,
            parts=[new_text_part(text)],
        )
        if metadata:
            message.metadata.update(metadata)
        return message
