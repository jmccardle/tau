"""editor_widgets — split out of app.py."""

from rich.markup import escape
from textual.widgets import Static
from tau_agent_core.attachments import AttachmentCompletions, SENDABLE_KINDS, Attachment, human_size
from tau_agent_core.commands import ArgumentCompletions, CommandCompletions
from typing import Sequence
from textual.containers import Vertical
from textual.message import Message
from textual import events


class LaneStrip(Static):
    """One-line footer strip naming every FOREIGN lane currently streaming (B3-b).

    Reference: docs/SUBMISSION-LIFECYCLE.md phase 3. The transcript shows a
    foreign lane's *content* — badged bubbles, a labelled exchange — but content
    scrolls, and a forked sub-agent that runs for two minutes inside a collapsed
    exchange three screens up is running invisibly. This is the ambient half: while
    anything the user did not type is in flight, one line says so, and it says
    which.

    Deliberately a separate widget from :class:`ExtensionStatusBar` rather than a
    slot in it. That bar's slots are an EXTENSION's to name (``ctx.ui.set_status``
    keys come from extension code), so lane activity living there would be one
    ``set_status("lanes", …)`` away from being silently overwritten by the very
    extension whose fork it is reporting.

    Same idiom as that bar, though — an insertion-ordered dict of live entries,
    joined by a thin separator, hidden (``display = False``) at zero entries so it
    costs no rows on an ordinary session. Only foreign lanes are listed: the
    frontend's own typed turn already has its exchange on screen and the header
    subtitle to say it is working, and a strip that lit up for every prompt would
    be the noise the badge rules exist to avoid.
    """

    _SEPARATOR = "  │  "

    def __init__(self) -> None:
        super().__init__("", id="lane-strip")
        # lane id -> origin badge, in the order the lanes opened.
        self._lanes: dict[str, str] = {}
        self.display = False

    def open_lane(self, lane: str, label: str | None) -> None:
        """Track ``lane`` as live under its origin badge.

        ``label is None`` is this frontend's own typed turn, which the strip does
        not report — not a filtered-out source, a lane the reader is already
        looking at.
        """
        if label is None:
            return
        self._lanes[lane] = label
        self._render_strip()

    def close_lane(self, lane: str) -> None:
        """Drop ``lane`` from the strip. A lane it never tracked is a no-op —
        that is the ordinary interactive lane ending."""
        if self._lanes.pop(lane, None) is not None:
            self._render_strip()

    def clear_lanes(self) -> None:
        """Forget every tracked lane (a backend/session swap abandons them)."""
        if self._lanes:
            self._lanes = {}
            self._render_strip()

    @property
    def lanes(self) -> dict[str, str]:
        """The live lanes, ``{lane: badge}``, in open order."""
        return dict(self._lanes)

    @property
    def summary(self) -> str:
        """The line this strip currently shows — ``""`` when it is hidden.

        Derived from :attr:`lanes` rather than cached, so what the strip says and
        what it is tracking cannot drift; the widget's own text is set from here.
        """
        if not self._lanes:
            return ""
        count = len(self._lanes)
        noun = "lane" if count == 1 else "lanes"
        return f"⑂ {count} other {noun}: " + self._SEPARATOR.join(self._lanes.values())

    def _render_strip(self) -> None:
        summary = self.summary
        self.display = bool(summary)
        self.update(summary)


