"""`/prune` (D24 section 9): a conversation shortened without a model.

The walk is rewritten by the user's turns: old turns become a transcript in the user text of the
turn before the latest, that turn keeps its calls with outcomes reduced to ok or error, and the
latest keeps its results. The result is a new line beside the old, and the cursor moves onto it."""

import json
from pathlib import Path

from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, scenario

from tau import (
    Author,
    Body,
    Node,
    Outcome,
    Part,
    Prune,
    Stop,
)

BIG = "x" * 300


def call(id: str, **args: object) -> Part:
    return Part.Call(id, "work", json.dumps(args))


def asks(*parts: Part) -> Reply:
    return Reply([Part.Thinking("let me think"), *parts], Stop.Tools)


def says(text: str) -> Reply:
    return Reply([Part.Thinking("done thinking"), Part.Text(text)], Stop.End)


def thoughtful(text: str) -> Reply:
    """A reply with a long train of thought, which is what prune has to shed."""
    return Reply([Part.Thinking("t" * 300), Part.Text(text)], Stop.End)


def conversation() -> list[Reply]:
    """Four turns: two calls in the first, one in each other, a long argument in each."""
    return [
        asks(call("a", note=BIG), call("b", path="one.txt")),
        says("Done one."),
        asks(call("c", path="two.txt")),
        says("Finished two."),
        asks(call("d", path="three.txt", note=BIG)),
        says("Three done."),
        asks(call("e", path="four.txt", note=BIG)),
        says("Four done."),
        says("Five done."),
    ]


async def talked(tmp_path: Path, turns: int = 4) -> Harness:
    work = FakeTool("work", "the result is long enough to see")
    h = await Harness.create(
        tmp_path, provider=FakeProvider(conversation()), tools=[work], extensions=[Prune()]
    )
    h.session.set_auto(h.cursor, True)
    for n in range(turns):
        await h.send(["task one", "task two", "task three", "task four"][n])
        await h.settle()
    return h


async def prune(h: Harness) -> str | None:
    """Run `/prune` and let its step end."""
    result = await h.run("prune")
    await h.settle()
    return result


def _part(part: Part) -> str:
    match part:
        case Part.Thinking():
            return "think"
        case Part.Text(text=text):
            return f"text:{text}"
        case Part.Call(id=id, args_raw=args):
            return f"call:{id}:{args}"
        case _:
            raise AssertionError(part)


def shown(node: Node) -> str:
    """One line of a node: what a reader of the walk sees."""
    match node.body:
        case Body.User(text=text):
            return f"user {text!r}"
        case Body.Assistant(parts=parts):
            return "assistant " + " ".join(_part(p) for p in parts)
        case Body.Results(outcomes=outcomes):
            return "results " + " ".join(
                f"{id}={o.value if isinstance(o, Outcome.Ok) else o}" for id, o, _ in outcomes
            )
        case other:
            return type(other).__name__


def line(h: Harness) -> list[Node]:
    walk = h.session.tree.walk(h.session.cursor(h.cursor).at)
    return walk[2:]  # after the root Config and the System prefix


@scenario
async def test_prune_writes_a_new_line_and_moves_the_cursor_onto_it(tmp_path: Path) -> None:
    h = await talked(tmp_path)
    old_tip = h.session.cursor(h.cursor).at
    old_size = len(h.session.tree.walk(old_tip))
    result = await prune(h)
    assert result is not None
    assert result.startswith("pruned ~")
    new_tip = h.session.cursor(h.cursor).at
    assert new_tip != old_tip
    assert len(h.session.tree.walk(new_tip)) < old_size
    assert h.session.tree.walk(old_tip)[-1].body == Body.Assistant(
        [Part.Thinking("done thinking"), Part.Text("Four done.")], Stop.End, None, None
    ), "the old line is untouched"
    await h.close()


@scenario
async def test_the_transcript_is_prepended_to_the_user_text_of_the_turn_before_the_latest(
    tmp_path: Path,
) -> None:
    h = await talked(tmp_path)
    await prune(h)
    first = line(h)[0]
    assert isinstance(first.body, Body.User)
    assert first.body.text == (
        "<pruned>\n"
        "User: task one\n"
        "[pruned: 1 message, 2 tool calls]\n"
        "Assistant: Done one.\n"
        "User: task two\n"
        "[pruned: 1 message, 1 tool call]\n"
        "Assistant: Finished two.\n"
        "</pruned>\n\n"
        "task three"
    )
    await h.close()


@scenario
async def test_turn_n_minus_1_reduces_outcomes_and_turn_n_keeps_its_results(
    tmp_path: Path,
) -> None:
    h = await talked(tmp_path)
    await prune(h)
    lines = [shown(n) for n in line(h)]
    pruned = '{"path": "three.txt", "note": "[pruned: 300 chars]"}'
    assert lines[1:] == [
        f"assistant call:d:{pruned}",
        "results d=ok",
        "assistant text:Three done.",
        "user 'task four'",
        'assistant call:e:{"path": "four.txt", "note": "[pruned: 300 chars]"}',
        "results e=the result is long enough to see",
        "assistant text:Four done.",
    ]
    await h.close()


