"""gamemaster: the main cursor is a game master; every character is a cursor of its own.

The GM's conversation with the player is the main cursor. A character is a branch off the same
root `Config` with its own system note (the persona); in that branch the GM is the *user* and
the model plays the character. The GM model decides who is in the scene (`ask_characters`),
rolls dice (`roll`), and narrates what came back as its next reply on the main cursor. A
character who is not named is not sampled and its history does not grow. A character is
nothing but a cursor labelled `char:<name>`, so the cast survives a reload with no state of
ours. Asking is `await rest` (let it finish what it was doing), `send` to the character's cursor,
`await rest` again (the step the message caused), and reading the reply off the tree; if asking
fails or the GM is aborted, the characters it asked are aborted.

The player talks to a character directly by sending to that cursor from the frontend.
"""

import asyncio
import random
import re
from typing import Annotated

from tau import (
    Body,
    Cursor,
    Ending,
    Model,
    NodeHandle,
    Part,
    Progress,
    ToolError,
    TreeHandle,
    model_config,
)

PREFIX = "char:"
MAX_DICE = 100
DICE = re.compile(r"(\d+)d(\d+)([+-]\d+)?")

GM_SYSTEM = (
    "You are the gamemaster of an interactive story. Introduce characters with `introduce`. "
    "A character knows only what you tell them: for each beat, call `ask_characters` with "
    "the names of those present and what they perceive, and `roll` for anything uncertain. "
    "Leave out everyone who is not in the scene. Then narrate what happened in a few lines."
)


def persona_prompt(name: str, persona: str) -> str:
    return (
        f"You are {name}, a character in a story. {persona}\n"
        "The gamemaster tells you what you perceive; reply in character, briefly."
    )


def cast(tree: TreeHandle) -> dict[str, int]:
    """Who is in the story: every character and its cursor."""
    return {
        label.removeprefix(PREFIX): c
        for c in tree.cursors()
        for label in tree.cursor(c).labels
        if label.startswith(PREFIX)
    }


def present(tree: TreeHandle) -> str:
    """Who is in the story, and which of them is busy answering."""
    return "\n".join(
        f"{name} (cursor {c}){' answering' if tree.cursor(c).busy else ''}"
        for name, c in cast(tree).items()
    )


def enlist(tree: TreeHandle, root: int, name: str, persona: str, model: Model | None) -> str:
    """A cursor off `root` whose first node is the persona; `model` plays it, if given."""
    if name in cast(tree):
        raise ValueError(f"{name} is already in the story")
    at = tree.append(root, Body.System(persona_prompt(name, persona)))
    if model is not None:
        at = tree.append(at, Body.Config(model_config(model)))
    tree.set_label(PREFIX + name, tree.open(at))
    return f"{name} has joined the story"


def text_of(body: Body) -> str:
    """What a reply said."""
    if not isinstance(body, Body.Assistant):
        raise ValueError(f"expected a reply, found {body}")
    return "".join(p.text for p in body.parts if isinstance(p, Part.Text))


class Gamemaster:
    name = "gamemaster"

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.tools = (self.roll, self.introduce, self.ask_characters, present)
        self.commands = (self.roll, self.character, present)

    def roll(self, dice: Annotated[str, "NdM, optionally +K or -K: 2d6+1"]) -> str:
        """Roll dice."""
        m = DICE.fullmatch(dice.strip())
        if m is None:
            raise ValueError(f"cannot roll {dice!r}: say NdM, such as 2d6+1")
        count, sides, bonus = int(m[1]), int(m[2]), int(m[3] or 0)
        if not 1 <= count <= MAX_DICE or sides < 1:
            raise ValueError(f"cannot roll {dice!r}: 1 to {MAX_DICE} dice of 1 or more sides")
        rolls = [self.rng.randint(1, sides) for _ in range(count)]
        return f"{dice}: {rolls} {bonus:+d} = {sum(rolls) + bonus}"

    def introduce(
        self, node: NodeHandle, name: str, persona: str, model: Model | None = None
    ) -> str:
        """Add a character to the story. `persona` is all it knows about itself."""
        return enlist(node, node.walk()[0].id, name, persona, model)

    def character(self, cursor: Cursor, name: str, persona: str, model: Model | None = None) -> str:
        """Add a character to the story: `/character Mira "a wary innkeeper"`."""
        return enlist(cursor, cursor.walk()[0].id, name, persona, model)

    async def ask_characters(
        self,
        node: NodeHandle,
        who: Annotated[list[str], "the characters in this scene; no one else is asked"],
        message: Annotated[str, "what they perceive, in your voice: they know nothing else"],
        progress: Progress,
    ) -> str:
        """Tell these characters, at once, what they perceive; returns what each says."""
        everyone = cast(node)
        asked = list(dict.fromkeys(who))
        if missing := [n for n in asked if n not in everyone]:
            raise ValueError(f"not in the story: {', '.join(missing)}")
        if own := [n for n in asked if everyone[n] == node.cursor_id]:
            raise ToolError(f"{', '.join(own)}: a character cannot ask itself")

        async def answer(name: str) -> Ending | None:
            cursor = everyone[name]
            await node.rest(cursor)  # whatever it is doing now is not ours: wait it out first
            node.send(cursor, message)
            ending = await node.rest(cursor)  # the step our message caused
            if isinstance(ending, Ending.Done):
                progress(f"{name} has answered")
            return ending

        try:
            async with asyncio.TaskGroup() as group:  # one failing stops the others sending
                tasks = [group.create_task(answer(n)) for n in asked]
            endings = [t.result() for t in tasks]
            for name, ending in zip(asked, endings, strict=True):
                if ending != Ending.Done():  # `None`: not known; it cannot be after our own send
                    raise RuntimeError(f"{name} did not answer: {ending}")
        except BaseException:  # failed or aborted: the characters it asked stop spending too
            for name in asked:
                node.abort(everyone[name])
            raise
        lines = [f"{n}: {text_of(node.tree.node(node.cursor(everyone[n]).at).body)}" for n in asked]
        silent = [n for n in everyone if n not in asked]
        return "\n".join([*lines, f"(not asked: {', '.join(silent) or 'no one'})"])
