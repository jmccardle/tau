"""An extension's administrator constants (akb #643): settings of how the harness runs an
extension, read from the config layers when the extension is built, never options. A client
configures what an extension offers it (`/config`, a `Config` node, `set_options`), and only an
extension with `options` has any; one that reads `extensions.NAME` through `constants` has none, so
a client cannot set these, and they never appear in the `config` form."""

from collections.abc import Mapping

from tau._config import ConfigError

_WORDS = {bool: "true or false", int: "a whole number", str: "a string"}


def constants(
    extensions: Mapping[str, Mapping[str, object]], name: str, known: Mapping[str, type]
) -> dict[str, object]:
    """The settings the config layers give extension `name`: `known` maps each key it has to its
    type (`bool`, `int`, `str`). `ConfigError` for a key it has not, and for a value of another
    type (a `bool` is no `int`)."""
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
