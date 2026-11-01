"""examples/speculative/bash_guard.py, driven through the Harness and a chat line client."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fakes import FAKE_MODEL, FakeProvider, FakeTool, Reply
from harness import Harness, say, scenario

from tau import (
    BashTool,
    Body,
    ConfigError,
    CursorView,
    Ending,
    Outcome,
    Part,
    Stop,
    UserConfig,
    read_models,
)
from tau.commandline import parse

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


def load(path: Path, name: str) -> ModuleType:
    sys.path.insert(0, str(EXAMPLES))  # chat.py imports `common`
    try:
        spec = importlib.util.spec_from_file_location(f"speculative_{name}", path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(EXAMPLES))


guard = load(EXAMPLES / "speculative" / "bash_guard.py", "bash_guard")
chat = load(EXAMPLES / "chat.py", "chat")


def layers(**options: object) -> dict[str, object]:
    """The `cli` layer: one fake model and bash_guard's options."""
    models = {FAKE_MODEL: {"model": FAKE_MODEL, "provider": "fake"}}
    return {"models": models, "extensions": {"bash_guard": options}}


def configured(**options: object) -> object:
    return read_models(cli=layers(**options))


def run(*commands: str) -> Reply:
    """The model asks to run each command, in one batch: call ids a, b, c."""
    calls = [
        Part.Call("abc"[i], "bash", json.dumps({"command": c})) for i, c in enumerate(commands)
    ]
    return Reply(calls, Stop.Tools)


async def start(
    tmp_path: Path, script: list[Reply], *, tool: object = None, **options: object
) -> tuple[Harness, FakeTool]:
    fake = FakeTool("bash", "output")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        tools=[tool or fake],  # pyright: ignore[reportArgumentType]
        extensions=[guard.BashGuard()],
        catalog=configured(**options),  # pyright: ignore[reportArgumentType]
        user_config=UserConfig(tmp_path / "tau-home", cli=layers(**options)),
    )
    await h.send("go")
    await h.settle()
    return h, fake


def results(h: Harness) -> list[tuple[str, Outcome]]:
    body = next(b for b in reversed(h.bodies()) if isinstance(b, Body.Results))
    return [(call, outcome) for call, outcome, _ in body.outcomes]


@scenario
async def test_a_command_that_prints_little_is_not_asked_about(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, [run("ls", "git log -n 5", "cat x | head"), say("ok")])
    assert tool.calls == [
        {"command": "ls"},
        {"command": "git log -n 5"},
        {"command": "cat x | head"},
    ]
    await h.close()


@scenario
async def test_the_frontend_sees_a_busy_cursor_and_a_typed_question(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, [run("find / -name x"), say("ok")])
    view = await h.view()
    assert view.busy
    (question,) = view.questions
    assert question.text.endswith("\n  find / -name x"), "the command is only in the text"
    assert question.schema == {
        "type": "string",
        "enum": ["reject", "allow", "always", "truncate"],
    }
    assert tool.calls == [], "the tool waits for the answer"
    await h.respond("allow")
    await h.settle()
    assert tool.calls == [{"command": "find / -name x"}]
    shown = [e for e in h.heard if isinstance(e, CursorView) and e.questions]
    assert shown, "the client was told of the question as cursor state, with no event of its own"
    await h.close()


@scenario
async def test_truncate_rewrites_the_call_and_tells_the_model_what_ran(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, [run("find / -name x"), say("ok")])
    await h.respond("truncate")
    await h.settle()
    (grep,) = (await h.view()).questions
    assert grep.schema == {"type": "string"}
    await h.respond("needle")
    await h.settle()
    ran = "{ find / -name x\n} 2>&1 | grep -e needle | tail -n 200"
    assert tool.calls == [{"command": ran}]
    ((_, outcome),) = results(h)
    assert outcome == Outcome.Ok(f"[bash_guard ran: {ran}]\noutput")
    assistant = h.bodies()[-3]
    assert isinstance(assistant, Body.Assistant)
    call = assistant.parts[0]
    assert isinstance(call, Part.Call)
    assert json.loads(call.args_raw) == {"command": "find / -name x"}, "the tree keeps the original"
    await h.close()


@scenario
async def test_declining_the_grep_pattern_is_just_the_tail(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, [run("journalctl -u sshd"), say("ok")])
    await h.respond("truncate")
    await h.settle()
    await h.respond(None)
    await h.settle()
    assert tool.calls == [{"command": "{ journalctl -u sshd\n} 2>&1 | tail -n 200"}]
    await h.close()


@scenario
async def test_reject_is_a_result_that_says_how_to_narrow_it(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, [run("npm install"), say("ok")])
    await h.respond("reject")
    await h.settle()
    assert tool.calls == []
    ((_, outcome),) = results(h)
    assert isinstance(outcome, Outcome.Err)
    assert "the user said no" in outcome.error
    assert "Narrow it" in outcome.error
    await h.close()


