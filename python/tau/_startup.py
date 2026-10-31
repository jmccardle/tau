"""What a frontend does before the first sample (D13): load the config, pick a model.

`load_config` finds the user config and the project, gets the project trusted, and
reads the catalog and the environment for keys. `select_model` turns a requested
name (or `default_model`) into one qualified name. `prompts` says whether a person
can be asked, and how.
"""

import difflib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from tau._config import ConfigError
from tau._config_layers import ModelCatalog, read_models
from tau._trust import (
    Trusted,
    ensure_trusted,
    find_project,
    is_interactive,
    project_environment,
    safe_text,
    tau_home,
    terminal_ask,
    user_config_path,
)
from tau._user_config import UserConfig

Ask = Callable[[str], bool]
Pick = Callable[[tuple[str, ...]], str]


@dataclass(frozen=True)
class LoadedConfig:
    """The result of `load_config`: the `catalog` of models, `environ` (the shell
    environment over a trusted project's `.env`; where keys are read from), the trusted
    `project` (`None`: there is none), `home` (`TAU_HOME`), and the `user` config: the
    layers read again after τ writes the user's file (`Driver(user_config=)`)."""

    catalog: ModelCatalog
    environ: Mapping[str, str]
    project: Trusted | None
    home: Path
    user: UserConfig


def load_config(
    cwd: Path,
    environ: Mapping[str, str],
    *,
    ask: Ask | None,
    cli: Mapping[str, object] | None = None,
) -> LoadedConfig:
    """The layers for a frontend started in `cwd`: user config, the project's if trusted,
    then `cli` overrides.

    `ask` is the live terminal's approval question (`prompts(...).ask`); without one an
    untrusted project raises `UntrustedProject`. `ConfigError` and `TrustError` propagate
    unchanged. The returned `environ` is `environ` over the trusted project's `.env`.
    """
    home = tau_home(environ)
    user = user_config_path(home)
    root = find_project(cwd, environ).root
    project = None if root is None else ensure_trusted(root, home, ask=ask)
    catalog = project.read_models(user, cli) if project else read_models(user, cli=cli)
    user_layers = UserConfig(home, None if project is None else project.layer(), cli)
    return LoadedConfig(catalog, project_environment(project, environ), project, home, user_layers)


def _close(catalog: ModelCatalog, name: str) -> list[str]:
    bare = {q.rsplit(":", 1)[-1] for q in catalog.names()}
    found = difflib.get_close_matches(name, [*catalog.names(), *bare], n=3, cutoff=0.5)
    out: dict[str, None] = {}
    for hit in found:
        for q in catalog.find(hit):
            out[q] = None
    return list(out)


def select_model(catalog: ModelCatalog, requested: str | None = None, *, pick: Pick | None) -> str:
    """The qualified name to use: `requested` (`--model`), else the catalog's `default_model`.

    A name with `:` must be exactly a qualified name. A bare name with one match is that
    match; with several, `pick` (a live terminal) chooses among the qualified names and
    without one `ConfigError` lists them. No match is a `ConfigError` with close names.
    """
    if requested is not None and not requested:
        raise ConfigError("the model name must not be empty")
    name = requested if requested is not None else catalog.default_model
    if not name:
        raise ConfigError(
            'no model chosen: pass --model NAME, or set "default_model" in your config'
        )
    found = catalog.find(name)
    if len(found) == 1:
        return found[0]
    if not found:
        close = ", ".join(safe_text(q) for q in _close(catalog, name))
        hint = f"; did you mean {close}?" if close else ""
        available = ", ".join(safe_text(q) for q in catalog.names())
        raise ConfigError(f"model {name!r} is not in your config{hint} (available: {available})")
    if pick is None:
        listed = ", ".join(safe_text(q) for q in found)
        raise ConfigError(f"model {name!r} is ambiguous; use one of: {listed}")
    return pick(found)


def terminal_pick(stdin: TextIO, stdout: TextIO) -> Pick:
    """A `Pick` that lists the qualified names numbered on `stdout` and reads a number or a
    name from `stdin`. Anything else, or end of input, is a `ConfigError`."""

    def pick(names: tuple[str, ...]) -> str:
        stdout.write("Several models match; choose one:\n")
        for i, q in enumerate(names, 1):
            stdout.write(f"  {i}. {safe_text(q)}\n")
        stdout.write("number: ")
        stdout.flush()
        line = stdin.readline()
        if not line:  # end of input: leave the prompt line
            stdout.write("\n")
            raise ConfigError("no model chosen: no answer")
        answer = line.strip()
        if answer in names:
            return answer
        if answer.isdecimal() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        raise ConfigError(f"no model chosen: {answer!r} is not one of 1-{len(names)}")

    return pick


@dataclass(frozen=True)
class Prompts:
    """What a person can be asked: `ask` (trust a project) and `pick` (one of several
    models). Both are `None` unless stdin and stdout are terminals."""

    ask: Ask | None
    pick: Pick | None


def prompts(stdin: TextIO, stdout: TextIO) -> Prompts:
    """The `Prompts` for these streams."""
    if not is_interactive(stdin, stdout):
        return Prompts(None, None)
    return Prompts(terminal_ask(stdin, stdout), terminal_pick(stdin, stdout))
