"""τ configuration: the single reader/writer for ``~/.tau/config.json``.

Before this module there were **two** divergent readers — ``cli.load_config``
(validating) and ``Parley.load_config`` (non-validating, and it wrote a *third*,
hardcoded default that disagreed with the packaged ``tau_default_config.json``
template) — plus two definitions of ``TAU_DIR``. Any new config key had to be
taught to both, and ``action_edit_system_prompt`` persisted the *runtime* config
(CLI overrides merged in) back to disk, silently promoting a one-run ``--model``
flag into the on-disk default.

One reader, one writer, one ``TAU_DIR``, one default template.

Reference: docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §2 (``models.<name>.grammar``
/ ``extra_body``), docs/JMFTS-INTEGRATION-PLAN.md §3.1 (``session_store``) — both
add keys here, so the divergence is fixed first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

TAU_DIR = Path.home() / ".tau"

CONFIG_PATH = TAU_DIR / "config.json"

# The packaged first-run template. Previously shipped but referenced by nothing,
# while the TUI wrote its own divergent default — so the file users actually got
# was not the file we maintained.
DEFAULT_CONFIG_TEMPLATE = Path(__file__).parent / "tau_default_config.json"


class ConfigError(Exception):
    """A malformed or unreadable τ configuration.

    Base of ``headless.CLIError`` so ``main()``'s existing handler catches both.
    """


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load ``~/.tau/config.json``; ``{}`` when absent.

    Fail-Early: a config file that exists but is not a JSON object raises rather
    than being silently coerced to an empty config (which would look like "no
    models configured" and send the user hunting in the wrong place).
    """
    config_path = path or CONFIG_PATH
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
    config_path = path or CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic: write a sibling temp file, fsync, then os.replace (atomic on POSIX and
    # Windows). A bare write_text truncates the target first, so a crash or ^C between
    # truncate and flush leaves a HALF-WRITTEN config.json — which load_config then
    # rejects as malformed, and τ refuses to start at all. Losing the config to a
    # mistimed Ctrl-C while editing the system prompt is not an acceptable failure.
    # The temp file is a sibling so os.replace never crosses a filesystem boundary.
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
    config_path = path or CONFIG_PATH
    if config_path.exists():
        return load_config(config_path)
    template: Any = json.loads(DEFAULT_CONFIG_TEMPLATE.read_text())
    if not isinstance(template, dict):
        raise ConfigError(f"packaged template {DEFAULT_CONFIG_TEMPLATE} is not a JSON object")
    config: dict[str, Any] = template
    save_config(config, config_path)
    return config
