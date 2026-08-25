"""One system prompt, one builder, three frontends.

The defect these pin: ``TauBackend`` BUILDS the system prompt — base text,
project context files, ``Available tools:`` list — but the TUI and print mode
each used to compose a *second* string from ``config["system_prompt"]`` and store
it as the session's first message. That message takes precedence over the built
prompt, so everything the builder composed was discarded. The TUI's version also
substituted ``"You are a helpful assistant."`` when the config named no prompt,
which meant the message was never absent and the built prompt was never used on
that path at all: no AGENTS.md context and no tool list ever reached a model.

The fix is a seam, not a special case. Every frontend now folds the configured
prompt onto the MODEL ENTRY, where ``TauBackend`` reads it as ``custom_prompt``,
and stores what the backend built. These tests assert the two halves of that:
the frontends agree on what they hand the builder, and the session records what
came back.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import run_print

CONFIG: dict[str, Any] = {
    "models": {
        "m": {"backend": "openai", "model": "m", "base_url": "http://x/v1", "api_key": "k"},
    },
    "default_model": "m",
    "system_prompt": "BASE TEXT FROM CONFIG",
}


class _Recorder:
    """A backend double that records the model config it was built from.

    Composes its ``system_prompt`` the way ``TauBackend`` does — via the shared
    helper — so the double cannot disagree with the real backend about where
    appended sections go.
    """

    instances: list[_Recorder] = []

    def __init__(self, config: dict) -> None:
        from tau_agent_core.sdk import BASE_SYSTEM_PROMPT, append_system_prompt

        self.config = config
        base = config.get("system_prompt") or None
        sections = config.get("append_system_prompt")
        if sections:
            base = append_system_prompt(base or BASE_SYSTEM_PROMPT, list(sections))
        self.system_prompt = base or ""
        _Recorder.instances.append(self)

    async def load_extensions(
        self, explicit_paths=None, *, discover=True, user_dir=None, extensions_config=None
    ):
        from tau_agent_core.sdk import LoadExtensionsResult

        return LoadExtensionsResult()

    async def stream_submission(
        self, submission, context, callback, on_event=None, on_pi_event=None
    ):
        from tau_agent_core.submission import SubmissionResult

        self.context = context
        callback("ok")
        new_messages = [{"role": "assistant", "content": "ok"}]
        result = SubmissionResult(
            accepted=True, submission_id=submission.submission_id, messages=new_messages
        )
        return "ok", {"total_tokens": 1}, new_messages, [], result


@pytest.fixture(autouse=True)
def _clear_recorder():
    _Recorder.instances = []
    yield
    _Recorder.instances = []


def _headless_model_config(monkeypatch, tmp_path, args: CLIArgs) -> dict:
    """Run print mode against the recorder and return the config it received."""
    monkeypatch.setattr("tau_coding_agent.backends.create_backend", _Recorder)
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    asyncio.run(run_print(args, dict(CONFIG)))
    return _Recorder.instances[-1].config


def test_headless_hands_the_configured_prompt_to_the_builder(monkeypatch, tmp_path):
    """``tau -p`` folds ``config["system_prompt"]`` onto the ENTRY.

    Not into the session's first message: the entry is what reaches the builder,
    and only a value on the entry composes with context files and the tool list.
    """
    mc = _headless_model_config(monkeypatch, tmp_path, CLIArgs(messages=["hi"], print_mode=True))
    assert mc["system_prompt"] == "BASE TEXT FROM CONFIG"


def test_tui_hands_the_builder_the_same_thing_headless_does(monkeypatch, tmp_path, make_app):
    """The TUI and print mode agree on the builder's inputs for ONE config.

    This is the invariant the split violated: the same config.json produced a
    coding-agent prompt under ``tau -p`` and ``"You are a helpful assistant."``
    under ``tau``. Comparing the builder's INPUTS rather than its output keeps
    the test independent of whatever AGENTS.md the checkout happens to carry.
    """
    headless_mc = _headless_model_config(
        monkeypatch, tmp_path, CLIArgs(messages=["hi"], print_mode=True)
    )

    app = make_app(create_backend=_Recorder, config=dict(CONFIG))
    tui_mc = app._apply_run_config(app.config["models"]["m"])

    keys = ("system_prompt", "append_system_prompt", "no_context_files")
    assert {k: tui_mc.get(k) for k in keys} == {k: headless_mc.get(k) for k in keys}


def test_append_sections_reach_the_builder_unfolded(monkeypatch, tmp_path):
    """``--append-system-prompt`` rides as its own key, applied once.

    It used to be folded into the stored prompt by each frontend. Now the
    backend applies it to the base text it resolves, so the sections land ahead
    of the project context and the tool list — and cannot be applied twice.
    """
    mc = _headless_model_config(
        monkeypatch,
        tmp_path,
        CLIArgs(messages=["hi"], print_mode=True, append_system_prompt=["EXTRA RULE"]),
    )
    assert mc["append_system_prompt"] == ["EXTRA RULE"]
    assert mc["system_prompt"] == "BASE TEXT FROM CONFIG"
    built = _Recorder.instances[-1].system_prompt
    assert built == "BASE TEXT FROM CONFIG\n\nEXTRA RULE"
    assert built.count("EXTRA RULE") == 1


def test_headless_session_stores_what_the_backend_built(monkeypatch, tmp_path):
    """The session's first message is the COMPOSED prompt, not a second string.

    Storing anything else is what discarded the built prompt: the stored message
    is what goes on the wire.
    """
    monkeypatch.setattr("tau_coding_agent.backends.create_backend", _Recorder)
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    asyncio.run(run_print(CLIArgs(messages=["hi"], print_mode=True), dict(CONFIG)))

    backend = _Recorder.instances[-1]
    system_messages = [m for m in backend.context if m.get("role") == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["content"] == backend.system_prompt


def test_a_config_without_a_prompt_leaves_the_base_text_to_the_builder(monkeypatch, tmp_path):
    """No config prompt means no entry key — NOT a stand-in string.

    The TUI used to substitute ``"You are a helpful assistant."`` here, which is
    why a default install was told it was a chat assistant instead of a coding
    agent. Absence must reach the builder as absence so ``BASE_SYSTEM_PROMPT``
    applies.
    """
    config = {k: v for k, v in CONFIG.items() if k != "system_prompt"}
    monkeypatch.setattr("tau_coding_agent.backends.create_backend", _Recorder)
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    asyncio.run(run_print(CLIArgs(messages=["hi"], print_mode=True), config))

    assert "system_prompt" not in _Recorder.instances[-1].config
