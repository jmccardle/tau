"""E7 §3 (S46) — the command output channel on the headless path.

A headless prompt that is entirely a registered extension slash-command
(``/name args``) RUNS the command instead of a model turn: the handler's returned
value is printed (``--mode text``) or emitted as a ``command_output`` record
(``--mode json``). The command output is display-only chrome — it must not append
a user turn or persist onto the session path, and (because it short-circuits before
``stream_chat``) it never calls the model, so these tests need no provider.

Driven through the real ``run_print`` with a REAL ``TauBackend`` + a real
extension file, so the command actually runs on an actual session registry.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §3 S46 (anchor G7).
"""

from __future__ import annotations

import json

import tau_coding_agent.session_store as store
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import run_print

# An extension registering a report command whose handler RETURNS a string (the
# output channel) — no marker file needed, the printed/emitted output proves it ran.
_OUTPUT_EXT = '''
def register(api):
    def _todos(args, ctx):
        return "TODOS:" + args

    api.register_command("todos", {"description": "list todos", "handler": _todos})
'''


def _config() -> dict:
    return {
        "models": {
            "m": {"backend": "openai", "model": "m", "api_key": "not-needed"},
        },
        "default_model": "m",
        "system_prompt": "You are helpful.",
    }


def _write_ext(tmp_path) -> str:
    ext = tmp_path / "todos_ext.py"
    ext.write_text(_OUTPUT_EXT)
    return str(ext)


async def test_text_mode_prints_command_output(monkeypatch, tmp_path, capsys):
    """``--mode text``: the handler's returned report is printed to stdout."""
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    ext = _write_ext(tmp_path)

    args = CLIArgs(
        messages=["/todos alpha"],
        print_mode=True,
        mode="text",
        model="m",
        extensions=[ext],
        no_extensions=True,  # only the explicit -e loads (no global discovery)
    )
    rc = await run_print(args, _config())
    assert rc == 0

    out = capsys.readouterr().out
    assert "TODOS:alpha" in out


async def test_json_mode_emits_command_output_record(monkeypatch, tmp_path, capsys):
    """``--mode json``: one ``command_output`` record; no model events, no header line."""
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    ext = _write_ext(tmp_path)

    args = CLIArgs(
        messages=["/todos beta"],
        print_mode=True,
        mode="json",
        model="m",
        extensions=[ext],
        no_extensions=True,
    )
    rc = await run_print(args, _config())
    assert rc == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    # A command short-circuits before the model turn, so the ONLY record is the
    # command_output one — no session header, no message_* events.
    assert lines == [{"type": "command_output", "command": "todos", "output": "TODOS:beta"}]


async def test_command_run_does_not_persist_a_user_turn(monkeypatch, tmp_path, capsys):
    """Display-only: running a command appends no user turn to the persisted session.

    Reload-invariance in the spirit of S29 — the on-disk session after a pure
    command run carries only its system message (the report is chrome, never a
    path node), so a reload sees no fabricated user/command turn.
    """
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    ext = _write_ext(tmp_path)

    args = CLIArgs(
        messages=["/todos gamma"],
        print_mode=True,
        mode="text",
        model="m",
        extensions=[ext],
        no_extensions=True,
    )
    rc = await run_print(args, _config())
    assert rc == 0

    # Reload every persisted session under this cwd and assert none recorded the
    # command text as a user turn (nor any assistant/tool output).
    import os

    from tau_coding_agent.session_store import Session, list_sessions

    infos = list_sessions(cwd=os.getcwd())
    assert infos, "the run should have created a persisted session"
    for info in infos:
        session = Session.load(info.path)
        for msg in session.context:
            assert msg.get("role") != "user", "a command run must not persist a user turn"
            assert "TODOS:" not in str(msg.get("content", ""))


async def test_unknown_slash_prompt_is_not_a_command(monkeypatch, tmp_path):
    """An unknown ``/…`` is NOT a registered command → it falls through to the model.

    We don't want to hit a provider, so assert the fall-through indirectly: with no
    matching command the run proceeds to ``stream_chat`` (which, against a real
    provider config, raises rather than silently short-circuiting). The point is
    that ``run_extension_command`` reported ``handled=False`` and the code did NOT
    treat the prompt as a handled command.
    """
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    ext = _write_ext(tmp_path)

    dispatched: list[tuple[str, str]] = []
    from tau_agent_core.agent_session import AgentSession, ExtensionCommandResult

    real_run = AgentSession.run_extension_command

    async def _spy(self, name, args=""):
        result = await real_run(self, name, args)
        dispatched.append((name, result.handled))
        return result

    monkeypatch.setattr(AgentSession, "run_extension_command", _spy)

    # Stub stream_chat on the backend class so the fall-through does not hit a
    # provider — capture that the model path WAS reached for the unknown command.
    reached_model: list[bool] = []
    from tau_coding_agent.backends import TauBackend

    async def _fake_stream(self, messages, callback, on_event=None, on_pi_event=None):
        reached_model.append(True)
        callback("hi")
        return "hi", {"total_tokens": 1}, [], []

    monkeypatch.setattr(TauBackend, "stream_chat", _fake_stream)

    args = CLIArgs(
        messages=["/nope not-a-command"],
        print_mode=True,
        mode="text",
        model="m",
        extensions=[ext],
        no_extensions=True,
    )
    rc = await run_print(args, _config())
    assert rc == 0

    # The command was probed and reported NOT handled, then the model path ran.
    assert dispatched == [("nope", False)]
    assert reached_model == [True]

    _ = ExtensionCommandResult  # imported for the type it names above
