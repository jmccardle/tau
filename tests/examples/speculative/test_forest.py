"""examples/speculative/forest.py through the public API: parallel branches, scores in the tree.

Every scripted sample is consumed in the order the cursors ask the provider: a round's candidates
(cursor id order), then its judges (candidate order). `FakeProvider` cannot answer by request, so
the scores asserted per candidate text also check that order.
"""

import asyncio
import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from fakes import FAKE_MODEL, FakeProvider, Gate, Reply
from harness import Harness, say, scenario

from tau import (
    Appended,
    Body,
    CommandFailed,
    Config,
    CursorView,
    Ending,
    Live,
    Notice,
    Part,
    Stop,
    TextDelta,
    model_config,
)
from tau.commandline import parse


def load(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "examples" / "speculative" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"speculative_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


forest = load("forest")
START = 1  # the System node: where the main cursor begins


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("never happened")


def options(**values: object) -> Config:
    return model_config(FAKE_MODEL, {"forest": values})


def verdicts(*marks: float) -> list[Reply]:
    return [say(f"SCORE: {m}") for m in marks]


def texts(h: Harness) -> dict[str, float]:
    """Each scored candidate's text, and its score."""
    tree = h.session.tree
    return {forest.text_of(tree, s.reply): s.value for s in forest.scores(tree, START).values()}


# Two rounds of three: A B C scored, then B expanded into B1 B2 B3.
TWO_ROUNDS = [
    *verdicts(0.2, 0.9, 0.5),
    *[say(t) for t in ("ans B1", "ans B2", "ans B3")],
    *verdicts(0.95, 0.4, 0.7),
]


def two_rounds(gate: Gate | None = None) -> FakeProvider:
    first = [Reply([Part.Text(t)], gate=gate) for t in ("ans A", "ans B", "ans C")]
    return FakeProvider([*first, *TWO_ROUNDS])


@scenario
async def test_n_cursors_sample_at_once_and_the_main_cursor_goes_to_the_best_leaf(
    tmp_path: Path,
) -> None:
    gate = Gate()
    h = await Harness.create(tmp_path, provider=two_rounds(gate), extensions=[forest.Forest()])
    running = asyncio.ensure_future(h.run("search", {"n": 3, "question": "q"}))
    await until(lambda: len(h.provider.requests) == 3)
    assert gate.held == 3, "all three samples are in flight together"
    assert [h.session.cursor(c).busy for c in h.session.cursors()] == [True] * 4
    gate.release()
    result = await asyncio.wait_for(running, 5)
    await h.settle()
    assert texts(h) == {
        "ans A": 0.2,
        "ans B": 0.9,
        "ans C": 0.5,
        "ans B1": 0.95,
        "ans B2": 0.4,
        "ans B3": 0.7,
    }
    assert result == (
        "#5  0.20  (n=1, mean 0.20)  ans A\n"
        "#6  0.90  (n=4, mean 0.74)  ans B\n"
        "  #20  0.95  (n=1, mean 0.95)  ans B1\n"
        "  #21  0.40  (n=1, mean 0.40)  ans B2\n"
        "  #22  0.70  (n=1, mean 0.70)  ans B3\n"
        "#7  0.50  (n=1, mean 0.50)  ans C\n"
        "best #20 (0.95); 12 samples, 0 tokens"
    ), "B's mean backs up its three refinements"
    assert h.session.cursor(h.cursor).at == 20
    assert h.notices()[0].startswith("round 1: [0.2, 0.9, 0.5], 6/12")
    assert len(h.notices()) == 2
    assert len(h.session.cursors()) == 1 + 12, (
        "12 samples opened 12 cursors, and nothing closes them"
    )
    assert not any(h.session.cursor(c).busy for c in h.session.cursors())
    await h.close()


@scenario
async def test_the_second_round_expands_what_the_first_scored_best(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=two_rounds(), extensions=[forest.Forest()])
    await h.run("search", {"n": 3, "question": "q"})
    tree = h.session.tree
    walks = [[tree.node(i).body for i in r.walk] for r in h.provider.requests]
    refine = [w for w in walks if w[-1] == Body.User("Improve on that answer.")]
    assert len(refine) == 3, "three refinements"
    assert all(w[-2] == Body.Assistant([Part.Text("ans B")], Stop.End) for w in refine), (
        "each sees B, not A or C"
    )
    judge = [w for w in walks if w[-1] == Body.User(forest.JUDGE)]
    assert len(judge) == 6
    await h.close()


@scenario
async def test_a_frontend_sees_cursors_scores_and_progress(tmp_path: Path) -> None:
    """What a TUI has to work with: a CursorView per new cursor, Appended score nodes, Notices."""
    gate = Gate()
    first = [
        Reply([TextDelta(t), Part.Text(t)], gate=gate, gate_after=1) for t in ("ans A", "ans B")
    ]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([*first, *verdicts(0.4, 0.6)]),
        extensions=[forest.Forest()],
        config=options(max_samples=4),
    )
    tui = await h.connect("tui")
    views: dict[int, CursorView] = {}
    seen: list[object] = []

    async def listen() -> None:
        async for event in tui:
            seen.append(event)
            if isinstance(event, CursorView):
                views[event.cursor] = event

    listener = asyncio.ensure_future(listen())
    running = asyncio.ensure_future(h.run("search", {"n": 2, "question": "q"}))
    await until(lambda: gate.held == 2)
    await until(lambda: sum(v.busy for v in views.values()) == 3)
    kids = sorted(c for c, v in views.items() if c != h.cursor and v.busy)
    assert len(kids) == 2, "the tui knows both branches are sampling, and that they are cursors"
    await tui.watch(kids)
    await until(lambda: sum(isinstance(e, Live) for e in seen) == 2)
    assert [e.delta for e in seen if isinstance(e, Live)] == [
        TextDelta("ans A"),
        TextDelta("ans B"),
    ]
    gate.release()
    await asyncio.wait_for(running, 5)
    await h.settle()
    await tui.close()
    await listener

    appended = [n for e in seen if isinstance(e, Appended) for n in e.nodes]
    kinds = [n.body.kind for n in appended if isinstance(n.body, Body.Custom)]
    assert kinds == ["forest/score"] * 2, "a score is an ordinary node: no new event kind"
    assert not [e for e in seen if isinstance(e, Notice)], "progress went to the client that ran it"
    assert h.notices() == ["round 1: [0.4, 0.6], 4/4 samples"]
    best = max(forest.scores(h.session.tree, START).values(), key=lambda s: s.value)
    assert views[h.cursor].at == best.reply, "the tui learns where the main cursor went"
    await h.close()


