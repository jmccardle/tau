"""The transcript's boxes: a message, its thinking region and its tool boxes.

Carried from τ 0.15: tau-coding-agent/src/tau_coding_agent/chat_widgets.py (tag v0.15.0):
`MessageBox`, `ReasoningRegion`, `ToolBox`, `QuietCollapsible`, `MarkdownLineFormatter` and the
formatters. `ExchangeBox` is not carried: nodes are the unit here (D23 section 2), and a fold per
exchange would hide the reply. They are fed from nodes (`tau.tui._transcript`) instead of 0.15's
message dicts.

Built on Textual's `Collapsible`: collapse and expand is the framework's reactive-to-CSS
mechanism, so widgets mount once as content streams in and folding never remounts. A box takes
text before it is mounted (a node is drawn complete, a live reply streams) and catches up when it
mounts.
"""

import json
from typing import Literal, cast

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Collapsible, Markdown, Static
from textual.widgets.markdown import MarkdownStream

ContentSource = Literal["markdown", "verbatim"]
"""What a body's text is: an assistant's markdown, or output to show line by line (a tool result,
a message the person typed)."""

_RESULT_LIMIT = 2000  # characters of a tool result drawn; the node holds all of it
_ARGUMENT_LIMIT = 40
_SUMMARY_LIMIT = 60


def _plain(text: str) -> str:
    """`text` as a title that is never read as markup: `echo [draft]` shows as typed. (A
    `Collapsible` reads a `str` title as markup and a `Content` as it is.)"""
    return cast("str", Content(text))


def format_tool_summary(name: str, arguments: object) -> str:
    """A one-line call signature, `name(key=val, ...)`, truncated for the collapsed title. The
    arguments are the decoded JSON object, or the raw text when the model's JSON did not parse."""
    inner = ""
    if isinstance(arguments, dict) and arguments:
        parts: list[str] = []
        for key, value in arguments.items():  # pyright: ignore[reportUnknownVariableType]
            text = value if isinstance(value, str) else json.dumps(value, default=str)
            text = text.replace("\n", " ")
            if len(text) > _ARGUMENT_LIMIT:
                text = text[: _ARGUMENT_LIMIT - 1] + "…"
            parts.append(f"{key}={text}")
        inner = ", ".join(parts)
    elif isinstance(arguments, str):
        inner = arguments.replace("\n", " ")
    if len(inner) > _SUMMARY_LIMIT:
        inner = inner[: _SUMMARY_LIMIT - 1] + "…"
    return f"{name}({inner})"


class MarkdownLineFormatter:
    """Prepare a message body for a Textual `Markdown` widget, according to what the body's
    SOURCE is.

    A `"verbatim"` body (a tool result, shell output) is not markdown, and markdown collapses each
    of its lone newlines into a space. Every newline is doubled so each line survives as its own
    paragraph, except inside a fenced code block, where a newline is already literal. A
    `"markdown"` body (an assistant's answer) is passed through untouched: doubling there puts a
    blank line between every wrapped line of a model that hard-wraps its prose. The split is by
    source and never by inspecting the text.

    Stateful: whether a newline is inside a fence depends on every line before it. The state is
    carried across `feed` calls, so a streaming caller hands over one delta at a time and gets the
    document a single whole-text call would produce, wherever the deltas split. A fence is a line
    whose first non-space characters are three backticks; an opener and its closer need not match.
    """

    __slots__ = ("_in_fence", "_line", "_source")

    def __init__(self, source: ContentSource) -> None:
        self._source: ContentSource = source
        self._in_fence = False
        self._line = ""

    @property
    def source(self) -> ContentSource:
        """What this formatter was told the body is."""
        return self._source

    @property
    def in_fence(self) -> bool:
        """Whether the next character belongs to an open fenced code block."""
        return self._in_fence

    def feed(self, text: str) -> str:
        """Format the next fragment, continuing from the state so far."""
        out: list[str] = []
        double = self._source == "verbatim"
        for char in text:
            if char != "\n":
                out.append(char)
                self._line += char
                continue
            is_delimiter = self._line.lstrip().startswith("```")
            out.append("\n" if (not double or self._in_fence or is_delimiter) else "\n\n")
            if is_delimiter:
                self._in_fence = not self._in_fence
            self._line = ""
        return "".join(out)


