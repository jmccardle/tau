"""What the driver asks of providers, tools, extensions, and the clock."""

import json
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from tau_next._config import ModelConfig
from tau_next._core import Node, Outcome, Part, Stop
from tau_next.events import Delta
from tau_next.handles import CallDraft, Cursor, NodeHandle, Subject


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
    """The replay data `provider` may send back for `part`: the parsed JSON object when the
    part carries replay tagged with `provider`; `None` for a part with none, or with another
    provider's, which the caller treats as unsigned (`model.md` 10.4)."""
    replay = part.replay if isinstance(part, Part.Text | Part.Thinking | Part.Call) else None
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


class ProviderClosed(SampleError):
    """A sample on a provider whose `aclose()` has been called, or one cut off by it. Not a
    transient failure: a retry cannot succeed, so a retrying wrapper passes it on."""


class ToolContractError(TypeError):
    """An object given to the driver as a tool that does not satisfy `Tool`, or a second tool
    with a name already taken. The message names the tool."""


def check_tools(tools: Sequence[object]) -> dict[str, "Tool"]:
    """The tools keyed by name, each checked against the `Tool` contract; raises
    `ToolContractError` naming the tool for a missing or wrong attribute or a duplicate name."""
    out: dict[str, Tool] = {}
    for i, candidate in enumerate(tools):
        name: object = getattr(candidate, "name", None)
        if not isinstance(name, str) or not name:
            raise ToolContractError(f"tool {i} ({candidate!r}): `name` must be a non-empty str")
        if not isinstance(getattr(candidate, "description", None), str):
            raise ToolContractError(f"tool {name!r}: `description` must be a str")
        if not isinstance(getattr(candidate, "parameters", None), Mapping):
            raise ToolContractError(f"tool {name!r}: `parameters` must be a Mapping")
        if not callable(getattr(candidate, "call", None)):
            raise ToolContractError(f"tool {name!r}: `call` must be callable")
        if name in out:
            raise ToolContractError(f"two tools are named {name!r}")
        out[name] = cast("Tool", candidate)
    return out


class Provider(Protocol):
    """One API family, long-lived: the program that builds it closes it (D15). Owes the
    driver `model.md` section 10 (the parts of it built so far)."""

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

        The driver calls `check(model)` first and fails the sample, with the problems listed,
        if it returns any; `sample` may rely on a model that passed. It closes the generator
        when it stops reading.

        Raise `SampleError` to fail; parts yielded before it are kept. If the sample is
        cancelled or fails, the driver saves the text or thinking whose deltas arrived but
        whose part never did. A part yielded while deltas are open must be the part those deltas
        were building (same kind, same text), and `Finish` may not come with deltas open.
        """
        ...

    async def aclose(self) -> None:
        """Close what the provider holds open (connections)."""
        ...


class Tool(Protocol):
    """What the driver and providers depend on; `@tool` is one way to build one.

    Any object with these attributes works, whatever it is written in. The tool
    owns argument validation: it gets the call's raw JSON text.
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> Mapping[str, object]:
        """A JSON Schema object describing the arguments."""
        ...

    async def call(self, args_raw: str) -> Outcome:
        """Run with the call's `args_raw`. Answer `Ok` or `Err`; an exception raised is
        the call's `Err(str(e))`."""
        ...


class CursorHook(Protocol):
    async def __call__(self, cursor: Cursor, subject: Subject, /) -> str | None: ...


class ToolHook(Protocol):
    async def __call__(self, node: NodeHandle, call: CallDraft, /) -> str | None: ...


class Extension(Protocol):
    """Hooks are methods named for their hook point, found by name and all optional.

    Cursor hooks (`CursorHook`): `pre_step`, `pre_input`, `pre_sample`,
    `pre_reply`, `post_reply`, `post_result`, `post_step`. Tool hooks
    (`ToolHook`): `pre_tool`, `pre_result`.
    """

    @property
    def name(self) -> str: ...


class Clock(Protocol):
    def now(self) -> int:
        """Milliseconds."""
        ...
