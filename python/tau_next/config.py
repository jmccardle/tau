"""Model configuration (D13): a `Config` node names a model; the driver resolves it.

`model_config` and `model_name` write and read a `Config` node's `data` (a JSON object
core never reads; glossary: config): exactly `{"model": "<qualified name>"}`. `ModelConfig`
is the resolved model a provider is handed. `read_models` and `check_config` (in
`config_layers`) read the layered files: user, project, CLI overrides.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlparse

from tau_next._core import Config


class ConfigError(ValueError):
    """A `Config`'s data that does not name a model, a model that is not in the config, an
    invalid resolved model, or a key that cannot be read."""


def _text(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"config {key!r} must be a string, not {type(value).__name__}")
    if not value:
        raise ConfigError(f"config {key!r} must not be empty")
    return value


@dataclass(frozen=True)
class ModelConfig:
    """A resolved model: which model to ask, where, and the *name* of the variable holding
    its API key. The driver builds it from the catalog at each sample; no node holds it.

    The key itself is never a field: it is read from the environment at sample
    time (`api_key`) and so never reaches a node or the log.
    """

    model: str
    base_url: str
    api_key_env: str | None = None
    provider: str = "openai"
    literal_api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _text(self.model, "model")
        url = urlparse(_text(self.base_url, "base_url"))
        if url.scheme not in ("http", "https") or not url.netloc:
            raise ConfigError(f"config 'base_url' must be an http(s) URL, not {self.base_url!r}")
        if self.api_key_env is not None:
            _text(self.api_key_env, "api_key_env")
        _text(self.provider, "provider")
        if self.literal_api_key is not None:
            _text(self.literal_api_key, "api_key")

    def api_key(self, environ: Mapping[str, str]) -> str | None:
        """The key in `environ` named by `api_key_env`; `None` if no variable is named.

        A literal `api_key` from a config file wins. Raises `ConfigError` if a
        variable is named and is unset or empty.
        """
        if self.literal_api_key is not None:
            return self.literal_api_key
        if self.api_key_env is None:
            return None
        key = environ.get(self.api_key_env)
        if not key:
            raise ConfigError(f"environment variable {self.api_key_env} is not set")
        return key


def model_config(qualified: str) -> Config:
    """The `Config` node body naming the model `qualified` (a catalog's qualified name).
    It holds that name and nothing resolved from the file."""
    return Config(json.dumps({"model": _text(qualified, "model")}))


def model_name(config: Config) -> str:
    """The qualified name `config` holds. `ConfigError` for any other shape: not JSON,
    not an object, a missing or non-string `model`, or any other key."""
    try:
        data: object = json.loads(config.data)
    except ValueError as e:
        raise ConfigError(f"config data is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"config data must be a JSON object, not {type(data).__name__}")
    fields = cast("dict[str, object]", data)
    extra = sorted(str(k) for k in fields if k != "model")
    if extra:
        raise ConfigError(
            f'config data must be exactly {{"model": name}}; unexpected keys {extra}; '
            "a session from before D13 names no model: append a root with model_config(name)"
        )
    if "model" not in fields:
        raise ConfigError("config is missing 'model'")
    return _text(fields["model"], "model")
