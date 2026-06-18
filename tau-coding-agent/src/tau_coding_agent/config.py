"""Configuration system for τ-coding-agent.

Loads settings from:
1. `.tau/settings.json` in the project root (or current working directory)
2. `~/.tau/settings.json` in the user's home directory

Settings are merged with user settings overriding project settings.

Reference: PHASE-4-SUBPHASE-1.md — Config system
Reference: SUBPHASE-0.0.md — Model type + provider config
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Default settings — used when no config file is found
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS: dict[str, Any] = {
    "model": "gpt-4",
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_version": "2024-05-01-preview",
    "context_window": 128000,
    "max_tokens": 4096,
    "system_prompt": "You are a helpful coding assistant.",
    "thinking": "off",
    "tools": [],
    "theme": "catppuccin-mocha",
}

# ---------------------------------------------------------------------------
# Config path helpers
# ---------------------------------------------------------------------------


def get_project_config_path(project_root: Path | None = None) -> Path:
    """Return the path to the project-level config file.

    Args:
        project_root: Directory to search. Defaults to cwd.

    Returns:
        Path to `.tau/settings.json`.
    """
    root = project_root or Path.cwd()
    return root / ".tau" / "settings.json"


def get_user_config_path() -> Path:
    """Return the path to the user-level config file.

    Returns:
        Path to `~/.tau/settings.json`.
    """
    home = Path.home()
    return home / ".tau" / "settings.json"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_json_file(path: Path) -> dict[str, Any]:
    """Load a JSON config file.

    Args:
        path: Path to the JSON file.

    Returns:
        Parsed JSON as dict.

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge two dicts. Values in `override` take precedence.

    For nested dicts, merge recursively. For other types, override wins.

    Args:
        base: Base configuration dict.
        override: Override configuration dict.

    Returns:
        New merged dict (neither base nor override is mutated).
    """
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(
    project_root: Path | None = None,
    user_config_override: Path | None = None,
) -> dict[str, Any]:
    """Load and merge configuration from project and user config files.

    Loading order (later values override earlier):
    1. DEFAULT_SETTINGS (base)
    2. `.tau/settings.json` in project_root (or cwd)
    3. `~/.tau/settings.json` (user home)

    Args:
        project_root: Root directory for project config. Defaults to cwd.
        user_config_override: Path to user config file (for testing).
            Defaults to `~/.tau/settings.json`.

    Returns:
        Merged configuration dict.
    """
    # Start with defaults
    settings = dict(DEFAULT_SETTINGS)

    # Load project config
    project_config_path = get_project_config_path(project_root)
    if project_config_path.exists():
        try:
            project_cfg = _load_json_file(project_config_path)
            settings = _deep_merge(settings, project_cfg)
        except (json.JSONDecodeError, IOError):
            # Silently ignore corrupt config files
            pass

    # Load user config
    user_config_path = user_config_override or get_user_config_path()
    if user_config_path.exists():
        try:
            user_cfg = _load_json_file(user_config_path)
            settings = _deep_merge(settings, user_cfg)
        except (json.JSONDecodeError, IOError):
            # Silently ignore corrupt config files
            pass

    return settings


def save_config(
    path: Path,
    settings: dict[str, Any],
) -> Path:
    """Save configuration to a JSON file.

    Creates parent directories if they don't exist.

    Args:
        path: Destination file path.
        settings: Configuration dict to save.

    Returns:
        The path that was written to.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    return path
