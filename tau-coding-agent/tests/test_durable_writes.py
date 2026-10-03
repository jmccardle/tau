"""A turn killed with SIGKILL leaves what it finished on disk (docs/TAU-SERVE.md §4).

The child process runs one turn on the file store: the model calls a tool, the
tool answers, and the second completion streams one delta and then hangs. The
parent kills the child once that completion's entry is on disk, then reopens the
file.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tau_agent_core.conversation_tree import ConversationTree, IncompleteEntryError
from tau_agent_core.session_log import default_leaf, is_incomplete

from tau_coding_agent.session_store import Session

_CHILD = r'''
import asyncio, sys
from unittest.mock import patch
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.tools.base import AgentTool, ToolDefinition
from tau_coding_agent.session_store import Session

model = Model(id="m", provider="openai", api="openai-completions",
              base_url="http://127.0.0.1:1/v1", name="m", context_window=8192, max_tokens=256)

def reply(content):
    return AssistantMessage(content=content, api="openai-completions", provider="openai",
                            model="m", stop_reason="stop", timestamp=1_700_000_000_000,
                            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2))

class Calls:
    def __aiter__(self):
        async def gen():
            final = reply([ToolCall(id="c1", name="echo", arguments={"text": "kept"})])
            yield DoneEvent(final=final, usage=final.usage)
        return gen()
    def abort(self): pass

class Hangs:
    def __aiter__(self):
        async def gen():
            yield TextDeltaEvent(delta="half an ans", partial=reply([TextContent(text="half an ans")]))
            print("streaming", flush=True)
            await asyncio.Event().wait()
        return gen()
    def abort(self): pass

streams = iter([Calls(), Hangs()])

async def fake(model, context, options=None):
    return next(streams)

async def echo(tool_call_id, args, signal=None):
    return args["text"]

tool = AgentTool(definition=ToolDefinition(
    name="echo", label="Echo", description="echo", execute=echo, execution_mode="parallel",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}}))

async def main():
    log = Session.load(__import__("pathlib").Path(sys.argv[1]))
    session = AgentSession(session_log=log, model=model, tools=[tool])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("please echo")

asyncio.run(main())
'''


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_sigkill_mid_turn_keeps_the_prompt_and_the_tool_result(tmp_path: Path) -> None:
    log = Session.create(str(tmp_path), "m", "openai", base_dir=tmp_path / "s")
    assert log.path is not None
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(log.path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        assert child.stdout is not None
        line = child.stdout.readline()
        assert line.strip() == "streaming", child.stderr.read() if child.stderr else line
        deadline = time.monotonic() + 10
        while not any(is_incomplete(e) for e in _lines(log.path)[1:]):
            assert time.monotonic() < deadline, "the open entry never reached the disk"
            time.sleep(0.02)
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait()

    entries = Session.load(log.path).entries()
    roles = [e["message"]["role"] for e in entries if e["type"] == "message"]
    assert roles == ["user", "assistant", "toolResult", "assistant"]
    interrupted = entries[-1]
    assert is_incomplete(interrupted), "the streaming completion is marked, not made up"
    tool_result = next(e for e in entries if e.get("message", {}).get("role") == "toolResult")
    assert tool_result["message"]["content"][0]["text"] == "kept"
    assert default_leaf(entries) == tool_result["id"], "a reopen continues before the break"
    with pytest.raises(IncompleteEntryError):
        ConversationTree(entries, interrupted["id"]).context_for()
