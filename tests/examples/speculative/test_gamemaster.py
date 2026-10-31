"""examples/speculative/gamemaster.py through the Harness: the cast, a scene, an abort."""

import importlib.util
import random
from collections.abc import AsyncGenerator, Mapping, Sequence
from pathlib import Path

import pytest
from fakes import Fail, FakeProvider, Gate, Reply
from harness import Harness, say, scenario

from tau import (
    Appended,
    Author,
    Body,
    CommandFailed,
    CursorView,
    Delta,
    Ending,
    Finish,
    Labels,
    Live,
    ModelConfig,
    Node,
    Outcome,
    Part,
    Stop,
    TextDelta,
    Tool,
    ToolProgress,
    model_config,
    read_models,
)

SPEC = importlib.util.spec_from_file_location(
    "gamemaster", Path(__file__).resolve().parents[3] / "examples" / "speculative" / "gamemaster.py"
)
assert SPEC is not None
assert SPEC.loader is not None
gamemaster = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gamemaster)

MODELS = ("gm", "mira", "borin", "cass")


class ByModel(FakeProvider):
    """One FakeProvider cannot script several cursors independently: its script is one queue,
    taken in the order samples start. This routes each sample to the script of its model, so
    a cursor's replies are its own whatever the other cursors do."""

    def __init__(self, scripts: Mapping[str, Sequence[Reply | Fail]]) -> None:
        super().__init__([])
        self.by_model = {m: FakeProvider(scripts.get(m, [])) for m in MODELS}

    @property
    def gates(self) -> list[Gate]:
        return [g for p in self.by_model.values() for g in p.gates]

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        async for item in self.by_model[model.model].sample(walk, model, tools):
            yield item

    def asked(self, model: str) -> int:
        return len(self.by_model[model].requests)


def calls(*calls: tuple[str, str, str]) -> Reply:
    return Reply([Part.Call(c, tool, args) for c, tool, args in calls], Stop.Tools)


async def stage(
    tmp_path: Path, scripts: Mapping[str, Sequence[Reply | Fail]], seed: int = 7
) -> tuple[Harness, ByModel]:
    """A GM on `gm`, and `Mira` on `mira`, `Borin` on `borin`, `Cass` on `cass`, introduced by
    the player's `/character` command."""
    provider = ByModel(scripts)
    catalog = read_models(cli={"models": {m: {"model": m, "provider": "fake"} for m in MODELS}})
    gm = gamemaster.Gamemaster(random.Random(seed))
    h = await Harness.create(
        tmp_path,
        config=model_config("gm"),
        system=gamemaster.GM_SYSTEM,
        provider=provider,
        catalog=catalog,
        extensions=[gm],
    )
    for name, persona in [
        ("Mira", "a wary innkeeper"),
        ("Borin", "a dwarf who owes her money"),
        ("Cass", "a sleeping cat"),
    ]:
        await h.run("character", {"name": name, "persona": persona, "model": name.lower()})
        await h.settle()  # `run` returns when the step has ended, a beat before the cursor rests
    await h.settle()
    return h, provider


def cursor_of(h: Harness, name: str) -> int:
    return h.session.label(f"char:{name}")


def spoken(h: Harness, cursor: int | None = None) -> list[tuple[str, str]]:
    """A cursor's walk as (role, text), from its first message."""
    out: list[tuple[str, str]] = []
    for body in h.bodies(cursor):
        match body:
            case Body.System(text=t):
                out.append(("system", t))
            case Body.User(text=t):
                out.append(("user", t))
            case Body.Assistant(parts=parts):
                out.append(
                    ("assistant", "".join(p.text for p in parts if isinstance(p, Part.Text)))
                )
            case Body.Results(outcomes=outcomes):
                out.append(("results", " | ".join(str(o) for _, o, _ in outcomes)))
            case _:
                pass
    return out


def outcomes(h: Harness) -> dict[str, Outcome]:
    """Every tool result in the GM's walk, by call id."""
    return {
        call_id: outcome
        for body in h.bodies()
        if isinstance(body, Body.Results)
        for call_id, outcome, _ in body.outcomes
    }


