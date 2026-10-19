"""Per-session warnings that extensions and τ keep current (docs/extensions.md "Warnings").

A notification is gone once shown; a warning stays until its writer clears it
or the session swaps, so a head or a client attaching later can still read it.
"""

from __future__ import annotations

from collections.abc import Callable
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


class ExtensionWarnings:
    """A session's current warnings, keyed by ``(source, key)``, in first-set order."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], ExtensionWarning] = {}
        self._listeners: list[WarningsListener] = []

    def current(self) -> list[ExtensionWarning]:
        """Every current warning, oldest key first."""
        return list(self._entries.values())

    def set(self, warning: ExtensionWarning) -> bool:
        """Add ``warning`` or replace the one under its ``(source, key)``.

        Returns:
            Whether anything changed; an identical warning changes nothing.

        Raises:
            ValueError: an empty ``source`` or ``key``, a message that is empty
                or more than one line, or an unknown severity.
        """
        if not warning.source or not warning.key:
            raise ValueError("an extension warning needs a non-empty source and key")
        if not warning.message.strip() or "\n" in warning.message or "\r" in warning.message:
            raise ValueError(f"an extension warning is one non-empty line, got {warning.message!r}")
        if warning.severity not in WARNING_SEVERITIES:
            raise ValueError(
                f"severity {warning.severity!r} is not one of {', '.join(WARNING_SEVERITIES)}"
            )
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

    def clear_all(self) -> bool:
        """Remove every warning; returns whether there was any."""
        if not self._entries:
            return False
        self._entries.clear()
        self._changed()
        return True

    def subscribe(self, listener: WarningsListener) -> Callable[[], None]:
        """Call ``listener`` after each change; returns the unsubscribe."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _changed(self) -> None:
        """Tell every listener the new set."""
        current = self.current()
        for listener in list(self._listeners):
            listener(current)
