"""A request is sent with the settings in force when it starts (docs/TURN-SETTINGS.md).

The loop reads model, key, reasoning and temperature per request from the
session, and the session records a change ahead of the request it governs. Only
``stream_simple`` is faked; every test runs the real loop.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_loop import AgentLoop
from tau_agent_core.agent_loop_types import AgentLoopConfig, TurnSettings
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.cursor import TURN_CURSOR
from tau_agent_core.session_log import CONFIG_ENTRY_TYPE, InMemorySessionLog, config_at
from tau_agent_core.submission import Submission

_TS = 1_700_000_000_000


def _model(mid: str = "m") -> Model:
    return Model(
        id=mid,
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name=mid,
        context_window=8192,
        max_tokens=256,
    )


def _assistant(content: list[Any], mid: str, stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model=mid,
        stop_reason=stop_reason,
        timestamp=_TS,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for event in self._events:
                yield event

        return gen()

    def abort(self) -> None:
        pass


class _Provider:
    """Answers each request with text; records each request's model id and options."""

    def __init__(self, tool_first: bool = False) -> None:
        self.calls: list[str] = []
        self.options: list[dict[str, Any]] = []
        self._tool_first = tool_first

    def __call__(self, model: Any, context: Any, options: Any = None, **_: Any) -> Any:
        self.calls.append(model.id)
        self.options.append(dict(options or {}))
        if self._tool_first and len(self.calls) == 1:
            call = ToolCall(type="toolCall", id="c1", name="missing", arguments={})
            final = _assistant([call], model.id, stop_reason="toolUse")
            return _Stream([DoneEvent(final=final, usage=final.usage)])
        final = _assistant([TextContent(text="ok")], model.id)
        return _Stream(
            [TextDeltaEvent(delta="ok", partial=final), DoneEvent(final=final, usage=final.usage)]
        )


def _sub(text: str, **kw: Any) -> Submission:
    return Submission(text=text, source="interactive", submitter="human", submission_id=text, **kw)


def _session(*extensions: Any) -> AgentSession:
    session = AgentSession(
        session_log=InMemorySessionLog(), model=_model(), tools=[], extensions=list(extensions)
    )
    session.set_model_resolver(lambda name: _model(name))
    return session


def _on_first_turn_end(action: Any) -> Any:
    """An extension factory running ``action(ctx)`` in the first ``turn_end`` only."""

    def factory(api: Any) -> None:
        fired = {"n": 0}

        def on_turn_end(event: Any, ctx: Any) -> None:
            fired["n"] += 1
            if fired["n"] == 1:
                action(ctx)

        api.on("turn_end", on_turn_end)

    return factory


def _config_entries(session: AgentSession) -> list[dict[str, Any]]:
    return [
        e
        for e in session.session_log.entries()
        if e.get("type") == "customEntry" and e.get("customType") == CONFIG_ENTRY_TYPE
    ]


class TestASwitchReachesTheNextRequest:
    async def test_turn_end_set_model_reaches_the_next_request_in_the_same_loop(self):
        session = _session(_on_first_turn_end(lambda ctx: ctx.set_model("cheap")))
        provider = _Provider(tool_first=True)
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await session.submit(_sub("go"))
        assert provider.calls == ["m", "cheap"]

    async def test_turn_end_set_reasoning_reaches_the_next_request(self):
        session = _session(_on_first_turn_end(lambda ctx: ctx.set_reasoning("high")))
        provider = _Provider(tool_first=True)
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await session.submit(_sub("go"))
        assert [o.get("reasoning") for o in provider.options] == [None, "high"]

    async def test_the_key_and_temperature_follow_the_model(self):
        cheap = _model("cheap").model_copy(update={"temperature": 0.3})

        class _Resolver:
            def __call__(self, name: str) -> Model:
                return cheap if name == "cheap" else _model(name)

            def api_key(self, name: str) -> str:
                return f"key-{name}"

        session = _session(_on_first_turn_end(lambda ctx: ctx.set_model("cheap")))
        session.set_model_resolver(_Resolver())
        provider = _Provider(tool_first=True)
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await session.submit(_sub("go"))
        assert provider.options[1]["api_key"] == "key-cheap"
        assert provider.options[1]["temperature"] == 0.3
        assert "api_key" not in provider.options[0]


