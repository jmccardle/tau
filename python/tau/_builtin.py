"""The built-in extensions: `tau` (D16 section 2) and `session_name` (D18 section 3).

`tau` is `/model`, and `/config`'s function.

Both write a `Config` node on the cursor's branch as a step of the cursor; the file stays
`tau config`'s to edit. `/model` is a command like any other. `/config` takes a mapping, which
the type language has no hint for, so it is not listed: `Commands.configure` runs `config`.
"""

from collections.abc import Mapping
from pathlib import Path

from tau._config import model_config
from tau._core import Body
from tau._handles import Cursor
from tau._sessions import read_name, rename_session
from tau._values import Model

NAME = "tau"
SESSION_NAME = "session_name"


async def model(cursor: Cursor, model: Model) -> None:
    """Switch this branch to a configured model."""
    cursor.set_model(model)


async def config(cursor: Cursor, extension: str, values: Mapping[str, object]) -> None:
    """Change an extension's options on this branch."""
    cursor.write(Body.Config(model_config(extensions={extension: values})))


class Tau:
    """The built-in extension: `/model`."""

    name = NAME
    commands = (model,)


class SessionName:
    """The built-in extension that names the session in `directory` (D18): `/name [NAME]` shows
    its name, or sets it; an empty name removes it. The program that starts the session adds it
    after the extensions it loads, so it has no key and `load` does not govern it. A rename takes
    no lease (it replaces `info.json`), so it works while the session runs."""

    name = SESSION_NAME

    def __init__(self, directory: Path) -> None:
        def name(name: str | None = None) -> str:
            """Show this session's name, or set it (an empty name removes it)."""
            if name is None:
                return read_name(directory) or "(no name)"
            rename_session(directory, name)
            return f"named {name.strip()!r}" if name.strip() else "name removed"

        self.commands = (name,)
