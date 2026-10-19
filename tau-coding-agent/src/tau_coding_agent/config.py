"""τ configuration: the single reader and writer of ``<τ home>/config.json``.

One reader, one writer, one default template; it replaced two divergent readers
that disagreed on the first-run default. The home is resolved at each use, never
at import, so a bad ``TAU_HOME`` cannot break ``import`` or ``--help``
(docs/PORTABLE-HOME.md §8).

Reference: docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §2 (``models.<name>.grammar``
/ ``extra_body``), docs/JMFTS-INTEGRATION-PLAN.md §3.1 (``session_store``) — both
add keys here, so the divergence is fixed first. The ``compaction`` block is
validated here too (docs/PRICING-AND-COMPACTION-CONFIG.md §3).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tau_agent_core.compaction import CompactionConfig, CompactionSettings
from tau_agent_core.tau_home import resolve_tau_home

CONFIG_FILENAME = "config.json"

DEFAULT_CONFIG_TEMPLATE = Path(__file__).parent / "tau_default_config.json"


def default_config_path() -> Path:
    """τ's ``config.json``: ``<τ home>/config.json``, resolved now.

    Raises:
        TauHomeError: The τ home variables name something unusable.
    """
    return resolve_tau_home() / CONFIG_FILENAME


class ConfigError(Exception):
    """A malformed or unreadable τ configuration.

    Base of ``headless.CLIError`` so ``main()``'s existing handler catches both.
    """


class ModelConfigError(ConfigError, ValueError):
    """A ``models.<name>`` entry τ cannot use as written; the message names the key and the fix.

    Also a ``ValueError``, so callers that already catch that keep working.
    """


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load τ's ``config.json`` (``<τ home>/config.json`` unless ``path``); ``{}`` when absent.

    Fail-Early: a config file that exists but is not a JSON object raises rather
    than being silently coerced to an empty config (which would look like "no
    models configured" and send the user hunting in the wrong place).
    """
    config_path = path or default_config_path()
    if not config_path.exists():
        return {}
    try:
        loaded = json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{config_path} must contain a JSON object")
    return loaded


