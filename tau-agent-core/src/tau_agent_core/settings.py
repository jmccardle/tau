"""τ-agent-core settings.

Configuration for the τ agent system, loaded from ~/.tau/settings.json.

Reference: PHASE-5-SUBPHASE-0.md
Reference: SUBPHASE-0.0.md
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields as dataclass_fields
from pathlib import Path


@dataclass
class Settings:
    """τ settings (from ~/.tau/settings.json).

    Attributes:
        default_model: Default model identifier for LLM calls
        thinking_level: Thinking mode ("off", "low", "high")
        compaction_enabled: Whether automatic session compaction is enabled
        context_margin: Token margin before hitting context limit
        extension_dirs: Directories to search for extension modules
        api_keys: Mapping of provider name to API key
        custom_system_prompt: Optional custom system prompt override
        tool_execution_mode: Default tool execution mode ("parallel", "sequential")
        max_retries: Maximum number of retries for failed LLM calls
        temperature: Default sampling temperature
        max_tokens: Maximum output tokens (None = provider default)
        reasoning_level: Reasoning mode ("off", "low", "high")
    """

    default_model: str = "gpt-4o"
    thinking_level: str = "off"
    compaction_enabled: bool = True
    context_margin: int = 2000
    extension_dirs: list[str] = field(
        default_factory=lambda: [str(Path.home() / ".tau" / "extensions")],
    )
    api_keys: dict[str, str] = field(default_factory=dict)
    custom_system_prompt: str | None = None
    tool_execution_mode: str = "parallel"
    max_retries: int = 3
    temperature: float = 0.7
    max_tokens: int | None = None
    reasoning_level: str = "off"

    @classmethod
    def load(cls, cwd: str | None = None) -> "Settings":
        """Load settings from ~/.tau/settings.json and project-local override.

        Settings are loaded in order of precedence (later overrides earlier):
        1. Default values (built-in)
        2. Global settings from ~/.tau/settings.json
        3. Project-local settings from {cwd}/.tau/settings.json

        Args:
            cwd: Working directory for project-local settings lookup.

        Returns:
            A fully resolved Settings instance.
        """
        settings = cls()  # Start with defaults

        # Load global settings
        global_path = Path.home() / ".tau" / "settings.json"
        if global_path.exists():
            settings = settings._merge_from_file(global_path)

        # Load project-local settings
        if cwd:
            local_path = Path(cwd) / ".tau" / "settings.json"
            if local_path.exists():
                settings = settings._merge_from_file(local_path)

        return settings

    def _merge_from_file(self, path: Path) -> "Settings":
        """Merge settings from a JSON file into a copy of self.

        Args:
            path: Path to a JSON settings file.

        Returns:
            A new Settings instance with merged values.
        """
        with open(path) as f:
            data = json.load(f)

        # Build a mapping of field name -> value from the JSON
        field_names = {f.name for f in dataclass_fields(self)}
        merged_data = {k: v for k, v in data.items() if k in field_names}

        # Merge list fields by appending (extension_dirs)
        if "extension_dirs" in merged_data and "extension_dirs" in self.__dict__:
            existing_dirs = list(self.extension_dirs)
            existing_dirs.extend(merged_data["extension_dirs"])
            merged_data["extension_dirs"] = existing_dirs

        # Merge dict fields by updating (api_keys)
        if "api_keys" in merged_data and "api_keys" in self.__dict__:
            existing_keys = dict(self.api_keys)
            existing_keys.update(merged_data["api_keys"])
            merged_data["api_keys"] = existing_keys

        # Apply merged values, preserving defaults for missing keys
        result_data = self.__dict__.copy()
        result_data.update(merged_data)
        return self.__class__(**result_data)
