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
    "Ending",
    "Exists",
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
    """Python view of [`tree::Stop`]."""

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
    """Python view of [`step::Hook`]: the seven cursor hook points."""

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
    """Python view of [`tree::Next`]."""

    Wait: ClassVar[Next]
    Sample: ClassVar[Next]
    RunTools: ClassVar[Next]
    def __int__(self) -> int: ...
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Config:
    """Python view of [`tree::Config`]."""

    def __new__(cls, data: str) -> Config: ...
    @property
    def data(self) -> str: ...
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
    def id(self) -> int: ...
    @property
    def parent(self) -> int | None: ...
    @property
    def time(self) -> int: ...
    @property
    def by(self) -> Author: ...
    @property
    def body(self) -> Body: ...
    @property
    def delivers(self) -> list[int]: ...
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Move:
    """Python view of [`tree::Move`]. `from` is a Python keyword, so `from_`."""

    @property
    def cursor(self) -> int: ...
    @property
    def from_(self) -> int | None: ...
    @property
    def to(self) -> int: ...
    @property
    def by(self) -> Author: ...
    @property
    def time(self) -> int: ...
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Item:
    """Python view of [`tree::Item`]."""

    @property
    def id(self) -> int: ...
    @property
    def to(self) -> int: ...
    @property
    def data(self) -> ItemData: ...
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Draft:
    """Python view of [`step::Draft`]."""

    def __new__(cls, body: Body, delivers: Sequence[int], by: Author) -> Draft: ...
    @property
    def body(self) -> Body: ...
    @property
    def delivers(self) -> list[int]: ...
    @property
    def by(self) -> Author: ...
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class RunCall:
    """Python view of [`step::RunCall`]."""

    @property
    def id(self) -> str: ...
    @property
    def tool(self) -> str: ...
    @property
    def args_raw(self) -> str: ...
    def __hash__(self) -> int: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...

@final
class Replay:
    """Python view of [`tree::Replay`]. Construction validates, so a `Part` never holds a bad one."""

    def __new__(cls, provider: str, data: str) -> Replay: ...
    @property
    def provider(self) -> str: ...
    @property
    def data(self) -> str: ...
    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

@disjoint_base
class Part:
    """Python view of [`tree::Part`]."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Text(Part):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, text: str, replay: Replay | None = None) -> Part.Text: ...
        @property
        def text(self) -> str: ...
        @property
        def replay(self) -> Replay | None: ...

    @final
    class Thinking(Part):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, text: str, replay: Replay | None = None) -> Part.Thinking: ...
        @property
        def text(self) -> str: ...
        @property
        def replay(self) -> Replay | None: ...

    @final
    class Call(Part):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(
            cls, id: str, tool: str, args_raw: str, replay: Replay | None = None
        ) -> Part.Call: ...
        @property
        def id(self) -> str: ...
        @property
        def tool(self) -> str: ...
        @property
        def args_raw(self) -> str: ...
        @property
        def replay(self) -> Replay | None: ...

@disjoint_base
class Outcome:
    """Python view of [`tree::Outcome`]."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Ok(Outcome):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, value: str) -> Outcome.Ok: ...
        @property
        def value(self) -> str: ...

    @final
    class Err(Outcome):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, error: str) -> Outcome.Err: ...
        @property
        def error(self) -> str: ...

    @final
    class Interrupted(Outcome):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Outcome.Interrupted: ...

@disjoint_base
class Author:
    """Python view of [`tree::Author`]."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Frontend(Author):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, client: str) -> Author.Frontend: ...
        @property
        def client(self) -> str: ...

    @final
    class Provider(Author):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Author.Provider: ...

    @final
    class Tools(Author):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Author.Tools: ...

    @final
    class Core(Author):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Author.Core: ...

    @final
    class Extension(Author):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, name: str) -> Author.Extension: ...
        @property
        def name(self) -> str: ...

@disjoint_base
class Body:
    """Python view of [`tree::Body`]."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class System(Body):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, text: str) -> Body.System: ...
        @property
        def text(self) -> str: ...

    @final
    class User(Body):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, text: str) -> Body.User: ...
        @property
        def text(self) -> str: ...

    @final
    class Assistant(Body):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(
            cls,
            parts: Sequence[Part],
            stop: Stop,
            finish_reason: str | None = None,
            meta: str | None = None,
        ) -> Body.Assistant: ...
        @property
        def parts(self) -> list[Part]: ...
        @property
        def stop(self) -> Stop: ...
        @property
        def finish_reason(self) -> str | None: ...
        @property
        def meta(self) -> str | None: ...

    @final
    class Results(Body):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, outcomes: Sequence[tuple[str, Outcome, Author]]) -> Body.Results: ...
        @property
        def outcomes(self) -> list[tuple[str, Outcome, Author]]: ...

    @final
    class Config(Body):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, config: Config) -> Body.Config: ...
        @property
        def config(self) -> Config: ...

    @final
    class Custom(Body):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, kind: str, data: str) -> Body.Custom: ...
        @property
        def kind(self) -> str: ...
        @property
        def data(self) -> str: ...

@disjoint_base
class ItemData:
    """Python view of [`tree::ItemData`]."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Input(ItemData):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, by: Author, text: str) -> ItemData.Input: ...
        @property
        def by(self) -> Author: ...
        @property
        def text(self) -> str: ...

    @final
    class Extension(ItemData):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, name: str, data: str) -> ItemData.Extension: ...
        @property
        def name(self) -> str: ...
        @property
        def data(self) -> str: ...

@disjoint_base
class Ending:
    """Python view of [`step::Ending`]."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Done(Ending):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Ending.Done: ...

    @final
    class Halted(Ending):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, reason: str) -> Ending.Halted: ...
        @property
        def reason(self) -> str: ...

    @final
    class Failed(Ending):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, error: str) -> Ending.Failed: ...
        @property
        def error(self) -> str: ...

    @final
    class Aborted(Ending):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Ending.Aborted: ...

