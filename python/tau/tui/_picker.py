"""`SessionPicker` (D23 section 5): choose a saved session from the catalog.

Carried from τ 0.15: tau-coding-agent/src/tau_coding_agent/session_picker.py (tag v0.15.0): the
dialog, its `DataTable` with aligned columns, the scope toggle (Tab: this directory or all), the
`/` filter (`matches_query`: every term, any order, case-insensitive), `elide_start` and
`home_relative` for the directory column. What changed: the rows are `SessionInfo`s of
`tau.list_sessions`, given whole (a listing is a directory scan, so there is no worker and no
"Loading…"); the title is `SessionInfo.title`; the age is `age_of`; the message count column is
gone (D18 section 2); a row with an `error` is dimmed and cannot be picked; a row held by a live
`tau serve` says `served`, and one in use by another process or whose server does not answer
says so and cannot be picked.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Input, Static

from tau._sessions import SessionInfo, age_of
from tau.tui._dialogs import TauDialog


@dataclass(frozen=True)
class Holder:
    """Who has a session, as the picker shows it: a `state` (`served`: a live `tau serve`, which
    opening attaches to; `current`: this very session; `in use`: another process holds its lease;
    `no reply`: a serve record whose server did not answer `hello`) and the `detail` that says
    more."""

    state: str
    detail: str = ""

    @property
    def pickable(self) -> bool:
        """Whether opening it can work: not a session in use, nor one whose server is silent."""
        return self.state in ("served", "current")


def home_relative(path: str) -> str:
    """`/home/ada/Development/tau` as `~/Development/tau`: only an exact home-directory prefix is
    folded (`/home/adam` is not inside `/home/ada`)."""
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def elide_start(text: str, width: int) -> str:
    """`text` cut at the front, with a leading `…` where the cut is: a path's identifying part is
    its last component."""
    if width < 2 or len(text) <= width:  # noqa: PLR2004 - room for the mark and a character
        return text
    return "…" + text[-(width - 1) :]


def search_text(row: SessionInfo) -> str:
    """What the `/` filter matches against: the session's id, name and first message."""
    return " ".join(part for part in (row.id, row.name or "", row.first or "") if part)


def matches_query(query: str, haystack: str) -> bool:
    """Whether `haystack` holds every whitespace-separated term of `query`, in any order and any
    case. Not fuzzy, and no scoring, so the rows keep their order (newest first) as one types."""
    lowered = haystack.lower()
    return all(term in lowered for term in query.lower().split())


