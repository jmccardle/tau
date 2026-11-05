"""`tau serve` (D21 section 9): the server in the foreground, detached with `-d`, stopped with
`--stop`, and its schema with `--schema`.

- `tau serve [session flags] [-e ...] [--socket PATH]` starts the session the shared start
  (`tau.start_session`) opens, binds a Unix socket, writes the serve record and serves until
  SIGTERM or SIGINT (what is running is aborted and the agents it started are closed, as an
  interrupted `-p` does; then the driver stops, each stream gets `end`, the socket and record
  are removed, exit 0) or until the driver fails (exit 1). With `--json` it prints the record as
  one line.
- `tau serve -d [--json]` starts the same server detached and prints its record once a `hello`
  answers: the record of a server already serving the session with `"started": false` (a record
  whose socket does not answer is stale and replaced); a failure before the record appears, with
  its message intact, exit 1. The parent asks what needs a person (trust) first; the child has no
  terminal. A session another process holds without serving it never yields a record: the parent
  waits `BOUND_SECONDS` for one and then fails with the `Leased` error.
- `tau serve --stop [session flags]` checks that the recorded pid answers `hello` and sends it
  SIGTERM, and waits for the record to go.

`--json` output is one JSON object per line: the record (with `started` for `-d`), an error
`{"error": {"type", "message"}}`, and `--stop`'s `{session, pid, stopped}`.
"""

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Final, Protocol, TextIO

from tau._choice import choose_session
from tau._clock import SystemClock
from tau._config import ConfigError
from tau._core import CursorClosed
from tau._driver import Driver
from tau._project_read import safe_block, safe_text
from tau._protocol import PROTOCOL, schema_text
from tau._serve import Server
from tau._serve_record import (
    ServeFailed,
    ServeRecord,
    checked_socket,
    live_record,
    live_server,
    log_path,
    probe,
    read_record,
    record_path,
    serve_directory,
    socket_path,
    write_record,
)
from tau._session_start import Started, start_report, start_session
from tau._sessions import SessionFlags
from tau._startup import load_config, prompts
from tau._trust import tau_home

IDLE_SECONDS: Final = 600.0
"""How long a detached server rests (no client connected, every cursor at rest) before it exits."""
BOUND_SECONDS: Final = 5.0
"""How long `-d` waits for a record to appear and `--stop` for it to go. No wait is infinite."""
WIND_DOWN_SECONDS: Final = 3.0
"""How long a stopping server waits for aborted cursors to rest, under `--stop`'s own bound. A
hook never returns on an abort (M7), so the wait is bounded; past it the driver stops as a crash
would."""
_POLL: Final = 0.05


class Process(Protocol):
    """What a command runs in: the streams, environment and directory `tau.cli.main` was given,
    and the arguments after `serve`."""

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
    @property
    def argv(self) -> Sequence[str]: ...


def error_line(error: BaseException) -> str:
    """`error` as the JSON line `--json` prints and a detached child leaves in its log."""
    kind = error.kind if isinstance(error, ServeFailed) else type(error).__name__
    return json.dumps({"error": {"type": kind, "message": str(error)}})


def serve(run: Process, args: argparse.Namespace) -> int:
    """`tau serve` as `args` (parsed by `tau.cli`) say."""
    if args.schema:
        run.stdout.write(schema_text())
        return 0
    if args.stop:
        return asyncio.run(_stop(run, args))
    if args.detach and args.detached_log is None:
        return asyncio.run(_detach(run, args))
    return asyncio.run(_foreground(run, args))


def _emit(run: Process, data: Mapping[str, object]) -> None:
    run.stdout.write(json.dumps(data) + "\n")


# The server, in the foreground


async def _foreground(run: Process, args: argparse.Namespace) -> int:
    """Start the session, serve it, and clean up. The child of `-d` is this, with `detached_log`
    (the file its output goes to until the session's id names the log) and the idle exit."""
    home = tau_home(run.environ)
    directory = serve_directory(home)
    flags = SessionFlags.of(args, run.cwd)
    if flags.no_session:
        raise ConfigError(
            "a served session is kept: --no-session has no id to name its socket and record"
        )
    explicit = None if args.socket is None else Path(os.path.abspath(run.cwd / args.socket))
    if explicit is not None:
        checked_socket(explicit)
        if await probe(str(explicit)) is not None:
            raise ServeFailed(f"another server already answers at {explicit}")
    started = await start_session(
        args, environ=run.environ, cwd=run.cwd, prompts=prompts(run.stdin, run.stdout)
    )
    if args.verbose:
        for line in start_report(started):
            run.stderr.write("tau: " + safe_block(line) + "\n")
    session = None if started.directory is None else started.directory.name
    assert session is not None  # `--no-session` was refused above
    try:
        socket = explicit or socket_path(directory, session)
        return await _serving(run, args, started, _Place(directory, session, socket))
    except BaseException:
        await started.stop()
        raise


