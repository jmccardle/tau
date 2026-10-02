"""Cursor — a position a conversation tree is extended from (docs/CURSORS.md).

Every writer is a cursor over one shared log, and none is privileged. Context
isolation between two cursors falls out of the fold: a cursor's entries are never
ancestors of another cursor's leaf. A sub-agent's branch and a user's fork of the
same shape are therefore indistinguishable, which is correct (docs/LANE-REMOVAL.md).
"""

from __future__ import annotations

import pytest

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.cursor import Cursor
from tau_agent_core.session_log import InMemorySessionLog, default_leaf


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _texts(messages: list[dict]) -> list[str]:
    return [
        b["text"]
        for m in messages
        for b in (m.get("content") or [])
        if isinstance(b, dict) and "text" in b
    ]


@pytest.fixture
async def head() -> Cursor:
    """The head's cursor, past a shared two-message prefix."""
    cursor = Cursor.newest(InMemorySessionLog())
    await cursor.append_message(_msg("user", "shared prefix"))
    await cursor.append_message(_msg("assistant", "shared reply"))
    return cursor


async def test_a_second_cursor_shares_the_tree_and_owns_its_leaf(head):
    tip = head.leaf
    second = Cursor(head.log, tip, label="evaluate")

    assert second.session_id == head.session_id, "one conversation, two positions"
    assert second.id != head.id
    assert second.leaf == tip

    await second.append_message(_msg("user", "second cursor's work"))

    assert second.leaf != tip, "the writer moved onto its entry"
    assert head.leaf == tip, "...and the other cursor did not"
    assert len(second.entries()) == len(head.entries()), "one shared entry list"


async def test_a_cursors_context_is_the_shared_prefix_plus_its_own_work(head):
    second = Cursor(head.log, head.leaf, label="evaluate")
    await second.append_message(_msg("user", "branch question"))
    await second.append_message(_msg("assistant", "branch answer"))

    assert _texts(second.context()) == [
        "shared prefix",
        "shared reply",
        "branch question",
        "branch answer",
    ]


async def test_one_cursors_work_never_leaks_into_anothers_context(head):
    """Structural: the other cursor's entries are never ANCESTORS of this leaf."""
    second = Cursor(head.log, head.leaf, label="evaluate")
    await second.append_message(_msg("user", "SECRET branch work"))

    assert _texts(head.context()) == ["shared prefix", "shared reply"]
    assert "SECRET branch work" in _texts(
        [e["message"] for e in head.entries() if e["type"] == "message"]
    ), "it is in the shared log, not filtered out of it"


async def test_no_entry_records_which_cursor_wrote_it(head):
    """A cursor's id is runtime identity only (docs/CURSORS.md §2)."""
    second = Cursor(head.log, head.leaf, label="evaluate")
    await second.append_message(_msg("user", "branch work"))

    blob = repr(head.entries())
    assert second.id not in blob and head.id not in blob
    assert all("branchOf" not in e for e in head.entries())


async def test_a_three_way_fork_and_three_sub_agents_are_INDISTINGUISHABLE():
    """Three cursors off one node and one cursor moved back three times produce the
    same tree, so every reader must answer the same for both."""

    def _message_shape(entries):
        by_id = {e["id"]: e for e in entries}

        def _depth(entry):
            depth, parent = 0, entry.get("parentId")
            while parent is not None:
                depth += 1
                parent = by_id[parent].get("parentId")
            return depth

        return sorted(
            (_depth(e), _texts([e["message"]])[0]) for e in entries if e.get("type") == "message"
        )

    subs = Cursor.newest(InMemorySessionLog())
    fork_point = await subs.append_message(_msg("user", "shared prefix"))
    for label in ("A", "B", "C"):
        await Cursor(subs.log, fork_point, label=label).append_message(_msg("assistant", label))

    forks = Cursor.newest(InMemorySessionLog())
    root = await forks.append_message(_msg("user", "shared prefix"))
    for label in ("A", "B", "C"):
        forks.move(root)
        await forks.append_message(_msg("assistant", label))

    assert _message_shape(subs.entries()) == _message_shape(forks.entries()), "identical trees"

    subs_tree = ConversationTree(subs.entries(), default_leaf(subs.entries()))
    forks_tree = ConversationTree(forks.entries(), default_leaf(forks.entries()))
    assert _texts(subs_tree.context_for()) == _texts(forks_tree.context_for())
    assert _texts(subs_tree.context_for()) == ["shared prefix", "C"]
    assert subs_tree.subtree_text(fork_point) == forks_tree.subtree_text(root)


async def test_two_cursors_from_one_parent_have_distinct_ids(head):
    """The fan-out shape: a renderer keys each sub-agent's stream by cursor id."""
    a = Cursor(head.log, head.leaf, label="evaluator A")
    b = Cursor(head.log, head.leaf, label="evaluator B")
    assert a.id != b.id

    await a.append_message(_msg("user", "from A"))
    await b.append_message(_msg("user", "from B"))

    assert _texts(a.context()) == ["shared prefix", "shared reply", "from A"]
    assert _texts(b.context()) == ["shared prefix", "shared reply", "from B"]


