"""gamemaster: the main cursor is a game master; every character is a cursor of its own.

The GM's conversation with the player is the main cursor. A character is a branch off the same
root `Config` with its own system note (the persona); in that branch the GM is the *user* and
the model plays the character. The GM model decides who is in the scene (`ask_characters`),
rolls dice (`roll`), and narrates what came back as its next reply on the main cursor. A
character who is not named is not sampled and its history does not grow. A character is
nothing but a cursor labelled `char:<name>`, so the cast survives a reload with no state of
ours; the only in-memory state is the replies a tool is waiting for.

The player talks to a character directly by sending to that cursor from the frontend.
"""

import asyncio
import random
import re
from typing import Annotated

from tau_next import (
    Body,
    Cursor,
    Ending,
    ItemData,
    Model,
    NodeHandle,
    Part,
    Progress,
    Subject,
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
        self.waiting: dict[int, tuple[int, asyncio.Future[str]]] = {}  # character -> (item, reply)
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
        for name in asked:
            cursor = everyone[name]
            # WISH: `node.say(cursor, message)`, a User node authored by Extension("gamemaster"),
            # which wakes the cursor and returns its reply. `node.send` would author the
            # message as the human at the keyboard, and returns an item, not a reply.
            item = node.add_item(cursor, message)
            self.waiting[cursor] = (item, asyncio.get_running_loop().create_future())

        async def reply(name: str) -> str:
            said = await self.waiting[everyone[name]][1]
            progress(f"{name} has answered")
            return f"{name}: {said}"

        try:
            lines = await asyncio.gather(*(reply(n) for n in asked))
        finally:  # abort cancels this call; the characters' own steps run on regardless
            for name in asked:
                self.waiting.pop(everyone[name], None)
        silent = [n for n in everyone if n not in asked]
        return "\n".join([*lines, f"(not asked: {', '.join(silent) or 'no one'})"])

    # Hooks: a character's wake delivers the GM's message, and its end answers the tool.

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        items = [
            i
            for i in cursor.cursor(cursor.id).items
            if isinstance(i.data, ItemData.Extension) and i.data.name == self.name
        ]
        if items:
            texts = [i.data.data for i in items if isinstance(i.data, ItemData.Extension)]
            cursor.write(Body.User("\n\n".join(texts)), delivers=[i.id for i in items])
            cursor.set_auto(True)  # a wake with auto-step off would stop before the sample
        return None

    async def post_step(self, cursor: Cursor, subject: Subject) -> str | None:
        item, reply = self.waiting.get(cursor.id, (0, None))
        if reply is None or reply.done():
            return None
        if any(i.id == item for i in cursor.cursor(cursor.id).items):
            return None  # an earlier step of this cursor ended; the message is not delivered yet
        if isinstance(subject.ending, Ending.Done):
            reply.set_result(text_of(cursor.node().body))
        else:
            reply.set_exception(RuntimeError(f"the character did not answer: {subject.ending}"))
        return None
