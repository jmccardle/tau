"""examples/speculative/router.py: four fake models, chosen by the text of the input."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Windows: the example imports readline, which Python on Windows does not have",
        allow_module_level=True,
    )

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider, FakeTool, Gate, Reply
from harness import Harness, say, scenario

from tau import (
    Appended,
    Body,
    Client,
    ConfigError,
    Event,
    Notice,
    Part,
    Stop,
    model_config,
    read_models,
)

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
spec = importlib.util.spec_from_file_location(
    "speculative_router", EXAMPLES / "speculative/router.py"
)
assert spec is not None
assert spec.loader is not None
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

sys.path.insert(0, str(EXAMPLES))  # chat.py imports `common`
spec = importlib.util.spec_from_file_location("speculative_chat", EXAMPLES / "chat.py")
assert spec is not None
assert spec.loader is not None
chat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chat)
sys.path.remove(str(EXAMPLES))

IDS = {"main": "main-1", "cheap": "tiny-1", "think": "reason-1", "coder": "code-1"}
TABLE = {
    "when": ["```", r"(?i)\b(prove|derive)\b", "^.{0,24}$", "(?s)."],
    "use": ["coder", "think", "cheap", "main"],
}


def catalog(**options: object) -> Any:
    models = {name: {"model": id_, "provider": "fake"} for name, id_ in IDS.items()}
    return read_models(cli={"models": models, "extensions": {"router": {**TABLE, **options}}})


async def start(tmp_path: Path, replies: int, **options: object) -> tuple[Harness, FakeProvider]:
    provider = FakeProvider([say(f"reply {i}") for i in range(replies)])
    h = await Harness.create(
        tmp_path,
        config=model_config("main"),
        provider=provider,
        extensions=[router.Router()],
        catalog=catalog(**options),
    )
    return h, provider


async def say_to(h: Harness, text: str) -> None:
    await h.send(text)
    await h.settle()


def used(provider: FakeProvider) -> list[str]:
    """The provider's model id of each sample so far, by the name the catalog gave it."""
    names = {v: k for k, v in IDS.items()}
    return [names[r.model.model] for r in provider.requests]


def story(h: Harness) -> list[str]:
    """The walk as one short line per node."""
    out: list[str] = []
    for node in h.session.tree.walk(h.session.cursor(h.cursor).at):
        match node.body:
            case Body.Config(config=config):
                out.append(f"{node.by.__class__.__name__} config {config.data}")
            case Body.Custom(kind=kind, data=data):
                out.append(f"custom {kind} {data}")
            case Body.User(text=text):
                out.append(f"user {text!r}")
            case Body.Assistant():
                out.append("assistant")
            case _:
                out.append(type(node.body).__name__)
    return out


async def listen(client: Client, heard: list[Event]) -> None:
    async for event in client:
        heard.append(event)


@scenario
async def test_each_input_is_routed_by_its_text_and_a_node_is_written_only_on_a_change(
    tmp_path: Path,
) -> None:
    h, provider = await start(tmp_path, 6)
    for text in [
        "hi",
        "hello again",  # still cheap: nothing is written
        "prove that the square root of 2 is irrational",
        "```py\nprint(1)\n```",
        "now explain what that code does, in detail please",
        "ok",
    ]:
        await say_to(h, text)
    assert used(provider) == ["cheap", "cheap", "think", "coder", "main", "cheap"]
    assert story(h) == [
        'Core config {"model": "main"}',
        "System",
        'Extension config {"model": "cheap"}',
        'custom route {"model": "cheap", "why": "/^.{0,24}$/"}',
        "user 'hi'",
        "assistant",
        "user 'hello again'",
        "assistant",
        'Extension config {"model": "think"}',
        'custom route {"model": "think", "why": "/(?i)\\\\b(prove|derive)\\\\b/"}',
        "user 'prove that the square root of 2 is irrational'",
        "assistant",
        'Extension config {"model": "coder"}',
        'custom route {"model": "coder", "why": "/```/"}',
        "user '```py\\nprint(1)\\n```'",
        "assistant",
        'Extension config {"model": "main"}',
        'custom route {"model": "main", "why": "/(?s)./"}',
        "user 'now explain what that code does, in detail please'",
        "assistant",
        'Extension config {"model": "cheap"}',
        'custom route {"model": "cheap", "why": "/^.{0,24}$/"}',
        "user 'ok'",
        "assistant",
    ]
    await h.close()


