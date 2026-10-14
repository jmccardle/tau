"""The ``codemode`` tool inside a real AgentSession, and its prompt rendering."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest
from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, TextContent, Usage
from tau_llm.types import ToolCall as TauToolCall

from tau_agent_core.codemode.extension import CodeModeConfig, format_result, register
from tau_agent_core.codemode.prompt import (
    CPYTHON_TEMPLATES,
    MONTY_TEMPLATES,
    PromptFieldError,
    merge_templates,
    render_prompt,
    render_type_check_stubs,
    render_typescript_stubs,
)
from tau_agent_core.codemode.protocol import RunLimits, RunResult, ToolStub
from tau_agent_core.sdk import create_agent_session

try:
    import pydantic_monty  # noqa: F401

    HAVE_MONTY = True
except ImportError:
    HAVE_MONTY = False

_TS = 1_700_000_000_000

READ = ToolStub(
    "read",
    "Read the contents of a file. Supports text and images.",
    {
        "type": "object",
        "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
        "required": ["path"],
    },
)


def _assistant(blocks: list[Any], stop_reason: str) -> AssistantMessage:
    return AssistantMessage(
        content=blocks,
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=_TS,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    def __init__(self, final: AssistantMessage) -> None:
        self._events = [DoneEvent(final=final, usage=final.usage)]

    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for e in self._events:
                yield e

        return _gen()

    async def result(self) -> AssistantMessage:
        return self._events[0].final

    def abort(self) -> None:
        pass


SCRIPT = """\
import asyncio
a, b = await asyncio.gather(tools.read(path="a.txt"), tools.read(path="b.txt"))
listing = await tools.ls(path=".")
print(len(a.splitlines()), len(b.splitlines()), "a.txt" in listing)
"""


@pytest.mark.parametrize(
    "provider",
    ["cpython", pytest.param("monty", marks=pytest.mark.skipif(not HAVE_MONTY, reason="no monty"))],
)
async def test_codemode_turn_end_to_end(tmp_path: Any, provider: str) -> None:
    (tmp_path / "a.txt").write_text("1\n2\n3\n")
    (tmp_path / "b.txt").write_text("x\n")
    seen: list[Any] = []

    async def fake_stream_simple(model: Any, context: Any, options: Any = None) -> _Stream:
        seen.append(context)
        if len(seen) == 1:
            call = TauToolCall(id="c1", name="codemode", arguments={"code": SCRIPT})
            return _Stream(_assistant([call], "toolUse"))
        return _Stream(_assistant([TextContent(text="done")], "stop"))

    session = create_agent_session(
        model="gpt-4o",
        tools=["read", "ls", "bash"],
        cwd=str(tmp_path),
        no_context_files=True,
        extensions=[lambda api: register(api, {"provider": provider})],
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake_stream_simple):
        try:
            await session.prompt("count lines")
            await session.prompt("again")
        finally:
            await session.emit_session_shutdown()

    def system(ctx: dict[str, Any]) -> str:
        head = ctx["messages"][0]
        assert head["role"] == "system"
        return str(head["content"])

    first = system(seen[0])
    assert "## Code mode" in first
    assert "async def read(*, path: str" in first
    assert "codemode" in [t.name for t in seen[0]["tools"]]
    assert first.count("## Code mode") == 1
    assert system(seen[2]) == first

    def field(m: Any, key: str) -> Any:
        return m.get(key) if isinstance(m, dict) else getattr(m, key, None)

    results = [m for m in seen[1]["messages"] if field(m, "role") == "toolResult"]
    block = field(results[0], "content")[0]
    text = field(block, "text")
    assert text.startswith("3 1 True"), text
    assert "[3 tool calls: read×2, ls×1]" in text


def test_prompt_renders_per_provider_and_lifetime() -> None:
    limits = RunLimits()
    for templates in (CPYTHON_TEMPLATES, MONTY_TEMPLATES):
        sections = {
            lt: render_prompt(
                templates, provider="p", language="python", lifetime=lt, limits=limits, stubs=[READ]
            )
            for lt in ("script", "turn", "session")
        }
        assert len({s for s, _ in sections.values()}) == 3
        assert "fresh interpreter" in sections["script"][0]
        assert "REPL" in sections["session"][0]
    _, monty_desc = render_prompt(
        MONTY_TEMPLATES,
        provider="monty",
        language="python",
        lifetime="script",
        limits=limits,
        stubs=None,
    )
    assert "Monty" in monty_desc
    assert "read" not in monty_desc.split("`")[1:2][0] or "tools.read" in monty_desc


def test_description_is_independent_of_tools() -> None:
    a = render_prompt(
        CPYTHON_TEMPLATES,
        provider="c",
        language="python",
        lifetime="script",
        limits=RunLimits(),
        stubs=[READ],
    )
    b = render_prompt(
        CPYTHON_TEMPLATES,
        provider="c",
        language="python",
        lifetime="script",
        limits=RunLimits(),
        stubs=[],
    )
    assert a[1] == b[1]


def test_config_overrides_and_unknown_fields() -> None:
    merged = merge_templates(
        CPYTHON_TEMPLATES, {"lifetime": {"session": "Kernel {{provider}} persists."}}
    )
    section, _ = render_prompt(
        merged,
        provider="cpython",
        language="python",
        lifetime="session",
        limits=RunLimits(),
        stubs=None,
    )
    assert "Kernel cpython persists." in section
    bad = merge_templates(CPYTHON_TEMPLATES, {"system": "{{nope}}"})
    with pytest.raises(PromptFieldError):
        render_prompt(
            bad, provider="c", language="python", lifetime="script", limits=RunLimits(), stubs=None
        )
    with pytest.raises(ValueError):
        merge_templates(CPYTHON_TEMPLATES, {"sistem": "x"})
    with pytest.raises(ValueError):
        CodeModeConfig.from_mapping({"provider": "v8"})
    with pytest.raises(ValueError):
        CodeModeConfig.from_mapping({"lifetimes": "session"})


def test_typescript_and_type_check_stubs() -> None:
    ts = render_typescript_stubs([READ])
    assert "declare const tools: {" in ts
    assert "read(args: { path: string; offset?: number }): Promise<string>;" in ts
    pyi = render_type_check_stubs([READ])
    assert "async def read(self, *, path: str, offset: int | None = None) -> str: ..." in pyi
    compile(pyi, "stubs.pyi", "exec")


def test_output_cap_writes_full_text() -> None:
    result = RunResult(output="x" * 100)
    text, path = format_result(result, RunLimits(output_chars=10))
    assert path is not None and os.path.exists(path)
    assert text.startswith("x" * 10 + "\n[output cut at 10 of 100 characters")
    with open(path) as fh:
        assert fh.read() == "x" * 100
    os.unlink(path)


async def test_typescript_provider_refuses_to_run(tmp_path: Any) -> None:
    from tau_agent_core.codemode.extension import CodeMode

    mode = CodeMode(CodeModeConfig.from_mapping({"provider": "typescript"}))
    assert "declare const tools" in mode.section([])
    out = await mode.run(
        "console.log(1)", [], cursor_id="c", call_id="x", signal=None, cwd=str(tmp_path)
    )
    assert out["is_error"] and "no runtime" in out["content"][0]["text"]
