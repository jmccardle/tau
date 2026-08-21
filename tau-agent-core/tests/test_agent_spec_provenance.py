"""W2 — the ``agent_spec`` provenance node (NODE-ADDRESSABLE-AGENTS.md).

Because the frame (model, system prompt, tools, extensions, cwd) is invoker-owned
and unpersisted (§2 I2), a transcript read back cannot tell which spec produced
which turns. ``AgentSession`` writes a NON-AUTHORITATIVE ``agent_spec``
``customEntry`` at construction and at every runtime spec swap (``set_model``) so
that legibility gap has an answer, without the record becoming a reader that
reconstructs an agent (§5 Decision 3 — a record, never a contract).

This file exercises the AgentSession-level write; the SessionLog-algebra
properties it depends on (durable, reload-invariant, excluded from
``context_for``) are T4 in ``testing/session_log_contract.py``.
"""

from __future__ import annotations

import hashlib
import os

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession, _system_prompt_digest
from tau_agent_core.session_log import InMemorySessionLog
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


def _spec_entries(session: AgentSession) -> list[dict]:
    return [
        e
        for e in session.session_log.entries()
        if e.get("type") == "customEntry" and e.get("customType") == "agent_spec"
    ]


class TestConstructionWritesOneRecord:
    def test_exactly_one_agent_spec_entry_at_construction(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        assert len(_spec_entries(session)) == 1

    def test_it_never_reaches_model_input(self):
        """A plain customEntry — ConversationTree excludes the KIND from the fold
        (conversation_tree.py), so this needs no bespoke exclusion of its own."""
        session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        assert session.messages == []

    def test_carries_the_model_projection(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model("gpt-4o"))
        data = _spec_entries(session)[0]["data"]
        assert data["model"] == session.get_model()
        assert data["model"]["id"] == "gpt-4o"

    def test_carries_tool_names_not_tool_objects(self):
        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), tools=[_tool("read"), _tool("grep")]
        )
        data = _spec_entries(session)[0]["data"]
        assert data["tools"] == ["read", "grep"]

    def test_carries_extension_labels(self):
        def my_extension(api):
            pass

        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), extensions=[my_extension]
        )
        data = _spec_entries(session)[0]["data"]
        assert len(data["extensions"]) == 1
        assert "my_extension" in data["extensions"][0]

    def test_carries_the_process_cwd(self):
        session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        data = _spec_entries(session)[0]["data"]
        assert data["cwd"] == os.getcwd()


class TestSystemPromptIsDigestedNeverVerbatim:
    def test_digest_matches_the_documented_sha256_convention(self):
        prompt = "You are a helpful assistant with access to project secrets."
        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), system_prompt=prompt
        )
        data = _spec_entries(session)[0]["data"]
        assert data["system_prompt_digest"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        assert data["system_prompt_digest"] == _system_prompt_digest(prompt)

    def test_the_prompt_text_itself_never_appears_in_the_entry(self):
        prompt = "SECRET-PROJECT-INSTRUCTIONS-MARKER"
        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), system_prompt=prompt
        )
        entry = _spec_entries(session)[0]
        assert prompt not in str(entry)

    def test_two_different_prompts_never_collide(self):
        s1 = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), system_prompt="prompt one"
        )
        s2 = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), system_prompt="prompt two"
        )
        d1 = _spec_entries(s1)[0]["data"]["system_prompt_digest"]
        d2 = _spec_entries(s2)[0]["data"]["system_prompt_digest"]
        assert d1 != d2


class TestApiKeyNeverEntersTheTree:
    def test_api_key_absent_hashed_or_otherwise(self):
        """Absolute prohibition (W2): api_key must NEVER enter the tree, not even
        hashed or truncated."""
        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), api_key="sk-super-secret-key"
        )
        entry = _spec_entries(session)[0]
        blob = str(entry)
        assert "sk-super-secret-key" not in blob
        assert "api_key" not in entry["data"]
        # Not even a digest of it under a different name.
        key_digest = hashlib.sha256(b"sk-super-secret-key").hexdigest()
        assert key_digest not in blob


class TestSetModelIsASpecSwap:
    def test_set_model_appends_a_second_agent_spec_record(self):
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model("model-a"),
            model_resolver=lambda name: _model(name),
        )
        assert len(_spec_entries(session)) == 1

        session.set_model("model-b")

        entries = _spec_entries(session)
        assert len(entries) == 2
        assert entries[0]["data"]["model"]["id"] == "model-a"
        assert entries[1]["data"]["model"]["id"] == "model-b"

    def test_set_model_still_never_reaches_model_input(self):
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model("model-a"),
            model_resolver=lambda name: _model(name),
        )
        session.set_model("model-b")
        assert session.messages == []


class TestLoadedFileExtensionsAppearInALaterRecord:
    """Spec gap (NODE-ADDRESSABLE-AGENTS.md W2, review pass): the ``extensions``
    field used to read only ``self._extensions`` (inline factories), so a file
    extension bound via :meth:`~AgentSession.load_extensions` -- the path the
    TUI and headless actually use -- never appeared even in a record written
    AFTER it loaded. ``load_extensions`` itself is deliberately NOT a new
    re-record trigger (see ``_record_agent_spec``'s docstring for why -- it
    would falsify ``test_60_retrieval_review.py``'s
    ``test_the_fan_out_writes_nothing_to_the_tree``); this proves the field
    is correct on the trigger that already exists (``set_model``).
    """

    async def test_a_loaded_file_extension_appears_after_a_later_swap(self, tmp_path):
        ext_path = tmp_path / "my_ext.py"
        ext_path.write_text("def register(api):\n    pass\n")

        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model("model-a"),
            model_resolver=lambda name: _model(name),
        )
        result = await session.load_extensions([str(ext_path)], discover=False)
        assert len(result.extensions) == 1

        session.set_model("model-b")

        data = _spec_entries(session)[-1]["data"]
        assert str(ext_path) in data["extensions"]

    async def test_a_disabled_file_extension_drops_out_of_a_later_record(self, tmp_path):
        ext_path = tmp_path / "my_ext.py"
        ext_path.write_text("def register(api):\n    pass\n")

        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model("model-a"),
            model_resolver=lambda name: _model(name),
        )
        await session.load_extensions([str(ext_path)], discover=False)
        await session.disable_extension(str(ext_path))

        session.set_model("model-b")

        data = _spec_entries(session)[-1]["data"]
        assert str(ext_path) not in data["extensions"]

    async def test_load_extensions_alone_writes_no_new_record(self, tmp_path):
        """The deliberate non-fix, pinned: loading a file extension with no
        subsequent spec swap leaves the construction-time record as the only
        one, still showing ``extensions: []`` -- documented above, not silently
        assumed."""
        ext_path = tmp_path / "my_ext.py"
        ext_path.write_text("def register(api):\n    pass\n")

        session = AgentSession(session_log=InMemorySessionLog(), model=_model("model-a"))
        await session.load_extensions([str(ext_path)], discover=False)

        entries = _spec_entries(session)
        assert len(entries) == 1
        assert entries[0]["data"]["extensions"] == []