class QuietCollapsible(Collapsible):
    """A `Collapsible` that does not scroll its container when it folds.

    Textual's own `_watch_collapsed` ends by scrolling the box into view, which is right for a page
    of collapsibles a reader clicks through and wrong for a transcript: these boxes fold without
    anyone asking (thinking folds when the answer starts, Ctrl+R folds every one), and each fold
    would drag a reader who is deliberately reading history. The scroll is dropped and the
    state-keeping lines kept. The messages still post, so anything that wants to react to a fold
    can; the transcript decides where it scrolls.
    """

    def _watch_collapsed(self, collapsed: bool) -> None:
        self._update_collapsed(collapsed)
        if self.collapsed:
            self.post_message(self.Collapsed(self))
        else:
            self.post_message(self.Expanded(self))


class ReasoningRegion(QuietCollapsible):
    """A collapsible block of the model's thinking, kept apart from the answer so it can be
    folded on its own. It takes its text whole (`text`), or streams it: a delta that arrives
    before the markdown is mounted is held and written with the next."""

    def __init__(self, text: str = "", *, collapsed: bool = False) -> None:
        self._md = Markdown(text)
        super().__init__(self._md, title="Thinking…", collapsed=collapsed)
        self.add_class("reasoning-region")
        self._held = ""
        self._stream: MarkdownStream | None = None

    async def append_delta(self, delta: str) -> None:
        """Stream one delta into the body without reparsing the document."""
        if not delta:
            return
        self._held += delta
        if not self._md.is_mounted:
            return
        if self._stream is None:
            self._stream = Markdown.get_stream(self._md)
        held, self._held = self._held, ""
        await self._stream.write(held)

    async def finish_stream(self) -> None:
        """Stop this region's open stream, if any (a no-op when nothing was streamed)."""
        if self._stream is not None:
            stream, self._stream = self._stream, None
            await stream.stop()

    def mark_done(self) -> None:
        """Freeze the title: the thinking is complete."""
        self.title = "Thought"


class ToolBox(QuietCollapsible):
    """A tool call paired with its outcome in ONE collapsible.

    Folded (the default) it shows the call signature, with a mark once the outcome lands (the
    check for a result, a cross for an error, a stop sign for an abort); unfolded, the arguments
    and the result. The two bodies are mounted on first unfold, so a turn of sixty calls does not
    cost sixty bodies nobody opened. `result_markdown` is the body whether or not it was ever
    mounted.
    """

    def __init__(self, name: str, arguments: object, call_id: str = "") -> None:
        self.tool_name = name
        self.call_id = call_id
        self._summary = format_tool_summary(name, arguments)
        self._arguments = arguments
        self._result_markdown = ""
        self._built = False
        self._result_md: Markdown | None = None
        super().__init__(title=_plain(self._summary), collapsed=True)
        self.add_class("tool-box")
        self.has_result = False

    def on_mount(self) -> None:
        if not self.collapsed:
            self._build_body()

    def _on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if not self.collapsed:
            self._build_body()

    def _build_body(self) -> None:
        if self._built or not self.is_mounted:
            return
        self._built = True
        args = Markdown(self._args_block(self._arguments))
        self._result_md = Markdown(self._result_markdown)
        self._result_md.display = bool(self._result_markdown)
        self.query_one(Collapsible.Contents).mount(args, self._result_md)

    @property
    def result_markdown(self) -> str:
        """The result body as markdown, `""` until one lands."""
        return self._result_markdown

    def _write_result_body(self, markdown: str) -> None:
        self._result_markdown = markdown
        if self._result_md is not None:
            self._result_md.display = True
            self._result_md.update(markdown)

    @staticmethod
    def _args_block(arguments: object) -> str:
        if isinstance(arguments, str):
            return f"```\n{arguments}\n```"
        return "```json\n" + json.dumps(arguments, indent=2, default=str) + "\n```"

    @staticmethod
    def _fenced(text: str) -> str:
        shown = text if len(text) <= _RESULT_LIMIT else text[:_RESULT_LIMIT] + "\n…(truncated)"
        return f"```\n{shown}\n```"

    def set_arguments(self, text: str) -> None:
        """The argument text so far of a call the model is still writing."""
        self._arguments = text
        self._summary = format_tool_summary(self.tool_name, self._arguments)
        if not self.has_result:
            self.title = _plain(self._summary)

    def set_progress(self, text: str) -> None:
        """Show the output the call has written so far, until its outcome replaces it."""
        self._write_result_body(self._fenced(text))

    def set_result(self, text: str, *, error: bool = False, interrupted: bool = False) -> None:
        """Fold the call's outcome in: `text` is what the tool returned or why it failed."""
        mark = "⊘" if interrupted else "✗" if error else "✓"
        self.title = _plain(f"{mark} {self._summary}")
        self._write_result_body(self._fenced(text))
        self.has_result = True
        if error or interrupted:
            self.add_class("box-error")


