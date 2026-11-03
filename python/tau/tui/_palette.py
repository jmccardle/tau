"""The command palette (Ctrl+P, D23 section 2): what the app does that has no key of its own.

Textual's own palette, with `TauCommands` in place of its system commands (no screenshots, no key
help): `Theme` picks a theme and saves it, `Sessions` and `New session` are Ctrl+O and Ctrl+N,
`Tree` is Ctrl+G, `Quit` leaves. Each is a move (`TauApp.action_move`), so the palette and the keys
do the same thing.
"""

from collections.abc import Callable
from typing import Protocol, cast

from textual.command import DiscoveryHit, Hit, Hits, Provider

_COMMANDS = (
    ("Theme", "Pick a theme and keep it for every session", "theme"),
    ("Tree", "Browse the session's tree (Ctrl+G)", "tree"),
    ("Sessions", "Switch to another session (Ctrl+O)", "sessions"),
    ("New session", "Start a new session (Ctrl+N)", "new"),
)


class Moving(Protocol):
    """The app the palette belongs to."""

    def action_move(self, what: str) -> None: ...


class TauCommands(Provider):
    """The palette's commands."""

    async def discover(self) -> Hits:
        """Every command, before anything is typed."""
        for name, help_text, move in _COMMANDS:
            yield DiscoveryHit(name, self._run(move), help=help_text)
        yield DiscoveryHit("Quit", self.app.action_quit, help="Leave tau (Ctrl+Q)")

    async def search(self, query: str) -> Hits:
        """The commands that match `query`, best first."""
        matcher = self.matcher(query)
        commands: list[tuple[str, str, Callable[[], object]]] = [
            (n, h, self._run(m)) for n, h, m in _COMMANDS
        ]
        commands.append(("Quit", "Leave tau (Ctrl+Q)", self.app.action_quit))
        for name, help_text, call in commands:
            if (score := matcher.match(name)) > 0:
                yield Hit(score, matcher.highlight(name), call, help=help_text)

    def _run(self, move: str) -> Callable[[], None]:
        app = cast("Moving", self.app)
        return lambda: app.action_move(move)
