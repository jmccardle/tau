"""`tau -p` (D22): the one-shot client. It opens a session (`open_client`: a new one, or the one a
`tau serve` holds), sends one message or runs one command line, waits for the cursor to rest,
answers the questions on the way by `--answer` rules (or declines them), prints the answer and
exits with a status that says how it ended.

- **stdout** is the answer: the text of the last `Assistant` node of the run's own turn (from its
  own message to the next message another client wrote), if it has no calls, taken from the node
  (`Appended`), never from `Live`. With `--json` it is instead the
  `Welcome` line and the events the client hears, one object per line, as the wire sends them.
- **stderr** gets one line each, prefixed `tau: `: every `Notice`, a hook's failure, each wait
  before a retry (`tau: HTTP 429: busy: retry 1 of 3 in 2 s`), each question answered by a rule
  or declined, why a run stopped before it finished, and the failure that ended
  it; with `--verbose` also the start report and one line per node, step and question.
- **Exit status**: 0 the model finished, 1 a failure, 2 usage, 3 stopped before finishing, 130
  interrupted (the first SIGINT aborts the cursor and waits for it to rest; the second leaves at
  once).

**Rest** is the first `CursorView` of the cursor, after the call that woke it returned, that is
not busy. The calls publish their events before they return, and the stream is read only after
`send` returned, so the view that `send` published (busy) is the first one read.
"""

import asyncio
import contextlib
import getpass
import json
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, TextIO, cast

from tau._client import Client
from tau._commands import CommandFailed
from tau._core import Author, Body, Busy, Ending, ItemData, Node, Outcome, Part
from tau._events import (
    Appended,
    CursorView,
    Event,
    HookFailure,
    Live,
    Notice,
    OpenQuestion,
    Retrying,
)
from tau._launch import AnswerRule, UsageError, message_of
from tau._open import Opened, open_client
from tau._project_read import safe_block
from tau._protocol import encode, encode_event
from tau._session_start import start_report
from tau._values import Json
from tau.commandline import STDIN, Parsed, UnknownCommand, value

_INTERRUPTED = 130
STOP_NOTICE = 2.0  # seconds a stop takes before it is said why
SIGPIPE_STATUS = 141  # 128 + SIGPIPE: the reader of standard output went away
_SHOWN = 60  # characters of a message shown in a `--verbose` line


class Process(Protocol):
    """What `tau.cli.main` was given."""

    @property
    def stdin(self) -> TextIO: ...
    @property
    def stdout(self) -> TextIO: ...
    @property
    def stderr(self) -> TextIO: ...
    @property
    def environ(self) -> Mapping[str, str]: ...
    @property
    def cwd(self) -> Path: ...


class _Refused(Exception):
    """An `--answer` rule whose value the question's schema refused."""


class _Gone(Exception):
    """Standard output has no reader any more (`tau -p --json ... | head -1`)."""


