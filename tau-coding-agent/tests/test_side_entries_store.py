"""Side entries in the file store and the TUI tree browser (docs/STEP-DEBUGGER.md §3.7.2)."""

from __future__ import annotations

import pytest

from tau_agent_core.cursor import Cursor
from tau_agent_core.session_log import OpenSideEntryError, default_leaf
from tau_coding_agent import tree_browser
from tau_coding_agent.session_store import FileSessionCatalog, Session

CWD = "/srv/work/proj"


async def _session(tmp_path) -> tuple[Session, Cursor, str, str]:
    session = Session.create(CWD, "local-llm", "openai", system_prompt="sys", base_dir=tmp_path)
    cursor = Cursor.newest(session)
    user = await cursor.append_message({"role": "user", "content": "hello"})
    reply = await cursor.append_message({"role": "assistant", "content": "hi"})
    return session, cursor, user, reply


async def test_a_reloaded_file_keeps_the_side_entry_off_the_default_leaf(tmp_path):
    session, cursor, _user, reply = await _session(tmp_path)
    side = await cursor.open_side("suspension", site="turn_end")
    await cursor.finalize_side(side, site="turn_end", resolution={"action": "step"})

    reloaded = Session.load(session.path)

    final = next(e for e in reloaded.entries() if e["id"] == side)
    assert final["parentId"] == reply and "status" not in final
    assert default_leaf(reloaded.entries()) == reply


async def test_a_fork_copies_finalized_side_children_and_refuses_an_open_one(tmp_path):
    session, cursor, user, reply = await _session(tmp_path)
    verdict = await cursor.open_side("suspension")
    await cursor.finalize_side(verdict, resolution={"action": "step"})
    pending = await cursor.open_side("suspension")
    catalog = FileSessionCatalog(base_dir=tmp_path)

    with pytest.raises(OpenSideEntryError, match=pending):
        catalog.fork(session, CWD, at=reply)
    with pytest.raises(OpenSideEntryError, match=pending):
        Session.fork(session, CWD, base_dir=tmp_path)

    at_user = catalog.fork(session, CWD, at=user)
    assert user in {e["id"] for e in at_user.entries()}
    assert not {verdict, pending} & {e["id"] for e in at_user.entries()}

    await cursor.finalize_side(pending, resolution={"action": "abort"})
    forked = catalog.fork(session, CWD, at=reply)
    copied = Session.load(forked.path).entries()
    by_id = {e["id"]: e for e in copied}
    assert {verdict, pending} <= set(by_id)
    assert by_id[verdict]["parentId"] == reply
    assert default_leaf(copied) == reply
    assert [e["id"] for e in session.entries()] == [e["id"] for e in copied]


async def test_the_browser_draws_a_side_entry_as_a_mark_not_a_row(tmp_path):
    _session_, cursor, _user, reply = await _session(tmp_path)
    side = await cursor.open_side("suspension")
    roots = cursor.tree().tree()

    rows = tree_browser.plan_tree_rows(roots)

    assert side not in {row.node.id for row in rows}
    reply_row = next(row for row in rows if row.node.id == reply)
    assert not reply_row.has_children
    label = tree_browser.SessionTreeModal._label(reply_row.node)
    assert "⏸ suspension (open)" in label and label.endswith("◀ current")
    await cursor.finalize_side(side, resolution={"action": "step"})
    reply_node = next(
        r.node for r in tree_browser.plan_tree_rows(cursor.tree().tree()) if r.node.id == reply
    )
    assert "(open)" not in tree_browser.SessionTreeModal._label(reply_node)
