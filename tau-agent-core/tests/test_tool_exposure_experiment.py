"""The tool-exposure experiment (experiments/tool-exposure/RESULTS.md).

Nested tool calls through the pipeline, discovery, exposure recorded in the
tree, typed returns, and per-call output capture. Each session test runs the
full loop with only ``stream_simple`` faked; the fake answers from a script
keyed on what the model has already seen, and records the tool names each
completion was offered.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from typing import Any, Callable
from unittest.mock import patch

import pydantic
import pytest
from pydantic import TypeAdapter
from tau_llm.streaming import DoneEvent
from typing_extensions import TypedDict
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.cursor import Cursor
from tau_agent_core.events import AgentEvent
from tau_agent_core.sdk import _resolve_tools
from tau_agent_core.session_log import InMemorySessionLog, config_at
from tau_agent_core.prompt_cache import tool_block_change
from tau_agent_core.tool_dispatch import call_tool_definition, tool_search_definition
from tau_agent_core.tool_output import MAX_CAPTURED_CHARS, CapturedOutput, capture_tool_output
from tau_agent_core.tool_pipeline import (
    MAX_NESTING_DEPTH,
    NestedCallError,
    ToolCallBlocked,
)
from tau_agent_core.tool_schema import (
    output_schema_of,
    render_stub,
    render_stubs,
    tool_from_function,
)
from tau_agent_core.tools.bash import BashResult, BashTool
from tau_agent_core.tools.base import ToolError
from tau_agent_core.tools.grep import GrepResult, GrepTool

_TS = 1_700_000_000_000

_MODEL = Model(
    id="gpt-4o",
    name="GPT-4o",
    api="openai-completions",
    provider="openai",
    base_url="https://api.openai.com/v1",
    context_window=128000,
    max_tokens=4096,
)


def _assistant(content: list[Any], stop: str) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason=stop,  # type: ignore[arg-type]
        timestamp=_TS,
        usage=Usage(),
    )


class _Stream:
    def __init__(self, final: AssistantMessage) -> None:
        self._events = [DoneEvent(final=final, usage=Usage())]

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        self._i += 1
        return self._events[self._i - 1]

    async def result(self) -> Any:
        return self._events[0].final

    def abort(self) -> None:
        pass


class ScriptedModel:
    """Fake ``stream_simple``: completion N issues ``script[N]``'s tool calls, then text.

    ``offered`` records the tool names each completion was declared.
    """

    def __init__(self, *script: list[tuple[str, str, dict[str, Any]]]) -> None:
        self.script = list(script)
        self.offered: list[list[str]] = []
        self.blocks: list[str] = []
        self.contexts: list[list[Any]] = []

    def __call__(self, model: Any, context: Any, options: Any = None) -> _Stream:
        tools = context.get("tools") or []
        self.offered.append([t.name for t in tools])
        self.blocks.append(
            json.dumps([[t.name, t.description, t.parameters] for t in tools], sort_keys=True)
        )
        self.contexts.append(list(context.get("messages", [])))
        n = len(self.offered) - 1
        if n < len(self.script):
            calls = [
                ToolCall(type="toolCall", id=cid, name=name, arguments=args)
                for cid, name, args in self.script[n]
            ]
            return _Stream(_assistant(calls, "toolUse"))  # type: ignore[arg-type]
        return _Stream(_assistant([TextContent(text="done")], "stop"))


def _session(tmp_path: Any, *extensions: Callable[..., Any], log: Any = None) -> AgentSession:
    return AgentSession(
        session_log=log or InMemorySessionLog(),
        model=_MODEL,
        tools=_resolve_tools(["read", "bash", "grep", "ls"], cwd=str(tmp_path)),
        extensions=list(extensions),
        cwd=str(tmp_path),
    )


def _results(messages: list[Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "toolResult":
            out[m["tool_call_id"]] = m
    return out


def _text(message: dict[str, Any]) -> str:
    return "".join(b.get("text", "") for b in message["content"])


async def _prompt(session: AgentSession, model: ScriptedModel, text: str = "go") -> list[Any]:
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=model):
        return await session.prompt(text)


def _tool(name: str, execute: Any, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"the {name} tool",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "execute": execute,
        **extra,
    }


# ── typed returns ────────────────────────────────────────────────────────────


async def test_bash_returns_a_structured_value_that_fits_its_declared_schema(tmp_path):
    out = await BashTool(cwd=str(tmp_path)).execute(
        "c1", {"command": "echo hi; echo err >&2; exit 3"}
    )
    value = TypeAdapter(BashResult).validate_python(out["value"])
    assert value["exit_code"] == 3
    assert value["stdout"] == "hi\n"
    assert value["stderr"] == "err\n"
    assert out["content"][0]["text"] == value["output"]
    assert out["is_error"] is True
    schema = _resolve_tools(["bash"])[0].output_schema
    assert schema is not None and set(schema["properties"]) == set(BashResult.__annotations__)


async def test_grep_returns_matches_as_data_and_the_model_text_is_unchanged(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n# TODO one\n")
    (tmp_path / "b.py").write_text("# TODO two\n")
    out = await GrepTool(cwd=str(tmp_path)).execute("c1", {"pattern": "TODO"})
    value = TypeAdapter(GrepResult).validate_python(out["value"])
    assert sorted((m["path"], m["line"]) for m in value["matches"]) == [("a.py", 2), ("b.py", 1)]
    assert sorted(out["content"][0]["text"].splitlines()) == [
        "a.py:2:# TODO one",
        "b.py:1:# TODO two",
    ]
    assert out["details"] == {"matches": 2, "files_searched": 2}


class _Count(TypedDict):
    words: int
    lines: int


@dataclasses.dataclass
class _Point:
    x: int
    y: int


class _Model(pydantic.BaseModel):
    name: str
    tags: list[str]


@pytest.mark.parametrize(
    ("returns", "expected_type"),
    [
        (_Count, "object"),
        (_Point, "object"),
        (_Model, "object"),
        (int, "integer"),
        (list[str], "array"),
    ],
)
def test_output_schema_derives_from_any_return_annotation(returns, expected_type):
    def fn() -> None: ...

    fn.__annotations__ = {"return": returns}
    schema = output_schema_of(fn)
    assert schema is not None and schema["type"] == expected_type


def test_no_return_annotation_is_no_output_schema():
    def fn():  # noqa: ANN202
        return 1

    assert output_schema_of(fn) is None


async def test_tool_from_function_derives_parameters_and_output_and_injects_ctx():
    seen: list[Any] = []

    async def word_count(text: str, ctx: Any, min_len: int = 0) -> _Count:
        """Count words longer than min_len.

        More prose the model never reads.
        """
        seen.append(ctx)
        return {"words": len([w for w in text.split() if len(w) > min_len]), "lines": 1}

    spec = tool_from_function(word_count, tags=["text"])
    assert spec["description"] == "Count words longer than min_len."
    assert set(spec["parameters"]["properties"]) == {"text", "min_len"}
    assert spec["parameters"]["required"] == ["text"]
    assert spec["output_schema"]["title"] == "_Count"
    out = await spec["execute"]("c1", {"text": "a bb ccc", "min_len": 1}, None, None, "CTX")
    assert out["value"] == {"words": 2, "lines": 1}
    assert out["content"][0]["text"] == '{"words": 2, "lines": 1}'
    assert seen == ["CTX"]


def test_tool_from_function_refuses_what_it_cannot_describe():
    def undocumented(x: int) -> int:
        return x

    def unannotated(x) -> int:  # noqa: ANN001
        return 1

    def no_return(x: int):  # noqa: ANN202
        return x

    with pytest.raises(ValueError, match="no description"):
        tool_from_function(undocumented)
    with pytest.raises(TypeError, match="unannotated"):
        tool_from_function(unannotated, description="d")
    with pytest.raises(TypeError, match="return annotation is required"):
        tool_from_function(no_return, description="d")


def test_a_typing_typeddict_is_refused_on_3_11_with_the_reason():
    import typing

    class Plain(typing.TypedDict):
        a: int

    def fn() -> None: ...

    fn.__annotations__ = {"return": Plain}
    if sys.version_info >= (3, 12):
        assert output_schema_of(fn) is not None
    else:
        with pytest.raises(TypeError, match="typing_extensions"):
            output_schema_of(fn)


def test_grep_renders_as_a_python_stub_that_executes():
    grep = _resolve_tools(["grep"])[0].definition
    stub = render_stub(grep.name, grep.description, grep.parameters, grep.output_schema)
    assert (
        "async def grep(pattern: str, path: str | None = None, files: list[str] | None = None, "
        "ignore_case: bool | None = None) -> GrepResult:"
    ) in stub
    assert stub.index("class GrepMatch(TypedDict)") < stub.index("class GrepResult(TypedDict)")
    namespace: dict[str, Any] = {}
    exec(render_stubs([grep, _resolve_tools(["bash"])[0].definition]), namespace)
    assert "grep" in namespace and "BashResult" in namespace


def test_a_text_only_tool_stubs_as_returning_str():
    read = _resolve_tools(["read"])[0].definition
    assert (
        render_stub(read.name, read.description, read.parameters, None)
        .splitlines()[0]
        .endswith("-> str:")
    )


# ── nested calls ─────────────────────────────────────────────────────────────


def _outer_calling(name: str, args: dict[str, Any], sink: list[Any]) -> Callable[..., Any]:
    async def execute(tool_call_id, params, signal, on_update, ctx):
        try:
            sink.append(await ctx.execute_tool(name, args))
        except ToolError as exc:
            sink.append(exc)
            raise
        return {"content": [{"type": "text", "text": "outer done"}]}

    return execute


async def test_a_nested_call_runs_through_the_pipeline_and_is_recorded_on_its_parent(tmp_path):
    (tmp_path / "f.txt").write_text("needle here\n")
    got: list[Any] = []
    calls: list[dict[str, Any]] = []

    def ext(api):
        api.register_tool(_tool("outer", _outer_calling("grep", {"pattern": "needle"}, got)))
        api.on("tool_call", lambda event, ctx: calls.append(dict(event)))

    session = _session(tmp_path, ext)
    events: list[AgentEvent] = []
    session.subscribe(events.append)
    messages = await _prompt(session, ScriptedModel([("call_1", "outer", {})]))

    nested = got[0]
    assert nested.tool_call_id == "call_1/1"
    assert nested.as_type(GrepResult)["matches"][0]["path"] == "f.txt"
    assert [c["tool_name"] for c in calls] == ["outer", "grep"]
    assert "parent_tool_call_id" not in calls[0]
    assert calls[1]["parent_tool_call_id"] == "call_1"
    record = _results(messages)["call_1"]["details"]["nested_calls"]
    assert [(r["tool_call_id"], r["tool_name"], r["status"], r["args"]) for r in record] == [
        ("call_1/1", "grep", "ok", {"pattern": "needle"})
    ]
    assert "call_1/1" not in _results(messages)
    starts = [
        (e.tool_call_id, e.parent_tool_call_id) for e in events if e.type == "tool_execution_start"
    ]
    assert starts == [("call_1", None), ("call_1/1", "call_1")]


async def test_a_vetoed_nested_call_raises_tool_call_blocked_in_the_caller(tmp_path):
    got: list[Any] = []

    def ext(api):
        api.register_tool(_tool("outer", _outer_calling("bash", {"command": "rm -rf /"}, got)))
        api.on(
            "tool_call",
            lambda event, ctx: (
                {"block": True, "reason": "no bash from tools"}
                if event["tool_name"] == "bash"
                else None
            ),
        )

    session = _session(tmp_path, ext)
    messages = await _prompt(session, ScriptedModel([("call_1", "outer", {})]))
    assert isinstance(got[0], ToolCallBlocked) and got[0].blocked_by.endswith("ext")
    outer = _results(messages)["call_1"]
    assert outer["is_error"] and _text(outer) == "no bash from tools"
    assert outer["details"]["nested_calls"][0]["status"] == "blocked"


@pytest.mark.parametrize("exposure", ["model-only", "hidden"])
async def test_a_tool_closed_to_nested_calls_is_unavailable(tmp_path, exposure):
    got: list[Any] = []

    def ext(api):
        api.register_tool(_tool("outer", _outer_calling("inner", {}, got)))
        api.register_tool(_tool("inner", _outer_calling("x", {}, []), exposure=exposure))

    session = _session(tmp_path, ext)
    await _prompt(session, ScriptedModel([("call_1", "outer", {})]))
    assert isinstance(got[0], NestedCallError) and "not available" in str(got[0])


async def test_a_nested_tool_error_raises_with_the_tools_text(tmp_path):
    got: list[Any] = []

    async def failing(tool_call_id, params, signal, on_update, ctx):
        raise ToolError("inner says no")

    def ext(api):
        api.register_tool(_tool("outer", _outer_calling("inner", {}, got)))
        api.register_tool(_tool("inner", failing, exposure="callable"))

    session = _session(tmp_path, ext)
    messages = await _prompt(session, ScriptedModel([("call_1", "outer", {})]))
    assert isinstance(got[0], NestedCallError) and str(got[0]) == "inner says no"
    assert got[0].result is not None and got[0].result.is_error
    assert _text(_results(messages)["call_1"]) == "inner says no"


async def test_recursion_stops_at_the_depth_limit(tmp_path):
    depths: list[int] = []

    async def recurse(tool_call_id, params, signal, on_update, ctx):
        depths.append(tool_call_id.count("/"))
        await ctx.execute_tool("recurse", {})
        return {"content": [{"type": "text", "text": "unreachable"}]}

    def ext(api):
        api.register_tool(_tool("recurse", recurse))

    session = _session(tmp_path, ext)
    messages = await _prompt(session, ScriptedModel([("c", "recurse", {})]))
    assert depths == list(range(MAX_NESTING_DEPTH + 1))
    assert "limited to" in _text(_results(messages)["c"])


async def test_parallel_parents_each_record_only_their_own_nested_calls(tmp_path):
    async def slow(tool_call_id, params, signal, on_update, ctx):
        await asyncio.sleep(0.01)
        return {"content": [{"type": "text", "text": params["tag"]}]}

    async def outer(tool_call_id, params, signal, on_update, ctx):
        for _ in range(2):
            await ctx.execute_tool("slow", {"tag": params["tag"]})
        return {"content": [{"type": "text", "text": "ok"}]}

    def ext(api):
        api.register_tool(_tool("slow", slow, exposure="callable"))
        api.register_tool(_tool("outer", outer))

    session = _session(tmp_path, ext)
    script = ScriptedModel([("a", "outer", {"tag": "A"}), ("b", "outer", {"tag": "B"})])
    results = _results(await _prompt(session, script))
    for parent, tag in (("a", "A"), ("b", "B")):
        record = results[parent]["details"]["nested_calls"]
        assert [r["tool_call_id"] for r in record] == [f"{parent}/1", f"{parent}/2"]
        assert {r["args"]["tag"] for r in record} == {tag}


async def test_a_hook_that_rewrites_content_drops_the_stale_value(tmp_path):
    (tmp_path / "s.txt").write_text("password=hunter2\n")
    got: list[Any] = []

    def redact(event, ctx):
        if event["tool_name"] == "grep":
            return {"content": [{"type": "text", "text": "[redacted]"}]}
        return None

    def ext(api):
        api.register_tool(_tool("outer", _outer_calling("grep", {"pattern": "password"}, got)))
        api.on("tool_result", redact)

    session = _session(tmp_path, ext)
    await _prompt(session, ScriptedModel([("c", "outer", {})]))
    assert got[0].text == "[redacted]"
    assert got[0].value is None


async def test_an_extension_can_call_a_tool_outside_any_tool_call(tmp_path):
    session = _session(tmp_path)
    result = await session.execute_tool("bash", {"command": "echo out"})
    assert result.tool_call_id.startswith("ext-")
    assert result.value["stdout"] == "out\n"


# ── exposure ─────────────────────────────────────────────────────────────────


async def test_only_direct_and_model_only_tools_are_offered_to_the_model(tmp_path):
    async def noop(tool_call_id, params, signal, on_update, ctx):
        return {"content": [{"type": "text", "text": "x"}]}

    def ext(api):
        for exposure in ("direct", "model-only", "callable", "deferred", "hidden"):
            api.register_tool(_tool(f"t_{exposure.replace('-', '_')}", noop, exposure=exposure))

    model = ScriptedModel()
    await _prompt(_session(tmp_path, ext), model)
    assert [n for n in model.offered[0] if n.startswith("t_")] == ["t_direct", "t_model_only"]


def _roles(messages: list[Any]) -> list[str]:
    return [m.get("role") if isinstance(m, dict) else m.role for m in messages]


async def test_masking_keeps_the_tools_block_refuses_the_call_and_tells_the_model(tmp_path):
    log = InMemorySessionLog()
    session = _session(tmp_path, log=log)
    first = ScriptedModel()
    await _prompt(session, first)
    before = session.cursor.leaf
    await session.set_tool_exposure("bash", "hidden")
    assert config_at(log.entries(), session.cursor.leaf)["tool_exposure"] == {"bash": "hidden"}
    notice = [e for e in log.entries() if e.get("customType") == "tool_exposure"]
    assert len(notice) == 1 and "`bash` is no longer available" in str(notice[0])

    model = ScriptedModel([("b", "bash", {"command": "echo hi"})])
    messages = await _prompt(session, model)
    assert model.blocks[0] == first.blocks[0]
    assert "it was set to 'hidden'" in _text(_results(messages)["b"])
    assert _results(messages)["b"]["is_error"]

    resumed = _session(tmp_path, log=log)
    model = ScriptedModel([("b2", "bash", {"command": "echo hi"})])
    messages = await _prompt(resumed, model)
    assert model.blocks[0] == first.blocks[0]
    assert _results(messages)["b2"]["is_error"]

    earlier = AgentSession(
        cursor=Cursor(log, before),
        model=_MODEL,
        tools=_resolve_tools(["read", "bash", "grep", "ls"], cwd=str(tmp_path)),
    )
    model = ScriptedModel([("b3", "bash", {"command": "echo hi"})])
    messages = await _prompt(earlier, model)
    assert not _results(messages)["b3"]["is_error"]


async def test_a_mask_set_mid_turn_is_announced_after_the_tool_results(tmp_path):
    async def masker(tool_call_id, params, signal, on_update, ctx):
        await ctx.set_tool_exposure("bash", "hidden")
        return {"content": [{"type": "text", "text": "masked"}]}

    def ext(api):
        api.register_tool(_tool("masker", masker))

    session = _session(tmp_path, ext)
    model = ScriptedModel([("m", "masker", {})])
    await _prompt(session, model)
    assert model.blocks[0] == model.blocks[1]
    assert _roles(model.contexts[1])[-2:] == ["toolResult", "user"]
    assert "`bash` is no longer available" in json.dumps(model.contexts[1][-1], default=str)


async def test_a_deferred_tool_is_found_and_called_without_changing_the_tools_block(tmp_path):
    async def secret(tool_call_id, params, signal, on_update, ctx):
        return {"content": [{"type": "text", "text": "the secret " + params["key"]}]}

    def ext(api):
        api.register_tool(tool_search_definition())
        api.register_tool(call_tool_definition())
        api.register_tool(
            {
                **_tool("secret", secret, exposure="deferred", tags=["vault"]),
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            }
        )

    session = _session(tmp_path, ext)
    model = ScriptedModel(
        [("s", "tool_search", {"query": "vault"})],
        [("u", "call_tool", {"name": "secret", "arguments": {"key": "k"}})],
    )
    messages = await _prompt(session, model)
    assert "secret" not in model.offered[0]
    assert len(set(model.blocks)) == 1
    assert "async def secret(key: str) -> str:" in _text(_results(messages)["s"])
    called = _results(messages)["u"]
    assert _text(called) == "the secret k"
    assert called["details"]["nested_calls"][0]["tool_name"] == "secret"


@pytest.mark.parametrize("exposure", ["callable", "hidden"])
async def test_call_tool_refuses_what_the_model_may_not_reach(tmp_path, exposure):
    async def inner(tool_call_id, params, signal, on_update, ctx):
        return {"content": [{"type": "text", "text": "ran"}]}

    def ext(api):
        api.register_tool(call_tool_definition())
        api.register_tool(_tool("inner", inner, exposure=exposure))

    session = _session(tmp_path, ext)
    model = ScriptedModel([("u", "call_tool", {"name": "inner", "arguments": {}})])
    result = _results(await _prompt(session, model))["u"]
    assert result["is_error"] and "ran" not in _text(result)


async def test_only_an_explicit_redeclaration_changes_the_block_and_it_is_reported(tmp_path):
    async def extra(tool_call_id, params, signal, on_update, ctx):
        return {"content": []}

    def ext(api):
        api.register_tool(_tool("extra", extra, exposure="deferred"))

    session = _session(tmp_path, ext)
    first = ScriptedModel()
    await _prompt(session, first)
    preview = session.preview_redeclare({"extra": True})
    assert preview.added == ("extra",) and preview.invalidates_cache
    assert not session.preview_redeclare({}).invalidates_cache
    await session.redeclare_tools({"extra": True})
    model = ScriptedModel()
    await _prompt(session, model)
    assert model.blocks[0] != first.blocks[0] and "extra" in model.offered[0]
    with pytest.raises(ValueError, match="no tool named"):
        await session.redeclare_tools({"nope": True})


def test_tool_block_change_is_pure_and_names_each_kind_of_change():
    @dataclasses.dataclass
    class T:
        name: str
        description: str = "d"
        parameters: dict = dataclasses.field(default_factory=dict)

    a, b, c = T("a"), T("b"), T("c")
    assert not tool_block_change([a, b], [T("a"), T("b")]).invalidates_cache
    change = tool_block_change([a, b], [b, T("a", "new"), c])
    assert change.added == ("c",) and change.changed == ("a",) and change.reordered
    assert tool_block_change([a, b], [a]).removed == ("b",)


async def test_an_unchanged_tool_set_serializes_identically_across_turns(tmp_path):
    session = _session(tmp_path)
    first = [d.__dict__ for d in session.describe_tools()]
    await _prompt(session, ScriptedModel())
    assert [d.__dict__ for d in session.describe_tools()] == first


async def test_set_tool_exposure_refuses_unknown_names_and_values(tmp_path):
    session = _session(tmp_path)
    with pytest.raises(ValueError, match="no tool named"):
        await session.set_tool_exposure("nope", "hidden")
    with pytest.raises(ValueError, match="is not one of"):
        await session.set_tool_exposure("bash", "invisible")


# ── discovery ────────────────────────────────────────────────────────────────


def _noop_tool(name: str, **extra: Any) -> dict[str, Any]:
    async def noop(tool_call_id, params, signal, on_update, ctx):
        return {"content": []}

    return _tool(name, noop, **extra)


def ext_git(api):
    api.register_tool(_noop_tool("git_status", tags=["vcs"]))
    api.register_tool(_noop_tool("git_log", tags=["vcs"], exposure="callable"))
    api.register_tool(_noop_tool("gh_pr", namespace=f"{EXT_GIT}.github"))


EXT_GIT = f"{__name__}.ext_git"
"""An inline factory's identifier is its ``module.qualname`` (docs/EXTENSION-PACKAGES.md §2)."""


