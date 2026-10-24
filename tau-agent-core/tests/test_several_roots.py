"""A session holding a main conversation, fork children and fresh-root children: every walker sees all of it."""

from __future__ import annotations

import pytest
from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.export import render_session_html
from tau_agent_core.projections import browse_rows
from tau_agent_core.cursor import Cursor
from tau_agent_core.session_log import InMemorySessionLog, default_leaf
from tau_agent_core.submission import Submission


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


def _model() -> Model:
    return Model(
        id="test-model",
        name="test-model",
        api="openai-completions",
        provider="openai",
        base_url="http://localhost",
        context_window=128000,
        max_tokens=4096,
    )


@pytest.mark.usefixtures("fake_llm")
async def test_agent_written_entries_carry_source_and_no_other_entry_does():
    session, branch_point, main_leaf = await _session()
    entries = session.session_log.entries()
    main_path = {e["id"] for e in ConversationTree(entries, main_leaf).path()}

    for entry in entries:
        if entry["id"] in main_path:
            assert "source" not in entry, entry
        else:
            assert entry["source"] == "agent", entry
    child_configs = [
        e for e in entries if e.get("customType") == "config" and e["id"] not in main_path
    ]
    assert len(child_configs) == 2, "each fresh root records its frame, stamped"


@pytest.mark.usefixtures("fake_llm")
async def test_a_session_an_agent_child_wrote_last_reopens_in_the_main_conversation():
    session, _branch_point, main_leaf = await _session()
    entries = session.session_log.entries()
    assert entries[-1]["source"] == "agent", "a child wrote last"
    assert default_leaf(entries) == main_leaf
    assert Cursor.newest(session.session_log).leaf == main_leaf


@pytest.mark.usefixtures("fake_llm")
async def test_a_human_who_continues_in_a_child_branch_reopens_there():
    session, _branch_point, _main_leaf = await _session()
    child_leaf = session.session_log.entries()[-1]["id"]
    session.cursor.move(child_leaf)
    await session.prompt("a person typed here")
    human_leaf = session.cursor.leaf
    assert await session.spawn(None, "a later child", tools=[]) is not None

    entries = session.session_log.entries()
    assert entries[-1]["source"] == "agent"
    assert default_leaf(entries) == human_leaf
    assert "source" not in next(e for e in entries if e["id"] == human_leaf)


@pytest.mark.usefixtures("fake_llm")
async def test_a_session_holding_only_agent_branches_falls_back_to_the_newest():
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        compaction_settings=CompactionSettings(auto=False),
    )
    assert (await session.spawn(None, "only child", tools=[])).ok
    entries = session.session_log.entries()
    assert all(e["source"] == "agent" for e in entries)
    assert default_leaf(entries) == entries[-1]["id"]


def _submission(text: str, source: str) -> Submission:
    return Submission(text=text, source=source, submitter="test", submission_id=text)  # type: ignore[arg-type]


@pytest.mark.usefixtures("fake_llm")
async def test_an_agent_loop_on_the_main_cursor_is_not_passed_over():
    session, _branch_point, _main_leaf = await _session()
    for n in range(3):
        result = await session.submit(_submission(f"continue {n}", "agent"))
        assert result.accepted
    loop_leaf = session.cursor.leaf
    assert await session.spawn(None, "a child after the loop", tools=[]) is not None

    entries = session.session_log.entries()
    assert entries[-1]["source"] == "agent"
    assert default_leaf(entries) == loop_leaf
    loop_path = ConversationTree(entries, loop_leaf).path()
    assert all("source" not in e for e in loop_path), "the head's writes are never stamped"


@pytest.mark.usefixtures("fake_llm")
async def test_a_persons_turn_on_a_child_cursor_is_not_stamped():
    session, branch_point, _main_leaf = await _session()
    child = await session.open_cursor(branch_point, owner=session.cursor, label="side")
    result = await session.submit(
        _submission("a person, on a child cursor", "interactive"), cursor=child
    )
    assert result.accepted
    written = ConversationTree(session.session_log.entries(), child.leaf).path()
    assert written[-1]["id"] == child.leaf
    assert "source" not in written[-1] and "source" not in written[-2]


@pytest.mark.usefixtures("fake_llm")
async def test_a_client_turn_and_an_idle_write_on_a_child_cursor_are_not_stamped():
    session, branch_point, _main_leaf = await _session()
    child = await session.open_cursor(branch_point, owner=session.cursor, label="client")
    result = await session.submit(_submission("a client prompt, unsourced", "rpc"), cursor=child)
    assert result.accepted
    turn = ConversationTree(session.session_log.entries(), child.leaf).path()[-2:]
    assert all("source" not in e for e in turn)

    config = await child.append_config(model="other")
    entries = session.session_log.entries()
    assert "source" not in next(e for e in entries if e["id"] == config)
    assert default_leaf(entries) == config, "a reopen lands on the new config"
