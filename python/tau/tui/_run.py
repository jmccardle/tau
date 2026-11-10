"""`tau` with no `-p` (D23 section 1): the order of the launch.

One asyncio loop: `open_client` first, with prompts on the plain terminal (a trust question is
asked before the screen opens, the start report of `--verbose` goes to standard error before it
too), then `await TauApp(...).run_async()` on the same loop as the driver, then `Opened.close`.
Everything that can refuse the launch (usage, an unknown theme, a session another process holds,
flags a server would ignore) does so before the screen is taken.

`-r` runs the session picker first, as a small app of its own (`PickerApp`), and becomes
`--session ID`. When the person leaves the screen for another session (Ctrl+O, Ctrl+N) the launch
returns the `Switch` after closing the session, and `tui_mode` replaces the process with `tau`
started for it (D23 section 5).
"""

import asyncio
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, TextIO

from tau._config_layers import read_models
from tau._events import CursorView
from tau._launch import UsageError
from tau._open import Opened, client_id, open_client
from tau._project_read import safe_block
from tau._session_start import start_report
from tau._sessions import SessionFlags, UnknownSession, list_sessions, sessions_directory
from tau._startup import prompts
from tau._trust import tau_home, user_config_path
from tau.tui._app import TauApp
from tau.tui._switch import PickerApp, Switch, session_holders, switch_argv
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


async def launch(
    process: Process, args: object, theme: str | None, home: Path
) -> tuple[int, Switch | None]:
    """Open the session, run the app on this loop, close the session; the exit status and where
    the person asked to go next, if they did."""
    me = client_id("tui")
    opened = await open_client(
        args,
        client=me,
        environ=process.environ,
        cwd=process.cwd,
        prompts=prompts(process.stdin, process.stdout),
        model=getattr(args, "model"),  # noqa: B009
    )
    try:
        for warning in opened.warnings:
            process.stderr.write("tau: " + safe_block(warning) + "\n")
        if getattr(args, "verbose"):  # noqa: B009
            for line in report(opened):
                process.stderr.write("tau: " + safe_block(line) + "\n")
        flags = SessionFlags.of(args, process.cwd)
        app = TauApp(
            opened.client,
            opened.cursor,
            welcome=opened.welcome,
            home=home,
            snapshot=opened.snapshot,
            cwd=process.cwd if opened.started is None else opened.started.cwd,
            theme=theme,
            reconnect=opened.reconnect if opened.started is None else None,
            sessions=sessions_directory(home, flags.directory),
        )
        await app.run_async()
        return app.return_code or 0, app.switch
    finally:
        await opened.close()


def choose_session(process: Process, args: object, theme: str | None, home: Path) -> bool:
    """`-r`: show the picker (before the session starts) and make `args` name the session picked,
    as `--session ID`. False when the person cancelled. `UnknownSession` when there is none."""
    flags = SessionFlags.of(args, process.cwd)
    sessions = sessions_directory(home, flags.directory)
    rows = list_sessions(sessions)
    if not rows:
        raise UnknownSession(f"no session in {sessions} to resume")
    held = asyncio.run(session_holders(home, rows, None))
    picker = PickerApp(rows, process.cwd, held, home=home, theme=theme)
    picked = picker.run()
    if picked is None:
        return False
    setattr(args, "resume", False)  # noqa: B010
    setattr(args, "session", picked.id)  # noqa: B010
    return True


def replace_process(process: Process, args: object, theme: str | None, switch: Switch) -> None:
    """Become `tau` started for `switch`: in the directory this one was typed in, so the flags
    that name paths mean what they meant. Does not return."""
    argv = switch_argv(args, process.cwd, theme, switch)
    os.chdir(process.cwd)
    process.stdout.flush()
    os.execve(sys.executable, argv, dict(process.environ))


def tui_mode(process: Process, args: object) -> int:
    """`tau` as `args` (parsed by `tau.cli`) say: returns the exit status. `UsageError` without a
    terminal on standard input and output; `ThemeError` for an unknown theme. When the person
    leaves the screen for another session, this process is replaced by `tau` started for it."""
    if not (process.stdin.isatty() and process.stdout.isatty()):
        raise UsageError(
            "the terminal UI needs a terminal on standard input and output; "
            "`tau -p MESSAGE` sends one message without one"
        )
    home = tau_home(process.environ)
    theme = chosen_theme(home, getattr(args, "theme"))  # noqa: B009
    if getattr(args, "resume") and not choose_session(process, args, theme, home):  # noqa: B009
        process.stderr.write("tau: no session chosen\n")
        return 1
    status, switch = asyncio.run(launch(process, args, theme, home))
    if switch is not None:
        replace_process(process, args, theme, switch)
    return status
