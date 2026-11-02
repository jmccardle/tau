"""Resolving the session flags to the one thing a start does (D18 section 5, step 0): a new
session, an existing one or a fork. Reads the sessions directory and writes nothing."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from tau._config import ConfigError
from tau._core import NotASession
from tau._refs import parse_ref
from tau._sessions import (
    PickSession,
    SessionFlags,
    SessionInfo,
    UnknownSession,
    find_session,
    list_sessions,
    sessions_directory,
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
