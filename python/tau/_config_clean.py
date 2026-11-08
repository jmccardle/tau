"""`tau config check --clean` and `--purge` (D13): take out of a config file what it names that
τ does not have, or what an extension no longer needs.

An extension may be just a file that gets deleted, so a config can name things that are gone, and
τ cannot rely on an extension to clean up after itself. Loading only warns about them
(`option_problems`); this removes them, from the layer files the person names and nowhere else.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tau._config import ConfigError
from tau._config_layers import ModelCatalog
from tau._options import make_options, option_fields
from tau._trust import write_atomically
from tau._values import Context

Layer = dict[str, object]


@dataclass(frozen=True)
class Cleaning:
    """What the person asked to be taken out: `clean` (everything the check warns the layer
    names that is missing or invalid) and `purge` (these extensions, whatever they hold)."""

    clean: bool
    purge: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.clean or bool(self.purge)


@dataclass(frozen=True)
class Against:
    """What a start has, which a layer file is cleaned against: the `catalog` the layers make,
    the `sources` (every extension source's key), the loaded `extensions` and their `tools`."""

    catalog: ModelCatalog
    sources: Collection[str]
    extensions: Sequence[object]
    tools: Sequence[str]


def clean_layer(data: Layer, cleaning: Cleaning, against: Against) -> list[str]:
    """Edit `data` (one layer file's JSON object) as `cleaning` asks. Returns one line per thing
    removed; `data` is unchanged if there are none."""
    removed: list[str] = []
    options = cast("dict[str, object]", data.get("extensions") or {})
    switches = cast("dict[str, object]", data.get("load") or {})
    for name in cleaning.purge:
        removed += _purge(name, options, switches, against.sources)
    if cleaning.clean:
        removed += _clean_load(switches, against.sources)
        removed += _clean_extensions(options, against)
    for key, value in (("extensions", options), ("load", switches)):
        if key in data and not value:
            del data[key]
        elif value:
            data[key] = value
    return removed


def _clean_load(switches: Layer, sources: Collection[str]) -> list[str]:
    """Remove the `load` keys that no source has."""
    gone = [key for key in switches if key not in sources]
    for key in gone:
        del switches[key]
    return [f"load.{key}" for key in gone]


def _clean_extensions(options: Layer, against: Against) -> list[str]:
    """Remove the options of extensions that are not loaded or take none, and the invalid parts
    of the others'."""
    removed: list[str] = []
    by_name = {str(getattr(e, "name")): e for e in against.extensions}  # noqa: B009
    for name in list(options):
        mine = options[name]
        ext = by_name.get(name)
        if ext is None or getattr(ext, "options", None) is None or not isinstance(mine, dict):
            del options[name]
            removed.append(f"extensions.{name}")
        else:
            removed += _clean_options(name, ext, cast("Layer", mine), against)
    return removed


def _purge(name: str, options: Layer, switches: Layer, sources: Collection[str]) -> list[str]:
    """Take `name`'s options out, and turn it off if it is a source (else drop its `load` key)."""
    removed: list[str] = []
    if name in options:
        del options[name]
        removed.append(f"extensions.{name}")
    if name in sources:
        if switches.get(name) is not False:
            switches[name] = False
            removed.append(f"load.{name} set to false")
    elif name in switches:
        del switches[name]
        removed.append(f"load.{name}")
    return removed


def _clean_options(name: str, ext: object, mine: Layer, against: Against) -> list[str]:
    """Remove from `mine` (the layer's options of the loaded extension `ext`) the fields it does
    not have, those its type refuses, and the list items that name a missing tool."""
    options = cast("type", getattr(ext, "options"))  # noqa: B009
    fields = {f.name: f for f in option_fields(name, options)}
    context = Context(against.catalog)
    removed: list[str] = []
    for key in list(mine):
        field = fields.get(key)
        problem = "is not an option" if field is None else field.kind.check(mine[key], context)
        if problem is not None:
            del mine[key]
            removed.append(f"extensions.{name}.{key}")
    try:
        made = make_options(name, options, tuple(fields.values()), mine, context)
    except ConfigError:
        return removed  # what is left still does not fit as a whole; the check says so
    missing = getattr(made, "missing_tools", None)
    lost: dict[str, list[str]] = {} if missing is None else missing(against.tools)
    for key, gone in lost.items():
        if key in mine:
            mine[key] = [e for e in cast("list[str]", mine[key]) if e not in gone]
            removed += [f"extensions.{name}.{key}: {item!r}" for item in gone]
    return removed


def clean_files(files: Mapping[str, Path], cleaning: Cleaning, against: Against) -> list[str]:
    """Clean each layer file in `files` (a name to its path; a path with no file is skipped) and
    write the ones that changed, atomically. Returns the lines to tell the person: a file's path,
    then what was removed from it."""
    lines: list[str] = []
    for layer, path in files.items():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ConfigError(f"{path}: cannot be cleaned: {e}") from None
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: cannot be cleaned: not a JSON object")
        removed = clean_layer(cast("Layer", data), cleaning, against)
        if not removed:
            continue
        body = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        write_atomically(path.resolve(), body)
        lines.append(f"{layer} config {path}: removed")
        lines += [f"  {item}" for item in removed]
        if layer == "project":
            lines.append("  the project's file changed: run `tau trust` to approve it again")
    return lines
