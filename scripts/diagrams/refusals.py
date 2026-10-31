"""Which calls a cursor accepts or refuses in each state, found by making them.

For every (call, state) pair this builds a fresh session through the public
Python API (`tau`), brings one cursor to the state, makes the call once,
and writes down what came back: `ok`, or the typed exception it raised. Nothing
here is a hand-written expectation. Prints the result as JSON on stdout; the
Markdown and the SVG are made from that JSON by `render.py`.
"""

import json
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from tau import (
    Author,
    Body,
    Config,
    ItemData,
    Outcome,
    Part,
    Session,
    Stop,
)
from tau._core import Answer, Hook, Job, SampleEnd

NOW = 10


@dataclass
class Ctx:
    """What a call needs to know about the state it is made in."""

    session: Session
    cursor: int
    node: int = 1
    item: int = 0
    held: list[Session] = field(default_factory=list)


def reply(*calls: tuple[str, str]) -> Answer:
    if calls:
        return Answer.Sampled(
            [Part.Call(i, t, "{}") for i, t in calls], SampleEnd.Finished(Stop.Tools)
        )
    return Answer.Sampled([Part.Text("ok")], SampleEnd.Finished(Stop.End))


def result_for(job: Job, calls: tuple[tuple[str, str], ...]) -> list[Answer]:
    match job:
        case Job.Hooks(draft=draft):
            return [Answer.Hooks(None, draft)]
        case Job.Sample():
            return [reply(*calls)]
        case Job.RunCalls(calls=run):
            return [Answer.CallDone(c.id, Outcome.Ok("done"), Author.Tools()) for c in run]
    raise AssertionError(f"no answer for {job}")


def drive_to(ctx: Ctx, stop: Callable[[Job], bool], calls: tuple[tuple[str, str], ...] = ()) -> Job:
    """Perform jobs until `stop(job)`; that job stays out, not answer."""
    for _ in range(100):
        job = ctx.session.next_job(ctx.cursor)
        if stop(job):
            return job
        if isinstance(job, Job.Rest):
            raise AssertionError("the step ended before the state was reached")
        for answer in result_for(job, calls):
            ctx.session.answer(ctx.cursor, answer, NOW)
    raise AssertionError("state not reached")


def at_hook(point: Hook) -> Callable[[Job], bool]:
    return lambda job: isinstance(job, Job.Hooks) and job.point == point


def rest(ctx: Ctx) -> None:
    drive_to(ctx, lambda job: isinstance(job, Job.Rest))


def fresh(directory: Path | None = None) -> Ctx:
    """A session whose one cursor has an item and a label, at rest."""
    session = Session.create(Config("{}"), "sys", 0, directory)
    cursor = session.cursors()[0]
    ctx = Ctx(session, cursor)
    ctx.item = session.add_item(cursor, ItemData.Extension("ext", "note"))
    session.set_label("main", cursor)
    rest(ctx)
    return ctx


def started(ctx: Ctx) -> None:
    ctx.session.input(ctx.cursor, "term", "hi")
    ctx.session.set_auto(ctx.cursor, True)


# Each state brings `fresh()`'s cursor to one place and returns the context.
# Order is the order of the table's columns.


def s_rest(ctx: Ctx) -> None:
    pass


def s_rest_auto(ctx: Ctx) -> None:
    ctx.session.set_auto(ctx.cursor, True)
    rest(ctx)


def hook_state(point: Hook) -> Callable[[Ctx], None]:
    def build(ctx: Ctx) -> None:
        started(ctx)
        drive_to(ctx, at_hook(point))

    return build


def s_sample(ctx: Ctx) -> None:
    started(ctx)
    drive_to(ctx, lambda job: isinstance(job, Job.Sample))


def s_calls(ctx: Ctx) -> None:
    started(ctx)
    drive_to(ctx, lambda job: isinstance(job, Job.RunCalls), (("a", "t"), ("b", "t")))


def s_pending(ctx: Ctx) -> None:
    s_calls(ctx)
    ctx.session.answer(ctx.cursor, Answer.CallDone("a", Outcome.Ok("x"), Author.Tools()), NOW)
    job = ctx.session.next_job(ctx.cursor)
    assert isinstance(job, Job.Pending), job


def s_post_result(ctx: Ctx) -> None:
    started(ctx)
    drive_to(ctx, at_hook(Hook.PostResult), (("a", "t"),))


def s_unknown_cursor(ctx: Ctx) -> None:
    ctx.cursor = 999


def s_closed(ctx: Ctx) -> None:
    ctx.session.close()