class SessionPicker(TauDialog[SessionInfo | None]):
    """Pick a saved session, newest first. `rows` are `tau.list_sessions` of the sessions
    directory (all of them); `here` is the working directory the "this directory" scope keeps
    (rows with an `error` are always listed); `held` says who holds some of the sessions, by id
    (`session_holders`): `served`, `current`, `in use`, `no reply`. Tab toggles this directory
    and all; `/` filters; Enter picks; Esc leaves the filter, then cancels. Dismissed with the
    row, or `None`. A row with an `error`, one in use or one whose server does not answer cannot
    be picked: the status line says why. The current session can be (it dismisses
    with its row; the caller does nothing)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("tab", "toggle_scope", "This directory / all"),
        Binding("slash", "focus_filter", "Filter"),
    ]

    DIALOG_ID = "session-picker-dialog"
    TITLE_TEXT = "Open a session"

    AGE_WIDTH = 5
    STATE_WIDTH = 6
    CWD_WIDTH = 26
    MIN_TITLE_WIDTH = 20

    scope: reactive[Literal["here", "all"]] = reactive("here", init=False)
    filter_text: reactive[str] = reactive("", init=False)

    def __init__(
        self,
        rows: Sequence[SessionInfo],
        here: Path,
        *,
        held: Mapping[str, Holder] | None = None,
        scope: Literal["here", "all"] = "here",
        title: str | None = None,
    ) -> None:
        super().__init__(title)
        self._rows = tuple(rows)
        self._here = str(here.resolve())
        self._held = held or {}
        self.set_reactive(SessionPicker.scope, scope)
        self.status_text = ""
        """The status line: what is listed, or why the highlighted row cannot be picked."""

    def compose_body(self) -> ComposeResult:
        yield Input(
            placeholder="filter sessions…", id="session-picker-filter", classes="tau-dialog-input"
        )
        table: DataTable[Text] = DataTable(id="session-picker-table")
        table.cursor_type = "row"
        yield table
        yield Static("", id="session-picker-status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        """List the rows and put the keyboard on the table."""
        self.query_one(DataTable).focus()
        self._populate()

    # The rows

    def shown(self) -> list[SessionInfo]:
        """The rows the scope and the filter admit, newest first."""
        rows = [r for r in self._rows if self.scope == "all" or r.error or r.cwd == self._here]
        return [r for r in rows if matches_query(self.filter_text, search_text(r))]

    def _title_width(self, table: DataTable[Text]) -> int:
        columns = 4 if self.scope == "all" else 3
        fixed = self.AGE_WIDTH + self.STATE_WIDTH + (self.CWD_WIDTH if self.scope == "all" else 0)
        return max(self.MIN_TITLE_WIDTH, table.content_size.width - fixed - 2 * columns)

    @staticmethod
    def _cell(text: str, *, dim: bool = False) -> Text:
        """A cell cut with a visible `…`, never wrapped: a wrapped cell makes one session three
        rows."""
        return Text(
            text.replace("\n", " "), no_wrap=True, overflow="ellipsis", style="dim" if dim else ""
        )

    def _table(self) -> DataTable[Text]:
        return cast("DataTable[Text]", self.query_one(DataTable))

    def _populate(self) -> None:
        table = self._table()
        table.clear(columns=True)
        width = self._title_width(table)
        table.add_column("Session", width=width, key="title")
        table.add_column("Age", width=self.AGE_WIDTH, key="age")
        table.add_column("State", width=self.STATE_WIDTH, key="state")
        if self.scope == "all":
            table.add_column("Directory", width=self.CWD_WIDTH, key="cwd")
        for row in self.shown():
            broken = row.error is not None
            cells = [
                self._cell(row.id if broken else row.title, dim=broken),
                self._cell(age_of(row), dim=broken),
                self._cell(
                    "error" if broken else self._state(row), dim=broken or not self._ok(row)
                ),
            ]
            if self.scope == "all":
                where = elide_start(home_relative(row.cwd or ""), self.CWD_WIDTH)
                cells.append(self._cell(where, dim=broken))
            table.add_row(*cells, key=row.id)
        self._status(
            f"{self._count()} in "
            + ("every directory" if self.scope == "all" else home_relative(self._here))
        )

    def _state(self, row: SessionInfo) -> str:
        holder = self._held.get(row.id)
        return "" if holder is None else holder.state

    def _ok(self, row: SessionInfo) -> bool:
        """Whether the row can be opened (or is this session, which asks for nothing)."""
        holder = self._held.get(row.id)
        return row.error is None and (holder is None or holder.pickable)

    def _why_not(self, row: SessionInfo) -> str:
        """Why `row` cannot be opened."""
        holder = self._held.get(row.id)
        why = (
            row.error
            if row.error is not None
            else f"{holder.state}: {holder.detail}"
            if holder
            else ""
        )
        return f"{row.id} cannot be opened: {why}"

    def _count(self) -> str:
        shown = len(self.shown())
        return f"{shown} session{'' if shown == 1 else 's'}"

    def _status(self, text: str) -> None:
        self.status_text = text
        self.query_one("#session-picker-status", Static).update(text)

    def _row(self, session: str) -> SessionInfo:
        return next(r for r in self._rows if r.id == session)

    # Reactives and events

    def watch_scope(self) -> None:
        """The scope changed: list again."""
        self._populate()

    def watch_filter_text(self) -> None:
        """The filter changed: list again."""
        self._populate()

    def on_resize(self) -> None:
        """The table's width changed: the title column takes what is left."""
        self._populate()

    def action_toggle_scope(self) -> None:
        """Tab: this directory, or every directory."""
        self.scope = "all" if self.scope == "here" else "here"

    def action_focus_filter(self) -> None:
        """`/`: type a filter."""
        self.query_one("#session-picker-filter", Input).focus()

    def action_cancel(self) -> None:
        """Esc: out of the filter first, out of the picker second."""
        if self.query_one("#session-picker-filter", Input).has_focus:
            self.query_one(DataTable).focus()
            return
        self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Typing in the filter narrows the rows."""
        self.filter_text = event.value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter returns to the list: picking is a second, deliberate Enter, so a
        half-typed filter never opens whatever sorts first."""
        event.stop()
        self.query_one(DataTable).focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Say which session the highlighted row is, or why it cannot be opened."""
        key = event.row_key.value
        row = None if key is None else self._row(key)
        if row is not None and not self._ok(row):
            self._status(self._why_not(row))
        elif row is not None:
            self._status(
                f"{self._count()}; {row.id}"
                + (f"  ({self._state(row)})" if self._state(row) else "")
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row: pick its session, unless it cannot be opened."""
        event.stop()
        key = event.row_key.value
        if key is None:
            raise ValueError("a session picker row has no session id")
        row = self._row(key)
        if not self._ok(row):
            self._status(self._why_not(row))
            return
        self.dismiss(row)
