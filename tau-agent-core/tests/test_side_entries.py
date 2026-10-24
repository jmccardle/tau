"""Side entries: written beside a cursor's leaf, and left out by every walker (docs/STEP-DEBUGGER.md §3.7.1-§3.7.2)."""

from __future__ import annotations

import pytest
from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.conversation_tree import ConversationTree, entries_to_messages
from tau_agent_core.cursor import Cursor
from tau_agent_core.export import render_session_html
from tau_agent_core.projections import browse_rows
from tau_agent_core.session_log import (
    SIDE_KINDS,
    InMemorySessionLog,
    OpenSideEntryError,
    default_leaf,
    entries_for_fork,
    is_side_entry,
)

_TS = 1_700_000_000_000


def _msg(role: str, text: str, offset: int = 0) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}], "timestamp": _TS + offset}


async def _conversation() -> tuple[InMemorySessionLog, Cursor, str, str]:
    """A user message and its reply; returns the log, a cursor at the reply, and both ids."""
    log = InMemorySessionLog()
    cursor = Cursor(log, None)
    user = await cursor.append_message(_msg("user", "hello", 0))
    reply = await cursor.append_message(_msg("assistant", "hi", 1))
    return log, cursor, user, reply


def _by_id(log: InMemorySessionLog) -> dict[str, dict]:
    return {e["id"]: e for e in log.entries()}


def test_the_predicate_reads_the_kind_alone():
    assert SIDE_KINDS == frozenset({"suspension"})
    assert is_side_entry({"type": "suspension", "status": "incomplete"})
    assert is_side_entry({"type": "suspension"})
    assert not is_side_entry({"type": "message"})
    assert not is_side_entry({"type": "navigate"})
    assert not is_side_entry({})


async def test_open_and_finalize_side_write_under_the_leaf_and_never_move_it():
    log, cursor, _user, reply = await _conversation()

    side = await cursor.open_side("suspension", site="turn_end", spec={"title": "t"})
    opened = _by_id(log)[side]
    assert opened["parentId"] == reply and opened["status"] == "incomplete"
    assert opened["site"] == "turn_end"
    assert cursor.leaf == reply

    await cursor.finalize_side(side, site="turn_end", resolution={"action": "step"})
    final = _by_id(log)[side]
    assert "status" not in final and final["resolution"] == {"action": "step"}
    assert final["parentId"] == reply and final["type"] == "suspension"
    assert cursor.leaf == reply


async def test_open_side_refuses_a_path_kind_and_finalize_side_a_path_entry():
    log, cursor, _user, reply = await _conversation()
    with pytest.raises(ValueError, match="not a side kind"):
        await cursor.open_side("message", message=_msg("user", "x"))
    path_entry = await cursor.open("message", message=_msg("assistant", "partial"))
    with pytest.raises(ValueError, match="names no side entry"):
        await cursor.finalize_side(path_entry, message=_msg("assistant", "done"))
    assert len(log.entries()) == 3


async def test_a_cursor_never_sits_on_a_side_entry():
    log, cursor, _user, _reply = await _conversation()
    side = await cursor.open_side("suspension")
    with pytest.raises(ValueError, match="side entry"):
        cursor.move(side)
    with pytest.raises(ValueError, match="side entry"):
        Cursor(log, side)


async def test_default_leaf_skips_a_side_entry_open_and_finalized():
    log, cursor, _user, reply = await _conversation()
    side = await cursor.open_side("suspension")
    assert default_leaf(log.entries()) == reply
    await cursor.finalize_side(side, resolution={"action": "step"})
    assert default_leaf(log.entries()) == reply
    assert Cursor.newest(log).leaf == reply


async def test_the_next_append_hangs_from_the_leaf_not_the_side_entry():
    log, cursor, _user, reply = await _conversation()
    side = await cursor.open_side("suspension")
    await cursor.finalize_side(side, resolution={"action": "step"})

    nxt = await cursor.append_message(_msg("user", "again", 2))

    assert _by_id(log)[nxt]["parentId"] == reply
    tree = ConversationTree(log.entries(), nxt)
    assert side not in {e["id"] for e in tree.path()}
    assert [m["content"][0]["text"] for m in tree.context_for()] == ["hello", "hi", "again"]


async def test_the_tree_counts_no_branch_for_a_side_entry():
    log, cursor, user, reply = await _conversation()
    side = await cursor.open_side("suspension", spec={"title": "check"})
    tree = ConversationTree(log.entries(), reply)

    assert tree.children_of(reply) == []
    assert tree.side_of(reply) == [side]
    assert tree.descendants_of(user) == [reply]
    roots = tree.tree()
    reply_node = roots[0].children[0]
    assert reply_node.children == []
    assert [(m.entry_id, m.kind, m.status) for m in reply_node.side] == [
        (side, "suspension", "incomplete")
    ]
    assert [n.entry_id for n in tree.browse()] == [user, reply]
    assert entries_to_messages(tree.context_entries()) == tree.context_for()
    assert len(tree.context_for()) == 2


async def test_get_tree_rows_carry_the_mark_on_the_parent():
    log, cursor, user, reply = await _conversation()
    side = await cursor.open_side("suspension")
    rows = browse_rows(ConversationTree(log.entries(), reply))
    assert [r["entry_id"] for r in rows] == [user, reply]
    assert rows[0]["side"] == []
    assert rows[1]["side"] == [
        {
            "entry_id": side,
            "kind": "suspension",
            "status": "incomplete",
            "preview": "suspension (open)",
        }
    ]


