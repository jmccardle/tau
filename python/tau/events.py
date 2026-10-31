"""What a client hears (D16 section 5): live deltas, and the state of the session in whole.

A provider's `sample` stream yields deltas (no cursor: the provider does not know which
cursor it serves), and a running tool reports `ToolProgress` through its `progress` callback.
The driver wraps each as a `Live` naming the cursor. Everything else a client hears is state
sent whole whenever it changes: `CursorView` per open cursor (or `CursorGone` once it is closed),
`Labels`, the nodes `Appended`, and a `Notice` for one client. A stream opens with a `CursorView`
per open cursor, a `CursorGone` per closed one, and `Labels`. Every event
is a plain frozen value, compared by value. A `CursorView` holds its questions' schemas, which
are dicts, so it is not hashable: a frontend keys on `cursor`, not on the event.
"""

from dataclasses import dataclass

from tau._core import Ending, Item, Node
from tau._values import Json


@dataclass(frozen=True)
class TextDelta:
    """Text of the reply arriving."""

    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """Thinking text arriving."""

    text: str


@dataclass(frozen=True)
class CallNamed:
    """A call begins forming: call number `index` in this reply, with its `id` and tool."""

    index: int
    id: str
    tool: str


@dataclass(frozen=True)
class CallArgs:
    """More of call `index`'s argument text (`args_raw`)."""

    index: int
    text: str


@dataclass(frozen=True)
class ToolProgress:
    """A running call showing partial output: `text` is new output since the last
    `ToolProgress` of the same call, not the whole so far. The call is named by `node`, the
    batch's node (the `Assistant` node holding the call), and `call_id`: a server may reuse
    ids across batches, so the id alone does not say which call. Only the driver makes one (a
    tool calls its `progress`); a provider cannot yield one. What the call saves is its
    `Outcome`, never this."""

    node: int
    call_id: str
    text: str


Delta = TextDelta | ThinkingDelta | CallNamed | CallArgs
"""What a provider yields while a part is still arriving."""
LiveDelta = Delta | ToolProgress
"""What a `Live` carries: a provider's delta, or a tool's progress."""


@dataclass(frozen=True)
class Live:
    """A delta, on the cursor whose sample or tool call produced it. Never in the tree: a client
    draws it as a layer over the cursor. Only a client watching the cursor hears it."""

    cursor: int
    delta: LiveDelta


@dataclass(frozen=True)
class OpenQuestion:
    """A question an extension asked and nobody has answered: `id` (what `answer` names), whom
    it is for (`to`: a client id, `None` for anyone), what it asks, and the JSON Schema of the
    answer."""

    id: int
    to: str | None
    text: str
    schema: Json


@dataclass(frozen=True)
class HookFailure:
    """A cursor hook of `extension` raised, or broke its contract, at hook `point`
    (`"pre_step"`, `"post_step"`, ...). `error` is `"Type: msg"`, the text a `post_step` hook
    sees in `Ending.Failed`."""

    extension: str
    point: str
    error: str


@dataclass(frozen=True)
class CursorView:
    """One cursor as a client sees it, whole: where it is (`at`), whether a step is running
    (`busy`), `auto`-step, its open `questions` (oldest first), its pending `items` (an
    `ItemData.Input` says who wrote it: its `by`), and how its last step ended (`ending`) with the
    `hook_error` that ended it, if a cursor hook failed in
    it. Both are `None` from the start of a step until it ends, and after a reload."""

    cursor: int
    at: int
    busy: bool
    auto: bool
    questions: tuple[OpenQuestion, ...]
    items: tuple[Item, ...]
    ending: Ending | None
    hook_error: HookFailure | None


@dataclass(frozen=True)
class CursorGone:
    """Cursor `cursor` is closed (D17): a client drops its view of it. `at` is the node it was
    last at, for restarting (`reopen`). A cursor has one state event at a time: its `CursorView`
    while open, this after it closes. Labels may still name it: they are its restart record."""

    cursor: int
    at: int


@dataclass(frozen=True)
class Labels:
    """Every label and the cursor it is on, in label order."""

    labels: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Appended:
    """Nodes added to the tree, in id order, bodies included."""

    nodes: tuple[Node, ...]


@dataclass(frozen=True)
class Notice:
    """A message for this client alone: `cursor.tell`, an input a hook refused."""

    text: str


Event = Live | CursorView | CursorGone | Labels | Appended | Notice


class CutOff(ConnectionError):
    """The stream of a client that fell too far behind ended: its `Appended` and `Notice` backlog
    passed the buffer. Like every stream that ends in error, it is a `ConnectionError`: connect
    again for a fresh snapshot."""
