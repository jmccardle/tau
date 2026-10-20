"""A resumed session whose recorded model is gone opens; only a turn is refused (docs/SILENT-DEFAULTS.md §10).

The serve head has its own end-to-end test (test_serve.py); this covers the
shared resolution the REPL and TUI use, the TUI's resume, and ``tau -p -c``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from tau_agent_core import ModelUnavailableError
from tau_agent_core.cursor import Cursor
from tau_agent_core.submission import Submission
from tau_agent_core.tau_home import HOME_ENV
from tau_agent_core.testing import child_env
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, TextContent, Usage

from tau_coding_agent.backends import create_backend, make_model_resolver
from tau_coding_agent.chat_widgets import ChatSelected
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import CLIError, resolve_model_config
from tau_coding_agent.session_store import FileSessionCatalog

_UNUSED = {"backend": "openai", "model": "u", "base_url": "http://127.0.0.1:1/v1", "api_key": "x"}
_CONFIG: dict[str, Any] = {"models": {"unused": _UNUSED}}


class _Stream:
    def __init__(self, text: str) -> None:
        self._message = AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model="u",
            stop_reason="stop",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    def __aiter__(self):
        async def gen():
            yield TextDeltaEvent(delta=self._message.content[0].text, partial=self._message)
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return gen()

    def abort(self) -> None:
        pass


async def _ok(model: Any, context: Any, options: Any = None) -> _Stream:
    return _Stream("ok")


def _open(tmp_path: Path, config: dict[str, Any], args: CLIArgs | None = None):
    catalog = FileSessionCatalog(tmp_path / "sessions")
    log = catalog.create(str(tmp_path), "local-llm-tun", "openai")
    name, model_config = resolve_model_config(
        config,
        args or CLIArgs(),
        fallback_model=log.model,
        prior_config=log.config,
        open_unavailable=True,
    )
    backend: Any = create_backend({**model_config, "cwd": str(tmp_path)})
    backend.bind_cursor(Cursor.newest(log))
    backend.agent_session.set_model_resolver(make_model_resolver(config))
    return name, backend.agent_session


async def test_the_shared_resolution_opens_it_refuses_a_turn_and_set_model_repairs(tmp_path):
    name, session = _open(tmp_path, _CONFIG)
    assert name == session.model_name == "local-llm-tun"
    message = session.model_unavailable
    assert message is not None and "'local-llm-tun' is unavailable: it is not configured" in message
    assert "(configured: unused)" in message and "/model" in message
    with pytest.raises(ModelUnavailableError, match="'local-llm-tun' is unavailable"):
        session.get_model()
    assert session.get_session_stats().context_window is None

    with patch("tau_agent_core.agent_loop.stream_simple") as never:
        result = await session.submit(
            Submission(submission_id=uuid4().hex, text="hi", source="test", submitter="t")
        )
        never.assert_not_called()
    assert not result.accepted and result.rejection_reason == message
    assert session.messages == []

    session.set_model("unused")
    assert session.model_unavailable is None
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_ok):
        result = await session.submit(
            Submission(submission_id=uuid4().hex, text="hi", source="test", submitter="t")
        )
    assert result.accepted


def test_a_named_model_is_still_refused_in_one_line(tmp_path):
    with pytest.raises(CLIError, match="model 'nope' is not configured"):
        _open(tmp_path, _CONFIG, CLIArgs(model="nope"))


def test_an_entry_that_no_longer_builds_opens_with_the_build_error(tmp_path):
    config = {"models": {**_CONFIG["models"], "local-llm-tun": {"backend": "acme", "model": "m"}}}
    _, session = _open(tmp_path, config)
    assert "backend 'acme' is not a registered provider" in (session.model_unavailable or "")


def test_print_continue_refuses_in_one_line_naming_the_recorded_model(tmp_path):
    home = tmp_path / "tau-home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps(_CONFIG))
    env = child_env(tmp_path / "home", **{HOME_ENV: str(home), "TMPDIR": str(tmp_path)})
    seed = (
        "from tau_coding_agent.store_factory import build_session_catalog;"
        f"build_session_catalog({{}}, None, None).create({str(tmp_path)!r}, 'local-llm-tun', 'openai')"
    )
    subprocess.run([sys.executable, "-c", seed], env=env, check=True, timeout=60)
    out = subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", "-p", "-c", "hi"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert out.returncode == 2, out.stderr
    assert out.stderr.count("\n") == 1, out.stderr
    assert out.stderr.startswith(
        "tau: error: the resumed session's model 'local-llm-tun' is unavailable: it is not configured"
    )
    assert "Pass --model NAME" in out.stderr


async def test_the_tui_resumes_it_and_says_why_a_turn_will_be_refused(make_app):
    app = make_app()
    notes: list[tuple[str, str]] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, **kw: notes.append((message, kw.get("severity", "")))  # type: ignore[method-assign]
        seeded = app.session_catalog.create(os.getcwd(), "local-llm-tun", "openai")
        await Cursor.newest(seeded).append_message({"role": "user", "content": "hello"})
        app.post_message(ChatSelected(str(seeded.path)))
        await pilot.pause()
        assert app.current_session is not None and app.current_session.id == seeded.id
        assert app.messages[-1] == {"role": "user", "content": "hello"}
        [(error, severity)] = [n for n in notes if "unavailable" in n[0]]
        assert severity == "error" and "'local-llm-tun' is unavailable" in error


_BROKEN = [
    (None, "models.local-llm-tun must be a JSON object; got null"),
    ("x", 'models.local-llm-tun must be a JSON object; got "x"'),
    (
        {**_UNUSED, "compaction": {"reserve_tokens": "lots"}},
        "'models.local-llm-tun.compaction.reserve_tokens' must be an integer; got \"lots\"",
    ),
]
_BROKEN_IDS = ["null", "string", "bad-compaction"]


@pytest.mark.parametrize(("recorded", "why"), _BROKEN, ids=_BROKEN_IDS)
async def test_a_malformed_recorded_entry_opens_and_refuses_only_a_turn(tmp_path, recorded, why):
    """The resolution REPL, TUI and serve share; never a TypeError or an unnamed block."""
    config = {"models": {**_CONFIG["models"], "local-llm-tun": recorded}}
    _, session = _open(tmp_path, config)
    assert f"'local-llm-tun' is unavailable: {why}" in (session.model_unavailable or "")
    with patch("tau_agent_core.agent_loop.stream_simple") as never:
        result = await session.submit(
            Submission(submission_id=uuid4().hex, text="hi", source="test", submitter="t")
        )
        never.assert_not_called()
    assert not result.accepted


def _seeded_child(
    tmp_path: Path, config: dict[str, Any], *argv: str
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "tau-home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps(config))
    env = child_env(tmp_path / "home", **{HOME_ENV: str(home), "TMPDIR": str(tmp_path)})
    seed = (
        "from tau_coding_agent.store_factory import build_session_catalog;"
        f"build_session_catalog({{}}, None, None).create({str(tmp_path)!r}, 'local-llm-tun', 'openai')"
    )
    subprocess.run([sys.executable, "-c", seed], env=env, check=True, timeout=60)
    return subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", *argv],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


@pytest.mark.parametrize(("recorded", "why"), _BROKEN, ids=_BROKEN_IDS)
def test_print_continue_on_a_malformed_recorded_entry_is_the_resumed_one_liner(
    tmp_path, recorded, why
):
    out = _seeded_child(
        tmp_path, {"models": {**_CONFIG["models"], "local-llm-tun": recorded}}, "-p", "-c", "hi"
    )
    assert out.returncode == 2, out.stderr
    assert out.stderr.count("\n") == 1, out.stderr
    assert out.stderr.startswith(
        f"tau: error: the resumed session's model 'local-llm-tun' is unavailable: {why}"
    )
    assert out.stderr.rstrip().endswith("Pass --model NAME to continue it on a configured model")


@pytest.mark.parametrize(("recorded", "why"), _BROKEN, ids=_BROKEN_IDS)
def test_repl_continue_on_a_malformed_recorded_entry_opens_and_says_why(tmp_path, recorded, why):
    out = _seeded_child(
        tmp_path,
        {"models": {**_CONFIG["models"], "local-llm-tun": recorded}},
        "--mode",
        "repl",
        "-c",
    )
    assert out.returncode == 0, out.stderr
    assert "Traceback" not in out.stderr and "tau: error" not in out.stderr
    said = " ".join((out.stdout + out.stderr).split())
    assert f"'local-llm-tun' is unavailable: {why}" in said


@pytest.mark.parametrize(("recorded", "why"), _BROKEN, ids=_BROKEN_IDS)
async def test_the_tui_resumes_a_malformed_recorded_entry(make_app, recorded, why):
    app = make_app(
        config={
            "models": {**_CONFIG["models"], "local-llm-tun": recorded},
            "default_model": "unused",
        }
    )
    notes: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, **kw: notes.append(message)  # type: ignore[method-assign]
        seeded = app.session_catalog.create(os.getcwd(), "local-llm-tun", "openai")
        app.post_message(ChatSelected(str(seeded.path)))
        await pilot.pause()
        assert app.current_session is not None and app.current_session.id == seeded.id
        assert app._session_facts().model == f"local-llm-tun — {why}"
    assert any(f"'local-llm-tun' is unavailable: {why}" in note for note in notes), notes