@dataclass(frozen=True)
class _Place:
    """Where a served session's files are: the serve `directory`, the `session` id, the
    `socket`."""

    directory: Path
    session: str
    socket: Path


async def _serving(run: Process, args: argparse.Namespace, started: Started, place: _Place) -> int:
    detached = args.detached_log is not None
    session, socket = place.session, place.socket
    loop = asyncio.get_running_loop()
    signalled = asyncio.Event()  # before the record says we are up: SIGTERM then means a clean stop
    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, signalled.set)
    server = await Server.start(
        started.driver,
        socket,
        session=session,
        cwd=started.cwd,
        idle=IDLE_SECONDS if detached else None,
    )
    log = log_path(place.directory, session) if detached else None
    path = record_path(place.directory, session)
    try:
        if log is not None:
            os.replace(args.detached_log, log)
        record = ServeRecord(
            str(socket),
            os.getpid(),
            session,
            str(started.cwd),
            PROTOCOL,
            version("ffwf-tau"),
            SystemClock().now(),
            None if log is None else str(log),
        )
        write_record(path, record)
        if args.json:
            _emit(run, json.loads(record.text()))
        else:
            run.stdout.write(safe_text(f"serving {session} on {socket} (pid {record.pid})") + "\n")
        run.stdout.flush()
        failure = await _until_done(server, signalled)
        if failure is None:
            await _wind_down(started.driver)
    finally:
        for number in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(number)
        await started.driver.stop()  # each stream is told `end`, then the server unbinds
        await server.close()
        socket.unlink(missing_ok=True)
        await started.stop()
        path.unlink(missing_ok=True)  # last: no record means the lease is free
    if failure is None and log is not None:
        log.unlink(missing_ok=True)  # kept when the driver failed: it says why
    if failure is not None:
        raise ServeFailed(f"the driver failed: {type(failure).__name__}: {failure}")
    return 0


async def _wind_down(driver: Driver) -> None:
    """Abort every cursor and wait for each to rest, so a tool that started an agent is
    cancelled and closes the agent's cursor (D24 section 8): the session is left as an
    interrupted `-p` leaves it, not as a crash. Bounded by `WIND_DOWN_SECONDS`."""
    async with driver.client("serve:stop") as client:
        cursors = list(driver.session.cursors())
        for cursor in cursors:
            with contextlib.suppress(CursorClosed):  # an agent closed since the list was made
                await client.abort(cursor)
        with contextlib.suppress(TimeoutError, CursorClosed):
            async with asyncio.timeout(WIND_DOWN_SECONDS):
                for cursor in cursors:
                    await driver.rest(cursor)


async def _until_done(server: Server, signalled: asyncio.Event) -> Exception | None:
    """Wait for `signalled` (`None`), or for the server to return: the driver failed (its
    failure) or the detached server rested long enough (`None`)."""
    waiting = asyncio.ensure_future(server.wait())
    stopping = asyncio.ensure_future(signalled.wait())
    try:
        await asyncio.wait({waiting, stopping}, return_when=asyncio.FIRST_COMPLETED)
        return waiting.result() if waiting.done() else None
    finally:
        waiting.cancel()
        stopping.cancel()


# -d


async def _detach(run: Process, args: argparse.Namespace) -> int:
    """Print the record of the server of the session, starting one detached unless one answers."""
    home = tau_home(run.environ)
    directory = serve_directory(home)
    flags = SessionFlags.of(args, run.cwd)
    if flags.no_session:
        raise ConfigError(
            "a served session is kept: --no-session has no id to name its socket and record"
        )
    choice = choose_session(flags, home=home, cwd=run.cwd)
    existing = choice.source.id if choice.kind == "existing" and choice.source else None
    if existing is not None:
        record = await live_record(directory, existing)
        if record is not None:
            return _announce(run, args, record, started=False)
    # What needs a person is asked here; the child has no terminal.
    load_config(choice.cwd, run.environ, ask=prompts(run.stdin, run.stdout).ask)
    pending = directory / f".starting-{os.getpid()}.log"
    child = _spawn(run, pending)
    found = await _record_of(directory, existing, child, pending)
    if found is not None:
        pending.unlink(missing_ok=True)
        return _announce(run, args, found, started=found.pid == child.pid)
    raise _why_not(child, pending, existing)