@scenario
async def test_the_new_line_records_what_it_copied_and_what_it_computed(tmp_path: Path) -> None:
    h = await talked(tmp_path)
    old = h.session.tree.walk(h.session.cursor(h.cursor).at)
    await prune(h)
    new = line(h)
    by_id = {n.id: n for n in old}
    transcript = new[0]
    assert transcript.derived_from == next(n.id for n in old if shown(n) == "user 'task three'")
    assert transcript.copied_from is None
    user_four = next(n for n in new if shown(n) == "user 'task four'")
    assert by_id[user_four.copied_from or -1].body == user_four.body
    assert user_four.derived_from is None
    results = next(n for n in new if shown(n) == "results d=ok")
    assert results.copied_from is None
    assert results.derived_from is not None
    assert results.by == Author.Extension("prune")
    assert new[0].parent == 1, "the new line hangs from the end of the prefix"
    await h.close()


@scenario
async def test_the_next_sample_sends_the_pruned_line(tmp_path: Path) -> None:
    h = await talked(tmp_path)
    await prune(h)
    pruned = [n.id for n in h.session.tree.walk(h.session.cursor(h.cursor).at)]
    await h.send("task five")
    await h.settle()
    sent = h.provider.requests[-1].walk
    assert list(sent[:-1]) == pruned
    assert len(sent) == len(pruned) + 1
    await h.close()


@scenario
async def test_a_second_prune_has_nothing_to_do(tmp_path: Path) -> None:
    h = await talked(tmp_path)
    await prune(h)
    tip = h.session.cursor(h.cursor).at
    assert await prune(h) == "nothing to prune"
    assert h.session.cursor(h.cursor).at == tip
    await h.close()


@scenario
async def test_one_turn_has_only_its_reasoning_to_lose(tmp_path: Path) -> None:
    h = await talked(tmp_path, turns=1)
    await prune(h)
    lines = [shown(n) for n in line(h)]
    assert lines[0] == "user 'task one'"
    assert lines[1].startswith("assistant call:a:")
    assert "think" not in " ".join(lines)
    assert lines[2].startswith("results a=the result")
    await h.close()


@scenario
async def test_pruning_again_after_more_turns_flattens_the_earlier_transcript(
    tmp_path: Path,
) -> None:
    h = await talked(tmp_path)
    await prune(h)
    await h.send("task five")
    await h.settle()
    await prune(h)
    first = line(h)[0]
    assert isinstance(first.body, Body.User)
    text = first.body.text
    assert text.count("<pruned>") == 1
    for turn in ("one", "two", "three"):
        assert f"User: task {turn}" in text
    assert text.endswith("task four")
    await h.close()


@scenario
async def test_prune_keeps_the_config_nodes_it_passes(tmp_path: Path) -> None:
    work = FakeTool("work", "result")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([thoughtful("one"), thoughtful("two"), thoughtful("three")]),
        tools=[work],
        extensions=[Prune()],
    )
    for n, text in enumerate(["task one", "task two", "task three"]):
        h.session.set_auto(h.cursor, True)
        await h.send(text)
        await h.settle()
        if n == 0:
            await h.configure("prune", {})
            await h.settle()
    old = h.session.tree.walk(h.session.cursor(h.cursor).at)
    configs = [n.id for n in old[2:] if isinstance(n.body, Body.Config)]
    assert configs, "the test needs a Config node in an old turn"
    await prune(h)
    copied = [n.copied_from for n in line(h) if isinstance(n.body, Body.Config)]
    assert copied == configs
    await h.close()


@scenario
async def test_a_prune_that_would_not_be_shorter_writes_nothing(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([Reply([Part.Text(t)]) for t in ("one", "two", "three")]),
        extensions=[Prune()],
    )
    h.session.set_auto(h.cursor, True)
    for text in ("task one", "task two", "task three"):
        await h.send(text)
        await h.settle()
    tip = h.session.cursor(h.cursor).at
    assert await prune(h) == "nothing to prune: it would not be shorter"
    assert h.session.cursor(h.cursor).at == tip
    assert not [n for n in h.nodes() if n.copied_from is not None or n.derived_from is not None]
    await h.close()


@scenario
async def test_long_strings_inside_lists_and_objects_are_pruned_too(tmp_path: Path) -> None:
    edits = json.dumps({"path": "p.txt", "edits": [{"old": BIG, "new": "short"}], "n": 3})
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(
            [
                says("one"),
                says("two"),
                asks(Part.Call("e", "work", edits)),
                says("three"),
            ]
        ),
        tools=[FakeTool("work", "done")],
        extensions=[Prune()],
    )
    h.session.set_auto(h.cursor, True)
    for text in ("task one", "task two", "task three"):
        await h.send(text)
        await h.settle()
    await prune(h)
    calls = [r for r in (shown(n) for n in line(h)) if r.startswith("assistant call:e:")]
    assert calls == [
        'assistant call:e:{"path": "p.txt", "edits": [{"old": "[pruned: 300 chars]", '
        '"new": "short"}], "n": 3}'
    ]
    await h.close()
