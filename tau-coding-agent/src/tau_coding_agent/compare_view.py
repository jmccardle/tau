"""The TUI's view of a comparison: one column per model, streaming side by side (docs/TAU-SERVE.md §8).

The app routes every render event of a comparison's streams here, by the
``compare`` correlation its turns carry, so the columns fill the same way
in-process and under ``--connect``. Keeping one is the core's
(``Comparison.end``); this screen asks for it and shows what streamed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from rich.markdown import Markdown
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static

from tau_coding_agent.dialogs import TauDialog

EndHandler = Callable[[str, str | None], Awaitable[bool]]
"""``(comparison_id, keep)`` → whether the comparison ended; the screen closes on ``True``."""


@dataclass
class ColumnState:
    """What one column has received, kept whether or not the screen is mounted.

    Attributes:
        model: The model name the column runs.
        cursor_id: The compare cursor, or ``""`` until its stream opens.
        text: The streamed answer, with tool calls inlined as one line each.
        status: ``waiting``, ``thinking``, ``running``, ``done`` or a cost line.
        finished: Whether the stream has closed.
        tools: Tool names by call id, so a result line names its call.
    """

    model: str
    cursor_id: str = ""
    text: str = ""
    status: str = "waiting"
    finished: bool = False
    tools: dict[str, str] = field(default_factory=dict)


class CompareColumn(VerticalScroll):
    """One model's stream in a bordered, scrolling column."""

    def __init__(self, index: int, state: ColumnState) -> None:
        super().__init__(id=f"compare-column-{index}", classes="compare-column")
        self._index = index
        self.state = state

    def compose(self) -> ComposeResult:
        yield Static(classes="compare-body")

    def on_mount(self) -> None:
        self.paint()

    def paint(self) -> None:
        """Redraw the title, the status and the text from :attr:`state`."""
        self.border_title = f"{self._index + 1} · {self.state.model}"
        self.border_subtitle = self.state.status
        self.set_class(self.state.finished, "compare-finished")
        self.query_one(".compare-body", Static).update(Markdown(self.state.text or " "))
        self.scroll_end(animate=False)


class CompareScreen(TauDialog[str | None]):
    """N columns, one per compare cursor; a digit keeps that column, ``escape`` keeps none.

    Built from the comparison's correlation (``{id, models, index, cursor_id}``) the
    first time one of its streams opens, so a client that did not start the
    comparison draws it too. Dismisses with the kept cursor id, or ``None``.
    """

    DIALOG_ID: ClassVar[str] = "compare-dialog"
    BINDINGS = [
        Binding("escape", "discard", "keep none", show=False),
        *[Binding(str(n), f"keep({n - 1})", f"keep {n}", show=False) for n in range(1, 10)],
    ]

    def __init__(self, comparison_id: str, models: list[str], on_end: EndHandler) -> None:
        super().__init__(f"Compare · {len(models)} models")
        self.comparison_id = comparison_id
        self.columns = [ColumnState(model=name) for name in models]
        self._on_end = on_end
        self._ending = False

    def compose_body(self) -> ComposeResult:
        with Horizontal(classes="compare-columns"):
            for index, state in enumerate(self.columns):
                yield CompareColumn(index, state)
        yield Static(
            f"1–{len(self.columns)} keep that answer and continue from it · "
            "esc keep none (branches stay in /tree)",
            classes="tau-dialog-hint",
        )

    def feed(self, index: int, event: dict[str, Any]) -> None:
        """Apply one render event to column ``index``, and redraw it if it is on screen."""
        state = self.columns[index]
        kind = event.get("kind")
        if kind == "stream_start":
            state.status = "running"
        elif kind == "text_delta":
            state.text += str(event.get("delta") or "")
            state.status = "running"
        elif kind == "reasoning_delta":
            state.status = "thinking"
        elif kind == "tool_call":
            name = str(event.get("name") or "?")
            state.tools[str(event.get("id"))] = name
            state.text += f"\n\n`→ {name}`\n\n"
        elif kind == "tool_result":
            name = state.tools.get(str(event.get("id")), str(event.get("name") or "?"))
            outcome = "error" if event.get("is_error") else "ok"
            state.text += f"`← {name}: {outcome}`\n\n"
        elif kind == "stream_end":
            state.finished = True
            seconds = event.get("seconds")
            took = f" · {seconds:.1f}s" if isinstance(seconds, (int, float)) else ""
            state.status = f"done · ↓{int(event.get('output', 0) or 0)}{took}"
        else:
            return
        if self.is_mounted:
            self.query_one(f"#compare-column-{index}", CompareColumn).paint()

    def bind_cursor(self, index: int, cursor_id: str) -> None:
        """Record which compare cursor column ``index`` shows."""
        self.columns[index].cursor_id = cursor_id

    async def action_keep(self, index: int) -> None:
        """Keep column ``index``: the head moves onto its leaf and the others close."""
        if index >= len(self.columns):
            return
        cursor_id = self.columns[index].cursor_id
        if not cursor_id:
            self.notify(f"{self.columns[index].model} has not started yet", severity="warning")
            return
        await self._end(cursor_id)

    async def action_discard(self) -> None:
        """Keep none: running turns are aborted, every compare cursor closes."""
        await self._end(None)

    async def _end(self, keep: str | None) -> None:
        if self._ending:
            return
        self._ending = True
        try:
            ended = await self._on_end(self.comparison_id, keep)
        finally:
            self._ending = False
        if ended:
            self.dismiss(keep)
