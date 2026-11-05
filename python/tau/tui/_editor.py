"""The editor and what sits around it: `ChatInput`, `CommandPopup` and `PendingInput`.

Written in the manner of τ 0.15's tau-coding-agent/src/tau_coding_agent/chat_widgets.py
(`ChatInput`) and editor_widgets.py (`CommandPopup`, `PendingInput`; tag v0.15.0), whose look and
behavior they keep, but not copied from them. `StreamStrip` is not carried: the cursor bar
replaces it (8C-2). What changed from 0.15: completion is one call to the
session (`Client.complete_line`, `Client.complete`), so it is asynchronous and the editor is given
the function that asks; the popup shows rows it is handed (the commands, or the candidates of a
Tab); the pending lines come from `CursorView.items`; Enter sends and Ctrl+J starts a new line
(D23: no `enter` setting).
"""

from collections.abc import Awaitable, Callable
from typing import ClassVar

from rich.markup import escape
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static, TextArea

from tau.tui._fields import Refused

Completion = tuple[int, list[str]]
"""Where in the text the word to replace starts, and the words that could replace it."""

Completer = Callable[[str], Awaitable[Completion]]
"""What the editor asks on Tab: the text before the cursor, to what could finish its last word."""


class ChatInput(TextArea):
    """The message editor. Enter sends, Ctrl+J (or Shift+Enter where the terminal says so) starts a
    new line, Up and Down at the first and last line walk the lines sent before, and Tab finishes
    the last word through `completer`; further Tabs cycle through the candidates."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+j", "newline", "New line", show=False),
        Binding("ctrl+z", "undo_or_edit", "Undo / edit the last message", show=False),
    ]

    class Submitted(Message):
        """Enter: `text` is the draft. The app decides what it is (a message, a command)."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class Reclaim(Message):
        """Up on an empty editor, with a line of this client's waiting in the cursor's
        scratchpad: take it back."""

    class EditLast(Message):
        """Ctrl+Z on an empty editor: edit the last message."""

    class Candidates(Message):
        """A Tab found `items`; the one the text now holds is `selected`."""

        def __init__(self, items: list[str], selected: int) -> None:
            super().__init__()
            self.items = items
            self.selected = selected

    def __init__(self, completer: Completer) -> None:
        super().__init__(id="chat-input", soft_wrap=True, show_line_numbers=False)
        self.completer = completer
        self.sent: list[str] = []
        self._at = -1  # which history line the draft shows; -1: the draft itself
        self._draft = ""
        self._cycle: list[str] = []
        self._start = 0
        self._chosen = 0
        self._cycle_text = ""
        self.reclaimable = False
        """Whether this client has a pending line `Up` on an empty editor takes back."""

    @property
    def cycling(self) -> bool:
        """Whether the text is what a Tab put there (typing or sending ends the cycle)."""
        return bool(self._cycle) and self.text == self._cycle_text

    def action_newline(self) -> None:
        """Start a new line."""
        self.insert("\n")

    def action_undo_or_edit(self) -> None:
        """Ctrl+Z: undo what was typed; with nothing typed, ask to edit the last message."""
        if self.text:
            self.action_undo()
        else:
            self.post_message(self.EditLast())

    def send(self) -> None:
        """Take the draft out of the editor and hand it to the app as `Submitted`, remembering it
        in the history. The app gives it back (`restore`) if it could not use it."""
        text = self.text
        if text.strip() and (not self.sent or self.sent[-1] != text):
            self.sent.append(text)
        self.clear_draft()
        if text.strip():
            self.post_message(self.Submitted(text))

    def restore(self, text: str) -> None:
        """Put `text` back at the start of the draft: a line the app could not use."""
        self.load_text(f"{text}\n{self.text}" if self.text else text)
        self.move_cursor(self.document.end)

    def clear_draft(self) -> None:
        """Empty the editor."""
        self.clear()
        self._at, self._cycle = -1, []

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.send()
        elif event.key == "shift+enter":
            event.prevent_default()
            event.stop()
            self.action_newline()
        elif event.key == "tab":
            event.prevent_default()
            event.stop()
            self.run_worker(self._complete(), exclusive=True)
        elif event.key == "up" and self.reclaimable and not self.text:
            event.prevent_default()
            event.stop()
            self.post_message(self.Reclaim())
        elif event.key in ("up", "down") and self._walk(event.key == "up"):
            event.prevent_default()
            event.stop()

    def _walk(self, back: bool) -> bool:
        """Show an earlier (`back`) or later line of the history, if the cursor is on the first
        (or last) line. Returns whether the key was used."""
        row = self.cursor_location[0]
        edge = row == 0 if back else row == self.document.line_count - 1
        if not self.sent or not edge:
            return False
        if back:
            if self._at == 0:
                return True
            if self._at == -1:
                self._draft = self.text
                self._at = len(self.sent)
            self._at -= 1
        else:
            if self._at == -1:
                return False
            self._at += 1
            if self._at == len(self.sent):
                self._at = -1
        self.load_text(self._draft if self._at == -1 else self.sent[self._at])
        self.move_cursor(self.document.end)
        return True

    async def _complete(self) -> None:
        if self.cycling:
            self._chosen = (self._chosen + 1) % len(self._cycle)
        else:
            try:
                self._start, self._cycle = await self.completer(self.text_before_cursor)
            except ConnectionError as e:
                self.post_message(Refused(str(e)))
                return
            self._chosen = 0
        if not self._cycle:
            return
        self._replace(self._cycle[self._chosen])
        self.post_message(self.Candidates(self._cycle, self._chosen))

    @property
    def text_before_cursor(self) -> str:
        """The text up to the cursor."""
        return self.get_text_range((0, 0), self.cursor_location)

    def _replace(self, word: str) -> None:
        """Put `word` where the word being completed is. A command name that was the only
        candidate gets a space after it, so the arguments can follow."""
        before = self.text_before_cursor
        shown = before[: self._start] + word
        if len(self._cycle) == 1 and word.startswith("/") and " " not in shown.strip():
            shown += " "
        self.load_text(shown)
        self.move_cursor(self.document.end)
        self._cycle_text = shown


