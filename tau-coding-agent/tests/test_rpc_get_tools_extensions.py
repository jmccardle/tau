"""``--mode rpc`` lists extension tools through ``get_tools`` (issue #46).

Drives a real ``python -m tau_coding_agent.cli --mode rpc`` child over pipes,
with its own ``$HOME`` and ``$TMPDIR`` and no inherited τ home, so it
cannot read a tester's config or extensions. No turn is sent, so the model's
base URL is never reached.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tau_agent_core.testing import child_env

_EXTENSION = """
async def _run(tool_call_id, params, signal, on_update, ctx):
    return {{"content": [{{"type": "text", "text": "ok"}}]}}


def register(api):
    api.register_tool(
        {{
            "name": "{name}",
            "label": "{name}",
            "description": "extension tool {name}",
            "parameters": {{"type": "object", "properties": {{}}}},
            "execute": _run,
        }}
    )
"""


def _child_env(tmp_path: Path) -> dict[str, str]:
    """An isolated environment: a fake ``$HOME`` with one unreachable model."""
    tau_dir = tmp_path / "home" / ".tau"
    tau_dir.mkdir(parents=True)
    (tau_dir / "config.json").write_text(
        json.dumps(
            {
                "models": {
                    "fake": {
                        "backend": "openai",
                        "model": "fake-model",
                        "base_url": "http://127.0.0.1:1/v1",
                        "api_key": "x",
                    }
                },
                "default_model": "fake",
            }
        )
    )
    (tmp_path / "tmp").mkdir()
    return child_env(tmp_path / "home", TMPDIR=str(tmp_path / "tmp"))


def test_no_builtin_tools_with_two_extensions_lists_exactly_their_tools(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    for stem, tool in (("a", "alpha"), ("b", "beta")):
        (work / f"{stem}.py").write_text(_EXTENSION.format(name=tool))
    request = {"jsonrpc": "2.0", "id": 1, "method": "get_tools"}

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "tau_coding_agent.cli",
            "--mode",
            "rpc",
            "--no-extensions",
            "--no-builtin-tools",
            "-e",
            "a.py",
            "-e",
            "b.py",
        ],
        input=json.dumps(request) + "\n",
        cwd=str(work),
        env=_child_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr
    replies = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    (reply,) = [r for r in replies if r.get("id") == 1]
    assert [t["name"] for t in reply["result"]["tools"]] == ["alpha", "beta"]
