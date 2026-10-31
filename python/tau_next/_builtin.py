"""The built-in extension `tau` (D16 section 2): `/model`, and `/config`'s function.

Both write a `Config` node on the cursor's branch as a step of the cursor; the file stays
`tau config`'s to edit. `/model` is a command like any other. `/config` takes a mapping, which
the type language has no hint for, so it is not listed: `Commands.configure` runs `config`.
"""

from collections.abc import Mapping

from tau_next._config import model_config
from tau_next._core import Body
from tau_next._values import Model
from tau_next.handles import Cursor

NAME = "tau"


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
