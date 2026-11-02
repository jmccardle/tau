"""The serve directory and the serve record (D21 sections 2 and 9): how a program finds the
server of a session.

`$TAU_HOME/serve/` (mode 700, and refused if another user owns it or can write it) holds, for a
served session, `<session id>.sock` (the socket), `<session id>.json` (the **serve record**,
removed when the server exits) and `<session id>.log` (a detached server's output). The file
permissions are the access control: there is no token.

The record is the discovery: `{socket, pid, session, cwd, protocol, version, since, log}`. A record
whose socket answers `hello` is a live server; one whose socket does not is stale (a crash left
it) and `live_record` removes it.
"""

import asyncio
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from websockets.exceptions import WebSocketException

from tau._config import ConfigError
from tau._protocol import Welcome
from tau._remote import RemoteClient
from tau._trust import TrustError, write_atomically

SERVE: Final = "serve"
SOCKET_LIMIT: Final = 107
"""The longest Unix socket path, in bytes (`sun_path` is 108 with its terminator)."""
PROBE_SECONDS: Final = 2.0
"""How long a probe waits for a `hello` to be answered."""
_PRIVATE: Final = 0o700
_OTHERS_WRITE: Final = 0o022


@dataclass(frozen=True)
class ServeRecord:
    """What `<session id>.json` holds: the `socket` a server listens on, its `pid`, the `session`
    id it serves, that session's `cwd`, the `protocol` and τ `version` it speaks, when it started
    (`since`, milliseconds since the epoch) and its `log` (`None`: a foreground server has
    none)."""

    socket: str
    pid: int
    session: str
    cwd: str
    protocol: int
    version: str
    since: int
    log: str | None

    def text(self) -> str:
        """The record as the one line of JSON it is written as."""
        return json.dumps(asdict(self))


def serve_directory(home: Path) -> Path:
    """`<home>/serve`, made with mode 700 if it is not there. `TrustError` if it is not a
    directory of yours that only you can write."""
    path = home / SERVE
    try:
        path.mkdir(mode=_PRIVATE, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as e:
        raise TrustError(f"{path} cannot be made or inspected: {e.strerror}", path) from e
    if not stat.S_ISDIR(info.st_mode):
        raise TrustError(f"{path} is not a directory", path)
    if info.st_uid != os.geteuid():
        raise TrustError(f"{path} is not owned by you (uid {info.st_uid})", path)
    if stat.S_IMODE(info.st_mode) & _OTHERS_WRITE:
        raise TrustError(
            f"{path} is writable by group or others (mode {stat.S_IMODE(info.st_mode):o}); "
            f"run: chmod 700 {path}",
            path,
        )
    return path


def socket_path(directory: Path, session: str) -> Path:
    """`<directory>/<session>.sock`. `ConfigError` if it is too long for a Unix socket."""
    return checked_socket(directory / f"{session}.sock")


def checked_socket(path: Path) -> Path:
    """`path`, if a Unix socket can be bound there; else `ConfigError` suggesting `--socket`."""
    size = len(os.fsencode(path))
    if size > SOCKET_LIMIT:
        raise ConfigError(
            f"the socket path {path} is {size} bytes and a Unix socket's is at most "
            f"{SOCKET_LIMIT}; give a shorter one with --socket PATH"
        )
    return path


def record_path(directory: Path, session: str) -> Path:
    return directory / f"{session}.json"


def log_path(directory: Path, session: str) -> Path:
    return directory / f"{session}.log"


def read_record(path: Path) -> ServeRecord | None:
    """The record in `path`, or `None` if there is none. `ConfigError` if it is not a record."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ServeRecord(**data)
    except FileNotFoundError:
        return None
    except (ValueError, TypeError) as e:
        raise ConfigError(f"{path} is not a serve record: {e}") from e


def write_record(path: Path, record: ServeRecord) -> None:
    write_atomically(path, record.text() + "\n")


class ServeFailed(Exception):
    """`tau serve` could not do what it was asked. `kind` names the failure (`"Leased"`, a
    child's error type) for `--json`."""

    def __init__(self, message: str, kind: str = "ServeFailed") -> None:
        super().__init__(message)
        self.kind = kind


async def probe(socket: str) -> Welcome | None:
    """What a server at `socket` answers `hello` with, or `None` if there is none: the socket
    file is missing or nobody is listening on it (a crash left it). Anything else is not proof of
    death and is a `ServeFailed`: a server that does not answer in `PROBE_SECONDS` (it may be
    busy), a peer that is no server. `ProtocolMismatch` if a server answers that speaks another
    protocol."""
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            async with await RemoteClient.connect(socket, "tau:probe") as client:
                return client.welcome
    except (FileNotFoundError, ConnectionRefusedError):
        return None
    except (OSError, TimeoutError, WebSocketException) as e:
        raise ServeFailed(
            f"the server at {socket} did not answer hello ({type(e).__name__}: {e}); "
            "it may be busy, so its record is kept"
        ) from e


async def live_server(directory: Path, session: str) -> tuple[ServeRecord, Welcome] | None:
    """The record of the server of `session` and what it answered `hello`, if its socket answers;
    a record whose socket is gone or refuses is stale and is removed (`None`)."""
    path = record_path(directory, session)
    record = read_record(path)
    if record is None:
        return None
    welcome = await probe(record.socket)
    if welcome is None:
        path.unlink(missing_ok=True)
        return None
    return record, welcome


async def live_record(directory: Path, session: str) -> ServeRecord | None:
    """`live_server`'s record."""
    found = await live_server(directory, session)
    return None if found is None else found[0]
