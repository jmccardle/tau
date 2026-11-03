"""`SessionView` (D23 section 2): a frontend's picture of the session, built from its stream.

Events go in with `hear`; what they said stays: each open cursor's `CursorView`, the closed
cursors, the labels, the nodes by id, the notices, and for each cursor whose `Live` the client
hears, the live layer: what the sample in flight has said and the tree does not hold yet. No
Textual and no calls: the widgets read it, and `tests/tui` drives it with events alone.

The live layer follows model.md section 9's client rules: a reply's deltas are drawn over the
cursor and dropped when its view has another `at` or is not busy; a `CallArgs` whose `CallNamed`
was never heard is ignored; a call's tool progress, keyed by `(node, call_id)`, is dropped when
the `Results` node answering it arrives (or when the cursor is no longer busy).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from tau._core import Body, Node
from tau._events import (
    Appended,
    CallArgs,
    CallNamed,
    CursorGone,
    CursorView,
    Event,
    Labels,
    Live,
    LiveDelta,
    Notice,
    Retrying,
    TextDelta,
    ThinkingDelta,
    ToolProgress,
)


@dataclass
class FormingCall:
    """A call the model is still writing: its provider `id`, the `tool` and the `args` text so far
    (`args_raw` once it is a node)."""

    id: str
    tool: str
    args: str = ""


@dataclass
class LiveLayer:
    """What a watched cursor's sample has said and the tree does not hold yet: the reply's `text`
    and `thinking` so far, the `calls` forming (by their index in the reply), and each running
    call's `progress` output keyed by `(node, call_id)`. `retrying` is the `Retrying` the provider
    last said, until the sample says anything else or ends. `at` is the cursor's node when the
    first delta arrived: the layer is drawn over it."""

    at: int | None = None
    text: str = ""
    thinking: str = ""
    calls: dict[int, FormingCall] = field(default_factory=lambda: {})
    progress: dict[tuple[int, str], str] = field(default_factory=lambda: {})
    retrying: Retrying | None = None

    @property
    def empty(self) -> bool:
        """Whether there is nothing to draw."""
        return not (self.text or self.thinking or self.calls or self.progress)

    def drop_reply(self) -> None:
        """Forget the reply's text, thinking and calls (the node arrived, or the step ended)."""
        self.at = None
        self.retrying = None
        self.text = ""
        self.thinking = ""
        self.calls = {}


class SessionView:
    """What the session's stream has told one client. Feed it every event, in order, with `hear`;
    read `views`, `gone`, `labels`, `nodes`, `live` and `notices`. It makes no call and keeps no
    widget: `tau.tui.TauApp` renders from it."""

    def __init__(self) -> None:
        self.views: dict[int, CursorView] = {}
        """Each open cursor as last told."""
        self.gone: dict[int, int] = {}
        """Each closed cursor and the node it was last at."""
        self.labels: tuple[tuple[str, int], ...] = ()
        """Every label and its cursor."""
        self.nodes: dict[int, Node] = {}
        """The nodes the stream has carried, by id."""
        self.live: dict[int, LiveLayer] = {}
        """The live layer of each cursor whose `Live` was heard."""
        self.notices: list[str] = []
        """Every `Notice`, oldest first."""

    def hear(self, event: Event) -> None:
        """Take one event into the picture."""
        match event:
            case CursorView():
                self._view(event)
            case CursorGone(cursor=cursor, at=at):
                self.views.pop(cursor, None)
                self.live.pop(cursor, None)
                self.gone[cursor] = at
            case Labels(labels=labels):
                self.labels = labels
            case Appended(nodes=nodes):
                self._appended(nodes)
            case Notice(text=text):
                self.notices.append(text)
            case Live(cursor=cursor, delta=delta):
                self._live(cursor, delta)

    def forget_live(self, cursor: int) -> None:
        """Drop `cursor`'s live layer: the client stopped watching it, so what the layer holds
        would be heard again, from the start, if it is watched again."""
        self.live.pop(cursor, None)

    def labels_of(self, cursor: int) -> tuple[str, ...]:
        """The labels on `cursor`, in label order."""
        return tuple(name for name, on in self.labels if on == cursor)

    def path(self, tip: int, at: int) -> list[Node] | None:
        """The nodes after `tip` down to `at`, oldest first, if `at` descends from `tip` through
        nodes this view holds (`[]` when they are the same node). `None` if it does not, or a node
        on the way was never carried: the caller reads `walk(at)` instead."""
        found: list[Node] = []
        node = at
        while node != tip:
            held = self.nodes.get(node)
            if held is None or held.parent is None:
                return None
            found.append(held)
            node = held.parent
        found.reverse()
        return found

    def _view(self, view: CursorView) -> None:
        self.gone.pop(view.cursor, None)
        self.views[view.cursor] = view
        layer = self.live.get(view.cursor)
        if layer is None:
            return
        if not view.busy:
            layer.drop_reply()
            layer.progress = {}
        elif layer.at is not None and layer.at != view.at:
            layer.drop_reply()

    def _appended(self, nodes: Iterable[Node]) -> None:
        for node in nodes:
            self.nodes[node.id] = node
            if isinstance(node.body, Body.Results) and node.parent is not None:
                for layer in self.live.values():
                    for key in [k for k in layer.progress if k[0] == node.parent]:
                        del layer.progress[key]

    def _live(self, cursor: int, delta: LiveDelta) -> None:
        layer = self.live.setdefault(cursor, LiveLayer())
        view = self.views.get(cursor)
        if layer.at is None and view is not None:
            layer.at = view.at
        if not isinstance(delta, Retrying | ToolProgress):
            layer.retrying = None  # the sample is going again
        match delta:
            case Retrying():
                layer.retrying = delta
            case TextDelta(text=text):
                layer.text += text
            case ThinkingDelta(text=text):
                layer.thinking += text
            case CallNamed(index=index, id=id, tool=tool):
                layer.calls[index] = FormingCall(id, tool)
            case CallArgs(index=index, text=text):
                if index in layer.calls:
                    layer.calls[index].args += text
            case ToolProgress(node=node, call_id=call_id, text=text):
                key = (node, call_id)
                layer.progress[key] = layer.progress.get(key, "") + text
