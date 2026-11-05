"""The transcript: the walk of the viewed cursor as boxes (D23 section 2).

In the manner of τ 0.15's tau-coding-agent/src/tau_coding_agent/transcript.py (tag v0.15.0): a
scrolling container that follows the tail while the reader is at it. Written new: none of its
event routing is carried; the render window is `tau.tui._window`, in the manner of the 0.15 one).
It is fed nodes (`add`, `rebuild`) and the live layer (`live`, `progress`, `end_live`) by the app,
which reads them from `SessionView`.

`User` is a box titled with its author when that is not this client, `Assistant` a box with its
thinking in a folding region and a tool box per call, whose outcome is folded in from the
`Results` node by call id. `System` is one folded line, `Config` one dim line, `Custom` a
`sample_error` as an error box and any other kind one dim line; a stop other than `End` or `Tools`
is a dim line under the reply.
"""

import asyncio
import json
from collections.abc import Callable, Sequence

from textual.widgets import Collapsible, Static

from tau._core import Author, Body, Node, Outcome, Part, Stop
from tau._events import CallArgs, CallNamed, LiveDelta, TextDelta, ThinkingDelta
from tau._usage import tokens
from tau.tui._chat import MessageBox, QuietCollapsible
from tau.tui._view import LiveLayer
from tau.tui._window import tail_start
from tau.tui._windowed import WindowedWalk

_NOTE_LIMIT = 100  # characters of a record we do not know, shown on its one line


def author_name(author: Author) -> str:
    """Who wrote a node, as the transcript titles it: the client id, the extension's name, or
    what wrote it (`model`, `tools`, `core`)."""
    match author:
        case Author.Frontend(client=who) | Author.Extension(name=who):
            return who
        case Author.Provider():
            return "model"
        case Author.Tools():
            return "tools"
        case _:
            return "core"


def usage_line(meta: str | None) -> str:
    """`↑ in ↓ out` from a reply's `meta` (the normalized `usage`), or `""` if it reports none."""
    if meta is None:
        return ""
    usage = json.loads(meta).get("usage")
    if not isinstance(usage, dict):
        return ""
    sent, got = usage.get("input"), usage.get("output")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(sent, int) or not isinstance(got, int):
        return ""
    return f"↑ {tokens(sent)}  ↓ {tokens(got)}"


def _arguments(args_raw: str) -> object:
    """The call's arguments decoded for the box, else the text as the model wrote it."""
    try:
        return json.loads(args_raw)
    except ValueError:
        return args_raw


def _outcome(outcome: Outcome) -> tuple[str, bool, bool]:
    """The text, whether it failed and whether it was interrupted."""
    match outcome:
        case Outcome.Ok(value=value):
            return value, False, False
        case Outcome.Err(error=error):
            return error, True, False
        case _:
            return "interrupted before it finished", False, True


def config_line(data: str) -> str:
    """What a `Config` node changed, in one line."""
    fields = json.loads(data)
    parts: list[str] = []
    if "model" in fields:
        parts.append(f"model: {fields['model']}")
    for name, options in fields.get("extensions", {}).items():
        parts.append(f"{name}: " + ", ".join(f"{k}={v}" for k, v in options.items()))
    return "  ".join(parts)


