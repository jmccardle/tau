"""Switching sessions (D23 section 5): the picker before the start (`-r`), and the re-exec.

One process holds one session and one working directory (D18 section 1, D21 section 1), so the
app never opens another session in its own interpreter: Ctrl+O (pick) and Ctrl+N (new) leave the
screen with a `Switch`, the launch closes the session as `Opened.close` does (a session never
used is removed), and `tau.tui._run` replaces the process with `tau` started for the other one.
`switch_argv` is that command line: the flags of this run that still mean something.
"""

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from textual.app import App

from tau._config import ConfigError
from tau._extensions import absolute_explicit
from tau._protocol import ProtocolError
from tau._serve_record import SERVE, ServeFailed, live_server, serve_directory
from tau._sessions import SessionFlags, SessionInfo, is_leased
from tau.tui._picker import Holder, SessionPicker
from tau.tui._themes import build_theme_registry, install_themes


@dataclass(frozen=True)
class Switch:
    """Where the person asked to go: the `session` id to open, or `None` for a new session.
    `served` says that a live `tau serve` holds it, so the extensions and options of this run do
    not carry (the server's are loaded already)."""

    session: str | None
    served: bool = False


async def session_holders(
    home: Path, rows: Sequence[SessionInfo], current: str | None
) -> dict[str, Holder]:
    """Who holds which of `rows`, by id. A record whose socket is gone is stale, and removed; a
    server that does not answer, or a record that cannot be read, is a `no reply` row with the
    reason (the listing still opens). A session with no server whose lease is held is `in use`."""
    found: dict[str, Holder] = {}
    # On Windows no server runs (`require_unix_sockets`), so there are no records to ask.
    records = [] if sys.platform == "win32" else sorted(serve_directory(home).glob("*.json"))
    directory = home / SERVE
    paths = {row.id: row.path for row in rows}
    for record in records:
        try:
            if await live_server(directory, record.stem, paths.get(record.stem)) is not None:
                found[record.stem] = Holder("served")
        except (ServeFailed, ConfigError, ProtocolError) as e:
            found[record.stem] = Holder("no reply", str(e))
    for row in rows:
        if row.id == current:
            found[row.id] = Holder("current")
        elif row.id not in found and row.error is None and is_leased(row.path):
            found[row.id] = Holder("in use", "another tau process holds this session")
    return found


class PickerApp(App[SessionInfo | None]):
    """A small app that shows a `SessionPicker` and exits with the row picked (`None`: cancelled):
    `tau -r`, before the session is started."""

    CSS_PATH = "tau.tcss"
    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        rows: list[SessionInfo],
        here: Path,
        held: Mapping[str, Holder],
        *,
        home: Path,
        theme: str | None,
    ) -> None:
        super().__init__()
        self._picker = SessionPicker(rows, here, held=held, title="Resume a session")
        self.theme_errors: list[str] = []
        registry = build_theme_registry(home, errors=self.theme_errors)
        install_themes(self, home, theme, registry=registry)

    def on_mount(self) -> None:
        for problem in self.theme_errors:
            self.notify(problem, severity="error", timeout=10)
        self.push_screen(self._picker, self.exit)


def switch_argv(args: object, cwd: Path, theme: str | None, switch: Switch) -> list[str]:
    """The command that is `tau` started for `switch`, from the line this run was started with
    (`args`, in `cwd`): the sessions directory and the theme; for a new session its model, prompt
    flags and `--no-session`; the extension flags (`-e`, `--no-extensions`, `--set`) unless a
    server holds the session, which would refuse them."""

    def option(name: str) -> object:
        return getattr(args, name)

    flags = SessionFlags.of(args, cwd)
    argv = [sys.executable, "-m", "tau"]
    if flags.directory is not None:
        argv += ["--session-dir", str(flags.directory)]
    if theme is not None:
        argv += ["--theme", theme]
    if not switch.served:
        for value in absolute_explicit(cast("list[str]", option("extension")), cwd):
            argv += ["-e", value]
        if option("no_extensions"):
            argv.append("--no-extensions")
        for extension, name, given in cast("list[tuple[str, str, str]]", option("set_options")):
            argv += ["--set", f"{extension}.{name}={given}"]
    if switch.session is not None:
        return [*argv, "--session", switch.session]
    if flags.no_session:
        argv.append("--no-session")
    if (model := cast("str | None", option("model"))) is not None:
        argv += ["-m", model]
    if flags.system_prompt is not None:
        argv += ["--system-prompt", flags.system_prompt]
    for text in flags.append_system_prompt:
        argv += ["--append-system-prompt", text]
    if flags.no_context_files:
        argv.append("-nc")
    return argv