async def test_tools_are_found_by_glob_namespace_tag_and_exposure(tmp_path):
    ctx = _session(tmp_path, ext_git)._extension_api.context
    assert [d.name for d in ctx.list_tools("git_*")] == ["git_status", "git_log"]
    assert [d.name for d in ctx.list_tools(namespace="core")] == ["read", "bash", "grep", "ls"]
    assert [d.name for d in ctx.list_tools(namespace=f"{EXT_GIT}.*")] == ["gh_pr"]
    assert [d.name for d in ctx.list_tools(tag="vcs", exposure="callable")] == ["git_log"]
    grep = ctx.describe_tool("grep")
    assert grep.namespace == "core" and grep.output_schema is not None and grep.callable
    with pytest.raises(KeyError):
        ctx.describe_tool("missing")


async def test_extensions_are_found_by_name(tmp_path):
    ctx = _session(tmp_path, ext_git)._extension_api.context
    info = ctx.find_extension(EXT_GIT)
    assert info is not None and info.tools == ("git_status", "git_log", "gh_pr")
    assert [e.name for e in ctx.list_extensions("*.ext_*")] == [EXT_GIT]
    assert ctx.find_extension("absent") is None


def test_an_extension_cannot_file_a_tool_outside_its_namespace(tmp_path):
    def squatter(api):
        api.register_tool(_noop_tool("fake_read", namespace="core"))

    with pytest.raises(ValueError, match="only in its own namespace"):
        _session(tmp_path, squatter)