class PendingInput(Static):
    """The lines typed during a turn that have not reached the model yet.

    Reference: docs/TUI-STEERING.md §3.

    It sits between the transcript and the input box, and it is the ONLY place a
    steering message is visible between the Enter that wrote it and the boundary
    that delivers it. Without it the input box would accept text during a turn
    and appear to swallow it: the transcript cannot show the line yet, because
    the model has not been given it yet.

    Hidden (``display = False``) with nothing pending, like :class:`LaneStrip`,
    so an ordinary turn costs no rows. It holds no state of its own — the app
    owns the buffer and calls :meth:`show` — because the buffer has to survive
    the reclaim gesture, which empties the widget and refills the editor.
    """

    def __init__(self) -> None:
        super().__init__("", id="pending-input")
        self._text = ""
        self.display = False

    @property
    def text(self) -> str:
        """The line this widget currently shows — ``""`` when it is hidden.

        Same idiom as :attr:`LaneStrip.summary`: the widget's own text is set
        from here, so what it says and what it was told cannot drift, and a
        caller asking what is on screen does not have to reach into Textual's
        rendering internals to find out.
        """
        return self._text

    def show(self, lines: list[str], note: str) -> None:
        """Display ``lines`` under ``note``, or hide the widget when there are none.

        Args:
            lines: The pending messages, oldest first — the app's buffer verbatim.
            note: When these will be delivered, in words. The app writes it from
                the steering strategy in force, because "after the tool it is
                running" and "when this turn ends" are the two different promises
                the two strategies make, and a widget that named neither would
                leave the reader unable to tell which one they are waiting for.
        """
        self.display = bool(lines)
        if not lines:
            self._text = ""
            self.update("")
            return
        body = "\n".join(f"› {line}" for line in lines)
        self._text = f"{note}  ·  ↑ to edit\n{body}"
        self.update(self._text)


def attachment_row_text(attachment: Attachment) -> str:
    """The one line :class:`AttachmentRow` shows for one attached file.

    Three shapes, because there are three things that can happen to a ``@file``
    and a bar that showed only the name would hide the two that matter: the image
    that will cost a vision call, and the file whose CONTENT is not being sent.

    Args:
        attachment: A sendable attachment (:data:`~tau_agent_core.attachments.SENDABLE_KINDS`).

    Returns:
        Rich markup for the row, starting with the ``✕`` that removes it.
    """
    name = escape(attachment.token)
    if attachment.kind == "image":
        detail = f"image · {human_size(attachment.size)}"
    elif attachment.kind == "reference":
        detail = f"path only · {escape(attachment.note)}"
    else:
        detail = human_size(attachment.size)
    return f"[b]✕[/b]  {name}  [dim]{detail}[/dim]"


class AttachmentRow(Static):
    """One attached file in the bar above the editor.

    Reference: docs/FILE-ATTACHMENTS.md §4.

    Clicking it removes the attachment, which means deleting the ``@…`` word from
    the editor — the word IS the attachment, so there is no second place for the
    two to disagree. The whole row is the click target rather than the ``✕``
    glyph alone: a one-line row inside a bordered bar leaves a two-cell target
    that is easy to miss, and the cost of a mis-click is retyping one path.
    """

    def __init__(self, attachment: Attachment) -> None:
        self._text = attachment_row_text(attachment)
        super().__init__(self._text, classes="attachment-row")
        self.attachment = attachment

    @property
    def text(self) -> str:
        """The markup this row shows.

        Same idiom as :attr:`PendingInput.text`: the widget's own content is set
        from here, so a caller asking what is on screen does not have to reach
        into Textual's rendering internals.
        """
        return self._text

    def on_click(self, event: events.Click) -> None:
        """Ask the app to remove this attachment from the editor's text."""
        event.stop()
        self.post_message(AttachmentBar.Remove(self.attachment))


