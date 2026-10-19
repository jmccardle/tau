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


# ── issue #55: a tool whose stub fails is left out, visibly ──────────────────


def _bad_ref_extension(api: Any) -> None:
    async def execute(tool_call_id: str, params: Any, signal: Any, on_update: Any, ctx: Any) -> Any:
        return {"content": [{"type": "text", "text": "ran"}]}

    api.register_tool(
        {
            "name": "remote",
            "description": "A tool whose schema points outside itself.",
            "parameters": {
                "type": "object",
                "properties": {"spec": {"$ref": "other.json#/Spec"}},
            },
            "execute": execute,
        }
    )


BAD_REF_SCRIPT = """\
print(len(await tools.read(path="a.txt")))
try:
    await tools.remote(spec={})
except Exception as exc:
    print("refused:", exc)
"""


class _Notices:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def notify(self, message: str, level: str = "info") -> None:
        self.seen.append((message, level))


async def test_a_bad_ref_tool_is_left_out_with_a_notice_and_the_rest_still_work(
    tmp_path: Any,
) -> None:
    (tmp_path / "a.txt").write_text("abc")
    seen: list[Any] = []

    async def fake_stream_simple(model: Any, context: Any, options: Any = None) -> _Stream:
        seen.append(context)
        if len(seen) == 1:
            call = TauToolCall(id="c1", name="codemode", arguments={"code": BAD_REF_SCRIPT})
            return _Stream(_assistant([call], "toolUse"))
        return _Stream(_assistant([TextContent(text="done")], "stop"))

    session = create_agent_session(
        model="gpt-4o",
        tools=["read"],
        cwd=str(tmp_path),
        no_context_files=True,
        extensions=[_bad_ref_extension, lambda api: register(api, {"provider": "cpython"})],
    )
    notices = _Notices()
    session.set_ui_delegate(notices)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake_stream_simple):
        try:
            await session.prompt("go")
            await session.prompt("again")
        finally:
            await session.emit_session_shutdown()

    system = str(seen[0]["messages"][0]["content"])
    assert "async def read(*, path: str" in system
    assert "remote" not in system.split("Tools callable from scripts:")[1]
    assert str(seen[-1]["messages"][0]["content"]) == system
    assert notices.seen == [
        (
            "tool 'remote' is unavailable in code mode: $ref 'other.json#/Spec' "
            "is not a local '#/...' pointer",
            "warning",
        )
    ]

    def field(m: Any, key: str) -> Any:
        return m.get(key) if isinstance(m, dict) else getattr(m, key, None)

    results = [m for m in seen[1]["messages"] if field(m, "role") == "toolResult"]
    text = field(field(results[0], "content")[0], "text")
    assert text.startswith(
        "3\nrefused: tool 'remote' is unavailable in code mode: $ref 'other.json#/Spec' "
        "is not a local '#/...' pointer"
    ), text


LATE_SCRIPT = """\
try:
    await tools.later_bad()
except Exception as exc:
    print("refused:", exc)
"""


