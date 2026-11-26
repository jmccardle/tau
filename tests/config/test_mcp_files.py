"""The files `mcp` reads (D24 section 12): `$TAU_HOME/mcp.json`, a trusted project's `.mcp.json`
and `.tau/mcp.json` (pinned by trust), the `mcpServers` shape, `${VAR}`, `disabled`, and what
the trust report and `tau ext list` say."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from world import World, loaded, tau, trust

from tau import Mcp, McpConfigError, McpServer, UntrustedProject, load_config

PROJECTS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows: projects (D13 trust) are not ported; finding one is an error",
)
SECRET = "ghp_not-a-real-token-41"
MOCK = {"default_model": "m", "models": {"m": {"model": "m", "provider": "mock"}}}


def write(path: Path, servers: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def servers(world: World, cwd: Path | None = None) -> list[McpServer]:
    return list(built(world, cwd).servers)


def built(world: World, cwd: Path | None = None) -> Mcp:
    return Mcp.of(loaded(world, cwd), cwd or world.home)


def test_no_file_is_no_server(world: World) -> None:
    assert servers(world) == []


def test_the_users_file_in_the_shape_other_clients_write(world: World) -> None:
    write(
        world.tau / "mcp.json",
        {
            "fs": {"command": "npx", "args": ["-y", "server-fs", "/tmp"], "alwaysAllow": ["x"]},
            "web": {"url": "https://mcp.example.com/mcp", "headers": {"X-Team": "a"}},
            "typed": {"type": "stdio", "command": "srv"},
        },
    )
    fs, typed, web = servers(world)
    assert (fs.name, fs.transport, fs.command, fs.args) == (
        "fs",
        "stdio",
        "npx",
        ("-y", "server-fs", "/tmp"),
    )
    assert fs.origin == str(world.tau / "mcp.json")
    assert (typed.transport, typed.command) == ("stdio", "srv")
    assert (web.transport, web.url, dict(web.headers)) == (
        "http",
        "https://mcp.example.com/mcp",
        {"X-Team": "a"},
    )


def test_variables_are_replaced_and_a_missing_one_names_itself_not_a_value(world: World) -> None:
    write(
        world.tau / "mcp.json",
        {
            "gh": {
                "command": "${TOOLS:-/opt}/gh-mcp",
                "env": {"GITHUB_TOKEN": "${GH_TOKEN}"},
            }
        },
    )
    world.environ["GH_TOKEN"] = SECRET
    (gh,) = servers(world)
    assert gh.command == "/opt/gh-mcp"
    assert dict(gh.env) == {"GITHUB_TOKEN": SECRET}
    assert SECRET not in repr(gh)
    del world.environ["GH_TOKEN"]
    skipped = built(world)
    assert skipped.servers == ()
    assert skipped.warnings == (
        f"warning: {world.tau / 'mcp.json'}: MCP server 'gh': ${{GH_TOKEN}} is not set (set it in "
        "the environment or the project's .env, or write ${GH_TOKEN:-default}) (skipped)",
    )


def test_each_bad_server_is_skipped_with_a_warning_and_the_rest_are_used(world: World) -> None:
    write(
        world.tau / "mcp.json",
        {
            "old": {"type": "sse", "url": "https://x/sse"},
            "none": {"args": ["x"]},
            "args": {"command": "x", "args": "x"},
            "off": {"disabled": True},
            "fine": {"command": "fine"},
        },
    )
    found = built(world)
    assert [s.name for s in found.servers] == ["fine"]
    lines = [w.removeprefix("warning: ").removesuffix(" (skipped)") for w in found.warnings]
    assert [line.split(": ", 1)[1] for line in lines] == [
        "MCP server 'args': 'args' must be a list of strings",
        "MCP server 'none': needs 'command', a non-empty string",
        "MCP server 'old': the SSE transport (type 'sse') is not supported; use its streamable "
        "HTTP endpoint (type 'http'), or turn it off with \"disabled\": true",
    ]


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("{", "not a JSON file"),
        ("[]", "must hold a JSON object with 'mcpServers'"),
        ('{"mcpServers": []}', "'mcpServers' must be an object of servers by name"),
        ('{"mcpServers": {"a__b": {"command": "x"}}}', "without `__`"),
    ],
)
def test_a_file_that_cannot_be_read_is_skipped_saying_why(
    world: World, text: str, why: str
) -> None:
    (world.tau).mkdir(parents=True, exist_ok=True)
    (world.tau / "mcp.json").write_text(text, encoding="utf-8")
    found = built(world)
    assert found.servers == ()
    (warning,) = found.warnings
    assert warning.startswith(f"warning: {world.tau / 'mcp.json'}: ")
    assert why in warning


@PROJECTS
def test_a_project_reads_mcp_json_and_tau_mcp_json_nearest_first(world: World) -> None:
    root = world.project()
    write(world.tau / "mcp.json", {"a": {"command": "user-a"}, "b": {"command": "user-b"}})
    write(root / ".mcp.json", {"b": {"command": "claude-b"}, "c": {"command": "claude-c"}})
    write(root / ".tau" / "mcp.json", {"c": {"disabled": True}, "d": {"command": "tau-d"}})
    trust(world, root)
    assert [(s.name, s.command) for s in servers(world, root)] == [
        ("a", "user-a"),
        ("b", "claude-b"),
        ("d", "tau-d"),
    ]


@PROJECTS
def test_a_projects_files_are_pinned_by_trust(world: World) -> None:
    root = world.project()
    write(root / ".mcp.json", {"gh": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": SECRET}}})
    report = tau(world, "trust", cwd=root)
    assert report.code == 0, report.err
    assert "Its .mcp.json: MCP servers" in report.out
    assert "  gh: gh-mcp  (sets GITHUB_TOKEN)" in report.out
    assert SECRET not in report.out
    project = loaded(world, root).project
    assert project is not None
    assert dict(project.mcp_files) == {".mcp.json": (root / ".mcp.json").read_bytes()}
    write(root / ".mcp.json", {"gh": {"command": "evil"}})
    with pytest.raises(UntrustedProject) as caught:
        load_config(root, world.environ, ask=None)
    assert "  .mcp.json changed" in str(caught.value)
    assert "  gh: evil" in str(caught.value)


@PROJECTS
def test_a_project_values_come_from_its_env_file(world: World) -> None:
    root = world.project()
    (root / ".env").write_text(f"GH_TOKEN={SECRET}\n", encoding="utf-8")
    write(
        root / ".tau" / "mcp.json",
        {"gh": {"url": "https://x/mcp", "headers": {"Authorization": "Bearer ${GH_TOKEN}"}}},
    )
    trust(world, root)
    (gh,) = servers(world, root)
    assert dict(gh.headers) == {"Authorization": f"Bearer {SECRET}"}


def test_it_is_off_until_enabled_and_a_bad_file_is_a_warning_at_the_start(world: World) -> None:
    listed = tau(world, "ext", "list")
    assert "mcp           off        built-in" in listed.out
    assert "off (default); to turn it on: tau ext enable mcp" in listed.out
    (world.tau / "mcp.json").parent.mkdir(parents=True, exist_ok=True)
    (world.tau / "mcp.json").write_text("{", encoding="utf-8")
    assert tau(world, "ext", "enable", "mcp").code == 0
    assert "mcp           on         built-in" in tau(world, "ext", "list").out
    world.user_config({**MOCK, "load": {"mcp": True}})
    run = tau(world, "-p", "hi")
    assert run.code == 0, run.err
    said = f"warning: {world.tau / 'mcp.json'}: not a JSON file"
    assert f"tau: {said}" in run.err  # the standing notice: tau -p prints notices on stderr


def test_a_permission_rule_for_an_mcp_tool_is_no_misspelling(world: World) -> None:
    """An MCP server's tools are listed only once it runs, so a rule naming one is not checked
    against the tools the extensions provide."""
    world.tau.mkdir(parents=True, exist_ok=True)
    config = {"extensions": {"permissions": {"ask": ["mcp__gh", "mcp__gh__create_issue"]}}}
    (world.tau / "config.json").write_text(json.dumps(config), encoding="utf-8")
    checked = tau(world, "config", "check")
    assert "mcp__gh" not in checked.both, checked.both
    refused = tau(world, "config", "set", "permissions.ask=bsh")
    assert "names the tool 'bsh', which no loaded extension provides" in refused.both


@PROJECTS
def test_tau_mcp_config_names_files_nearest_of_all(world: World) -> None:
    """A program that starts τ (tau-acp, with an editor's servers) adds its own without
    touching the user's or the project's files."""
    root = world.project()
    write(
        root / ".tau" / "mcp.json", {"a": {"command": "project-a"}, "b": {"command": "project-b"}}
    )
    trust(world, root)
    extra, more = world.home / "editor.json", world.home / "more.json"
    write(extra, {"a": {"command": "editor-a"}, "c": {"command": "editor-c"}})
    write(more, {"b": {"disabled": True}})
    world.environ["TAU_MCP_CONFIG"] = f"{extra}{os.pathsep}{more}"
    assert [(s.name, s.command, s.origin) for s in servers(world, root)] == [
        ("a", "editor-a", str(extra)),
        ("c", "editor-c", str(extra)),
    ]
    world.environ["TAU_MCP_CONFIG"] = str(world.home / "gone.json")
    with pytest.raises(
        McpConfigError, match=r"gone\.json: no such file \(TAU_MCP_CONFIG names it\)"
    ):
        servers(world, root)


@pytest.mark.skipif(sys.platform == "win32", reason="the test server's signals")
def test_tau_p_runs_with_the_servers_a_program_names(world: World, tmp_path: Path) -> None:
    """How tau-acp hands τ an editor's servers: `TAU_MCP_CONFIG` and `--set load.mcp=true`."""
    mock = {"provider": "mock", "models": {"echo": {"model": "echo"}}}
    world.user_config({"default_model": "mock:echo", "models": {"mock": mock}})
    server = Path(__file__).parents[1] / "mcp_server.py"
    log = tmp_path / "log"
    editor = tmp_path / "editor.json"
    write(
        editor,
        {"ed": {"command": sys.executable, "args": [str(server)], "env": {"MCP_LOG": str(log)}}},
    )
    world.environ["TAU_MCP_CONFIG"] = str(editor)
    result = tau(world, "-p", "--set", "load.mcp=true", "/mcp")
    assert result.code == 0, result.err
    assert result.out.startswith(f"ed (stdio: {sys.executable} {server}): running\n  mcp__ed__echo")
    assert '{"stdin": "closed"}' in log.read_text()  # stopped when the run ended


@pytest.mark.skipif(sys.platform == "win32", reason="the test server's signals")
def test_tau_p_goes_on_when_a_server_does_not_start_and_says_so(
    world: World, tmp_path: Path
) -> None:
    world.user_config({**MOCK, "load": {"mcp": True}})
    server = Path(__file__).parents[1] / "mcp_server.py"
    env = {"MCP_LOG": str(tmp_path / "log"), "FAIL_START": "no database"}
    write(
        world.tau / "mcp.json",
        {"down": {"command": sys.executable, "args": [str(server)], "env": env}},
    )
    result = tau(world, "-p", "hi")
    assert result.code == 0, result.err
    assert result.out == "mock: hi\n"
    assert "tau: MCP server down exited (status 2): no database; τ starts without it" in result.err


@pytest.mark.skipif(sys.platform == "win32", reason="the test server's signals")
def test_a_stdio_server_gets_only_the_minimal_environment_and_its_env(
    world: World, tmp_path: Path
) -> None:
    """A key in τ's environment reaches only the servers whose `env` names it: the others get
    `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER` (Windows: its own list), as the MCP SDK's
    clients give."""
    world.user_config({**MOCK, "load": {"mcp": True}})
    server = Path(__file__).parents[1] / "mcp_server.py"
    log = tmp_path / "log"
    env = {"MCP_LOG": str(log), "GITHUB_TOKEN": "${TAU_CANARY_PASSED}"}
    spec = {"command": sys.executable, "args": [str(server)], "env": env}
    write(world.tau / "mcp.json", {"gh": spec})
    environ = {
        **world.environ,
        "PATH": os.environ["PATH"],
        "TAU_CANARY_SECRET": "must-not-leak",
        "TAU_CANARY_PASSED": "passed-on-purpose",
    }
    done = subprocess.run(
        [sys.executable, "-m", "tau", "-p", "--no-session", "/mcp"],
        env=environ,
        cwd=world.home,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("gh (stdio: "), done.stdout
    (started,) = [e for e in map(json.loads, log.read_text().splitlines()) if "started" in e]
    got = started["environ"]
    assert "TAU_CANARY_SECRET" not in got
    assert "TAU_CANARY_PASSED" not in got  # passed only under the name `env` gives it
    assert got["GITHUB_TOKEN"] == "passed-on-purpose"
    assert got["PATH"] == os.environ["PATH"]
    assert got["HOME"] == world.environ["HOME"]
