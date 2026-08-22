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
_OUTPUT_EXT = """
def register(api):
    def _todos(args, ctx):
        return "TODOS:" + args

    api.register_command("todos", {"description": "list todos", "handler": _todos})
"""


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
    from pathlib import Path

    from tau_coding_agent.session_store import Session, list_sessions

    infos = list_sessions(cwd=os.getcwd())
    assert infos, "the run should have created a persisted session"
    for info in infos:
        session = Session.load(Path(info.ref))
        for msg in session.context:
            assert msg.get("role") != "user", "a command run must not persist a user turn"
            assert "TODOS:" not in str(msg.get("content", ""))


async def test_unknown_slash_prompt_is_not_a_command(monkeypatch, tmp_path):
    """An unknown ``/…`` is NOT a registered command → it falls through to the model.

    We don't want to hit a provider, so assert the fall-through indirectly: with no
    matching command the run proceeds to ``stream_submission`` (which, against a real
    provider config, raises rather than silently short-circuiting).

    B2-c changed HOW that conclusion is reached, and the change is visible here.
    ``resolve_command`` decides against the known vocabulary BEFORE anything runs, so
    an unknown slash no longer *probes* the extension registry — ``/nope`` used to
    reach ``run_extension_command`` and be told ``handled=False``. Nothing observable
    to the user changes (the text still goes to the model verbatim); what changes is
    that no handler dispatch is attempted for text that was never a command.
    """
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    ext = _write_ext(tmp_path)

    dispatched: list[tuple[str, bool]] = []
    from tau_agent_core.agent_session import AgentSession

    real_run = AgentSession.run_extension_command

    async def _spy(self, name, args=""):
        result = await real_run(self, name, args)
        dispatched.append((name, result.handled))
        return result

    monkeypatch.setattr(AgentSession, "run_extension_command", _spy)

    # Stub stream_submission on the backend class so the fall-through does not hit a
    # provider — capture that the model path WAS reached for the unknown command, and
    # with the print-mode submission (not one derived by the adapter).
    admitted: list = []
    from tau_agent_core.submission import SubmissionResult
    from tau_coding_agent.backends import TauBackend

    async def _fake_stream(self, submission, context, callback, on_event=None, on_pi_event=None):
        admitted.append(submission)
        callback("hi")
        return (
            "hi",
            {"total_tokens": 1},
            [],
            [],
            SubmissionResult(accepted=True, submission_id=submission.submission_id),
        )

    monkeypatch.setattr(TauBackend, "stream_submission", _fake_stream)

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

    # No handler dispatch was attempted, and the unrecognised slash reached the model
    # as ordinary prompt text.
    assert dispatched == []
    assert [s.text for s in admitted] == ["/nope not-a-command"]


async def test_slash_resume_says_print_mode_cannot_perform_it(monkeypatch, tmp_path):
    """The Fail-Early half of §7's third surface, in the mode that has no picker.

    ``/resume`` is a built-in now (``tau_agent_core.commands.FRONTEND_COMMANDS``),
    so print mode RESOLVES it and then says out loud that it cannot perform it —
    the same contract ``/tree`` and ``/compact`` have here. Letting it fall through
    to the model instead is the silent fallback this lifecycle removes: the user
    asked to resume and would have got a paragraph about resuming.

    It matches what ``cli.main`` does with the flag, too: ``tau -p --resume`` is
    refused rather than ignored. One decision, stated in both places.
    """
    import pytest

    from tau_agent_core.commands import UnsupportedCommandError

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)

    args = CLIArgs(messages=["/resume"], print_mode=True, mode="text", model="m")
    with pytest.raises(UnsupportedCommandError) as exc:
        await run_print(args, _config())
    assert "/resume" in str(exc.value)
    assert "print mode" in str(exc.value)
