"""Type stubs for the native module built from crates/tau-py.

Hand-written. `python -m mypy.stubtest tau._core` (run by
scripts/check.sh) fails when this file and the built module disagree.

Every value type is frozen and hashable. An enum with data is a base class
with one nested class per variant, so `match job:` works.
"""

from collections.abc import Callable, Sequence
from os import PathLike
from typing import Any, ClassVar, final

from typing_extensions import disjoint_base

__all__ = [
    "Answer",
    "Author",
    "BadRecord",
    "BadReplay",
    "Bash",
    "BashRun",
    "Body",
    "Busy",
    "Cancel",
    "Closed",
    "Config",
    "Corrupt",
    "CursorClosed",
    "CursorState",
    "Draft",
    "DuplicateCall",
    "Edit",
    "Ending",
    "Exists",
    "Find",
    "Grep",
    "HandleWrites",
    "Hook",
    "Item",
    "ItemData",
    "Job",
    "Leased",
    "Move",
    "Next",
    "NoSuchCursor",
    "NoSuchItem",
    "NoSuchLabel",
    "NoSuchNode",
    "Node",
    "NotASession",
    "NotInHook",
    "NotPending",
    "Outcome",
    "Part",
    "Poisoned",
    "Read",
    "Replay",
    "RunCall",
    "SampleEnd",
    "Session",
    "Stop",
    "TooLarge",
    "TraceError",
    "Tree",
    "Undecodable",
    "Version",
    "Write",
    "WrongAnswer",
    "first_message",
    "stop_trace",
    "trace_clock",
    "trace_to",
    "wire_decode",
    "wire_encode",
    "wire_schema",
]

class NoSuchNode(LookupError):
    """A node id that is not in the session."""

class NoSuchCursor(LookupError):
    """A cursor id that is not open."""

class CursorClosed(NoSuchCursor):
    """A call on a cursor that was closed; its last node is in `closed()`."""

class NoSuchLabel(LookupError):
    """A label that is not set."""

class Busy(RuntimeError):
    """A step on a cursor whose step is running."""

class NotInHook(RuntimeError):
    """A hook's move, write, or auto-step change when no cursor hook is running on that cursor."""

class NoSuchItem(LookupError):
    """An item id that is not in the scratchpad."""

class NotPending(LookupError):
    """A write that delivers an item not pending for that cursor."""

class DuplicateCall(ValueError):
    """A Results or Assistant body naming a call id twice (V2)."""

class BadRecord(ValueError):
    """A logged record that replay cannot apply."""

class BadReplay(ValueError):
    """Replay data that is not the text of a JSON object, or a replay with no provider name."""

class WrongAnswer(ValueError):
    """An answer to no job; nothing changed."""

class Closed(RuntimeError):
    """The session was closed."""

class Leased(OSError):
    """Another store holds the session's lease."""

class Exists(FileExistsError):
    """A session already exists in that directory."""

class NotASession(ValueError):
    """Not a τ log or scratchpad file."""

class Version(ValueError):
    """A session file of a version this build does not read."""

class Corrupt(ValueError):
    """A damaged scratchpad file."""

class Undecodable(ValueError):
    """A log record whose bytes are intact but do not decode; the log is not cut."""

class TooLarge(ValueError):
    """A record of 4 GiB or more, which the log cannot hold; the session is closed, as after a failed save."""

class Poisoned(RuntimeError):
    """A call panicked while it held the session (that call raised PanicException); load the session again."""

class TraceError(RuntimeError):
    """A trace started while one runs, stopped while none runs, or that cannot be installed."""

def first_message(directory: str | PathLike[str]) -> str | None:
    """The text of the first `User` node in the session in `directory`, read from the start of its
    log only as far as that node; no lease. `None` if it has none.
    """

def trace_to(path: str | PathLike[str]) -> None:
    """Start recording spans to a Chrome trace file at `path`."""