def save_config(config: dict[str, Any], path: Path | None = None) -> None:
    """Write the config back to disk.

    Callers must pass the **on-disk** config, never the runtime one: the TUI
    merges CLI overrides into ``self.config`` (``_apply_cli_overrides``), and
    writing that back would persist a one-run ``--model``/``--system-prompt`` flag
    as the permanent default. Use :func:`update_config` for read-modify-write.
    """
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    tmp = config_path.with_name(f".{config_path.name}.tmp{os.getpid()}")
    try:
        with tmp.open("w") as fh:
            fh.write(json.dumps(config, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, config_path)
    finally:
        tmp.unlink(missing_ok=True)


def update_config(key: str, value: Any, path: Path | None = None) -> dict[str, Any]:
    """Re-read the on-disk config, set one top-level key, write it back.

    The read-modify-write that keeps runtime-only state (CLI overrides) out of the
    persisted file. Returns the config as written.
    """
    config = load_config(path)
    config[key] = value
    save_config(config, path)
    return config


def bootstrap_config(path: Path | None = None) -> dict[str, Any]:
    """Create ``config.json`` from the packaged template if it does not exist.

    Returns the loaded config either way. The template is the single source of
    the first-run default — the TUI no longer carries its own copy.
    """
    config_path = path or default_config_path()
    if config_path.exists():
        return load_config(config_path)
    template: Any = json.loads(DEFAULT_CONFIG_TEMPLATE.read_text())
    if not isinstance(template, dict):
        raise ConfigError(f"packaged template {DEFAULT_CONFIG_TEMPLATE} is not a JSON object")
    config: dict[str, Any] = template
    save_config(config, config_path)
    return config


COMPACTION_KEYS: dict[str, type] = {
    "auto": bool,
    "reserve_tokens": int,
    "keep_recent_tokens": int,
    "summarizer": str,
}


def parse_compaction_config(block: Any) -> CompactionConfig:
    """Validate a ``"compaction"`` block; ``{}`` is the all-defaults answer.

    Args:
        block: The block as loaded from JSON.

    Returns:
        The block as settings plus an optional summarizer name
        (docs/PRICING-AND-COMPACTION-CONFIG.md §3).

    Raises:
        ConfigError: ``block`` is not an object, names a key outside
            :data:`COMPACTION_KEYS`, gives a key the wrong type, gives a token
            count that is not positive, or gives an empty ``summarizer``.
    """
    if not isinstance(block, dict):
        raise ConfigError(f"'compaction' must be an object; got {type(block).__name__}")
    for key, value in block.items():
        expected = COMPACTION_KEYS.get(key)
        if expected is None:
            raise ConfigError(
                f"unknown key 'compaction.{key}'; known keys: {', '.join(COMPACTION_KEYS)}"
            )
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ConfigError(f"'compaction.{key}' must be {expected.__name__}; got {value!r}")
        if isinstance(value, int) and not isinstance(value, bool) and value <= 0:
            raise ConfigError(f"'compaction.{key}' must be positive; got {value}")
    summarizer = block.get("summarizer")
    if summarizer == "":
        raise ConfigError("'compaction.summarizer' must name a model; got an empty string")
    overrides = {
        key: block[key] for key in ("auto", "reserve_tokens", "keep_recent_tokens") if key in block
    }
    return CompactionConfig(settings=CompactionSettings(**overrides), summarizer=summarizer)


def compaction_for(config: Mapping[str, Any], name: str | None) -> CompactionConfig:
    """The compaction config model ``name`` runs under.

    The model entry's own ``"compaction"`` block replaces the top-level one whole;
    with neither, the defaults. A ``name`` that is not a configured model (an
    ad-hoc ``provider/id``) gets the top-level block.

    Args:
        config: The loaded ``config.json``.
        name: A config model name, or ``None``.

    Returns:
        The parsed block.

    Raises:
        ConfigError: the block in force is malformed, or its ``summarizer``
            names no configured model.
    """
    models = config.get("models") or {}
    entry = models.get(name) if name is not None else None
    if isinstance(entry, Mapping) and "compaction" in entry:
        block, where = entry["compaction"], f"models.{name}.compaction"
    elif "compaction" in config:
        block, where = config["compaction"], "compaction"
    else:
        return CompactionConfig(settings=CompactionSettings())
    try:
        parsed = parse_compaction_config(block)
    except ConfigError as exc:
        if where == "compaction":
            raise
        raise ConfigError(f"{where}: {exc}") from exc
    if parsed.summarizer is not None and parsed.summarizer not in models:
        known = ", ".join(sorted(models)) or "(none configured)"
        raise ConfigError(
            f"'{where}.summarizer' names unknown model {parsed.summarizer!r}; "
            f"configured models: {known}"
        )
    return parsed


def validate_compaction_config(config: Mapping[str, Any]) -> None:
    """Parse the top-level ``"compaction"`` block and every model's own, so a bad one fails at startup.

    Raises:
        ConfigError: any block fails :func:`compaction_for`.
    """
    compaction_for(config, None)
    for name in config.get("models") or {}:
        compaction_for(config, name)


def apply_compaction_config(
    model_config: dict[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    """``model_config`` with the compaction block it starts under copied onto it.

    The model entry's own block (already on ``model_config``) replaces the
    top-level one whole; with neither, ``model_config`` comes back as it is.
    ``TauBackend`` reads the copied block for the settings it starts with, and for
    whether a summarizer must wait for the resolver; the resolver is what supplies
    compaction from then on (docs/PRICING-AND-COMPACTION-CONFIG.md §6).

    Args:
        model_config: The config ``create_backend`` is about to receive; not mutated.
        config: The loaded ``config.json``.

    Returns:
        A new dict when a block applies; ``model_config`` itself otherwise.

    Raises:
        ConfigError: the block is malformed or ``summarizer`` names no
            configured model.
    """
    if "compaction" in model_config:
        block = model_config["compaction"]
    elif "compaction" in config:
        block = config["compaction"]
    else:
        return model_config
    compaction_for({**config, "compaction": block}, None)
    return {**model_config, "compaction": block}