SCENE = calls(
    ("r", "roll", '{"dice": "2d6+1"}'),
    ("a", "ask_characters", '{"who": ["Mira", "Borin"], "message": "Borin walks in, wet."}'),
)


@scenario
async def test_the_cast_is_cursors_each_with_a_persona_and_its_own_model(tmp_path: Path) -> None:
    h, _ = await stage(tmp_path, {})
    cast = {n: cursor_of(h, n) for n in ("Mira", "Borin", "Cass")}
    assert sorted(cast.values()) == [1, 2, 3]
    for name, cursor in cast.items():
        role, text = spoken(h, cursor)[0]
        assert (role, text.startswith(f"You are {name}, a character")) == ("system", True)
        assert len(spoken(h, cursor)) == 1, "its history is its persona and nothing else"
    assert len(spoken(h)) == 1, "the GM's own conversation is untouched by the cast"
    assert await h.run("present") == "Mira (cursor 1)\nBorin (cursor 2)\nCass (cursor 3)"
    with pytest.raises(CommandFailed, match="Mira is already in the story"):
        await h.run("character", {"name": "Mira", "persona": "another"})


@scenario
async def test_the_cast_survives_a_reload_because_it_is_only_labels(tmp_path: Path) -> None:
    h, _ = await stage(tmp_path, {})
    await h.crash()
    await h.settle()  # a reload wakes every cursor, so each is `busy` for one empty step
    assert await h.run("present") == "Mira (cursor 1)\nBorin (cursor 2)\nCass (cursor 3)"


@scenario
async def test_the_gm_introduces_a_character_itself(tmp_path: Path) -> None:
    gm = [
        calls(("i", "introduce", '{"name": "Dov", "persona": "a thief", "model": "cass"}')),
        say("Dov slips in."),
    ]
    h, _ = await stage(tmp_path, {"gm": gm})
    await h.send("Someone is watching.")
    await h.settle()
    assert outcomes(h)["i"] == Outcome.Ok("Dov has joined the story")
    assert spoken(h, cursor_of(h, "Dov"))[0][1].startswith("You are Dov")


@scenario
async def test_a_scene_asks_only_those_present_and_they_answer_in_parallel(tmp_path: Path) -> None:
    mira, borin = Gate(), Gate()
    h, provider = await stage(
        tmp_path,
        {
            "gm": [SCENE, say("Mira stiffens as Borin drips on her floor.")],
            "mira": [Reply([Part.Text("You owe me, Borin.")], gate=mira)],
            "borin": [Reply([Part.Text("Aye. Ale first.")], gate=borin)],
        },
    )
    await h.send("Borin enters the tavern.")
    await h.settle()
    assert (mira.held, borin.held) == (1, 1), "both characters are mid-sample at once"
    assert h.session.cursor(h.cursor).busy, "the GM waits inside its tool call"
    mira.release()
    borin.release()
    await h.settle()

    # Who was sampled: the cost, countable from the provider and visible in the tree.
    assert [provider.asked(m) for m in MODELS] == [2, 1, 1, 0]
    assert len(spoken(h, cursor_of(h, "Cass"))) == 1, "Cass heard nothing and was never sampled"
    assert not any(h.session.cursor(cursor_of(h, n)).busy for n in ("Mira", "Borin")), (
        "the GM leaves the characters at rest: success aborts no one"
    )

    # In Mira's cursor the GM is the user and she is the assistant.
    assert spoken(h, cursor_of(h, "Mira"))[1:] == [
        ("user", "Borin walks in, wet."),
        ("assistant", "You owe me, Borin."),
    ]
    told = h.session.tree.node(h.session.cursor(cursor_of(h, "Mira")).at - 1)
    assert told.by == Author.Extension("gamemaster"), "the GM wrote it, not the player"

    # The GM's walk: the tool results carry the raw lines, the next reply is the summary.
    rolled = random.Random(7)
    dice = [rolled.randint(1, 6), rolled.randint(1, 6)]
    assert outcomes(h)["r"] == Outcome.Ok(f"2d6+1: {dice} +1 = {sum(dice) + 1}")
    assert outcomes(h)["a"] == Outcome.Ok(
        "Mira: You owe me, Borin.\nBorin: Aye. Ale first.\n(not asked: Cass)"
    )
    assert spoken(h)[-1] == ("assistant", "Mira stiffens as Borin drips on her floor.")
    assert provider.by_model["gm"].requests[1].walk[-1] == h.session.cursor(h.cursor).at - 1