def stop_trace() -> None:
    """Stop recording and return once the trace file is complete."""

def trace_clock() -> float:
    """Microseconds since the first `trace_to`, on the clock the spans use."""

@final
class Stop:
    """Why a reply ended. Only `Tools` makes its calls runnable. It is not an integer:
    `Stop.Tools != 1`.
    """

    End: ClassVar[Stop]
    Tools: ClassVar[Stop]
    Length: ClassVar[Stop]
    Interrupted: ClassVar[Stop]
    Error: ClassVar[Stop]
    def __int__(self) -> int: ...
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Hook:
    """The seven cursor hook points, in the order a step reaches them. It is not an integer."""

    PreStep: ClassVar[Hook]
    PreInput: ClassVar[Hook]
    PreSample: ClassVar[Hook]
    PreReply: ClassVar[Hook]
    PostReply: ClassVar[Hook]
    PostResult: ClassVar[Hook]
    PostStep: ClassVar[Hook]
    @property
    def name(self) -> str:
        """The name an extension defines the hook by."""
    def __int__(self) -> int: ...
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Next:
    """What a cursor's walk needs next."""

    Wait: ClassVar[Next]
    Sample: ClassVar[Next]
    RunTools: ClassVar[Next]
    def __int__(self) -> int: ...
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Config:
    """How a step runs from this node on: the text of a JSON object with at most two keys, `"model"`
    and `"extensions"` (the options). The constructor does not check the text, and core stores it
    without reading it.
    """

    def __new__(cls, data: str) -> Config: ...
    @property
    def data(self) -> str:
        """The JSON object text, as given."""
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Node:
    """One record of a session's tree, written once and never changed: its `id`, its `parent`
    (`None` for a root), the `time` the driver gave it in milliseconds, who wrote it (`by`), what
    it holds (`body`), and the scratchpad items it consumed (`delivers`).
    """

    @property
    def id(self) -> int:
        """The node's id; ids are dense from 0."""
    @property
    def parent(self) -> int | None:
        """The node it follows, or `None` for a root."""
    @property
    def time(self) -> int:
        """The time the driver gave it, in milliseconds."""
    @property
    def by(self) -> Author:
        """Who wrote it."""
    @property
    def body(self) -> Body:
        """What it holds."""
    @property
    def delivers(self) -> list[int]:
        """The ids of the scratchpad items it consumed."""
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Move:
    """The record of a cursor changing node. `from` is a Python keyword, so `from_`."""

    @property
    def cursor(self) -> int:
        """The cursor that moved."""
    @property
    def from_(self) -> int | None:
        """The node it left, or `None` when the move opened the cursor."""
    @property
    def to(self) -> int:
        """The node it moved to."""
    @property
    def by(self) -> Author:
        """Who asked for the move."""
    @property
    def time(self) -> int:
        """The time the driver gave it, in milliseconds."""
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Item:
    """One item in a cursor's scratchpad. Ids are never reused."""

    @property
    def id(self) -> int:
        """The item's id."""
    @property
    def to(self) -> int:
        """The cursor it is addressed to."""
    @property
    def data(self) -> ItemData:
        """What it holds."""
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Draft:
    """What the current phase will commit as a node. A hook may edit it; none (`None`) commits
    nothing. Built as `Draft(body, delivers, by)`.
    """

    def __new__(cls, body: Body, delivers: Sequence[int], by: Author) -> Draft: ...
    @property
    def body(self) -> Body:
        """What the node will hold."""
    @property
    def delivers(self) -> list[int]:
        """The ids of the scratchpad items the node will consume."""
    @property
    def by(self) -> Author:
        """Who the node will be written by."""
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class RunCall:
    """One call of a batch, handed out in `Job.RunCalls` to be run."""

    @property
    def id(self) -> str:
        """The call's id, as in the reply."""
    @property
    def tool(self) -> str:
        """The name of the tool to run."""
    @property
    def args_raw(self) -> str:
        """The arguments exactly as the model produced them."""
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Replay:
    """Data only the provider that wrote a part can send back (a thinking signature, for one).
    `provider` names that provider; `data` is the text of a JSON object, kept exactly as given.
    Construction raises `BadReplay` for an empty or blank provider name, or `data` that is not
    a strict JSON object, so a `Part` never holds a bad one.
    """

    def __new__(cls, provider: str, data: str) -> Replay: ...
    @property
    def provider(self) -> str:
        """The name of the provider that wrote the part."""
    @property
    def data(self) -> str:
        """The text of the JSON object, exactly as the provider gave it."""
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

