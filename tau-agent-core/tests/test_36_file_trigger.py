"""Smoke test for ``examples/36_file_trigger.py`` — external world -> conversation (S61).

Proves:

* ``check_trigger_once`` reads+consumes a trigger file, queues
  ``"External trigger: <content>"`` via ``api.send_user_message(...,
  deliver_as="nextTurn")``, and truncates the file so it is not re-consumed;
  a missing/empty file is a silent no-op (pi parity);
* ``FileTriggerWatcher`` start/stop is a clean, idempotent thread lifecycle
  that actually notices a file write end-to-end (a real background thread,
  polled with a short interval for test speed);
* the queued content actually rides along with the NEXT real
  ``AgentSession.prompt()`` call (the ``nextTurn`` delivery-mode contract,
  ``agent_session.py:1074`` / ``:633``) — i.e. the full "watcher ->
  conversation" path, not just the queueing call.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S61.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "36_file_trigger.py"
_spec = importlib.util.spec_from_file_location("file_trigger_36_example", _PATH)
assert _spec is not None and _spec.loader is not None
file_trigger = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = file_trigger
_spec.loader.exec_module(file_trigger)


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(),
    )


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event

    async def result(self) -> Any:
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self) -> None:
        pass


def _fake_text_reply(captured: list[dict[str, Any]]) -> Any:
    async def fake(model: Any, context: Any, options: Any = None) -> _Stream:
        if isinstance(context, dict):
            captured.append(context)
        final = _text_assistant("ok")
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _user_texts(context: dict[str, Any]) -> list[str]:
    """Every user-turn text block in the raw context ``stream_simple`` received.

    Messages here are a mix of plain dicts (system prompt) and un-dumped
    ``UserMessage``/``AssistantMessage`` pydantic objects (``agent_loop.py``
    concatenates ``context + prompts`` before dumping), so both shapes are read.
    """
    out: list[str] = []
    for m in context.get("messages", []):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role != "user":
            continue
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                text = (
                    block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
                )
                if text is not None:
                    out.append(text)
    return out


class _FakeApi:
    def __init__(self) -> None:
        self.queued: list[tuple[str, str]] = []

    def send_user_message(self, content: str, deliver_as: str = "followUp") -> None:
        self.queued.append((content, deliver_as))


# ── check_trigger_once ───────────────────────────────────────────────────────


def test_check_trigger_once_queues_and_truncates(tmp_path) -> None:
    trigger = tmp_path / "trigger.txt"
    trigger.write_text("Run the tests\n")
    api = _FakeApi()

    fired = file_trigger.check_trigger_once(str(trigger), api)

    assert fired is True
    assert api.queued == [("External trigger: Run the tests", "nextTurn")]
    assert trigger.read_text() == ""


def test_check_trigger_once_missing_file_is_silent(tmp_path) -> None:
    api = _FakeApi()
    fired = file_trigger.check_trigger_once(str(tmp_path / "does-not-exist.txt"), api)
    assert fired is False
    assert api.queued == []


def test_check_trigger_once_empty_file_is_silent(tmp_path) -> None:
    trigger = tmp_path / "trigger.txt"
    trigger.write_text("   \n")
    api = _FakeApi()
    fired = file_trigger.check_trigger_once(str(trigger), api)
    assert fired is False
    assert api.queued == []


# ── FileTriggerWatcher thread lifecycle ─────────────────────────────────────


def test_watcher_start_stop_is_idempotent_and_clean(tmp_path) -> None:
    watcher = file_trigger.FileTriggerWatcher(str(tmp_path / "trigger.txt"), poll_interval=0.02)
    api = _FakeApi()

    watcher.start(api)
    watcher.start(api)  # second start is a no-op, not a second thread
    assert watcher._thread is not None and watcher._thread.is_alive()

    watcher.stop()
    assert watcher._thread is None

    watcher.stop()  # second stop is a no-op


def test_watcher_notices_a_real_file_write(tmp_path) -> None:
    trigger = tmp_path / "trigger.txt"
    watcher = file_trigger.FileTriggerWatcher(str(trigger), poll_interval=0.02)
    api = _FakeApi()

    watcher.start(api)
    try:
        trigger.write_text("build the docs")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not api.queued:
            time.sleep(0.02)
    finally:
        watcher.stop()

    assert api.queued == [("External trigger: build the docs", "nextTurn")]


# ── end-to-end: watcher -> queued nextTurn message -> next real prompt() ────


async def test_triggered_content_rides_the_next_prompt_call(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    trigger = tmp_path / "trigger.txt"

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt="You are a helpful assistant.",
        extensions=[],
    )
    api = session._bind_extension_api("examples/36_file_trigger.py")
    file_trigger.file_trigger_extension(api, trigger_file=str(trigger), poll_interval=0.02)
    session.set_ui_delegate(_NullUI())

    await session.emit_session_start()
    try:
        trigger.write_text("Run the tests")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and trigger.read_text() != "":
            time.sleep(0.02)

        captured: list[dict[str, Any]] = []
        with patch(
            "tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply(captured)
        ):
            await session.prompt("what's next?")
    finally:
        await session.emit_session_shutdown()

    texts = _user_texts(captured[0])
    assert any("External trigger: Run the tests" in t for t in texts)


class _NullUI:
    def notify(self, message: str, level: str = "info") -> None:
        pass