@scenario
async def test_every_client_is_told_and_sees_the_nodes(tmp_path: Path) -> None:
    h, _ = await start(tmp_path, 1)
    watcher = await h.connect("watcher@phone:web", watching=[h.cursor])
    heard: list[Event] = []
    listener = asyncio.ensure_future(listen(watcher, heard))
    await say_to(h, "prove it")
    await asyncio.sleep(0)
    assert h.notices() == ["router: think (/(?i)\\b(prove|derive)\\b/)"]
    assert [e.text for e in heard if isinstance(e, Notice)] == h.notices(), "the watcher too"
    nodes = [n.body for e in heard if isinstance(e, Appended) for n in e.nodes]
    assert [type(b) for b in nodes[:4]] == [Body.Config, Body.Custom, Body.User, Body.Assistant]
    listener.cancel()
    await h.close()


# Overrides


@scenario
async def test_a_prefix_forces_a_model_and_is_not_sent(tmp_path: Path) -> None:
    h, provider = await start(tmp_path, 4)
    await say_to(h, "!think what is 2+2?")
    await say_to(h, "!think and 3+3?")  # already there: stripped, nothing written
    await say_to(h, "!oops what is this")  # not a model: text, routed by the table
    await say_to(h, "!main hello there")  # a prefix is a model name, not a rule
    assert used(provider) == ["think", "think", "cheap", "main"]
    users = [line for line in story(h) if line.startswith("user")]
    assert users == [
        "user 'what is 2+2?'",
        "user 'and 3+3?'",
        "user '!oops what is this'",
        "user 'hello there'",
    ]
    assert 'custom route {"model": "think", "why": "prefix"}' in story(h)
    await h.close()


@scenario
async def test_route_off_stops_the_table_but_not_the_prefix(tmp_path: Path) -> None:
    h, provider = await start(tmp_path, 4)
    assert await h.run("route", {"mode": "off"}) == "routing off"
    await say_to(h, "prove it")
    await say_to(h, "!coder fix this")
    await say_to(h, "hi")
    assert await h.run("route", {"mode": "on"}) == "routing on"
    await say_to(h, "prove it again")
    assert used(provider) == ["main", "coder", "coder", "think"]
    await h.configure("router", {"enabled": False})  # what /config router enabled=false does
    options = await h.client.options(h.cursor, "router")
    assert options.schema["properties"]["enabled"]["default"] is False  # type: ignore[index]
    await h.close()


@scenario
async def test_two_inputs_in_one_step_are_routed_each_but_sampled_once(tmp_path: Path) -> None:
    h, provider = await start(tmp_path, 1)
    await h.input("prove it")
    await h.input("hi")
    await h.settle()  # each input woke the cursor, which rested again: auto-step is off
    await h.step()
    await h.settle()
    assert used(provider) == ["cheap"], "the last input's route, for both"
    assert 'custom route {"model": "think", "why": "/(?i)\\\\b(prove|derive)\\\\b/"}' in story(h)
    await h.close()


@scenario
async def test_a_later_gate_that_refuses_the_input_leaves_the_routers_nodes_behind(
    tmp_path: Path,
) -> None:
    class Refuses:
        name = "refuses"

        async def pre_input(self, cursor: Any, subject: Any) -> str | None:
            return "not today"

    h = await Harness.create(
        tmp_path,
        config=model_config("main"),
        provider=FakeProvider([]),
        extensions=[router.Router(), Refuses()],
        catalog=catalog(),
    )
    await say_to(h, "prove it")
    assert "not today" in h.notices()
    assert [line.split()[0] for line in story(h)] == ["Core", "System", "Extension", "custom"]
    assert not [n for n in h.nodes() if isinstance(n.body, Body.User)], "no input was delivered"
    await h.close()