@disjoint_base
class Part:
    """One piece of a reply, in the order the provider streamed it: `Text`, `Thinking` or `Call`.
    Each may carry a `Replay`.
    """

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Text(Part):
        """Words for the reader."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, text: str, replay: Replay | None = None) -> Part.Text: ...
        @property
        def text(self) -> str:
            """The words."""
        @property
        def replay(self) -> Replay | None:
            """Data only the writing provider can send back, or `None`."""

    @final
    class Thinking(Part):
        """The model's reasoning. `text` may be empty when `replay` holds a redacted block."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, text: str, replay: Replay | None = None) -> Part.Thinking: ...
        @property
        def text(self) -> str:
            """The reasoning, possibly empty."""
        @property
        def replay(self) -> Replay | None:
            """Data only the writing provider can send back, or `None`."""

    @final
    class Call(Part):
        """A request to run one tool. A call is never a node of its own."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(
            cls, id: str, tool: str, args_raw: str, replay: Replay | None = None
        ) -> Part.Call: ...
        @property
        def id(self) -> str:
            """The provider's id for the call, unique within its node."""
        @property
        def tool(self) -> str:
            """The name of the tool to run."""
        @property
        def args_raw(self) -> str:
            """The arguments exactly as the model produced them, never re-serialised."""
        @property
        def replay(self) -> Replay | None:
            """Data only the writing provider can send back, or `None`."""

@disjoint_base
class Outcome:
    """How one tool call ended: `Ok`, `Err` or `Interrupted`."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Ok(Outcome):
        """The call succeeded."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, value: str) -> Outcome.Ok: ...
        @property
        def value(self) -> str:
            """What the tool returned."""

    @final
    class Err(Outcome):
        """The call failed."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, error: str) -> Outcome.Err: ...
        @property
        def error(self) -> str:
            """Why, as the model will read it."""

    @final
    class Interrupted(Outcome):
        """An abort stopped the call or kept it from starting; the tool may or may not have run."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Outcome.Interrupted: ...

@disjoint_base
class Author:
    """Who wrote a node, or asked for a move. It is recorded and never used to decide anything."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Frontend(Author):
        """A frontend, named by its client name."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, client: str) -> Author.Frontend: ...
        @property
        def client(self) -> str:
            """The client name the frontend gave."""

    @final
    class Provider(Author):
        """The model's provider."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Author.Provider: ...

    @final
    class Tools(Author):
        """The tools that ran a call."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Author.Tools: ...

    @final
    class Core(Author):
        """The core itself."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Author.Core: ...

    @final
    class Extension(Author):
        """An extension."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, name: str) -> Author.Extension: ...
        @property
        def name(self) -> str:
            """The extension's name."""

