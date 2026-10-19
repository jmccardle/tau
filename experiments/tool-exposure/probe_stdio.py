"""Probe: what a tool's stdio does to a head's protocol stream.

Two measurements, each run with and without τ's per-call capture:

1. ``print`` inside an extension tool under ``tau -p --mode json``. The real
   ``run_print`` path runs with only ``stream_simple`` faked; the probe counts
   stdout lines that are not JSON.
2. ``bash`` running ``cat`` while the parent's stdin is a pipe carrying a
   protocol line, the way ``tau --mode rpc`` is fed. The probe reports whether
   the child read the line.

And one cost: the overhead capture adds to a tool call that prints nothing, and
to one that prints 1000 lines.

Run from the repo root: ``venv/bin/python experiments/tool-exposure/probe_stdio.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, TextContent, ToolCall, Usage

NOISY_EXTENSION = """
def register(api):
    async def noisy(tool_call_id, params, signal, on_update, ctx):
        print("debug: noisy tool ran")
        return {"content": [{"type": "text", "text": "ok"}]}

    api.register_tool({
        "name": "noisy",
        "description": "prints a debug line",
        "parameters": {"type": "object", "properties": {}},
        "execute": noisy,
    })
"""


def _assistant(content, stop):
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="qwen",
        stop_reason=stop,
        timestamp=1_700_000_000_000,
        usage=Usage(),
    )


class _Stream:
    def __init__(self, final):
        self.final = final

    def __aiter__(self):
        self._done = False
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return DoneEvent(final=self.final, usage=Usage())

    async def result(self):
        return self.final

    def abort(self):
        pass


def _fake(model, context, options=None):
    seen = any(
        isinstance(m, dict) and m.get("role") in ("tool", "toolResult")
        for m in context.get("messages", [])
    )
    if seen:
        return _Stream(_assistant([TextContent(text="done")], "stop"))
    return _Stream(_assistant([ToolCall(id="c1", name="noisy", arguments={})], "toolUse"))


def json_stream_probe(capture: bool) -> dict:
    """Run ``tau -p --mode json`` once with the noisy tool; count non-JSON stdout lines."""
    from tau_coding_agent.cli import CLIArgs
    from tau_coding_agent.headless import run_print

    import tau_agent_core.tool_pipeline as pipeline

    with tempfile.TemporaryDirectory() as tmp:
        ext = Path(tmp) / "noisy.py"
        ext.write_text(NOISY_EXTENSION)
        config = {
            "models": {
                "m": {
                    "backend": "openai",
                    "model": "qwen",
                    "base_url": "http://localhost:9/v1",
                    "api_key": "not-needed",
                    "tools": [],
                }
            },
            "default_model": "m",
            "system_prompt": "s",
        }
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(tmp)
        disable = (
            contextlib.nullcontext()
            if capture
            else patch.object(
                pipeline, "capture_tool_output", lambda: contextlib.nullcontext(_NullSink())
            )
        )
        try:
            with (
                patch.dict(os.environ, {"TAU_HOME": tmp}),
                patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake),
                disable,
                contextlib.redirect_stdout(out),
            ):
                rc = asyncio.run(
                    run_print(
                        CLIArgs(
                            messages=["go"],
                            print_mode=True,
                            mode="json",
                            extensions=[str(ext)],
                            no_extensions=True,
                        ),
                        config,
                    )
                )
        finally:
            os.chdir(cwd)
    bad = []
    lines = out.getvalue().splitlines()
    for line in lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad.append(line)
    return {"capture": capture, "rc": rc, "lines": len(lines), "non_json_lines": bad}


class _NullSink:
    def as_details(self):
        return None


CHILD = r"""
import asyncio, json, sys
from tau_agent_core.tools.bash import BashTool
out = asyncio.run(BashTool().execute("c", {"command": "cat", "timeout": 1500}))
sys.stderr.write(json.dumps(out["content"][0]["text"]))
"""


def stdin_probe() -> dict:
    """Feed a child τ process a protocol line on stdin while ``bash`` runs ``cat``."""
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    proc.stdin.write(b'{"type": "prompt", "message": "the next RPC command"}\n')
    proc.stdin.flush()
    time.sleep(2.5)
    proc.stdin.close()
    assert proc.stderr is not None
    err = proc.stderr.read()
    proc.wait(timeout=10)
    text = json.loads(err.decode().strip().splitlines()[-1])
    return {"bash_output": text, "stole_protocol_line": "next RPC command" in text}


def overhead_probe() -> dict:
    """Per-call cost of the capture context: no prints, and 1000 prints."""
    from tau_agent_core.tool_output import _ContextStream, capture_tool_output

    def quiet():
        pass

    def loud():
        for _ in range(1000):
            print("x" * 40)

    results = {}
    for name, body, n in (("quiet", quiet, 200_000), ("print_1000_lines", loud, 200)):
        start = time.perf_counter()
        for _ in range(n):
            with capture_tool_output():
                body()
        captured = (time.perf_counter() - start) / n
        sink = io.StringIO()
        start = time.perf_counter()
        with contextlib.redirect_stdout(sink):
            for _ in range(n):
                body()
        plain = (time.perf_counter() - start) / n
        proxied_sink = io.StringIO()
        saved = sys.stdout
        sys.stdout = _ContextStream(proxied_sink, "stdout")
        try:
            start = time.perf_counter()
            for _ in range(n):
                body()
            proxied = (time.perf_counter() - start) / n
        finally:
            sys.stdout = saved
        results[name] = {
            "with_capture_us": round(captured * 1e6, 2),
            "through_proxy_outside_any_tool_us": round(proxied * 1e6, 2),
            "plain_us": round(plain * 1e6, 2),
        }
    return results


if __name__ == "__main__":
    report = {
        "python": sys.version.split()[0],
        "json_mode_without_capture": json_stream_probe(capture=False),
        "json_mode_with_capture": json_stream_probe(capture=True),
        "bash_stdin": stdin_probe(),
        "overhead": overhead_probe(),
    }
    print(json.dumps(report, indent=2), file=sys.__stderr__)
