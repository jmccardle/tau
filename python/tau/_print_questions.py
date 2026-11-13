"""`tau -p`'s questions (D22 section 5): which ones the run may answer, answering or declining
them by its `--answer` and `--decline` rules, and stopping for those no rule matches. A question
is never declined for want of a rule (amended 2026-11-12, F-6)."""

import contextlib
from typing import TYPE_CHECKING

from tau._events import CursorView, OpenQuestion
from tau.commandline import value

if TYPE_CHECKING:
    from tau._print_mode import Run


class RuleRefused(Exception):
    """An `--answer` rule whose value the question's schema refused."""


class Unanswered(Exception):
    """Questions the run may answer that no rule matches: `args[0]`, their ids."""


def opened_by_run(run: "Run", cursor: int) -> bool:
    """Whether this run's cursor handed work to `cursor`, which nothing records (D24 section 7:
    no `opened_by`). It did if an extension wrote a user message (a task) onto its walk after
    the attach, while the run's own cursor was busy (a sub-agent: it opens on the session's
    prefix, not on the run's turn; or an agent the session already had, sent a follow-up), or
    its walk passes through the run's own turn. A cursor another client opens on a node that
    exists (`open(at)`) is not. Remembered once it is."""
    if cursor == run.cursor or cursor in run.sub:
        return True
    own = run.views.get(run.cursor)
    view = run.views.get(cursor)
    if own is None or view is None:
        return False
    mine = {n.id for n in run.turn(own.at)}
    task = False  # a task was written onto the walk during the run
    node = run.nodes.get(view.at)
    while node is not None:
        if node.id in mine:
            run.sub.add(cursor)
            return True
        task = task or node.id in run.tasks
        node = None if node.parent is None else run.nodes.get(node.parent)
    if task:
        run.sub.add(cursor)
        return True
    return False


def ours(run: "Run", cursor: int, *, snapshot: bool) -> bool:
    """Whether the run may answer the questions on `cursor`. Local, every cursor's. Attached,
    those of the cursor it attached to, open already or not, and of a cursor the run opened since
    (a sub-agent its step started); every other cursor's are the server's other clients' (D22
    section 5)."""
    if not run.attached or cursor == run.cursor:
        return True
    return not snapshot and opened_by_run(run, cursor)


async def questions(run: "Run", view: CursorView, *, snapshot: bool) -> None:
    """Deal with the open questions `-p` may answer (`ours`): each by the first rule that
    matches. `Unanswered` names those no rule matches, after every answer is given."""
    unanswered: list[int] = []
    for question in view.questions:
        if question.id not in run.seen and run.verbose:
            run.note(f'question {question.id} from {question.extension}: "{question.text}"')
        run.seen.add(question.id)
        if question.id in run.handled or question.to not in (None, run.client.id):
            continue
        run.handled.add(question.id)
        if not ours(run, view.cursor, snapshot=snapshot):
            run.note(
                f"question {question.id} from {question.extension} on cursor {view.cursor} "
                "is left to the server's other clients"
            )
        elif not await resolve(run, question, view.cursor):
            unanswered.append(question.id)
    if unanswered:
        raise Unanswered(unanswered)


async def resolve(run: "Run", question: OpenQuestion, cursor: int) -> bool:
    """Answer the question by the first rule that matches (a `--decline` one declines it).
    False, and the question shown, if none does: it is never declined for want of a rule."""
    asked = f"question {question.id} from {question.extension}"
    said = f'"{question.text}"'
    rule = next((r for r in run.rules if r.matches(question.extension, question.text)), None)
    if rule is None:
        run.note(f"{asked} on cursor {cursor} has no rule: {said}")
        return False
    if rule.value is None:
        await answer(run, question.id, None)
        run.note(f"declined {asked} on cursor {cursor} ({rule.flag}): {said}")
        return True
    try:
        await answer(run, question.id, value(rule.value, question.schema))
    except ValueError as e:
        raise RuleRefused(f"{rule.flag}: {asked} ({said}): {e}") from None
    run.note(f"answered {asked} with {rule.value}: {said}")
    return True


async def unanswered(run: "Run", ids: list[int]) -> int:
    """Stop for the questions no rule matches. Attached, they stay open for the server's other
    clients or the next `-p`. Local, nobody else can answer them: the run's cursor is aborted."""
    named = ", ".join(map(str, ids))
    if run.attached:
        return run.stopped(
            f"question {named} has no --answer or --decline rule; it stays open: answer it in "
            "another client, or with tau -p -c --answer (or --decline)"
        )
    await run.abort()
    return run.stopped(f"question {named} has no --answer or --decline rule")


async def answer(run: "Run", question: int, given: object) -> None:
    with contextlib.suppress(LookupError):  # closed already: the first good answer won
        await run.client.answer(question, given)
