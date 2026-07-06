"""SessionLog wiring — the persistence facade AgentSession depends on.

Step 1d, Decision-4 option (B): AgentSession persists through a ``SessionLog``
(read via ``ConversationTree``), not the retired System-A ``SessionManager``.
This suite covers the core half:

- ``InMemorySessionLog`` — the SDK-default log: append algebra (parentId
  chaining, cursor advance, navigate/branch_summary validation) + camelCase
  entry shape so ``ConversationTree`` folds it identically to an on-disk Session.
- The SDK default path (``create_agent_session()`` with no session) persists this
  turn's messages into that log and reads context back through ``ConversationTree``.

The live-path coverage (the coding-agent file ``Session`` injected as the
SessionLog) lives in ``tau-coding-agent/tests`` — tau-agent-core must not import
tau-coding-agent (that would be the circular import Decision 4 exists to avoid).

Reference: SESSION-TREE-IMPLEMENTATION.md §2.6, §2.7, §4.2; "Decision 4" (B).
"""

from __future__ import annotations

import asyncio

import pytest

from tau_ai.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.sdk import create_agent_session
from tau_agent_core.session_log import InMemorySessionLog, SessionLog


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
        assert log.cursor is None
        assert isinstance(log.id, str) and log.id

    def test_append_message_advances_cursor_and_chains_parent(self):
        log = InMemorySessionLog()
        id1 = log.append_message(_um("one"))
        id2 = log.append_message(_um("two"))
        entries = log.entries()
        assert [e["type"] for e in entries] == ["message", "message"]
        # cursor is the tip; parentId chains root→leaf.
        assert log.cursor == id2
        assert entries[0]["parentId"] is None
        assert entries[1]["parentId"] == id1
        # entries() returns copies — mutating them can't corrupt the log.
        entries[0]["type"] = "mutated"
        assert log.entries()[0]["type"] == "message"

    def test_append_compaction_writes_camelcase_shape(self):
        log = InMemorySessionLog()
        first = log.append_message(_um("keep"))
        log.append_compaction(summary="recap", first_kept_id=first, tokens_before=123)
        comp = log.entries()[-1]
        assert comp["type"] == "compaction"
        assert comp["summary"] == "recap"
        assert comp["firstKeptId"] == first  # camelCase, like session_store.Session
        assert comp["tokensBefore"] == 123

    def test_append_navigate_moves_leaf_to_target(self):
        log = InMemorySessionLog()
        a = log.append_message(_um("a"))
        log.append_message(_um("b"))
        nav_id = log.append_navigate(a)
        # The navigate entry parents at the previous leaf, but the cursor lands on
        # the target (not the navigate entry itself).
        assert log.cursor == a
        assert log.entries()[-1]["id"] == nav_id
        assert log.entries()[-1]["targetId"] == a

    def test_append_navigate_none_targets_pre_root(self):
        log = InMemorySessionLog()
        log.append_message(_um("a"))
        log.append_navigate(None)
        assert log.cursor is None

    def test_append_navigate_unknown_target_raises(self):
        log = InMemorySessionLog()
        log.append_message(_um("a"))
        with pytest.raises(ValueError, match="navigate target"):
            log.append_navigate("deadbeef")

    def test_append_branch_summary_validates_from_id(self):
        log = InMemorySessionLog()
        with pytest.raises(ValueError, match="branch_summary from"):
            log.append_branch_summary("s", "nope")
        a = log.append_message(_um("a"))
        bs = log.append_branch_summary("s", a)
        assert log.entries()[-1]["id"] == bs
        assert log.entries()[-1]["fromId"] == a

    def test_satisfies_sessionlog_protocol(self):
        assert isinstance(InMemorySessionLog(), SessionLog)


# ── Fold parity: context built via ConversationTree over the log entries ──────


class TestConversationTreeOverLog:
    def test_messages_fold_matches_conversation_tree(self):
        log = InMemorySessionLog()
        log.append_message(_um("first"))
        log.append_message({"role": "assistant", "content": [{"type": "text", "text": "reply"}]})
        session = AgentSession(session_log=log, model=_model())
        expected = ConversationTree(log.entries(), log.cursor).context_for()
        assert session.messages == expected
        assert [m["role"] for m in session.messages] == ["user", "assistant"]

    def test_compaction_splice_drops_prefix(self):
        log = InMemorySessionLog()
        log.append_message(_um("old"))
        keep = log.append_message(_um("keep me"))
        log.append_compaction(summary="SUM", first_kept_id=keep, tokens_before=10)
        session = AgentSession(session_log=log, model=_model())
        texts = [m["content"][0]["text"] for m in session.messages]
        assert texts == ["[[Compaction summary: SUM]]", "keep me"]
        assert "old" not in " ".join(texts)


# ── SDK default path: persists + reads through the in-memory SessionLog ───────


@pytest.mark.usefixtures("fake_llm")
class TestSdkDefaultPathPersistsAndReads:
    def test_default_session_log_is_in_memory(self):
        session = create_agent_session(model="gpt-4o")
        assert isinstance(session._session_log, InMemorySessionLog)

    def test_prompt_persists_into_the_log_and_reads_back(self):
        log = InMemorySessionLog()
        session = create_agent_session(model="gpt-4o", session_log=log)
        asyncio.run(session.prompt("hello"))

        # The turn was appended to the injected log (persist path), and context is
        # rebuilt from it via ConversationTree (read path).
        kinds = [e["type"] for e in log.entries()]
        assert kinds and all(k == "message" for k in kinds)
        assert session.messages == ConversationTree(log.entries(), log.cursor).context_for()
        roles = [m["role"] for m in session.messages]
        assert "user" in roles and "assistant" in roles
        assert session.messages[0]["content"][0]["text"] == "hello"

    def test_two_default_sessions_are_isolated(self):
        s1 = create_agent_session(model="gpt-4o")
        s2 = create_agent_session(model="gpt-4o")
        asyncio.run(s1.prompt("only in one"))
        assert len(s1.messages) > 0
        assert s2.messages == []
        assert s1._session_log.id != s2._session_log.id

    def test_state_session_id_is_the_log_uuid(self):
        log = InMemorySessionLog()
        session = create_agent_session(model="gpt-4o", session_log=log)
        assert session.state.session_id == log.id