@scenario
async def test_a_frontend_sees_every_cursor_and_can_tell_who_was_called(tmp_path: Path) -> None:
    h, _ = await stage(
        tmp_path,
        {
            "gm": [SCENE, say("They glare.")],
            "mira": [say("Mira glares.")],
            "borin": [say("Borin shrugs.")],
        },
    )
    await h.client.watch([0, cursor_of(h, "Mira")])
    mark = len(h.heard)
    await h.send("Borin enters.")
    await h.settle()
    heard = h.heard[mark:]

    # A client is told of every cursor; the characters are only known by their label.
    labels = [e for e in h.heard if isinstance(e, Labels)]
    assert dict(labels[-1].labels) == {"char:Mira": 1, "char:Borin": 2, "char:Cass": 3}
    ran = {e.cursor for e in heard if isinstance(e, CursorView) and e.busy}
    assert ran == {0, 1, 2}, "the cost is visible: Cass (3) never ran a step"
    appended = [n for e in heard if isinstance(e, Appended) for n in e.nodes]
    users = [n for n in appended if isinstance(n.body, Body.User)]
    # An Appended node does not say which cursor it belongs to; the frontend asks the tree.
    owners = {n.id: [c for c in (1, 2, 3) if n.id in walk_ids(h, c)] for n in users}
    assert [owners[n.id] for n in users if n.by == Author.Extension("gamemaster")] == [[1], [2]]
    # The GM's cursor, watched, shows the scene advancing as each character answers.
    shown = [
        e.delta.text for e in heard if isinstance(e, Live) and isinstance(e.delta, ToolProgress)
    ]
    assert sorted(shown) == ["Borin has answered", "Mira has answered"]


def walk_ids(h: Harness, cursor: int) -> list[int]:
    return [n.id for n in h.session.tree.walk(h.session.cursor(cursor).at)]


@scenario
async def test_the_player_can_watch_a_character_think_and_talk_to_it_directly(
    tmp_path: Path,
) -> None:
    h, _ = await stage(
        tmp_path,
        {"mira": [Reply([TextDelta("Wh"), TextDelta("at?"), Part.Text("What?")])]},
    )
    mira = cursor_of(h, "Mira")
    await h.client.watch([mira])
    mark = len(h.heard)
    await h.send("Psst. It's the player, not the GM.", mira)
    await h.settle()
    streamed = [
        e.delta.text
        for e in h.heard[mark:]
        if isinstance(e, Live) and e.cursor == mira and isinstance(e.delta, TextDelta)
    ]
    assert streamed == ["Wh", "at?"]
    assert spoken(h, mira)[1:] == [
        ("user", "Psst. It's the player, not the GM."),
        ("assistant", "What?"),
    ]
    user = h.session.tree.node(h.session.cursor(mira).at - 1)
    assert user.by == Author.Frontend("term"), "here the user is the human; the GM never knew"
    assert len(spoken(h)) == 1, "the main conversation heard nothing"


