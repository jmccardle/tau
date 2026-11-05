"""`Agents`: the built-in extension that lets the model hand a task to a sub-agent (D24 section 8).

The model gets a tool, `agent(task, to=None)`. A call opens a cursor at the end of the session's
prefix (the root `Config` and the system prompt: a fresh context that shares the prompt-cache
prefix), labelled `agent:N`, sends it the task, waits for it to rest, and returns its final reply
as `agent:N answered:` and the text. A finished agent is closed (D17): its label and its nodes
stay in the tree. `to="agent:N"` reopens that agent and sends it a follow-up, so it remembers its
earlier work. Several calls in one reply run at once.

An agent's cursor is offered the `tools` option's tools, or every tool except `agent`, so there is
no recursion and no depth counter. Nothing else is special about it: the step limit bounds it,
the other extensions' hooks (permissions' questions included) run on its cursor, and usage counts
its replies. If the call is cancelled (the parent is aborted) its `finally` closes the agent,
which aborts it first. It is off by default: `load: {agents: true}`, or `tau ext enable agents`.
"""

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated

from tau._config import model_config
from tau._core import Body, CursorClosed, Ending, NoSuchLabel, Part
from tau._handles import Cursor, NodeHandle, Subject
from tau._lines import prefix_length
from tau._tools import ToolError
from tau._values import Model

TOOL = "agent"
PREFIX = "agent:"


@dataclass(frozen=True)
class Options:
    """`Agents`' options (`extensions.agents`; `/config agents`)."""

    model: Annotated[Model | None, "the model sub-agents use (none: the calling branch's)"] = None
    # A list option's default is a tuple (a dataclass refuses a list); the value is a list.
    tools: Annotated[list[str], "the tools a sub-agent is offered (none: all but `agent`)"] = ()  # pyright: ignore[reportAssignmentType]

    def __post_init__(self) -> None:
        if TOOL in self.tools:
            raise ValueError(f"a sub-agent cannot be offered {TOOL!r}: agents do not nest")

    def tool_problems(self, tools: Sequence[str]) -> list[str]:
        """The `tools` names that nothing loaded provides: a sub-agent would be offered no such
        tool, and nothing would say so."""
        return [
            f"tools names {name!r}, which no loaded extension provides; the tools are "
            f"{sorted(tools)}"
            for name in self.tools
            if name not in tools
        ]


def _final(label: str, node: Body) -> str:
    """What a final reply said. `ToolError` if `node` is not one (a reply that asks for tools:
    the agent was stopped, by a step limit) or said nothing."""
    if not isinstance(node, Body.Assistant) or any(isinstance(p, Part.Call) for p in node.parts):
        raise ToolError(f"{label} stopped without a final reply (a step limit?)")
    text = "".join(p.text for p in node.parts if isinstance(p, Part.Text))
    if not text:
        raise ToolError(f"{label} gave an empty reply")
    return text


def _why(label: str, ending: Ending | None) -> str:
    match ending:
        case Ending.Failed(error=error):
            return f"{label} failed: {error}"
        case Ending.Halted(reason=reason):
            return f"{label} was halted: {reason}"
        case Ending.Aborted():
            return f"{label} was aborted"
        case _:
            return f"{label} did not finish ({ending})"


class Agents:
    """Gives the model the `agent` tool (the module docstring)."""

    name = "agents"
    options = Options

    def __init__(self) -> None:
        self._offered: dict[int, tuple[str, ...]] = {}  # an agent's cursor -> its `tools` option
        self.tools = (self.agent,)

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> str | None:
        """Offer an agent's cursor its tools: the `tools` option's, or all but `agent`."""
        offered = self._offered.get(cursor.id)
        if offered is not None:
            subject.tools = offered or tuple(t for t in cursor.tools() if t != TOOL)
        return None

    async def agent(
        self,
        node: NodeHandle,
        task: Annotated[str, "everything the agent needs: it sees none of this conversation"],
        to: Annotated[str | None, "an earlier agent to continue, such as agent:1"] = None,
    ) -> str:
        """Give a task to a sub-agent that starts with a fresh context and returns its final
        reply. With `to`, send a follow-up to an agent you used before; it remembers its work."""
        options = node.options()
        child, label = (
            self._resume(node, to) if to is not None else self._start(node, options.model)
        )
        self._offered[child] = tuple(options.tools)
        try:
            node.send(child, task)
            ending = await node.rest(child)
            if not isinstance(ending, Ending.Done):
                raise ToolError(_why(label, ending))
            text = _final(label, node.tree.node(node.cursor(child).at).body)
            return f"{label} answered:\n{text}"
        finally:
            del self._offered[child]
            with contextlib.suppress(CursorClosed):  # someone closed it while it worked
                await node.close_cursor(child)

    def _start(self, node: NodeHandle, model: str | None) -> tuple[int, str]:
        """A new agent: a cursor at the end of the prefix, on `model` (the branch's own if none),
        labelled with the first `agent:N` no cursor has."""
        walk = node.walk()
        at = walk[prefix_length(walk) - 1].id
        wanted = model or node.model()
        if wanted != node.model(at=at):
            at = node.append(at, Body.Config(model_config(wanted)))
        child = node.open(at)
        number = 1
        while self._labelled(node, f"{PREFIX}{number}"):
            number += 1
        label = f"{PREFIX}{number}"
        node.set_label(label, child)
        return child, label

    def _resume(self, node: NodeHandle, label: str) -> tuple[int, str]:
        """The agent `label`: reopened if it is closed, as it is if it is open and at rest (a
        crash left it so)."""
        if not label.startswith(PREFIX) or not self._labelled(node, label):
            raise ToolError(f"there is no agent {label!r}")
        old = node.label(label)
        if old not in node.cursors():
            return node.reopen(old), label
        if node.cursor(old).busy:
            raise ToolError(f"{label} is still working: wait for its answer")
        return old, label

    @staticmethod
    def _labelled(node: NodeHandle, label: str) -> bool:
        try:
            node.label(label)
        except NoSuchLabel:
            return False
        return True
