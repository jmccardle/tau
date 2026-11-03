"""Moving around the session (D23 sections 2, 5, 6): what the cursor keys do.

Alt+Left and Alt+Right view the previous or next open cursor; Ctrl+Z edits the last message;
Ctrl+G opens the tree screen; Ctrl+O the session picker; the palette's Theme picks a theme and saves
it (`tui.theme` of the user's config file); Ctrl+N asks for a new session; Up on an
empty editor takes back a line this client has waiting. Each is a call or two on the `Client` and
a dialog; none changes the screen itself, `TauApp.view` does. A refusal (the session cannot take
the call, a connection lost) is a toast, never a crash.
"""

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from textual.notifications import SeverityLevel
from textual.screen import Screen
from textual.theme import Theme

from tau._client import Client
from tau._core import Author, Body, Busy, ItemData, NoSuchCursor
from tau._events import CursorView
from tau._protocol import Welcome
from tau._sessions import SessionInfo, is_leased, list_sessions
from tau._user_config import UserConfig
from tau.tui._choose import PickDialog
from tau.tui._dialogs import ConfirmDialog
from tau.tui._picker import SessionPicker
from tau.tui._switch import Switch, session_holders
from tau.tui._tree import TreeIntent, TreeScreen, read_nodes
from tau.tui._view import SessionView


class Mover(Protocol):
    """What the moves need of the app."""

    client: Client
    cursor: int
    session: SessionView
    switch: Switch | None
    home: Path
    sessions: Path | None
    cwd: Path
    welcome: Welcome | None

    theme: Any  # Textual's reactive, a `str` to read and to set

    @property
    def available_themes(self) -> Mapping[str, Theme]: ...

    def toast(
        self, text: str, *, title: str = "", severity: SeverityLevel = "information"
    ) -> None: ...

    def restore_draft(self, text: str) -> None: ...

    async def view(self, cursor: int) -> None: ...

    async def push_screen_wait[T](self, screen: Screen[T]) -> T: ...

    async def action_quit(self) -> None: ...


