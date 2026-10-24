"""A session holding a main conversation, fork children and fresh-root children: every walker sees all of it."""

from __future__ import annotations

import pytest
from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.export import render_session_html
from tau_agent_core.projections import browse_rows
from tau_agent_core.session_log import InMemorySessionLog


async def _session() -> tuple[AgentSession, str, str]:
    """Main conversation of two turns, two fork children at its first reply, two fresh roots."""
    model = Model(
        id="test-model",
        name="test-model",
        api="openai-completions",
        provider="openai",
        base_url="http://localhost",
        context_window=128000,
        max_tokens=4096,
    )
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=model,
        compaction_settings=CompactionSettings(auto=False),
    )
    await session.prompt("main one")
    branch_point = session.cursor.leaf
    assert branch_point is not None
    await session.prompt("main two")
    main_leaf = session.cursor.leaf
    assert main_leaf is not None
    for n in (1, 2):
        assert (await session.spawn(branch_point, f"fork child {n}", tools=[])).ok
    for n in (1, 2):
        assert (await session.spawn(None, f"root child {n}", tools=[])).ok
    return session, branch_point, main_leaf


@pytest.mark.usefixtures("fake_llm")
async def test_every_walker_sees_every_root_and_branch():
    session, branch_point, main_leaf = await _session()
    entries = session.session_log.entries()
    tree = ConversationTree(entries, main_leaf)

    roots = tree.tree()
    assert len(roots) == 3
    assert len(tree.children_of(branch_point)) == 3
    browsed = [n.entry_id for n in tree.browse()]
    assert sorted(browsed) == sorted(e["id"] for e in entries)
    assert [r["entry_id"] for r in browse_rows(tree)] == browsed
    assert tree.complete_message_id("in_session", limit=100).total == len(entries)
    assert [m["content"][0]["text"] for m in tree.context_for() if m["role"] == "user"] == [
        "main one",
        "main two",
    ]

    page = render_session_html(entries, leaf=main_leaf)
    assert f"{len(entries)} in 5 branches" in page
    assert page.count('<details class="branch">') == 4
