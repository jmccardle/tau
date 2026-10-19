"""``--bus`` without nats-py refuses to start, in every head (#56)."""

from __future__ import annotations

import importlib.util
import sys

import pytest

from tau_agent_core.sdk import (
    BUS_DEPENDENCY_HINT,
    BusDependencyError,
    create_agent_session,
    require_bus_dependency,
)
from tau_coding_agent import cli

_real_find_spec = importlib.util.find_spec


@pytest.fixture
def no_nats(monkeypatch):
    """Make ``nats`` unlocatable, and fail the test if anything imports it."""

    def find_spec(name, *args, **kwargs):
        if name == "nats" or name.startswith("nats."):
            return None
        return _real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setitem(sys.modules, "nats", None)


@pytest.fixture
def nothing_proceeds(monkeypatch):
    """Fail if startup reaches any head's launch; config loads as an empty one."""
    import tau_coding_agent.repl as repl
    import tau_coding_agent.rpc_mode as rpc_mode

    def boom(*_args, **_kwargs):
        raise AssertionError("startup proceeded past the --bus dependency check")

    monkeypatch.setattr(cli, "load_config", lambda: {})
    for module, name in (
        (cli, "_launch_tui"),
        (cli, "run_print"),
        (rpc_mode, "run_rpc"),
        (repl, "run_repl"),
    ):
        monkeypatch.setattr(module, name, boom)


def test_require_bus_dependency_refuses_with_the_hint(no_nats):
    with pytest.raises(BusDependencyError) as info:
        require_bus_dependency()
    assert str(info.value) == BUS_DEPENDENCY_HINT
    assert BUS_DEPENDENCY_HINT == "--bus needs nats-py: pip install 'ffwf-tau-agent-core[bus]'"


def test_require_bus_dependency_passes_when_nats_is_locatable(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())
    require_bus_dependency()


def test_create_agent_session_with_a_bus_refuses_without_nats(no_nats):
    with pytest.raises(BusDependencyError):
        create_agent_session(model="gpt-4o", bus_available=True)


@pytest.mark.parametrize(
    "argv",
    [
        ["--bus"],
        ["-p", "--bus", "hi"],
        ["-p", "--mode", "json", "--bus", "hi"],
        ["--mode", "rpc", "--bus"],
        ["--mode", "repl", "--bus"],
    ],
    ids=["tui", "print", "print-json", "rpc", "repl"],
)
def test_every_head_exits_2_with_the_hint(argv, no_nats, nothing_proceeds, capsys):
    assert cli.main(argv) == 2
    assert capsys.readouterr().err.strip() == f"tau: error: {BUS_DEPENDENCY_HINT}"


def test_no_bus_flag_does_not_check(no_nats, nothing_proceeds):
    with pytest.raises(AssertionError, match="proceeded past"):
        cli.main(["-p", "hi"])


@pytest.mark.parametrize(
    ("argv", "config"),
    [(["--connect", "127.0.0.1:1", "--bus"], {}), (["--bus"], {"serve": {"autostart": True}})],
    ids=["connect", "autostart"],
)
def test_bus_under_connect_is_the_local_only_error_not_the_dependency(
    argv, config, no_nats, monkeypatch, capsys
):
    """The flag is wrong there whatever is installed, so that is the error reported."""
    monkeypatch.setattr(cli, "load_config", lambda: config)
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "--bus configures a session in this process" in err
    assert "nats-py" not in err