@disjoint_base
class Body:
    """What a node holds. `System`, `User`, `Assistant` and `Results` are what the model reads;
    `Config` and `Custom` never reach it.
    """

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class System(Body):
        """The system text a session starts with."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, text: str) -> Body.System: ...
        @property
        def text(self) -> str:
            """The system text."""

    @final
    class User(Body):
        """A message from the person (or an extension) using the session."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, text: str) -> Body.User: ...
        @property
        def text(self) -> str:
            """The message."""

    @final
    class Assistant(Body):
        """The model's reply."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(
            cls,
            parts: Sequence[Part],
            stop: Stop,
            finish_reason: str | None = None,
            meta: str | None = None,
        ) -> Body.Assistant: ...
        @property
        def parts(self) -> list[Part]:
            """The reply's pieces, in stream order."""
        @property
        def stop(self) -> Stop:
            """Why the reply ended."""
        @property
        def finish_reason(self) -> str | None:
            """The provider's own finish string, or `None`."""
        @property
        def meta(self) -> str | None:
            """What the response carried besides its parts (usage, for one), as JSON object text,
            or `None`.
            """

    @final
    class Results(Body):
        """The answer to a batch of calls: one `(call id, outcome, author)` per call, in call order."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, outcomes: Sequence[tuple[str, Outcome, Author]]) -> Body.Results: ...
        @property
        def outcomes(self) -> list[tuple[str, Outcome, Author]]:
            """The outcomes, one per call."""

    @final
    class Config(Body):
        """A change of how steps run from here on."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, config: Config) -> Body.Config: ...
        @property
        def config(self) -> Config:
            """The new configuration."""

    @final
    class Custom(Body):
        """A record an extension (or core) keeps in the tree that the model never reads."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, kind: str, data: str) -> Body.Custom: ...
        @property
        def kind(self) -> str:
            """What kind of record this is; core writes `sample_error`."""
        @property
        def data(self) -> str:
            """The record's data, as text."""

@disjoint_base
class ItemData:
    """What a scratchpad item holds: an `Input`, or an `Extension`'s own data."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Input(ItemData):
        """A line for the cursor to take as a `User` message."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, by: Author, text: str) -> ItemData.Input: ...
        @property
        def by(self) -> Author:
            """Who wrote it."""
        @property
        def text(self) -> str:
            """The line."""

    @final
    class Extension(ItemData):
        """Data an extension keeps in the scratchpad; core does not read it."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, name: str, data: str) -> ItemData.Extension: ...
        @property
        def name(self) -> str:
            """The extension's name."""
        @property
        def data(self) -> str:
            """The extension's data, as text."""

