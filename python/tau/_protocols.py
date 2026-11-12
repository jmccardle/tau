"""What the driver asks of providers, tools, extensions, and the clock."""

import inspect
import json
import re
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from tau._config import ModelConfig
from tau._core import Node, Outcome, Part, Stop
from tau._counts import Counter
from tau._events import Delta
from tau._handles import Cursor, NodeHandle, Subject
from tau._values import DefinitionError


@dataclass(frozen=True)
class Finish:
    """The end of a reply: the normalised `stop`, and the provider's own finish string
    (`None` if it has none to report), and `meta`: what the response carried besides its
    parts, as the text of a JSON object (`None` if it carried nothing). Anything else raises
    `ValueError` here."""

    stop: Stop
    reason: str | None = None
    meta: str | None = None

    def __post_init__(self) -> None:
        check_meta(self.meta)


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def check_meta(meta: object) -> None:
    """`meta` is `None` or the text of a strict JSON object. Anything else fails here, not in
    the binding: `TypeError` for a non-`str`, `ValueError` for text that is not a JSON object
    (`NaN` and `Infinity` are not JSON)."""
    if meta is None:
        return
    if not isinstance(meta, str):
        raise TypeError(f"meta must be JSON object text (str) or None, got {type(meta).__name__}")
    try:
        ok = isinstance(json.loads(meta, parse_constant=_no_constant), dict)
    except ValueError as e:
        raise ValueError(f"meta must be the text of a JSON object, got {meta!r}: {e}") from e
    if not ok:
        raise ValueError(f"meta must be the text of a JSON object, got {meta!r}")


def replay_for(part: Part, provider: str) -> dict[str, object] | None:
    """The replay data `provider` may send back for `part`, parsed: the JSON object in
    `part.replay.data` when the part carries replay tagged with `provider`; `None` for a part
    with none, or with another provider's, which the caller treats as unsigned (`model.md`
    10.4). The exact text is `part.replay.data`: string values parse back exactly, but numbers
    and escapes may not re-serialize to the same bytes. Raises `TypeError` for a non-`Part`."""
    if not isinstance(part, Part.Text | Part.Thinking | Part.Call):
        raise TypeError(f"replay_for takes a Part, got {part!r}")
    replay = part.replay
    if replay is None or replay.provider != provider:
        return None
    return cast("dict[str, object]", json.loads(replay.data))


