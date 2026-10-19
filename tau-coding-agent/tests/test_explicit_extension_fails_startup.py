"""#47: an extension named with ``-e`` that fails stops startup; a discovered one warns.

Reference: docs/EXTENSION-PACKAGES.md §12. In 0.13.0 a ``--bus`` extension whose
``session_start`` raised printed a warning, and its tools were still offered.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tau_agent_core import extension_sources
from tau_agent_core.testing import child_env
from tau_coding_agent.backends import create_backend
from tau_coding_agent.headless import CLIError, start_extensions

_START_RAISES = """
def register(api):
    api.register_tool({
        "name": "speak",
        "description": "speak",
        "parameters": {"type": "object", "properties": {}},
        "execute": lambda *a: {"content": []},
    })
    def _start(event, ctx):
        raise RuntimeError("nats-py is missing")
    api.on("session_start", _start)
"""

_REGISTER_RAISES = "def register(api):\n    raise RuntimeError('no config')\n"


def _backend():
    return create_backend({"backend": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1"})


async def test_an_explicit_register_failure_is_a_cli_error_naming_it(tmp_path):
    ext = tmp_path / "handset_bus.py"
    ext.write_text(_REGISTER_RAISES)
    with pytest.raises(CLIError, match=r"extension handset_bus .*failed at load: RuntimeError: no"):
        await start_extensions(_backend(), [str(ext)], discover=False, extensions_config={})


async def test_an_explicit_session_start_failure_is_a_cli_error_naming_it(tmp_path):
    ext = tmp_path / "handset_bus.py"
    ext.write_text(_START_RAISES)
    with pytest.raises(CLIError, match=r"handset_bus .*session_start: nats-py is missing"):
        await start_extensions(_backend(), [str(ext)], discover=False, extensions_config={})


async def test_a_discovered_failure_warns_and_is_listed_as_failed():
    directory = extension_sources.default_extensions_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "found.py").write_text(_START_RAISES)
    backend = _backend()
    await start_extensions(backend, None, discover=True, extensions_config={})
    [record] = backend.extension_records()
    assert (record.identifier, record.state) == ("found", "failed")
    assert record.reason == "session_start raised: nats-py is missing"


def test_rpc_mode_exits_non_zero_before_the_first_turn(tmp_path):
    home = tmp_path / "home"
    (home / ".tau").mkdir(parents=True)
    (home / ".tau" / "config.json").write_text(
        json.dumps(
            {
                "default_model": "m",
                "models": {
                    "m": {"backend": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1"}
                },
            }
        )
    )
    ext = tmp_path / "handset_bus.py"
    ext.write_text(_START_RAISES)
    src = Path(__file__).resolve().parents[2]
    env = child_env(
        home,
        PYTHONPATH=os.pathsep.join(
            str(src / pkg / "src")
            for pkg in ("tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts", "tau-meta")
        ),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", "--mode", "rpc", "--no-session"]
        + ["--no-extensions", "-e", str(ext)],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "handset_bus" in proc.stderr and "nats-py is missing" in proc.stderr
