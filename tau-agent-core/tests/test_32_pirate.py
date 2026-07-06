"""Smoke test for ``examples/32_pirate.py`` — the ``before_agent_start`` chain (S60).

Proves, through the FULL ``AgentSession.prompt`` path (only the network
boundary faked):

* ``/pirate`` is runnable via ``run_extension_command`` (S46 output channel) and
  toggles the addendum on/off, notifying via ``ctx.ui``;
* with pirate mode OFF, the system prompt reaching the model is unchanged;
* with pirate mode ON, the system prompt reaching the model carries the pirate
  addendum — read off the injected ``role: "system"`` message the loop inserts
  (``agent_loop.py`` ``_stream_response``), i.e. what the model ACTUALLY sees,
  not merely what the handler returned;
* toggling back OFF resets the next turn's prompt to the base (no bleed-through)
  — the per-call framing E5 §1 guarantees (never a persisted tree node).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S60.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "32_pirate.py"
_spec = importlib.util.spec_from_file_location("pirate_32_example", _PATH)
assert _spec is not None and _spec.loader is not None
pirate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pirate
_spec.loader.exec_module(pirate)


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


def _fake_text_reply(captured: list[dict[str, Any]]):
    async def fake(model, context, options=None):
        if isinstance(context, dict):
            captured.append(context)
        final = _text_assistant("ahoy")
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


def _system_prompt_text(context: dict[str, Any]) -> str:
    for m in context.get("messages", []):
        if isinstance(m, dict) and m.get("role") == "system":
            return str(m.get("content") or "")
    return ""


def _session_with_pirate() -> AgentSession:
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt="You are a helpful assistant.",
        extensions=[],
    )
    pirate.pirate_extension(session._bind_extension_api("examples/32_pirate.py"))
    return session


async def test_pirate_off_by_default_system_prompt_unchanged() -> None:
    session = _session_with_pirate()
    captured: list[dict[str, Any]] = []
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply(captured)):
        await session.prompt("hello")
    assert "PIRATE MODE" not in _system_prompt_text(captured[0])


async def test_pirate_command_toggles_and_notifies() -> None:
    session = _session_with_pirate()
    delegate_calls: list[tuple[str, str]] = []

    class _Delegate:
        def notify(self, message: str, level: str = "info") -> None:
            delegate_calls.append((message, level))

    session.set_ui_delegate(_Delegate())

    result_on = await session.run_extension_command("pirate", "")
    assert result_on.handled is True
    assert result_on.output == "Arrr! Pirate mode enabled!"
    assert delegate_calls == [("Arrr! Pirate mode enabled!", "info")]

    result_off = await session.run_extension_command("pirate", "")
    assert result_off.output == "Pirate mode disabled"


async def test_pirate_on_appends_addendum_to_the_system_prompt_the_model_sees() -> None:
    session = _session_with_pirate()
    await session.run_extension_command("pirate", "")  # turn it on

    captured: list[dict[str, Any]] = []
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply(captured)):
        await session.prompt("hello")

    system_text = _system_prompt_text(captured[0])
    assert "You are a helpful assistant." in system_text
    assert "PIRATE MODE" in system_text
    assert "Arrr!" in system_text


async def test_pirate_off_again_resets_next_turns_prompt() -> None:
    session = _session_with_pirate()
    await session.run_extension_command("pirate", "")  # on
    await session.run_extension_command("pirate", "")  # off again

    captured: list[dict[str, Any]] = []
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply(captured)):
        await session.prompt("hello")

    assert "PIRATE MODE" not in _system_prompt_text(captured[0])
