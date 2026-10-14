"""The log writes a print-mode backend double owes, now that ``run_print`` binds.

``tau -p`` binds its cursor and appends nothing itself (docs/REPL-HEAD.md §3,
"bind, do not append"), so a double standing in for ``TauBackend`` must perform
the writes the core performs, or a test reads a session the head never wrote.
"""

from __future__ import annotations

import time
from typing import Any


class BoundPrintFake:
    """Mixin: ``bind_cursor`` plus the core's writes for one print-mode turn."""

    cursor: Any = None
    config: dict

    def bind_cursor(self, cursor: Any) -> None:
        """Record the head's cursor, as ``TauBackend.bind_cursor`` does."""
        self.cursor = cursor

    async def set_session_name(self, name: str) -> None:
        """Append the rename the core would append."""
        await self.cursor.append("session_info", name=name)

    async def record_model_change(self, name: str) -> None:
        """Append the config entry ``TauBackend.record_model_change`` appends."""
        await self.cursor.append_config(model=name, backend=self.config.get("backend", ""))

    async def persist_turn(self, submission: Any, new_messages: list[dict]) -> list[dict]:
        """Append the user message, read the context, append the turn's messages.

        Returns:
            The context the core would hand the loop: the path up to and
            including this turn's user message.
        """
        await self.cursor.append_message(
            {"role": "user", "content": submission.text, "timestamp": int(time.time() * 1000)}
        )
        context = self.cursor.context()
        for message in new_messages:
            await self.cursor.append_message(message)
        return context
