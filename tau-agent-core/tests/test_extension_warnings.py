"""Per-session extension warnings: the store, the api, the RPC field (#60)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_warnings import (
    ExtensionWarning,
    ExtensionWarnings,
    warnings_marker,
)
from tau_agent_core.rpc import capabilities, commands
from tau_agent_core.rpc.handler import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog

_WARNER = """
def register(api):
    api.set_warning("excluded", "bash was left out", "error")

    async def clear(args, ctx):
        api.clear_warning("excluded")

    api.register_command("unwarn", {"description": "clear it", "handler": clear})
"""


def _session() -> AgentSession:
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


async def _load_warner(session: AgentSession, tmp_path: Path) -> str:
    path = tmp_path / "warner.py"
    path.write_text(_WARNER)
    await session.load_extensions([str(path)], discover=False)
    return str(path)


class TestTheStore:
    def test_set_replace_and_clear_keep_first_set_order(self):
        store = ExtensionWarnings()
        seen: list[list[ExtensionWarning]] = []
        store.subscribe(seen.append)

        assert store.set(ExtensionWarning("a", "k", "one", "warning"))
        assert store.set(ExtensionWarning("b", "k", "two", "error"))
        assert store.set(ExtensionWarning("a", "k", "one, again", "warning"))
        assert not store.set(ExtensionWarning("a", "k", "one, again", "warning"))

        assert [(w.source, w.message) for w in store.current()] == [
            ("a", "one, again"),
            ("b", "two"),
        ]
        assert store.clear("a", "k") and not store.clear("a", "k")
        assert store.current() == [ExtensionWarning("b", "k", "two", "error")]
        assert len(seen) == 4

    def test_clear_source(self):
        store = ExtensionWarnings()
        store.set(ExtensionWarning("a", "1", "x", "warning"))
        store.set(ExtensionWarning("a", "2", "y", "warning"))
        store.set(ExtensionWarning("tau", "1", "z", "warning"))

        assert store.clear_source("a")
        assert [w.source for w in store.current()] == ["tau"]
        assert not store.clear_source("a")

    def test_a_batch_calls_listeners_once_and_only_if_the_set_changed(self):
        store = ExtensionWarnings()
        store.set(ExtensionWarning("a", "1", "x", "warning"))
        seen: list[list[ExtensionWarning]] = []
        store.subscribe(seen.append)

        with store.batch():
            store.clear("a", "1")
            store.set(ExtensionWarning("a", "1", "x", "warning"))
        assert seen == []

        with store.batch():
            store.set(ExtensionWarning("a", "2", "y", "warning"))
            store.set(ExtensionWarning("a", "3", "z", "warning"))
        assert [len(s) for s in seen] == [3]

    @pytest.mark.parametrize(
        "warning",
        [
            ExtensionWarning("", "k", "m", "warning"),
            ExtensionWarning("a", "", "m", "warning"),
            ExtensionWarning("a", "k", "", "warning"),
            ExtensionWarning("a", "k", "two\nlines", "warning"),
            ExtensionWarning("a", "k", "m", "fatal"),  # type: ignore[arg-type]
        ],
    )
    def test_a_malformed_warning_is_refused(self, warning):
        with pytest.raises(ValueError):
            ExtensionWarnings().set(warning)

    @pytest.mark.parametrize(
        ("key", "message", "field"),
        [
            (7, "m", "key"),
            ("   ", "m", "key"),
            ("k", 5, "message"),
            ("k", "   ", "message"),
        ],
    )
    async def test_the_api_refuses_a_non_string_or_blank_key_or_message(
        self, tmp_path, key, message, field
    ):
        session = _session()
        await _load_warner(session, tmp_path)
        api = session._bind_extension_api("probe")
        with pytest.raises(ValueError, match=field):
            api.set_warning(key, message)

    def test_a_non_string_source_is_refused(self):
        with pytest.raises(ValueError, match="source"):
            ExtensionWarnings().set(ExtensionWarning(3, "k", "m", "warning"))  # type: ignore[arg-type]


class TestTheMarker:
    def test_none_when_empty_and_errors_counted_first(self):
        assert warnings_marker([]) is None
        assert warnings_marker([ExtensionWarning("a", "k", "m", "warning")]) == (
            "1 extension warning — /extensions"
        )
        assert warnings_marker(
            [
                ExtensionWarning("a", "1", "m", "warning"),
                ExtensionWarning("a", "2", "m", "error"),
                ExtensionWarning("b", "1", "m", "warning"),
            ]
        ) == ("1 extension error, 2 warnings — /extensions")


class TestTheSession:
    async def test_an_extension_sets_and_clears_under_its_own_name(self, tmp_path, capsys):
        session = _session()
        await _load_warner(session, tmp_path)

        assert session.extension_warnings() == [
            ExtensionWarning("warner", "excluded", "bash was left out", "error")
        ]
        assert "[τ] error: warner: bash was left out" in capsys.readouterr().err

        await session.run_extension_command("unwarn", "")
        assert session.extension_warnings() == []

    async def test_tau_writes_its_own_and_a_repeat_notifies_once(self, capsys):
        session = _session()
        session.set_extension_warning("tau", "loader", "one extension was withheld")
        session.set_extension_warning("tau", "loader", "one extension was withheld")

        assert capsys.readouterr().err.count("withheld") == 1
        assert session.extension_warnings()[0].source == "tau"

    async def test_disabling_or_reloading_an_extension_clears_its_warnings(self, tmp_path):
        session = _session()
        path = await _load_warner(session, tmp_path)
        session.set_extension_warning("tau", "keep", "unrelated")

        assert (await session.disable_extension(path)).ok
        assert [w.source for w in session.extension_warnings()] == ["tau"]

        assert (await session.reload_extension(path)).ok
        assert [w.key for w in session.extension_warnings()] == ["keep", "excluded"]

    async def test_a_swap_keeps_them_and_shows_each_once_more(self, capsys):
        session = _session()
        session.set_extension_warning("tau", "k", "m")
        capsys.readouterr()
        seen: list[list[ExtensionWarning]] = []
        session.subscribe_extension_warnings(seen.append)

        session.session_log = InMemorySessionLog()

        assert session.extension_warnings() == [ExtensionWarning("tau", "k", "m", "warning")]
        assert capsys.readouterr().err.count("tau: m") == 1
        assert seen == [], "the set did not change, so no listener runs"


class TestLoadFailures:
    async def test_a_failed_load_is_a_tau_error_that_outlives_a_swap(self, tmp_path):
        session = _session()
        broken = tmp_path / "broken.py"
        broken.write_text("raise RuntimeError('nope')\n")
        await session.load_extensions([str(broken)], discover=False, collect_explicit_errors=True)

        expected = [
            ExtensionWarning("tau", f"load:{broken}", "broken failed to load: nope", "error")
        ]
        assert session.extension_warnings() == expected

        session.session_log = InMemorySessionLog()
        assert session.extension_warnings() == expected


class TestTheRpcField:
    async def test_get_extension_state_carries_them_for_cheap_polling(self, tmp_path):
        session = _session()
        await _load_warner(session, tmp_path)

        result = await commands._handle_get_extension_state(RPCHandler(session), None, {})

        assert commands.validate_params(commands.GET_EXTENSION_STATE_RESULT_SCHEMA, result) is None
        assert [w["key"] for w in result["warnings"]] == ["excluded"]

    async def test_get_capabilities_carries_the_current_warnings(self, tmp_path):
        session = _session()
        await _load_warner(session, tmp_path)

        result = await commands._handle_get_capabilities(RPCHandler(session), None, {})

        assert commands.validate_params(commands.GET_CAPABILITIES_RESULT_SCHEMA, result) is None
        assert result["warnings"] == [
            {
                "source": "warner",
                "key": "excluded",
                "message": "bash was left out",
                "severity": "error",
            }
        ]

    def test_the_static_document_has_none_and_the_field_is_typed(self):
        doc = capabilities.build_capabilities()
        assert doc["warnings"] == []
        row = next(c for c in doc["commands"] if c["name"] == "get_capabilities")
        warnings = row["result_schema"]["properties"]["warnings"]
        assert warnings["items"] == {"$ref": "#/$defs/ExtensionWarning"}
        record = doc["$defs"]["ExtensionWarning"]
        assert set(record["properties"]) == {"source", "key", "message", "severity"}
        assert sorted(record["required"]) == ["key", "message", "severity", "source"]
        assert record["properties"]["severity"]["enum"] == ["warning", "error"]


_THREADED = """
import asyncio
import pathlib
import threading