class AttachmentBar(Vertical):
    """The files the draft in the editor will attach, and how to drop one.

    Reference: docs/FILE-ATTACHMENTS.md §4.

    It sits between :class:`PendingInput` and the editor, and it holds no state
    the editor does not: :meth:`show` is called from the editor's ``Changed``
    handler with whatever :func:`~tau_agent_core.attachments.scan_attachments`
    found, so the bar is a VIEW of the draft rather than a second buffer that
    could drift from it. That is what makes removal simple — it edits the text,
    the text change redraws the bar.

    Hidden (``display = False``) with nothing attached, like :class:`LaneStrip`
    and :class:`PendingInput`, so an ordinary line costs no rows.

    Unresolved references are deliberately NOT shown. A ``@word`` that names no
    file is ordinary prose on its way to the model — the same answer an
    unrecognised ``/…`` gets — and the popup under the editor is where that is
    said, while the cursor is still in the word.
    """

    class Remove(Message):
        """A row was clicked. The app deletes the reference from the editor."""

        def __init__(self, attachment: Attachment) -> None:
            super().__init__()
            self.attachment = attachment

    def __init__(self) -> None:
        super().__init__(id="attachment-bar")
        self._attachments: tuple[Attachment, ...] = ()
        self.display = False

    @property
    def attachments(self) -> tuple[Attachment, ...]:
        """What the bar is currently showing, in the order it shows them."""
        return self._attachments

    def show(self, attachments: Sequence[Attachment]) -> None:
        """Redraw the bar from a scan of the editor's text.

        Args:
            attachments: Every reference the scan found, unresolved ones
                included. This method does the filtering, so a caller cannot
                accidentally show a row for a word that attaches nothing.
        """
        sendable = tuple(a for a in attachments if a.kind in SENDABLE_KINDS)
        if sendable == self._attachments:
            return
        self._attachments = sendable
        self.remove_children()
        self.display = bool(sendable)
        if sendable:
            self.mount_all([AttachmentRow(a) for a in sendable])


