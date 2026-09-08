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

from tau_llm.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.sdk import create_agent_session
from tau_agent_core.session_log import (
    InMemorySessionLog,
    SessionLog,
    agent_spec_in_force,
    normalize_loaded_entries,
    open_branch,
)

_PROV = {
    "summarizer_model_id": "test-summarizer",
    "summary_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    "covered_entries": 1,
    "covered_tokens": 50,
    "agent_spec_id": None,
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
        log.append_compaction(summary="recap", first_kept_id=first, tokens_before=123, **_PROV)
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


class TestAgentSpecInForce:
    """TREE-BROWSER-AS-EDITOR.md §8.3 — the frame a splice anchor points at."""

    def test_it_finds_the_nearest_agent_spec_ancestor(self):
        log = InMemorySessionLog()
        log.append_custom_entry("agent_spec", {"model": {"id": "first"}})
        log.append_message(_um("under the first spec"))
        second = log.append_custom_entry("agent_spec", {"model": {"id": "second"}})
        leaf = log.append_message(_um("under the second spec"))

        assert agent_spec_in_force(log.entries(), leaf) == second

    def test_a_spec_on_a_sibling_branch_never_governs_this_path(self):
        """Ancestry, not load order. A ``set_model`` on an abandoned branch is
        chronologically the most recent ``agent_spec`` in the log and governed
        nothing on this leaf's path — the distinction docs/LANE-REMOVAL.md §1
        removed the ``branchOf`` tag over."""
        log = InMemorySessionLog()
        mine = log.append_custom_entry("agent_spec", {"model": {"id": "mine"}})
        fork_point = log.append_message(_um("shared prefix"))
        leaf = log.append_message(_um("my continuation"))

        log.append_navigate(fork_point)
        log.append_custom_entry("agent_spec", {"model": {"id": "the other branch"}})
        log.append_message(_um("their continuation"))

        assert agent_spec_in_force(log.entries(), leaf) == mine

    def test_a_log_with_no_agent_spec_answers_none(self):
        """An honest absence — a pi-imported log, or a store driven without an
        AgentSession, has no such node. §11.3's "no defaults" rule is what keeps
        this answer distinct from a caller that never looked."""
        log = InMemorySessionLog()
        leaf = log.append_message(_um("no frame was ever recorded"))

        assert agent_spec_in_force(log.entries(), leaf) is None
        assert agent_spec_in_force(log.entries(), None) is None

    def test_a_non_agent_spec_custom_entry_is_not_mistaken_for_one(self):
        log = InMemorySessionLog()
        log.append_custom_entry("jmfts:document", {"docId": "42"})
        leaf = log.append_message(_um("hello"))

        assert agent_spec_in_force(log.entries(), leaf) is None


class TestBranchViewRecordsAnchorProvenance:
    """A branch adds nothing to the provenance and must subtract nothing.

    ``BranchView`` is the SessionLog implementation with no storage of its own, so
    it is the one that could plausibly forward a widened call by dropping the new
    keywords and still look correct — its writes land in the underlying log either
    way, just without §8's fields.
    """

    def test_a_branchs_compaction_carries_the_full_provenance(self):
        log = InMemorySessionLog()
        root = log.append_message(_um("shared"))
        branch = open_branch(log, root, label="reviewer")
        keep = branch.append_message(_um("kept in the lane"))

        anchor_id = branch.append_compaction(
            "LANE SUMMARY",
            keep,
            77,
            summarizer_model_id="lane-summarizer",
            summary_usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            covered_entries=2,
            covered_tokens=31,
            agent_spec_id=None,
        )

        anchor = next(e for e in log.entries() if e["id"] == anchor_id)
        assert anchor["summarizerModelId"] == "lane-summarizer"
        assert anchor["summaryUsage"] == {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}
        assert anchor["coveredEntries"] == 2
        assert anchor["coveredTokens"] == 31
        assert anchor["agentSpecId"] is None

    def test_a_branchs_elide_carries_its_span(self):
        log = InMemorySessionLog()
        root = log.append_message(_um("shared"))
        branch = open_branch(log, root, label="reviewer")
        keep = branch.append_message(_um("kept in the lane"))

        anchor_id = branch.append_elide(
            keep, covered_entries=1, covered_tokens=12, agent_spec_id=None
        )

        anchor = next(e for e in log.entries() if e["id"] == anchor_id)
        assert anchor["coveredEntries"] == 1
        assert anchor["coveredTokens"] == 12


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
        log.append_compaction(summary="SUM", first_kept_id=keep, tokens_before=10, **_PROV)
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

        kinds = [e["type"] for e in log.entries()]
        assert kinds[0] == "customEntry"
        assert kinds[1:] and all(k == "message" for k in kinds[1:])
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


class TestEntryTimestampIsTheEventTime:
    """An entry's ``timestamp`` says when the event happened, not when the log
    was written — a whole turn persists in one pass, so the write time collapses
    every completion onto one millisecond (docs/MESSAGE-TIMESTAMPS.md §1)."""

    def test_message_timestamp_drives_the_entry(self):
        log = InMemorySessionLog()
        log.append_message({"role": "user", "content": "hi", "timestamp": 1_700_000_000_000})
        entry = log.entries()[-1]
        assert entry["timestamp"] == "2023-11-14T22:13:20.000Z"

    def test_one_turn_persisted_at_once_keeps_distinct_entry_times(self):
        """The defect this fixes: four completions written in one pass used to
        share a millisecond, so nothing downstream could order or time them."""
        log = InMemorySessionLog()
        stamps = [1_700_000_000_000, 1_700_000_004_000, 1_700_000_009_000]
        for stamp in stamps:
            log.append_message({"role": "assistant", "content": [], "timestamp": stamp})
        written = [e["timestamp"] for e in log.entries()]
        assert len(set(written)) == 3
        assert written == sorted(written)

    def test_an_entry_with_no_event_clock_takes_the_write_time(self):
        """A navigate has no event of its own; so does a message carrying None."""
        log = InMemorySessionLog()
        log.append_message({"role": "assistant", "content": [], "timestamp": None})
        assert log.entries()[-1]["timestamp"].endswith("Z")


class TestNormalizeLoadedEntries:
    """The ONE place a stored 0 is interpreted (docs/MESSAGE-TIMESTAMPS.md §2)."""

    def test_legacy_assistant_zero_becomes_none(self):
        entries = [{"type": "message", "message": {"role": "assistant", "timestamp": 0}}]
        assert normalize_loaded_entries(entries)[0]["message"]["timestamp"] is None

    def test_real_timestamps_are_untouched(self):
        entries = [{"type": "message", "message": {"role": "assistant", "timestamp": 1699999999999}}]
        assert normalize_loaded_entries(entries)[0]["message"]["timestamp"] == 1699999999999

    def test_a_user_zero_is_left_alone(self):
        """Only ``openai-completions`` fabricated a zero, and only on assistant
        messages; rewriting any other role would invent a claim about the data."""
        entries = [{"type": "message", "message": {"role": "user", "timestamp": 0}}]
        assert normalize_loaded_entries(entries)[0]["message"]["timestamp"] == 0

    def test_a_non_message_entry_is_left_alone(self):
        entries = [{"type": "navigate", "timestamp": "2026-01-01T00:00:00.000Z"}]
        assert normalize_loaded_entries(entries)[0]["timestamp"] == "2026-01-01T00:00:00.000Z"
