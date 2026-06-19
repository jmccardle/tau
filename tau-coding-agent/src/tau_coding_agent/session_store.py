"""Persistence for τ chat sessions — the on-disk format shared by the TUI and
the headless ``--print`` path.

A *session* is one :class:`Chat` JSON file under ``~/.tau/chats/``. Both the
Parley TUI (``app.py``) and ``tau -p`` (``headless.py``) read and write this same
format, so a headless run shows up in the sidebar and can be resumed there.

This module is deliberately free of any Textual import: ``tau -p`` must not pull
in the TUI just to save a session.

Reference: docs/CLI-PLAN.md (session persistence), docs/TUI-FOLLOWUPS.md (#1).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

# τ data dir for config and chat storage (matches app.py / cli.py).
TAU_DIR = Path.home() / ".tau"


@dataclass
class Chat:
    """One persisted chat conversation.

    ``model`` is the *config key* (e.g. ``"local-llm"``), not the API model id —
    this is what the TUI looks up in ``config["models"]`` when resuming, so a
    saved session is only resumable if its ``model`` is a configured key.
    ``messages`` holds the full τ message list (system + user + assistant/tool),
    where assistant/tool content is a list of block dicts (the τ message shape).
    """

    model: str
    backend: str
    messages: list[dict]
    created_at: float
    title: Optional[str] = None

    def save(self) -> Path:
        """Save chat to ``~/.tau/chats/<created_at>.json`` and return the path."""
        chats_dir = TAU_DIR / "chats"
        chats_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{int(self.created_at)}.json"
        path = chats_dir / filename
        path.write_text(json.dumps(asdict(self), indent=2))
        return path

    @classmethod
    def load(cls, path: Path) -> "Chat":
        """Load chat from a JSON file."""
        data = json.loads(path.read_text())
        return cls(**data)

    @classmethod
    def list_recent(cls, limit: int = 50) -> list[Path]:
        """List recent chat files, newest first."""
        chats_dir = TAU_DIR / "chats"
        if not chats_dir.exists():
            return []

        files = sorted(
            chats_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return files[:limit]

    def get_display_title(self) -> str:
        """Get a display title for this chat (first user message, else model)."""
        if self.title:
            return self.title

        # Use first user message as title (strip newlines for display).
        for msg in self.messages:
            if msg["role"] == "user":
                content = msg["content"]
                if not isinstance(content, str):
                    continue
                title = content.replace("\n", " ")[:50]
                if len(content) > 50:
                    title += "..."
                return title

        return f"Chat ({self.model})"