async def test_complete_message_id_does_not_offer_a_side_entry():
    log, cursor, user, reply = await _conversation()
    side = await cursor.open_side("suspension")
    tree = ConversationTree(log.entries(), reply)
    offered = {m.entry_id for m in tree.complete_message_id("in_session").matches}
    assert offered == {user, reply}
    assert side not in {
        m.entry_id for m in tree.complete_message_id("descendants_of_leaf", leaf=user).matches
    }


async def test_fork_admission_refuses_an_open_side_child_and_a_side_entry():
    log, cursor, user, reply = await _conversation()
    side = await cursor.open_side("suspension")
    tree = ConversationTree(log.entries(), reply)
    reason = tree.fork_admission_reason(reply)
    assert reason is not None and side in reason
    assert "side entry" in (tree.fork_admission_reason(side) or "")
    assert tree.fork_admission_reason(user) is None
    await cursor.finalize_side(side, resolution={"action": "step"})
    assert ConversationTree(log.entries(), reply).fork_admission_reason(reply) is None


async def test_a_fork_copies_finalized_side_children_and_refuses_an_open_one():
    log, cursor, user, reply = await _conversation()
    verdict = await cursor.open_side("suspension")
    await cursor.finalize_side(verdict, resolution={"action": "step"})
    cursor.move(user)
    sibling = await cursor.append_message(_msg("assistant", "other", 3))
    cursor.move(reply)
    pending = await cursor.open_side("suspension")
    entries = log.entries()

    with pytest.raises(OpenSideEntryError, match=pending) as refused:
        entries_for_fork(entries, reply)
    assert refused.value.entry_id == pending

    at_user = [e["id"] for e in entries_for_fork(entries, user)]
    assert at_user == [user]

    await cursor.finalize_side(pending, resolution={"action": "abort"})
    entries = log.entries()
    assert [e["id"] for e in entries_for_fork(entries, reply)] == [user, reply, verdict, pending]
    assert sibling not in {e["id"] for e in entries_for_fork(entries, reply)}
    with pytest.raises(ValueError, match="not found"):
        entries_for_fork(entries, "nope")


async def test_a_fork_at_a_point_copies_finalized_session_marks_and_drops_open_ones():
    log, cursor, user, reply = await _conversation()
    session_level = Cursor(log, None)
    kept = await session_level.open_side("suspension")
    await session_level.finalize_side(kept, resolution={"action": "step"})
    still_open = await session_level.open_side("suspension")
    for at in (user, reply, None):
        copied = [e["id"] for e in entries_for_fork(log.entries(), at)]
        assert kept in copied and still_open not in copied


async def test_a_whole_fork_drops_an_open_side_entry_off_the_default_leaf():
    log, cursor, user, reply = await _conversation()
    cursor.move(user)
    stale = await cursor.open_side("suspension")
    cursor.move(reply)
    copied = [e["id"] for e in entries_for_fork(log.entries(), None)]
    assert copied == [user, reply] and stale not in copied
    at_leaf = await cursor.open_side("suspension")
    with pytest.raises(OpenSideEntryError, match=at_leaf):
        entries_for_fork(log.entries(), None)


async def test_html_export_counts_no_tip_for_a_side_entry():
    log, cursor, _user, reply = await _conversation()
    side = await cursor.open_side("suspension", spec={"title": "check"})
    await cursor.finalize_side(side, resolution={"action": "step"})

    page = render_session_html(log.entries())

    assert "3 in 1 branch" in page
    assert '<details class="branch">' not in page
    assert f'id="e-{side}"' in page
    assert "session continues here" in page.split(f'id="e-{side}"')[0]


async def test_ctx_leaf_id_is_the_acting_cursors_leaf():
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=Model(
            id="test-model",
            name="test-model",
            api="openai-completions",
            provider="openai",
            base_url="http://localhost",
            context_window=128000,
            max_tokens=4096,
        ),
        compaction_settings=CompactionSettings(auto=False),
    )
    ctx = session._extension_api.context
    before = ctx.leaf_id()
    assert before == session.cursor.leaf
    entry = await session.cursor.append_message(_msg("user", "x"))
    await session.cursor.open_side("suspension")
    assert ctx.leaf_id() == entry


async def test_a_side_entry_before_the_first_entry_is_a_session_mark_not_a_root():
    log = InMemorySessionLog()
    cursor = Cursor(log, None)
    side = await cursor.open_side("suspension", site="command")
    first = await cursor.append_message(_msg("user", "hello"))
    tree = ConversationTree(log.entries(), first)

    assert [n.id for n in tree.tree()] == [first]
    assert [n.entry_id for n in tree.browse()] == [first]
    assert [(m.entry_id, m.preview) for m in tree.side_marks(None)] == [(side, "suspension (open)")]
    assert default_leaf(log.entries()) == first
    page = render_session_html(log.entries())
    assert "2 in 1 branch" in page and f'id="e-{side}"' in page
    await cursor.finalize_side(side, resolution={"action": "step"})
    marks = ConversationTree(log.entries(), first).side_marks(None)
    assert [m.preview for m in marks] == ["suspension (closed)"]