@scenario
async def test_scores_survive_a_restart_and_any_client_can_read_them_back(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=two_rounds(), extensions=[forest.Forest()])
    result = await h.run("search", {"n": 3, "question": "q"})
    assert result is not None
    assert h.session.cursor(h.cursor).busy, "run returned with the command; its step is not over"
    await h.settle()  # closing now would lose the score nodes: the step has not synced yet
    await h.close()
    again = await Harness.load(tmp_path, extensions=[forest.Forest()])
    assert texts(again)["ans B1"] == 0.95
    shown = await again.run("forest_tree", {"node": START})
    assert shown == result.rsplit("\n", 1)[0], "the same tree, from the log alone"
    assert not again.session.cursor(again.cursor).auto
    await again.close()


@scenario
async def test_abort_reaches_the_candidates_mid_sample_and_no_more_samples_run(
    tmp_path: Path,
) -> None:
    gate = Gate()
    h = await Harness.create(tmp_path, provider=two_rounds(gate), extensions=[forest.Forest()])
    running = asyncio.ensure_future(h.run("search", {"n": 3, "question": "q"}))
    await until(lambda: gate.held == 3)
    await h.abort()
    await h.settle()
    with pytest.raises(CommandFailed, match="Aborted"):
        await running
    assert not h.session.cursor(h.cursor).busy
    assert [(await h.view(c)).ending for c in (1, 2, 3)] == [Ending.Aborted()] * 3
    assert [h.session.cursor(c).busy for c in (1, 2, 3)] == [False] * 3
    assert gate.held == 0, "their samples were cancelled at the gate"
    gate.release()
    await h.settle()
    assert len(h.provider.requests) == 3, "no judge ran, and nothing resumed"
    assert h.session.cursor(h.cursor).at == START
    assert texts(h) == {}
    await h.close()


@scenario
async def test_abort_reaches_the_judges_mid_sample_and_no_more_samples_run(tmp_path: Path) -> None:
    gate = Gate()
    judging = [Reply([Part.Text("SCORE: 0.5")], gate=gate)] * 2
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("ans A"), say("ans B"), *judging, say("never")]),
        extensions=[forest.Forest()],
        config=options(max_samples=8),
    )
    running = asyncio.ensure_future(h.run("search", {"n": 2, "question": "q"}))
    await until(lambda: gate.held == 2)
    await h.abort()
    await h.settle()
    with pytest.raises(CommandFailed, match="Aborted"):
        await running
    judges = [c for c in h.session.cursors() if c > 2]
    assert len(judges) == 2
    assert [(await h.view(c)).ending for c in judges] == [Ending.Aborted()] * 2
    assert not any(h.session.cursor(c).busy for c in h.session.cursors())
    gate.release()
    await h.settle()
    assert len(h.provider.requests) == 4, "two candidates, two judges, no refinement"
    assert texts(h) == {}, "no score was written"
    await h.close()


