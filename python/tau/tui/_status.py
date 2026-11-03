"""The line above the transcript and the lines below the editor (D23 section 2, "Status").

The header shows the session's title (its first message), id and working directory. The status bar
shows the viewed cursor: its model, whether a step is running, auto-step, how the last step ended,
the cursor hook that failed in it, and one line per extension that shows something (`Cursor.show`).
Both hold the text they show, as `text`, and are set whole whenever it changes.

In the manner of 0.15's `ExtensionStatusBar` and `WarningsMarker`
(tau-coding-agent/src/tau_coding_agent/extension_ui.py, tag v0.15.0); written new, fed from
`CursorView`.
"""

from textual.widgets import Static

from tau._core import Author, Ending, ItemData
from tau._events import CursorView
from tau.tui._transcript import author_name


def ending_text(ending: Ending | None) -> str:
    """How the last step ended, in a few words; `""` for one that ran to its end."""
    match ending:
        case Ending.Halted(reason=reason):
            return f"halted: {reason}"
        case Ending.Failed(error=error):
            return f"failed: {error}"
        case Ending.Aborted():
            return "aborted"
        case _:
            return ""


def status_lines(view: CursorView) -> list[str]:
    """The status bar's lines for a cursor's view."""
    state = ["working…" if view.busy else "idle"]
    if view.auto:
        state.append("auto")
    head = [f"model: {view.model or '(none)'}", *state]
    ended = ending_text(view.ending)
    if ended:
        head.append(ended)
    lines = ["  ·  ".join(head)]
    if view.hook_error is not None:
        failure = view.hook_error
        lines.append(f"{failure.point} of {failure.extension} failed: {failure.error}")
    lines.extend(f"{who}: {text}" for who, text in view.shown)
    return lines


def pending_line(data: ItemData, me: str) -> str:
    """One pending item as a line: an input's text (under its author's name when someone else
    wrote it, not the client `me`), an extension's item by its name."""
    if isinstance(data, ItemData.Input):
        return (
            data.text if data.by == Author.Frontend(me) else f"[{author_name(data.by)}] {data.text}"
        )
    assert isinstance(data, ItemData.Extension)
    return f"[{data.name}]"


class Header(Static):
    """Title, session id and working directory."""

    def __init__(self) -> None:
        super().__init__("", id="header", markup=False)
        self.text = ""

    def show(self, title: str, session: str, cwd: str) -> None:
        """Set the header's three parts."""
        self.text = f"{title}  ·  {session}  ·  {cwd}"
        self.update(self.text)


class StatusBar(Static):
    """The viewed cursor's state, one line, then a line per failure or shown line."""

    def __init__(self) -> None:
        super().__init__("", id="status-bar", markup=False)
        self.text = ""

    def show(self, lines: list[str]) -> None:
        """Set the bar's lines."""
        self.text = "\n".join(lines)
        self.update(self.text)