class Moves:
    """The moves of one app. A move that asks a question or reads the session first holds on to the
    cursor that was viewed when it began: the view may move while a dialog is up, and a choice
    made about one cursor is never carried out on another."""

    def __init__(self, app: Mover) -> None:
        self.app = app
        self._by_name: dict[str, Callable[[int], Awaitable[None]]] = {
            "previous": lambda cursor: self._step(cursor, -1),
            "next": lambda cursor: self._step(cursor, 1),
            "edit": self._edit_last,
            "tree": self._tree,
            "sessions": self._pick_session,
            "new": self._new_session,
            "reclaim": self._reclaim,
            "theme": self._theme,
        }

    async def run(self, name: str) -> None:
        """Do the move `name` on the cursor viewed now; a refusal is a toast."""
        try:
            await self._by_name[name](self.app.cursor)
        except (ConnectionError, LookupError, ValueError, PermissionError, Busy) as e:
            self.app.toast(str(e), severity="error")

    def _view(self, cursor: int) -> CursorView:
        """`cursor`'s view; `NoSuchCursor` if it has been closed since the move began."""
        view = self.app.session.views.get(cursor)
        if view is None:
            raise NoSuchCursor(f"cursor {cursor} is closed")
        return view

    async def _step(self, cursor: int, by: int) -> None:
        """View the open cursor `by` places along (around the ends)."""
        ids = sorted(self.app.session.views)
        if len(ids) < 2:  # noqa: PLR2004 - one cursor has nowhere to go
            self.app.toast("there is only one cursor")
            return
        await self.app.view(ids[(ids.index(cursor) + by) % len(ids)])

    async def _leave(self, cursor: int, what: str) -> bool:
        """Whether to go on: a busy cursor's step is aborted by moving it (or by leaving), so ask
        first."""
        if not self._view(cursor).busy:
            return True
        return await self.app.push_screen_wait(
            ConfirmDialog("Abort the step?", f"The cursor is working. {what} aborts its step.")
        )

    async def _edit_last(self, cursor: int) -> None:
        """Ctrl+Z on an empty editor: go to the node before the last `User` node of the walk and
        put its text in the editor (0.15's rollback, as a branch: the old turn stays in the
        tree)."""
        app = self.app
        walk = await app.client.walk(self._view(cursor).at)
        last = next((n for n in reversed(walk) if isinstance(n.body, Body.User)), None)
        if last is None or last.parent is None or not isinstance(last.body, Body.User):
            app.toast("there is no message to edit")
            return
        if not await self._leave(cursor, "Editing the last message"):
            return
        await app.client.go_to(cursor, last.parent)
        app.restore_draft(last.body.text)

    async def _tree(self, cursor: int) -> None:
        """Ctrl+G: the tree screen, and the call it came back with, made on the cursor that was
        viewed when it was asked (the one it opened on, unless the person chose another there)."""
        app = self.app
        nodes = await read_nodes(app.client)
        screen = TreeScreen(nodes, app, cursor)
        intent: TreeIntent | None = await app.push_screen_wait(screen)
        if intent is None:
            return
        cursor = intent.cursor  # `v` in the tree may have changed the viewed cursor
        match intent.action:
            case "go":
                if await self._leave(cursor, f"Going to node {intent.node}"):
                    await app.client.go_to(cursor, intent.node)
            case "open":
                new = await app.client.open(intent.node)
                app.toast(f"opened cursor {new} at node {intent.node}; Alt+Right views it")
            case "label":
                assert intent.text is not None
                await app.client.set_label(self._view(cursor).cursor, intent.text)

    async def _pick_session(self, _cursor: int) -> None:
        """Ctrl+O: choose a session; the app leaves for it (the launch re-executes `tau`)."""
        app = self.app
        if app.sessions is None:
            raise ValueError("this app was not given a sessions directory to list")
        rows: list[SessionInfo] = list_sessions(app.sessions)
        current = None if app.welcome is None else app.welcome.session
        held = await session_holders(app.home, rows, current)
        picked: SessionInfo | None = await app.push_screen_wait(
            SessionPicker(rows, app.cwd, held=held)
        )
        if picked is None or picked.id == current:
            return
        served = picked.id in held and held[picked.id].state == "served"
        if not served and is_leased(picked.path):  # it may have been taken since the listing
            raise PermissionError(f"session {picked.id} is in use by another tau process")
        app.switch = Switch(picked.id, served)
        await app.action_quit()

    async def _new_session(self, cursor: int) -> None:
        """Ctrl+N: the app leaves for a new session (asking first if a step is running)."""
        if await self._leave(cursor, "Starting a new session"):
            self.app.switch = Switch(None)
            await self.app.action_quit()

    async def _theme(self, _cursor: int) -> None:
        """The palette's Theme: choose one of the themes; it is saved as the user's theme for
        every later `tau` and then worn. A file that cannot be written is a toast, and the theme
        stays as it was."""
        app = self.app
        picked = await app.push_screen_wait(
            PickDialog("Theme", sorted(app.available_themes), app.theme)
        )
        if picked is None:
            return
        UserConfig(app.home).save_theme(picked)
        app.theme = picked
        app.toast(f"{picked} (saved for every session)", title="theme")

    async def _reclaim(self, cursor: int) -> None:
        """Up on an empty editor: take back the last line this client has waiting in the
        scratchpad (`remove_item`) and put it in the editor. One the cursor took already is a
        toast."""
        app = self.app
        me = Author.Frontend(app.client.id)
        mine = [
            i
            for i in self._view(cursor).items
            if isinstance(i.data, ItemData.Input) and i.data.by == me
        ]
        if not mine:
            return
        taken = mine[-1]
        assert isinstance(taken.data, ItemData.Input)
        try:
            await app.client.remove_item(cursor, taken.id)
        except LookupError:
            app.toast("the cursor took that line already")
            return
        app.restore_draft(taken.data.text)
