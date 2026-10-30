"""What the driver asks of providers, tools, extensions, and the clock."""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from tau_next._core import Config, Node, Outcome, Part, Stop
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


class SampleError(Exception):
    """A sample that failed: an HTTP error, a reset connection, a conversion error. `meta` is
    what the response had already said about itself (as on `Finish`), kept on the failed reply."""

    def __init__(self, message: str = "", meta: str | None = None) -> None:
        super().__init__(message)
        check_meta(meta)
        self.meta = meta


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
    """One API family. Owes the driver `model.md` section 10 (the parts of it built so far)."""

    def sample(
        self, walk: Sequence[Node], config: Config, tools: Sequence["Tool"]
    ) -> AsyncIterator[Part | Finish]:
        """Yield the reply's parts in order, then its `Finish`, then end. `tools` are the
        driver's, offered to the model for this sample only; they are never recorded.

        Raise `SampleError` to fail; parts yielded before it are kept.
        """
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
