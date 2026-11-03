"""`TauApp` (D23): the Textual frontend over a `Client`.

The app touches only its `Client`, never `Driver`, `Session` or a provider. One listener, a worker
running `async for event in client`, feeds a `SessionView`; the widgets render from it and nothing
else holds session state. One cursor is viewed at a time (`view` switches it); the cursor bar
shows the rest.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from time import monotonic
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.command import Provider
from textual.css.query import NoMatches
from textual.notifications import SeverityLevel

from tau._client import Client
from tau._core import Author, ItemData
from tau._events import (
    Appended,
    CursorGone,
    CursorView,
    Event,
    Labels,
    Live,
    Notice,
    ToolProgress,
)
from tau._protocol import Welcome
from tau._values import Json
from tau.tui._chat import ReasoningRegion, ToolBox
from tau.tui._editor import ChatInput, CommandPopup, Composer, PendingInput
from tau.tui._fields import Refused
from tau.tui._forms import QuestionBox, QuestionStack
from tau.tui._line import CommandLine
from tau.tui._moves import Moves
from tau.tui._palette import TauCommands
from tau.tui._status import CursorBar, Header, StatusBar, bar_text, pending_line, status_lines
from tau.tui._switch import Switch
from tau.tui._themes import build_theme_registry, install_themes
from tau.tui._transcript import ChatDisplay
from tau.tui._view import SessionView

_QUIT_WINDOW = 3.0  # seconds a first Ctrl+C waits for the second


class TauApp(App[None]):
    """The terminal UI of one session, over `client`, viewing `cursor`.

    `welcome` is the server's (or the local start's) answer: the session id and working directory
    the header shows (`None` for a session kept in memory, whose header shows `cwd`, the directory
    it works in). `snapshot` are the events the client's
    stream already gave up (`Opened.snapshot`); a client whose stream has not been read needs
    none. `theme` names one of the themes (`None`: the default); user themes are read from
    `home`, which is `TAU_HOME`. `reconnect`, for a remote client, makes a new client with the
    same id; without it a stream that ends in `ConnectionError` ends the app's session: the
    failure is shown, the transcript stays readable and `return_code` is 1 once the person quits.

    The keys: Enter sends, Ctrl+J starts a new line, Tab completes, Esc aborts the cursor's step
    (or declines the question the keyboard is in), Ctrl+C aborts if busy, else clears the draft,
    else quits when pressed twice, Ctrl+R folds the thinking and Ctrl+T the tool boxes, Ctrl+Q
    quits. `/` starts a command; one missing arguments opens a form for them.
    """

    CSS_PATH = "tau.tcss"
    COMMANDS: ClassVar[set[type[Provider] | Callable[[], type[Provider]]]] = {TauCommands}
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "abort", "Abort", show=False),
        Binding("ctrl+c", "interrupt", "Abort / quit", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+r", "fold('thinking')", "Fold thinking", show=False),
        Binding("ctrl+t", "fold('tools')", "Fold tool boxes", show=False),
        Binding("alt+left", "move('previous')", "Previous cursor", show=False),
        Binding("alt+right", "move('next')", "Next cursor", show=False),
        Binding("ctrl+g", "move('tree')", "The tree", show=False),
        Binding("ctrl+o", "move('sessions')", "Sessions", show=False),
        Binding("ctrl+n", "move('new')", "New session", show=False),
        Binding("ctrl+up", "scroll('up')", "Scroll up", show=False),
        Binding("ctrl+down", "scroll('down')", "Scroll down", show=False),
    ]

    def __init__(  # noqa: PLR0913 - what one frontend of one session is given
        self,
        client: Client,
        cursor: int,
        *,
        welcome: Welcome | None,
        home: Path,
        cwd: Path,
        snapshot: Sequence[Event] = (),
        theme: str | None = None,
        reconnect: Callable[[], Awaitable[Client]] | None = None,
        sessions: Path | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.cursor = cursor
        self.welcome = welcome
        self.home = home
        self.cwd = cwd
        self.sessions = sessions
        self.switch: Switch | None = None
        """Where the person asked to go (Ctrl+O, Ctrl+N), once the app has left for it."""
        self.failure: ConnectionError | None = None
        """What ended the session's stream, if it ended in error and was not reconnected."""
        self.session = SessionView()
        self.toasts: list[str] = []
        """Every toast shown, oldest first: notices, command results, refusals."""
        self._line = CommandLine(self)
        self._moves = Moves(self)
        self._drawing = asyncio.Lock()  # one event, or one switch of cursor, draws at a time
        self._snapshot = tuple(snapshot)
        self._reconnect = reconnect
        self._quit_until = 0.0  # while the clock is before this, one more Ctrl+C quits
        self._theme_errors: list[str] = []
        registry = build_theme_registry(home, errors=self._theme_errors)
        install_themes(self, home, theme, registry=registry)

    # Layout

    def compose(self) -> ComposeResult:
        """The screen, top to bottom: header, cursor bar, transcript, open questions, pending
        lines, popup and editor, status bar."""
        yield Header()
        yield CursorBar()
        yield ChatDisplay(self.client.id, self._drawing, lambda: self.session.live.get(self.cursor))
        yield QuestionStack(self)
        yield PendingInput()
        yield Composer(
            self._line.complete_text, self._line.rows, self._line.submit, self.action_move
        )
        yield StatusBar()

    @property
    def _transcript(self) -> ChatDisplay:
        return self.query_one(ChatDisplay)

    @property
    def _editor(self) -> ChatInput:
        return self.query_one(ChatInput)

    @property
    def draft(self) -> str:
        """The text in the editor."""
        return self._editor.text

    def restore_draft(self, text: str) -> None:
        """Give the editor back a line that was not used."""
        self._editor.restore(text)

    def focus_editor(self) -> None:
        """Put the keyboard in the editor."""
        self._editor.focus()

    async def complete_value(self, schema: Json, text: str) -> list[str]:
        """What could finish `text` as a value of `schema` (a question's answer)."""
        return await self._line.complete_value(schema, text)

    def _on_mount(self) -> None:
        for problem in self._theme_errors:
            self.toast(problem, severity="error", timeout=10)
        self.focus_editor()
        self.run_worker(self._listen(), name="listener")

    def toast(
        self,
        text: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float = 5,
    ) -> None:
        """Show a toast, and keep it in `toasts`."""
        self.toasts.append(f"{title}: {text}" if title else text)
        self.notify(text, title=title, severity=severity, timeout=timeout)

    def screen_text(self) -> str:
        """What the screen says, without its styling: the header, the cursor bar, the transcript,
        the open questions, the pending lines, the popup and the status bar."""
        parts = [
            self.query_one(Header).text,
            self.query_one(CursorBar).text,
            self._transcript.text(self.session.live.get(self.cursor)),
            *(box.text for box in self.query(QuestionBox)),
            self.query_one(PendingInput).text,
            self.query_one(CommandPopup).text,
            self.query_one(StatusBar).text,
        ]
        return "\n".join(p for p in parts if p)

    # The listener

    async def _listen(self) -> None:
        """Take the client's events into the `SessionView` and onto the screen, until the stream
        ends. A stream that ends in error is a `ConnectionError`: reconnect if the app can, else
        show it."""
        while True:
            try:
                await self.client.watch([self.cursor])
                await self._line.refresh()
                snapshot, self._snapshot = self._snapshot, ()
                for event in snapshot:
                    await self._hear(event)
                async for event in self.client:
                    await self._hear(event)
            except ConnectionError as e:
                if not await self._lost(e):
                    return
                continue
            except NoMatches:
                if not self.is_running:
                    return  # an event drawn into a screen that is already gone
                raise
            self.toast("the session has ended", severity="warning")
            self.query_one(StatusBar).show(["the session has ended"])
            return

    async def _lost(self, error: ConnectionError) -> bool:
        """The stream ended in `error`. Make a new client when we can, and say whether to go on."""
        if self._reconnect is not None:
            self.toast(f"connection lost ({error}); reconnecting", severity="warning")
            try:
                self.client = await self._reconnect()
            except OSError as e:
                error = ConnectionError(f"could not reconnect: {e}")
            else:
                self.session = SessionView()
                await self._transcript.end_live()  # the new stream catches the reply up again
                return True
        self.failure = error
        self.toast(str(error), title="session lost", severity="error", timeout=15)
        self.query_one(StatusBar).show([f"session lost: {error}"])
        return False

    async def _hear(self, event: Event) -> None:
        async with self._drawing:
            self.session.hear(event)
            match event:
                case CursorView(cursor=c) if c == self.cursor:
                    await self._follow(event)
                case Live(cursor=c, delta=ToolProgress(node=node, call_id=call)) if (
                    c == self.cursor
                ):
                    self._transcript.progress(
                        node, call, self.session.live[c].progress[(node, call)]
                    )
                case Live(cursor=c, delta=delta) if c == self.cursor:
                    await self._transcript.live(delta, self.session.live[c])
                case CursorGone(cursor=c) if c == self.cursor and self.session.views:
                    self.toast(f"cursor {c} was closed", severity="warning")
                    await self._show(min(self.session.views))
                case CursorGone(cursor=c) if c == self.cursor:
                    self.failure = ConnectionError(f"cursor {c} was closed")
                    self.query_one(StatusBar).show([f"cursor {c} was closed"])
                case Appended(nodes=nodes):
                    for node in nodes:
                        if self._transcript.is_failed_sample_here(node):
                            await self._transcript.add_side(node)
                case Notice(text=text):
                    self.toast(text)
                case _:
                    pass
            if isinstance(event, CursorView | CursorGone | Labels):
                self._refresh_bar()

    def _refresh_bar(self) -> None:
        text = bar_text(self.session.views, self.session.labels, self.cursor)
        self.query_one(CursorBar).show(text)

    async def view(self, cursor: int) -> None:
        """View `cursor`, an open one: watch it (and so stop hearing the last one's `Live`), read
        its walk again and show its state. `LookupError` for a cursor that is not open."""
        async with self._drawing:
            await self._show(cursor)

    async def _show(self, cursor: int) -> None:
        if cursor not in self.session.views:
            raise LookupError(f"cursor {cursor} is not open")
        await self.client.watch([cursor])
        self.session.forget_live(self.cursor)  # its deltas are not heard now; a later watch
        self.session.forget_live(cursor)  # replays what the sample in flight has said
        self.cursor = cursor
        view = self.session.views[cursor]
        await self._transcript.rebuild(await self.client.walk(view.at))
        await self._line.refresh()
        await self._follow(view)
        self._refresh_bar()

    async def _follow(self, view: CursorView) -> None:
        """The viewed cursor's state changed: extend the transcript or read the walk again, then
        show its questions, pending lines and status."""
        transcript = self._transcript
        if view.at != transcript.tip:
            path = None
            if transcript.tip is not None:
                path = self.session.path(transcript.tip, view.at)
            if path is None:
                await transcript.rebuild(await self.client.walk(view.at))
            else:
                await transcript.end_live()
                for node in path:
                    await transcript.add(node)
        layer = self.session.live.get(view.cursor)
        if transcript.streaming and (layer is None or layer.at is None):
            await transcript.end_live()
        await self.query_one(QuestionStack).follow(view)
        me = self.client.id
        pending = [pending_line(i.data, me) for i in view.items]
        self.query_one(PendingInput).show(pending)
        self._editor.reclaimable = any(
            isinstance(i.data, ItemData.Input) and i.data.by == Author.Frontend(me)
            for i in view.items
        )
        self.query_one(StatusBar).show(status_lines(view))
        title = " ".join((transcript.first or "(no messages)").split())[:60]
        session = "(in memory)" if self.welcome is None else self.welcome.session
        cwd = str(self.cwd) if self.welcome is None else self.welcome.cwd
        self.query_one(Header).show(title, session, cwd)

    # The editor

    def _on_refused(self, event: Refused) -> None:
        self.toast(event.text, severity="error")

    # Keys

    async def action_abort(self) -> None:
        """Abort the viewed cursor's step (it declines the questions it has open)."""
        try:
            await self.client.abort(self.cursor)
        except ConnectionError as e:
            self.toast(str(e), severity="error")

    async def action_interrupt(self) -> None:
        """Ctrl+C: abort if busy, else clear the draft, else quit when pressed twice."""
        view = self.session.views.get(self.cursor)
        if view is not None and view.busy:
            await self.action_abort()
        elif self._editor.text:
            self._editor.clear_draft()
        elif monotonic() < self._quit_until:
            await self.action_quit()
        else:
            self._quit_until = monotonic() + _QUIT_WINDOW
            self.toast("press Ctrl+C again to quit")

    async def action_quit(self) -> None:
        """Leave. The exit status is 1 if the session was lost."""
        self.exit(return_code=0 if self.failure is None else 1)

    def action_move(self, what: str) -> None:
        """Alt+Left, Alt+Right, Ctrl+Z, Ctrl+G, Ctrl+O, Ctrl+N: a move (`tau.tui._moves`), a task
        of its own because it may ask a question."""
        self.run_worker(self._moves.run(what), group="moves", exclusive=True)

    def action_scroll(self, direction: str) -> None:
        """Ctrl+Up, Ctrl+Down: a page of the transcript; against its edge, the render window
        slides a turn."""
        self._transcript.scroll_page(direction)

    def action_fold(self, what: str) -> None:
        """Fold or unfold the thinking regions, or the tool boxes."""
        self._transcript.fold(ReasoningRegion if what == "thinking" else ToolBox)
