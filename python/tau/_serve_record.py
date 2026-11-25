"""The serve directory and the serve record (D21 sections 2 and 9, D29): how a program finds the
server of a session.

`$TAU_HOME/serve/` (mode 700, and refused if another user owns it or can write it; on Windows,
refused unless its access list keeps out all but the user, SYSTEM and Administrators) holds, for a
served session, `<session id>.sock` (the socket), `<session id>.json` (the **serve record**, mode
600, removed when the server exits) and `<session id>.log` (a detached server's output). The
socket's access control is the file permissions; the HTTP edge's is the bearer token, which only
the record (and the user's terminal) carries.

The record is the discovery: `{pid, session, cwd, protocol, version, since, log}`, plus `socket`
when the server listens on one and `url`, `host`, `port` and `token` when it has an HTTP edge, and
the session's `name` if it has one. A record whose server answers `hello` is a live server; one
whose server does not is stale (a crash left it) and `live_record` removes it.
"""

import asyncio
import contextlib
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from websockets.exceptions import WebSocketException

from tau._config import ConfigError
from tau._core import private_dir
from tau._project_read import TrustError
from tau._protocol import Welcome
from tau._remote import RemoteClient
from tau._sessions import is_leased
from tau._trust import write_atomically

SERVE: Final = "serve"
SOCKET_LIMIT: Final = 107 if sys.platform == "linux" else 103
"""The longest Unix socket path, in bytes (`sun_path` is 108 with its terminator on Linux, 104 on
macOS)."""
PROBE_SECONDS: Final = 2.0
"""How long a probe waits for a `hello` to be answered."""
_PRIVATE: Final = 0o700


_OPTIONAL: Final = ("socket", "url", "host", "port", "token", "name")
"""The record's fields that are written only when they have a value."""


@dataclass(frozen=True)
class ServeRecord:
    """What `<session id>.json` holds: the server's `pid`, the `session` id it serves, that
    session's `cwd`, the `protocol` and τ `version` it speaks, when it started (`since`,
    milliseconds since the epoch) and its `log` (`None`: a foreground server has none); the
    `socket` it listens on, if it does; its HTTP edge's `url`, `host`, `port` and bearer `token`,
    if it has one; and the session's `name`, if it had one when the server started."""

    pid: int
    session: str
    cwd: str
    protocol: int
    version: str
    since: int
    log: str | None
    socket: str | None = None
    url: str | None = None
    host: str | None = None
    port: int | None = None
    token: str | None = None
    name: str | None = None

    def text(self) -> str:
        """The record as the one line of JSON it is written as (a field in `_OPTIONAL` only if it
        has a value)."""
        data = asdict(self)
        return json.dumps({k: v for k, v in data.items() if v is not None or k not in _OPTIONAL})

    def endpoints(self) -> str:
        """Where the server listens, for a person: `PATH`, `URL`, or `PATH and URL`."""
        return " and ".join(e for e in (self.socket, self.url) if e is not None)


