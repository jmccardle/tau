"""The minimal model config: what a `Config` node's `data` says about the model.

A small slice of the planned config overhaul (D13): one model, no groups, no
merging, no file. `data` is a JSON object; core never reads it (glossary:
config).
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlparse

from tau_next._core import Config

KEYS = ("model", "base_url", "api_key_env")


class ConfigError(ValueError):
    """A `Config`'s data that is not a valid `ModelConfig`, or a key that cannot be read."""


def _text(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"config {key!r} must be a string, not {type(value).__name__}")
    if not value:
        raise ConfigError(f"config {key!r} must not be empty")
    return value


@dataclass(frozen=True)
class ModelConfig:
    """Which model to ask, where, and the *name* of the variable holding its API key.

    The key itself is never a field: it is read from the environment at sample
    time (`api_key`) and so never reaches a node or the log.
    """

    model: str
    base_url: str
    api_key_env: str | None = None

    def __post_init__(self) -> None:
        _text(self.model, "model")
        url = urlparse(_text(self.base_url, "base_url"))
        if url.scheme not in ("http", "https") or not url.netloc:
            raise ConfigError(f"config 'base_url' must be an http(s) URL, not {self.base_url!r}")
        if self.api_key_env is not None:
            _text(self.api_key_env, "api_key_env")

    @classmethod
    def parse(cls, config: Config) -> "ModelConfig":
        """Read and validate `config.data`. Unknown keys and wrong types raise `ConfigError`."""
        try:
            data: object = json.loads(config.data)
        except ValueError as e:
            raise ConfigError(f"config data is not JSON: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"config data must be a JSON object, not {type(data).__name__}")
        fields = cast("dict[str, object]", data)
        unknown = sorted(str(k) for k in fields if k not in KEYS)
        if unknown:
            raise ConfigError(f"unknown config keys {unknown}; known keys are {list(KEYS)}")
        for required in ("model", "base_url"):
            if required not in fields:
                raise ConfigError(f"config is missing {required!r}")
        env = fields.get("api_key_env")
        return cls(
            _text(fields["model"], "model"),
            _text(fields["base_url"], "base_url"),
            None if "api_key_env" not in fields else _text(env, "api_key_env"),
        )

    def to_config(self) -> Config:
        """The `Config` node body holding this, ready for `Session.create`."""
        data: dict[str, str] = {"model": self.model, "base_url": self.base_url}
        if self.api_key_env is not None:
            data["api_key_env"] = self.api_key_env
        return Config(json.dumps(data))

    def api_key(self, environ: Mapping[str, str]) -> str | None:
        """The key in `environ` named by `api_key_env`; `None` if no variable is named.

        Raises `ConfigError` if one is named and is unset or empty.
        """
        if self.api_key_env is None:
            return None
        key = environ.get(self.api_key_env)
        if not key:
            raise ConfigError(f"environment variable {self.api_key_env} is not set")
        return key
