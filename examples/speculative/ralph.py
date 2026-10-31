"""ralph: `while true; do cat PROMPT.md | agent; done`, as an extension.

`/ralph PROMPT.md [root]` writes the file's text as a `User` node under `root` (default: where the
cursor is now, so the context is whatever is above it) and turns auto-step on. Each time the model
finishes (a reply that asks for no tools), `post_step` moves the cursor back to `root` and writes
the file again, re-read, so the model may rewrite its own prompt. Every iteration is a new branch
from `root` with a fresh context; all state that matters is in files the model edits.

It stops when a file `DONE` exists beside the prompt, or after `max_iterations`, or when
auto-step is turned off by anything else: an abort, a failed step, `StepLimit`. The loop's state
(root, prompt, who asked) is this object's memory; auto-step is off after a reload, so a reloaded
session does not loop, and `/ralph` starts it again.

WISH: the loop is the one thing here the API had no word for.

    async def post_rest(cursor, subject): ...   # runs when a cursor comes to rest, auto or not
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from tau_next import Body, Cursor, Ending, Next, NodeId, Subject


@dataclass(frozen=True)
class Options:
    done: Annotated[str, "a file beside the prompt whose existence ends the loop"] = "DONE"
    max_iterations: Annotated[int, "iterations before the loop stops"] = 25

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError(f"max_iterations must be at least 1, not {self.max_iterations}")


@dataclass
class Loop:
    root: int
    prompt: Path
    iterations: int = 1


class Ralph:
    name = "ralph"
    options = Options

    def __init__(self) -> None:
        self._loops: dict[int, Loop] = {}
        self.commands = (self.ralph,)

    async def ralph(self, cursor: Cursor, prompt: Path, root: NodeId | None = None) -> str:
        """Send a prompt file to the model, again each time it finishes, from a fresh context."""
        at = cursor.cursor(cursor.id).at
        loop = Loop(at if root is None else root, prompt)
        self._restart(cursor, loop)
        self._loops[cursor.id] = loop
        cursor.set_auto(True)
        return f"ralph: {prompt} from node {loop.root}"

    def _restart(self, cursor: Cursor, loop: Loop) -> None:
        text = loop.prompt.read_text()  # before moving: a failure leaves the cursor where it is
        cursor.move(loop.root)
        cursor.write(Body.User(text))

    async def post_step(self, cursor: Cursor, subject: Subject) -> str | None:
        loop = self._loops.pop(cursor.id, None)  # back in only if the loop goes on
        if loop is None:
            return None
        state = cursor.cursor(cursor.id)
        if not isinstance(subject.ending, Ending.Done):  # aborted, failed or halted
            self._stop(cursor, loop, str(subject.ending))
            return None
        if not state.auto:  # switched off from outside: a client, StepLimit
            self._stop(cursor, loop, "auto-step off")
            return None
        if state.next != Next.Wait:
            self._loops[cursor.id] = loop
            return None  # mid-iteration: tools to run, or results to answer
        cursor.tell(f"ralph: iteration {loop.iterations} finished")
        options = cursor.options()
        if (loop.prompt.parent / options.done).exists():
            self._stop(cursor, loop, f"{options.done} exists")
        elif loop.iterations >= options.max_iterations:
            self._stop(cursor, loop, f"max_iterations {options.max_iterations}")
        else:
            loop.iterations += 1
            self._restart(cursor, loop)
            self._loops[cursor.id] = loop
        return None

    def _stop(self, cursor: Cursor, loop: Loop, why: str) -> None:
        cursor.set_auto(False)
        cursor.tell(f"ralph: stopped after {loop.iterations} iterations: {why}")
