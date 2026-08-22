"""The session picker — choose among SAVED sessions (Phase B).

``SessionPickerModal`` is the modal ``tau --resume`` and the palette's "Resume
session…" entry open: a list of the sessions on disk, newest first, scoped to the
current working directory with a ``Tab`` toggle to every directory. Picking one
dismisses with its :class:`~tau_agent_core.session_catalog.SessionInfo` ``ref``,
which the app hands to the *existing* load path (``ChatSelected`` →
``Parley.on_chat_selected`` → ``SessionCatalog.load``) — one loader, two entry
points.

**This is not the tree browser.** ``app.SessionTreeModal`` navigates the
conversation tree *inside one session*; this chooses *which session*. They share
nothing but the word "session" and are deliberately separate screens.

It lives in its own module rather than in ``app.py`` because it needs nothing
from the app — a :class:`~tau_agent_core.session_catalog.SessionCatalog` and a cwd
string are its whole input — and ``app.py`` is already 5,000 lines of things that
do need each other.

Reference: docs/SESSION-UX-REDESIGN.md §6 (the picker), §5.7 (``SessionInfo``),
§5.8 (listing & cwd scoping). pi parity:
packages/coding-agent/src/modes/interactive/components/session-selector.ts.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.fuzzy import Matcher
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static
from textual.worker import get_current_worker

from tau_agent_core.session_catalog import SessionCatalog, SessionInfo

#: The two listing scopes (§5.8). ``SCOPE_CWD`` lists one dashed-cwd directory,
#: which is the cheap case the on-disk partitioning exists to make cheap;
#: ``SCOPE_ALL`` walks every directory under the session base. pi's picker calls
#: the same pair "Current" and "All" and toggles them with Tab.
SCOPE_CWD = "cwd"
SCOPE_ALL = "all"


def format_age(then: datetime, now: datetime) -> str:
    """``then`` as an age relative to ``now``: ``"12s"``, ``"5m"``, ``"3h"``, ``"2d"``.

    Both arguments are explicit so the formatting has no clock of its own. That is
    not a testing convenience — it is the rule ``tagline.py`` states for
    randomness applied to time: the picker reads the wall clock in exactly ONE
    place (:meth:`SessionPickerModal._populate`, once per repopulate, so every row
    in a frame is measured against the same instant) and everything downstream of
    it is a pure function.

    A ``then`` in the future is clamped to ``0s``. A negative age is not a
    condition the picker can act on — it means the file was written by a machine
    whose clock disagrees with this one — and rendering ``-4s`` in the Updated
    column reads as a bug in this table rather than as the clock skew it is.
    """
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 365:
        return f"{days}d"
    return f"{days // 365}y"


def home_relative(path: str) -> str:
    """``/home/ada/Development/tau`` → ``~/Development/tau``.

    The status line has one row and a path is the longest thing on it, so the
    prefix every path on a machine shares is the cheapest thing to drop. Only an
    exact home-directory prefix is folded — a sibling like ``/home/adam`` is
    not inside ``/home/ada`` and must not be rewritten as though it were.
    """
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def elide_start(text: str, width: int) -> str:
    """*text* cut at the FRONT, with a leading ``…`` where the cut is.

    The opposite end from ``app._elide``, and for a reason: a message preview
    reads left to right, so cutting its tail keeps the part that identifies it,
    while a path's identifying part is its LAST component. Cutting
    ``/home/ada/Development/agent-harness-py`` down to
    ``/home/ada/Development/ag…`` says only that it is somewhere under
    ``Development``, which every row in the column already said.

    A width with no room for the marker plus a character returns the text
    unchanged, matching ``_elide``: a label elided to nothing tells the reader
    less than one that overflows visibly.
    """
    if width < 2 or len(text) <= width:
        return text
    return "…" + text[-(width - 1) :]


def search_text(info: SessionInfo) -> str:
    """The haystack the ``/`` filter matches against (§6: name / first / last).

    One string rather than three separate matches, so a query may span the fields
    the way a person remembers a conversation ("the compaction one where it read
    session_manager").
    """
    return " ".join(part for part in (info.name or "", info.first_message, info.last_message))


class SessionPickerModal(ModalScreen[Optional[str]]):
    """Pick a saved session; dismiss with its ``ref``, or ``None`` on cancel.

    Built on ``DataTable(cursor_type="row")`` because it is the only Textual
    widget with genuinely aligned columns — ``OptionList`` fakes them with padding
    that drifts the moment a title contains a wide glyph, and ``SelectionList``
    says "choose several", which is the wrong question (§6).

    The listing is fetched by a ``@work(thread=True)`` worker: ``catalog.list()``
    is a blocking call, and for the JMFTS-backed catalog a genuinely slow one
    (the sidebar's own docstring measures it at ~154 ms over 18 HTTP requests).
    The result lands on the ``sessions`` reactive from the UI thread, whose
    watcher repopulates the table — the same shape ``ChatSidebar.refresh_chats``
    uses, for the same reason.
    """

    BINDINGS = [
        # Tab is Screen's focus-next key. It is rebound rather than shared because
        # this screen has exactly two focus targets and an explicit key for each
        # (`/` to the filter, Esc back to the table), so focus cycling buys
        # nothing — and Current↔All is the toggle pi's picker puts on Tab.
        Binding("tab", "toggle_scope", "Current ↔ all"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("escape", "cancel", "Cancel"),
    ]

    #: Fixed widths for the two narrow columns, so the title column gets whatever
    #: the dialog has left. Each is its own HEADER's width, which is the real
    #: floor — a column narrower than its label draws "Update" over a list of
    #: ages, and a header nobody can read is worse than a column of slack.
    UPDATED_WIDTH = 7
    MSGS_WIDTH = 4
    #: The directory column, drawn only in ``SCOPE_ALL`` — in cwd scope every row
    #: has the same one and it would be a column of identical text.
    CWD_WIDTH = 26
    #: Below this the title column is too narrow to identify a conversation, so
    #: the table is allowed to overflow into its own horizontal scrollbar instead
    #: of eliding every row down to nothing.
    MIN_TITLE_WIDTH = 20

    #: ``always_update`` because a reactive only notifies on a CHANGED value, and
    #: two empty listings compare equal: a directory with no sessions assigned
    #: ``[]`` over the initial ``[]``, the watcher never ran, and the picker sat on
    #: "Loading…" forever — a load that finished looking exactly like one that
    #: hung. The listing is a snapshot of the disk, not a value to diff.
    sessions: reactive[list[SessionInfo]] = reactive(list, init=False, always_update=True)
    scope: reactive[str] = reactive(SCOPE_CWD, init=False)
    filter_query: reactive[str] = reactive("", init=False)

    def __init__(self, catalog: SessionCatalog, cwd: str) -> None:
        super().__init__()
        self._catalog = catalog
        self._cwd = cwd
        #: True between starting a load and applying its result — the status line
        #: says "Loading…" rather than "0 sessions", which is a different claim.
        self._loading = True

    # -- composition ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Container(id="session-picker-dialog"):
            yield Static("Resume a session", id="session-picker-title")
            yield Input(placeholder="filter sessions…", id="session-picker-filter")
            table: DataTable[Text] = DataTable(id="session-picker-table")
            table.cursor_type = "row"
            yield table
            yield Static("", id="session-picker-status")
            # Inside the dialog, not beside it: `ModalScreen { align: center
            # middle }` in parley.tcss centres every direct child of the screen,
            # and a bottom-docked Footer is the one child that must not be.
            yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.focus()
        self._reload()

    # -- loading (§5.8) ------------------------------------------------------

    def _reload(self) -> None:
        self._loading = True
        self._refresh_status()
        self._load_sessions()

    @work(thread=True, exclusive=True, group="session-picker-load")
    def _load_sessions(self) -> None:
        """The blocking listing, off the event loop.

        ``exclusive=True`` cancels a still-running load when the scope is toggled
        again before the first one lands. Cancellation only flips
        ``worker.is_cancelled`` — a thread already inside ``catalog.list()`` runs
        to completion — so the scope is captured before the call and re-checked
        after it, and a superseded result is dropped rather than painted over the
        newer one.
        """
        worker = get_current_worker()
        scope = self.scope
        infos = self._catalog.list(self._cwd if scope == SCOPE_CWD else None)
        if worker.is_cancelled:
            return
        self.app.call_from_thread(self._apply_sessions, scope, infos)

    def _apply_sessions(self, scope: str, infos: list[SessionInfo]) -> None:
        """Runs on the UI thread — the only place ``sessions`` is written."""
        if scope != self.scope:
            return
        self._loading = False
        self.sessions = infos

    # -- reactives -----------------------------------------------------------

    def watch_sessions(self, sessions: list[SessionInfo]) -> None:
        self._populate()

    def watch_scope(self, scope: str) -> None:
        self._reload()

    def watch_filter_query(self, query: str) -> None:
        self._populate()

    # -- the table -----------------------------------------------------------

    def visible_sessions(self) -> list[SessionInfo]:
        """The rows the current filter admits, in catalog order (newest first).

        ``textual.fuzzy.Matcher`` scores a candidate and returns ``0.0`` for no
        match, so the filter is "score above zero" and the ORDER is left alone.
        Re-sorting by score would mean the list reshuffles under the cursor as the
        user types, and recency is the ordering a resume list is read in.
        """
        if not self.filter_query:
            return list(self.sessions)
        matcher = Matcher(self.filter_query)
        return [info for info in self.sessions if matcher.match(search_text(info)) > 0]

    def _title_width(self, table: DataTable[Text]) -> int:
        """Columns left for the title once the fixed ones and their padding are paid."""
        columns = 4 if self.scope == SCOPE_ALL else 3
        fixed = self.UPDATED_WIDTH + self.MSGS_WIDTH
        if self.scope == SCOPE_ALL:
            fixed += self.CWD_WIDTH
        padding = 2 * table.cell_padding * columns
        return max(self.MIN_TITLE_WIDTH, table.content_size.width - fixed - padding)

    @staticmethod
    def _cell(text: str, width: int) -> Text:
        """A cell that is cut with a visible ``…`` rather than wrapped or clipped.

        ``DataTable`` gives an over-long cell in a fixed-width column to Rich,
        which wraps it — turning one session into three rows and pushing the rest
        off the screen. ``no_wrap`` + ``overflow="ellipsis"`` is the same contract
        the tree browser's ``_elide`` states: the reader can see there is more.
        """
        return Text(text.replace("\n", " "), no_wrap=True, overflow="ellipsis")

    @classmethod
    def _path_cell(cls, path: str, width: int) -> Text:
        """A directory cell: home-folded, and cut at the front (:func:`elide_start`)."""
        return cls._cell(elide_start(home_relative(path), width), width)

    def _populate(self) -> None:
        table = self.query_one(DataTable)
        table.clear(columns=True)
        title_width = self._title_width(table)
        table.add_column("Session", width=title_width, key="title")
        table.add_column("Updated", width=self.UPDATED_WIDTH, key="updated")
        table.add_column("Msgs", width=self.MSGS_WIDTH, key="msgs")
        if self.scope == SCOPE_ALL:
            table.add_column("Directory", width=self.CWD_WIDTH, key="cwd")

        # The one clock read in this module, once per frame, so every row in a
        # given table is an age measured from the same instant.
        now = datetime.now(timezone.utc)
        for info in self.visible_sessions():
            cells = [
                self._cell(info.display_title(), title_width),
                self._cell(format_age(info.modified, now), self.UPDATED_WIDTH),
                self._cell(str(info.message_count), self.MSGS_WIDTH),
            ]
            if self.scope == SCOPE_ALL:
                cells.append(self._path_cell(info.cwd, self.CWD_WIDTH))
            table.add_row(*cells, key=info.ref)
        self._refresh_status()

    def _refresh_status(self) -> None:
        """The one line that says what is being listed.

        It names the scope because a picker showing four rows is ambiguous
        otherwise — four sessions in this directory, or four on the whole machine?
        It does NOT repeat the Tab hint: the Footer below it already renders
        ``tab  Current ↔ all`` off this screen's ``BINDINGS``, and a caption that
        restates the footer is a caption competing with the path for one row.
        """
        status = self.query_one("#session-picker-status", Static)
        if self._loading:
            status.update("Loading…")
            return
        shown = len(self.visible_sessions())
        total = len(self.sessions)
        counted = f"{shown} of {total}" if shown != total else str(total)
        plural = "" if total == 1 else "s"
        prefix = f"{counted} session{plural} in "
        if self.scope == SCOPE_ALL:
            status.update(prefix + "every directory")
            return
        # Cut the PATH rather than let the CSS clip the line: `text-overflow`
        # takes the tail, which on a directory is the only part that identifies
        # it. Width 0 before the first layout — the full string is written and
        # `on_resize` rewrites it once the row has a width.
        room = status.content_size.width - len(prefix)
        where = home_relative(self._cwd)
        status.update(prefix + (elide_start(where, room) if room > 0 else where))

    def on_resize(self) -> None:
        # The title column is a function of the table's width, so it has to be
        # recomputed when that changes — the same reason SessionTreeModal relabels
        # its rows on resize.
        self._populate()

    # -- actions -------------------------------------------------------------

    def action_toggle_scope(self) -> None:
        self.scope = SCOPE_ALL if self.scope == SCOPE_CWD else SCOPE_CWD

    def action_focus_filter(self) -> None:
        self.query_one("#session-picker-filter", Input).focus()

    def action_cancel(self) -> None:
        """Esc: out of the filter first, out of the picker second.

        Two meanings for one key, resolved by where focus is rather than by a
        mode flag: while typing a filter, Esc is "stop typing"; on the list, it is
        "never mind".
        """
        if self.query_one("#session-picker-filter", Input).has_focus:
            self.query_one(DataTable).focus()
            return
        self.dismiss(None)

    # -- events --------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "session-picker-filter":
            self.filter_query = event.value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter box returns to the list; it does not pick a row.

        Picking is a second, deliberate Enter on a highlighted row. Submitting the
        filter straight into a load would resume whatever happened to sort first
        under a half-typed query.
        """
        if event.input.id == "session-picker-filter":
            event.stop()
            self.query_one(DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        ref = event.row_key.value
        if ref is None:
            # Every row is added with the SessionInfo's ref as its key, so a
            # keyless row means this table was populated by something other than
            # _populate. Dismissing with None would look like a cancel.
            raise ValueError("session picker row has no ref key")
        self.dismiss(str(ref))
