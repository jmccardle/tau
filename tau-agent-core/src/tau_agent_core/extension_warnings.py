"""Extension warnings that stay current until cleared (docs/extensions.md "Warnings").

A notification is gone once shown; a warning stays until its writer clears it
or its extension goes away, so a head or a late client can still read it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from tau_llm.docs import agent_facing

WarningSeverity = Literal["warning", "error"]
"""How bad a warning is."""

WARNING_SEVERITIES: tuple[str, ...] = ("warning", "error")
"""Every :data:`WarningSeverity`, in rising order."""

TAU_SOURCE = "tau"
"""The ``source`` of a warning τ itself writes, such as a loader notice."""


@agent_facing(topic="extensions")
@dataclass(frozen=True)
class ExtensionWarning:
    """One current warning on a session.

    A load failure is mirrored as ``source="tau"``, ``key="load:<label>"``,
    severity ``"error"``, with only the error's first line. The full record is
    in ``load_errors``, so a client that shows those may filter these out by
    that prefix.

    Attributes:
        source: The extension identifier that set it, or ``"tau"``.
        key: Its name within ``source``; setting the same key replaces it.
        message: One line for the user.
        severity: One of :data:`WARNING_SEVERITIES`.
    """

    source: str
    key: str
    message: str
    severity: WarningSeverity

    def summary(self) -> str:
        """The line every head shows for this warning.

        Returns:
            ``"<source>: <message>"``.
        """
        return f"{self.source}: {self.message}"


@agent_facing(topic="extensions")
def warnings_marker(warnings: list[ExtensionWarning]) -> str | None:
    """The persistent one-line marker a head shows while any warning is current.

    Args:
        warnings: The current warnings, as :meth:`AgentSession.extension_warnings` reads them.

    Returns:
        ``"1 extension warning — /extensions"``, or with errors counted first,
        ``"1 extension error, 2 warnings — /extensions"``; ``None`` when empty.
    """
    errors = sum(1 for w in warnings if w.severity == "error")
    plain = len(warnings) - errors
    counts = [
        (n, noun + ("s" if n != 1 else ""))
        for n, noun in ((errors, "error"), (plain, "warning"))
        if n
    ]
    if not counts:
        return None
    parts = [f"{n} {noun}" for n, noun in counts]
    parts[0] = parts[0].replace(" ", " extension ", 1)
    return f"{', '.join(parts)} — /extensions"


WarningsListener = Callable[[list[ExtensionWarning]], None]
"""Called with every current warning after the set changes."""


def validate_warning(warning: ExtensionWarning) -> None:
    """Refuse a warning the store would not hold.

    Args:
        warning: The warning to check.

    Raises:
        ValueError: ``source``, ``key`` or ``message`` is not a string or is
            blank, the message is more than one line, or the severity is unknown.
    """
    for field in ("source", "key", "message"):
        value = getattr(warning, field)
        if not isinstance(value, str):
            raise ValueError(f"an extension warning's {field} must be a string, got {value!r}")
        if not value.strip():
            raise ValueError(f"an extension warning's {field} must not be blank")
    if "\n" in warning.message or "\r" in warning.message:
        raise ValueError(f"an extension warning's message is one line, got {warning.message!r}")
    if warning.severity not in WARNING_SEVERITIES:
        raise ValueError(
            f"severity {warning.severity!r} is not one of {', '.join(WARNING_SEVERITIES)}"
        )


class ExtensionWarnings:
    """A session's current warnings, keyed by ``(source, key)``, in first-set order.

    Not thread-safe: :class:`~tau_agent_core.agent_session.AgentSession` mutates
    it only on its own loop.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], ExtensionWarning] = {}
        self._listeners: list[WarningsListener] = []
        self._batch_depth = 0
        self._batch_start: list[ExtensionWarning] | None = None

    def current(self) -> list[ExtensionWarning]:
        """Every current warning, oldest key first."""
        return list(self._entries.values())

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Hold listener calls until the outermost batch ends, then call them once if the set changed."""
        if self._batch_depth == 0:
            self._batch_start = self.current()
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                start, self._batch_start = self._batch_start, None
                if start != self.current():
                    self._changed()

    def set(self, warning: ExtensionWarning) -> bool:
        """Add ``warning`` or replace the one under its ``(source, key)``.

        Returns:
            Whether anything changed; an identical warning changes nothing.

        Raises:
            ValueError: :func:`validate_warning` refused it.
        """
        validate_warning(warning)
        slot = (warning.source, warning.key)
        if self._entries.get(slot) == warning:
            return False
        self._entries[slot] = warning
        self._changed()
        return True

    def clear(self, source: str, key: str) -> bool:
        """Remove the warning under ``(source, key)``; returns whether there was one."""
        if self._entries.pop((source, key), None) is None:
            return False
        self._changed()
        return True

    def clear_source(self, source: str) -> bool:
        """Remove every warning ``source`` set; returns whether there was any."""
        slots = [slot for slot in self._entries if slot[0] == source]
        for slot in slots:
            del self._entries[slot]
        if slots:
            self._changed()
        return bool(slots)

    def subscribe(self, listener: WarningsListener) -> Callable[[], None]:
        """Call ``listener`` after each change; returns the unsubscribe."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _changed(self) -> None:
        """Tell every listener the new set, unless a batch is holding the calls."""
        if self._batch_depth:
            return
        current = self.current()
        for listener in list(self._listeners):
            listener(current)
