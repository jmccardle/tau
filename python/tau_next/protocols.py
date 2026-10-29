"""What the driver asks of providers, tools, extensions, and the clock."""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Protocol

from tau_next._core import Config, Node, Part, Stop
from tau_next.handles import CallDraft, Cursor, NodeHandle, Subject


class SampleError(Exception):
    """A sample that failed: an HTTP error, a reset connection, a conversion error."""


class Provider(Protocol):
    """One API family. Owes the driver `model.md` section 10 (the parts of it built so far)."""

    def sample(self, walk: Sequence[Node], config: Config) -> AsyncIterator[Part | Stop]:
        """Yield the reply's parts in order, then its stop, then end.

        Raise `SampleError` to fail; parts yielded before it are kept.
        """
        ...


class Tool(Protocol):
    @property
    def name(self) -> str: ...

    async def call(self, args: Mapping[str, object]) -> str:
        """Run with parsed arguments. A raised exception is the call's `Err`."""
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

    async def sleep_until(self, t: int) -> None: ...
