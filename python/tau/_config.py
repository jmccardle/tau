"""Model configuration (D13): a `Config` node names a model; the driver resolves it.

`model_config` and `config_fields` write and read `Config` nodes' `data` (a JSON object core
never reads; glossary: config): `{"model": "<qualified name>"}`, and for a branch's
extensions `{"extensions": {"<name>": {"<option>": value}}}` (D16 section 4). Core's
`config_at` is the nearest `Config` only, so a node holds just what it changes and a reader
folds the walk: the model is the last one named, an option the last one set. `ModelConfig`
is the resolved model a provider is handed. `read_models` and `check_config` (in
`config_layers`) read the layered files: user, project, CLI overrides.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import cast
from urllib.parse import urlparse

from tau._core import Config


class ConfigError(ValueError):
    """A `Config`'s data that does not name a model, a model that is not in the config, an
    invalid resolved model, a key that cannot be read, or options that do not fit their
    extension: an extension with none, an unknown option, a wrong type, a missing required
    one, or a refusal by the options type's own `__post_init__`."""


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


_PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")


def _positive(value: object, key: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"config {key!r} must be a positive integer, not {value!r}")


def _money(value: object, key: str) -> Decimal:
    """`value` as an exact non-negative `Decimal`. A float is read as the shortest text that
    gives it back (`0.3` is 0.3, not 0.299999...), so a price written in a file is the price."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float):
        raise ConfigError(f"config price {key!r} must be a number, not {value!r}")
    number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    if not number.is_finite() or number < 0:
        raise ConfigError(f"config price {key!r} must be a number of at least 0, not {value!r}")
    return number


@dataclass(frozen=True)
class Price:
    """What a model costs, in USD per million tokens: `input` (prompt tokens neither read from
    nor written to the cache), `output`, `cache_read` and `cache_write`. All four are required:
    a model that writes no cache repeats its `input` price for `cache_write` (D24 ruling 6).
    `cache_write_1h` is optional: the price of a cache write kept for an hour (a reply's
    `written_1h`), which is `cache_write` when it is not given (D26 section 2).
    Amounts are `Decimal`, so a total of many replies has no float drift. `ConfigError` for a
    number below 0, or one that is not a number."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal
    cache_write_1h: Decimal | None = None

    def __post_init__(self) -> None:
        for key in _PRICE_FIELDS:
            object.__setattr__(self, key, _money(getattr(self, key), key))
        hour = self.cache_write if self.cache_write_1h is None else self.cache_write_1h
        object.__setattr__(self, "cache_write_1h", _money(hour, "cache_write_1h"))

    @classmethod
    def of(cls, fields: object) -> "Price":
        """The price a config's `price` object says. `ConfigError` unless it is an object with
        the four fields, and `cache_write_1h` if it likes, each a number of at least 0."""
        if not isinstance(fields, Mapping):
            raise ConfigError(f"config 'price' must be an object, not {type(fields).__name__}")
        given = cast("Mapping[str, object]", fields)
        missing = [k for k in _PRICE_FIELDS if k not in given]
        if missing:
            raise ConfigError(
                f"config 'price' is missing {missing}; all of {list(_PRICE_FIELDS)} are "
                "required (USD per million tokens; a model that writes no cache repeats "
                "its input price for cache_write)"
            )
        known = (*_PRICE_FIELDS, "cache_write_1h")
        extra = sorted(str(k) for k in given if k not in known)
        if extra:
            raise ConfigError(f"config 'price' has unknown keys {extra}; it holds {known}")
        four = (cast("Decimal", given[k]) for k in _PRICE_FIELDS)
        return cls(*four, cache_write_1h=cast("Decimal | None", given.get("cache_write_1h")))


@dataclass(frozen=True)
class ModelConfig:
    """A resolved model: which model to ask, where (`base_url`, optional: a provider that
    needs one says so in its `check`), the *name* of the variable holding its API key,
    `max_tokens` (optional, positive), and `options`: a JSON object only the named provider
    reads, which the config loader never looks inside (D15). `context_window` (optional,
    positive) is the model's context size in tokens and `price` (optional) what it costs:
    extensions read them (D24); no provider does. The driver builds it from the catalog at
    each sample; no node holds it.

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
    context_window: int | None = None
    price: Price | None = None

    def __post_init__(self) -> None:
        _text(self.model, "model")
        if self.base_url is not None:
            problem = url_problem(_text(self.base_url, "base_url"))
            if problem is not None:
                raise ConfigError(problem)
        if self.max_tokens is not None:
            _positive(self.max_tokens, "max_tokens")
        if self.context_window is not None:
            _positive(self.context_window, "context_window")
        if self.price is not None and not isinstance(self.price, Price):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ConfigError(f"config 'price' must be a Price, not {type(self.price).__name__}")
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


def model_config(
    model: str | None = None, extensions: Mapping[str, Mapping[str, object]] | None = None
) -> Config:
    """The `Config` node body that sets `model` (a catalog's qualified name; it holds that name
    and nothing resolved from the file) and/or the options in `extensions`
    (`{"step_limit": {"limit": 10}}`), from this node on, and nothing else. `ConfigError` if
    it sets neither."""
    fields: dict[str, object] = {}
    if model is not None:
        fields["model"] = _text(model, "model")
    if extensions:
        fields["extensions"] = extensions
    if not fields:
        raise ConfigError("a Config node sets a model or some extension options")
    return Config(json.dumps(fields))


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