@disjoint_base
class Job:
    """Python view of [`step::Job`]."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Hooks(Job):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(
            cls, point: Hook, draft: Draft | None, node: int | None, ending: Ending | None
        ) -> Job.Hooks: ...
        @property
        def point(self) -> Hook: ...
        @property
        def draft(self) -> Draft | None: ...
        @property
        def node(self) -> int | None: ...
        @property
        def ending(self) -> Ending | None: ...

    @final
    class Sample(Job):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Job.Sample: ...

    @final
    class RunCalls(Job):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, node: int, calls: Sequence[RunCall]) -> Job.RunCalls: ...
        @property
        def node(self) -> int: ...
        @property
        def calls(self) -> list[RunCall]: ...

    @final
    class Pending(Job):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Job.Pending: ...

    @final
    class Rest(Job):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> Job.Rest: ...

@disjoint_base
class SampleEnd:
    """Python view of [`step::SampleEnd`]: how a sample ended."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Finished(SampleEnd):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(
            cls, stop: Stop, reason: str | None = None, meta: str | None = None
        ) -> SampleEnd.Finished: ...
        @property
        def stop(self) -> Stop: ...
        @property
        def reason(self) -> str | None: ...
        @property
        def meta(self) -> str | None: ...

    @final
    class Failed(SampleEnd):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, error: str, meta: str | None = None) -> SampleEnd.Failed: ...
        @property
        def error(self) -> str: ...
        @property
        def meta(self) -> str | None: ...

    @final
    class Cancelled(SampleEnd):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls) -> SampleEnd.Cancelled: ...

@disjoint_base
class Answer:
    """Python view of [`step::Answer`]."""

    def __eq__(self, other: object, /) -> bool: ...
    def __repr__(self) -> str: ...
    def __hash__(self) -> int: ...

    @final
    class Hooks(Answer):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, stop: str | None = None, draft: Draft | None = None) -> Answer.Hooks: ...
        @property
        def stop(self) -> str | None: ...
        @property
        def draft(self) -> Draft | None: ...

    @final
    class HookFailed(Answer):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, error: str) -> Answer.HookFailed: ...
        @property
        def error(self) -> str: ...

    @final
    class Sampled(Answer):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, parts: Sequence[Part], end: SampleEnd) -> Answer.Sampled: ...
        @property
        def parts(self) -> list[Part]: ...
        @property
        def end(self) -> SampleEnd: ...

    @final
    class CallDone(Answer):
        __match_args__: ClassVar[tuple[str, ...]]
        def __new__(cls, call: str, outcome: Outcome, by: Author) -> Answer.CallDone: ...
        @property
        def call(self) -> str: ...
        @property
        def outcome(self) -> Outcome: ...
        @property
        def by(self) -> Author: ...

@final
class Tree:
    """The session's nodes: `Session.tree`. It shares the session's lock, so a
    read is as current as the call that made it, and a closed session raises
    `Closed` here too.
    """

    def node(self, id: int) -> Node: ...
    def walk(self, node: int) -> list[Node]:
        """The nodes root to `node`, inclusive."""
    def children(self, node: int) -> list[int]:
        """The children of `node`, in append order."""
    def __len__(self) -> int: ...
    def config_at(self, node: int) -> Config: ...
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
    def auto(self) -> bool: ...
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
    """One session: its tree, cursors, scratchpad, and every cursor's step.

    `frozen`: the state lives behind a `Mutex`, so the class has no mutable
    Python attributes and is shared across threads on free-threaded builds
    without further locking. Each call that changes the session hands its
    queued records to the store (when there is one) before it returns; Python
    never saves. `tree` is the read view of the nodes; `cursor` snapshots one
    cursor.
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
    def tree(self) -> Tree: ...
    def input(self, cursor: int, by: Author, text: str) -> int: ...
    def step(self, cursor: int) -> None: ...
    def go_to(self, cursor: int, to: int, client: str, now: int) -> bool:
        """Returns whether the driver must cancel the effect in flight."""
    def set_auto(self, cursor: int, on: bool) -> None: ...
    def abort(self, cursor: int) -> bool:
        """Returns whether the driver must cancel the effect in flight."""
    def close_cursor(self, cursor: int) -> None:
        """Close a cursor at rest (`Busy` otherwise): the driver aborts and waits first."""
    def open(self, at: int, by: Author, now: int) -> int: ...
    def add_item(self, to: int, data: ItemData) -> int: ...
    def remove_item(self, id: int) -> None: ...
    def append(self, parent: int, body: Body, by: Author, now: int) -> int:
        """Append a node under `parent` without moving any cursor."""
    def append_root(self, config: Config, by: Author, now: int) -> int:
        """Append a root `Config` (a detached tree) without moving any cursor."""
    def set_label(self, label: str, cursor: int) -> None: ...
    def remove_label(self, label: str) -> None: ...
    def hook_move(self, cursor: int, to: int, by: Author, now: int) -> None: ...
    def hook_write(
        self, cursor: int, body: Body, delivers: Sequence[int], by: Author, now: int
    ) -> int: ...
    def hook_set_auto(self, cursor: int, on: bool) -> None: ...
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
    """A flag that stops a running `read` or `grep`."""

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
