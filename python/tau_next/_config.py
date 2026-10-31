"""Model configuration (D13): a `Config` node names a model; the driver resolves it.

`model_config` and `model_name` write and read `Config` nodes' `data` (a JSON object core
never reads; glossary: config): `{"model": "<qualified name>"}`, and for a branch's
extensions `{"extensions": {"<name>": {"<option>": value}}}` (D16 section 4). Core's
`config_at` is the nearest `Config` only, so a node holds just what it changes and a reader
folds the walk: the model is the last one named, an option the last one set. `ModelConfig`
is the resolved model a provider is handed. `read_models` and `check_config` (in
`config_layers`) read the layered files: user, project, CLI overrides.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast
from urllib.parse import urlparse

from tau_next._core import Body, Config, Node


class ConfigError(ValueError):
    """A `Config`'s data that does not name a model, a model that is not in the config, an
    invalid resolved model, or a key that cannot be read."""


def _text(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"config {key!r} must be a string, not {type(value).__name__}")
    if not value:
        raise ConfigError(f"config {key!r} must not be empty")
    return value


def url_problem(value: str) -> str | None:
    """Why `value` is not an http(s) URL with a host, or `None` if it is one."""
    try:
        url = urlparse(value)
    except ValueError as e:  # `http://[x/v1` and the like
        return f"config 'base_url' is not a valid URL ({e}): {value!r}"
    if url.scheme not in ("http", "https") or not url.netloc:
        return f"config 'base_url' must be an http(s) URL, not {value!r}"
    return None


def _freeze(value: object) -> object:
    """`value` (JSON) with every object read-only and every list a tuple, as a copy."""
    if isinstance(value, Mapping):
        items = cast("Mapping[str, object]", value)
        return MappingProxyType({k: _freeze(v) for k, v in items.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in cast("list[object]", value))
    return value


def key_problem(key: str) -> str | None:
    """What keeps `key` from travelling in an HTTP header, or `None`. Never quotes the key."""
    for c in key:
        if c == " ":
            return "contains a space; an API key has none"
        if c.isascii() and not c.isprintable():
            return "contains a control character (a newline, carriage return or NUL?)"
        if not c.isascii():
            return "contains a non-ASCII character (a smart quote?)"
    return None


@dataclass(frozen=True)
class ModelConfig:
    """A resolved model: which model to ask, where (`base_url`, optional: a provider that
    needs one says so in its `check`), the *name* of the variable holding its API key,
    `max_tokens` (optional, positive), and `options`: a JSON object only the named provider
    reads, which the config loader never looks inside (D15). The driver builds it from the
    catalog at each sample; no node holds it.

    The key itself is never a field: it is read from the environment at sample
    time (`api_key`) and so never reaches a node or the log.
    """

    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    provider: str = "openai"
    literal_api_key: str | None = field(default=None, repr=False)
    max_tokens: int | None = None
    options: Mapping[str, object] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        _text(self.model, "model")
        if self.base_url is not None:
            problem = url_problem(_text(self.base_url, "base_url"))
            if problem is not None:
                raise ConfigError(problem)
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)  # pyright: ignore[reportUnnecessaryIsInstance]
            or self.max_tokens < 1
        ):
            raise ConfigError(
                f"config 'max_tokens' must be a positive integer, not {self.max_tokens!r}"
            )
        if not isinstance(self.options, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ConfigError(
                f"config 'options' must be an object, not {type(self.options).__name__}"
            )
        # A deep read-only copy: neither the caller's dict nor the catalog can change under us.
        object.__setattr__(self, "options", _freeze(self.options))
        if self.api_key_env is not None:
            _text(self.api_key_env, "api_key_env")
        _text(self.provider, "provider")
        if self.literal_api_key is not None:
            _text(self.literal_api_key, "api_key")

    def api_key(self, environ: Mapping[str, str]) -> str | None:
        """The key in `environ` named by `api_key_env`; `None` if no variable is named.

        A literal `api_key` from a config file wins. Raises `ConfigError` if a
        variable is named and is unset or empty, or if the key has a character outside
        visible ASCII (it could not travel in an HTTP header). The error never holds the key.
        """
        if self.literal_api_key is not None:
            if (kind := key_problem(self.literal_api_key)) is not None:
                raise ConfigError(f"the literal api_key {kind}")
            return self.literal_api_key
        if self.api_key_env is None:
            return None
        key = environ.get(self.api_key_env)
        if not key:
            raise ConfigError(f"environment variable {self.api_key_env} is not set")
        if (kind := key_problem(key)) is not None:
            raise ConfigError(f"environment variable {self.api_key_env} {kind}")
        return key


def config_node(
    model: str | None = None, extensions: Mapping[str, Mapping[str, object]] | None = None
) -> Config:
    """The `Config` node body that sets `model` (a catalog's qualified name) and/or the
    options in `extensions` (`{"step_limit": {"limit": 10}}`), and nothing else. `ConfigError`
    if it sets neither."""
    fields: dict[str, object] = {}
    if model is not None:
        fields["model"] = _text(model, "model")
    if extensions:
        fields["extensions"] = extensions
    if not fields:
        raise ConfigError("a Config node sets a model or some extension options")
    return Config(json.dumps(fields))


def model_config(
    qualified: str, extensions: Mapping[str, Mapping[str, object]] | None = None
) -> Config:
    """The `Config` node body naming the model `qualified` (a catalog's qualified name).
    It holds that name and nothing resolved from the file. `extensions` adds options
    (`{"step_limit": {"limit": 10}}`) that hold from this node on."""
    return config_node(qualified, extensions)


@dataclass(frozen=True)
class ConfigFields:
    """What one `Config` node sets: a `model` (qualified name) and/or `extensions` options."""

    model: str | None
    extensions: Mapping[str, Mapping[str, object]]


def config_fields(config: Config) -> ConfigFields:
    """What `config` sets. `ConfigError` for any other shape: not JSON, not an object, a key
    other than `model` and `extensions`, a `model` that is not a non-empty string, or
    `extensions` that are not an object of objects."""
    try:
        data: object = json.loads(config.data)
    except ValueError as e:
        raise ConfigError(f"config data is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"config data must be a JSON object, not {type(data).__name__}")
    fields = cast("dict[str, object]", data)
    extra = sorted(str(k) for k in fields if k not in ("model", "extensions"))
    if extra:
        raise ConfigError(
            f'config data holds only "model" and "extensions"; unexpected keys {extra}; '
            "a session from before D13 names no model: append a root with model_config(name)"
        )
    model = _text(fields["model"], "model") if "model" in fields else None
    raw = fields.get("extensions", {})
    if not isinstance(raw, dict):
        raise ConfigError(f"config 'extensions' must be an object, not {type(raw).__name__}")
    extensions: dict[str, Mapping[str, object]] = {}
    for name, options in cast("dict[str, object]", raw).items():
        if not isinstance(options, dict):
            raise ConfigError(
                f"config 'extensions.{name}' must be an object, not {type(options).__name__}"
            )
        extensions[name] = cast("dict[str, object]", options)
    return ConfigFields(model, extensions)


def _configs(walk: Sequence[Node]) -> list[ConfigFields]:
    return [config_fields(n.body.config) for n in walk if isinstance(n.body, Body.Config)]


def model_name(walk: Sequence[Node]) -> str:
    """The qualified name of the last model a `Config` on `walk` names. `ConfigError` if a
    `Config` on it is malformed (`config_fields`) or none names a model."""
    models = [c.model for c in _configs(walk) if c.model is not None]
    if not models:
        raise ConfigError("config is missing 'model'")
    return models[-1]