class CommandPopup(Static):
    """The slash commands — or the file paths — a half-typed line could become.

    Reference: docs/SLASH-COMMANDS.md, docs/FILE-ATTACHMENTS.md §3.

    It serves two vocabularies through two methods, :meth:`show` and
    :meth:`show_files`, and the name is historical: it was the command popup
    before ``@file`` existed. Only one vocabulary can apply at a time, so one
    widget is one row of chrome rather than two.

    It sits under the editor and answers one question the editor could not: is
    this ``/…`` a command τ knows, or ordinary text on its way to the model? An
    unknown slash resolves to ``None`` and is sent as a prompt
    (:func:`~tau_agent_core.commands.resolve_command`), which is the right
    behaviour — refusing every unrecognised slash would break pasting a file path
    — but it is silent, and a user who mistypes ``/exntesions`` finds out by
    reading the model's guess at what they meant. This widget says so before the
    Enter key.

    Hidden (``display = False``) with nothing to say, like :class:`LaneStrip` and
    :class:`PendingInput`, so an ordinary line costs no rows. It holds no state:
    :class:`ChatInput` owns the Tab cycle, because the cycle has to survive the
    text edits that redraw this.
    """

    MAX_ROWS = 8

    def __init__(self) -> None:
        super().__init__("", id="command-popup")
        self._text = ""
        self.display = False

    @property
    def text(self) -> str:
        """What this widget currently shows — ``""`` when it is hidden.

        Same idiom as :attr:`LaneStrip.summary` and :attr:`PendingInput.text`: the
        widget's own content is set from here, so what it says and what it was
        told cannot drift, and a test does not have to reach into Textual's
        rendering internals to read it.
        """
        return self._text

    def show(self, completions: CommandCompletions | None, selected: int | None = None) -> None:
        """Display ``completions``, or hide the widget when there are none.

        Args:
            completions: What :func:`~tau_agent_core.commands.complete_command`
                said about the editor's current text. ``None`` hides the widget.
                A value with an EMPTY ``matches`` does not hide it — that is the
                unknown-command warning, and it is the case this widget exists
                for.
            selected: Index into ``completions.matches`` of the candidate a Tab
                press has inserted, or ``None`` when no cycle is running. Marks
                the row and decides which slice of a long list is visible.
        """
        if completions is None:
            self.display = False
            self._text = ""
            self.update("")
            return

        self.display = True
        self.set_class(not completions.matches, "command-popup-unknown")
        if not completions.matches:
            shown = escape(f"/{completions.token}".replace("\n", "↵"))
            self._text = f"{shown} is not a command — this line goes to the model as text"
            self.update(self._text)
            return

        self._show_rows(
            [
                (f"/{escape(match.name)}", escape(match.description))
                for match in completions.matches
            ],
            selected,
        )

    def show_files(
        self, completions: AttachmentCompletions | None, selected: int | None = None
    ) -> None:
        """Display path candidates for a half-typed ``@…`` (docs/FILE-ATTACHMENTS.md §3).

        The same widget as the command list, because only one of the two can be
        relevant at a time: the cursor is either inside a ``@…`` or it is not, and
        :meth:`TauApp._refresh_command_popup` asks in that order. Two popups would
        be two rows of chrome to answer one question.

        Args:
            completions: What :func:`~tau_agent_core.attachments.complete_attachment`
                said. ``None`` hides the widget. An EMPTY ``matches`` does not —
                that is the warning that this ``@…`` names no file, which is the
                same service this widget performs for an unknown ``/…``.
            selected: Index into ``completions.matches`` of the candidate a Tab
                press has inserted, or ``None`` when no cycle is running.
        """
        if completions is None:
            self.display = False
            self._text = ""
            self.update("")
            return

        self.display = True
        self.set_class(not completions.matches, "command-popup-unknown")
        if not completions.matches:
            shown = escape(f"@{completions.token}".replace("\n", "↵"))
            self._text = f"{shown} matches no file — this word goes to the model as text"
            self.update(self._text)
            return

        rows = [(f"@{escape(match.name)}", escape(match.detail)) for match in completions.matches]
        hidden = completions.total - len(completions.matches)
        self._show_rows(rows, selected, extra_hidden=hidden)

    def show_values(
        self, completions: ArgumentCompletions | None, selected: int | None = None
    ) -> None:
        """Display the values legal for the argument being typed.

        The third vocabulary, and the one that answers a question the other two
        cannot: ``/model `` is a complete, correct command word, so the command
        list has nothing left to say about it while the reader is still deciding
        what to type next.

        Args:
            completions: What the app got from
                :func:`~tau_agent_core.commands.complete_command_argument` joined to
                :func:`~tau_agent_core.flows.enumerate_domain`. ``None`` hides the
                widget. An empty ``matches`` does not: with an ``error`` it says why
                the values could not be listed, and without one it says the query
                matches nothing — two states that must not look alike.
            selected: Index into ``completions.matches`` of the value a Tab press
                has inserted, or ``None`` when no cycle is running.
        """
        if completions is None:
            self.display = False
            self._text = ""
            self.update("")
            return

        self.display = True
        self.set_class(not completions.matches, "command-popup-unknown")
        if completions.error is not None:
            self._text = escape(completions.error)
            self.update(self._text)
            return
        if not completions.matches:
            slot = completions.slot
            typed = escape(slot.query.replace("\n", "↵"))
            self._text = f"no {slot.domain.name} matches {typed!r}"
            self.update(self._text)
            return

        rows = [
            (escape(value.value), escape(value.label) if value.label != value.value else "")
            for value in completions.matches
        ]
        self._show_rows(rows, selected, extra_hidden=completions.total - len(completions.matches))

    def _show_rows(
        self,
        rows: list[tuple[str, str]],
        selected: int | None,
        extra_hidden: int = 0,
    ) -> None:
        """Render ``(label, detail)`` rows, windowed around ``selected``.

        Shared by both vocabularies so the marker, the window and the "… N more"
        line cannot drift between them.

        Args:
            rows: The candidates, already escaped and already prefixed with the
                sigil their vocabulary uses.
            selected: Which row a running Tab cycle has inserted, or ``None``.
            extra_hidden: Candidates the caller did not pass at all (a listing cap
                upstream), added to the count of those scrolled out of the window.
        """
        start = 0
        if selected is not None and selected >= self.MAX_ROWS:
            start = selected - self.MAX_ROWS + 1
        window = rows[start : start + self.MAX_ROWS]

        lines = []
        for offset, (label, detail) in enumerate(window):
            marker = "▸" if selected == start + offset else " "
            row = f"{marker} [b]{label}[/b]"
            if detail:
                row += f"  {detail}"
            lines.append(row)
        hidden = len(rows) - start - len(window) + extra_hidden
        if hidden:
            lines.append(f"  … {hidden} more")

        self._text = "\n".join(lines)
        self.update(self._text)