class MessageBox(Static):
    """The one widget per message: a bordered box whose title says who wrote it.

    A box renders, top to bottom: an optional `ReasoningRegion` (an assistant's thinking), the
    text (markdown), and a `ToolBox` per call, in which the call's outcome is folded by call id.
    A user's text is `verbatim`, an assistant's `markdown`. Children may be added before the box is
    mounted: they are held until it is.
    """

    def __init__(
        self, role: str, title: str, content: str = "", subtitle: str = "", *, source: ContentSource
    ) -> None:
        super().__init__(classes=f"chat-message box-{role}")
        self.role = role
        self._title = title
        self._subtitle = subtitle
        self._reasoning: ReasoningRegion | None = None
        self._tool_boxes: dict[str, ToolBox] = {}
        self._deferred: list[tuple[str, Widget]] = []
        self._stream: MarkdownStream | None = None
        self._formatter = MarkdownLineFormatter(source)
        self._slots: dict[str, Vertical] = {}
        self._body = Markdown(self._formatter.feed(content), classes="message-content")
        self._held = ""  # formatted text that came before the body was mounted

    def compose(self) -> ComposeResult:
        self._slots = {
            "reasoning": Vertical(classes="message-reasoning"),
            "tools": Vertical(classes="message-tools"),
        }
        yield self._slots["reasoning"]
        yield self._body
        yield self._slots["tools"]

    def on_mount(self) -> None:
        self.border_title = self._title
        if self._subtitle:
            self.border_subtitle = self._subtitle
        for slot, widget in self._deferred:
            self._slots[slot].mount(widget)
        self._deferred.clear()

    def _mount_in(self, slot: str, widget: Widget) -> None:
        if slot in self._slots:
            self._slots[slot].mount(widget)
        else:
            self._deferred.append((slot, widget))

    async def append_content_delta(self, delta: str) -> None:
        """Stream one delta into the text; a delta that arrives before the body is mounted is held
        and written with the next. The live text itself is `SessionView`'s (`LiveLayer.text`)."""
        if not delta:
            return
        self._held += self._formatter.feed(delta)
        if not self._body.is_mounted:
            return
        if self._stream is None:
            self._stream = Markdown.get_stream(self._body)
        held, self._held = self._held, ""
        await self._stream.write(held)

    async def finish_stream(self) -> None:
        """Stop the open content stream, if any (a no-op when nothing was streamed)."""
        if self._stream is not None:
            stream, self._stream = self._stream, None
            await stream.stop()

    def add_thinking(self, text: str) -> ReasoningRegion:
        """Add the thinking of a reply that is complete, folded."""
        region = ReasoningRegion(text, collapsed=True)
        region.mark_done()
        self._reasoning = region
        self._mount_in("reasoning", region)
        return region

    async def stream_thinking(self, delta: str) -> None:
        """Stream a delta of thinking into a box that is mounted, opening the region first."""
        if self._reasoning is None:
            self._reasoning = ReasoningRegion()
            await self._slots["reasoning"].mount(self._reasoning)
        await self._reasoning.append_delta(delta)

    def add_tool_call(self, name: str, arguments: object, call_id: str) -> ToolBox:
        """Add a call as a `ToolBox`, tracked by id for its outcome."""
        box = ToolBox(name, arguments, call_id)
        self._tool_boxes[call_id] = box
        self._mount_in("tools", box)
        return box

    @property
    def reasoning(self) -> ReasoningRegion | None:
        """The thinking region, if there is thinking."""
        return self._reasoning

    @property
    def tool_boxes(self) -> dict[str, ToolBox]:
        """The calls' boxes, by call id."""
        return self._tool_boxes
