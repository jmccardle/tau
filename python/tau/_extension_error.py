"""`ExtensionLoadError`, in a module of its own so a built-in's loader can raise it."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tau._extensions import ExtensionSource


class ExtensionLoadError(ImportError):
    """One or more extension sources did not load: the message is every failure (what failed,
    its source, the underlying error, and the command that gets past it), and `sources` their
    `ExtensionSource` records. The first underlying exception is `__cause__`."""

    def __init__(self, message: str, sources: Sequence["ExtensionSource"] = ()) -> None:
        super().__init__(message)
        self.sources = tuple(sources)
