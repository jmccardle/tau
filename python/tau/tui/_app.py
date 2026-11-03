"""`TauApp` (D23): the Textual frontend over a `Client`.

The app touches only its `Client`, never `Driver`, `Session` or a provider. One listener, a worker
running `async for event in client`, feeds a `SessionView`; the widgets render from it and nothing
else holds session state. This is the first cut (8C-1): one cursor is viewed, the one given.
"""

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from time import monotonic
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.css.query import NoMatches
from textual.notifications import SeverityLevel

from tau._client import Client
from tau._events import (
    Appended,
    CursorGone,
    CursorView,
    Event,
    Live,
    Notice,
    ToolProgress,
)
from tau._protocol import Welcome
from tau._values import Json
from tau.tui._chat import ReasoningRegion, ToolBox
from tau.tui._editor import ChatInput, CommandPopup, PendingInput
from tau.tui._fields import Refused
from tau.tui._forms import QuestionBox, QuestionStack
from tau.tui._line import CommandLine
from tau.tui._status import Header, StatusBar, pending_line, status_lines
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
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "abort", "Abort", show=False),
        Binding("ctrl+c", "interrupt", "Abort / quit", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+r", "fold('thinking')", "Fold thinking", show=False),
        Binding("ctrl+t", "fold('tools')", "Fold tool boxes", show=False),
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
    ) -> None:
        super().__init__()
        self.client = client
        self.cursor = cursor
        self.welcome = welcome
        self._cwd = cwd
        self.failure: ConnectionError | None = None
        """What ended the session's stream, if it ended in error and was not reconnected."""
        self.session = SessionView()
        self.toasts: list[str] = []
        """Every toast shown, oldest first: notices, command results, refusals."""
        self._line = CommandLine(self)
        self._snapshot = tuple(snapshot)
        self._reconnect = reconnect
        self._quit_until = 0.0  # while the clock is before this, one more Ctrl+C quits
        self._theme_errors: list[str] = []
        registry = build_theme_registry(home, errors=self._theme_errors)
        self.theme_name = install_themes(self, home, theme, registry=registry).name

    # Layout

    def compose(self) -> ComposeResult:
        """The screen, top to bottom: header, transcript, open questions, pending lines, popup,
        editor, status bar."""
        yield Header()
        yield ChatDisplay(self.client.id)
        yield QuestionStack(self)
        yield PendingInput()
        yield CommandPopup()
        yield ChatInput(self._line.complete_text)
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
        """What the screen says, without its styling: the header, the transcript, the open
        questions, the pending lines, the popup and the status bar."""
        parts = [
            self.query_one(Header).text,
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
                await self._start()
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

    async def _start(self) -> None:
        """Watch the viewed cursor, ask for the commands, and take the snapshot we were given."""
        await self.client.watch([self.cursor])
        await self._line.refresh()
        snapshot, self._snapshot = self._snapshot, ()
        for event in snapshot:
            await self._hear(event)

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
        self.session.hear(event)
        match event:
            case CursorView(cursor=c) if c == self.cursor:
                await self._follow(event)
            case Live(cursor=c, delta=ToolProgress(node=node, call_id=call)) if c == self.cursor:
                self._transcript.progress(node, call, self.session.live[c].progress[(node, call)])
            case Live(cursor=c, delta=delta) if c == self.cursor:
                await self._transcript.live(delta, self.session.live[c])
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
        self.query_one(StatusBar).show(status_lines(view))
        title = " ".join((transcript.first or "(no messages)").split())[:60]
        session = "(in memory)" if self.welcome is None else self.welcome.session
        cwd = str(self._cwd) if self.welcome is None else self.welcome.cwd
        self.query_one(Header).show(title, session, cwd)

    # The editor

    def _on_text_area_changed(self, event: ChatInput.Changed) -> None:
        editor, popup = self._editor, self.query_one(CommandPopup)
        text = editor.text
        if editor.cycling:
            return
        if not text.startswith("/") or " " in text or "\n" in text:
            popup.hide()
        elif rows := self._line.rows(text):
            popup.show(rows)
        else:
            popup.warn(f"{text} is not a command: this line goes to the model as text")

    def _on_chat_input_candidates(self, event: ChatInput.Candidates) -> None:
        self.query_one(CommandPopup).show([(item, "") for item in event.items], event.selected)

    def _on_refused(self, event: Refused) -> None:
        self.toast(event.text, severity="error")

    def _on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        if event.text.strip():
            self._submit(event.text)

    @work(group="submit")
    async def _submit(self, text: str) -> None:
        await self._line.submit(text)

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

    def action_fold(self, what: str) -> None:
        """Fold or unfold the thinking regions, or the tool boxes."""
        self._transcript.fold(ReasoningRegion if what == "thinking" else ToolBox)