async def test_another_cursors_write_landing_last_is_where_a_reload_continues(head):
    """The default leaf is the newest entry, whoever wrote it (docs/CURSORS.md §4).
    The live cursor is unaffected: only a cursor's own appends move it."""
    tip = head.leaf
    second = Cursor(head.log, tip, label="evaluate")
    landed_last = await second.append_message(_msg("assistant", "landed LAST"))

    assert default_leaf(head.entries()) == landed_last
    assert head.leaf == tip


async def test_a_cursor_move_is_not_a_reload_point(head):
    """§1.1: a sub-agent moving itself used to write a ``navigate`` that became the
    conversation's reload point. A move writes nothing now, so the newest content wins."""
    root = head.entries()[0]["id"]
    second = Cursor(head.log, root, label="sub")
    newest = await second.append_message(_msg("user", "sub"))
    second.move(root)

    assert default_leaf(head.entries()) == newest


async def test_a_move_writes_nothing_and_can_go_anywhere(head):
    second = Cursor(head.log, head.leaf, label="evaluate")
    inside = await second.append_message(_msg("assistant", "inside the branch"))
    before = head.entries()

    head.move(inside)

    assert head.entries() == before
    assert head.leaf == inside
    assert "inside the branch" in _texts(head.context())


def test_opening_at_a_dangling_leaf_raises(head):
    with pytest.raises(ValueError, match="cursor leaf"):
        Cursor(head.log, "does-not-exist")


def test_moving_to_a_dangling_entry_raises(head):
    with pytest.raises(ValueError, match="move target"):
        head.move("does-not-exist")


async def test_a_cursor_can_open_before_the_first_entry(head):
    """``leaf=None`` is legal — a branch with no inherited context at all."""
    fresh = Cursor(head.log, None, label="from scratch")
    await fresh.append_message(_msg("user", "no inherited context"))
    assert _texts(fresh.context()) == ["no inherited context"]


async def test_a_branch_summary_moves_to_the_branch_point_first(head):
    branch_point = head.entries()[0]["id"]
    summary = await head.append_branch_summary("SUMMARY", branch_point)

    by_id = {e["id"]: e for e in head.entries()}
    assert by_id[summary]["parentId"] == branch_point
    assert head.leaf == summary


async def test_a_cursors_anchors_carry_their_full_provenance(head):
    """The typed appenders live once, on Cursor; none may drop a §8 field."""
    second = Cursor(head.log, head.leaf, label="reviewer")
    keep = await second.append_message(_msg("user", "kept in the branch"))

    compaction = await second.append_compaction(
        "BRANCH SUMMARY",
        keep,
        77,
        summarizer_model_id="branch-summarizer",
        summary_usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        covered_entries=2,
        covered_tokens=31,
        config_id=None,
    )
    elide = await second.append_elide(keep, covered_entries=1, covered_tokens=12, config_id=None)

    by_id = {e["id"]: e for e in head.entries()}
    assert by_id[compaction]["summarizerModelId"] == "branch-summarizer"
    assert by_id[compaction]["summaryUsage"] == {
        "input_tokens": 5,
        "output_tokens": 2,
        "total_tokens": 7,
    }
    assert by_id[compaction]["coveredTokens"] == 31
    assert by_id[elide]["coveredEntries"] == 1
    assert by_id[elide]["coveredTokens"] == 12


async def test_subtree_text_collects_THE_WHOLE_NAMED_SUBTREE(head):
    """Bounded by descendants of the named node and nothing else
    (docs/LANE-REMOVAL.md §6.2): a sub-agent run off a node inside the region is
    part of what happened there."""
    abandoned = await head.append_message(_msg("user", "an abandoned line of thought"))
    await head.append_message(_msg("assistant", "more of the abandoned branch"))

    sub = Cursor(head.log, abandoned, label="sub-agent")
    await sub.append_message(_msg("user", "the sub-agent's notes"))
    await sub.append_message(_msg("assistant", "sub-agent scratch work"))

    text = head.tree().subtree_text(abandoned)

    assert "an abandoned line of thought" in text
    assert "more of the abandoned branch" in text
    assert "the sub-agent's notes" in text, "a descendant is a descendant"
    assert "sub-agent scratch work" in text


async def test_subtree_text_reaches_DOWN_but_never_SIDEWAYS(head):
    left = await head.append_message(_msg("user", "the region being summarized"))
    await head.append_message(_msg("assistant", "inside the region"))

    elsewhere = Cursor(head.log, None, label="unrelated")
    await elsewhere.append_message(_msg("user", "SOMEWHERE ELSE ENTIRELY"))

    text = head.tree().subtree_text(left)

    assert "the region being summarized" in text and "inside the region" in text
    assert "SOMEWHERE ELSE ENTIRELY" not in text
    assert "shared prefix" not in text, "and it never reaches up the ancestor line"
