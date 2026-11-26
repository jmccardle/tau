"""An extension's **administrator keys** (akb #643): the keys of `extensions.NAME` in the config
files that say how the harness runs an extension on this machine or in this project, read when the
extension is built, never options. A client configures what an extension offers it (`/config`, a
`Config` node, `set_options`); an administrator writes these in a config file (by hand, or `tau
config set NAME.KEY=VALUE`), and no client can read or set them.

An extension declares them as `administrator_keys`, each key's type (`bool`, `int`, `str`), and
reads them with `constants`. An extension has options or administrator keys under
`extensions.NAME`, not both: that object is its options when it has `options`. One that needs
both keeps the administrator's values in its settings file (`settings.py`)."""

from collections.abc import Mapping, Sequence

from tau._config import ConfigError
from tau._config_tree import ConfigProblem

_WORDS = {bool: "true or false", int: "a whole number", str: "a string"}
_SCHEMAS = {bool: "boolean", int: "integer", str: "string"}

NOT_OPTIONS = (
    "an extension's administrator keys are not options, and no client sets them: they are in a "
    "config file (`tau config set NAME.KEY=VALUE` writes yours)"
)
"""Said where a client asks to configure an extension that has no options."""


def constants(
    extensions: Mapping[str, Mapping[str, object]], name: str, known: Mapping[str, type]
) -> dict[str, object]:
    """The administrator keys the config layers set for extension `name` (`extensions` is
    `LoadedConfig.catalog.extensions`): `known` maps each key it has to its type (`bool`, `int`,
    `str`). `ConfigError` for a key it has not, and for a value of another type (a `bool` is no
    `int`)."""
    found: dict[str, object] = {}
    for key, value in extensions.get(name, {}).items():
        kind = known.get(key)
        where = f"extensions.{name}.{key}"
        if kind is None:
            raise ConfigError(
                f"{where}: {name!r} has no such setting; its settings are {', '.join(known)}"
            )
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ConfigError(f"{where} must be {_WORDS[kind]}, not {value!r}")
        found[key] = value
    return found


def administrator_keys(extension: object) -> Mapping[str, type] | None:
    """The administrator keys `extension` declares, or `None` for none."""
    return getattr(extension, "administrator_keys", None)


def key_schema(kind: type) -> dict[str, object]:
    """The JSON schema a command line reads a key of type `kind` by (`tau config set`)."""
    return {"type": _SCHEMAS[kind]}


def key_problems(
    given: Mapping[str, Mapping[str, object]],
    extensions: Sequence[object],
    sources: Mapping[str, str],
) -> list[ConfigProblem]:
    """An `error` for each of `extensions` whose administrator keys `given` sets wrong, as its own
    build would refuse them; `sources` names the layer that set each extension's."""
    problems: list[ConfigProblem] = []
    for extension in extensions:
        name = str(getattr(extension, "name"))  # noqa: B009
        if (known := administrator_keys(extension)) is not None:
            try:
                constants(given, name, known)
            except ConfigError as e:
                path = f"extensions.{name}"
                problems.append(ConfigProblem("error", str(e), sources.get(name), path))
    return problems
