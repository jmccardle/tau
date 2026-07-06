"""E5 §2 (S26/S27) — ``AgentSession.load_extensions`` binds FILE extensions live.

The E0–E4 loader (``sdk._load_extensions``) was called only by tests: it imported
and invoked ``register(api)`` against a *standalone* api whose hooks reached no
running loop (E5 §0, the orphan chain). This closes that seam — a file extension
loaded through ``AgentSession.load_extensions`` is bound to the session's live
``ExtensionRunner`` bucket (labelled by its file path), so its mutating hooks FIRE
in the same loop the session drives.

These tests assert the wiring END-TO-END through the real ``AgentSession`` +
fake-``stream_simple`` loop (the S24 bridge harness): a regression that re-orphans
file extensions FAILS here.

Reference: docs/EXTENSIONS-E5-WIRING.md §2, S26/S27; D-E5-7 (load split from bind).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog


# ── loop harness (a faked network boundary; everything else is real) ──────────


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


def _tool_call_assistant(call_id: str, name: str, args: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=args)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=Usage(),
    )


class _Stream:
    """Minimal async stream matching the stream_simple contract."""

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


def _has_tool_result(messages: list[Any], tool_name: str) -> bool:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return True
    return False


def _tool_result_text(messages: list[Any], tool_name: str) -> str:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            content = m["content"] if isinstance(m, dict) else m.content
            block = content[0]
            return block["text"] if isinstance(block, dict) else block.text
    raise AssertionError(f"no toolResult for {tool_name}")


def _fake_stream_calling(tool_name: str, tool_args: dict[str, Any]):
    """Emit one tool call, then a text stop once a toolResult for it appears."""

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if _has_tool_result(messages, tool_name):
            final = _text_assistant("done")
            return _Stream(
                [
                    TextDeltaEvent(delta="done", partial=final),
                    DoneEvent(final=final, usage=Usage()),
                ]
            )
        final = _tool_call_assistant("call_1", tool_name, tool_args)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _make_session() -> AgentSession:
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    return AgentSession(session_log=InMemorySessionLog(), model=model)


# ── extension source fixtures (registered against the LIVE session api) ───────

# A tool + a tool_result hook that appends a marker to the tool's result. If the
# hook is bound to the session's live runner, the marker appears on the persisted
# toolResult node the model sees (the durable-hook template, E5 §1.1).
_TOOL_RESULT_EXT = """
async def _exec(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "raw-result"}]}

def register(api):
    api.register_tool({
        "name": "probe",
        "description": "a probe tool",
        "parameters": {"type": "object", "properties": {}},
        "execute": _exec,
    })

    def on_tool_result(event, ctx):
        content = event.get("content") or []
        text = content[0].get("text", "") if content else ""
        return {"content": [{"type": "text", "text": text + " +EXT_MARKER"}]}

    api.on("tool_result", on_tool_result)
"""

# Same shape, but an ASYNC register() — only awaiting it registers the hook.
_ASYNC_TOOL_RESULT_EXT = _TOOL_RESULT_EXT.replace("def register(api):", "async def register(api):")

_BROKEN_EXT = "raise RuntimeError('boom during import')\n"


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


# ── the spine: a file extension's hook fires in the live loop ─────────────────


class TestFileExtensionBindsLive:
    async def test_file_tool_result_hook_fires_end_to_end(self, tmp_path):
        """A file extension loaded via load_extensions edits the tool result live."""
        ext = _write(tmp_path / "probe_ext.py", _TOOL_RESULT_EXT)
        session = _make_session()

        result = await session.load_extensions([str(ext)], discover=False)

        # Loaded, no errors, and the hook populated the session's live runner.
        assert len(result.extensions) == 1
        assert result.errors == []
        assert session._extension_runner.has_handlers("tool_result") is True

        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_fake_stream_calling("probe", {}),
        ):
            messages = await session.prompt("call the probe tool")

        # The hook ran in the loop: the persisted toolResult carries the marker.
        assert "+EXT_MARKER" in _tool_result_text(messages, "probe")

    async def test_bucket_is_labelled_by_file_path(self, tmp_path):
        """The runner bucket for a file extension is keyed by its resolved path."""
        ext = _write(tmp_path / "probe_ext.py", _TOOL_RESULT_EXT)
        session = _make_session()

        await session.load_extensions([str(ext)], discover=False)

        paths = [b.path for b in session._extension_runner._extensions if b.handlers]
        assert paths == [str(ext)]

    async def test_async_register_is_awaited(self, tmp_path):
        """An async register() is awaited by the loader — the hook still binds."""
        ext = _write(tmp_path / "aprobe.py", _ASYNC_TOOL_RESULT_EXT)
        session = _make_session()

        await session.load_extensions([str(ext)], discover=False)

        assert session._extension_runner.has_handlers("tool_result") is True


# ── error policy is the loader's: explicit raises, discovered collected ───────


class TestErrorPolicy:
    async def test_explicit_failure_raises(self, tmp_path):
        broken = _write(tmp_path / "broken.py", _BROKEN_EXT)
        session = _make_session()

        with pytest.raises(RuntimeError, match="boom"):
            await session.load_extensions([str(broken)], discover=False)

    async def test_discovered_failure_collected(self, tmp_path):
        """A broken *discovered* extension → errors[]; the good one still binds."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        _write(global_dir / "broken.py", _BROKEN_EXT)
        _write(global_dir / "good.py", _TOOL_RESULT_EXT)
        session = _make_session()

        result = await session.load_extensions(None, discover=True, user_dir=str(global_dir))

        assert len(result.extensions) == 1
        assert len(result.errors) == 1
        assert result.errors[0].path.endswith("broken.py")
        assert session._extension_runner.has_handlers("tool_result") is True

    async def test_discovery_off_loads_nothing(self, tmp_path):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        _write(global_dir / "good.py", _TOOL_RESULT_EXT)
        session = _make_session()

        result = await session.load_extensions(None, discover=False, user_dir=str(global_dir))

        assert result.extensions == []
        assert session._extension_runner.has_handlers("tool_result") is False
