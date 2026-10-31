"""The one config file τ writes: the user's `<TAU_HOME>/config.json` (D1, ruling 6).

`UserConfig` is the layers a frontend loaded, so that after an edit it can read them again. Its
only write is `extensions.<name>.<field>` in the user layer. The project layer has no write: an
edit τ made to `.tau/config.json` would have to re-record the trust hash, and that waits for a
trust rule.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tau._config import ConfigError
from tau._config_layers import LayerText, ModelCatalog, read_models
from tau._trust import user_config_path, write_atomically


@dataclass(frozen=True)
class UserConfig:
    """The layers a frontend loaded: the user's file under `home` (`TAU_HOME`), the approved
    `project` layer over it and the `cli` overrides over that, as `load_config` read them.

    `save_options` writes the user layer and reads all three again, so the catalog it returns is
    what `load_config` would return now. The file is what every session reads; a `Config` node is
    one branch's."""

    home: Path
    project: LayerText | None = None
    cli: Mapping[str, object] | None = None

    def save_options(
        self,
        changes: Mapping[str, Mapping[str, object]],
        check: Callable[[ModelCatalog], None],
    ) -> ModelCatalog:
        """Set `extensions.<name>.<field>` in the user's file for each field of `changes`
        (extension, then field, to JSON value); the file's other content stays. `check` is
        given the catalog the layers make with the change, before anything is written, and
        raises `ConfigError` for options it refuses. Writes atomically, creating the file (and
        `home`, private) if absent. Returns the catalog; nothing is written if any step raised.
        `ConfigError` for a file or a result the loader refuses, `TrustError` for a disk that
        refuses."""
        path = self.home / "config.json"
        data = self._read(path)
        extensions = dict(cast("dict[str, object] | None", data.get("extensions")) or {})
        for name, fields in changes.items():
            extensions[name] = {
                **(cast("dict[str, object] | None", extensions.get(name)) or {}),
                **fields,
            }
        data["extensions"] = extensions
        try:
            body = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        except (TypeError, ValueError) as e:
            raise ConfigError(f"options are not JSON: {e}") from None
        catalog = read_models(LayerText(str(path), body), self.project, self.cli)
        check(catalog)
        write_atomically(path, body)
        return catalog

    def _read(self, path: Path) -> dict[str, object]:
        """The user's file as an object (empty if there is none), once the loader accepts it."""
        existing = user_config_path(self.home)
        if existing is None:
            return {}
        try:
            text = existing.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ConfigError(f"{path}: cannot be read: {getattr(e, 'strerror', None) or e}") from e
        read_models(LayerText(str(path), text))  # its own problems, in the loader's words
        return cast("dict[str, object]", json.loads(text))