@scenario
async def test_a_frontend_aborts_one_branch_and_the_search_goes_on_without_it(
    tmp_path: Path,
) -> None:
    gate = Gate()
    first = [Reply([Part.Text(t)], gate=gate) for t in ("ans A", "ans B", "ans C")]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([*first, *verdicts(0.3, 0.8)]),
        extensions=[forest.Forest()],
        config=options(max_samples=6),
    )
    running = asyncio.ensure_future(h.run("search", {"n": 3, "question": "q"}))
    await until(lambda: gate.held == 3)
    await h.abort(2)  # the middle branch: "ans B" was its sample
    gate.release()
    result = await asyncio.wait_for(running, 5)
    assert result is not None
    assert texts(h) == {"ans A": 0.3, "ans C": 0.8}
    assert result.endswith("4 samples, 0 tokens")
    await h.close()


@scenario
async def test_the_judge_is_searched_when_asked_to_be(tmp_path: Path) -> None:
    """Two verdicts on one answer; each is rated by one judge; the better-rated verdict stands."""
    script = [say("ans A"), *verdicts(0.2, 0.8), *verdicts(0.3, 0.9)]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        extensions=[forest.Forest()],
        config=options(judges=2, judge_depth=1, max_samples=5),
    )
    result = await h.run("search", {"n": 1, "question": "q"})
    assert texts(h) == {"ans A": 0.8}, "the verdict rated 0.9 says 0.8"
    assert result is not None
    assert result.endswith("5 samples, 0 tokens")
    (scored,) = forest.scores(h.session.tree, START).values()
    assert len(scored.verdicts) == 2
    await h.close()


@scenario
async def test_a_judge_that_gives_no_score_fails_the_search_and_leaves_the_candidates(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([say("ans A"), say("I like it")]),
        extensions=[forest.Forest()],
    )
    with pytest.raises(CommandFailed, match="ValueError: the judge gave no score: 'I like it'"):
        await h.run("search", {"n": 1, "question": "q"})
    view = await h.view()
    assert view.hook_error is not None
    assert (view.hook_error.extension, view.hook_error.point) == ("forest", "pre_step")
    assert h.session.cursor(h.cursor).at == START, "the main cursor did not move"
    assert texts(h) == {}
    await h.close()


@scenario
async def test_a_budget_below_one_round_is_refused_before_any_sample(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[forest.Forest()], config=options(max_samples=3))
    with pytest.raises(CommandFailed, match="one round costs 6 samples, max_samples is 3"):
        await h.run("search", {"n": 3, "question": "q"})
    assert h.provider.requests == []
    await h.close()


@scenario
async def test_the_person_steers_between_rounds_with_a_question(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=two_rounds(),
        extensions=[forest.Forest()],
        config=options(steer=True),
    )
    running = asyncio.ensure_future(h.run("search", {"n": 3, "question": "q"}))
    await h.settle()
    (question,) = (await h.view()).questions
    assert question.text.startswith("round 1: [0.2, 0.9, 0.5], 6/12 samples. Expand #")
    assert question.schema.get("enum") == ["yes", "no"]
    await h.respond("no")
    result = await asyncio.wait_for(running, 5)
    assert result is not None
    assert result.endswith("6 samples, 0 tokens")
    assert (
        h.session.cursor(h.cursor).at
        == max(forest.scores(h.session.tree, START).values(), key=lambda s: s.value).reply
    )
    await h.close()


@scenario
async def test_tokens_are_summed_from_the_replies_usage(tmp_path: Path) -> None:
    usage = '{"usage": {"input": 10, "output": 5}}'
    script = [Reply([Part.Text("ans A")], meta=usage), Reply([Part.Text("SCORE: 1")], meta=usage)]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        extensions=[forest.Forest()],
        config=options(max_samples=2),
    )
    result = await h.run("search", {"n": 1, "question": "q"})
    assert result is not None
    assert result.endswith("2 samples, 30 tokens")
    await h.close()


@scenario
async def test_the_question_is_the_rest_of_the_command_line(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[forest.Forest()])
    commands = await h.client.commands()
    parsed = parse("/search 3 what is 2+2", commands, {})
    assert parsed.args == {"n": 3, "question": "what is 2+2"}
    await h.close()


@scenario
async def test_a_failed_judge_stops_the_branches_still_sampling(tmp_path: Path) -> None:
    gate = Gate()
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(
            [
                say("ans A"),
                say("ans B"),
                say("I like it"),
                Reply([Part.Text("SCORE: 1")], gate=gate),
            ]
        ),
        extensions=[forest.Forest()],
        config=options(max_samples=8),
    )
    with pytest.raises(CommandFailed, match="the judge gave no score"):
        await h.run("search", {"n": 2, "question": "q"})
    await h.settle()
    assert not any(h.session.cursor(c).busy for c in h.session.cursors())
    assert (await h.view(4)).ending == Ending.Aborted(), "B's judge was held, and is stopped"
    gate.release()
    await h.settle()
    assert len(h.provider.requests) == 4, "nothing sampled after the failure"
    assert texts(h) == {}
    await h.close()
