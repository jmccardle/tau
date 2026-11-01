"""The session catalog (D18): where sessions live, how one is named, found and chosen.

A session is a directory under the **sessions directory** (`$TAU_HOME/sessions`, or
`--session-dir`), named by its **session id** (`20261031-142233-7f3a9c`: the UTC creation time and
six hex digits, so `ls` lists them in creation order). It holds the store's files (`log`,
`scratchpad`) and `info.json`, which is the catalog's alone: the working directory the session was
created in (`cwd`, never changed), an optional `name`, and an optional `forked_from`. Core and the
store never read it. `info.json` is replaced whole and atomically.

Listing is a scan, with no index: `list_sessions` returns a `SessionInfo` per directory, a row
with an `error` for one that is not a readable session. `find_session` resolves a ref (`parse_ref`):
a path, an id, an exact unique name, or a unique id prefix. `choose_session` turns the session
flags into the one thing a start does (`SessionChoice`): a new session, an existing one, or a
fork. `first_cursor` and `first_cursor_node` are the rule for which cursor a frontend shows. Which
session a program opens, and how it starts, is `tau.session_start`.
"""

import json
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from tau._config import ConfigError
from tau._core import Exists, NotASession, Session, first_message

if TYPE_CHECKING:
    from tau.client import Client

SESSIONS = "sessions"
INFO = "info.json"
_LOG = "log"
_PRIVATE = 0o700
_TITLE_WIDTH = 80
_NO_MESSAGES = "(no messages)"
_NODE = re.compile(r"^(?P<session>.*):(?P<node>\d+)$", re.DOTALL)


class UnknownSession(LookupError):
    """A ref that names no session, or a name or id prefix that names several. The message lists
    the candidates with their `cwd` and age."""


@dataclass(frozen=True)
class ForkedFrom:
    """Where a forked session's walk was copied from: the source's `session` id and the `node` of
    its tree the copy ends at."""

    session: str
    node: int


@dataclass(frozen=True)
class SessionInfo:
    """One row of the catalog. `id` and `path` locate the session; `cwd` is its working
    directory, `name` its name, `forked_from` its source; `updated` is the log's modification
    time in seconds since the epoch (every synced write moves it); `first` is the text of its
    first `User` node, read only when it has no `name` and only when asked for. A row that is not
    a readable session has an `error` saying why, and no other field can be relied on."""

    id: str
    path: Path
    cwd: str | None
    name: str | None
    forked_from: ForkedFrom | None
    updated: float | None
    first: str | None
    error: str | None

    @property
    def title(self) -> str:
        """What a frontend shows for it: the name, else the first message, on one line (cut to
        80 characters), else `(no messages)`."""
        line = " ".join((self.name or self.first or "").split())
        if not line:
            return _NO_MESSAGES
        return line if len(line) <= _TITLE_WIDTH else line[: _TITLE_WIDTH - 1] + "…"


# -- the sessions directory and ids


def sessions_directory(home: Path, given: Path | None) -> Path:
    """`given` (`--session-dir`, already absolute) or `<home>/sessions`."""
    return home / SESSIONS if given is None else given


def new_session_id(now: int) -> str:
    """A session id for `now` (milliseconds since the epoch): UTC time to the second, then six
    random hex digits."""
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now / 1000)) + "-" + secrets.token_hex(3)


def _make_private(path: Path) -> None:
    """`path` and any ancestor it lacks, each with mode 700."""
    for missing in reversed([p for p in (path, *path.parents) if not p.exists()]):
        missing.mkdir(mode=_PRIVATE, exist_ok=True)


def _info_data(cwd: str, name: str | None, forked_from: ForkedFrom | None) -> dict[str, object]:
    data: dict[str, object] = {"cwd": cwd}
    if name:
        data["name"] = name
    if forked_from is not None:
        data["forked_from"] = {"session": forked_from.session, "node": forked_from.node}
    return data


def _write_info(directory: Path, data: Mapping[str, object]) -> None:
    """`info.json` written whole: a temporary file, synced, renamed over, the directory synced."""
    fd, temporary = tempfile.mkstemp(prefix=f"{INFO}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, directory / INFO)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def make_session_directory(
    sessions: Path, now: int, cwd: Path, name: str | None, forked_from: ForkedFrom | None
) -> Path:
    """A new session directory (exclusive `mkdir`, mode 700) with its `info.json` written before
    the log is created. `Exists` on an id collision. Remove it with `shutil.rmtree` if creating
    the session in it fails."""
    _make_private(sessions)
    identity = new_session_id(now)
    path = sessions / identity
    try:
        path.mkdir(mode=_PRIVATE)
    except FileExistsError as e:
        raise Exists(f"session {identity} already exists in {sessions}") from e
    try:
        _write_info(path, _info_data(str(cwd), name, forked_from))
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