async def test_a_bad_ref_tool_declared_after_the_first_turn_is_refused_with_a_notice(
    tmp_path: Any,
) -> None:
    async def execute(tool_call_id: str, params: Any, signal: Any, on_update: Any, ctx: Any) -> Any:
        return {"content": [{"type": "text", "text": "ran"}]}

    def later(api: Any) -> None:
        api.register_tool(
            {
                "name": "later_bad",
                "description": "Joins late.",
                "parameters": {
                    "type": "object",
                    "properties": {"k": {"$ref": "#/$defs/Missing"}},
                },
                "execute": execute,
                "exposure": "deferred",
            }
        )

    seen: list[Any] = []
    script = iter([None, LATE_SCRIPT])

    async def fake_stream_simple(model: Any, context: Any, options: Any = None) -> _Stream:
        seen.append(context)
        last = context["messages"][-1]
        role = last.get("role") if isinstance(last, dict) else getattr(last, "role", None)
        code = next(script) if role == "user" else None
        if code is not None:
            call = TauToolCall(id=f"c{len(seen)}", name="codemode", arguments={"code": code})
            return _Stream(_assistant([call], "toolUse"))
        return _Stream(_assistant([TextContent(text="done")], "stop"))

    session = create_agent_session(
        model="gpt-4o",
        tools=["read"],
        cwd=str(tmp_path),
        no_context_files=True,
        extensions=[later, lambda api: register(api, {"provider": "cpython"})],
    )
    notices = _Notices()
    session.set_ui_delegate(notices)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake_stream_simple):
        try:
            await session.prompt("first")
            assert notices.seen == []
            await session.redeclare_tools({"later_bad": True})
            await session.prompt("second")
        finally:
            await session.emit_session_shutdown()

    notice = (
        "tool 'later_bad' is unavailable in code mode: $ref '#/$defs/Missing' "
        "does not resolve (no '$defs' in the schema)"
    )
    assert notices.seen == [(notice, "warning")]

    def field(m: Any, key: str) -> Any:
        return m.get(key) if isinstance(m, dict) else getattr(m, key, None)

    results = [m for m in seen[-1]["messages"] if field(m, "role") == "toolResult"]
    assert field(field(results[-1], "content")[0], "text").startswith(f"refused: {notice}")


@pytest.mark.parametrize("head", ["stderr", "json"])
async def test_a_late_exclusion_notice_reaches_a_headless_head_not_captured_output(
    tmp_path: Any, capfd: Any, head: str
) -> None:
    import json

    async def execute(tool_call_id: str, params: Any, signal: Any, on_update: Any, ctx: Any) -> Any:
        return {"content": [{"type": "text", "text": "ran"}]}

    def later(api: Any) -> None:
        api.register_tool(
            {
                "name": "later_bad",
                "description": "Joins late.",
                "parameters": {"type": "object", "properties": {"k": {"$ref": "#/$defs/Missing"}}},
                "execute": execute,
                "exposure": "deferred",
            }
        )

    seen: list[Any] = []
    script = iter([None, LATE_SCRIPT])

    async def fake_stream_simple(model: Any, context: Any, options: Any = None) -> _Stream:
        seen.append(context)
        last = context["messages"][-1]
        role = last.get("role") if isinstance(last, dict) else getattr(last, "role", None)
        code = next(script) if role == "user" else None
        if code is not None:
            call = TauToolCall(id=f"c{len(seen)}", name="codemode", arguments={"code": code})
            return _Stream(_assistant([call], "toolUse"))
        return _Stream(_assistant([TextContent(text="done")], "stop"))

    session = create_agent_session(
        model="gpt-4o",
        tools=["read"],
        cwd=str(tmp_path),
        no_context_files=True,
        extensions=[later, lambda api: register(api, {"provider": "cpython"})],
    )
    if head == "json":
        session.set_extension_record_sink(lambda r: print(json.dumps(r), flush=True))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake_stream_simple):
        try:
            await session.prompt("first")
            await session.redeclare_tools({"later_bad": True})
            await session.prompt("second")
        finally:
            await session.emit_session_shutdown()

    notice = (
        "tool 'later_bad' is unavailable in code mode: $ref '#/$defs/Missing' "
        "does not resolve (no '$defs' in the schema)"
    )
    out = capfd.readouterr()
    if head == "stderr":
        assert f"[τ] warning: {notice}" in out.err
    else:
        records = [json.loads(line) for line in out.out.splitlines() if line.startswith("{")]
        assert any(r.get("kind") == "notify" and r.get("message") == notice for r in records)

    def field(m: Any, key: str) -> Any:
        return m.get(key) if isinstance(m, dict) else getattr(m, key, None)

    results = [m for m in seen[-1]["messages"] if field(m, "role") == "toolResult"]
    details = field(results[-1], "details") or {}
    assert notice not in json.dumps(details.get("captured_output", {}))