class TestTheChangeIsRecordedAheadOfItsRequest:
    async def test_a_config_entry_sits_between_the_tool_result_and_the_second_answer(self):
        session = _session(_on_first_turn_end(lambda ctx: ctx.set_model("cheap")))
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_Provider(True)):
            await session.submit(_sub("go"))
        path = [e for e in session.cursor.entries()]
        kinds = [
            "config"
            if e.get("customType") == CONFIG_ENTRY_TYPE
            else (e.get("message") or {}).get("role", e.get("type"))
            for e in path
        ]
        assert kinds == ["config", "user", "assistant", "toolResult", "config", "assistant"]
        mid_loop = path[4]["data"]
        assert set(mid_loop) == {"model", "model_spec"}
        assert mid_loop["model_spec"]["id"] == "cheap"
        assert (path[5]["message"] or {}).get("model") == "cheap"

    async def test_reasoning_is_recorded_as_thinking(self):
        session = _session(_on_first_turn_end(lambda ctx: ctx.set_reasoning("low")))
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_Provider(True)):
            await session.submit(_sub("go"))
        assert config_at(session.cursor.entries(), session.cursor.leaf)["thinking"] == "low"

    async def test_no_switch_writes_one_config_entry_and_sends_the_same_request(self):
        session = _session()
        provider = _Provider(tool_first=True)
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await session.submit(_sub("go"))
        assert len(_config_entries(session)) == 1
        assert provider.calls == ["m", "m"]
        assert provider.options[0] == provider.options[1]

    async def test_a_turn_that_stores_no_history_records_nothing(self):
        session = _session(_on_first_turn_end(lambda ctx: ctx.set_model("cheap")))
        provider = _Provider(tool_first=True)
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await session.submit(_sub("go", store_history=False))
        assert provider.calls == ["m", "cheap"]
        assert session.session_log.entries() == []


class TestTheModelNameIsRecordedForResume:
    async def test_ctx_set_model_is_recorded_as_the_config_name(self):
        session = _session(_on_first_turn_end(lambda ctx: ctx.set_model("cheap")))
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_Provider()):
            await session.submit(_sub("one"))
            await session.submit(_sub("two"))
        recorded = config_at(session.cursor.entries(), session.cursor.leaf)
        assert recorded["model"] == "cheap"
        assert recorded["model_spec"]["id"] == "cheap"

    async def test_no_name_is_recorded_until_one_is_resolved(self):
        session = _session()
        await session.start()
        assert "model" not in config_at(session.cursor.entries(), session.cursor.leaf)

    async def test_a_name_already_on_the_path_is_not_written_again(self):
        session = _session()
        session.set_model("cheap")
        await session.cursor.append_config(model="cheap", backend="openai")
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_Provider()):
            await session.submit(_sub("one"))
        assert [e["data"].get("model") for e in _config_entries(session)] == ["cheap", None]

    async def test_a_non_head_cursor_records_its_own_name(self):
        session = _session()
        other = await session.open_cursor(None, owner=session.cursor, label="sub")
        token = TURN_CURSOR.set(other)
        try:
            session.set_model("side")
        finally:
            TURN_CURSOR.reset(token)
        assert other.frame is not None and other.frame.model_name == "side"
        assert session._turn_model_name() is None


class TestSetReasoning:
    def test_an_unknown_level_raises(self):
        session = _session()
        with pytest.raises(ValueError, match="not a thinking level"):
            session.set_reasoning("extreme")

    def test_off_is_a_level_and_is_what_the_next_request_sends(self):
        session = _session()
        assert session.set_reasoning("off") == "off"
        assert session._turn_reasoning() == "off"

    async def test_a_non_head_cursor_gets_it_on_its_frame(self):
        session = _session()
        other = await session.open_cursor(None, owner=session.cursor, label="sub")
        token = TURN_CURSOR.set(other)
        try:
            session.set_reasoning("minimal")
        finally:
            TURN_CURSOR.reset(token)
        assert other.frame is not None and other.frame.reasoning == "minimal"
        assert session._turn_reasoning() is None


class TestAStandaloneLoop:
    async def test_without_a_source_every_request_uses_the_construction_values(self):
        provider = _Provider()
        loop = AgentLoop(
            config=AgentLoopConfig(api_key="k", reasoning="low", temperature=0.5),
            model=_model("fixed"),
        )
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await loop.run(prompts=[{"role": "user", "content": "hi", "timestamp": _TS}])
        assert provider.calls == ["fixed"]
        assert provider.options[0] == {"api_key": "k", "reasoning": "low", "temperature": 0.5}

    async def test_a_source_is_awaited_before_every_request(self):
        provider = _Provider(tool_first=True)
        asked: list[int] = []

        async def settings() -> TurnSettings:
            asked.append(len(provider.calls))
            return TurnSettings(model=_model(f"r{len(asked)}"))

        loop = AgentLoop(config=AgentLoopConfig(), turn_settings=settings)
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider):
            await loop.run(prompts=[{"role": "user", "content": "hi", "timestamp": _TS}])
        assert asked == [0, 1]
        assert provider.calls == ["r1", "r2"]