# ── output capture ───────────────────────────────────────────────────────────


async def test_a_printing_tool_is_captured_onto_its_details_not_stdout(tmp_path, capsys):
    async def noisy(tool_call_id, params, signal, on_update, ctx):
        print("debug from tool")
        print("warn", file=sys.stderr)
        await asyncio.to_thread(print, "from a thread")
        return {"content": [{"type": "text", "text": "ok"}]}

    def ext(api):
        api.register_tool(_tool("noisy", noisy))

    messages = await _prompt(_session(tmp_path, ext), ScriptedModel([("n", "noisy", {})]))
    captured = _results(messages)["n"]["details"]["captured_output"]
    assert captured == {"stdout": "debug from tool\nfrom a thread\n", "stderr": "warn\n"}
    out = capsys.readouterr()
    assert "debug from tool" not in out.out and "warn" not in out.err


async def test_concurrent_captures_do_not_mix_and_outside_writes_pass_through(capsys):
    async def write(tag: str) -> str:
        with capture_tool_output() as sink:
            for _ in range(3):
                print(tag)
                await asyncio.sleep(0)
        return sink.stdout.getvalue()

    a, b = await asyncio.gather(write("A"), write("B"))
    print("outside")
    assert (a, b) == ("A\nA\nA\n", "B\nB\nB\n")
    assert capsys.readouterr().out == "outside\n"


