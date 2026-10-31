"""The built-in extension `tau` (D16 section 2): `/model` and `/config`.

Both are commands that run as a step of the cursor and write a `Config` node on its branch
(`Cursor.configure`); the file stays `tau config`'s to edit.
"""

import json
from collections.abc import Sequence
from typing import Annotated, Any, Literal, cast

from tau_next.handles import Cursor
from tau_next.values import Model

NAME = "tau"


async def model(cursor: Cursor, model: Model) -> None:
    """Switch this branch to a configured model."""
    cursor.configure(model=model)


def _config_command(extensions: Sequence[str]):
    def config(
        cursor: Cursor,
        extension: str,
        values: Annotated[str, "a JSON object of the options to change, e.g. '{\"limit\": 10}'"],
    ) -> None:
        """Change an extension's options on this branch."""
        decoded: object = json.loads(values)
        if not isinstance(decoded, dict):
            raise ValueError(f"values must be a JSON object, not {type(decoded).__name__}")
        cursor.configure(extensions={extension: decoded})  # pyright: ignore[reportUnknownArgumentType]

    # The extension is a Literal of those that have options, known only when the driver is built.
    config.__annotations__["extension"] = cast("Any", Literal)[tuple(extensions)]
    return config


class Tau:
    """The built-in extension: `/model`, and `/config` if any extension has options."""

    name = NAME

    def __init__(self, configurable: Sequence[str]) -> None:
        self.commands = [model, *([_config_command(configurable)] if configurable else [])]