@scenario
async def test_aborting_the_gm_mid_scene_aborts_the_characters_it_asked(tmp_path: Path) -> None:
    mira = Gate()
    h, provider = await stage(
        tmp_path,
        {
            "gm": [SCENE],
            "mira": [Reply([Part.Text("Get out.")], gate=mira)],
            "borin": [say("Aye.")],
        },
    )
    await h.send("Borin enters.")
    await h.settle()
    assert mira.held == 1, "Mira is mid-sample"
    assert h.session.cursor(cursor_of(h, "Borin")).busy is False
    await h.abort()
    await h.settle()
    assert [type(o) for o in outcomes(h).values()] == [Outcome.Ok, Outcome.Interrupted]
    assert (await h.view(cursor_of(h, "Mira"))).ending == Ending.Aborted()
    assert not h.session.cursor(cursor_of(h, "Mira")).busy, "her sample was cancelled"
    assert not h.session.cursor(cursor_of(h, "Mira")).auto
    assert mira.held == 0
    mira.release()
    await h.settle()
    assert ("assistant", "Get out.") not in spoken(h, cursor_of(h, "Mira")), "she never answered"
    assert [provider.asked(m) for m in MODELS] == [1, 1, 1, 0], "no more samples ran, no summary"


@scenario
async def test_a_character_that_fails_fails_the_call_and_the_gm_sees_no_lines(
    tmp_path: Path,
) -> None:
    h, _ = await stage(
        tmp_path,
        {"gm": [SCENE, say("Silence.")], "mira": [Fail("overloaded")], "borin": [say("Aye.")]},
    )
    await h.send("Borin enters.")
    await h.settle()
    outcome = outcomes(h)["a"]
    assert isinstance(outcome, Outcome.Err)
    assert "did not answer" in str(outcome)
    assert spoken(h, cursor_of(h, "Borin"))[-1] == ("assistant", "Aye."), "Borin did answer"


@scenario
async def test_naming_someone_who_is_not_in_the_story_asks_no_one(tmp_path: Path) -> None:
    ghost = calls(("a", "ask_characters", '{"who": ["Mira", "Ghost"], "message": "boo"}'))
    h, provider = await stage(tmp_path, {"gm": [ghost, say("Oh.")]})
    await h.send("Something is wrong.")
    await h.settle()
    assert outcomes(h)["a"] == Outcome.Err("ValueError: not in the story: Ghost")
    assert [provider.asked(m) for m in MODELS] == [2, 0, 0, 0]


@scenario
async def test_a_character_cannot_ask_itself_and_nobody_is_asked(tmp_path: Path) -> None:
    mira = calls(("a", "ask_characters", '{"who": ["Borin", "Mira"], "message": "boo"}'))
    h, provider = await stage(tmp_path, {"mira": [mira, say("Oh.")]})
    await h.send("Wake up.", cursor_of(h, "Mira"))
    await h.settle()
    (result,) = [
        o
        for b in h.bodies(cursor_of(h, "Mira"))
        if isinstance(b, Body.Results)
        for _, o, _ in b.outcomes
    ]
    assert result == Outcome.Err("Mira: a character cannot ask itself")
    assert [provider.asked(m) for m in MODELS] == [0, 2, 0, 0], "Borin was never sent anything"
    assert len(spoken(h, cursor_of(h, "Borin"))) == 1, "Borin heard nothing"


@scenario
async def test_a_character_busy_with_something_else_answers_the_gms_message(
    tmp_path: Path,
) -> None:
    """The GM waits out the step in progress, so the ending it reads is its own message's."""
    gate = Gate()
    ask = calls(("a", "ask_characters", '{"who": ["Mira"], "message": "Borin enters."}'))
    h, _ = await stage(
        tmp_path,
        {
            "gm": [ask, say("She nods.")],
            "mira": [Reply([Part.Text("lost")], gate=gate), say("Fine.")],
        },
    )
    mira = cursor_of(h, "Mira")
    await h.send("Psst.", mira)
    await h.settle()
    assert gate.held == 1
    await h.send("Borin enters.")
    await h.settle()
    assert h.session.cursor(h.cursor).busy, "the GM waits for Mira, who is busy with the player"
    await h.abort(mira)  # the player's step ends Aborted: not an answer to the GM
    await h.settle()
    assert outcomes(h)["a"] == Outcome.Ok("Mira: Fine.\n(not asked: Borin, Cass)")
    assert spoken(h)[-1] == ("assistant", "She nods.")