async def test_bash_never_reads_the_heads_stdin(tmp_path):
    out = await BashTool(cwd=str(tmp_path)).execute("c", {"command": "cat", "timeout": 2000})
    assert out["value"]["timed_out"] is False
    assert out["value"]["stdout"] == ""


def test_capture_is_capped():
    with capture_tool_output() as sink:
        print("x" * (MAX_CAPTURED_CHARS + 10), end="")
    assert sink.as_details() == {"stdout": "x" * MAX_CAPTURED_CHARS, "dropped": 10}


# ── issue #20: defects found while documenting the surface ───────────────────


async def test_preview_redeclare_refuses_an_unknown_name_as_redeclare_does(tmp_path):
    session = _session(tmp_path)
    with pytest.raises(ValueError, match="no tool named"):
        session.preview_redeclare({"nope": True})


async def test_redeclaring_what_is_already_declared_writes_nothing(tmp_path):
    log = InMemorySessionLog()
    session = _session(tmp_path, log=log)
    before = len(log.entries())
    change = await session.redeclare_tools({"bash": True})
    assert not change.invalidates_cache
    assert len(log.entries()) == before
    await session.redeclare_tools({"bash": False})
    withdrawn = len(log.entries())
    assert withdrawn > before
    await session.redeclare_tools({"bash": False})
    assert len(log.entries()) == withdrawn
    await session.redeclare_tools({"bash": True})
    assert len(log.entries()) > withdrawn


