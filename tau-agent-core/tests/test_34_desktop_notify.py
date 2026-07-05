"""Smoke test for ``examples/34_desktop_notify.py`` — OSC 777 on agent_end (S61).

Proves, through the FULL ``AgentSession.prompt`` path (only the network
boundary faked):

* ``notify_osc777`` emits the exact OSC 777 escape sequence (pi parity byte
  for byte, modulo the ``tau``/``Ready for input`` strings);
* the extension wires ``agent_end`` — a plain notify ``AgentEvent`` on the
  ``EventBus`` (NOT a mutating hook) — and the handler fires exactly once per
  completed ``prompt()`` call, writing to the injected stream.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S61.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "34_desktop_notify.py"
_spec = importlib.util.spec_from_file_location("desktop_notify_34_example", _PATH)
assert _spec is not None and _spec.loader is not None
desktop_notify = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = desktop_notify
_spec.loader.exec_module(desktop_notify)


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


async def _fake_text_reply(model: Any, context: Any, options: Any = None) -> _Stream:
    final = _text_assistant("ok")
    return _Stream([DoneEvent(final=final, usage=Usage())])


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


# ── pure OSC 777 formatting ──────────────────────────────────────────────────


def test_notify_osc777_writes_exact_escape_sequence() -> None:
    buf = io.StringIO()
    desktop_notify.notify_osc777("tau", "Ready for input", stream=buf)
    assert buf.getvalue() == "\x1b]777;notify;tau;Ready for input\x07"


def test_on_agent_end_pings_with_fixed_title_and_body() -> None:
    buf = io.StringIO()
    desktop_notify.on_agent_end(object(), stream=buf)
    assert buf.getvalue() == "\x1b]777;notify;tau;Ready for input\x07"


# ── real agent_end dispatch through the full session ────────────────────────


async def test_agent_end_fires_notify_through_a_real_prompt(monkeypatch) -> None:
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt="You are a helpful assistant.",
        extensions=[],
    )
    api = session._bind_extension_api("examples/34_desktop_notify.py")

    calls: list[Any] = []
    buf = io.StringIO()

    def spy_on_agent_end(event: Any, *, stream: Any = None) -> None:
        calls.append(event.type)
        desktop_notify.notify_osc777("tau", "Ready for input", stream=stream)

    api.on("agent_end", lambda event: spy_on_agent_end(event, stream=buf))

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply):
        await session.prompt("hello")

    assert calls == ["agent_end"]
    assert buf.getvalue() == "\x1b]777;notify;tau;Ready for input\x07"


async def test_desktop_notify_extension_registers_agent_end_only(monkeypatch) -> None:
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt="You are a helpful assistant.",
        extensions=[],
    )
    api = session._bind_extension_api("examples/34_desktop_notify.py")

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    desktop_notify.desktop_notify_extension(api)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply):
        await session.prompt("hello")

    assert buf.getvalue() == "\x1b]777;notify;tau;Ready for input\x07"
