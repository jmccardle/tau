"""The `mcp` built-in (D24 section 12): its servers' tools offered to the model, called, listed
again, and what happens when a server fails to start or exits; over stdio (tests/mcp_server.py)
and streamable HTTP (tests/mcp_http.py)."""

import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider, Reply, fake_catalog
from harness import Harness, scenario
from mcp_http import HttpMcp
from proctable import alive

from tau import (
    Body,
    ImageLimits,
    Mcp,
    McpServer,
    Notice,
    Outcome,
    Part,
    Permissions,
    Stop,
)

SERVER = str(Path(__file__).parents[1] / "mcp_server.py")
DONE = Reply([Part.Text("done")])
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the test server's signals")


def stdio(log: Path, name: str = "test", **env: str) -> McpServer:
    return McpServer(
        name,
        "test",
        "stdio",
        command=sys.executable,
        args=(SERVER,),
        env={"MCP_LOG": str(log), **env},
    )


def running(pid: int) -> bool:
    """Whether `pid` runs (a zombie does not)."""
    return alive(pid)


def logged(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def calling(tool: str, args: object = None) -> Reply:
    return Reply([Part.Call("c1", tool, json.dumps(args or {}))], Stop.Tools)


def outcome_of(h: Harness) -> Outcome:
    results = next(b for b in reversed(h.bodies()) if isinstance(b, Body.Results))
    return results.outcomes[0][1]


async def run(tmp_path: Path, servers: list[McpServer], *replies: Reply) -> Harness:
    provider = FakeProvider([*replies, DONE], image_limits=ImageLimits(1_000_000, 4000))
    return await Harness.create(
        tmp_path,
        provider=provider,
        catalog=fake_catalog(images=True),
        extensions=[Mcp(servers, tmp_path)],
    )


@scenario
async def test_with_no_servers_it_offers_nothing_and_starts_nothing(tmp_path: Path) -> None:
    h = await run(tmp_path, [])
    await h.send("hi")
    await h.settle()
    assert h.provider.requests[0].tools == ()
    assert (
        await h.run("mcp")
        == "no MCP servers are configured (mcp.json in TAU_HOME, .mcp.json in a project)"
    )
    await h.close()


@scenario
async def test_its_tools_are_offered_namespaced_and_called(tmp_path: Path) -> None:
    log = tmp_path / "log"
    h = await run(tmp_path, [stdio(log)], calling("mcp__test__echo", {"text": "hello"}))
    await h.send("go")
    await h.settle()
    names = h.provider.requests[0].tool_names
    assert "mcp__test__echo" in names
    assert "mcp__test__dotted_name" in names  # a character outside [A-Za-z0-9_-] becomes _
    echo = next(t for t in h.provider.requests[0].tools if t.name == "mcp__test__echo")
    assert echo.description == "the echo tool"
    assert echo.parameters["properties"] == {"text": {"type": "string"}}
    assert outcome_of(h) == Outcome.Ok("hello")
    calls = [e for e in logged(log) if e.get("method") == "tools/call"]
    assert calls[0]["params"] == {"name": "echo", "arguments": {"text": "hello"}}
    listing = await h.run("mcp")
    assert listing is not None
    assert listing.startswith(
        f"test (stdio: {sys.executable} {SERVER}): running\n  mcp__test__echo"
    )
    await h.close()


@scenario
async def test_a_paged_list_is_followed(tmp_path: Path) -> None:
    h = await run(tmp_path, [stdio(tmp_path / "log", PAGE_SIZE="3")])
    await h.send("go")
    await h.settle()
    assert "mcp__test__tool_4" in h.provider.requests[0].tool_names
    await h.close()


@scenario
async def test_results_carry_errors_images_structure_and_say_what_was_left_out(
    tmp_path: Path,
) -> None:
    h = await run(
        tmp_path,
        [stdio(tmp_path / "log")],
        calling("mcp__test__fail"),
        calling("mcp__test__image"),
        calling("mcp__test__structured"),
    )
    await h.send("go")
    await h.settle()
    results = [b.outcomes[0][1] for b in h.bodies() if isinstance(b, Body.Results)]
    assert results[0] == Outcome.Err("it failed")
    image = results[1]
    assert isinstance(image, Outcome.Ok)
    assert image.value == "before\n[audio content left out: τ tool results are text and images]"
    assert [(i.mime, i.width) for i in image.images] == [("image/png", 1)]
    assert results[2] == Outcome.Ok('{"answer": 42}')
    await h.close()


@scenario
async def test_env_reaches_the_server_and_stderr_is_not_shown(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    h = await run(
        tmp_path, [stdio(tmp_path / "log", TEST_SECRET="s3cret")], calling("mcp__test__env")
    )
    await h.send("go")
    await h.settle()
    assert outcome_of(h) == Outcome.Ok("s3cret")
    await h.close()
    assert "test server: starting up" not in capfd.readouterr().err


@scenario
async def test_stdout_that_is_not_json_is_skipped_and_kept_for_the_exit(tmp_path: Path) -> None:
    """Servers print banners and stray log lines: such a line fails nothing, and when a server
    exits it is what the exit names (it is usually the reason)."""
    servers = [
        stdio(tmp_path / "log", BANNER="Server v1 listening on stdio"),
        stdio(tmp_path / "down", "down", STDOUT_EXIT="Error: cannot open the database"),
    ]
    noise, echo = calling("mcp__test__garbage"), calling("mcp__test__echo", {"text": "ok"})
    h = await run(tmp_path, servers, noise, echo)
    await h.send("go")
    await h.settle()
    results = [b.outcomes[0][1] for b in h.bodies() if isinstance(b, Body.Results)]
    assert results == [Outcome.Ok("answered after the noise"), Outcome.Ok("ok")]
    down = (
        "MCP server down exited (status 2): [stdout] Error: cannot open the database; "
        "τ starts without it"
    )
    assert [n.text for n in h.notice_events()] == [down]
    await h.close()


@scenario
async def test_a_tool_that_cannot_be_offered_is_left_out_and_said(tmp_path: Path) -> None:
    """No input schema, one that is not an object, or a name too long: that tool is left out,
    with a warning naming it, when the server starts and when it lists its tools again."""
    h = await run(
        tmp_path,
        [stdio(tmp_path / "log", BAD_TOOLS="1")],
        calling("mcp__test__grow"),
        calling("mcp__test__echo", {"text": "the rest work"}),
    )
    await h.send("go")
    await h.settle()
    offered = h.provider.requests[0].tool_names
    assert "mcp__test__echo" in offered
    assert not [n for n in offered if "schema" in n or "xxx" in n]
    assert outcome_of(h) == Outcome.Ok("the rest work")
    said = [n.text for n in h.notice_events() if n.level == "error"]
    long = "mcp__test__" + "x" * 60
    left_out = [
        *(
            f"MCP server test: its tool {name!r} has no input schema that is a JSON object "
            "(inputSchema), so it is left out"
            for name in ("no_schema", "odd_schema")
        ),
        f"MCP server test: its tool {'x' * 60!r} would be named {long!r}, 71 characters (the "
        "limit is 64), so it is left out; give the server a shorter name",
    ]
    assert said == left_out * 2  # at the start, and when it listed its tools again
    await h.close()


@scenario
async def test_a_server_that_fails_to_start_is_reported_and_the_rest_start(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good"
    servers = [
        stdio(tmp_path / "bad", "broken", FAIL_START="cannot open the database"),
        McpServer("missing", "test", "stdio", command=str(tmp_path / "no-such-program")),
        stdio(good, "good"),
    ]
    h = await run(tmp_path, servers, calling("mcp__good__echo", {"text": "still here"}))
    broken = "MCP server broken exited (status 2): cannot open the database; τ starts without it"
    missing = f"MCP server missing: cannot start {tmp_path / 'no-such-program'}: No such file"
    err = capfd.readouterr().err
    assert f"tau: mcp: {broken}" in err
    assert f"tau: mcp: {missing}" in err
    await h.send("go")
    await h.settle()
    notices = [(n.extension, n.level, n.text) for n in h.notice_events()]
    assert ("mcp", "error", broken) in notices  # told during start, heard on connecting
    assert any(text.startswith(missing) for _, _, text in notices)
    assert outcome_of(h) == Outcome.Ok("still here")
    assert not [n for n in h.provider.requests[0].tool_names if not n.startswith("mcp__good__")]
    listing = str(await h.run("mcp")).splitlines()
    assert listing[0].startswith("broken (stdio: ")
    assert listing[0].endswith(
        ": failed: MCP server broken exited (status 2): cannot open the database"
    )
    await h.close()


@scenario
async def test_a_client_that_connects_later_hears_what_did_not_start(tmp_path: Path) -> None:
    h = await run(tmp_path, [stdio(tmp_path / "bad", "broken", FAIL_START="no db")])
    later = await h.connect("later")
    heard: list[object] = []
    while not any(isinstance(e, Notice) for e in heard):  # after its snapshot
        heard.append(await asyncio.wait_for(anext(later), 5))
    (notice,) = [e for e in heard if isinstance(e, Notice)]
    assert (notice.extension, notice.level, notice.cursor) == ("mcp", "error", None)
    assert notice.text == "MCP server broken exited (status 2): no db; τ starts without it"
    await later.close()
    await h.close()


@scenario
async def test_a_server_that_exits_is_told_and_its_tools_answer_an_error(tmp_path: Path) -> None:
    h = await run(
        tmp_path, [stdio(tmp_path / "log")], calling("mcp__test__die"), calling("mcp__test__echo")
    )
    await h.send("go")
    await h.settle()
    results = [b.outcomes[0][1] for b in h.bodies() if isinstance(b, Body.Results)]
    gone = "MCP server test exited (status 3): dying as asked"
    assert results == [Outcome.Err(f"{gone}; its tools answer this until τ restarts")] * 2
    notice = next(n for n in h.notice_events() if n.extension == "mcp")
    assert (notice.text, notice.level) == (
        f"{gone}; its tools answer an error until τ restarts",
        "error",
    )
    assert str(await h.run("mcp")).splitlines()[0].endswith(f": {gone}")
    await h.close()


@scenario
async def test_a_changed_list_is_listed_again_and_offered_from_the_next_sample(
    tmp_path: Path,
) -> None:
    h = await run(
        tmp_path, [stdio(tmp_path / "log")], calling("mcp__test__grow"), calling("mcp__test__grown")
    )
    await h.send("go")
    await h.settle()
    first, second, _ = h.provider.requests
    assert "mcp__test__grown" not in first.tool_names
    assert "mcp__test__grown" in second.tool_names
    assert outcome_of(h) == Outcome.Ok("called grown")
    assert "MCP server test listed its tools again: 12 tools" in h.notices()
    await h.close()


@scenario
async def test_an_abort_cancels_the_call_at_the_server(tmp_path: Path) -> None:
    log = tmp_path / "log"
    h = await run(tmp_path, [stdio(log)], calling("mcp__test__slow", {"seconds": 30}))
    await h.send("go")
    for _ in range(200):
        if any(e.get("method") == "tools/call" for e in logged(log)):
            break
        await asyncio.sleep(0.02)
    await h.abort()
    await h.settle()
    cancelled: list[dict[str, Any]] = []
    for _ in range(200):  # the server writes its log in its own time
        cancelled = [e for e in logged(log) if e.get("method") == "notifications/cancelled"]
        if cancelled:
            break
        await asyncio.sleep(0.02)
    assert cancelled
    assert cancelled[0]["params"]["reason"] == "cancelled by τ"
    await h.close()


@scenario
async def test_a_ping_from_the_server_is_answered(tmp_path: Path) -> None:
    log = tmp_path / "log"
    h = await run(tmp_path, [stdio(log)], calling("mcp__test__ping"))
    await h.send("go")
    await h.settle()
    assert outcome_of(h) == Outcome.Ok("pinged")
    await h.close()


@scenario
async def test_stop_ends_each_server_and_what_it_started(tmp_path: Path) -> None:
    log = tmp_path / "log"
    h = await run(tmp_path, [stdio(log, SPAWN_CHILD="1")])
    child = next(int(e["child"]) for e in logged(log) if "child" in e)  # type: ignore[arg-type]
    server = next(int(e["started"]) for e in logged(log) if "started" in e)  # type: ignore[arg-type]
    await h.close()
    assert not running(server)  # it was waited for
    for _ in range(100):  # what it started was signalled; its new parent reaps it
        if not running(child):
            break
        await asyncio.sleep(0.02)
    assert not running(child)


@scenario
async def test_a_server_rule_asks_about_every_tool_of_that_server(tmp_path: Path) -> None:
    guard = Permissions(tmp_path)
    provider = FakeProvider([calling("mcp__test__echo", {"text": "x"}), DONE])
    h = await Harness.create(
        tmp_path,
        provider=provider,
        extensions=[Mcp([stdio(tmp_path / "log")], tmp_path), guard],
    )
    await h.configure("permissions", {"ask": ["mcp__test"]})
    await h.send("go")
    await h.settle()
    (question,) = (await h.view()).questions
    assert question.text == 'mcp__test__echo: {"text": "x"}\n(ask rule: mcp__test)'
    await h.close()


@scenario
async def test_over_http_with_a_session_and_headers(tmp_path: Path) -> None:
    for sse in (False, True):
        server = HttpMcp(sse=sse, token="t0k")
        spec = McpServer(
            "web", "test", "http", url=server.url, headers={"Authorization": "Bearer t0k"}
        )
        h = await run(tmp_path / str(sse), [spec], calling("mcp__web__echo", {"text": "hi"}))
        await h.send("go")
        await h.settle()
        assert outcome_of(h) == Outcome.Ok("http: hi")
        await h.close()
        server.close()
        methods = [(e.get("method"), e.get("session")) for e in server.log if "method" in e]
        assert methods[0] == ("initialize", None)
        assert ("tools/call", "s-1") in methods
        assert {"delete": "s-1"} in server.log
        assert server.log[-2]["version"] == "2025-11-25"


@scenario
async def test_an_http_server_that_refuses_is_reported(tmp_path: Path) -> None:
    server = HttpMcp(token="right")
    spec = McpServer(
        "web", "test", "http", url=server.url, headers={"Authorization": "Bearer wrong"}
    )
    h = await run(tmp_path, [spec])
    server.close()
    await h.settle()
    (notice,) = h.notice_events()
    assert notice.text == (
        f"MCP server web: {server.url} refused the request (HTTP 401); τ starts without it"
    )
    assert "wrong" not in notice.text
    await h.close()


@scenario
async def test_a_slow_server_is_cut_before_the_start_timeout_and_named(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    catalog = dataclasses.replace(fake_catalog(), timeouts={"mcp": (3.0, None)})
    hung = stdio(tmp_path / "log", "hung", HANG_START="1")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([]),
        catalog=catalog,
        extensions=[Mcp([hung], tmp_path, start_timeout=3.0)],
    )
    err = capfd.readouterr().err
    assert f"MCP server hung did not start within 1.5 s ({sys.executable} {SERVER}; a first" in err
    assert "raise extensions.mcp.start_timeout" in err
    for _ in range(100):  # it was stopped; its log is written in its own time
        if {"stdin": "closed"} in logged(tmp_path / "log"):
            break
        await asyncio.sleep(0.02)
    assert {"stdin": "closed"} in logged(tmp_path / "log")
    await h.close()