class SampleError(Exception):
    """A sample that failed: an HTTP error, a reset connection, a conversion error. `meta` is
    what the response had already said about itself (as on `Finish`), kept on the failed reply.
    `status` is the HTTP status of an error response (`None` for a failure with none, such as a
    refused connection); `retry_after` is the seconds the server asked to wait (`None` if it
    did not say). A retrying wrapper provider reads both (D15)."""

    def __init__(
        self,
        message: str = "",
        meta: str | None = None,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        check_meta(meta)
        self.meta = meta
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Rendered:
    """A request as a provider would send it, from `Provider.render` (D26 section 3.1): no
    network, the same body `sample` posts. `url` is where it goes and `body` its JSON body.
    `messages` is each message of its conversation as the text it is sent as, in order, and
    `nodes` the ids of the nodes each one came from (`messages[i]` renders `nodes[i]`). `fixed`
    is the rest as one text: the URL and everything in the body but the conversation (the
    model, the system text, the tools, the options). Request A is a **byte prefix** of request
    B when their `fixed` are equal and A's `messages` are B's first ones (`extends`)."""

    url: str
    body: Mapping[str, object]
    messages: tuple[str, ...]
    nodes: tuple[tuple[int, ...], ...]
    fixed: str

    def extends(self, earlier: "Rendered") -> bool:
        """Whether `earlier` is a byte prefix of this request."""
        return (
            self.fixed == earlier.fixed
            and self.messages[: len(earlier.messages)] == earlier.messages
        )


class ProviderClosed(SampleError):
    """A sample on a provider whose `aclose()` has been called, or one cut off by it. Not a
    transient failure: a retry cannot succeed, so a retrying wrapper passes it on."""


TOOL_KEYWORDS = ("progress", "call_id")
"""The optional keyword parameters a `Tool`'s `call` opts in to by naming them."""


def check_tools(tools: Sequence[object]) -> tuple[dict[str, "Tool"], dict[str, frozenset[str]]]:
    """The tools keyed by name, each checked against the `Tool` contract, and for each that takes
    any the optional keywords it takes (`tool_keywords`); raises `DefinitionError` naming the
    tool for a missing or wrong attribute, a duplicate name or a keyword that cannot be
    passed by keyword."""
    out: dict[str, Tool] = {}
    keywords: dict[str, frozenset[str]] = {}
    for i, candidate in enumerate(tools):
        name: object = getattr(candidate, "name", None)
        if not isinstance(name, str) or not name:
            raise DefinitionError(f"tool {i} ({candidate!r}): `name` must be a non-empty str")
        if not isinstance(getattr(candidate, "description", None), str):
            raise DefinitionError(f"tool {name!r}: `description` must be a str")
        if not isinstance(getattr(candidate, "parameters", None), Mapping):
            raise DefinitionError(f"tool {name!r}: `parameters` must be a Mapping")
        if not callable(getattr(candidate, "call", None)):
            raise DefinitionError(f"tool {name!r}: `call` must be callable")
        if name in out:
            raise DefinitionError(f"two tools are named {name!r}")
        taken = tool_keywords(candidate)  # a malformed keyword fails here, not at the first call
        if taken:
            keywords[name] = taken
        out[name] = cast("Tool", candidate)
    return out, keywords


def tool_keywords(tool: object) -> frozenset[str]:
    """The optional keywords `tool.call` opts in to: those of `TOOL_KEYWORDS` it has a parameter
    named, which the driver then passes by keyword. One that cannot be passed by keyword
    (positional-only) raises `DefinitionError`. A `call` with no inspectable signature has
    opted in to none."""
    try:
        parameters = inspect.signature(getattr(tool, "call")).parameters  # noqa: B009
    except (ValueError, TypeError):
        return frozenset()
    taken: set[str] = set()
    for keyword in TOOL_KEYWORDS:
        param = parameters.get(keyword)
        if param is None:
            continue
        if param.kind is param.POSITIONAL_ONLY:
            raise DefinitionError(
                f"tool {getattr(tool, 'name', tool)!r}: `{keyword}` must be passable by keyword"
            )
        taken.add(keyword)
    return frozenset(taken)


class Provider(Protocol):
    """One API family, long-lived: the program that builds it closes it (D15). Owes the
    driver `model.md` section 10 (the parts of it built so far).

    A provider **may** also have `render(walk, model, tools) -> Rendered`: the request `sample`
    would send for the same arguments, without sending it or reading a key (D26 section 3.1).
    One that has it stamps each reply's `meta.sent` with what it sent, which is what proves a
    span on a server whose cache report cannot. `OpenAIProvider` and `GeminiProvider` have it."""

    @property
    def name(self) -> str:
        """What a model's `provider` setting calls it."""
        ...

    def check(self, model: ModelConfig) -> list[str]:
        """Problems with the model's settings as this provider reads them: a missing
        `base_url` it needs, one it has no use for, an unknown or ill-typed option, a model
        whose `provider` is not its own. Empty if the model is fine. `tau config check`
        calls it for every model that names the provider."""
        ...

    def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence["Tool"]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        """`model` is the session's named model, resolved by the driver at this sample. Yield,
        interleaved: deltas as text arrives (`TextDelta`, `ThinkingDelta`, `CallNamed` then
        `CallArgs`), each whole part as it finishes, then the `Finish`, then end. The driver
        saves only the parts and forwards the deltas. A part is yielded before the first delta
        of the next part; a delta that breaks this fails the sample. `tools` are the driver's,
        offered to the model for this sample only; they are never recorded.

        `sample` assumes `check(model)` returned no problems: the driver calls it first, for
        every sample, and fails the sample with the problems listed if it returns any, so
        `sample` does not re-check. It closes the generator when it stops reading.

        Raise `SampleError` to fail; parts yielded before it are kept. Any other `Exception`
        (from `check` or from `sample`) fails this sample the same way, with its message as
        `Type: message` and `internal: true` in `meta.error`, and its traceback on stderr: a
        bug in a provider costs one sample, never the driver. If the sample is cancelled or
        fails, the driver saves the text or thinking whose deltas arrived but whose part never
        did. A part yielded while deltas are open must be the part
        those deltas were building (same kind, same text), and `Finish` may not come with deltas
        open.
        """
        ...

    async def aclose(self) -> None:
        """Close what the provider holds open (connections)."""
        ...


Progress = Callable[[str], None]
"""A running tool's way to show partial output: `progress(text)` with the new output since the
last call. Safe to call from any thread. Once the tool's call is over (it returned, was
aborted, or was cancelled) calling it does nothing."""


class Tool(Protocol):
    """What the driver and providers depend on. A plain function is described as one
    (`tau._tools`); any object with these attributes works too.

    Any object with these attributes works, whatever it is written in. The tool
    owns argument validation: it gets the call's raw JSON text.

    A tool that shows partial output as it runs opts in by giving `call` a parameter named
    `progress` (`call(self, args_raw, *, progress=None)`; the `None` default keeps the tool a
    `Tool` and callable without a driver), detected once when the driver is built.
    The driver then passes a `Progress` by keyword. The same opt-in, a parameter named `call_id`
    (`call(self, args_raw, *, call_id=None)`), has the driver pass the call's id: the `id` of the
    `Part.Call` the model made, as in `CallDraft.id` and `ToolProgress.call_id`, which a tool
    running elsewhere can use to tell its calls apart. A keyword that cannot be passed by
    keyword (positional-only) is a `DefinitionError` when the driver is built. A tool whose
    `call` has neither is called with `args_raw` alone, as before. (A tool made from a function
    takes `Progress` by annotation; it has no call id to take.)
    """

    @property
    def name(self) -> str:
        """The name the model calls the tool by; unique among a driver's tools."""
        ...

    @property
    def description(self) -> str:
        """What the tool does, as the model reads it."""
        ...

    @property
    def parameters(self) -> Mapping[str, object]:
        """A JSON Schema object describing the arguments."""
        ...

    async def call(self, args_raw: str) -> Outcome:
        """Run with the call's `args_raw`. Answer `Ok` or `Err`; an exception raised is
        the call's `Err(str(e))`."""
        ...


class ProgressTool(Protocol):
    """The `call` of a `Tool` that opted in to progress (see `Tool`)."""

    async def call(self, args_raw: str, *, progress: Progress) -> Outcome: ...


class CallIdTool(Protocol):
    """The `call` of a `Tool` that opted in to its call id (see `Tool`)."""

    async def call(self, args_raw: str, *, call_id: str) -> Outcome: ...


CURSOR_POINTS = (
    "pre_step",
    "pre_input",
    "pre_sample",
    "pre_reply",
    "post_reply",
    "post_result",
    "post_step",
)
"""The names of the cursor hook points, in the order a step reaches them."""
TOOL_POINTS = ("pre_tool", "pre_result")
"""The names of the tool hook points."""


class CursorHook(Protocol):
    """A cursor hook: an extension's method named for one of `CURSOR_POINTS`. A string it
    returns halts the later hooks of the point; `None` goes on."""

    async def __call__(self, cursor: Cursor, subject: Subject, /) -> str | None: ...


class ToolHook(Protocol):
    """A tool hook: an extension's method named for one of `TOOL_POINTS`. A string it returns
    becomes the call's `Err` and halts the later hooks of the point; `None` goes on."""

    async def __call__(self, node: NodeHandle, call: "CallDraft", /) -> str | None: ...


class CallDraft:
    """The call a tool hook works on. `id` and `tool` are read-only: the call is already
    named, and an edit to either would be ignored. `args_raw` and `outcome` may be set.

    Setting `outcome` in `pre_tool` answers the call (the tool does not run, and later
    `pre_tool` hooks still see it and may change it); in `pre_result` it edits the outcome.
    """

    __slots__ = ("_id", "_tool", "args_raw", "outcome")

    def __init__(self, id: str, tool: str, args_raw: str, outcome: Outcome | None = None) -> None:
        self._id = id
        self._tool = tool
        self.args_raw = args_raw
        self.outcome = outcome

    @property
    def id(self) -> str:
        """The provider's id for the call. Read-only."""
        return self._id

    @property
    def tool(self) -> str:
        """The name of the tool being called. Read-only."""
        return self._tool

    def __repr__(self) -> str:
        return (
            f"CallDraft(id={self._id!r}, tool={self._tool!r}, "
            f"args_raw={self.args_raw!r}, outcome={self.outcome!r})"
        )


class Extension(Protocol):
    """Hooks are methods named for their hook point, found by name and all optional.

    Cursor hooks (`CursorHook`): `pre_step`, `pre_input`, `pre_sample`,
    `pre_reply`, `post_reply`, `post_result`, `post_step`. Tool hooks
    (`ToolHook`): `pre_tool`, `pre_result`. `name` is non-empty and unique among a
    driver's extensions; any attribute named `pre_*` or `post_*` that is not a hook point is
    refused when the driver is built (`DefinitionError`), so a misspelt hook cannot
    sit silent. That covers data too: a dataclass field `pre_prompt` is refused. Name such a
    field for what it holds (`prompt_prefix`, `before_prompt`), never with those two prefixes.

    Optional attributes besides the hooks: `tools`, `commands`, `options`, `yields_names`, and
    `rescan` (`Rescanning`).
    """

    @property
    def name(self) -> str:
        """The extension's name: letters, digits, `_` and `-`, unique among a driver's
        extensions."""
        ...


@runtime_checkable
class Rescanning(Protocol):
    """An extension whose commands come from files lists `rescan`: the driver calls it before it
    lists or runs a command, and it returns whether its `commands` changed (it has made them
    again, and the driver runs the new ones). It should be cheap when nothing changed: compare
    what a stat says, never read every file each time."""

    def rescan(self) -> bool:
        """Read the files again if they changed since the last call; whether `commands` did."""
        ...


EXTENSION_NAME = re.compile(r"[A-Za-z0-9_-]+")
"""What an extension's name, and a source's key, must match (D19 section 1)."""


def check_extensions(extensions: Sequence[object]) -> tuple[Extension, ...]:
    """The extensions, in order, each checked against the `Extension` contract (a `name` of
    letters, digits, `_` and `-`, unique; hooks that are points); raises `DefinitionError` naming
    the extension."""
    names: set[str] = set()
    counters: list[str] = []
    for i, candidate in enumerate(extensions):
        name: object = getattr(candidate, "name", None)
        if not isinstance(name, str) or not name:
            raise DefinitionError(f"extension {i} ({candidate!r}): `name` must be a non-empty str")
        if not EXTENSION_NAME.fullmatch(name):
            raise DefinitionError(
                f"extension {name!r}: a name is letters, digits, `_` and `-`: a `:` would break "
                "`/extension:command`, a `.` `tau config set EXTENSION.FIELD=V`, and a space a "
                "command line"
            )
        if name in names:
            raise DefinitionError(f"two extensions are named {name!r}")
        names.add(name)
        offered: object = getattr(candidate, "counter", None)
        if offered is not None:
            for method in ("count(job)", "observe(observation)"):
                if not callable(getattr(offered, method.partition("(")[0], None)):
                    raise DefinitionError(
                        f"extension {name!r}: `counter` must have a callable `{method}` method"
                    )
            counters.append(name)
            if len(counters) > 1:
                raise DefinitionError(
                    f"two extensions offer a counter: {counters[0]!r} and {name!r}"
                )
        for attr in dir(candidate):
            if not attr.startswith(("pre_", "post_")):
                continue
            if attr not in (*CURSOR_POINTS, *TOOL_POINTS):
                raise DefinitionError(
                    f"extension {name!r}: `{attr}` is not a hook point "
                    f"(the points are {', '.join((*CURSOR_POINTS, *TOOL_POINTS))})"
                )
            if not callable(getattr(candidate, attr)):
                raise DefinitionError(f"extension {name!r}: hook `{attr}` must be callable")
    return tuple(cast("Extension", e) for e in extensions)


def counter_of(extensions: Sequence[object]) -> tuple[str, Counter] | None:
    """The extension offering the counter (`check_extensions` allows one), as `(its name, the
    counter)`, or `None`."""
    for extension in extensions:
        offered: object = getattr(extension, "counter", None)
        if offered is not None:
            return cast("Extension", extension).name, cast("Counter", offered)
    return None


class Clock(Protocol):
    """What the driver reads time through, so a test can move time itself. `SystemClock` is the
    real one."""

    def now(self) -> int:
        """The current time in milliseconds. It stamps the nodes the driver writes."""
        ...