@disjoint_base
class Ending:
    """How a step ended; the subject of the `post_step` hook."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Done(Ending):
        """The step ran to its end."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Ending.Done: ...

    @final
    class Halted(Ending):
        """A cursor hook halted it."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, reason: str) -> Ending.Halted: ...
        @property
        def reason(self) -> str:
            """The reason the hook gave."""

    @final
    class Failed(Ending):
        """A hook or the sample failed."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, error: str) -> Ending.Failed: ...
        @property
        def error(self) -> str:
            """What went wrong."""

    @final
    class Aborted(Ending):
        """An abort ended it."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Ending.Aborted: ...

@disjoint_base
class Job:
    """What a step asks the driver to do next, from `Session.next_job`. Answer it with `Session.answer`."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Hooks(Job):
        """Run the extensions' hooks for one hook point."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(
            cls, point: Hook, draft: Draft | None, node: int | None, ending: Ending | None
        ) -> Job.Hooks: ...
        @property
        def point(self) -> Hook:
            """The hook point."""
        @property
        def draft(self) -> Draft | None:
            """The subject at `pre_input`, `pre_sample` and `pre_reply`; otherwise `None`."""
        @property
        def node(self) -> int | None:
            """The subject at `post_reply` and `post_result`: the node just committed; otherwise `None`."""
        @property
        def ending(self) -> Ending | None:
            """The subject at `post_step`; otherwise `None`."""

    @final
    class Sample(Job):
        """Ask the provider for a reply to the cursor's walk."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Job.Sample: ...

    @final
    class RunCalls(Job):
        """Run a batch of calls and answer each with `Answer.CallDone`."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, node: int, calls: Sequence[RunCall]) -> Job.RunCalls: ...
        @property
        def node(self) -> int:
            """The `Assistant` node holding the batch."""
        @property
        def calls(self) -> list[RunCall]:
            """The calls, in call order."""

    @final
    class Pending(Job):
        """Calls are still out; nothing to do until they are answered."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Job.Pending: ...

    @final
    class Rest(Job):
        """The step is over and the cursor is at rest."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Job.Rest: ...

@disjoint_base
class SampleEnd:
    """How a sample ended, as the driver saw it. Core decides what the tree records."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Finished(SampleEnd):
        """The provider finished the reply."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(
            cls, stop: Stop, reason: str | None = None, meta: str | None = None
        ) -> SampleEnd.Finished: ...
        @property
        def stop(self) -> Stop:
            """Why the reply ended."""
        @property
        def reason(self) -> str | None:
            """The provider's own finish string, or `None`."""
        @property
        def meta(self) -> str | None:
            """What the response carried besides its parts, as JSON object text, or `None`."""

    @final
    class Failed(SampleEnd):
        """The sample failed."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, error: str, meta: str | None = None) -> SampleEnd.Failed: ...
        @property
        def error(self) -> str:
            """What went wrong."""
        @property
        def meta(self) -> str | None:
            """The failure's details as JSON object text, or `None`."""

    @final
    class Cancelled(SampleEnd):
        """The cursor was aborted before the sample finished."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> SampleEnd.Cancelled: ...

@disjoint_base
class Answer:
    """What the driver answers a `Job` with. `Session.answer` accepts one only while the job it
    answers is out; any other raises `WrongAnswer`.
    """

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Hooks(Answer):
        """The hooks ran."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, stop: str | None = None, draft: Draft | None = None) -> Answer.Hooks: ...
        @property
        def stop(self) -> str | None:
            """A halt reason if a hook halted the step, else `None`."""
        @property
        def draft(self) -> Draft | None:
            """The draft after the hooks edited it; `None` commits nothing. At `pre_sample`, a
            reply here stands in for the sample.
            """

    @final
    class HookFailed(Answer):
        """A hook raised."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, error: str) -> Answer.HookFailed: ...
        @property
        def error(self) -> str:
            """What went wrong."""

    @final
    class Sampled(Answer):
        """The sample ended."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, parts: Sequence[Part], end: SampleEnd) -> Answer.Sampled: ...
        @property
        def parts(self) -> list[Part]:
            """What streamed before it ended."""
        @property
        def end(self) -> SampleEnd:
            """How it ended."""

    @final
    class CallDone(Answer):
        """One call of the batch ended."""

        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, call: str, outcome: Outcome, by: Author) -> Answer.CallDone: ...
        @property
        def call(self) -> str:
            """The call's id."""
        @property
        def outcome(self) -> Outcome:
            """How it ended."""
        @property
        def by(self) -> Author:
            """`Tools` if the tool ran unedited, else the extension that answered or last edited it."""

@final
class Tree:
    """The session's nodes: `Session.tree`. It shares the session's lock, so a
    read is as current as the call that made it, and a closed session raises
    `Closed` here too.
    """

    def node(self, id: int) -> Node:
        """The node with this id. `NoSuchNode` if there is none."""
    def walk(self, node: int) -> list[Node]:
        """The nodes root to `node`, inclusive."""
    def children(self, node: int) -> list[int]:
        """The children of `node`, in append order."""
    def __len__(self) -> int: ...
    def config_at(self, node: int) -> Config:
        """The `Config` in force at `node`: the nearest one on its walk. `NoSuchNode` if absent."""
    def lca(self, a: int, b: int) -> int | None:
        """The deepest node on both walks; `None` across trees."""