@dataclass
class _Run:
    """One run's state: the client's view of its cursor and of the nodes, and what was said."""

    opened: Opened
    out: TextIO
    err: TextIO
    rules: list[AnswerRule]
    json: bool
    verbose: bool
    views: dict[int, CursorView] = field(default_factory=lambda: {})
    nodes: dict[int, Node] = field(default_factory=lambda: {})
    tip: int | None = None  # where the cursor was when the run began, then the last node reported
    reported: set[int] = field(default_factory=lambda: set[int]())  # noqa: PLW0108
    seen: set[int] = field(default_factory=lambda: set[int]())  # noqa: PLW0108 - questions announced
    handled: set[int] = field(default_factory=lambda: set[int]())  # noqa: PLW0108 - answered or declined
    tasks: set[int] = field(default_factory=lambda: set[int]())  # noqa: PLW0108 - nodes an extension wrote while the run's cursor was busy
    sub: set[int] = field(default_factory=lambda: set[int]())  # noqa: PLW0108 - cursors this run opened
    ending: Ending | None = None
    hook_error: HookFailure | None = None
    shown: dict[str, str] = field(default_factory=lambda: {})  # each extension's line, as last seen
    stepped: bool = False

    @property
    def client(self) -> Client:
        return self.opened.client

    @property
    def cursor(self) -> int:
        return self.opened.cursor

    def note(self, text: str) -> None:
        self.err.write("tau: " + safe_block(text) + "\n")

    def write(self, stream: TextIO, text: str) -> None:
        """Write to `stream`; `_Gone` if it is standard output and nobody reads it."""
        try:
            stream.write(text)
            if stream is self.out:
                stream.flush()
        except BrokenPipeError:
            if stream is not self.out:
                raise
            raise _Gone from None

    def line(self, data: object) -> None:
        self.write(self.out, json.dumps(data) + "\n")

    @property
    def attached(self) -> bool:
        """Whether the session is a server's: its other clients may be at the same cursor."""
        return self.opened.started is None

    # What the client hears

    async def hear(self, event: Event, *, snapshot: bool = False) -> None:
        """One event: printed under `--json`, noted, remembered, and its questions dealt with.
        The snapshot is how things stood, not what the run did: no `--verbose` step lines."""
        if self.json:
            self.line(encode_event(event))
        if isinstance(event, Appended):
            self.nodes.update({n.id: n for n in event.nodes})
            if self.busy_now():
                self.tasks.update(
                    n.id
                    for n in event.nodes
                    if isinstance(n.body, Body.User) and isinstance(n.by, Author.Extension)
                )
        elif isinstance(event, Notice):
            self.note(event.text)
        elif isinstance(event, Live):
            if isinstance(event.delta, Retrying) and event.cursor == self.cursor:
                retry = event.delta
                self.note(
                    f"{retry.reason}: retry {retry.attempt} of {retry.attempts} "
                    f"in {retry.seconds:g} s"
                )
        elif isinstance(event, CursorView):
            self.views[event.cursor] = event
            if event.cursor == self.cursor:
                self.watch_cursor(event, snapshot=snapshot)
            await _questions(self, event, snapshot=snapshot)

    def watch_cursor(self, view: CursorView, *, snapshot: bool) -> None:
        """The own cursor's view: what changed since the last one is noted."""
        self.stepped = self.stepped or view.busy
        if view.hook_error is not None and view.hook_error != self.hook_error:
            e = view.hook_error
            self.note(f"hook error: {e.point} of {e.extension}: {e.error}")
        self.hook_error = view.hook_error
        if self.verbose and not snapshot:
            self.report_nodes(view)
            if view.ending is not None and view.ending != self.ending:
                self.note(f"step ended: {_ending(view.ending)}")
        self.ending = view.ending
        for who, text in view.shown:
            if self.verbose and not snapshot and self.shown.get(who) != text:
                self.note(f"{who}: {text}")  # what an extension shows, whichever it is
            self.shown[who] = text
        if self.tip is None:
            self.tip = view.at

    def chain(self, at: int) -> list[Node]:
        """The nodes this run added on the walk to `at`, oldest first."""
        found: list[Node] = []
        node = self.nodes.get(at)
        while node is not None and node.id != self.tip:
            found.append(node)
            node = None if node.parent is None else self.nodes.get(node.parent)
        return found[::-1]

    def turn(self, at: int) -> list[Node]:
        """The nodes of this run's own turn on the walk to `at`: from the first `User` node that
        is not another client's (`Frontend` of another client id) to the next one that is. On a
        cursor that other clients also use, what they wrote before and after is not the run's."""
        found: list[Node] = []
        for node in self.chain(at):
            foreign = (
                isinstance(node.body, Body.User)
                and isinstance(node.by, Author.Frontend)
                and node.by.client != self.client.id
            )
            if not found and (foreign or not isinstance(node.body, Body.User)):
                continue
            if found and foreign:
                break
            found.append(node)
        return found

    def busy_now(self) -> bool:
        view = self.views.get(self.cursor)
        return view is not None and view.busy

    def report_nodes(self, view: CursorView) -> None:
        for node in self.chain(view.at):
            if node.id not in self.reported:
                self.reported.add(node.id)
                self.note(_describe(node, self.nodes))

    # Waiting

    async def rest(self) -> CursorView:
        """Hear events until the own cursor's view is not busy; that view."""
        async for event in self.client:
            await self.hear(event)
            if isinstance(event, CursorView) and event.cursor == self.cursor and not event.busy:
                return event
        raise ConnectionError("the session's stream ended before the cursor rested")

    async def rest_if_busy(self) -> None:
        """Wait for the own cursor to rest if its last view says it is busy."""
        view = self.views.get(self.cursor)
        if view is not None and view.busy:
            await self.rest()

    async def hear_until(self, call: "asyncio.Future[str | None]") -> None:
        """Hear events while `call` runs, then those it published before it returned (the buffer
        is empty once a read started a loop turn ago has not finished)."""
        pending: asyncio.Task[Event] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(self._next())
                if call.done():
                    await asyncio.sleep(0)
                else:
                    await asyncio.wait({call, pending}, return_when=asyncio.FIRST_COMPLETED)
                if pending.done():
                    event, pending = pending.result(), None
                    await self.hear(event)
                elif call.done():
                    return
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _next(self) -> Event:
        try:
            return await anext(self.client)
        except StopAsyncIteration:
            raise ConnectionError("the session's stream ended during the run") from None

    async def abort(self) -> None:
        """Abort the own cursor, never another client's, and wait for it to rest."""
        with contextlib.suppress(ConnectionError):
            await self.client.abort(self.cursor)
        view = self.views.get(self.cursor)
        if view is None or view.busy:
            with contextlib.suppress(ConnectionError):
                await self.rest()

    def report(self) -> list[str]:
        """The start report (`--verbose`): the start's, or for a session a server holds what its
        `Welcome` and the cursor's view say."""
        opened = self.opened
        if opened.started is not None:
            return start_report(opened.started)
        assert opened.welcome is not None  # a server's client has its `Welcome`
        view = self.views.get(self.cursor)
        lines = [
            f"session {opened.welcome.session} (attached to pid {opened.welcome.pid})",
            f"working directory {opened.welcome.cwd}",
        ]
        return lines + ([f"model {view.model}"] if view is not None and view.model else [])

    # The reply

    def reply(self, at: int) -> str | None:
        """The text of the last `Assistant` node of this run's own turn on the walk to `at`, if it
        made no calls: the answer."""
        last = next(
            (n for n in reversed(self.turn(at)) if isinstance(n.body, Body.Assistant)), None
        )
        if last is None or not isinstance(last.body, Body.Assistant):
            return None
        parts = last.body.parts
        if any(isinstance(p, Part.Call) for p in parts):
            return None
        text = "\n".join(p.text for p in parts if isinstance(p, Part.Text)).rstrip("\n")
        return text or None

    def failed(self, kind: str, message: str, shown: str | None = None) -> int:
        """A failure: said on stderr, and under `--json` the last line of stdout."""
        self.note(shown or message)
        if self.json:
            self.line({"error": {"type": kind, "message": message}})
        return 1

    def verdict(self, view: CursorView, *, message: bool) -> int:
        """The exit status of a rest, said on stderr when it is not 0 (D22 section 6)."""
        code = self.ended(view.ending) if self.stepped else 0
        if code == 0 and message and view.items:
            return self.stopped("the message is still pending")
        if code == 0 and message and not view.auto:
            return self.stopped(
                "auto-step is off (a step limit, a hook, or a step that did not move)"
            )
        return code

    def ended(self, ending: Ending | None) -> int:
        """The status of how a step ended: 0 for `Done`."""
        match ending:
            case Ending.Failed(error=error):
                return self.failed("Failed", error, f"failed: {error}")
            case None:
                return self.failed("Failed", "the cursor rested with no recorded ending")
            case Ending.Halted(reason=reason):
                return self.stopped(f"halted: {reason}")
            case Ending.Aborted():
                return self.stopped("aborted")
            case _:
                return 0

    def stopped(self, why: str) -> int:
        self.note(f"stopped before finishing: {why}")
        return 3


