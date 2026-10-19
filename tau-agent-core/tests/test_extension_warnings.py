"""Per-session extension warnings: the store, the api, the RPC field (#60)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tau_llm.types import Model

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

        assert store.set(ExtensionWarning("a", "k", "one"))
        assert store.set(ExtensionWarning("b", "k", "two", "error"))
        assert store.set(ExtensionWarning("a", "k", "one, again"))
        assert not store.set(ExtensionWarning("a", "k", "one, again"))

        assert [(w.source, w.message) for w in store.current()] == [
            ("a", "one, again"),
            ("b", "two"),
        ]
        assert store.clear("a", "k") and not store.clear("a", "k")
        assert store.current() == [ExtensionWarning("b", "k", "two", "error")]
        assert len(seen) == 4

    def test_clear_source_and_clear_all(self):
        store = ExtensionWarnings()
        store.set(ExtensionWarning("a", "1", "x"))
        store.set(ExtensionWarning("a", "2", "y"))
        store.set(ExtensionWarning("tau", "1", "z"))

        assert store.clear_source("a")
        assert [w.source for w in store.current()] == ["tau"]
        assert store.clear_all() and not store.clear_all()

    @pytest.mark.parametrize(
        "warning",
        [
            ExtensionWarning("", "k", "m"),
            ExtensionWarning("a", "", "m"),
            ExtensionWarning("a", "k", ""),
            ExtensionWarning("a", "k", "two\nlines"),
            ExtensionWarning("a", "k", "m", "fatal"),  # type: ignore[arg-type]
        ],
    )
    def test_a_malformed_warning_is_refused(self, warning):
        with pytest.raises(ValueError):
            ExtensionWarnings().set(warning)


class TestTheMarker:
    def test_none_when_empty_and_errors_counted_first(self):
        assert warnings_marker([]) is None
        assert warnings_marker([ExtensionWarning("a", "k", "m")]) == (
            "1 extension warning — /extensions"
        )
        assert warnings_marker(
            [
                ExtensionWarning("a", "1", "m"),
                ExtensionWarning("a", "2", "m", "error"),
                ExtensionWarning("b", "1", "m"),
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

    async def test_a_swap_onto_another_tree_clears_them(self):
        session = _session()
        session.set_extension_warning("tau", "k", "m")

        session.session_log = InMemorySessionLog()

        assert session.extension_warnings() == []


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

        session.set_extension_warning("tau", "other", "gone on swap")
        session.session_log = InMemorySessionLog()
        assert session.extension_warnings() == expected


class TestTheRpcField:
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
        assert record["properties"]["severity"]["enum"] == ["warning", "error"]
