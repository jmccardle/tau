"""Speculative: plan mode, as an extension (a clone of the Claude Code / pi idea).

`/plan [task]` puts the branch in plan mode: the model is briefed to research and propose a plan,
and may only use read-only tools. When its reply ends, the person is asked to approve, revise or
reject the plan. Approve leaves plan mode and tells the model to carry the plan out; revise
sends the person's feedback and stays in plan mode; reject leaves plan mode and does nothing.

The mode is an option (`extensions.plan_mode.mode`), so it is a `Config` node on the branch: it
survives a reload, follows `go_to`, differs between cursors, and `/config plan_mode mode=act`
leaves it by hand. The module is the extension: it has a `name`.
"""

import json
import re
from dataclasses import dataclass
from typing import Annotated, Literal

from tau_next import Body, CallDraft, Cursor, NodeHandle, Stop, Subject

BRIEF = (
    "PLAN MODE. Research with the read-only tools, then reply with a step-by-step plan and "
    "make no changes: write, edit and shell commands are refused until the person approves."
)
APPROVED = "The plan is approved. Carry it out."
SHELL_SYNTAX = re.compile(r"[;&|<>`$()\n]")  # anything that chains, redirects or substitutes


@dataclass(frozen=True)
class Options:
    """Plan mode's options (`/config plan_mode`)."""

    mode: Annotated[Literal["act", "plan"], "plan: read-only tools, ask before acting"] = "act"
    # A list option's default is a tuple (a dataclass refuses a list); the value is a list.
    allow_tools: Annotated[list[str], "tools usable in plan mode"] = ("read", "grep")  # pyright: ignore[reportAssignmentType]
    allow_bash: Annotated[list[str], "shell commands usable in plan mode"] = (  # pyright: ignore[reportAssignmentType]
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


async def plan(cursor: Cursor, task: str | None = None) -> str:
    """Enter plan mode on this branch, optionally starting with a task to plan."""
    cursor.set_options(mode="plan")
    if task:
        cursor.send(cursor.id, task)
    return "plan mode on: read-only tools; you will be asked to approve the plan"


async def pre_sample(cursor: Cursor, subject: Subject) -> str | None:
    """Brief the model once per stay in plan mode, however plan mode was entered (`/plan`,
    `/config`, a config file, going back to a node on a plan-mode branch)."""
    if cursor.options().mode != "plan":
        return None
    ours = [n for n in cursor.walk() if n.by == cursor.by and isinstance(n.body, Body.User)]
    if not ours or ours[-1].body != Body.User(BRIEF):
        cursor.write(Body.User(BRIEF))
    return None


async def post_reply(cursor: Cursor, subject: Subject) -> str | None:
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
        cursor.send(cursor.id, feedback)  # the person's words, `by` the client who typed last
    elif verdict == "reject":
        cursor.set_options(mode="act")
        cursor.tell("plan rejected: plan mode off")
    else:
        return "plan neither approved nor rejected"  # declined, or nobody to ask
    return None


async def pre_tool(node: NodeHandle, call: CallDraft) -> str | None:
    """Refuse what the model may not do in plan mode."""
    # WISH: a per-branch tool list, so `write` and `bash` are not even offered in plan mode.
    # Here the model is shown every tool and finds out by being refused.
    options = node.options()
    if options.mode != "plan" or call.tool in options.allow_tools:
        return None
    if call.tool == "bash":
        command = str(json.loads(call.args_raw).get("command", "")).strip()
        listed = (command == a or command.startswith(a + " ") for a in options.allow_bash)
        if not SHELL_SYNTAX.search(command) and any(listed):
            return None  # not a sandbox: `git diff --output=f` writes. A guard, not a wall.
    return f"plan mode: {call.tool} is refused. Put the change in your plan instead."


name = "plan_mode"
options = Options
commands = [plan]