async def test_setting_a_tool_to_its_default_exposure_writes_nothing(tmp_path):
    log = InMemorySessionLog()
    session = _session(tmp_path, log=log)
    before = len(log.entries())
    await session.set_tool_exposure("bash", "direct")
    assert len(log.entries()) == before
    await session.set_tool_exposure("bash", "hidden")
    masked = len(log.entries())
    assert masked > before
    await session.set_tool_exposure("bash", "direct")
    assert len(log.entries()) > masked
    assert config_at(log.entries(), session.cursor.leaf)["tool_exposure"] == {"bash": "direct"}


def test_a_stub_with_non_identifier_names_is_valid_python_that_keeps_them():
    stub = render_stub(
        "copy",
        "Copy a range.",
        {
            "type": "object",
            "properties": {"from": {"type": "integer"}, "my-arg": {"type": "string"}},
            "required": ["from"],
        },
        {
            "type": "object",
            "title": "CopyResult",
            "properties": {"class": {"type": "string"}, "ok": {"type": "boolean"}},
            "required": ["class", "ok"],
        },
    )
    namespace: dict[str, Any] = {}
    exec("from typing_extensions import NotRequired, TypedDict, Unpack\n" + stub, namespace)
    args = namespace["copy_arguments"]
    assert args.__required_keys__ == {"from"} and args.__optional_keys__ == {"my-arg"}
    assert namespace["CopyResult"].__required_keys__ == {"class", "ok"}
    assert "async def copy(**kwargs: Unpack[copy_arguments]) -> CopyResult:" in stub