@scenario
async def test_a_real_bash_runs_the_truncated_command(tmp_path: Path) -> None:
    h, _ = await start(
        tmp_path,
        [run("seq 1 1000"), say("ok")],
        tool=BashTool(tmp_path),
        patterns=[r"\bseq\b"],
    )
    await h.respond("truncate")
    await h.settle()
    await h.respond(None)
    await h.settle()
    ((_, outcome),) = results(h)
    assert isinstance(outcome, Outcome.Ok)
    lines = outcome.value.splitlines()
    assert lines[0].startswith("[bash_guard ran: {")
    assert lines[-1] == "1000"
    assert "801" in lines
    assert "800" not in lines
    await h.close()


# A batch of three


@scenario
async def test_a_batch_asks_all_its_questions_at_once_and_clients_race_to_answer(
    tmp_path: Path,
) -> None:
    h, tool = await start(
        tmp_path, [run("find /", "npm install", "cargo build"), say("ok")], wait=60.0
    )
    alice = await h.connect("alice@laptop:tui")
    bob = await h.connect("bob@phone:web")
    first, second, third = (await h.view()).questions
    assert [q.text.splitlines()[1].strip() for q in (first, second, third)] == [
        "find /",
        "npm install",
        "cargo build",
    ]
    await bob.answer(first.id, "allow")
    with pytest.raises(LookupError):
        await alice.answer(first.id, "reject")  # the first good answer won
    await alice.answer(second.id, "reject")
    await h.settle()
    assert [q.id for q in (await h.view()).questions] == [third.id], "the cursor still waits"
    assert tool.calls == [{"command": "find /"}]
    await bob.answer(third.id, "allow")
    await h.settle()
    assert [call for call, _ in results(h)] == ["a", "b", "c"], "results are in call order"
    assert tool.calls == [{"command": "find /"}, {"command": "cargo build"}]
    await h.close()


@scenario
async def test_an_abort_declines_every_open_question(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, [run("find /", "npm install")], wait=60.0)
    assert len((await h.view()).questions) == 2
    await h.abort()
    await h.settle()
    view = await h.view()
    assert view.questions == ()
    assert view.ending == Ending.Aborted()
    assert tool.calls == []
    assert [type(o) for _, o in results(h)] == [Outcome.Err, Outcome.Err]
    await h.close()


# Nobody to ask: the question waits, and `wait` seconds pass


@scenario
async def test_with_no_client_connected_the_question_waits_then_unattended_decides(
    tmp_path: Path,
) -> None:
    fake = FakeTool("bash", "output")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([run("find /"), say("ok")]),
        tools=[fake],
        extensions=[guard.BashGuard()],
        catalog=configured(wait=0.1),  # pyright: ignore[reportArgumentType]
    )
    await h.client.close()  # overnight: auto-step, nobody attached
    await h.send("go")
    await h.settle()
    assert h.session.cursor(h.cursor).busy, "asked, with no one to answer: it waits"
    await asyncio.sleep(0.3)  # real time: the extension's timeout is the loop's, not the clock's
    await h.settle()
    assert fake.calls == []
    ((_, outcome),) = results(h)
    assert isinstance(outcome, Outcome.Err)
    assert "nobody was there to approve it" in outcome.error
    await h.close()


@scenario
async def test_unattended_truncate_runs_the_tail_when_nobody_answers(tmp_path: Path) -> None:
    fake = FakeTool("bash", "output")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([run("find /"), say("ok")]),
        tools=[fake],
        extensions=[guard.BashGuard()],
        catalog=configured(unattended="truncate", wait=0.1),  # pyright: ignore[reportArgumentType]
    )
    await h.client.close()
    await h.send("go")
    await h.settle()
    await asyncio.sleep(0.3)
    await h.settle()
    assert fake.calls == [{"command": "{ find /\n} 2>&1 | tail -n 200"}]
    await h.close()


@scenario
async def test_a_client_that_never_answers_is_given_up_on(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, [run("find /"), say("ok")], wait=0.05)
    assert h.session.cursor(h.cursor).busy
    await asyncio.sleep(0.2)  # real time: the extension's timeout is the loop's, not the clock's
    await h.settle()
    assert tool.calls == []
    ((_, outcome),) = results(h)
    assert isinstance(outcome, Outcome.Err)
    assert "nobody was there" in outcome.error
    await h.close()


# Always


@scenario
async def test_always_is_saved_to_the_users_file_for_every_session(tmp_path: Path) -> None:
    script = [run("find /"), run("find /home"), say("ok")]
    h, tool = await start(tmp_path, script)
    await h.respond("always")
    await h.settle()
    saved = tmp_path / "tau-home" / "config.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == {
        "extensions": {"bash_guard": {"allow": [r"\bfind\s+/(?:\s|$)"]}}
    }
    configs = [b.config.data for b in h.bodies() if isinstance(b, Body.Config)]
    assert not any("bash_guard" in data for data in configs), "no node: the file is global"
    options = await h.client.options(h.cursor, "bash_guard")
    allowed = options.schema["properties"]["allow"]["default"]  # type: ignore[index]
    assert allowed == [r"\bfind\s+/(?:\s|$)"]
    await h.step()  # the model asks again: no question
    await h.settle()
    assert tool.calls == [{"command": "find /"}, {"command": "find /home"}]
    await h.close()

    again = FakeTool("bash", "output")
    h = await Harness.load(  # a new instance and a new driver: only the file remembers
        tmp_path,
        provider=FakeProvider([run("find / -name y"), say("ok")]),
        tools=[again],
        extensions=[guard.BashGuard()],
        catalog=read_models(saved, cli=layers()),
    )
    await h.send("and now?")
    await h.settle()
    assert again.calls == [{"command": "find / -name y"}]
    await h.close()


