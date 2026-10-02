"""A session records its frame in the tree as config entries (docs/CURSORS.md §5).

``AgentSession._record_config`` writes, before a turn's first append, the frame
keys that differ from :func:`~tau_agent_core.session_log.config_at` at the turn's
cursor: config takes effect where it is recorded, and a path that already says
the same gets nothing. ``start()`` is the door for a reader that has not prompted.
The SessionLog-algebra properties (durable, excluded from ``context_for``) are the
contract suite's.
"""

from __future__ import annotations

import hashlib
import os

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession, _system_prompt_digest
from tau_agent_core.session_log import CONFIG_ENTRY_TYPE, InMemorySessionLog, config_at
from tau_agent_core.tools.base import AgentTool, ToolDefinition


def _model(model_id: str = "model-a") -> Model:
    return Model(
        id=model_id,
        name=model_id,
        api="openai-completions",
        provider="openai",
        base_url="http://localhost",
        context_window=128000,
        max_tokens=4096,
    )


def _tool(name: str) -> AgentTool:
    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name,
            description=name,
            parameters={"type": "object", "properties": {}, "required": []},
            execute=lambda ctx: "ok",
        )
    )


async def _config_entries(session: AgentSession) -> list[dict]:
    """Record the frame, then return every config entry in the log."""
    await session.start()
    return [
        e
        for e in session.session_log.entries()
        if e.get("type") == "customEntry" and e.get("customType") == CONFIG_ENTRY_TYPE
    ]


def _effective(session: AgentSession) -> dict:
    return config_at(session.cursor.entries(), session.cursor.leaf)


class TestTheFirstRecord:
    async def test_one_config_entry_carries_the_whole_frame(self):
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model("gpt-4o"),
            tools=[_tool("read"), _tool("grep")],
            reasoning="high",
        )
        (entry,) = await _config_entries(session)
        data = entry["data"]
        assert data["model_spec"] == session.get_model()
        assert data["model_spec"]["id"] == "gpt-4o"
        assert data["tools"] == ["read", "grep"]
        assert data["thinking"] == "high"
        assert data["cwd"] == os.getcwd()

    async def test_the_cwd_given_is_the_cwd_recorded(self, tmp_path):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model(), cwd=str(tmp_path))
        assert _effective(session) == {}
        await session.start()
        assert _effective(session)["cwd"] == str(tmp_path)

    async def test_extension_labels_are_recorded(self):
        def my_extension(api):
            pass

        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), extensions=[my_extension]
        )
        (entry,) = await _config_entries(session)
        assert len(entry["data"]["extensions"]) == 1
        assert "my_extension" in entry["data"]["extensions"][0]

    def test_it_never_reaches_model_input(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        assert session.messages == []

    async def test_recording_again_with_nothing_changed_writes_nothing(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        await session.start()
        before = session.session_log.entries()
        await session.start()
        assert session.session_log.entries() == before


class TestSystemPromptIsDigestedNeverVerbatim:
    async def test_digest_matches_the_documented_sha256_convention(self):
        prompt = "You are a helpful assistant with access to project secrets."
        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), system_prompt=prompt
        )
        (entry,) = await _config_entries(session)
        digest = entry["data"]["system_prompt_digest"]
        assert digest == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        assert digest == _system_prompt_digest(prompt)

    async def test_the_prompt_text_itself_never_appears_in_the_entry(self):
        prompt = "SECRET-PROJECT-INSTRUCTIONS-MARKER"
        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), system_prompt=prompt
        )
        (entry,) = await _config_entries(session)
        assert prompt not in str(entry)


class TestApiKeyNeverEntersTheTree:
    async def test_api_key_absent_hashed_or_otherwise(self):
        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), api_key="sk-super-secret-key"
        )
        (entry,) = await _config_entries(session)
        blob = str(entry)
        assert "sk-super-secret-key" not in blob
        assert "api_key" not in entry["data"]
        assert hashlib.sha256(b"sk-super-secret-key").hexdigest() not in blob


class TestAChangeIsRecordedWhereItTakesEffect:
    async def test_set_model_records_only_what_changed_at_the_next_record(self):
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model("model-a"),
            model_resolver=lambda name: _model(name),
        )
        await session.start()

        session.set_model("model-b")
        assert _effective(session)["model_spec"]["id"] == "model-a", "nothing written yet"

        entries = await _config_entries(session)
        assert len(entries) == 2
        assert set(entries[1]["data"]) == {"model_spec"}
        assert _effective(session)["model_spec"]["id"] == "model-b"

    async def test_a_loaded_file_extension_appears_in_the_next_record(self, tmp_path):
        ext_path = tmp_path / "my_ext.py"
        ext_path.write_text("def register(api):\n    pass\n")
        session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        await session.start()

        await session.load_extensions([str(ext_path)], discover=False)
        assert str(ext_path) not in _effective(session)["extensions"], "loading writes nothing"

        await session.start()
        assert str(ext_path) in _effective(session)["extensions"]

    async def test_a_disabled_file_extension_drops_out_of_the_next_record(self, tmp_path):
        ext_path = tmp_path / "my_ext.py"
        ext_path.write_text("def register(api):\n    pass\n")
        session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        await session.load_extensions([str(ext_path)], discover=False)
        await session.start()
        await session.disable_extension(str(ext_path))

        await session.start()
        assert str(ext_path) not in _effective(session)["extensions"]

    async def test_a_cursor_moved_back_reads_the_config_in_force_there(self):
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model("model-a"),
            model_resolver=lambda name: _model(name),
        )
        await session.start()
        before_switch = session.cursor.leaf
        session.set_model("model-b")
        await session.start()

        session.cursor.move(before_switch)
        assert _effective(session)["model_spec"]["id"] == "model-a"