def serve_directory(home: Path) -> Path:
    """`<home>/serve`, made with mode 700 if it is not there. `TrustError` if it is not a
    directory of yours. On Windows the directory's access list is checked instead
    (`tau._core.private_dir`): it holds the records, and a record holds the edge's token."""
    path = home / SERVE
    if sys.platform == "win32":
        try:
            private_dir(path, "the serve directory")
        except PermissionError as e:
            raise TrustError(str(e), path) from e
        return path
    try:
        path.mkdir(mode=_PRIVATE, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as e:
        raise TrustError(f"{path} cannot be made or inspected: {e.strerror}", path) from e
    if not stat.S_ISDIR(info.st_mode):
        raise TrustError(f"{path} is not a directory", path)
    if info.st_uid != os.geteuid():
        raise TrustError(f"{path} is not owned by you (uid {info.st_uid})", path)
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
        data = json.loads(_read_text(path))
        return ServeRecord(**data)
    except FileNotFoundError:
        return None
    except (ValueError, TypeError) as e:
        raise ConfigError(f"{path} is not a serve record: {e}") from e


_SHARE_ALL: Final = 0x7
"""`FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`."""


def _read_text(path: Path) -> str:
    """`path`'s text. On Windows the file is opened sharing delete, as Python's `open` does not,
    so a reader never makes the server's rename over its record, or its removal, fail; a record
    being removed reads as missing (`FileNotFoundError`)."""
    if sys.platform != "win32":
        return path.read_text(encoding="utf-8")
    import _winapi  # noqa: PLC0415 - Windows only
    import msvcrt  # noqa: PLC0415 - Windows only

    try:
        handle = _winapi.CreateFile(
            str(path), _winapi.GENERIC_READ, _SHARE_ALL, 0, _winapi.OPEN_EXISTING, 0, 0
        )
    except PermissionError as e:  # ERROR_ACCESS_DENIED: a file pending delete, in a private dir
        raise FileNotFoundError(e.errno, f"{path} is being removed") from e
    with open(msvcrt.open_osfhandle(handle, os.O_RDONLY), encoding="utf-8") as text:
        return text.read()


def write_record(path: Path, record: ServeRecord) -> None:
    write_atomically(path, record.text() + "\n")


class ServeFailed(Exception):
    """`tau serve` could not do what it was asked. `kind` names the failure (`"Leased"`, a
    child's error type) for `--json`."""

    def __init__(self, message: str, kind: str = "ServeFailed") -> None:
        super().__init__(message)
        self.kind = kind


async def dial(record: ServeRecord, client: str) -> RemoteClient:
    """A `RemoteClient` named `client` of the server `record` names: over its socket if it has
    one, else over its HTTP edge."""
    if record.socket is not None:
        return await RemoteClient.connect(record.socket, client)
    if record.url is None or record.token is None:
        raise ConfigError(f"the serve record of {record.session} names no socket and no URL")
    return await RemoteClient.connect_http(record.url, record.token, client)


async def probe_record(record: ServeRecord) -> Welcome | None:
    """`probe` for the server `record` names, by the way `dial` reaches it. A record whose pid is
    gone is stale without asking (its port may be another server's now), and so is one whose
    endpoint is answered by another session or process."""
    if not _alive(record.pid):
        return None
    if record.socket is not None:
        welcome = await probe(record.socket)
    else:
        welcome = await _probe_http(record)
    if welcome is None or (welcome.session, welcome.pid) != (record.session, record.pid):
        return None
    return welcome


async def _probe_http(record: ServeRecord) -> Welcome | None:
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            async with await dial(record, "tau:probe") as client:
                return client.welcome
    except ConnectionRefusedError:
        return None
    except (OSError, TimeoutError) as e:
        raise ServeFailed(
            f"the server at {record.url} did not answer hello ({type(e).__name__}: {e}); "
            "it may be busy, so its record is kept"
        ) from e


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


def _alive(pid: int) -> bool:
    """Whether a process with `pid` exists (this user's, or one we may not signal). On Windows
    `os.kill` would terminate it, so the process is opened and asked whether it has exited."""
    if sys.platform == "win32":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_QUERY_LIMITED: Final = 0x1000
"""`PROCESS_QUERY_LIMITED_INFORMATION`: enough to read an exit code, granted for most processes."""


def _alive_windows(pid: int) -> bool:
    if sys.platform != "win32":
        raise AssertionError("Windows only")
    import _winapi  # noqa: PLC0415 - Windows only

    try:
        handle = _winapi.OpenProcess(_QUERY_LIMITED, False, pid)
    except PermissionError:  # another user's process (ERROR_ACCESS_DENIED)
        return True
    except OSError:  # ERROR_INVALID_PARAMETER: no process has that pid
        return False
    try:
        return _winapi.GetExitCodeProcess(handle) == _winapi.STILL_ACTIVE
    finally:
        _winapi.CloseHandle(handle)


async def live_server(
    directory: Path, session: str, lease: Path | None
) -> tuple[ServeRecord, Welcome] | None:
    """The record of the server of `session` and what it answered `hello`, if its socket answers;
    a record whose socket is gone or refuses is stale and is removed (`None`). Unless the server is
    stopping: its socket is gone but its process still holds the session (`lease` is the session's
    directory), which is a `ServeFailed` ("try again") and keeps the record, as the record is
    removed after the lease is let go. `lease` of `None` skips that check."""
    path = record_path(directory, session)
    record = read_record(path)
    if record is None:
        return None
    welcome = await probe_record(record)
    if welcome is None:
        if lease is not None and _alive(record.pid) and is_leased(lease):
            raise ServeFailed(
                f"session {session} (pid {record.pid}) is stopping; try again", "Stopping"
            )
        with contextlib.suppress(ConfigError):  # not a record now: not ours to judge
            if read_record(path) == record:  # a new server may have written its own meanwhile
                path.unlink(missing_ok=True)
        return None
    return record, welcome


async def live_record(directory: Path, session: str, lease: Path | None) -> ServeRecord | None:
    """`live_server`'s record."""
    found = await live_server(directory, session, lease)
    return None if found is None else found[0]
