"""SessionLog — the storage seam, and the entry algebra every store agrees on.

- ``InMemorySessionLog``: ``append_at`` parenting, camelCase entry shape, deep
  copies, and no position of its own (docs/CURSORS.md §3).
- ``default_leaf``, ``config_entry_at``, ``session_name``: the pure reads
  every store shares.
- The SDK default path persists a turn into that log and reads context back
  through the session's cursor.

The cursor itself is ``test_cursor.py``. The live-path coverage (the file
``Session``) lives in ``tau-coding-agent/tests``.
"""

from __future__ import annotations

import asyncio

import pytest

from tau_llm.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.cursor import Cursor
from tau_agent_core.sdk import create_agent_session
from tau_agent_core.session_log import (
    InMemorySessionLog,
    config_at,
    config_entry_at,
    SessionLog,
    default_leaf,
    normalize_loaded_entries,
    session_name,
)

_PROV = {
    "summarizer_model_id": "test-summarizer",
    "summary_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    "covered_entries": 1,
    "covered_tokens": 50,
    "config_id": None,
}


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _um(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


# ── InMemorySessionLog unit behaviour ────────────────────────────────────────


class TestInMemorySessionLog:
    def test_fresh_log_is_empty(self):
        log = InMemorySessionLog()
        assert log.entries() == []
        assert default_leaf(log.entries()) is None
        assert isinstance(log.id, str) and log.id

    async def test_append_at_parents_where_told_and_returns_a_fresh_id(self):
        log = InMemorySessionLog()
        id1 = await log.append_at(None, "message", {"message": _um("one")})
        id2 = await log.append_at(id1, "message", {"message": _um("two")})
        entries = log.entries()
        assert [e["type"] for e in entries] == ["message", "message"]
        assert entries[0]["parentId"] is None
        assert entries[1]["parentId"] == id1
        assert id1 != id2
        entries[0]["type"] = "mutated"
        assert log.entries()[0]["type"] == "message", "entries() is a copy"

    async def test_append_at_an_unknown_parent_raises(self):
        log = InMemorySessionLog()
        with pytest.raises(ValueError, match="append parent"):
            await log.append_at("deadbeef", "message", {"message": _um("a")})

    async def test_compaction_written_through_a_cursor_is_camelcase(self):
        log = InMemorySessionLog()
        cursor = Cursor.newest(log)
        first = await cursor.append_message(_um("keep"))
        await cursor.append_compaction(
            summary="recap", first_kept_id=first, tokens_before=123, **_PROV
        )
        comp = log.entries()[-1]
        assert comp["type"] == "compaction"
        assert comp["summary"] == "recap"
        assert comp["firstKeptId"] == first  # camelCase, like session_store.Session
        assert comp["tokensBefore"] == 123

    def test_satisfies_sessionlog_protocol(self):
        assert isinstance(InMemorySessionLog(), SessionLog)


class TestDefaultLeaf:
    """Where a reopened tree continues (docs/CURSORS.md §4)."""

    async def test_it_is_the_newest_entry(self):
        log = InMemorySessionLog()
        a = await log.append_at(None, "message", {"message": _um("a")})
        b = await log.append_at(a, "message", {"message": _um("b")})
        assert default_leaf(log.entries()) == b

    async def test_a_legacy_navigate_is_skipped(self):
        log = InMemorySessionLog()
        a = await log.append_at(None, "message", {"message": _um("a")})
        b = await log.append_at(a, "message", {"message": _um("b")})
        await log.append_at(b, "navigate", {"targetId": a})
        assert default_leaf(log.entries()) == b

    def test_a_log_of_only_navigates_has_none(self):
        assert default_leaf([{"type": "navigate", "id": "n1", "targetId": None}]) is None


class TestSessionName:
    async def test_the_newest_session_info_in_append_order_names_the_session(self):
        log = InMemorySessionLog()
        cursor = Cursor.newest(log)
        await cursor.append("session_info", name="first")
        fork = await cursor.append_message(_um("shared"))
        await cursor.append("session_info", name="second")
        cursor.move(fork)
        await cursor.append_message(_um("on another branch"))
        assert session_name(log.entries()) == "second", "a name belongs to the session"

    def test_an_unnamed_session_has_none(self):
        assert session_name([]) is None


class TestAgentSpecInForce:
    """TREE-BROWSER-AS-EDITOR.md §8.3 — the frame a splice anchor points at."""

    async def test_it_finds_the_nearest_agent_spec_ancestor(self):
        log = InMemorySessionLog()
        cursor = Cursor.newest(log)
        await cursor.append_custom_entry("agent_spec", {"model": {"id": "first"}})
        await cursor.append_message(_um("under the first spec"))
        second = await cursor.append_custom_entry("agent_spec", {"model": {"id": "second"}})
        leaf = await cursor.append_message(_um("under the second spec"))

        assert config_entry_at(log.entries(), leaf) == second

    async def test_a_spec_on_a_sibling_branch_never_governs_this_path(self):
        """Ancestry, not load order. A ``set_model`` on an abandoned branch is
        chronologically the most recent ``agent_spec`` in the log and governed
        nothing on this leaf's path — the distinction docs/LANE-REMOVAL.md §1
        removed the ``branchOf`` tag over."""
        log = InMemorySessionLog()
        cursor = Cursor.newest(log)
        mine = await cursor.append_custom_entry("agent_spec", {"model": {"id": "mine"}})
        fork_point = await cursor.append_message(_um("shared prefix"))
        leaf = await cursor.append_message(_um("my continuation"))

        cursor.move(fork_point)
        await cursor.append_custom_entry("agent_spec", {"model": {"id": "the other branch"}})
        await cursor.append_message(_um("their continuation"))

        assert config_entry_at(log.entries(), leaf) == mine

    async def test_a_log_with_no_agent_spec_answers_none(self):
        """An honest absence — a pi-imported log, or a store driven without an
        AgentSession, has no such node. §11.3's "no defaults" rule is what keeps
        this answer distinct from a caller that never looked."""
        log = InMemorySessionLog()
        leaf = await Cursor.newest(log).append_message(_um("no frame was ever recorded"))

        assert config_entry_at(log.entries(), leaf) is None
        assert config_entry_at(log.entries(), None) is None

    async def test_a_non_agent_spec_custom_entry_is_not_mistaken_for_one(self):
        log = InMemorySessionLog()
        cursor = Cursor.newest(log)
        await cursor.append_custom_entry("jmfts:document", {"docId": "42"})
        leaf = await cursor.append_message(_um("hello"))

        assert config_entry_at(log.entries(), leaf) is None


# ── Fold parity: context built via ConversationTree over the log entries ──────


class TestConversationTreeOverLog:
    async def test_messages_fold_matches_the_cursor_context(self):
        log = InMemorySessionLog()
        cursor = Cursor.newest(log)
        await cursor.append_message(_um("first"))
        await cursor.append_message(
            {"role": "assistant", "content": [{"type": "text", "text": "reply"}]}
        )
        session = AgentSession(session_log=log, model=_model())
        assert session.messages == cursor.context()
        assert [m["role"] for m in session.messages] == ["user", "assistant"]

    async def test_compaction_splice_drops_prefix(self):
        log = InMemorySessionLog()
        cursor = Cursor.newest(log)
        await cursor.append_message(_um("old"))
        keep = await cursor.append_message(_um("keep me"))
        await cursor.append_compaction(summary="SUM", first_kept_id=keep, tokens_before=10, **_PROV)
        session = AgentSession(session_log=log, model=_model())
        texts = [m["content"][0]["text"] for m in session.messages]
        assert texts == ["[[Compaction summary: SUM]]", "keep me"]
        assert "old" not in " ".join(texts)


# ── SDK default path: persists + reads through the in-memory SessionLog ───────


@pytest.mark.usefixtures("fake_llm")
class TestSdkDefaultPathPersistsAndReads:
    def test_default_session_log_is_in_memory(self):
        session = create_agent_session(model="gpt-4o")
        assert isinstance(session.session_log, InMemorySessionLog)

    def test_prompt_persists_into_the_log_and_reads_back(self):
        log = InMemorySessionLog()
        session = create_agent_session(model="gpt-4o", session_log=log)
        asyncio.run(session.prompt("hello"))

        kinds = [e["type"] for e in log.entries()]
        assert kinds[0] == "customEntry"
        assert kinds[1:] and all(k == "message" for k in kinds[1:])
        assert session.messages == session.cursor.context()
        roles = [m["role"] for m in session.messages]
        assert "user" in roles and "assistant" in roles
        assert session.messages[0]["content"][0]["text"] == "hello"

    def test_two_default_sessions_are_isolated(self):
        s1 = create_agent_session(model="gpt-4o")
        s2 = create_agent_session(model="gpt-4o")
        asyncio.run(s1.prompt("only in one"))
        assert len(s1.messages) > 0
        assert s2.messages == []
        assert s1.session_log.id != s2.session_log.id

    def test_state_session_id_is_the_log_uuid(self):
        log = InMemorySessionLog()
        session = create_agent_session(model="gpt-4o", session_log=log)
        assert session.state.session_id == log.id


class TestEntryTimestampIsTheEventTime:
    """An entry's ``timestamp`` says when the event happened, not when the log
    was written — a whole turn persists in one pass, so the write time collapses
    every completion onto one millisecond (docs/MESSAGE-TIMESTAMPS.md §1)."""

    async def test_message_timestamp_drives_the_entry(self):
        log = InMemorySessionLog()
        await Cursor.newest(log).append_message(
            {"role": "user", "content": "hi", "timestamp": 1_700_000_000_000}
        )
        entry = log.entries()[-1]
        assert entry["timestamp"] == "2023-11-14T22:13:20.000Z"

    async def test_one_turn_persisted_at_once_keeps_distinct_entry_times(self):
        """The defect this fixes: four completions written in one pass used to
        share a millisecond, so nothing downstream could order or time them."""
        log = InMemorySessionLog()
        cursor = Cursor.newest(log)
        stamps = [1_700_000_000_000, 1_700_000_004_000, 1_700_000_009_000]
        for stamp in stamps:
            await cursor.append_message({"role": "assistant", "content": [], "timestamp": stamp})
        written = [e["timestamp"] for e in log.entries()]
        assert len(set(written)) == 3
        assert written == sorted(written)

    async def test_an_entry_with_no_event_clock_takes_the_write_time(self):
        """A compaction has no event of its own; so does a message carrying None."""
        log = InMemorySessionLog()
        await Cursor.newest(log).append_message(
            {"role": "assistant", "content": [], "timestamp": None}
        )
        assert log.entries()[-1]["timestamp"].endswith("Z")


class TestNormalizeLoadedEntries:
    """The ONE place a stored 0 is interpreted (docs/MESSAGE-TIMESTAMPS.md §2)."""

    def test_legacy_assistant_zero_becomes_none(self):
        entries = [{"type": "message", "message": {"role": "assistant", "timestamp": 0}}]
        assert normalize_loaded_entries(entries)[0]["message"]["timestamp"] is None

    def test_real_timestamps_are_untouched(self):
        entries = [
            {"type": "message", "message": {"role": "assistant", "timestamp": 1699999999999}}
        ]
        assert normalize_loaded_entries(entries)[0]["message"]["timestamp"] == 1699999999999

    def test_a_user_zero_is_left_alone(self):
        """Only ``openai-completions`` fabricated a zero, and only on assistant
        messages; rewriting any other role would invent a claim about the data."""
        entries = [{"type": "message", "message": {"role": "user", "timestamp": 0}}]
        assert normalize_loaded_entries(entries)[0]["message"]["timestamp"] == 0

    def test_a_non_message_entry_is_left_alone(self):
        entries = [{"type": "navigate", "timestamp": "2026-01-01T00:00:00.000Z"}]
        assert normalize_loaded_entries(entries)[0]["timestamp"] == "2026-01-01T00:00:00.000Z"


def test_default_leaf_skips_a_document_another_system_put_in_the_tree():
    """A store may surface a foreign document (``jmfts:document``); no cursor wrote it."""
    entries = [
        {"type": "message", "id": "m1", "parentId": None},
        {"type": "jmfts:document", "id": "d1", "parentId": "m1"},
    ]
    assert default_leaf(entries) == "m1"


class TestConfigAt:
    """docs/CURSORS.md §5: config is the fold of config entries on a path."""

    async def test_later_entries_override_earlier_ones_key_by_key(self):
        cursor = Cursor.newest(InMemorySessionLog())
        await cursor.append_config(model="a", backend="openai", thinking="low")
        await cursor.append_message(_um("hi"))
        await cursor.append_config(model="b")
        assert config_at(cursor.entries(), cursor.leaf) == {
            "model": "b",
            "backend": "openai",
            "thinking": "low",
        }

    async def test_a_sibling_branchs_config_never_governs_this_path(self):
        cursor = Cursor.newest(InMemorySessionLog())
        await cursor.append_config(model="a")
        fork = await cursor.append_message(_um("shared"))
        mine = await cursor.append_message(_um("mine"))
        cursor.move(fork)
        await cursor.append_config(model="b")
        assert config_at(cursor.entries(), mine)["model"] == "a"
        assert config_at(cursor.entries(), cursor.leaf)["model"] == "b"

    async def test_legacy_kinds_are_read_as_config(self):
        log = InMemorySessionLog()
        root = await log.append_at(None, "model_change", {"model": "m", "backend": "openai"})
        thought = await log.append_at(root, "thinking_change", {"level": "high"})
        spec = await log.append_at(
            thought,
            "customEntry",
            {
                "customType": "agent_spec",
                "data": {"model": {"id": "m-id"}, "tools": ["read"], "cwd": "/repo"},
            },
        )
        assert config_at(log.entries(), spec) == {
            "model": "m",
            "backend": "openai",
            "thinking": "high",
            "model_spec": {"id": "m-id"},
            "tools": ["read"],
            "cwd": "/repo",
        }
        assert config_entry_at(log.entries(), spec) == spec

    async def test_an_unknown_config_key_is_refused(self):
        cursor = Cursor.newest(InMemorySessionLog())
        with pytest.raises(ValueError, match="unknown key"):
            await cursor.append_config(api_key="nope")

    def test_a_path_with_no_config_has_none(self):
        assert config_at([], None) == {}
        assert config_entry_at([], None) is None