# One turn, or until told otherwise


@scenario
async def test_once_puts_the_branch_back_after_the_turns_last_reply(tmp_path: Path) -> None:
    provider = FakeProvider(
        [Reply([Part.Call("c", "t", "{}")], Stop.Tools), say("done"), say("ok")]
    )
    h = await Harness.create(
        tmp_path,
        config=model_config("main"),
        provider=provider,
        tools=[FakeTool("t", "result")],
        extensions=[router.Router()],
        catalog=catalog(once=True),
    )
    await say_to(h, "prove it")
    assert used(provider) == ["think", "think"], "a tool batch is the same turn"
    assert story(h)[-3:] == ["Results", "assistant", 'Extension config {"model": "main"}']
    await say_to(h, "tell me a long story about the sea")
    assert used(provider) == ["think", "think", "main"]
    assert len([line for line in story(h) if "config" in line]) == 3, "root, go, back"
    await h.close()


@scenario
async def test_an_abort_in_the_tool_phase_leaves_the_branch_on_the_routed_model(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([Reply([Part.Call("c", "t", "{}")], Stop.Tools)])
    h = await Harness.create(
        tmp_path,
        config=model_config("main"),
        provider=provider,
        tools=[FakeTool("t", "result", gate=Gate())],
        extensions=[router.Router()],
        catalog=catalog(once=True),
    )
    await h.send("prove it")
    await h.settle()
    await h.abort()
    await h.settle()
    assert story(h)[-3:] == ["user 'prove it'", "assistant", "Results"], "no reply, so no restore"
    assert [line for line in story(h) if "config" in line][
        -1
    ] == 'Extension config {"model": "think"}'
    await h.close()


@scenario
async def test_routing_survives_a_reload_because_it_is_in_the_tree(tmp_path: Path) -> None:
    h, _ = await start(tmp_path, 1)
    await say_to(h, "hi")
    await h.close()
    provider = FakeProvider([say("again")])
    h = await Harness.load(
        tmp_path, provider=provider, extensions=[router.Router()], catalog=catalog()
    )
    before = len(h.nodes())
    await say_to(h, "hi")
    assert used(provider) == ["cheap"]
    assert len(h.nodes()) == before + 2, "already on cheap: only the user and assistant nodes"
    await h.close()


# The options


@scenario
async def test_the_table_is_checked_when_it_is_configured(tmp_path: Path) -> None:
    h, _ = await start(tmp_path, 0)
    with pytest.raises(ConfigError, match="same length"):
        await h.configure("router", {"when": ["a"], "use": []})
    with pytest.raises(ConfigError, match=r"pattern '\(': "):
        await h.configure("router", {"when": ["("], "use": ["main"]})
    with pytest.raises(ConfigError, match="configured model, not 'gpt-9'"):
        await h.configure("router", {"when": ["a"], "use": ["gpt-9"]})
    await h.configure("router", {"when": ["x"], "use": ["coder"]})
    await h.close()


# A chat line client over the same extension


@scenario
async def test_a_chat_line_client_shows_the_notice_and_takes_the_command(tmp_path: Path) -> None:
    h, provider = await start(tmp_path, 2)
    client = await h.connect("chat:line", watching=[h.cursor])
    screen: list[str] = []

    async def never(prompt: str) -> str:
        raise AssertionError(f"asked for {prompt}")

    window = chat.Chat(client, h.cursor, screen.append, never)
    await window.refresh()
    listener = asyncio.ensure_future(window.listen())
    await window.line("/")
    assert "/route <mode>  Turn the router's table on or off on this branch.\n" in "".join(screen)
    await window.line("/route off")
    await window.finished()
    await h.settle()
    await window.line("prove it")  # off: no notice
    await h.settle()
    await window.line("!think prove it")
    await h.settle()
    assert "[routing off]" in "".join(screen)
    assert "[router: think (prefix)]" in "".join(screen)
    assert used(provider) == ["main", "think"]
    listener.cancel()
    await client.close()
    await h.close()