def _ending(ending: Ending) -> str:
    match ending:
        case Ending.Halted(reason=reason):
            return f"Halted: {reason}"
        case Ending.Failed(error=error):
            return f"Failed: {error}"
        case _:
            return type(ending).__name__


def _shown(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= _SHOWN else one[: _SHOWN - 3] + "..."


def _describe(node: Node, nodes: Mapping[int, Node]) -> str:
    """A `--verbose` line for a node."""
    body = node.body
    what = _said(body, nodes, node.parent)
    return f"node {node.id} {type(body).__name__}" + (f": {what}" if what else "")


def _said(body: Body, nodes: Mapping[int, Node], parent: int | None) -> str:
    """What a node's line says of its body."""
    match body:
        case Body.User(text=text):
            return f'"{_shown(text)}"'
        case Body.Assistant(parts=parts):
            return _reply_line(parts)
        case Body.Results(outcomes=outcomes):
            return _results_line(outcomes, nodes.get(parent) if parent is not None else None)
        case Body.Custom(kind=kind):
            return kind
        case _:
            return ""


def _reply_line(parts: Sequence[Part]) -> str:
    """An `Assistant` node's text, then its calls: `"Let me read..." + 2 calls: read, bash`."""
    texts = " ".join(p.text for p in parts if isinstance(p, Part.Text))
    calls = [p.tool for p in parts if isinstance(p, Part.Call)]
    said = f'"{_shown(texts)}"' if texts.strip() else ""
    made = f"{len(calls)} call{'s' * (len(calls) != 1)}: {', '.join(calls)}" if calls else ""
    return " + ".join(x for x in (said, made) if x)


def _results_line(outcomes: Sequence[tuple[str, Outcome, Author]], before: Node | None) -> str:
    """A `Results` node's outcomes by tool, named from the `Assistant` node before it."""
    names: dict[str, str] = {}
    if before is not None and isinstance(before.body, Body.Assistant):
        names = {p.id: p.tool for p in before.body.parts if isinstance(p, Part.Call)}
    return ", ".join(f"{names.get(i, i)} {_outcome(o)}" for i, o, _ in outcomes)


def _outcome(outcome: Outcome) -> str:
    match outcome:
        case Outcome.Ok():
            return "ok"
        case Outcome.Err():
            return "error"
        case _:
            return "interrupted"


def client_id() -> str:
    """`<user>@<host>:print`, the id `-p` is as a client."""
    return f"{getpass.getuser()}@{socket.gethostname()}:print"


async def _send(run: _Run, message: str) -> int:
    """Send the message and wait for the cursor to rest."""
    run.stepped = False
    await run.client.send(run.cursor, message)
    view = await run.rest()
    code = run.verdict(view, message=True)
    if code in (0, 3):
        reply = run.reply(view.at)
        if reply is not None and not run.json:
            run.write(run.out, reply + "\n")
    return code


def _with_stdin(parsed: Parsed, piped: str) -> Parsed:
    """`parsed` with `piped` as its command's `stdin`; `UsageError` for a command with none."""
    if STDIN not in cast("Json", parsed.command.schema.get("properties", {})):
        raise UsageError(f"/{parsed.command.name} does not read standard input")
    return replace(parsed, args={**parsed.args, STDIN: piped})


async def _command(run: _Run, line: str, piped: str) -> int:
    """Run the command line, and wait for any step it caused to end. If the command sent input,
    the reply to it is printed after the command's own result, as a message's is. `piped`, what
    standard input held, goes to the command's `stdin` parameter, which a command without one
    refuses."""
    parsed = await run.client.parse_line(run.cursor, line)
    if piped:
        parsed = _with_stdin(parsed, piped)
    if parsed.missing:
        raise UsageError(f"{line.split(maxsplit=1)[0]} needs: {', '.join(parsed.missing)}")
    if parsed.extension is not None and not parsed.args:
        raise UsageError(f"{line}: say what to change: name=value")
    await run.rest_if_busy()  # loading woke the cursor: a step of its own is under way
    run.stepped = False
    call = asyncio.ensure_future(parsed.run(run.client, run.cursor))
    try:
        await run.hear_until(call)
    except BaseException:
        call.cancel()
        raise
    result = call.result()
    view = run.views[run.cursor]
    if view.busy:
        view = await run.rest()
    sent = bool(view.items) or bool(run.turn(view.at))
    code = run.verdict(view, message=sent)  # a command that sent input is held to a message's rules
    if result is not None:
        run.write(run.err if run.json else run.out, result + "\n")
    if sent and code in (0, 3):
        reply = run.reply(view.at)
        if reply is not None and not run.json:
            run.write(run.out, reply + "\n")
    return code


async def _perform(run: _Run, message: str, piped: str) -> int:
    """The message: a command line if it starts with `/` (and names a command), else text, which
    is followed by what standard input held (`piped`)."""
    if message.startswith("/"):
        try:
            return await _command(run, message, piped)
        except UnknownCommand as e:
            if ":" in e.name:  # `/skills:nope` names an extension: it cannot be text
                return run.failed("UnknownCommand", f"no command /{e.name}")
            run.note(f"no command /{e.name}: sent as text")
        except (ValueError, LookupError) as e:
            raise UsageError(str(e)) from None
    return await _send(run, f"{message}\n\n{piped}" if piped else message)


def _check_rules(run: _Run) -> None:
    """`UsageError` for a rule naming an extension this process did not load: it could never
    match. (A server's extensions are not on the wire: attached, a rule is not checked.)"""
    started = run.opened.started
    if started is None:
        return
    loaded = sorted(
        {n for s in started.sources if s.state in ("on", "always") for n in s.extensions}
    )
    for rule in run.rules:
        if rule.extension != "*" and rule.extension not in loaded:
            raise UsageError(
                f"--answer {rule.spec}: no extension {rule.extension!r} is loaded "
                f"(loaded: {', '.join(loaded)})"
            )


async def _drive(run: _Run, message: str, piped: str) -> int:
    """The run, from the snapshot to the status."""
    _check_rules(run)
    await run.client.watch([run.cursor])  # `Live`: `--json` prints it; a retry wait is told
    for event in run.opened.snapshot:
        await run.hear(event, snapshot=True)
    if run.verbose:
        for line in run.report():
            run.note(line)
    try:
        return await _perform(run, message, piped)
    except _Refused as e:
        await run.abort()
        return run.failed("RuleRefused", str(e))
    except (CommandFailed, Busy, ConnectionError) as e:
        return run.failed(type(e).__name__, str(e))


def _opened_by_run(run: _Run, cursor: int) -> bool:
    """Whether this run's cursor handed work to `cursor`, which nothing records (D24 section 7:
    no `opened_by`). It did if an extension wrote a user message (a task) onto its walk after
    the attach, while the run's own cursor was busy (a sub-agent: it opens on the session's
    prefix, not on the run's turn; or an agent the session already had, sent a follow-up), or
    its walk passes through the run's own turn. A cursor another client opens on a node that
    exists (`open(at)`) is not. Remembered once it is."""
    if cursor == run.cursor or cursor in run.sub:
        return True
    own = run.views.get(run.cursor)
    view = run.views.get(cursor)
    if own is None or view is None:
        return False
    mine = {n.id for n in run.turn(own.at)}
    task = False  # a task was written onto the walk during the run
    node = run.nodes.get(view.at)
    while node is not None:
        if node.id in mine:
            run.sub.add(cursor)
            return True
        task = task or node.id in run.tasks
        node = None if node.parent is None else run.nodes.get(node.parent)
    if task:
        run.sub.add(cursor)
        return True
    return False


async def _questions(run: _Run, view: CursorView, *, snapshot: bool) -> None:
    """Deal with the open questions `-p` may answer. Local, all of them, on any cursor.
    Attached, only those that open after the attach on the cursor it sent to or on a cursor the
    run opened (a sub-agent its step started): the snapshot's, and every other cursor's, are
    the server's other clients' (D22 section 5)."""
    for question in view.questions:
        if question.id not in run.seen and run.verbose:
            run.note(f'question {question.id} from {question.extension}: "{question.text}"')
        run.seen.add(question.id)
        if question.id in run.handled or question.to not in (None, run.client.id):
            continue
        if run.attached and (snapshot or not _opened_by_run(run, view.cursor)):
            run.handled.add(question.id)
            run.note(
                f"question {question.id} from {question.extension} on cursor {view.cursor} "
                "is left to the server's other clients"
            )
        else:
            run.handled.add(question.id)
            await _resolve(run, question, view.cursor)


async def _resolve(run: _Run, question: OpenQuestion, cursor: int) -> None:
    """Answer the question by the first rule that matches, or decline it."""
    asked = f"question {question.id} from {question.extension}"
    said = f'"{question.text}"'
    rule = next((r for r in run.rules if r.matches(question.extension, question.text)), None)
    if rule is None:
        run.note(f"declined {asked} on cursor {cursor}: {said}")
        await _answer(run, question.id, None)
        return
    try:
        await _answer(run, question.id, value(rule.value, question.schema))
    except ValueError as e:
        raise _Refused(f"--answer {rule.spec}: {asked} ({said}): {e}") from None
    run.note(f"answered {asked} with {rule.value}: {said}")


async def _answer(run: _Run, question: int, given: object) -> None:
    with contextlib.suppress(LookupError):  # closed already: the first good answer won
        await run.client.answer(question, given)


async def _take_back(run: _Run) -> None:
    """After an interrupt: a message nobody answered is taken back, so the next `-c` does not
    send two user messages in a row. Nodes are written once (I2), so its `User` node stays in
    the tree, and the cursor goes back to the node before it; a message still pending is
    removed. Once the model has replied, or this run's cursor did anything else, nothing is
    taken back."""
    view = run.views.get(run.cursor)
    if view is None:
        return
    mine = Author.Frontend(run.client.id)
    with contextlib.suppress(ConnectionError):
        for item in view.items:
            if isinstance(item.data, ItemData.Input) and item.data.by == mine:
                await run.client.remove_item(run.cursor, item.id)
        added = run.chain(view.at)
        if len(added) == 1 and isinstance(added[0].body, Body.User) and added[0].by == mine:
            assert added[0].parent is not None  # a `User` node follows the `Config`
            await run.client.go_to(run.cursor, added[0].parent)


async def _main(process: Process, args: object, message: str, piped: str) -> int:
    opened = await open_client(
        args,
        client=client_id(),
        environ=process.environ,
        cwd=process.cwd,
        model=getattr(args, "model"),  # noqa: B009
    )
    run = _Run(
        opened,
        process.stdout,
        process.stderr,
        getattr(args, "answer"),  # noqa: B009
        getattr(args, "json"),  # noqa: B009
        getattr(args, "verbose"),  # noqa: B009
    )
    try:
        for warning in opened.warnings:
            process.stderr.write("tau: " + safe_block(warning) + "\n")
        if run.json and opened.welcome is not None:
            run.line({"kind": "Welcome", **cast("Json", encode(opened.welcome))})
        try:
            return await _drive(run, message, piped)
        except asyncio.CancelledError:  # the first SIGINT
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            await run.abort()
            await _take_back(run)
            run.note("interrupted")
            return _INTERRUPTED
    finally:
        await opened.close()


def _on_sigint(loop: asyncio.AbstractEventLoop, then: Callable[[], None]) -> None:
    """Run `then` in `loop` on the next SIGINT (Ctrl+C), in place of any handler before it.
    Windows' event loop has no `add_signal_handler`: there the handler is the interpreter's,
    which runs in the main thread and hands `then` to the loop."""
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(then))
    else:
        loop.add_signal_handler(signal.SIGINT, then)