@dataclasses.dataclass
class _Described:
    name: str
    description: str
    parameters: dict[str, Any]
    output_schema: dict[str, Any] | None


def test_render_stubs_gives_two_shapes_with_one_title_two_names():
    def tool(name: str, field: str) -> _Described:
        result = {
            "type": "object",
            "title": "Result",
            "properties": {field: {"type": "string"}},
            "required": [field],
        }
        return _Described(name, f"the {name} tool", {"type": "object", "properties": {}}, result)

    text = render_stubs([tool("a", "x"), tool("b", "y"), tool("c", "x")])
    namespace: dict[str, Any] = {}
    exec(text, namespace)
    assert namespace["Result"].__required_keys__ == {"x"}
    assert namespace["Result_2"].__required_keys__ == {"y"}
    assert "async def a() -> Result:" in text
    assert "async def b() -> Result_2:" in text
    assert "async def c() -> Result:" in text
    assert text.count("class Result(") == 1


def test_an_unresolvable_parameter_annotation_does_not_break_the_return_schema():
    def fn(x: "Missing") -> int:  # type: ignore[name-defined]  # noqa: F821
        return 1

    assert output_schema_of(fn) == {"type": "integer"}

    def unresolved_return() -> "Missing":  # type: ignore[name-defined]  # noqa: F821
        ...

    with pytest.raises(NameError):
        output_schema_of(unresolved_return)


def test_tool_from_function_does_not_resolve_the_ctx_annotation():
    def needs_ctx(text: str, ctx: "Unimported") -> int:  # type: ignore[name-defined]  # noqa: F821
        """Count characters."""
        return len(text)

    spec = tool_from_function(needs_ctx)
    assert set(spec["parameters"]["properties"]) == {"text"}


def test_captured_output_refuses_an_unknown_stream():
    with pytest.raises(ValueError, match="stdout"):
        CapturedOutput().write("stdrr", "x")


async def test_find_extension_refuses_an_ambiguous_name(tmp_path):
    def twin(api):
        api.register_tool(_noop_tool("git_other"))

    twin.__qualname__ = ext_git.__qualname__
    ctx = _session(tmp_path, ext_git, twin)._extension_api.context
    assert [e.name for e in ctx.list_extensions()] == [EXT_GIT, EXT_GIT]
    with pytest.raises(ValueError, match="2 loaded extensions"):
        ctx.find_extension(EXT_GIT)