STATES: list[tuple[str, str, Callable[[Ctx], None]]] = [
    ("rest", "at rest", s_rest),
    ("rest_auto", "at rest, auto-step on", s_rest_auto),
    ("pre_step", "busy: pre_step hook out", hook_state(Hook.PreStep)),
    ("pre_input", "busy: pre_input hook out", hook_state(Hook.PreInput)),
    ("pre_sample", "busy: pre_sample hook out", hook_state(Hook.PreSample)),
    ("sample", "busy: sample out", s_sample),
    ("pre_reply", "busy: pre_reply hook out", hook_state(Hook.PreReply)),
    ("post_reply", "busy: post_reply hook out", hook_state(Hook.PostReply)),
    ("calls", "busy: calls out", s_calls),
    ("pending", "busy: calls out, one answered", s_pending),
    ("post_result", "busy: post_result hook out", s_post_result),
    ("post_step", "busy: post_step hook out", hook_state(Hook.PostStep)),
    ("unknown_cursor", "a cursor that is not open", s_unknown_cursor),
    ("closed", "session closed", s_closed),
]

# The failed-save state needs a directory, so it is built apart.


def s_store_failed(ctx: Ctx, directory: Path) -> None:
    block = directory / "scratchpad.new"
    block.mkdir()  # the store cannot create its temporary scratchpad file here
    try:
        ctx.session.input(ctx.cursor, "term", "x")
    except OSError:
        pass
    else:
        raise AssertionError("the save was meant to fail")
    block.rmdir()


STORE_FAILED = ("store_failed", "closed by a failed save")

Call = Callable[[Ctx], object]

# (group, name, call). A call returns what the API returned.
CALLS: list[tuple[str, str, Call]] = [
    ("frontend", "input", lambda c: c.session.input(c.cursor, "term", "hi")),
    ("frontend", "step", lambda c: c.session.step(c.cursor)),
    ("frontend", "go_to (an ancestor)", lambda c: c.session.go_to(c.cursor, 0, "term", NOW)),
    ("frontend", "go_to (not a node)", lambda c: c.session.go_to(c.cursor, 999, "term", NOW)),
    ("frontend", "set_auto(on)", lambda c: c.session.set_auto(c.cursor, True)),
    ("frontend", "set_auto(off)", lambda c: c.session.set_auto(c.cursor, False)),
    ("frontend", "abort", lambda c: c.session.abort(c.cursor)),
    ("frontend", "set_label", lambda c: c.session.set_label("other", c.cursor)),
    ("frontend", "remove_label", lambda c: c.session.remove_label("main")),
    ("extension", "open", lambda c: c.session.open(1, Author.Extension("ext"), NOW)),
    (
        "extension",
        "add_item",
        lambda c: c.session.add_item(c.cursor, ItemData.Extension("ext", "n")),
    ),
    ("extension", "remove_item", lambda c: c.session.remove_item(c.item)),
    (
        "extension",
        "append (under at)",
        lambda c: c.session.append(
            c.session.cursor(c.cursor).at, Body.Custom("note", ""), Author.Core(), NOW
        ),
    ),
    (
        "hook",
        "hook_move",
        lambda c: c.session.hook_move(c.cursor, 0, Author.Extension("ext"), NOW),
    ),
    (
        "hook",
        "hook_write",
        lambda c: c.session.hook_write(
            c.cursor, Body.Custom("note", ""), [], Author.Extension("ext"), NOW
        ),
    ),
    (
        "hook",
        "hook_set_auto",
        lambda c: c.session.hook_set_auto(c.cursor, True),
    ),
    ("driver", "answer Hooks", lambda c: c.session.answer(c.cursor, Answer.Hooks(), NOW)),
    ("driver", "next_job", lambda c: c.session.next_job(c.cursor)),
    ("read", "cursor", lambda c: c.session.cursor(c.cursor)),
]


def outcome(call: Call, ctx: Ctx) -> str:
    try:
        value = call(ctx)
    except Exception as error:  # recording every refusal is the point
        return type(error).__name__
    if value is True:
        return "ok (true)"
    return "ok"


def cells() -> Iterator[tuple[str, str, str]]:
    for state_id, _, build in STATES:
        for _, name, call in CALLS:
            ctx = fresh()
            build(ctx)
            yield state_id, name, outcome(call, ctx)
    for _, name, call in CALLS:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "s"
            ctx = fresh(directory)
            s_store_failed(ctx, directory)
            yield STORE_FAILED[0], name, outcome(call, ctx)


def main() -> None:
    states = [(i, label) for i, label, _ in STATES] + [STORE_FAILED]
    result = {
        "states": [{"id": i, "label": label} for i, label in states],
        "calls": [{"group": g, "name": n} for g, n, _ in CALLS],
        "cells": {f"{s}|{c}": v for s, c, v in cells()},
    }
    json.dump(result, sys.stdout, indent=1, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