@final
class CursorState:
    """One cursor as `Session.cursor` read it: every field from the same instant,
    under one lock, so `busy` and `at` cannot disagree. Later changes do not
    reach it. Building one costs the cursor's items and labels, not its moves:
    a move list is append-only, so the snapshot keeps where it ended and reads
    `moves` (under the lock, raising `Closed` if the session is closed) when
    asked. A move made after the snapshot is not in it.
    """

    @property
    def at(self) -> int:
        """The node the cursor is on."""
    @property
    def auto(self) -> bool:
        """Whether auto-step is on."""
    @property
    def busy(self) -> bool:
        """A step is running."""
    @property
    def next(self) -> Next:
        """What the cursor needs next, read from its walk alone."""
    @property
    def items(self) -> list[Item]:
        """Its scratchpad, oldest first."""
    @property
    def labels(self) -> list[str]:
        """Its labels, in string order."""
    @property
    def moves(self) -> list[Move]:
        """Every move of the cursor, oldest first."""
    @property
    def moves_since_step(self) -> list[Move]:
        """Its moves since the running step woke, other hooks' included; none at rest."""
    def __repr__(self) -> str: ...

@final
class HandleWrites:
    """`Session`'s extension writes made by a hook's handle: with the hook's
    cursor, they ride its phase's sync while its hook job is out. Only the
    handles hold one (`HandleWrites(session, hook)`), so a frontend cannot name a
    cursor and skip a sync.
    """

    def __new__(cls, session: Session, hook: int | None) -> HandleWrites: ...
    # `Session`'s extension writes for the handles of a running hook (`hook`: its
    # cursor, or `None` for a tool hook's).
    def open(self, at: int, by: Author, now: int) -> int: ...
    def add_item(self, to: int, data: ItemData) -> int: ...
    def remove_item(self, id: int) -> None: ...
    def append(self, parent: int, body: Body, by: Author, now: int) -> int: ...
    def append_root(self, config: Config, by: Author, now: int) -> int: ...
    def set_label(self, label: str, cursor: int) -> None: ...
    def remove_label(self, label: str) -> None: ...