@scenario
async def test_always_adds_to_the_files_own_allow_not_to_the_layers_and_nodes(
    tmp_path: Path,
) -> None:
    """The file holds `old`. The overrides allow `cli` and this branch allows `node`: neither
    belongs in the file."""
    home = tmp_path / "tau-home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"extensions": {"bash_guard": {"allow": ["old"], "wait": 60}}}),
        encoding="utf-8",
    )
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([run("find /"), say("ok")]),
        tools=[FakeTool("bash", "output")],
        extensions=[guard.BashGuard()],
        catalog=configured(allow=["cli"]),  # pyright: ignore[reportArgumentType]
        user_config=UserConfig(home, cli=layers(allow=["cli"])),
    )
    await h.configure("bash_guard", {"allow": ["node"]})
    await h.settle()
    await h.send("go")
    await h.settle()
    await h.respond("always")
    await h.settle()
    saved = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert saved == {
        "extensions": {"bash_guard": {"allow": ["old", r"\bfind\s+/(?:\s|$)"], "wait": 60}}
    }
    await h.close()


@scenario
async def test_always_in_a_batch_is_not_applied_to_the_questions_already_open(
    tmp_path: Path,
) -> None:
    h, tool = await start(tmp_path, [run("find / -a", "find / -b"), say("ok")], wait=60.0)
    first, second = (await h.view()).questions
    await h.client.answer(first.id, "always")
    await h.settle()
    assert [q.id for q in (await h.view()).questions] == [second.id], "asked again for one pattern"
    await h.client.answer(second.id, "always")
    await h.settle()
    assert len(tool.calls) == 2
    await h.close()


# The options form


@scenario
async def test_the_built_in_patterns_are_the_default_and_show_in_the_form(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path, extensions=[guard.BashGuard()])
    form = await h.client.options(h.cursor, "bash_guard")
    properties: Any = form.schema["properties"]
    assert properties["patterns"]["default"] == list(guard.BUILT_IN), "the person sees them"
    assert properties["allow"]["default"] == []
    assert properties["unattended"]["enum"] == ["reject", "allow", "truncate"]
    with pytest.raises(ConfigError, match=r"pattern '\(': "):
        await h.configure("bash_guard", {"patterns": ["("]})
    await h.configure("bash_guard", {"patterns": [r"\bmake\b"]})
    await h.settle()
    await h.close()


@scenario
async def test_a_regex_with_a_comma_is_typed_to_config_as_a_json_array(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[guard.BashGuard()])
    commands = await h.client.commands()
    form = await h.client.options(h.cursor, "bash_guard")
    line = r"""/config bash_guard patterns='["a{1,2}", "b"]'"""
    parsed = parse(line, commands, {"bash_guard": form})
    assert parsed.args == {"patterns": ["a{1,2}", "b"]}
    await h.client.configure(h.cursor, "bash_guard", parsed.args)
    await h.settle()
    now: Any = (await h.client.options(h.cursor, "bash_guard")).schema["properties"]
    assert now["patterns"]["default"] == ["a{1,2}", "b"]
    await h.close()


# A chat line client over the same extension


@scenario
async def test_a_chat_line_client_asks_inline(tmp_path: Path) -> None:
    h, tool = await start(tmp_path, [run("find /", "npm install"), say("ok")], wait=60.0)
    client = await h.connect("chat:line", watching=[h.cursor])
    screen: list[str] = []

    async def never(prompt: str) -> str:
        raise AssertionError(f"asked for {prompt}")

    window = chat.Chat(client, h.cursor, screen.append, never)
    await window.refresh()
    listener = asyncio.ensure_future(window.listen())

    async def reply(to: str, line: str) -> None:
        for _ in range(10_000):
            if window.question is not None and to in window.question.text:
                return await window.line(line)
            await asyncio.sleep(0)
        raise AssertionError(f"never asked: {to}")

    await reply("find /", "truncate")
    await reply("npm install", "reject")  # asked before the grep question: oldest first
    await reply("grep -e what?", "")  # an empty line declines: just the tail
    await h.settle()
    text = "".join(screen)
    assert "[reject/allow/always/truncate]" in text
    assert text.count("? bash would print a lot") == 2
    assert tool.calls == [{"command": "{ find /\n} 2>&1 | tail -n 200"}]
    listener.cancel()
    await client.close()
    await h.close()