class Composer(Vertical):
    """The popup and the editor, and what ties them: typing a `/` line shows the commands it
    could be (`rows`) or warns that it is no command, a Tab shows its candidates, Enter hands the
    draft to `submit`, a task of its own so the person can answer a question the line asks. Up
    and Ctrl+Z on an empty editor are moves (`move`): take a line back, edit the last message."""

    def __init__(
        self,
        completer: Completer,
        rows: Callable[[str], list[tuple[str, str]]],
        submit: Callable[[str], Awaitable[None]],
        move: Callable[[str], None],
        renew: Callable[[], Awaitable[bool]],
    ) -> None:
        super().__init__(id="composer")
        self._renew = renew
        self._move = move
        self._completer = completer
        self._rows = rows
        self._submit = submit

    def compose(self) -> ComposeResult:
        yield CommandPopup()
        yield ChatInput(self._completer)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        event.stop()
        editor, popup = self.query_one(ChatInput), self.query_one(CommandPopup)
        text = editor.text
        if editor.cycling:
            return
        if not text.startswith("/") or "\n" in text:
            popup.hide()
        elif " " in text:  # the word and one space: what to type next, if the command says
            popup.show(self._rows(text) if text.count(" ") == 1 and text[-1] == " " else [])
        elif rows := self._rows(text):
            popup.show(rows)
            if text == "/":
                self._renewed()
        else:
            sent = "" if ":" in text else ": this line goes to the model as text"
            popup.warn(f"{text} is not a command{sent}")

    def on_chat_input_candidates(self, event: ChatInput.Candidates) -> None:
        event.stop()
        self.query_one(CommandPopup).show([(item, "") for item in event.items], event.selected)

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        event.stop()
        if event.text.strip():
            self._send(event.text)

    def on_chat_input_reclaim(self, event: ChatInput.Reclaim) -> None:
        event.stop()
        self._move("reclaim")

    def on_chat_input_edit_last(self, event: ChatInput.EditLast) -> None:
        event.stop()
        self._move("edit")

    @work(group="commands", exclusive=True)
    async def _renewed(self) -> None:
        """A `/` was typed: the session may have commands the list lacks (a new file). Show the
        list again if it did."""
        try:
            renewed = await self._renew()
        except ConnectionError:  # the lost session is the app's to say; the list stays as it was
            return
        if renewed and self.query_one(ChatInput).text == "/":
            self.query_one(CommandPopup).show(self._rows("/"))

    @work(group="submit")
    async def _send(self, text: str) -> None:
        await self._submit(text)


class CommandPopup(Static):
    """The rows a half-typed line could become: the commands while the first word is typed, the
    candidates while Tab cycles. One row is `(label, detail)`. A line that starts with `/` and
    names no command says so before Enter: it goes to the model as text. Hidden with nothing to
    say."""

    MAX_ROWS = 8

    def __init__(self) -> None:
        super().__init__("", id="command-popup")
        self._text = ""
        self.display = False

    @property
    def text(self) -> str:
        """What the popup shows (`""` when hidden)."""
        return self._text

    def hide(self) -> None:
        """Show nothing."""
        self.display = False
        self._text = ""
        self.update("")

    def warn(self, text: str) -> None:
        """Show one line of warning in place of rows."""
        self.display = True
        self.add_class("command-popup-unknown")
        self._text = text
        self.update(escape(text))

    def show(self, rows: list[tuple[str, str]], selected: int | None = None) -> None:
        """Show `rows`, windowed around the `selected` one; none hides the popup."""
        if not rows:
            self.hide()
            return
        self.display = True
        self.remove_class("command-popup-unknown")
        start = 0 if selected is None or selected < self.MAX_ROWS else selected - self.MAX_ROWS + 1
        window = rows[start : start + self.MAX_ROWS]
        lines: list[str] = []
        for offset, (label, detail) in enumerate(window):
            marker = "▸" if selected == start + offset else " "
            lines.append(f"{marker} {label}" + (f"  {detail}" if detail else ""))
        hidden = len(rows) - start - len(window)
        if hidden:
            lines.append(f"  … {hidden} more")
        self._text = "\n".join(lines)
        self.update("\n".join(escape(line) for line in lines))


class PendingInput(Static):
    """The lines waiting for the cursor to take them: its scratchpad's items, with their authors
    when someone else wrote them. Hidden with nothing pending."""

    def __init__(self) -> None:
        super().__init__("", id="pending-input")
        self._text = ""
        self.display = False

    @property
    def text(self) -> str:
        """The lines shown (`""` when hidden)."""
        return self._text

    def show(self, lines: list[str]) -> None:
        """Show `lines`, oldest first, or hide the widget when there are none."""
        self.display = bool(lines)
        self._text = "pending  ·  next step takes them\n" + "\n".join(f"> {x}" for x in lines)
        if not lines:
            self._text = ""
        self.update(escape(self._text))
