"""A throwaway home directory for startup and command tests. Never the real one."""

import json
from dataclasses import dataclass
from pathlib import Path

URL = "http://localhost:8100/v1"


@dataclass(frozen=True)
class World:
    home: Path
    tau: Path  # TAU_HOME
    environ: dict[str, str]

    def project(self, name: str = "proj", config: object | None = None) -> Path:
        root = self.home / name
        (root / ".tau").mkdir(parents=True)
        if config is not None:
            text = config if isinstance(config, str) else json.dumps(config)
            (root / ".tau" / "config.json").write_text(text, encoding="utf-8")
        return root

    def user_config(self, config: object) -> Path:
        self.tau.mkdir(mode=0o700, exist_ok=True)
        path = self.tau / "config.json"
        path.write_text(config if isinstance(config, str) else json.dumps(config), encoding="utf-8")
        return path


def models(**named: dict[str, object]) -> dict[str, object]:
    return {"models": {k: {"model": k, "base_url": URL, **v} for k, v in named.items()}}