def _off_sigint(loop: asyncio.AbstractEventLoop) -> None:
    """SIGINT back to the interpreter's default."""
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, signal.default_int_handler)
    else:
        loop.remove_signal_handler(signal.SIGINT)


def _leave(process: Process) -> None:
    """The second SIGINT: say so and exit now, though a hook never returns. No cleanup runs."""
    try:
        process.stderr.write("tau: interrupted\n")
        process.stderr.flush()
        process.stdout.flush()
    finally:
        os._exit(_INTERRUPTED)


def detach(stream: TextIO) -> None:
    """Point a closed pipe's descriptor at /dev/null, so the interpreter's own flush at exit
    does not fail again (the Python documentation's advice). A stream with no descriptor, such
    as an in-memory one, has nothing to redirect."""
    try:
        descriptor = stream.fileno()
    except (OSError, ValueError):
        return
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, descriptor)
    finally:
        os.close(null)


def reader_gone(out: TextIO) -> bool:
    """Whether a `BrokenPipeError` was standard output's: what it still holds cannot be flushed.
    If it was standard error's, standard output is left alone (its buffer may go to a file)."""
    try:
        out.flush()
    except BrokenPipeError:
        return True
    return False


def print_mode(process: Process, args: object) -> int:
    """`tau -p` as `args` (parsed by `tau.cli`) say: returns the exit status. `UsageError` for
    what is wrong with the line only once it is read (`-r`, nothing to send, a command line
    without its arguments, a rule for an extension that is not loaded). Tracebacks of failed
    hooks and commands go to stderr only with `--verbose`."""
    if getattr(args, "resume"):  # noqa: B009
        raise UsageError("-r chooses a session on a terminal; -p names it: --session REF, or -c")
    message = message_of(getattr(args, "message"), process.stdin, process.stderr)  # noqa: B009

    async def main() -> int:
        task = asyncio.current_task()
        if task is None or threading.current_thread() is not threading.main_thread():
            return await _main(process, args, *message)  # signals are the main thread's
        loop = asyncio.get_running_loop()

        def again() -> None:
            _leave(process)

        def waiting() -> None:
            process.stderr.write(
                "tau: still stopping (a hook is running); Ctrl+C again leaves at once, "
                "and then the message stays in the session\n"
            )
            process.stderr.flush()

        hint: list[asyncio.TimerHandle] = []

        def first() -> None:
            _on_sigint(loop, again)
            hint.append(loop.call_later(STOP_NOTICE, waiting))
            task.cancel()

        _on_sigint(loop, first)
        try:
            return await _main(process, args, *message)
        finally:
            _off_sigint(loop)
            for timer in hint:
                timer.cancel()

    with open(os.devnull, "w", encoding="utf-8") as silence:
        shown = process.stderr if getattr(args, "verbose") else silence  # noqa: B009
        with contextlib.redirect_stderr(shown):
            try:
                return asyncio.run(main())
            except _Gone:
                detach(process.stdout)
                return SIGPIPE_STATUS
