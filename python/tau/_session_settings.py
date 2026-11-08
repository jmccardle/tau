"""Session settings (D13): the layer nearest the run, for this start only and never written.

It is the third config layer (user, project, session), and it holds the same shape as a config
file's `extensions` and `load` keys, merged with the same JSON rules. It has two inputs, merged
with `--set` over the environment variable:

- `TAU_SETTINGS`, a JSON object: `{"load": {"retry": false}, "extensions": {"step_limit":
  {"limit": 5}}}`. For scripted runs, where a flag per setting would not carry through.
- `--set PATH=VALUE`, repeatable. `load.KEY=true|false` switches an extension source for this run,
  as `load.KEY` does in a file. `EXTENSION.FIELD=VALUE` sets an option, its value read as `/config`
  reads it and checked by the extension's own option type once the extension is loaded.

Nothing else is a session setting: models, `default_model` and `tui` belong in the files, and
`--system-prompt` is its own flag.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from tau._config import ConfigError

ENVIRONMENT = "TAU_SETTINGS"
"""The environment variable that carries session settings as a JSON object."""
KEYS = ("load", "extensions")
"""The config keys a session may set."""
LOAD = "load"
"""The first word of a `--set` that switches an extension source."""


def merged(base: Mapping[str, object], over: Mapping[str, object]) -> dict[str, object]:
    """`over` laid on `base`: objects merge by key, anything else in `over` replaces (a `null`
    stays, so the layers above drop the inherited key, as in a file)."""
    out = dict(base)
    for key, value in over.items():
        before = out.get(key)
        if isinstance(value, dict) and isinstance(before, dict):
            out[key] = merged(cast("dict[str, object]", before), cast("dict[str, object]", value))
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class SessionSettings:
    """The session layer of one start. `layer` is what is known before any extension loads (the
    environment variable's object, with the `load` switches of `--set` laid on it); `options` are
    the `--set` assignments of extension options, read once the extensions are loaded; `switched`
    are the `load` keys `--set` named."""

    layer: dict[str, object]
    options: tuple[tuple[str, str, str], ...]
    switched: tuple[str, ...]

    @classmethod
    def of(
        cls, environ: Mapping[str, str], assignments: Sequence[tuple[str, str, str]]
    ) -> "SessionSettings":
        """Read `TAU_SETTINGS` and split `assignments` (as `(first word, rest, value)`).
        `ConfigError` for a variable that is not a JSON object of `KEYS`, or a `load` value that
        is not `true` or `false`."""
        layer = _from_environment(environ.get(ENVIRONMENT))
        switches: dict[str, object] = {}
        options: list[tuple[str, str, str]] = []
        for first, rest, given in assignments:
            if first != LOAD:
                options.append((first, rest, given))
            elif given in ("true", "false"):
                switches[rest] = given == "true"
            else:
                raise ConfigError(f"{LOAD}.{rest}: {given!r} is not true or false")
        return cls(
            merged(layer, {LOAD: switches} if switches else {}), tuple(options), tuple(switches)
        )

    def refuse_missing(self, keys: Sequence[str]) -> None:
        """`ConfigError` for a `--set load.KEY` that names no extension source (`keys` are the
        sources there are): a flag typed by a person is a typo, where a file may name an
        extension that was deleted."""
        for key in self.switched:
            if key not in keys:
                raise ConfigError(
                    f"--set load.{key}: no extension source has the key {key!r}; "
                    f"`tau ext list` shows them"
                )

    def with_options(
        self, cli: Mapping[str, object] | None, changes: Mapping[str, Mapping[str, object]]
    ) -> dict[str, object]:
        """`cli` (the layer the start has built, this one included) with the `--set` option
        values `changes` laid on it."""
        return merged(cli or {}, {"extensions": {name: dict(v) for name, v in changes.items()}})


def _from_environment(text: str | None) -> dict[str, object]:
    if text is None or not text.strip():
        return {}
    try:
        found = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"{ENVIRONMENT} is not JSON: {e}") from None
    if not isinstance(found, dict):
        raise ConfigError(
            f'{ENVIRONMENT} must be a JSON object, such as {{"load": {{"x": false}}}}'
        )
    for key in cast("dict[str, object]", found):
        if key not in KEYS:
            raise ConfigError(
                f"{ENVIRONMENT} sets {key!r}; a session may set only {list(KEYS)} "
                f"(models and the rest belong in the config files)"
            )
    return cast("dict[str, object]", found)