class ChatDisplay(WindowedWalk):
    """The viewed cursor's walk, drawn as boxes. It holds the whole walk and draws a window of it
    (`tau.tui._windowed`). `tip` is the walk's last node (`None` before the first `rebuild`); the
    app extends the walk with `add` or replaces it with `rebuild`."""

    def __init__(
        self, client: str, lock: asyncio.Lock, layer: Callable[[], LiveLayer | None]
    ) -> None:
        super().__init__(id="chat-display", lock=lock)
        self.client = client
        self._layer = (
            layer  # the live layer of the viewed cursor, as the app's `SessionView` has it
        )
        self.first: str | None = None
        """The first `User` text of the walk, for the header's title."""
        self._sides: dict[int, list[Node]] = {}  # records beside the walk, by their parent
        self._boxes: dict[int, MessageBox] = {}
        self._live: MessageBox | None = None

    async def _shown(self) -> None:
        """A window drawn at the tail while a reply is in flight (the reader came back to it)
        draws the reply from the layer, whole."""
        layer = self._layer()
        if self.at_tail and layer is not None and layer.at is not None and not layer.empty:
            await self._begin_live(layer)

    def _reset(self) -> None:
        self._boxes = {}

    def _forget(self, node: int) -> None:
        self._boxes.pop(node, None)

    # What is drawn

    async def rebuild(self, walk: Sequence[Node]) -> None:
        """Take `walk` as the whole walk and draw its last turns."""
        await self.end_live()
        self._walk, self._sides = list(walk), {}
        self.first = next((n.body.text for n in walk if isinstance(n.body, Body.User)), None)
        await self._show(tail_start(self._walk), len(self._walk))

    async def add(self, node: Node) -> None:
        """Take `node`, which follows the tip, onto the walk. It is drawn when the window ends the
        walk, or when this client wrote it (the window moves to the tail to show it); otherwise it
        is counted in the row below."""
        if self.first is None and isinstance(node.body, Body.User):
            self.first = node.body.text
        mine = isinstance(node.body, Body.User) and node.by == Author.Frontend(self.client)
        await self.extend(node, jump=mine)

    async def _draw(self, index: int) -> None:
        """Draw `walk[index]`, which follows the last block, and the records hung from it."""
        node = self._walk[index]
        body = node.body
        widget: Static | MessageBox | Collapsible | None = None
        line = ""
        match body:
            case Body.User(text=text):
                who = author_name(node.by)
                title = "User" if node.by == Author.Frontend(self.client) else who
                widget = MessageBox("user", title, text, source="verbatim")
                line = f"[{title}] {text}"
            case Body.Assistant():
                widget, line = self._reply(node)
            case Body.Results(outcomes=outcomes):
                line = self._results(node, outcomes)
            case Body.System(text=text):
                widget = QuietCollapsible(
                    Static(text, markup=False), title="System prompt", collapsed=True
                )
                widget.add_class("system-box")
                line = "[System prompt]"
            case Body.Config(config=config):
                widget = self._dim(config_line(config.data))
                line = config_line(config.data)
            case Body.Custom(kind=kind, data=data):
                widget, line = self._custom(kind, data)
            case _:
                raise TypeError(f"node {node.id} holds a body the transcript does not know")
        await self.place(index, node.id, widget, line)
        for side in self._sides.get(node.id, ()):
            assert isinstance(side.body, Body.Custom)
            drawn, text = self._custom(side.body.kind, side.body.data)
            await self.place(index, side.id, drawn, text)

    def is_failed_sample_here(self, node: Node) -> bool:
        """Whether `node` is the `sample_error` core wrote under the node we are at."""
        body = node.body
        return (
            isinstance(body, Body.Custom)
            and body.kind == "sample_error"
            and node.parent is not None
            and node.parent == self.tip
        )

    async def add_side(self, node: Node) -> None:
        """Draw a record core wrote beside the walk and did not move to (a `sample_error`): it
        hangs under the tip, so it is not on any walk read later. It stays while this walk does."""
        assert isinstance(node.body, Body.Custom)
        assert node.parent is not None
        self._sides.setdefault(node.parent, []).append(node)
        if self.at_tail:
            drawn, line = self._custom(node.body.kind, node.body.data)
            await self.place(len(self._walk) - 1, node.id, drawn, line)

    def _reply(self, node: Node) -> tuple[MessageBox, str]:
        body = node.body
        assert isinstance(body, Body.Assistant)
        text = "".join(p.text for p in body.parts if isinstance(p, Part.Text))
        thought = "".join(p.text for p in body.parts if isinstance(p, Part.Thinking))
        box = MessageBox("assistant", "Assistant", text, usage_line(body.meta), source="markdown")
        self._boxes[node.id] = box
        lines = [f"[Assistant] {text}"]
        for part in body.parts:
            if isinstance(part, Part.Call):
                box.add_tool_call(part.tool, _arguments(part.args_raw), part.id)
                lines.append(f"  {part.tool}({part.args_raw})")
        if thought:
            box.add_thinking(thought)
        if body.stop not in (Stop.End, Stop.Tools):
            lines.append(f"  (stopped: {body.finish_reason or body.stop})")
        return box, "\n".join(lines)

    def _results(self, node: Node, outcomes: Sequence[tuple[str, Outcome, Author]]) -> str:
        assert node.parent is not None
        boxes = self._boxes[node.parent].tool_boxes
        lines: list[str] = []
        for call, outcome, _by in outcomes:
            text, error, interrupted = _outcome(outcome)
            boxes[call].set_result(text, error=error, interrupted=interrupted)
            lines.append(f"  {boxes[call].title}: {text}")
        return "\n".join(lines)

    def _custom(self, kind: str, data: str) -> tuple[Static | MessageBox, str]:
        if kind == "sample_error":
            return MessageBox("error", "Error", data, source="verbatim"), f"[Error] {data}"
        shown = f"{kind}: {data}"
        shown = shown if len(shown) <= _NOTE_LIMIT else shown[: _NOTE_LIMIT - 1] + "…"
        return self._dim(shown), shown

    @staticmethod
    def _dim(text: str) -> Static:
        return Static(text, classes="chat-note", markup=False)

    # The live layer

    async def live(self, delta: LiveDelta, layer: LiveLayer) -> None:
        """Draw one live delta of the reply in flight (`ToolProgress` is `progress`). `layer` is
        `SessionView`'s, which owns what the reply has said; this only draws it. While the window is
        slid back nothing is drawn (the reply would land under the row that says what is not
        drawn); when the window comes to the tail the reply is drawn from `layer`, whole."""
        if not self.at_tail:
            return
        if self._live is None:
            await self._begin_live(layer)
            return
        match delta:
            case TextDelta(text=text):
                await self._live.append_content_delta(text)
            case ThinkingDelta(text=text):
                await self._live.stream_thinking(text)
            case CallNamed(id=call, tool=tool):
                self._live.add_tool_call(tool, "", call)
            case CallArgs(index=index):
                call = layer.calls[index]
                self._live.tool_boxes[call.id].set_arguments(call.args)
            case _:
                return
        if self.scroll_y >= self.max_scroll_y - 1:
            self.scroll_end(animate=False)

    async def _begin_live(self, layer: LiveLayer) -> None:
        """Draw the reply in flight as `layer` has it now: its text, thinking and calls."""
        box = MessageBox("assistant", "Assistant", source="markdown")
        box.add_class("live")
        self._live = box
        await self._mount(box)
        if layer.text:
            await box.append_content_delta(layer.text)
        if layer.thinking:
            await box.stream_thinking(layer.thinking)
        for call in layer.calls.values():
            box.add_tool_call(call.tool, "", call.id)
            if call.args:
                box.tool_boxes[call.id].set_arguments(call.args)
        if self._following:
            self.scroll_end(animate=False)

    def progress(self, node: int, call_id: str, text: str) -> None:
        """Show the output a running call has written so far (`text` is all of it). A call whose
        box is not drawn (the window is slid back) is ignored: `text` is cumulative, so the box
        drawn later is caught up by its result."""
        drawn = self._boxes.get(node)
        if drawn is not None and call_id in drawn.tool_boxes:
            drawn.tool_boxes[call_id].set_progress(text)

    async def end_live(self) -> None:
        """Remove the live reply: the node it was becoming has arrived, or the step ended."""
        if self._live is not None:
            live, self._live = self._live, None
            await live.finish_stream()
            reasoning = live.reasoning
            if reasoning is not None:
                await reasoning.finish_stream()
            await live.remove()

    @property
    def streaming(self) -> bool:
        """Whether a live reply is drawn."""
        return self._live is not None

    # What it says

    def text(self, layer: LiveLayer | None) -> str:
        """The transcript as plain text, one block per box: what is drawn, folded parts included,
        and the reply in flight (`layer`) while a live box is drawn."""
        live = f"[Assistant, live] {layer.text}" if self._live is not None and layer else ""
        earlier = [self._earlier_text] if self._earlier is not None else []
        later = [self._later_text] if self._later is not None else []
        lines = [b.line for b in self._blocks if b.line]
        return "\n".join([*earlier, *lines, *later, *([live] if live else [])])

    def fold(self, kind: type[QuietCollapsible]) -> None:
        """Fold every box of `kind`, or unfold them all if each is folded already."""
        boxes = list(self.query(kind))
        fold = any(not b.collapsed for b in boxes)
        for box in boxes:
            box.collapsed = fold
