"""The files `mcp` reads (D24 section 12): `$TAU_HOME/mcp.json`, a trusted project's `.mcp.json`
and `.tau/mcp.json` (pinned by trust), the `mcpServers` shape, `${VAR}`, `disabled`, and what
the trust report and `tau ext list` say."""

import json
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


def write(path: Path, servers: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def servers(world: World, cwd: Path | None = None) -> list[McpServer]:
    return list(Mcp.of(loaded(world, cwd), cwd or world.home).servers)


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
    with pytest.raises(McpConfigError) as caught:
        servers(world)
    assert str(caught.value) == (
        f"{world.tau / 'mcp.json'}: MCP server 'gh': ${{GH_TOKEN}} is not set (set it in the "
        "environment or the project's .env, or write ${GH_TOKEN:-default})"
    )


def test_every_bad_server_is_named_at_once(world: World) -> None:
    write(
        world.tau / "mcp.json",
        {
            "old": {"type": "sse", "url": "https://x/sse"},
            "none": {"args": ["x"]},
            "args": {"command": "x", "args": "x"},
            "off": {"disabled": True},
        },
    )
    with pytest.raises(McpConfigError) as caught:
        servers(world)
    lines = str(caught.value).splitlines()
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
def test_a_file_that_cannot_be_read_says_why(world: World, text: str, why: str) -> None:
    (world.tau).mkdir(parents=True, exist_ok=True)
    (world.tau / "mcp.json").write_text(text, encoding="utf-8")
    with pytest.raises(McpConfigError, match=r"mcp\.json: ") as caught:
        servers(world)
    assert why in str(caught.value)


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


def test_it_is_off_until_enabled_and_a_bad_file_then_stops_the_start(world: World) -> None:
    listed = tau(world, "ext", "list")
    assert "mcp           off        built-in" in listed.out
    assert "off (default); to turn it on: tau ext enable mcp" in listed.out
    (world.tau / "mcp.json").parent.mkdir(parents=True, exist_ok=True)
    (world.tau / "mcp.json").write_text("{", encoding="utf-8")
    assert tau(world, "ext", "enable", "mcp").code == 0
    listed = tau(world, "ext", "list")
    assert "mcp           failed" in listed.out
    assert "mcp.json: not a JSON file" in listed.out


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
