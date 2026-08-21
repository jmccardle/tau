"""SessionLog conformance — the on-disk file ``Session`` (the live TUI/headless store).

The same suite that runs against ``InMemorySessionLog`` in tau-agent-core, proving the
two agree on the entry algebra. A JMFTS-backed store will subclass this same suite.

Both an in-memory and a real on-disk Session are exercised: the on-disk one is what the
TUI and `tau -p` actually write, and it must satisfy the algebra byte-for-byte after a
reload, not merely in RAM.
"""

from __future__ import annotations

import pytest

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.testing import SessionLogContractTests
from tau_coding_agent.session_store import Session


class TestInMemorySessionContract(SessionLogContractTests):
    """The `path is None` (ephemeral, --no-session) mode."""

    def make_log(self):
        return Session.create_in_memory(cwd="/tmp", model="m", backend="openai")


class TestOnDiskSessionContract(SessionLogContractTests):
    """The real JSONL store. Needs a tmp dir, so it overrides the fixture."""

    def make_log(self):  # pragma: no cover - overridden by the fixture below
        raise NotImplementedError

    @pytest.fixture
    def log(self, tmp_path):
        return Session.create(cwd="/tmp", model="m", backend="openai", base_dir=tmp_path)

    def reload(self, log):
        """Re-read the session from its JSONL — this is what `tau --resume` does."""
        return Session.load(log.path)


class TestOnDiskReloadRoundTrip:
    """The property a database store must also satisfy: reload == same tree."""

    def test_entries_and_cursor_survive_a_reload(self, tmp_path):
        session = Session.create(cwd="/tmp", model="m", backend="openai", base_dir=tmp_path)
        a = session.append_message({"role": "user", "content": "one"})
        session.append_message({"role": "assistant", "content": "abandoned"})
        session.append_navigate(a)
        session.append_message({"role": "assistant", "content": "kept"})

        expected_entries = session.entries()
        expected_cursor = session.cursor
        expected_context = ConversationTree(expected_entries, expected_cursor).context_for()

        reloaded = Session.load(session.path)

        assert reloaded.entries() == expected_entries
        assert reloaded.cursor == expected_cursor
        assert (
            ConversationTree(reloaded.entries(), reloaded.cursor).context_for() == expected_context
        )