def register(api):
    async def run(tool_call_id, params, signal, on_update, ctx):
        def from_worker():
            pathlib.Path(THREAD_FILE).write_text(str(threading.get_ident()))
            api.set_warning("backend", "the backend is failing")

        await asyncio.to_thread(from_worker)
        return {"content": [{"type": "text", "text": "ok"}]}

    api.register_tool(
        {
            "name": "probe",
            "label": "probe",
            "description": "sets a warning from a worker thread",
            "parameters": {"type": "object", "properties": {}},
            "execute": run,
        }
    )
"""


class _Stream:
    def __init__(self, final: AssistantMessage) -> None:
        self._final = final

    def __aiter__(self) -> "_Stream":
        self._done = False
        return self

    async def __anext__(self) -> Any:
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return DoneEvent(final=self._final, usage=Usage())

    async def result(self) -> AssistantMessage:
        return self._final

    def abort(self) -> None:
        pass


def _reply(content: list[Any], stop: str) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason=stop,  # type: ignore[arg-type]
        timestamp=1_700_000_000_000,
        usage=Usage(),
    )


class TestThreads:
    async def test_a_tool_setting_a_warning_from_a_worker_thread_lands_on_the_loop(self, tmp_path):
        session = _session()
        path = tmp_path / "threaded.py"
        thread_file = tmp_path / "thread.txt"
        path.write_text(f"THREAD_FILE = {str(thread_file)!r}\n{_THREADED}")
        await session.load_extensions([str(path)], discover=False)
        loop_thread = threading.get_ident()
        listened: list[int] = []
        session.subscribe_extension_warnings(lambda _: listened.append(threading.get_ident()))
        replies = iter(
            [
                _reply([ToolCall(type="toolCall", id="c1", name="probe", arguments={})], "toolUse"),
                _reply([TextContent(text="done")], "stop"),
            ]
        )

        async def fake(model, context, options=None):
            return _Stream(next(replies))

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
            await session.prompt("go")
        await asyncio.sleep(0)

        assert int(thread_file.read_text()) != loop_thread, "it was called off the loop"
        assert session.extension_warnings() == [
            ExtensionWarning("threaded", "backend", "the backend is failing", "warning")
        ]
        assert listened == [loop_thread]

    async def test_a_clear_from_a_worker_thread_lands_on_the_loop(self):
        session = _session()
        session.set_extension_warning("probe", "k", "m")
        listened: list[int] = []
        session.subscribe_extension_warnings(lambda _: listened.append(threading.get_ident()))

        await asyncio.to_thread(session.clear_extension_warning, "probe", "k")
        await asyncio.sleep(0)

        assert session.extension_warnings() == []
        assert listened == [threading.get_ident()]

    async def test_a_bad_warning_from_a_thread_is_refused_on_that_thread(self):
        session = _session()
        with pytest.raises(ValueError):
            await asyncio.to_thread(session.set_extension_warning, "probe", "k", "a\nb")

    def test_with_no_loop_anywhere_a_set_lands_at_once(self):
        session = _session()
        session.set_extension_warning("probe", "k", "m")
        assert session.extension_warnings() == [ExtensionWarning("probe", "k", "m", "warning")]

    def test_a_set_from_another_loop_between_turns_never_takes_the_session(self):
        """tectum's reproduction: one asyncio.run per turn, a warning from a loop thread between."""
        session = _session()
        replies = iter(
            [_reply([TextContent(text="one")], "stop"), _reply([TextContent(text="two")], "stop")]
        )

        async def fake(model, context, options=None):
            return _Stream(next(replies))

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
            asyncio.run(session.prompt("first"))
            bound = session._loop

            async def from_its_own_loop() -> None:
                session.set_extension_warning("probe", "backend", "down")

            worker = threading.Thread(target=lambda: asyncio.run(from_its_own_loop()))
            worker.start()
            worker.join()
            assert session._loop is bound, "a warning write must not rebind the session"

            asyncio.run(session.prompt("second"))

        assert [w.key for w in session.extension_warnings()] == ["backend"]


class TestTheReservedSource:
    async def test_an_extension_named_tau_cannot_write_warnings(self, tmp_path):
        session = _session()
        path = tmp_path / "tau.py"
        path.write_text('def register(api):\n    api.set_warning("load:x", "spoofed")\n')
        await session.load_extensions([str(path)], discover=False, collect_explicit_errors=True)

        [error] = session.get_extension_state().errors
        assert "reserved" in error.error
        assert [w.key for w in session.extension_warnings()] == [f"load:{path}"]