def _spawn(run: Process, pending: Path) -> subprocess.Popen[bytes]:
    """The detached server: this command line without `-d` and `--json`, with the log it writes
    until it knows its session's id. No terminal, its own session."""
    kept = [a for a in run.argv if a not in ("-d", "--detach", "--json")]
    command = [sys.executable, "-m", "tau", "serve", *kept, "--detached-log", str(pending)]
    with os.fdopen(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as log:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=run.cwd,
            env=dict(run.environ),
            start_new_session=True,
        )


async def _record_of(
    directory: Path, session: str | None, child: subprocess.Popen[bytes], pending: Path
) -> ServeRecord | None:
    """The record of a server that answers: the session's (its own, or a winner's if `child`
    lost the race for the lease), else the one `child` writes. `None` if none answers within
    `BOUND_SECONDS`, or `child` has failed for a reason that no record will cure."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BOUND_SECONDS
    while loop.time() < deadline:
        record = await _candidate(directory, session, child.pid)
        if record is not None and await probe(record.socket) is not None:
            return record
        if child.poll() is not None and not _leased(child, pending):
            return None  # nobody else's record can cure what went wrong
        await asyncio.sleep(_POLL)
    return None


def _leased(child: subprocess.Popen[bytes], pending: Path) -> bool:
    """Whether `child` failed because another process holds the session: the winner of that race
    writes the record this parent then prints."""
    said = _last_error(pending)
    return said is not None and said[0] == "Leased"


async def _candidate(directory: Path, session: str | None, pid: int) -> ServeRecord | None:
    """The record to try: the named session's, or whichever record has `pid`."""
    if session is not None:
        return read_record(record_path(directory, session))
    for path in directory.glob("*.json"):
        record = read_record(path)
        if record is not None and record.pid == pid:
            return record
    return None


def _why_not(child: subprocess.Popen[bytes], pending: Path, session: str | None) -> ServeFailed:
    """The failure of a `-d` that got no record: what the child said, or why we gave up."""
    code = child.poll()
    said = _last_error(pending)
    if said is not None:
        pending.unlink(missing_ok=True)
        return ServeFailed(said[1], said[0])
    if code is None:
        return ServeFailed(
            f"the server did not answer within {BOUND_SECONDS:g} s; its output so far is in "
            f"{pending}; `tau serve -d` again finds it once it is up"
        )
    return ServeFailed(
        f"the server exited with status {code} before it answered; its output is in {pending}"
        + ("" if session is None else f" (session {session})")
    )


def _last_error(log: Path) -> tuple[str, str] | None:
    """The `{"error": ...}` line a failed child left last in its log: its type and message."""
    with contextlib.suppress(OSError):
        for line in reversed(log.read_text(encoding="utf-8").splitlines()):
            with contextlib.suppress(ValueError, KeyError, TypeError):
                error = json.loads(line)["error"]
                return str(error["type"]), str(error["message"])
    return None


def _announce(run: Process, args: argparse.Namespace, record: ServeRecord, *, started: bool) -> int:
    if args.json:
        _emit(run, {**json.loads(record.text()), "started": started})
    else:
        verb = "started" if started else "already serving"
        run.stdout.write(
            safe_text(f"{verb} {record.session} on {record.socket} (pid {record.pid})") + "\n"
        )
    return 0


# --stop


async def _stop(run: Process, args: argparse.Namespace) -> int:
    """SIGTERM the server of the session if it answers, and wait for its record to go."""
    home = tau_home(run.environ)
    directory = serve_directory(home)
    choice = choose_session(SessionFlags.of(args, run.cwd), home=home, cwd=run.cwd)
    if choice.kind != "existing" or choice.source is None:
        raise ConfigError("--stop names the session to stop: -c, -r or --session REF")
    session = choice.source.id
    found = await live_server(directory, session)
    if found is None:
        result: Mapping[str, object] = {"session": session, "pid": None, "stopped": False}
    else:
        record, welcome = found
        if welcome.pid != record.pid:
            raise ServeFailed(
                f"{record_path(directory, session)} names pid {record.pid}, "
                f"but the server at {record.socket} is pid {welcome.pid}"
            )
        os.kill(record.pid, signal.SIGTERM)
        await _gone(record_path(directory, session), record.pid)
        result = {"session": session, "pid": record.pid, "stopped": True}
    if args.json:
        _emit(run, result)
    elif result["stopped"]:
        run.stdout.write(safe_text(f"stopped {session} (pid {result['pid']})") + "\n")
    else:
        run.stdout.write(safe_text(f"no server is serving {session}") + "\n")
    return 0


async def _gone(record: Path, pid: int) -> None:
    """Wait for the server's record to go (it goes last, after the lease is released)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BOUND_SECONDS
    while record.exists():
        if loop.time() > deadline:
            raise ServeFailed(f"the server (pid {pid}) did not stop within {BOUND_SECONDS:g} s")
        await asyncio.sleep(_POLL)
