"""What a frontend does before the first sample (D13): load the config, pick a model.

`load_config` finds the user config and the project, gets the project trusted, and
reads the catalog and the environment for keys. `select_model` turns a requested
name (or `default_model`) into one qualified name. `prompts` says whether a person
can be asked, and how.
"""

import difflib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, cast

from tau._config import ConfigError
from tau._config_layers import ModelCatalog, read_models
from tau._config_tree import ConfigProblem
from tau._options import option_fields
from tau._project_read import safe_text
from tau._trust import (
    Trusted,
    ensure_trusted,
    find_project,
    is_interactive,
    project_environment,
    tau_home,
    terminal_ask,
    user_config_path,
)
from tau._user_config import UserConfig
from tau._values import Field, Json, object_schema
from tau.commandline import value

if TYPE_CHECKING:
    from tau._extensions import ExtensionSource

Ask = Callable[[str], bool]
"""A yes-or-no question put to the person: takes the question, returns the answer."""
Pick = Callable[[tuple[str, ...]], str]
"""A choice put to the person: takes the qualified model names, returns one of them."""


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


def option_changes(
    extensions: Sequence[object], assignments: Sequence[tuple[str, str, str]]
) -> dict[str, tuple[type, tuple[Field, ...], Json]]:
    """`EXTENSION.FIELD=VALUE` assignments (as `(extension, field, text)`) read against the
    `extensions`' options: per extension, its options type, its fields and the JSON values set,
    each text read as `/config` reads it (`tau.commandline.value`). `ConfigError` for an
    extension without options, a field it does not have, or a text its field refuses."""
    known = {cast("str", getattr(e, "name")): e for e in extensions}  # noqa: B009
    changes: dict[str, tuple[type, tuple[Field, ...], Json]] = {}
    for extension, name, given in assignments:
        if extension == "load":
            raise ConfigError(
                f"load.{name} is not an extension option: `tau ext disable {name}` writes "
                f"load.{name} = false to your config file, and `tau ext enable {name}` undoes "
                f"it (`--set load.{name}=false` does it for one run)"
            )
        options = getattr(known.get(extension), "options", None)
        if options is None:
            having = [n for n, e in known.items() if getattr(e, "options", None) is not None]
            raise ConfigError(f"extension {extension!r} has no options; those that do: {having}")
        fields = option_fields(extension, options)
        properties = cast("Json", object_schema(fields, model=False)["properties"])
        if name not in properties:
            raise ConfigError(
                f"extension {extension!r} has no option {name!r}; its options: {list(properties)}"
            )
        try:
            decoded = value(given, cast("Json", properties[name]))
        except ValueError as e:
            raise ConfigError(f"{extension}.{name}: {e}") from None
        changes.setdefault(extension, (options, fields, {}))[2][name] = decoded
    return changes


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


def off_keys(sources: Sequence["ExtensionSource"]) -> set[str]:
    """The keys of the sources that exist but are not loaded: their options are kept quietly."""
    return {s.key for s in sources if s.kind != "load" and s.state not in ("on", "always")}


def load_key_warnings(
    catalog: ModelCatalog, sources: Sequence["ExtensionSource"]
) -> list[ConfigProblem]:
    """A warning for each `load` key no source has (the rows `_load_keys` made)."""
    return [
        ConfigProblem("warning", s.detail or "", catalog.load_sources[s.key], f"load.{s.key}")
        for s in sources
        if s.kind == "load" and s.state == "off"
    ]