# -- reading info.json and scanning


def _bad(path: Path, why: str) -> ValueError:
    return ValueError(f"{path.name}/{INFO} {why}")


def _text_field(data: Mapping[str, object], key: str, path: Path) -> str | None:
    value = data.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise _bad(path, f"has a {key!r} that is not a non-empty string")
    return value


def _forked_from(data: Mapping[str, object], path: Path) -> ForkedFrom | None:
    value = data.get("forked_from")
    if value is None:
        return None
    fields: Mapping[str, object] = (
        cast("dict[str, object]", value) if isinstance(value, dict) else {}
    )
    session, node = fields.get("session"), fields.get("node")
    if not isinstance(session, str) or not isinstance(node, int) or isinstance(node, bool):
        raise _bad(path, "has a 'forked_from' that is not {session, node}")
    return ForkedFrom(session, node)


def _read_info(path: Path) -> tuple[str, str | None, ForkedFrom | None]:
    """`(cwd, name, forked_from)` from `path/info.json`; `ValueError` or `OSError` saying what
    is wrong with it."""
    try:
        data: object = json.loads((path / INFO).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _bad(path, "is missing; it is not a session of this version (delete it)") from None
    except ValueError as e:
        raise _bad(path, f"is not JSON ({e})") from e
    if not isinstance(data, dict):
        raise _bad(path, "is not a JSON object")
    fields = cast("Mapping[str, object]", data)
    cwd = _text_field(fields, "cwd", path)
    if cwd is None:
        raise _bad(path, "has no 'cwd'")
    return cwd, _text_field(fields, "name", path), _forked_from(fields, path)


def session_info(path: Path, *, first: bool = False) -> SessionInfo:
    """The row for the session directory `path`: never raises for a bad session, the row's
    `error` says why. With `first`, a row with no `name` also reads its first message."""
    cwd: str | None = None
    name: str | None = None
    forked_from: ForkedFrom | None = None
    updated: float | None = None
    text: str | None = None
    try:
        cwd, name, forked_from = _read_info(path)
        updated = (path / _LOG).stat().st_mtime
        if first and name is None:
            text = first_message(path)
    except (OSError, ValueError) as e:  # a row, not a stop: one bad session hides no other
        problem = e.strerror if isinstance(e, OSError) and e.strerror else str(e)
        return SessionInfo(path.name, path, cwd, name, forked_from, updated, None, problem)
    return SessionInfo(path.name, path, cwd, name, forked_from, updated, text, None)


def _scan(sessions: Path, *, first: bool) -> list[SessionInfo]:
    try:
        found = sorted(e for e in sessions.iterdir() if e.is_dir())
    except FileNotFoundError:
        return []  # no session was ever made here
    return [session_info(e, first=first) for e in found]


def _newest_first(rows: Sequence[SessionInfo]) -> list[SessionInfo]:
    return sorted(rows, key=lambda r: (r.updated is None, -(r.updated or 0.0), r.id))


def list_sessions(sessions: Path, *, cwd: Path | None = None) -> list[SessionInfo]:
    """The rows of `sessions`, newest `updated` first; those whose `cwd` equals `cwd` (resolved)
    exactly, or all with `cwd=None`. A directory that is not a readable session is a row with an
    `error` and is listed either way. Reads `info.json` and stats `log` per session, and the
    start of the log of a session with no name (its `first`). Anything that is not a directory is
    ignored."""
    here = None if cwd is None else str(cwd.resolve())
    rows = _scan(sessions, first=True)
    return _newest_first([r for r in rows if here is None or r.error or r.cwd == here])


# -- refs


def parse_ref(text: str) -> tuple[str, int | None]:
    """`SESSION` and the node of `SESSION:NODE`: `:NODE` is read only when the ref ends in `:`
    and decimal digits."""
    found = _NODE.match(text)
    if found is None:
        return text, None
    return found["session"], int(found["node"])


def _age(updated: float | None, now: float) -> str:
    if updated is None:
        return "-"
    seconds = max(0, int(now - updated))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return "now"


def age_of(row: SessionInfo, now: float | None = None) -> str:
    """How long ago `row` was updated, as `now`, `5m`, `3h` or `2d` (`-` for a row with no log)."""
    return _age(row.updated, time.time() if now is None else now)


def _candidates(rows: Sequence[SessionInfo]) -> str:
    return "\n".join(
        f"  {r.id}  {age_of(r):>4}  {r.cwd or '?'}" + (f"  {r.name}" if r.name else "")
        for r in _newest_first(rows)
    )


def _unique(rows: Sequence[SessionInfo], what: str, sessions: Path) -> SessionInfo | None:
    if len(rows) > 1:
        raise UnknownSession(
            f"{what} names {len(rows)} sessions in {sessions}:\n{_candidates(rows)}"
        )
    return rows[0] if rows else None


def find_session(sessions: Path, session: str, *, first: bool = False) -> SessionInfo:
    """The session `session` names, tried in this order: a path (it contains `/`), an id, an
    exact name, a unique id prefix. The whole of `sessions` is searched. A name or prefix of
    several sessions, or of none, is `UnknownSession`. With `first`, the row also has `first`."""
    if session in ("", ".", ".."):
        raise UnknownSession(f"{session!r} is not a session ref")
    if "/" in session:
        path = Path(session)
        if not path.is_dir():
            raise UnknownSession(f"no session at {session}")
        return session_info(path, first=first)
    if (sessions / session).is_dir():
        return session_info(sessions / session, first=first)
    rows = _scan(sessions, first=False)
    named = _unique([r for r in rows if r.name == session], f"the name {session!r}", sessions)
    if named is not None:
        return session_info(named.path, first=first)
    started = _unique(
        [r for r in rows if r.id.startswith(session)], f"the prefix {session!r}", sessions
    )
    if started is None:
        raise UnknownSession(f"no session {session!r} in {sessions}")
    return session_info(started.path, first=first)


def read_name(path: Path) -> str | None:
    """The name of the session in `path`, or `None`. `ValueError` if `info.json` is not readable."""
    return _read_info(path)[1]


def rename_session(path: Path, name: str) -> None:
    """Give the session in `path` the name `name`; an empty `name` removes it. No lease is taken:
    the file is replaced whole, and the last of two renames wins. `ValueError` if `info.json` is
    not readable."""
    cwd, _, forked_from = _read_info(path)
    _write_info(path, _info_data(cwd, name.strip() or None, forked_from))


# -- the first cursor


def first_cursor_node(session: Session) -> int:
    """The node a frontend would show, without writing: the open cursor with the lowest id's
    `at`, else the closed cursor with the lowest id's last node. What a fork or an export of a
    session with no node given copies. `LookupError` for a session with no cursor."""
    opened = session.cursors()
    if opened:
        return session.cursor(min(opened)).at
    closed = session.closed()
    if not closed:
        raise LookupError("the session has no cursor")
    return closed[0][1]


async def first_cursor(session: Session, client: "Client") -> int:
    """The cursor a frontend shows, or `-p` sends to: the open cursor with the lowest id; if none
    is open, the closed cursor with the lowest id, which `client` reopens (a new id with the old
    labels). The choice among other cursors is the frontend's (D16, D17)."""
    opened = session.cursors()
    if opened:
        return min(opened)
    closed = session.closed()
    if not closed:
        raise LookupError("the session has no cursor")
    return await client.reopen(closed[0][0])


# -- the session flags, resolved


def absolute_ref(ref: str, cwd: Path) -> str:
    """`ref` with its path made absolute against `cwd` if it is a path (its session part has a
    `/`), keeping a `:NODE`; a name, an id or a prefix is left as it is."""
    session, node = parse_ref(ref)
    if "/" not in session:
        return ref
    path = os.path.abspath(cwd / session)
    return path if node is None else f"{path}:{node}"


@dataclass(frozen=True)
class SessionFlags:
    """What the session flags say, as `tau.cli.session_flags` parses them: `directory`
    (`--session-dir`), `continue_last` (`-c`), `resume` (`-r`), `session` and `fork` (REFs),
    `no_session` and `name` (`-n`). At most one of the first five choices; `no_session` takes
    none of the others. `ConfigError` otherwise."""

    directory: Path | None = None
    continue_last: bool = False
    resume: bool = False
    session: str | None = None
    fork: str | None = None
    no_session: bool = False
    name: str | None = None

    def __post_init__(self) -> None:
        chosen = {
            "-c": self.continue_last,
            "-r": self.resume,
            "--session": self.session is not None,
            "--fork": self.fork is not None,
            "--no-session": self.no_session,
        }
        given = [flag for flag, on in chosen.items() if on]
        if len(given) > 1:
            raise ConfigError(f"{' and '.join(given)} cannot be combined: each chooses the session")
        if self.no_session and (self.directory is not None or self.name is not None):
            raise ConfigError("--no-session keeps nothing, so it takes no --session-dir or -n")

    @classmethod
    def of(cls, args: object, cwd: Path) -> "SessionFlags":
        """The flags `session_flags()` parsed into `args`, with `--session-dir` and a path REF
        made absolute against `cwd`, the directory they were typed in."""

        def option(name: str) -> object:
            return getattr(args, name)

        def absolute(ref: object) -> str | None:
            return None if ref is None else absolute_ref(cast("str", ref), cwd)

        directory = option("session_dir")
        return cls(
            None if directory is None else Path(os.path.abspath(cwd / cast("str", directory))),
            cast("bool", option("continue_last")),
            cast("bool", option("resume")),
            absolute(option("session")),
            absolute(option("fork")),
            cast("bool", option("no_session")),
            cast("str | None", option("name")),
        )


@dataclass(frozen=True)
class SessionChoice:
    """What the session flags resolved to, with nothing written: a `new` session, an `existing`
    one (`source`) or a `fork` of `source` at `node` (`None`: the first cursor's); `sessions` is
    the sessions directory (`None` for `--no-session`: in memory); `cwd` is the working
    directory the program changes into; `name` is `-n`."""

    kind: Literal["new", "existing", "fork"]
    sessions: Path | None
    cwd: Path
    source: SessionInfo | None = None
    node: int | None = None
    name: str | None = None


PickSession = Callable[[Sequence[SessionInfo], Sequence[SessionInfo]], SessionInfo]
"""What `-r` asks of a frontend: given the rows of the working directory and all rows (newest
first), the one to open."""


def _openable(info: SessionInfo) -> SessionInfo:
    if info.error is not None:
        raise NotASession(f"{info.path}: {info.error}")
    cwd = Path(cast("str", info.cwd))
    if not cwd.is_dir():
        raise FileNotFoundError(
            f"session {info.id}'s working directory {cwd} no longer exists; "
            f"`tau export {info.id}` still reads it"
        )
    return info


def _resumed(sessions: Path, here: Path, pick: PickSession | None) -> SessionInfo:
    if pick is None:
        raise ConfigError("-r needs a terminal to choose from; name the session: --session REF")
    everywhere = list_sessions(sessions)
    nearby = [r for r in everywhere if r.cwd == str(here) or r.error]
    if not everywhere:
        raise UnknownSession(f"no session in {sessions} to resume")
    return pick(nearby, everywhere)


def choose_session(
    flags: SessionFlags, *, home: Path, cwd: Path, pick: PickSession | None = None
) -> SessionChoice:
    """Resolve `flags` for a program started in `cwd` (the directory the user typed the flags
    in) with `TAU_HOME` `home`. Reads the sessions directory and writes nothing. `-r` needs
    `pick`. `UnknownSession`, `NotASession` for a session whose `info.json` is unreadable,
    `FileNotFoundError` for one whose working directory is gone, `ConfigError` for `--session`
    with a `:NODE`."""
    here = cwd.resolve()
    if flags.no_session:
        return SessionChoice("new", None, here)
    sessions = sessions_directory(home, flags.directory)
    if flags.continue_last:
        rows = [r for r in list_sessions(sessions, cwd=here) if not r.error]
        if not rows:
            raise UnknownSession(f"no session for {here} in {sessions} to continue")
        return SessionChoice(
            "existing", sessions, Path(cast("str", rows[0].cwd)), rows[0], name=flags.name
        )
    if flags.resume:
        info = _openable(_resumed(sessions, here, pick))
        return SessionChoice(
            "existing", sessions, Path(cast("str", info.cwd)), info, name=flags.name
        )
    if flags.session is not None:
        session, node = parse_ref(flags.session)
        if node is not None:
            raise ConfigError(
                f"--session takes no node; to start from node {node}, copy it: "
                f"--fork {session}:{node}"
            )
        info = _openable(find_session(sessions, session))
        return SessionChoice(
            "existing", sessions, Path(cast("str", info.cwd)), info, name=flags.name
        )
    if flags.fork is not None:
        session, node = parse_ref(flags.fork)
        info = _openable(find_session(sessions, session))
        return SessionChoice("fork", sessions, Path(cast("str", info.cwd)), info, node, flags.name)
    return SessionChoice("new", sessions, here, name=flags.name)
