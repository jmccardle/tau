"""A throwaway home directory, and the ways a test starts τ in it. Never the real home."""

import io
import json
import os
import select
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath

import pytest

from tau import (
    Author,
    Body,
    LoadedConfig,
    Session,
    UntrustedProject,
    builtin_providers,
    load_config,
    model_config,
    prompts,
)
from tau.cli import main

Ask = Callable[[str], bool]
Pick = Callable[[tuple[str, ...]], str]
URL = "http://localhost:8100/v1"
PROVIDERS = builtin_providers({})  # the names `check_config` accepts; nothing is sampled
EOF = "\x04"  # Ctrl-D at the start of a line, on a terminal


@dataclass(frozen=True)
class World:
    home: Path
    tau: Path  # TAU_HOME
    environ: dict[str, str]

    def project(self, name: str = "proj", config: object | None = None) -> Path:
        if sys.platform == "win32":
            pytest.skip("Windows: projects (D13 trust) are not ported; finding one is an error")
        root = self.home / name
        (root / ".tau").mkdir(parents=True)
        if config is not None:
            text = config if isinstance(config, str) else json.dumps(config)
            (root / ".tau" / "config.json").write_text(text, encoding="utf-8")
        return root

    def session(  # noqa: PLR0913 - the fields of a session directory, as the catalog writes them
        self,
        id: str,
        *,
        cwd: PurePath | None = None,
        name: str | None = None,
        forked_from: dict[str, object] | None = None,
        says: Sequence[str] = (),
        build: Callable[[Session, int], int] | None = None,
        system: str = "sys",
        age: float = 0,
        sessions: Path | None = None,
    ) -> Path:
        """A session directory as D18 lays it out, written by hand: `info.json`, and a log from
        `Session.create` with `says` as user messages along one branch, then whatever `build`
        appends (given the session and its tip, returning the new tip), `age` seconds old. Cursor
        0 is left on the tip. Not held by anyone."""
        directory = (sessions or self.tau / "sessions") / id
        directory.mkdir(parents=True, mode=0o700)
        info: dict[str, object] = {"cwd": str(cwd or self.home)}
        if name is not None:
            info["name"] = name
        if forked_from is not None:
            info["forked_from"] = forked_from
        (directory / "info.json").write_text(json.dumps(info), encoding="utf-8")
        made = Session.create(model_config("m"), system, 0, directory)
        tip = made.cursor(0).at
        for text in says:
            tip = made.append(tip, Body.User(text), Author.Frontend("t"), 0)
        if build is not None:
            tip = build(made, tip)
        made.go_to(0, tip, "t", 0)
        made.close()
        when = time.time() - age
        os.utime(directory / "log", (when, when))
        return directory

    def user_config(self, config: object) -> Path:
        self.tau.mkdir(mode=0o700, exist_ok=True)
        path = self.tau / "config.json"
        path.write_text(config if isinstance(config, str) else json.dumps(config), encoding="utf-8")
        return path


def models(**named: dict[str, object]) -> dict[str, object]:
    return {"models": {k: {"model": k, "base_url": URL, **v} for k, v in named.items()}}


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str

    @property
    def both(self) -> str:
        return self.out + self.err


def tau(world: World, *argv: str, cwd: Path | None = None, stdin: str = "") -> Result:
    """The `tau` command with real files and in-memory streams."""
    out, err = io.StringIO(), io.StringIO()
    code = main(
        list(argv),
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        environ=world.environ,
        cwd=cwd or world.home,
    )
    return Result(code, out.getvalue(), err.getvalue())


def trust(world: World, root: Path) -> None:
    """Approve the project at `root` the way a person does: `tau trust`."""
    result = tau(world, "trust", cwd=root)
    assert result.code == 0, result.err


def yes(_report: str) -> bool:
    return True


def has_project(world: World, cwd: Path) -> bool:
    """Whether a frontend started in `cwd` finds a project (an untrusted one still counts)."""
    try:
        return load_config(cwd, world.environ, ask=None).project is not None
    except UntrustedProject:
        return True


def project_root(world: World, cwd: Path) -> Path | None:
    """The trusted project a frontend started in `cwd` loads, if any."""
    project = load_config(cwd, world.environ, ask=None).project
    return None if project is None else project.root


@dataclass(frozen=True)
class Terminal:
    """What a person at a pseudo-terminal can be asked, and what they have been shown."""

    ask: Ask | None
    pick: Pick | None
    master: int

    def screen(self) -> str:
        """Everything written to the terminal so far (a typed line echoes too)."""
        seen = b""
        while select.select([self.master], [], [], 0.2)[0]:
            seen += os.read(self.master, 4096)
        return seen.decode("utf-8", "replace")


@contextmanager
def terminal(typed: str) -> Iterator[Terminal]:
    """A pseudo-terminal that has been typed `typed` (`EOF`: end of input)."""
    if sys.platform == "win32":
        pytest.skip("Windows: no pseudo-terminals (os.openpty is Unix only)")
    master, slave = os.openpty()
    with (
        os.fdopen(os.dup(slave), "w") as tty_out,
        os.fdopen(slave, "r") as tty_in,
        os.fdopen(master, "w") as keys,
    ):
        live = prompts(tty_in, tty_out)
        keys.write(typed)
        keys.flush()
        yield Terminal(live.ask, live.pick, master)


def loaded(world: World, cwd: Path | None = None) -> LoadedConfig:
    return load_config(cwd or world.home, world.environ, ask=None)
