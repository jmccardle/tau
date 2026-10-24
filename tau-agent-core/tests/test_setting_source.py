"""Model and thinking changes record their source and notify extensions (docs/TURN-SETTINGS.md §8)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.rpc import commands
from tau_agent_core.sdk import create_agent_session
from tau_agent_core.session_log import CONFIG_ENTRY_TYPE, InMemorySessionLog, config_at
from tau_agent_core.setting_sources import (
    SettingSource,
    check_source,
    restored_source,
)
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


class _Stream:
    def __init__(self, final: AssistantMessage) -> None:
        self._final = final

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            yield TextDeltaEvent(delta="ok", partial=self._final)
            yield DoneEvent(final=self._final, usage=self._final.usage)

        return gen()

    def abort(self) -> None:
        pass


def _provider(model: Any, context: Any, options: Any = None, **_: Any) -> Any:
    return _Stream(
        AssistantMessage(
            content=[TextContent(text="ok")],
            api="openai-completions",
            provider="openai",
            model=model.id,
            stop_reason="stop",
            timestamp=_TS,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )
    )


def _sub(text: str) -> Submission:
    return Submission(text=text, source="interactive", submitter="human", submission_id=text)


def _session(*extensions: Any, **kw: Any) -> AgentSession:
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        tools=[],
        extensions=list(extensions),
        **kw,
    )
    session.set_model_resolver(lambda name: _model(name))
    return session


def _recorded(session: AgentSession) -> dict[str, Any]:
    return config_at(session.cursor.entries(), session.cursor.leaf)


def _config_entries(session: AgentSession) -> list[dict[str, Any]]:
    return [
        e["data"]
        for e in session.session_log.entries()
        if e.get("type") == "customEntry" and e.get("customType") == CONFIG_ENTRY_TYPE
    ]


async def _turn(session: AgentSession, text: str = "go") -> None:
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_provider):
        result = await session.submit(_sub(text))
    assert result.accepted, result.rejection_reason


def _observer(seen: list[dict[str, Any]]) -> Any:
    def factory(api: Any) -> None:
        api.on("turn_setting_changed", lambda event, ctx: seen.append(event))

    return factory


class TestVocabulary:
    @pytest.mark.parametrize(
        "source", ["default", "cli", "command", "rpc", "resume", "spawn", "extension:router"]
    )
    def test_every_value_is_accepted(self, source: str) -> None:
        assert check_source(source) == source

    @pytest.mark.parametrize("source", ["unknown", "", "extension:", "CLI"])
    def test_anything_else_raises(self, source: str) -> None:
        with pytest.raises(ValueError, match="not a setting source"):
            check_source(source)

    def test_a_setter_refuses_resume_and_unknown_sources(self) -> None:
        session = _session()
        with pytest.raises(ValueError, match="restored"):
            session.set_model("cheap", source="resume")
        with pytest.raises(ValueError, match="not a setting source"):
            session.set_reasoning("high", source="whoever")
        assert session.get_setting_source("model") == SettingSource("cli")

    def test_an_unknown_setting_raises(self) -> None:
        with pytest.raises(ValueError, match="not a turn setting"):
            _session().get_setting_source("temperature")


class TestConstruction:
    def test_constructor_arguments_are_cli_and_an_absent_level_is_default(self) -> None:
        session = _session()
        assert session.get_setting_source("model") == SettingSource("cli")
        assert session.get_setting_source("thinking") == SettingSource("default")
        assert _session(reasoning="high").get_setting_source("thinking") == SettingSource("cli")

    def test_the_sdk_default_model_is_default_and_a_named_one_cli(self) -> None:
        assert create_agent_session().get_setting_source("model") == SettingSource("default")
        named = create_agent_session(model="gpt-4o")
        assert named.get_setting_source("model") == SettingSource("cli")

    async def test_the_first_record_carries_both_sources(self) -> None:
        session = _session(model_source=SettingSource("default"))
        await session.start()
        recorded = _recorded(session)
        assert recorded["model_source"] == "default"
        assert recorded["thinking_source"] == "default"

    async def test_the_initial_setting_fires_no_event(self) -> None:
        seen: list[dict[str, Any]] = []
        session = _session(_observer(seen))
        await session.start()
        await _turn(session)
        assert seen == []


class TestSetterChanges:
    async def test_set_model_records_its_source_ahead_of_the_next_request(self) -> None:
        session = _session()
        await session.start()
        session.set_model("cheap", source="command")
        assert session.get_setting_source("model") == SettingSource("command")
        assert _recorded(session)["model_source"] == "cli"
        await _turn(session)
        assert _recorded(session)["model_source"] == "command"
        assert _recorded(session)["model"] == "cheap"

    async def test_the_event_payload(self) -> None:
        seen: list[dict[str, Any]] = []
        session = _session(_observer(seen), reasoning="low")
        await session.start()
        session.set_model("cheap", source="rpc")
        session.set_reasoning("high", source="command")
        await _turn(session)
        assert seen == [
            {
                "type": "turn_setting_changed",
                "cursor_id": session.cursor.id,
                "setting": "model",
                "old": "m",
                "new": "cheap",
                "source": "rpc",
            },
            {
                "type": "turn_setting_changed",
                "cursor_id": session.cursor.id,
                "setting": "thinking",
                "old": "low",
                "new": "high",
                "source": "command",
            },
        ]

    async def test_record_settings_announces_at_once(self) -> None:
        seen: list[dict[str, Any]] = []
        session = _session(_observer(seen))
        await session.start()
        session.set_model("cheap", source="command")
        await session.record_settings(backend="openai")
        assert [e["new"] for e in seen] == ["cheap"]
        assert _recorded(session)["model_source"] == "command"
        await _turn(session)
        assert len(seen) == 1

    async def test_a_same_value_pick_records_the_new_source_and_announces_it(self) -> None:
        seen: list[dict[str, Any]] = []
        session = _session(_observer(seen))
        session.set_model("m", source="cli")
        await session.start()
        session.set_model("m", source="command")
        await _turn(session)
        assert _recorded(session)["model_source"] == "command"
        assert [(e["old"], e["new"], e["source"]) for e in seen] == [("m", "m", "command")]

    async def test_an_unchanged_repeat_writes_and_announces_nothing(self) -> None:
        seen: list[dict[str, Any]] = []
        session = _session(_observer(seen))
        await session.start()
        before = len(_config_entries(session))
        session.set_reasoning("high", source="command")
        session.set_reasoning("high", source="command")
        await _turn(session)
        session.set_reasoning("high", source="command")
        await _turn(session, "again")
        assert len(seen) == 1
        assert len(_config_entries(session)) == before + 1

    async def test_rpc_verbs_record_rpc(self) -> None:
        session = _session()

        class _Handler:
            pass

        handler = _Handler()
        handler.session = session  # type: ignore[attr-defined]
        await commands._handle_set_reasoning(handler, 1, {"level": "high"})  # type: ignore[arg-type]
        assert session.get_setting_source("thinking") == SettingSource("rpc")


class TestExtensionAttribution:
    async def test_ctx_set_model_in_a_hook_is_the_extension(self) -> None:
        seen: list[dict[str, Any]] = []

        def router(api: Any) -> None:
            def on_input(event: Any, ctx: Any) -> None:
                ctx.set_model("cheap")
                ctx.set_reasoning("minimal")

            api.on("input", on_input)

        session = _session(router, _observer(seen))
        await _turn(session)
        source = session.get_setting_source("model").source
        assert source.startswith("extension:") and source.endswith("<locals>.router")
        assert session.get_setting_source("thinking") == SettingSource(source)
        assert _recorded(session)["model_source"] == source
        assert [e["source"] for e in seen] == [source, source]

    def test_ctx_set_model_outside_any_extension_raises(self) -> None:
        session = _session()
        with pytest.raises(RuntimeError, match="cannot be attributed"):
            session._extension_api.context.set_model("cheap")
        assert session.get_setting_source("model") == SettingSource("cli")

    async def test_ctx_reads_the_source_at_session_start_and_input(self) -> None:
        reads: list[tuple[str, SettingSource, SettingSource]] = []

        def reader(api: Any) -> None:
            def record(event: Any, ctx: Any) -> None:
                reads.append(
                    (
                        event["type"],
                        ctx.get_setting_source("model"),
                        ctx.get_setting_source("thinking"),
                    )
                )

            api.on("session_start", record)
            api.on("input", record)

        session = _session(
            reader,
            model_source=SettingSource("command", restored=True),
            reasoning_source=SettingSource("resume", restored=True),
        )
        await session.emit_session_start("startup")
        await _turn(session)
        restored = (SettingSource("command", True), SettingSource("resume", True))
        assert reads == [("session_start", *restored), ("input", *restored)]


class TestResume:
    def test_a_recorded_source_is_restored_as_recorded(self) -> None:
        recorded = {"model": "big", "model_source": "command", "thinking": "high"}
        assert restored_source(recorded, "model") == SettingSource("command", restored=True)

    def test_an_entry_written_before_sources_restores_as_resume(self) -> None:
        recorded = {"model": "big", "thinking": "high"}
        assert restored_source(recorded, "thinking") == SettingSource("resume", restored=True)

    async def test_a_restored_value_writes_no_source_and_fires_nothing(self) -> None:
        log = InMemorySessionLog()
        first = AgentSession(session_log=log, model=_model(), tools=[])
        first.set_model_resolver(lambda name: _model(name))
        first.set_model("m", source="command")
        await first.start()
        recorded = config_at(log.entries(), first.cursor.leaf)
        seen: list[dict[str, Any]] = []
        resumed = AgentSession(
            session_log=log,
            model=_model(),
            tools=[],
            extensions=[_observer(seen)],
            model_source=restored_source(recorded, "model"),
            reasoning_source=restored_source(recorded, "thinking"),
        )
        before = len(log.entries())
        await resumed.start()
        written = [e["data"] for e in log.entries()[before:]]
        assert not any("model_source" in d or "thinking_source" in d for d in written)
        assert resumed.get_setting_source("model") == SettingSource("command", restored=True)
        assert resumed.get_setting_source("thinking") == SettingSource("default", restored=True)
        assert seen == []


class TestCursorFrames:
    async def test_a_spawned_cursor_with_its_own_model_reads_spawn(self) -> None:
        reads: list[SettingSource] = []

        def factory(api: Any) -> None:
            api.on("input", lambda event, ctx: reads.append(ctx.get_setting_source("model")))

        session = _session(factory)
        await session.start()
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_provider):
            result = await session.spawn(
                at=session.cursor.leaf, prompt="sub", tools=[], model="cheap", hooks=True
            )
        assert result.ok
        assert reads == [SettingSource("spawn")]
