"""`open_client` (D22 section 8): how a frontend gets a session and a client of it, in one call.

The session flags choose a session. If the choice is an existing session that a live `tau serve`
holds (its serve record answers `hello`, D21 section 9), the client is a `RemoteClient` of that
server: the frontend **attaches**. Otherwise the shared start (`start_session`) runs in this
process and the client is a `LocalClient` of its driver. A new session or a fork never attaches.

Either way the client's stream is read up to the snapshot's `Labels`, and the first cursor is
picked from it: the open cursor with the lowest id; if none is open, the closed cursor with the
lowest id, which the client reopens (a new id with the old labels). The choice among other cursors
is the frontend's (D16, D17). The events read are kept, so a frontend loses none.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from tau._choice import SessionChoice, choose_session
from tau._client import Client
from tau._config import ConfigError
from tau._events import CursorGone, CursorView, Event, Labels
from tau._protocol import PROTOCOL, Welcome
from tau._protocols import Clock, Extension
from tau._remote import RemoteClient
from tau._serve_record import SERVE, ServeRecord, live_server, serve_directory
from tau._session_start import Started, SystemText, refuse_model, start_session
from tau._sessions import PickSession, SessionFlags, SessionInfo
from tau._startup import Prompts
from tau._trust import tau_home


@dataclass
class Opened:
    """A session opened for a client: the `client`, the first `cursor`, the `snapshot` (every
    event the client's stream held up to and including its first `Labels`, and for a reopened
    cursor the events up to its view), the `welcome` (the server's if attached; for a local start
    one built from it, `None` for a session kept in memory, which has no id) and the `started`
    session (`None` when attached: the server owns it). Attached, `socket` is the address in the
    server's record and `me` the client id, so `reconnect` can dial again; `client` is always the
    client to use now."""

    client: Client
    cursor: int
    snapshot: tuple[Event, ...]
    welcome: Welcome | None
    started: Started | None
    socket: Path | None
    me: str

    @property
    def warnings(self) -> tuple[str, ...]:
        """What the start's config names that it lacks (`Started.warnings`); none when attached,
        where the server said them."""
        return () if self.started is None else self.started.warnings

    async def reconnect(self) -> Client:
        """A new client of the server this session is attached to, with the same id: after a
        stream ended in `ConnectionError`. It replaces `client` (the old one is closed). `OSError`
        if nothing answers at the server's address; `ConnectionError` for a session this process
        runs, which has no server to dial."""
        if self.socket is None:
            raise ConnectionError("this session runs in this process: there is no server to dial")
        new = await RemoteClient.connect(self.socket, self.me)
        old, self.client = self.client, new
        await old.close()
        return new

    async def close(self) -> None:
        """Close the client; for a session this process started, then `Started.stop`."""
        await self.client.close()
        if self.started is not None:
            await self.started.stop()


async def _server_of(choice: SessionChoice, home: Path) -> tuple[ServeRecord, Welcome] | None:
    """The record and `Welcome` of the live server that holds the chosen session, if it is an
    existing one and one answers; a record whose socket does not is stale (removed)."""
    if choice.kind != "existing" or choice.source is None or not (home / SERVE).is_dir():
        return None
    return await live_server(serve_directory(home), choice.source.id)


def _refuse_ignored(
    args: object, flags: SessionFlags, own: Sequence[Extension], record: ServeRecord
) -> None:
    """What a running server would ignore: its extensions, options and name are loaded or
    written already."""
    ignored: list[str] = []
    if own:
        ignored.append("own= extensions")
    if flags.name is not None:
        ignored.append("-n")
    if getattr(args, "extension"):  # noqa: B009
        ignored.append("-e")
    if getattr(args, "no_extensions"):  # noqa: B009
        ignored.append("--no-extensions")
    if getattr(args, "set_options", None):
        ignored.append("--set")
    if ignored:
        raise ConfigError(
            f"session {record.session} is served by pid {record.pid}, whose extensions, "
            f"options and name are set; {' and '.join(ignored)} would be ignored. Stop the server "
            f"(`tau serve --stop --session {record.session}`), or work on a copy: "
            f"tau --fork {record.session}"
        )


async def _snapshot(client: Client) -> tuple[list[Event], int]:
    """The events up to the first `Labels`, and the first cursor, reopening a closed one."""
    events: list[Event] = []
    async for event in client:
        events.append(event)
        if isinstance(event, Labels):
            break
    else:
        raise ConnectionError("the session's stream ended before its snapshot was whole")
    opened = [e.cursor for e in events if isinstance(e, CursorView)]
    if opened:
        return events, min(opened)
    closed = [e.cursor for e in events if isinstance(e, CursorGone)]
    if not closed:
        raise LookupError("the session has no cursor")
    reopened = await client.reopen(min(closed))
    async for event in client:  # the new cursor's view was published before reopen returned
        events.append(event)
        if isinstance(event, CursorView) and event.cursor == reopened:
            return events, reopened
    raise ConnectionError("the session's stream ended before the reopened cursor's view")


async def _attach(  # noqa: PLR0913, PLR0917 - what attaching needs of the open
    args: object,
    flags: SessionFlags,
    choice: SessionChoice,
    record: ServeRecord,
    client: str,
    model: str | None,
    own: Sequence[Extension],
) -> RemoteClient:
    """A client of the server of the chosen session, once nothing the server would ignore was
    asked."""
    refuse_model(choice, model)
    _refuse_ignored(args, flags, own, record)
    return await RemoteClient.connect(record.socket, client)


async def open_client(  # noqa: PLR0913 - the start's inputs and the client's id
    args: object,
    *,
    client: str,
    environ: Mapping[str, str],
    cwd: Path,
    system: SystemText | None = None,
    prompts: Prompts | None = None,
    pick_session: PickSession | None = None,
    own: Sequence[Extension] = (),
    model: str | None = None,
    clock: Clock | None = None,
) -> Opened:
    """Open the session `args` choose (see `start_session` for the arguments and what it
    raises) and a client named `client` of it, as the module says: attached to the `tau serve`
    that holds it, else started here. `ConfigError` when attached and `-e`, `--no-extensions`,
    `--set`, `-n`, `own` or `--model` ask for what the server's loaded session cannot take;
    `Leased` for a session another process holds that is no server;
    `LookupError` for a session with no cursor. Close the result with `Opened.close`."""
    flags = SessionFlags.of(args, cwd)
    home = tau_home(environ)
    choice = choose_session(flags, home=home, cwd=cwd, pick=pick_session)
    found = await _server_of(choice, home)
    if found is not None:
        remote = await _attach(args, flags, choice, found[0], client, model, own)
        return await _opened(remote, found[1], None, Path(found[0].socket), client)
    chosen = pick_session if not flags.resume else _chosen(choice)
    started = await start_session(
        args,
        environ=environ,
        cwd=cwd,
        system=system,
        prompts=prompts,
        pick_session=chosen,
        own=own,
        model=model,
        clock=clock,
    )
    try:
        local = started.driver.client(client)
        return await _opened(local, _local_welcome(started), started, None, client)
    except BaseException:
        await started.stop()
        raise


def _chosen(choice: SessionChoice) -> PickSession:
    """A `PickSession` that gives back the session `-r` already chose: it is not asked twice."""
    source = choice.source
    assert source is not None

    def pick(_nearby: Sequence[SessionInfo], _everywhere: Sequence[SessionInfo]) -> SessionInfo:
        return source

    return pick


def _local_welcome(started: Started) -> Welcome | None:
    """What a server would have answered `hello`, for a session this process serves itself."""
    if started.directory is None:
        return None
    return Welcome(
        PROTOCOL, version("ffwf-tau"), started.directory.name, str(started.cwd), os.getpid()
    )


async def _opened(
    client: Client, welcome: Welcome | None, started: Started | None, socket: Path | None, me: str
) -> Opened:
    try:
        events, cursor = await _snapshot(client)
    except BaseException:
        await client.close()
        raise
    return Opened(client, cursor, tuple(events), welcome, started, socket, me)
