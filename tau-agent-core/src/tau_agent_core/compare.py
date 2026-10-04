"""Comparing models from one leaf: N cursors, one prompt, keep one (docs/TAU-SERVE.md §8).

Every head reaches a comparison through :func:`start_comparison` and ends it
through :meth:`Comparison.end`, so the TUI in-process, ``tau serve`` and anything
else that drives an :class:`~tau_agent_core.agent_session.AgentSession` open the
same cursors and keep the same leaf. A head renders the streams; it computes
nothing here.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tau_llm.docs import agent_facing

from tau_agent_core.cursor import Cursor, TurnFrame
from tau_agent_core.submission import Submission, SubmissionResult

if TYPE_CHECKING:
    from tau_agent_core.agent_session import AgentSession

__all__ = ["COMPARE_KEY", "Comparison", "split_compare_args", "start_comparison"]

COMPARE_KEY = "compare"
"""The ``Submission.correlation`` key a comparison's turns carry.

Its value is ``{"id", "models", "index", "cursor_id"}``, so a renderer that sees a
stream open can tell it belongs to a comparison, which column it is, and how many
columns there are, without having been the one that started it.
"""


@agent_facing(topic="sessions")
def split_compare_args(raw: str) -> dict[str, Any]:
    """Bind ``/compare``'s typed line: model names, then ``--``, then the prompt.

    ``/compare a b -- why is the sky blue`` binds both arguments. Without ``--``
    every word is a model name and the prompt is left unbound, so a head asks for
    it as the flow's next step.

    Returns:
        ``{"models": [...], "text": ...}``, with only the parts that were typed.

    Raises:
        ValueError: ``--`` with no model before it, or with nothing after it.
    """
    head, separator, tail = raw.partition("--")
    models = head.split()
    bound: dict[str, Any] = {}
    if models:
        bound["models"] = models
    if separator:
        if not models:
            raise ValueError("/compare needs at least one model name before '--'")
        if not tail.strip():
            raise ValueError("/compare has nothing after '--' to send")
        bound["text"] = tail.strip()
    return bound


@agent_facing(topic="sessions")
@dataclass
class Comparison:
    """One prompt running on N cursors that share a leaf, until one is kept.

    Attributes:
        id: Names the comparison on the wire and in each turn's correlation.
        session: The session whose head cursor owns every compare cursor.
        leaf: The entry all the cursors started from.
        models: The model names, in column order; one may repeat.
        cursors: One per model, in the same order, labelled with its model name.
        turns: Each cursor's running ``submit``, in the same order.
    """

    id: str
    session: AgentSession
    leaf: str | None
    models: tuple[str, ...]
    cursors: tuple[Cursor, ...]
    turns: tuple[asyncio.Task[SubmissionResult], ...]

    def cursor(self, cursor_id: str) -> Cursor:
        """The compare cursor ``cursor_id`` names.

        Raises:
            KeyError: it is not one of this comparison's.
        """
        for cursor in self.cursors:
            if cursor.id == cursor_id:
                return cursor
        raise KeyError(f"cursor {cursor_id!r} is not part of comparison {self.id}")

    async def end(self, keep: str | None) -> str | None:
        """Move the head onto ``keep``'s leaf, and close every compare cursor.

        A turn still running on a cursor that was not kept is aborted (or, not yet
        admitted, cancelled) and awaited first: picking a winner is the decision
        that the others are not wanted.
        Their branches stay in the tree, interrupted ones marked incomplete.
        ``keep`` ``None`` keeps none and leaves the head where it is.

        Returns:
            The head cursor's leaf afterwards.

        Raises:
            KeyError: ``keep`` names no cursor of this comparison.
            RuntimeError: the kept cursor's turn has not finished, or the head is
                running a turn; nothing was aborted or closed.
        """
        head = self.session.cursor
        kept = self.cursor(keep) if keep is not None else None
        if kept is not None:
            if not self.turns[self.cursors.index(kept)].done():
                raise RuntimeError(f"{kept.label} (cursor {kept.id}) is still running; wait for it")
            if head.busy:
                raise RuntimeError("the head cursor is running a turn; keep one when it ends")
        for cursor, turn in zip(self.cursors, self.turns):
            if cursor.busy:
                self.session.abort(cursor)
            elif not turn.done():
                turn.cancel()
        await asyncio.gather(*self.turns, return_exceptions=True)
        if kept is not None:
            head.move(kept.leaf)
        for cursor in self.cursors:
            if cursor in self.session.cursors:
                await self.session.close_cursor(cursor)
        return head.leaf


@agent_facing(topic="sessions")
async def start_comparison(
    session: AgentSession,
    leaf: str | None,
    models: list[str],
    text: str,
    *,
    allow_user_input: bool = True,
) -> Comparison:
    """Open one cursor per model at ``leaf`` and send each ``text``, all at once.

    Each cursor is owned by the head cursor (so aborting the head aborts them),
    labelled with its model name, and runs under a :class:`TurnFrame` with that
    model, every session tool and hooks on. The turns run as background tasks;
    this returns as soon as they are started.

    Args:
        session: The session to compare in; its model resolver turns names into models.
        leaf: The entry every cursor starts from, or ``None`` before the root.
        models: Configured model names, one column each.
        text: The prompt every cursor receives, sent as a prompt and never as a command.
        allow_user_input: Whether a hook in these turns may ask the user a question.

    Raises:
        ValueError: no models, no text, no model resolver, or ``leaf`` names no
            entry. Nothing was opened.
        KeyError: a model name the resolver does not know. Nothing was opened.
    """
    if not models:
        raise ValueError("compare needs at least one model")
    if not text.strip():
        raise ValueError("compare needs a prompt to send")
    if session.model_resolver is None:
        raise ValueError("compare needs a model resolver bound to the session")
    resolved = [session.resolve_model(name) for name in models]
    tools = tuple(tool.name for tool in session.tools)
    comparison_id = uuid.uuid4().hex[:8]
    head = session.cursor
    cursors: list[Cursor] = []
    for name, (model, api_key) in zip(models, resolved):
        cursor = await session.open_cursor(leaf, owner=head, label=name)
        cursor.frame = TurnFrame(tools=tools, model=model, api_key=api_key, hooks=True)
        cursors.append(cursor)
    turns = []
    for index, cursor in enumerate(cursors):
        submission = Submission(
            text=text,
            source="interactive",
            submitter="human",
            submission_id=uuid.uuid4().hex,
            multitask_strategy="reject",
            allow_user_input=allow_user_input,
            correlation={
                COMPARE_KEY: {
                    "id": comparison_id,
                    "models": list(models),
                    "index": index,
                    "cursor_id": cursor.id,
                }
            },
        )
        turns.append(asyncio.create_task(session.submit(submission, cursor=cursor)))
    return Comparison(
        id=comparison_id,
        session=session,
        leaf=leaf,
        models=tuple(models),
        cursors=tuple(cursors),
        turns=tuple(turns),
    )
