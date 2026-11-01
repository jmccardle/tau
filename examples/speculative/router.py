"""router: choose the model for a turn from the text of the input.

`Options.when` is a list of regexes and `Options.use` the model for each; the first regex that
matches the input (re.search) decides. The router writes a `Config` node (`cursor.set_model`)
and a `Custom` node of kind `route` saying why, both before the `User` node, only when the
branch is not on that model already. `!model text` forces a model and is stripped from the text
the model sees. `/route off` (or `/config router enabled=false`) stops the table, not the prefix.
With `once`, the router puts the branch back on the model it was on before it ever routed, after
the turn's last reply.
"""

import json
import re
from dataclasses import dataclass
from typing import Annotated, Literal

from tau import Author, Body, Cursor, Draft, Model, Node, Stop, Subject

PREFIX = re.compile(r"!(\S+)\s+")
ROUTER = Author.Extension("router")


@dataclass(frozen=True)
class Options:
    """router's options. `when[i]` selects `use[i]`: a table of (regex, Model) as two lists,
    because the type language has no mapping and no pair."""

    # A list option's default is a tuple (a dataclass refuses a list); the value is a list.
    when: Annotated[list[str], "regexes, tried in order against the input"] = ()  # pyright: ignore[reportAssignmentType]
    use: Annotated[list[Model], "the model for each regex in `when`"] = ()  # pyright: ignore[reportAssignmentType]
    enabled: Annotated[bool, "route by the table (the !model prefix always works)"] = True
    once: Annotated[bool, "after the turn's last reply, go back to the model before routing"] = (
        False
    )

    def __post_init__(self) -> None:
        if len(self.when) != len(self.use):
            raise ValueError("`when` and `use` must be the same length")
        for pattern in self.when:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"pattern {pattern!r}: {e}") from e

    def route(self, text: str) -> tuple[str, Model] | None:
        """The first regex that matches `text`, and its model."""
        return next(
            ((w, m) for w, m in zip(self.when, self.use, strict=True) if re.search(w, text)),
            None,
        )


def model_on(walk: list[Node], skip: Author | None = None) -> str | None:
    """The model the walk's `Config` nodes set last, ignoring those authored by `skip`."""
    models = (
        json.loads(n.body.config.data).get("model")
        for n in reversed(walk)
        if isinstance(n.body, Body.Config) and n.by != skip
    )
    return next((m for m in models if m), None)


async def route(cursor: Cursor, mode: Literal["on", "off"]) -> str:
    """Turn the router's table on or off on this branch."""
    cursor.set_options(enabled=mode == "on")
    return f"routing {mode}"


class Router:
    name = "router"
    options = Options
    commands = (route,)

    def switch(self, cursor: Cursor, model: str, why: str) -> bool:
        """Put the branch on `model` (writes nothing if it is on it); False if it is not a
        configured model."""
        if model not in cursor.models():
            return False
        if cursor.model() != model:
            cursor.set_model(Model(model))
            cursor.write(Body.Custom("route", json.dumps({"model": model, "why": why})))
            cursor.tell(f"router: {model} ({why})")
        return True

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        draft = subject.draft
        assert draft is not None
        assert isinstance(draft.body, Body.User)
        text = draft.body.text
        if (prefix := PREFIX.match(text)) and self.switch(cursor, prefix[1], "prefix"):
            subject.draft = Draft(Body.User(text[prefix.end() :]), draft.delivers, draft.by)
        elif (options := cursor.options()).enabled and (hit := options.route(text)):
            self.switch(cursor, hit[1], f"/{hit[0]}/")
        return None

    async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        reply = subject.node
        assert reply is not None
        if not cursor.options().once or not isinstance(reply.body, Body.Assistant):
            return None
        if reply.body.stop != Stop.Tools:  # a tool batch is still the same turn
            home = model_on(cursor.walk(), skip=ROUTER)
            if home and home != cursor.model():
                cursor.set_model(Model(home))
        return None


extensions = [Router()]  # what `chat.py --extension` adds
