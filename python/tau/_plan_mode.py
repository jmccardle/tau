"""`PlanMode`: the built-in extension that lets a person have the model plan before it acts.

It is on by default and inert until `/plan [task]` puts the branch in plan mode (the option
`extensions.plan_mode.mode`, so it is a `Config` node: it follows the branch, survives a reload
and `go_to`, and `/config plan_mode mode=act` leaves it by hand). In plan mode:

- the model is briefed once (a `User` node the extension writes) and is offered only the read-only
  tools, `READ_ONLY_TOOLS`, and `bash` for the commands in `READ_ONLY_COMMANDS` (`pre_sample`
  sets `subject.tools` to those of them the driver has);
- a call to anything else is refused (`pre_tool`): an offer is not a wall, and `bash` is checked
  against the command list, with anything that chains, redirects or substitutes refused. This is
  a guard, not a sandbox (`git diff --output=f` writes);
- the line `plan mode` is shown on the cursor (`cursor.show`), so every client has the badge;
- when a reply ends the plan (`Stop.End`), the person is asked to approve, revise or reject it.
  Approve leaves plan mode, tells the model to carry the plan out and turns auto-step on; revise
  asks what should change, sends it and stays in plan mode; reject leaves plan mode. A question
  nobody answers (nobody connected, an abort) stops with a reason and leaves plan mode on.

What counts as read-only is the mode's meaning, so it is fixed here, not an option: a client's
`/config` must not be able to widen it (an administrator who wants other lists shadows this
built-in with a drop-in named `plan_mode`, D19). The mode is the only option.
"""

import json
import re
from dataclasses import dataclass
from typing import Annotated, Literal

from tau._core import Body, Stop
from tau._handles import Cursor, NodeHandle, Subject
from tau._protocols import CallDraft

BRIEF = (
    "PLAN MODE. Research with the read-only tools, then reply with a step-by-step plan and "
    "make no changes: write, edit and shell commands are refused until the person approves."
)
APPROVED = "The plan is approved. Carry it out."
READ_ONLY_TOOLS = ("read", "grep", "find")
"""The tools plan mode offers and lets run, as far as the driver has them."""
READ_ONLY_COMMANDS = (
    "ls",
    "pwd",
    "cat",
    "head",
    "tail",
    "wc",
    "git status",
    "git diff",
    "git log",
)
"""The `bash` commands plan mode lets run: a command that is one of these, or starts with one
followed by a space, and has no shell syntax."""
SHELL_SYNTAX = re.compile(r"[;&|<>`$()\n]")  # anything that chains, redirects or substitutes
BADGE = "plan mode"


@dataclass(frozen=True)
class Options:
    """`PlanMode`'s one option (`/config plan_mode`)."""

    mode: Annotated[Literal["act", "plan"], "plan: read-only tools, ask before acting"] = "act"


def _allowed(command: str) -> bool:
    """Whether `command` is one of `READ_ONLY_COMMANDS`, with no shell syntax."""
    command = command.strip()
    listed = (command == a or command.startswith(a + " ") for a in READ_ONLY_COMMANDS)
    return not SHELL_SYNTAX.search(command) and any(listed)


class PlanMode:
    """The built-in extension `plan_mode` (the module docstring)."""

    name = "plan_mode"
    options = Options

    def __init__(self) -> None:
        self._badged: set[int] = set()  # cursors the badge was shown on: only those need clearing
        self.commands = (self.plan,)

    async def plan(self, cursor: Cursor, task: str | None = None) -> str:
        """Enter plan mode on this branch, optionally starting with a task to plan."""
        cursor.set_options(mode="plan")
        if task:
            cursor.send(cursor.id, task)
        return "plan mode on: read-only tools; you will be asked to approve the plan"

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        """Show the mode while the branch is in plan mode, however it was entered."""
        self._badge(cursor)
        return None

    async def post_step(self, cursor: Cursor, subject: Subject) -> str | None:
        """`pre_step`'s badge again: level-triggered at both ends of a step. Going back to a
        node by hand shows nothing until the next step."""
        self._badge(cursor)
        return None

    def _badge(self, cursor: Cursor) -> None:
        if cursor.options().mode == "plan":
            self._badged.add(cursor.id)
            cursor.show(BADGE)
        elif cursor.id in self._badged:
            self._badged.discard(cursor.id)
            cursor.show(None)

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> str | None:
        """Offer the read-only tools, and brief the model once per stay in plan mode, however
        plan mode was entered (`/plan`, `/config`, a config file, going back to a node on a
        plan-mode branch)."""
        if cursor.options().mode != "plan":
            return None
        have = cursor.tools()
        wanted = [*READ_ONLY_TOOLS, "bash"]
        subject.tools = [name for name in wanted if name in have]  # the ones this driver has
        marks = [Body.User(BRIEF), Body.User(APPROVED)]  # what we wrote; text we `send` is ours too
        last = [n.body for n in cursor.walk() if n.by == cursor.by and n.body in marks]
        if not last or last[-1] != marks[0]:
            cursor.write(marks[0])
        return None

    async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        """A reply that ends in plan mode is a plan: ask what to do with it."""
        reply = subject.node.body if subject.node else None
        if cursor.options().mode != "plan" or not isinstance(reply, Body.Assistant):
            return None
        if reply.stop != Stop.End:  # still researching (Tools), or cut off
            return None
        verdict = await cursor.ask("Approve this plan?", Literal["approve", "revise", "reject"])
        if verdict == "approve":
            cursor.set_options(mode="act")
            cursor.write(Body.User(APPROVED))  # `by` is this extension: the person only chose
            cursor.set_auto(True)  # the next step starts at once and carries it out
        elif verdict == "revise":
            feedback = await cursor.ask("What should change?", str)
            if feedback is None:
                return "plan neither approved nor revised"
            cursor.send(cursor.id, feedback)  # the person's words, sent by this extension
        elif verdict == "reject":
            cursor.set_options(mode="act")
            cursor.tell("plan rejected: plan mode off")
        else:
            return "plan neither approved nor rejected"  # declined, or nobody to ask
        return None

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        """Refuse what the model may not do in plan mode. The model is only offered the
        read-only tools and the driver refuses a call to any other (`tool not offered`); this
        hook refuses what is offered but not allowed: a `bash` command outside the list."""
        if node.options().mode != "plan" or call.tool in READ_ONLY_TOOLS:
            return None
        if call.tool == "bash" and _allowed(str(json.loads(call.args_raw).get("command", ""))):
            return None  # not a sandbox: `git diff --output=f` writes. A guard, not a wall.
        return f"plan mode: {call.tool} is refused. Put the change in your plan instead."
