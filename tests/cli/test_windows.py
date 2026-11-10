"""What is not ported to Windows says so where it is used, naming the platform (docs/plan.md,
Phase 9): the bash tool, `tau serve` and its clients, and projects (D13)."""

import asyncio
import sys
from pathlib import Path

import pytest
from world import World, tau

from tau import BashTool, RemoteClient, base_tools

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the errors of Windows only")


def test_the_bash_tool_is_an_error_and_the_base_tools_are_the_other_five(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="the bash tool is not available on win32"):
        BashTool(tmp_path)
    assert [tool.name for tool in base_tools(tmp_path)] == ["read", "write", "edit", "grep", "find"]


def test_tau_serve_is_an_error(world: World) -> None:
    result = tau(world, "serve")
    assert result.code != 0
    assert "not available on Windows" in result.err, result.err


def test_connecting_to_a_server_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="not available on Windows"):
        asyncio.run(RemoteClient.connect(tmp_path / "socket", "test"))


def test_a_project_is_an_error(world: World) -> None:
    root = world.home / "proj"
    (root / ".tau").mkdir(parents=True)
    result = tau(world, "-p", "hello", cwd=root)
    assert result.code != 0
    assert "projects are not available on Windows yet" in result.err, result.err
