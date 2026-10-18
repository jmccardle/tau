"""One script suite, run against every sandbox provider (experiments/sandbox-providers)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tau_agent_core.codemode.cpython import CPythonProvider
from tau_agent_core.codemode.invoker import DirectToolInvoker
from tau_agent_core.codemode.protocol import RunLimits, SandboxProvider
from tau_agent_core.tools.base import AgentTool, ToolDefinition, ToolError

try:
    import pydantic_monty  # noqa: F401

    HAVE_MONTY = True
except ImportError:
    HAVE_MONTY = False


def _tool(name: str, fn: Any, props: dict[str, Any], required: list[str]) -> AgentTool:
    async def execute(
        tool_call_id: str, args: dict[str, Any], signal: Any = None, on_update: Any = None
    ) -> Any:
        return await fn(**args)

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name,
            description=f"The {name} tool. Does {name} things.",
            parameters={"type": "object", "properties": props, "required": required},
            execute=execute,
        )
    )


async def _add(a: int, b: int) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": str(a + b)}]}


async def _slow(seconds: float) -> dict[str, Any]:
    await asyncio.sleep(seconds)
    return {"content": [{"type": "text", "text": f"slept {seconds}"}]}


async def _fail(reason: str) -> dict[str, Any]:
    raise ToolError(f"refused: {reason}")


async def _crash() -> dict[str, Any]:
    raise KeyError("internal")


TOOLS = [
    _tool("add", _add, {"a": {"type": "integer"}, "b": {"type": "integer"}}, ["a", "b"]),
    _tool("slow", _slow, {"seconds": {"type": "number"}}, ["seconds"]),
    _tool("fail", _fail, {"reason": {"type": "string"}}, ["reason"]),
    _tool("crash", _crash, {}, []),
    _tool("codemode", _crash, {}, []),
]

LIMITS = RunLimits(timeout_s=10.0, memory_bytes=512 * 1024 * 1024, max_calls=2000)

PROVIDERS = [
    "cpython",
    pytest.param("monty", marks=pytest.mark.skipif(not HAVE_MONTY, reason="no pydantic-monty")),
]


def _make(name: str, lifetime: str = "script") -> SandboxProvider:
    if name == "cpython":
        return CPythonProvider(lifetime)  # type: ignore[arg-type]
    from tau_agent_core.codemode.monty import MontyProvider

    return MontyProvider(lifetime)  # type: ignore[arg-type]


@pytest.fixture(params=PROVIDERS)
async def provider(request: pytest.FixtureRequest) -> AsyncIterator[SandboxProvider]:
    p = _make(request.param)
    await p.start()
    yield p
    await p.close()


@pytest.fixture(params=PROVIDERS)
async def session_provider(request: pytest.FixtureRequest) -> AsyncIterator[SandboxProvider]:
    p = _make(request.param, "session")
    await p.start()
    yield p
    await p.close()


def _invoker(**kw: Any) -> DirectToolInvoker:
    return DirectToolInvoker(TOOLS, **kw)


async def _run(p: SandboxProvider, code: str, limits: RunLimits = LIMITS, **kw: Any) -> Any:
    return await p.run(
        code, tools=kw.pop("tools", None) or _invoker(), limits=limits, scope="s1", **kw
    )


async def test_tool_call_and_print(provider: SandboxProvider) -> None:
    r = await _run(provider, "x = await tools.add(a=2, b=3)\nprint('sum', x)\nint(x) * 2")
    assert r.ok, r.error
    assert r.output == "sum 5\n"
    assert r.value == "10"
    assert [c.name for c in r.calls] == ["add"]


async def test_gather_runs_calls_concurrently(provider: SandboxProvider) -> None:
    code = "import asyncio\nr = await asyncio.gather(tools.slow(seconds=0.3), tools.slow(seconds=0.3))\nprint(r)"
    r = await _run(provider, code)
    assert r.ok, r.error
    assert "slept 0.3" in r.output
    assert len(r.calls) == 2
    assert r.duration_ms < 550


async def test_tool_error_text_reaches_script(provider: SandboxProvider) -> None:
    code = (
        "try:\n    await tools.fail(reason='no')\nexcept RuntimeError as e:\n    print('caught', e)\n"
        "await tools.fail(reason='twice')"
    )
    r = await _run(provider, code)
    assert "caught refused: no" in r.output
    assert r.error_kind == "script"
    assert "refused: twice" in (r.error or "")
    assert [c.ok for c in r.calls] == [False, False]


async def test_internal_tool_error_is_not_leaked(provider: SandboxProvider) -> None:
    r = await _run(provider, "await tools.crash()")
    assert r.error_kind == "script"
    assert "internal error" in (r.error or "")
    assert "KeyError" not in (r.error or "")


async def test_codemode_is_not_callable_from_a_script(provider: SandboxProvider) -> None:
    r = await _run(provider, "await tools.codemode()")
    assert r.error_kind == "script"
    assert not any(c.name == "codemode" and c.ok for c in r.calls)


async def test_bad_arguments_fail_validation(provider: SandboxProvider) -> None:
    r = await _run(provider, "await tools.add(a='x', b=1)")
    assert r.error_kind == "script"
    assert "Invalid arguments" in (r.error or "")


async def test_timeout_kills_the_run(provider: SandboxProvider) -> None:
    r = await _run(provider, "while True:\n    pass", RunLimits(timeout_s=0.5))
    assert r.error_kind == "timeout"
    assert r.duration_ms < 3000


async def test_timeout_covers_tool_time(provider: SandboxProvider) -> None:
    r = await _run(provider, "await tools.slow(seconds=5)", RunLimits(timeout_s=0.5))
    assert r.error_kind == "timeout"


async def test_memory_limit(provider: SandboxProvider) -> None:
    code = "x = ['a' * 1024] \nwhile True:\n    x = x + x"
    r = await _run(provider, code, RunLimits(memory_bytes=200 * 1024 * 1024, timeout_s=20))
    assert r.error_kind == "sandbox", (r.error_kind, r.error)
    assert r.limit == "memory"


async def test_call_budget(provider: SandboxProvider) -> None:
    code = "for i in range(10):\n    await tools.add(a=i, b=1)"
    r = await _run(provider, code, tools=_invoker(max_calls=3))
    assert r.error_kind == "sandbox"
    assert r.limit == "calls"


async def test_env_is_scrubbed(provider: SandboxProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAU_SECRET_PROBE", "leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    code = "import os\nprint(sorted(k for k in os.environ if 'TAU' in k or 'KEY' in k))"
    r = await _run(provider, code)
    assert "leak" not in r.output
    assert "TAU_SECRET_PROBE" not in r.output and "OPENAI_API_KEY" not in r.output


async def test_discovery(provider: SandboxProvider) -> None:
    code = "hits = await search_tools('add things')\nprint(hits[0]['name'])\nprint((await describe_tool('slow')).splitlines()[0])"
    r = await _run(provider, code)
    assert r.ok, r.error
    assert r.output.splitlines()[0] == "add"
    assert "async def slow(*, seconds: float) -> str" in r.output


async def test_script_lifetime_forgets(provider: SandboxProvider) -> None:
    await _run(provider, "kept = 41")
    r = await _run(provider, "kept + 1")
    assert r.error_kind == "script"
    assert "NameError" in (r.error or "")


async def test_session_lifetime_persists(session_provider: SandboxProvider) -> None:
    r1 = await _run(session_provider, "kept = 41\ndef inc(v):\n    return v + 1")
    assert r1.ok, r1.error
    r2 = await _run(session_provider, "inc(kept)")
    assert r2.ok, r2.error
    assert r2.value == "42"


async def test_session_resets_after_timeout(session_provider: SandboxProvider) -> None:
    await _run(session_provider, "kept = 1")
    r = await _run(session_provider, "while True:\n    pass", RunLimits(timeout_s=0.5))
    assert r.error_kind == "timeout" and r.reset
    r2 = await _run(session_provider, "kept")
    assert "NameError" in (r2.error or "")


async def test_session_scopes_are_separate(session_provider: SandboxProvider) -> None:
    await session_provider.run("kept = 1", tools=_invoker(), limits=LIMITS, scope="a")
    r = await session_provider.run("kept", tools=_invoker(), limits=LIMITS, scope="b")
    assert "NameError" in (r.error or "")


async def test_abort_signal(provider: SandboxProvider) -> None:
    class Signal:
        aborted = False

        def is_aborted(self) -> bool:
            return self.aborted

    signal = Signal()
    asyncio.get_running_loop().call_later(0.3, lambda: setattr(signal, "aborted", True))
    r = await _run(provider, "await tools.slow(seconds=5)", signal=signal)
    assert r.error_kind == "aborted"


async def test_syntax_error_is_a_script_error(provider: SandboxProvider) -> None:
    r = await _run(provider, "def (:")
    assert r.error_kind == "script"


@pytest.mark.parametrize("name", PROVIDERS)
async def test_turn_lifetime_resets_at_turn_end(name: str) -> None:
    from tau_agent_core.codemode.extension import CodeMode, CodeModeConfig

    mode = CodeMode(CodeModeConfig.from_mapping({"provider": name, "lifetime": "turn"}))
    try:
        r1 = await mode.run("kept = 7", TOOLS, cursor_id="c", call_id="1", signal=None, cwd=None)
        r2 = await mode.run("kept", TOOLS, cursor_id="c", call_id="2", signal=None, cwd=None)
        assert not r1["is_error"] and "[value] 7" in r2["content"][0]["text"]
        await mode.end_turn("c")
        r3 = await mode.run("kept", TOOLS, cursor_id="c", call_id="3", signal=None, cwd=None)
        assert r3["is_error"] and "NameError" in r3["content"][0]["text"]
    finally:
        await mode.close()


@pytest.mark.skipif(not HAVE_MONTY, reason="no pydantic-monty")
async def test_monty_type_check_fails_before_any_call() -> None:
    from tau_agent_core.codemode.monty import MontyProvider

    p = MontyProvider(type_check=True)
    await p.start()
    try:
        r = await _run(p, "await tools.add(a=1, b=1)\nawait tools.ad(a=1, b=2)")
        assert r.error_kind == "script"
        assert "type check failed" in (r.error or "")
        assert r.calls == []
        ok = await _run(p, "x = await tools.add(a=1, b=1)\nprint(x)")
        assert ok.ok, ok.error
    finally:
        await p.close()


def test_cpython_env_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    env = CPythonProvider().child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert not any(k.startswith("TAU_") for k in env)
    assert env.get("PATH") == os.environ.get("PATH")


# ── issues #32/#33: names that are not Python identifiers ────────────────────


async def _copy(**kwargs: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"{kwargs['from']}->{kwargs.get('my-arg')}"}]}


ODD = [
    _tool(
        "my-tool",
        _copy,
        {"from": {"type": "integer"}, "my-arg": {"type": "string"}},
        ["from"],
    ),
    _tool("from", _add, {"a": {"type": "integer"}, "b": {"type": "integer"}}, ["a", "b"]),
]


def test_the_prompt_keeps_a_non_identifier_parameter_and_names_the_stubbed_tool() -> None:
    from tau_agent_core.codemode.prompt import render_python_stubs, render_type_check_stubs

    stubs = DirectToolInvoker(ODD).stubs()
    text = render_python_stubs(stubs)
    assert "async def my_tool(**kwargs: Unpack[my_tool_arguments]) -> str:" in text
    assert '\'my_tool_arguments\', {"from": int, "my-arg": NotRequired[str]}' in text
    assert "async def from_(*, a: int, b: int) -> str:" in text
    namespace: dict[str, Any] = {}
    header = "from typing import Any, Literal\nfrom typing_extensions import NotRequired, TypedDict, Unpack\n"
    exec(header + text, namespace)
    assert namespace["my_tool_arguments"].__required_keys__ == {"from"}
    compile(render_type_check_stubs(stubs), "stubs.pyi", "exec")


async def test_a_script_calls_a_non_identifier_tool_by_the_name_its_stub_shows(
    provider: SandboxProvider,
) -> None:
    code = (
        'print(await tools.my_tool(**{"from": 2, "my-arg": "x"}))\n'
        "print(await tools.from_(a=1, b=2))"
    )
    r = await _run(provider, code, tools=DirectToolInvoker(ODD))
    assert r.ok, r.error
    assert r.output == "2->x\n3\n"
    assert [c.name for c in r.calls] == ["my-tool", "from"]


def test_discovery_answers_with_the_stub_name_and_takes_either() -> None:
    invoker = DirectToolInvoker(ODD)
    assert [h["name"] for h in invoker.search("my tool")][:1] == ["my_tool"]
    assert invoker.describe("my_tool") == invoker.describe("my-tool")
    assert "Unpack[my_tool_arguments]" in invoker.describe("my_tool")


def test_two_tools_sharing_a_stub_name_raise_rather_than_shadow() -> None:
    twin = _tool("my_tool", _add, {}, [])
    with pytest.raises(ValueError, match="'my-tool', 'my_tool' all stub as 'my_tool'"):
        DirectToolInvoker([*ODD, twin])


@pytest.mark.skipif(not HAVE_MONTY, reason="no pydantic-monty")
async def test_monty_type_checks_a_non_identifier_parameter() -> None:
    from tau_agent_core.codemode.monty import MontyProvider

    p = MontyProvider(type_check=True)
    await p.start()
    try:
        code = 'args: "my_tool_arguments" = {"from": 2}\nprint(await tools.my_tool(**args))'
        ok = await _run(p, code, tools=DirectToolInvoker(ODD))
        assert ok.ok, ok.error
        assert ok.output == "2->None\n"
    finally:
        await p.close()


def test_a_digit_leading_tool_stubs_with_a_letter_prefix_that_clashes_loudly() -> None:
    from tau_agent_core.tool_schema import stub_name, stub_names

    assert stub_name("1tool") == "t_1tool"
    assert stub_name("x²") == "x_"
    with pytest.raises(ValueError, match="'1tool', 't_1tool' all stub as 't_1tool'"):
        stub_names(["1tool", "t_1tool"])


async def test_both_runtimes_call_a_digit_leading_tool(provider: SandboxProvider) -> None:
    digit = _tool("1add", _add, {"a": {"type": "integer"}, "b": {"type": "integer"}}, ["a", "b"])
    r = await _run(
        provider, "print(await tools.t_1add(a=1, b=2))", tools=DirectToolInvoker([digit])
    )
    assert r.ok, r.error
    assert r.output == "3\n" and [c.name for c in r.calls] == ["1add"]


async def test_monty_refuses_a_tool_it_cannot_reach_and_cpython_calls_it() -> None:
    from tau_agent_core.codemode.extension import CodeMode, CodeModeConfig

    hidden = [_tool("_add", _add, {"a": {"type": "integer"}, "b": {"type": "integer"}}, ["a", "b"])]
    code = "print(await tools._add(a=1, b=2))"
    runner = _make("cpython")
    await runner.start()
    try:
        cpython = await _run(runner, code, tools=DirectToolInvoker(hidden))
    finally:
        await runner.close()
    assert cpython.ok and cpython.output == "3\n", cpython.error
    mode = CodeMode(CodeModeConfig.from_mapping({"provider": "monty"}))
    with pytest.raises(
        ValueError, match=r"cannot call '_add' \(as tools._add\).*starting with '_'"
    ):
        mode.section(hidden)
    if HAVE_MONTY:
        monty = _make("monty")
        await monty.start()
        try:
            with pytest.raises(ValueError, match="cannot call '_add'"):
                await _run(monty, code, tools=DirectToolInvoker(hidden))
        finally:
            await monty.close()