@final
class Session:
    """One session: its tree, cursors, scratchpad, and every cursor's step. It does no IO of its
    own: a program asks it for each cursor's next `Job` and gives back an `Answer`.

    A call that changes the session hands its records to the store (when there is one) before
    it returns, so a program never saves. Every call takes the session's one lock, so a
    session may be shared across threads. A closed session raises `Closed`.
    """

    @staticmethod
    def create(
        config: Config, system: str, now: int, directory: str | PathLike[str] | None = None
    ) -> Session:
        """A new session with a `Config` root, a `System` node, and cursor 0 on
        it. In `directory` (created if missing) when given, else in memory.
        """
    @staticmethod
    def load(directory: str | PathLike[str]) -> Session:
        """Open the session in `directory`, taking its lease. Every cursor loads
        with auto-step off, woken (load is a wake).
        """
    @staticmethod
    def read(directory: str | PathLike[str]) -> Session:
        """The session in `directory` as a copy in memory, as of its last sync, taking no lease
        and writing nothing (D18 section 7). It accepts changes and never saves them.
        """
    def fork(self, node: int, now: int, directory: str | PathLike[str] | None = None) -> Session:
        """A new session holding a copy of `walk(node)` (D18 section 6): node *i* of the copy is
        node *i* of the walk, with cursor 0 on its tip. In `directory` (created if missing)
        when given, else in memory, written with one sync. `NoSuchNode` if `node` is absent.
        """
    def close(self) -> None:
        """Drop the store and release the lease without writing what is not yet
        synced, as a crash would. Every later call raises `Closed`. A second
        close is `Closed` too.
        """
    @property
    def tree(self) -> Tree:
        """The session's nodes."""
    def input(self, cursor: int, by: Author, text: str) -> int:
        """Add an `Input` item for `cursor`, written `by` the author, and wake the cursor.
        Returns the item's id. `NoSuchCursor` or `CursorClosed` if the cursor is not open.
        """
    def step(self, cursor: int) -> None:
        """Run one step of `cursor` whether or not auto-step is on, and leave auto-step as it is.
        `Busy` if a step is already running.
        """
    def go_to(self, cursor: int, to: int, client: str, now: int) -> bool:
        """Move `cursor` to node `to` for the frontend named `client`, turn auto-step off and wake
        the cursor. On a busy cursor the step is aborted and the move applies when it rests.
        Returns whether the driver must cancel the effect in flight. `NoSuchNode` if `to`
        is absent.
        """
    def set_auto(self, cursor: int, on: bool) -> None:
        """Turn auto-step on or off for `cursor`; on wakes it."""
    def abort(self, cursor: int) -> bool:
        """Stop `cursor`'s step: auto-step goes off and no later phase runs. A running hook finishes.
        Returns whether the driver must cancel the effect in flight.
        """
    def close_cursor(self, cursor: int) -> None:
        """Close a cursor at rest (`Busy` otherwise): the driver aborts and waits first."""
    def open(self, at: int, by: Author, now: int) -> int:
        """Open a new cursor at node `at` and wake it. Returns its id. `NoSuchNode` if `at` is absent."""
    def add_item(self, to: int, data: ItemData) -> int:
        """Add an item to cursor `to`'s scratchpad and wake it. Returns the item's id."""
    def remove_item(self, id: int) -> None:
        """Remove an item from the scratchpad. `NoSuchItem` if it is not there."""
    def append(self, parent: int, body: Body, by: Author, now: int) -> int:
        """Append a node under `parent` without moving any cursor."""
    def append_root(self, config: Config, by: Author, now: int) -> int:
        """Append a root `Config` (a detached tree) without moving any cursor."""
    def set_label(self, label: str, cursor: int) -> None:
        """Put `label` on `cursor`; a label already on another cursor moves. `CursorClosed` if the
        cursor is closed.
        """
    def remove_label(self, label: str) -> None:
        """Remove `label`. `NoSuchLabel` if it is not set."""
    def hook_move(self, cursor: int, to: int, by: Author, now: int) -> None:
        """A cursor hook's move of its own cursor to node `to`. `NotInHook` unless a cursor hook
        job is out for `cursor`.
        """
    def hook_write(
        self, cursor: int, body: Body, delivers: Sequence[int], by: Author, now: int
    ) -> int:
        """A cursor hook's write: append a node under the cursor's node, move onto it and consume
        the `delivers` items. Returns the new node's id. `NotInHook` unless a cursor hook job is
        out for `cursor`; `NotPending` if an item is not pending for it.
        """
    def hook_set_auto(self, cursor: int, on: bool) -> None:
        """A cursor hook's change of its own cursor's auto-step. `NotInHook` unless a cursor hook
        job is out for `cursor`.
        """
    def next_job(self, cursor: int) -> Job:
        """The cursor's next job; `Job.Rest` when it has no step."""
    def answer(self, cursor: int, answer: Answer, now: int) -> None:
        """Answer the job last handed out for `cursor`."""
    def cursors(self) -> list[int]:
        """Every open cursor, by id."""
    def closed(self) -> list[tuple[int, int]]:
        """Each closed cursor with its last node, in id order."""
    def cursor(self, cursor: int) -> CursorState:
        """One cursor as it stands, all read under one lock."""
    def labels(self) -> list[tuple[str, int]]:
        """Every label with its cursor, in string order."""
    def label(self, label: str) -> int:
        """The cursor `label` is on."""
    def take_notices(self) -> list[tuple[str, str]]:
        """Drain the `(client, text)` messages to clients, oldest first."""

# The base tools (crates/tau-tools). `run` and `wait` release the GIL.
# `tau._base_tools` wraps these as `Tool`s.
# Defaults shown as `...` are constants in the Rust signature: max_output 10_000,
# max_overflow 64 MiB, timeout_ms 30_000, max_timeout_ms 600_000, kill_grace_ms 2_000.

