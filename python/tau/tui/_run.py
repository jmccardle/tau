"""`tau` with no `-p` (D23 section 1): the order of the launch.

One asyncio loop: `open_client` first, with prompts on the plain terminal (a trust question is
asked before the screen opens, the start report of `--verbose` goes to standard error before it
too), then `await TauApp(...).run_async()` on the same loop as the driver, then `Opened.close`.
Everything that can refuse the launch (usage, an unknown theme, a session another process holds,
flags a server would ignore) does so before the screen is taken.
"""

import asyncio
import getpass
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, TextIO

from tau._config_layers import read_models
from tau._events import CursorView
from tau._launch import UsageError
from tau._open import Opened, open_client
from tau._session_start import start_report
from tau._startup import prompts
from tau._trust import safe_block, tau_home, user_config_path
from tau.tui._app import TauApp
from tau.tui._themes import ThemeError, build_theme_registry, resolve_theme


class Process(Protocol):
    """What `tau.cli.main` was given."""

    @property
    def stdin(self) -> TextIO: ...
    @property
    def stdout(self) -> TextIO: ...
    @property
    def stderr(self) -> TextIO: ...
    @property
    def environ(self) -> Mapping[str, str]: ...
    @property
    def cwd(self) -> Path: ...


def client_id() -> str:
    """`<user>@<host>:tui`, the id the TUI is as a client."""
    return f"{getpass.getuser()}@{socket.gethostname()}:tui"


def chosen_theme(home: Path, given: str | None) -> str | None:
    """The theme to run in: `--theme`, else the user's config file's `tui.theme`, else `None` (the
    default). `ThemeError` (a `ConfigError`) for a name that is no theme; `ConfigError` for a
    config file the loader refuses."""
    name = given if given is not None else read_models(user_config_path(home)).tui.get("theme")
    failed: list[str] = []
    registry = build_theme_registry(home, errors=failed)
    try:
        resolve_theme(name, registry)
    except ThemeError as e:
        if failed:  # the theme asked for may be one of the files that did not load
            raise ThemeError(f"{e}\nthemes that did not load:\n" + "\n".join(failed)) from None
        raise
    return name


def report(opened: Opened) -> list[str]:
    """The start report (`--verbose`): the start's, or for a session a server holds what its
    `Welcome` and the cursor's view say."""
    if opened.started is not None:
        return start_report(opened.started)
    assert opened.welcome is not None  # a server's client has its `Welcome`
    lines = [
        f"session {opened.welcome.session} (attached to pid {opened.welcome.pid})",
        f"working directory {opened.welcome.cwd}",
    ]
    for event in opened.snapshot:
        if isinstance(event, CursorView) and event.cursor == opened.cursor and event.model:
            lines.append(f"model {event.model}")
    return lines


async def launch(process: Process, args: object, theme: str | None, home: Path) -> int:
    """Open the session, run the app on this loop, close the session; the exit status."""
    me = client_id()
    opened = await open_client(
        args,
        client=me,
        environ=process.environ,
        cwd=process.cwd,
        prompts=prompts(process.stdin, process.stdout),
        model=getattr(args, "model"),  # noqa: B009
    )
    try:
        if getattr(args, "verbose"):  # noqa: B009
            for line in report(opened):
                process.stderr.write("tau: " + safe_block(line) + "\n")
        app = TauApp(
            opened.client,
            opened.cursor,
            welcome=opened.welcome,
            home=home,
            snapshot=opened.snapshot,
            cwd=process.cwd if opened.started is None else opened.started.cwd,
            theme=theme,
            reconnect=opened.reconnect if opened.started is None else None,
        )
        await app.run_async()
        return app.return_code or 0
    finally:
        await opened.close()


def tui_mode(process: Process, args: object) -> int:
    """`tau` as `args` (parsed by `tau.cli`) say: returns the exit status. `UsageError` without a
    terminal on standard input and output; `ThemeError` for an unknown theme."""
    if not (process.stdin.isatty() and process.stdout.isatty()):
        raise UsageError(
            "the terminal UI needs a terminal on standard input and output; "
            "`tau -p MESSAGE` sends one message without one"
        )
    home = tau_home(process.environ)
    theme = chosen_theme(home, getattr(args, "theme"))  # noqa: B009
    return asyncio.run(launch(process, args, theme, home))
