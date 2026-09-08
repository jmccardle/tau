"""E7 §3 (S49) — extension activity on the headless ``--mode json`` stream (G10).

Before S49, ``api.ui.notify`` was invisible in ``--mode json`` (it only ever hit
stderr), so a parent orchestrating a child ``tau -p --mode json`` could not see the
child's extension activity. S49 adds a parallel record family —
``{"type": "extension", "kind": "notify", …}`` — that the JSON frontend writes
alongside the closed ``AgentEvent`` set (the same pattern as the session header and
the S46 ``command_output`` records). ``--mode text`` keeps the stderr behaviour.

Driven through the real ``run_print`` with a REAL ``TauBackend`` + a real extension
file. The extension notifies from ``session_start`` and from a slash-command handler,
so the whole flow runs WITHOUT a provider (a command short-circuits before the model
turn), yet still exercises the sink on both the lifecycle and command paths.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §3 S49 (anchor G10).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import tau_coding_agent.session_store as store
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import run_print
from tau_coding_agent.session_store import Session, list_sessions

_NOTIFY_EXT = """
def register(api):
    def _on_start(event, ctx):
        api.ui.notify("NOTIFYSIG-start", "info")

    def _ping(args, ctx):
        api.ui.notify("pong " + args, "warning")
        return "PONG:" + args

    api.on("session_start", _on_start)
    api.register_command("ping", {"description": "ping", "handler": _ping})
"""


def _config() -> dict:
    return {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "You are helpful.",
    }


def _write_ext(tmp_path) -> str:
    ext = tmp_path / "notify_ext.py"
    ext.write_text(_NOTIFY_EXT)
    return str(ext)


def _args(tmp_path, mode: str) -> CLIArgs:
    return CLIArgs(
        messages=["/ping alpha"],
        print_mode=True,
        mode=mode,
        model="m",
        extensions=[_write_ext(tmp_path)],
        no_extensions=True,  # only the explicit -e loads (no global discovery)
    )


async def test_json_mode_emits_extension_notify_records(monkeypatch, tmp_path, capsys):
    """``--mode json``: session_start and command notifies land as ``extension`` records."""
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)

    rc = await run_print(_args(tmp_path, "json"), _config())
    assert rc == 0

    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    ext_records = [r for r in lines if r.get("type") == "extension"]

    assert ext_records == [
        {
            "type": "extension",
            "kind": "notify",
            "extension": None,
            "level": "info",
            "message": "NOTIFYSIG-start",
        },
        {
            "type": "extension",
            "kind": "notify",
            "extension": None,
            "level": "warning",
            "message": "pong alpha",
        },
    ]
    # The command still emits its own output record (S46) — the two families coexist.
    assert {"type": "command_output", "command": "ping", "output": "PONG:alpha"} in lines
    # Routed to stdout, NOT the stderr sink.
    assert "NOTIFYSIG-start" not in captured.err
    assert "pong alpha" not in captured.err


async def test_text_mode_keeps_notify_on_stderr(monkeypatch, tmp_path, capsys):
    """``--mode text``: no record sink is installed → notify stays on stderr, off stdout."""
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)

    rc = await run_print(_args(tmp_path, "text"), _config())
    assert rc == 0

    captured = capsys.readouterr()
    # The notifies surface on stderr (unchanged pre-S49 behaviour)...
    assert "NOTIFYSIG-start" in captured.err
    assert "pong alpha" in captured.err
    # ...and no JSON extension record leaks onto stdout.
    assert '"type": "extension"' not in captured.out
    assert '"type":"extension"' not in captured.out


async def test_extension_records_are_display_only_not_persisted(monkeypatch, tmp_path):
    """Reload-invariance (S29 spirit): notify activity is chrome, never a path node.

    The records are a display channel — like ``command_output`` — so a reloaded
    session must carry no message fabricated from the notify text.

    The markers must be strings no prose would contain. This searches EVERY
    persisted message, and the system prompt of a run in this repository carries
    the project instructions — so the original ``"started"`` failed the day a
    docs paragraph used the word.
    """
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)

    rc = await run_print(_args(tmp_path, "json"), _config())
    assert rc == 0

    infos = list_sessions(cwd=os.getcwd())
    assert infos, "the run should have created a persisted session"
    for info in infos:
        session = Session.load(Path(info.ref))
        for msg in session.context:
            content = str(msg.get("content", ""))
            assert "NOTIFYSIG-start" not in content
            assert "pong" not in content
            assert "PONG" not in content
