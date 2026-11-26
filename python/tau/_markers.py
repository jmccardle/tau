"""Undo markers (D33, CV-01): how a change to a cursor's line says where going back goes.

An **undo marker** is a `Custom` node of kind `undo` whose data is a JSON object:

    {"label": "/compact", "return_to": 41, "reverses": "compact", "priority": "move", ...}

- `label`: what was done, as a person would name it (`/compact`, `/clear`, `edit of a.py`).
- `return_to`: the node going back puts the cursor on: where it was before the change.
- `reverses`: what kind of change it was, a short name its owner chooses (`compact`, `prune`,
  `clear`, `checkpoint`).
- `priority`: which `/undo` follows it. `move` (the default) is a change to the tree alone, which
  moving the cursor back reverses; a plain `/undo` follows these, and edit-last. Any other value
  (`file`, for a checkpoint of the working tree) is followed only by `/undo <priority>`, and only
  if the extension that wrote it is loaded and reverses it itself (its `reverse`).
- Any other key is the owner's own (a checkpoint's commit, say), kept as `extra`.

The marker is written by the extension that made the change, so its `by` names the owner. It is a
`Custom` node, so it is never sent to a model, and decides nothing: where a cursor rests is
decided by the node before it. It is a convention over what the tree already holds, not a protocol
change; a session without the `undo` extension keeps its markers as inert records.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from tau._core import Body

UNDO_KIND = "undo"
"""The `kind` of every undo marker's `Custom` node."""
MOVE = "move"
"""The priority of a marker that moving the cursor back reverses: what a plain `/undo` follows."""
FILE = "file"
"""The priority, by convention, of a marker for a change outside the tree (a file checkpoint):
followed only by `/undo file`, and reversed by the extension that wrote it."""
_KEYS = ("label", "return_to", "reverses", "priority")
PRIORITY = re.compile(r"[a-z0-9_-]+")
"""What a priority is made of: lower-case letters, digits, `_` and `-` (`fullmatch` it)."""


@dataclass(frozen=True)
class UndoMarker:
    """One undo marker (the module docstring): `label`, `return_to`, `reverses`, `priority`
    (`MOVE` unless given) and `extra`, the owner's own keys. `ValueError` for an empty `label` or
    `reverses`, a `return_to` that is not a node id, a `priority` other than lower-case letters,
    digits, `_` and `-`, or an `extra` key that is one of the four."""

    label: str
    return_to: int
    reverses: str
    priority: str = "move"
    extra: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in ("label", "reverses"):
            value: object = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"an undo marker's {name} is a non-empty string, not {value!r}")
        if not isinstance(self.return_to, int) or isinstance(self.return_to, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError(f"an undo marker's return_to is a node id, not {self.return_to!r}")
        if self.return_to < 0:
            raise ValueError(f"an undo marker's return_to is a node id, not {self.return_to}")
        if not isinstance(self.priority, str) or not PRIORITY.fullmatch(self.priority):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError(
                f"an undo marker's priority is lower-case letters, digits, _ and -, not "
                f"{self.priority!r}"
            )
        clash = sorted(set(self.extra) & set(_KEYS))
        if clash:
            raise ValueError(f"an undo marker's extra keys cannot be {clash}")
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    def body(self) -> Body:
        """The marker as the `Custom` node body to write (`cursor.write(marker.body())`)."""
        data = {
            **self.extra,
            "label": self.label,
            "return_to": self.return_to,
            "reverses": self.reverses,
            "priority": self.priority,
        }
        return Body.Custom(UNDO_KIND, json.dumps(data, ensure_ascii=False))

    @classmethod
    def of(cls, body: Body) -> "UndoMarker | None":
        """The marker `body` holds; `None` if it is not a `Custom` of kind `undo`. `ValueError`
        naming the problem for one whose data is not a marker's (not a JSON object, a key
        missing, a value of the wrong kind)."""
        if not isinstance(body, Body.Custom) or body.kind != UNDO_KIND:
            return None
        try:
            data: object = json.loads(body.data)
        except ValueError as e:
            raise ValueError(f"an undo marker's data is not JSON: {e}") from None
        if not isinstance(data, dict):
            raise ValueError(f"an undo marker's data is a JSON object, not {type(data).__name__}")
        fields = cast("dict[str, object]", data)
        missing = [k for k in ("label", "return_to", "reverses") if k not in fields]
        if missing:
            raise ValueError(f"an undo marker's data lacks {missing}")
        return cls(
            cast("str", fields["label"]),
            cast("int", fields["return_to"]),
            cast("str", fields["reverses"]),
            cast("str", fields.get("priority", MOVE)),
            {k: v for k, v in fields.items() if k not in _KEYS},
        )