@final
class Cancel:
    """A flag that stops a running `read`, `grep` or `find`."""

    def __new__(cls) -> Cancel: ...
    def cancel(self) -> None:
        """Set the flag; the call it was given to stops with an `Err`."""

@final
class Read:
    """The `read` tool."""

    def __new__(cls, cwd: str | PathLike[str], max_output: int) -> Read: ...
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters_json(self) -> str: ...
    def run(self, args_raw: str, cancel: Cancel) -> Outcome:
        """Run the call; releases the GIL."""

@final
class Write:
    """The `write` tool."""

    def __new__(cls, cwd: str | PathLike[str]) -> Write: ...
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters_json(self) -> str: ...
    def run(self, args_raw: str) -> Outcome:
        """Run the call; releases the GIL."""

@final
class Edit:
    """The `edit` tool."""

    def __new__(
        cls,
        cwd: str | PathLike[str],
        max_output: int,
        overflow_dir: str | PathLike[str] | None,
        max_overflow: int,
    ) -> Edit: ...
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters_json(self) -> str: ...
    def run(self, args_raw: str) -> Outcome:
        """Run the call; releases the GIL."""

@final
class Grep:
    """The `grep` tool."""

    def __new__(
        cls,
        cwd: str | PathLike[str],
        max_output: int,
        overflow_dir: str | PathLike[str] | None,
        max_overflow: int,
    ) -> Grep: ...
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters_json(self) -> str: ...
    def run(self, args_raw: str, cancel: Cancel) -> Outcome:
        """Run the call; releases the GIL."""

@final
class Find:
    """The `find` tool."""

    def __new__(
        cls,
        cwd: str | PathLike[str],
        max_output: int,
        overflow_dir: str | PathLike[str] | None,
        max_overflow: int,
    ) -> Find: ...
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters_json(self) -> str: ...
    def run(self, args_raw: str, cancel: Cancel) -> Outcome:
        """Run the call; releases the GIL."""

@final
class Bash:
    """The `bash` tool."""

    def __new__(
        cls,
        cwd: str | PathLike[str],
        max_output: int,
        overflow_dir: str | PathLike[str] | None,
        max_overflow: int,
        timeout_ms: int,
        max_timeout_ms: int,
        kill_grace_ms: int,
    ) -> Bash: ...
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters_json(self) -> str: ...
    def start(self, args_raw: str) -> BashRun:
        """Start the command. Never raises: a bad call is a `BashRun` whose `wait` answers `Err`."""

@final
class BashRun:
    """A started command, killable from any thread."""

    def wait(self, progress: Callable[[str], None] | None = None) -> Outcome:
        """Block, GIL released, until the command ends, is killed, or times out.

        With `progress`, a callable of one `str`, the output is also passed to it as it
        arrives, coalesced to at most one call per 100 ms, from a thread of its own (so the
        callable must be safe to call from any thread). The GIL is taken only for each call,
        never in the loop that reads the command's output. A slow `progress` never slows the
        command, but `wait` returns only after the call running and the final flush, so it
        delays the answer. Calls stop for good once the interpreter begins to exit.
        """
    def kill(self) -> None:
        """Ask for the command and its process group to end (SIGTERM, then SIGKILL
        after the grace period). Does not block; safe at any time, any number of times.
        """

def wire_encode(value: Node | Body | Author | Item | ItemData | Ending) -> str:
    """The JSON text of a core value: a `Node`, `Body`, `Author`, `Item`, `ItemData` or `Ending`.
    `TypeError` for anything else.
    """

def wire_decode(kind: str, text: str) -> Any:
    """The value of class `kind` that `text` (from `wire_encode`) encodes. `ValueError` if `text`
    is not one, `TypeError` for a `kind` that is not a class that is encoded.
    """

def wire_schema() -> str:
    """The JSON Schema `$defs` of the encoded classes and everything they hold, as JSON object text,
    one definition per type name. Their references are `#/$defs/<name>`.
    """
