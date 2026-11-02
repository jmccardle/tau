"""examples/speculative/harness_sense.py: the awareness tools, called by a scripted model.

Each test scripts the model's calls and then reads what the model was told: the `Results` the
tool wrote. The probe and hook tests also show what a frontend is sent meanwhile.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from fakes import FakeProvider, FakeTool, Gate, Reply
from harness import Harness, say, scenario

from tau import (
    Author,
    Body,
    CallArgs,
    CallNamed,
    CursorView,
    Labels,
    Outcome,
    Part,
    Stop,
    TextDelta,
)


def load(name: str) -> Any:
    path = Path(__file__).resolve().parents[3] / "examples" / "speculative" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"speculative_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # inspect finds a class's file through sys.modules
    spec.loader.exec_module(module)
    return module


harness_sense = load("harness_sense")
plan_mode = load("plan_mode")


def use(tool: str, **args: object) -> Reply:
    """The model calls one tool."""
    return Reply([Part.Call("c", tool, json.dumps(args))], Stop.Tools)


async def sensed(
    tmp_path: Path, script: list[Reply], tools: list[Any] | None = None, extras: Any = ()
) -> tuple[Harness, Any]:
    sense = harness_sense.HarnessSense()
    extensions = [sense, *extras]
    h = await Harness.create(
        tmp_path, provider=FakeProvider(script), tools=tools or [], extensions=extensions
    )
    sense.attach(h.driver, extensions, tools or [])
    return h, sense


def told(h: Harness, index: int = -2) -> str:
    """What the tool answered the model: the outcome of the `Results` node at `index`."""
    results = h.bodies()[index]
    assert isinstance(results, Body.Results)
    outcome = results.outcomes[0][1]
    assert isinstance(outcome, Outcome.Ok), outcome
    return outcome.value


def failed(h: Harness, index: int = -2) -> str:
    """What the model was told when the tool refused: the `Err` of the `Results` at `index`."""
    results = h.bodies()[index]
    assert isinstance(results, Body.Results)
    outcome = results.outcomes[0][1]
    assert isinstance(outcome, Outcome.Err), outcome
    return outcome.error


@scenario
async def test_whereami_says_where_the_cursor_is_and_what_the_harness_offers(
    tmp_path: Path,
) -> None:
    h, _ = await sensed(
        tmp_path,
        [use("whereami"), say("ok")],
        tools=[FakeTool("read", "x")],
        extras=[plan_mode],
    )
    await h.client.set_label(h.cursor, "main")
    await h.send("hi")
    await h.settle()
    answer = told(h)
    batch = h.session.cursor(h.cursor).at - 2
    assert f"cursor [0] at node {batch}, depth 3, labels ['main'], model fake" in answer
    assert "extension plan_mode (" in answer
    assert "plan_mode.py): {'tools': [], 'commands': ['plan']}, hooks ['pre_step'" in answer
    assert "extension harness_sense (" in answer
    assert "harness_sense.py)" in answer
    assert "tools offered: read, whereami, tree, context_budget" in answer
    assert "options now: {'mode': 'act'," in answer, "options as they stand"
    await h.close()


@scenario
async def test_tree_shows_the_branch_you_are_on_and_the_one_you_left(tmp_path: Path) -> None:
    h, _ = await sensed(tmp_path, [say("first"), say("second"), use("tree", depth=2), say("ok")])
    await h.send("one")
    await h.settle()
    await h.go_to(2)  # the first User node: branch from here
    await h.send("two")
    await h.settle()
    await h.send("three")
    await h.settle()
    shown = told(h)
    assert shown.splitlines()[1].endswith("User 'one'"), "it starts at the branch point"
    assert "Assistant 'first'" in shown, "the branch the cursor left is shown beside its own"
    assert shown.splitlines()[-1].endswith("calls ['tree']  <- you @0")
    await h.close()


CUT = "[truncated: 400 lines omitted before these; full output (51200 bytes) is in /tmp/t/9.out]"


@scenario
async def test_context_budget_reports_usage_the_biggest_nodes_and_cut_outputs(
    tmp_path: Path,
) -> None:
    usage = json.dumps({"usage": {"input": 1500, "output": 40, "cached": 1200}})
    bash = FakeTool("bash", f"{CUT}\nlast lines of a long build")
    script = [
        use("bash", command="make"),
        Reply([Part.Text("built")], meta=usage),
        use("context_budget"),
        say("ok"),
    ]
    h, _ = await sensed(tmp_path, script, tools=[bash])
    await h.configure("harness_sense", {"context_window": 3000})
    await h.send("build it")
    await h.settle()
    await h.send("how much room is left?")
    await h.settle()
    answer = told(h)
    assert "reported 1500 prompt tokens (1200 cached) at node " in answer
    assert "window 3000: " in answer
    assert f"cut: node 5 call c: {CUT}" in answer, "where the whole output is"
    assert "biggest: " in answer
    await h.close()


@scenario
async def test_context_budget_does_not_invent_a_window(tmp_path: Path) -> None:
    h, _ = await sensed(tmp_path, [use("context_budget"), say("ok")])
    await h.send("room?")
    await h.settle()
    assert "window unknown: /config harness_sense context_window=N" in told(h)
    await h.close()


class Flaky:
    """Fails its first `pre_step`."""

    name = "flaky"

    def __init__(self) -> None:
        self.broken = False

    async def pre_step(self, cursor: Any, subject: Any) -> str | None:
        if not self.broken:
            self.broken = True
            raise RuntimeError("boom")
        return None


@scenario
async def test_hook_errors_remembers_what_the_next_step_erases_from_the_view(
    tmp_path: Path,
) -> None:
    h, _ = await sensed(tmp_path, [use("hook_errors"), say("ok")], extras=[Flaky()])
    await h.send("hi")
    await h.settle()
    failed = {e.hook_error for e in h.heard if isinstance(e, CursorView) and e.hook_error}
    assert [(f.extension, f.point, f.error) for f in failed] == [
        ("flaky", "pre_step", "RuntimeError: boom")
    ]
    assert not (await h.view()).auto, "the failed step turned auto-step off"
    await h.step()  # the person tries again; the model can now ask what happened
    await h.settle()
    assert (await h.view()).hook_error is None, "the view forgot when the new step began"
    # one asked step: the reply, then its tools; with auto-step off the model is not sampled again
    assert told(h, -1) == "node 2: failed: RuntimeError: boom (pre_step of flaky)"
    await h.close()


@scenario
async def test_hook_errors_lists_the_calls_an_extension_refused(tmp_path: Path) -> None:
    script = [use("write", path="x"), use("hook_errors"), say("ok")]
    tools = [FakeTool(n, "ran") for n in ("read", "write", "grep", "bash")]
    h, _ = await sensed(tmp_path, script, tools=tools, extras=[plan_mode])
    await h.run("plan")
    await h.settle()  # `run` came back before its step ended: /config now would be Busy
    # "read-only" is not a property of a tool: plan mode knows `read` and `grep` by name, so
    # every observation tool of every other extension has to be named to it
    await h.configure("plan_mode", {"allow_tools": ["read", "grep", "hook_errors"]})
    await h.send("go")
    await h.settle()
    assert told(h).endswith(
        "call c: plan mode: write is refused. Put the change in your plan instead. (by plan_mode)"
    )
    await h.close()


@scenario
async def test_a_note_is_a_side_node_that_whereami_shows_on_that_branch_only(
    tmp_path: Path,
) -> None:
    script = [use("note", text="fixtures live in conftest"), say("ok"), use("whereami"), say("ok")]
    script += [use("whereami"), say("ok")]
    h, _ = await sensed(tmp_path, script)
    await h.send("one")
    await h.settle()
    assert told(h).startswith("noted as node ")
    await h.send("two")
    await h.settle()
    assert "note: fixtures live in conftest" in told(h), "a later turn, same branch"
    await h.go_to(2)
    await h.send("three")
    await h.settle()
    assert "note:" not in told(h), "another branch from before the note does not have it"
    await h.close()


@scenario
async def test_probe_gets_the_models_answer_on_a_scratch_branch(tmp_path: Path) -> None:
    script = [use("probe", prompt="say hi"), say("hi there"), say("it said hi")]
    h, _ = await sensed(tmp_path, script)
    await h.send("go")
    await h.settle()
    assert told(h) == "probe 1 replied: hi there"
    # the probe branched from just before the reply that called it, as a different author
    sent = [n for n in h.nodes() if n.body == Body.User("say hi")]
    assert [n.by for n in sent] == [Author.Frontend("harness_sense:probe")]
    assert sent[0].parent == 2, "the User node of 'go'"
    assert h.bodies()[-1] == Body.Assistant([Part.Text("it said hi")], Stop.End)
    assert [b.text for b in h.bodies() if isinstance(b, Body.User)] == ["go"], (
        "your branch is clean"
    )
    # what a frontend was sent meanwhile: a labelled cursor 1 that was busy
    assert any(isinstance(e, CursorView) and e.cursor == 1 and e.busy for e in h.heard)
    labels = [e for e in h.heard if isinstance(e, Labels)][-1]
    assert ("probe-1", 1) in labels.labels
    await h.close()


@scenario
async def test_a_probe_reports_the_calls_it_would_make_and_runs_none(tmp_path: Path) -> None:
    write = FakeTool("write", "wrote")
    attempt = Reply([Part.Call("p", "write", '{"path": "x"}')], Stop.Tools)
    script = [use("probe", prompt="change x"), attempt, say("ok")]
    h, _ = await sensed(tmp_path, script, tools=[write])
    await h.send("go")
    await h.settle()
    assert told(h) == 'probe 1 replied: \nit would call: write({"path": "x"})'
    assert write.calls == []
    (refusal,) = [
        n.body for n in h.nodes() if n.by == Author.Tools() and n.id != h.session.cursor(0).at - 1
    ]
    assert isinstance(refusal, Body.Results)
    assert refusal.outcomes[0][1] == Outcome.Err("probe: tools do not run on a probe branch")
    await h.close()


@scenario
async def test_a_probe_continues_only_a_probe(tmp_path: Path) -> None:
    script = [
        use("probe", prompt="one"),
        say("first"),
        use("probe", prompt="two", cursor=1),
        say("second"),
        use("probe", prompt="three", cursor=0),
        say("ok"),
    ]
    h, _ = await sensed(tmp_path, script)
    await h.send("go")
    await h.settle()
    assert [b.text for b in h.bodies(1) if isinstance(b, Body.User)] == ["go", "one", "two"]
    assert failed(h, -2) == "cursor 0 is not a probe of yours; leave `cursor` out"
    await h.close()


@scenario
async def test_a_probe_that_gets_no_answer_is_aborted_after_its_timeout(tmp_path: Path) -> None:
    held = Gate()
    script = [use("probe", prompt="slow"), Reply([Part.Text("late")], gate=held), say("ok")]
    h, _ = await sensed(tmp_path, script)
    await h.configure("harness_sense", {"probe_timeout": 0.05})
    await h.send("go")
    await h.settle()
    await asyncio.sleep(0.15)  # real time: the tool's timeout is the loop's, not the clock's
    await h.settle()
    assert failed(h) == "probe 1: no reply in time; aborted"
    assert not (await h.view(1)).busy
    await h.close()


def extension_file(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "edited.py"
    path.write_text(source)
    return path


@scenario
async def test_check_extension_says_what_loads_and_what_is_wrong_with_an_edit(
    tmp_path: Path,
) -> None:
    good = extension_file(
        tmp_path,
        'def shout(text: str) -> str:\n    """Shout."""\n    return text.upper()\n\n'
        'name = "shouter"\ntools = [shout]\n',
    )
    no_docstring = tmp_path / "nodoc.py"
    no_docstring.write_text(
        'def shout(text: str) -> str:\n    return text\n\nname = "n"\ntools = [shout]\n'
    )
    no_name = tmp_path / "noname.py"
    no_name.write_text("tools = []\n")
    broken = tmp_path / "broken.py"
    broken.write_text("def shout(:\n")
    script = [use("check_extension", path=str(p)) for p in (good, no_docstring, no_name, broken)]
    h, _ = await sensed(
        tmp_path / "s", [*(r for s in zip(script, [say("")] * 4, strict=True) for r in s)]
    )
    for _ in range(4):
        await h.send("check")
        await h.settle()
    outcomes = [b.outcomes[0][1] for b in h.bodies() if isinstance(b, Body.Results)]
    assert isinstance(outcomes[0], Outcome.Ok)
    assert outcomes[0].value.startswith("ok, not loaded: extension shouter (")
    assert "'tools': ['shout']" in outcomes[0].value
    errors = [o.error for o in outcomes[1:] if isinstance(o, Outcome.Err)]
    assert "no docstring to describe it" in errors[0]
    assert "`name` must be a non-empty str" in errors[1]
    assert errors[2].startswith("SyntaxError")
    await h.close()


@scenario
async def test_an_unattached_sense_says_so_instead_of_guessing(tmp_path: Path) -> None:
    sense = harness_sense.HarnessSense()
    h = await Harness.create(
        tmp_path, provider=FakeProvider([use("whereami"), say("ok")]), extensions=[sense]
    )
    await h.send("go")
    await h.settle()
    assert "did not call attach(driver, extensions, tools)" in failed(h)
    await h.close()


class Nosy:
    """Asks anyone a question before every step, as a permissions or confirmation hook would."""

    name = "nosy"

    async def pre_step(self, cursor: Any, subject: Any) -> str | None:
        await cursor.ask("go on?", bool)
        return None


@scenario
async def test_a_headless_question_waits_and_a_probes_own_questions_wait_too(
    tmp_path: Path,
) -> None:
    script = [use("probe", prompt="hello"), say("hi"), say("ok")]
    h, _ = await sensed(tmp_path, script, extras=[Nosy()])
    await h.send_unattended("go")  # headless: no frontend is attached
    # `settle` cannot be used: the probe tool is a unit that waits on another cursor, which the
    # Harness does not count as parked
    while h.driver.state().open_questions == 0:
        await asyncio.sleep(0)
    assert [q.text for q in (await h.view(0)).questions] == ["go on?"], "it waits for someone"
    # Whoever comes along answers: the cursor's own steps (two), and the probe cursor's wake, the
    # wake its input causes, and its step (three).
    late = await h.connect("late")  # `client` is closed: someone connects
    answered = 0
    while h.session.cursor(h.cursor).busy:
        for cursor in h.session.cursors():
            for question in (await h.view(cursor)).questions[:1]:
                await late.answer(question.id, True)
                answered += 1
        await asyncio.sleep(0)
    assert answered == 5
    assert told(h) == "probe 1 replied: hi"
    await h.close()


def load_chat() -> Any:
    examples = Path(__file__).resolve().parents[3] / "examples"
    sys.path.insert(0, str(examples))
    try:
        spec = importlib.util.spec_from_file_location("speculative_chat", examples / "chat.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(examples))


@scenario
async def test_a_line_client_shows_the_call_but_neither_its_answer_nor_the_probe(
    tmp_path: Path,
) -> None:
    chat = load_chat()
    streamed = Reply([TextDelta("it said hi"), Part.Text("it said hi")])
    args = '{"prompt": "say hi"}'
    call = Reply([CallNamed(0, "c", "probe"), CallArgs(0, args), Part.Call("c", "probe", args)])
    script = [Reply(call.parts, Stop.Tools), say("hi there"), streamed]
    h, _ = await sensed(tmp_path, script)
    client = await h.connect("chat:test", watching=[h.cursor])
    screen: list[str] = []

    async def typed(prompt: str) -> str:
        raise AssertionError(prompt)

    window = chat.Chat(client, h.cursor, screen.append, typed)
    listener = asyncio.ensure_future(window.listen())
    await window.line("go")
    await h.settle()
    for _ in range(1000):  # the listener is a task of its own
        await asyncio.sleep(0)
    text = "".join(screen)
    assert "[calling probe]" in text
    assert "it said hi" in text
    assert "probe 1 replied" not in text, "tool results are not drawn"
    assert "probe-1" not in text, "nor labels, nor other cursors"
    listener.cancel()
    await client.close()
    await h.close()
