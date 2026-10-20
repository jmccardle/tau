"""``tau -p --mode json`` stays JSON when an extension tool prints (experimental).

Without the per-call capture in ``tau_agent_core.tool_output``, the ``print`` in
the tool below lands on stdout between two event lines. The events of its nested
``ls`` call are written by the head from inside the tool's context, and must not
be captured with the print (experiments/tool-exposure/RESULTS.md §5).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, TextContent, ToolCall, Usage
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import run_print

_NOISY = """
def register(api):
    async def noisy(tool_call_id, params, signal, on_update, ctx):
        print("debug: noisy tool ran")
        await ctx.execute_tool("ls", {})
        return {"content": [{"type": "text", "text": "ok"}]}

    api.register_tool({
        "name": "noisy",
        "description": "prints a debug line",
        "parameters": {"type": "object", "properties": {}},
        "execute": noisy,
    })
"""


def _assistant(content: list[Any], stop: str) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="qwen",
        stop_reason=stop,  # type: ignore[arg-type]
        timestamp=1_700_000_000_000,
        usage=Usage(),
    )


class _Stream:
    def __init__(self, final: AssistantMessage) -> None:
        self.final = final

    def __aiter__(self) -> "_Stream":
        self._done = False
        return self

    async def __anext__(self) -> Any:
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return DoneEvent(final=self.final, usage=Usage())

    async def result(self) -> AssistantMessage:
        return self.final

    def abort(self) -> None:
        pass


def _fake(model: Any, context: Any, options: Any = None) -> _Stream:
    messages = [m for m in context.get("messages", []) if isinstance(m, dict)]
    if any(m.get("role") == "toolResult" for m in messages):
        return _Stream(_assistant([TextContent(text="done")], "stop"))
    return _Stream(_assistant([ToolCall(id="c1", name="noisy", arguments={})], "toolUse"))


async def test_a_printing_tool_does_not_break_the_jsonl_stream(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TAU_HOME", str(tmp_path))
    ext = tmp_path / "noisy.py"
    ext.write_text(_NOISY)
    config = {
        "models": {
            "m": {
                "backend": "openai",
                "model": "qwen",
                "base_url": "http://localhost:9/v1",
                "api_key": "not-needed",
                "tools": ["ls"],
            }
        },
        "default_model": "m",
        "system_prompt": "s",
    }
    args = CLIArgs(
        messages=["go"], print_mode=True, mode="json", extensions=[str(ext)], no_extensions=True
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
        assert await run_print(args, config) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    tools = [
        (r["type"], r["tool_name"], r.get("parent_tool_call_id")) for r in records if "tool_name" in r
    ]
    assert tools == [
        ("tool_execution_start", "noisy", None),
        ("tool_execution_start", "ls", "c1"),
        ("tool_execution_end", "ls", "c1"),
        ("tool_execution_end", "noisy", None),
    ]
    end = next(r for r in records if r["type"] == "tool_execution_end" and r["tool_name"] == "noisy")
    assert end["details"]["captured_output"] == {"stdout": "debug: noisy tool ran\n"}
