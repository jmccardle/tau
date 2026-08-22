"""BranchView — a second cursor over one entry log (C2/W14).

The load-bearing claim of the C2 design (JMFTS-INTEGRATION-PLAN.md §9.2) is that context
isolation falls out of the EXISTING fold, for free. "For free" is a claim about
behaviour, so it gets tested rather than asserted.

Since docs/LANE-REMOVAL.md that is the ONLY mechanism: the ``branchOf`` tag is gone, so
a branch is isolated by tree shape alone — and a sub-agent's branch and a user's fork of
the same shape are, correctly, indistinguishable.
"""

from __future__ import annotations

import pytest

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import (
    BranchView,
    InMemorySessionLog,
    SessionLog,
    open_branch,
    resolve_cursor,
)


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _texts(messages: list[dict]) -> list[str]:
    return [
        b["text"]
        for m in messages
        for b in (m.get("content") or [])
        if isinstance(b, dict) and "text" in b
    ]


def _context_of(log) -> list[str]:
    return _texts(ConversationTree(log.entries(), log.cursor).context_for())


@pytest.fixture
def primary() -> InMemorySessionLog:
    log = InMemorySessionLog()
    log.append_message(_msg("user", "shared prefix"))
    log.append_message(_msg("assistant", "shared reply"))
    return log


def test_a_branch_is_a_session_log(primary):
    """It must satisfy the Protocol — an AgentSession takes it with no changes."""
    branch = open_branch(primary, primary.cursor, label="evaluate")
    assert isinstance(branch, SessionLog)
    assert isinstance(branch, BranchView)


def test_a_branch_shares_identity_and_entries_but_owns_its_cursor(primary):
    tip = primary.cursor
    branch = open_branch(primary, tip, label="evaluate")

    assert branch.id == primary.id, "a branch is a lane in one conversation, not a new one"
    assert branch.cursor == tip

    branch.append_message(_msg("user", "branch work"))

    assert branch.cursor != tip, "the branch's own leaf moved"
    assert primary.cursor == tip, "...and the PRIMARY leaf did not"
    assert len(branch.entries()) == len(primary.entries()), "one shared entry list"


def test_the_branchs_context_is_the_shared_prefix_plus_its_own_work(primary):
    """The whole reason C2 is tractable: no new context plumbing exists."""
    branch = open_branch(primary, primary.cursor, label="evaluate")
    branch.append_message(_msg("user", "branch question"))
    branch.append_message(_msg("assistant", "branch answer"))

    assert _context_of(branch) == [
        "shared prefix",
        "shared reply",  # inherited, by walking up from parent_id
        "branch question",
        "branch answer",  # its own work
    ]


def test_branch_work_never_leaks_into_the_primary_context(primary):
    """Structural, not enforced: branch entries are never ANCESTORS of the primary leaf,
    so the leaf→root walk cannot reach them — even though entries() returns them."""
    branch = open_branch(primary, primary.cursor, label="evaluate")
    branch.append_message(_msg("user", "SECRET branch work"))
    branch.append_message(_msg("assistant", "branch conclusion"))

    assert "SECRET branch work" not in _context_of(primary)
    assert _context_of(primary) == ["shared prefix", "shared reply"]

    # ...and it is genuinely in the shared log, not filtered out of it.
    assert "SECRET branch work" in _texts(
        [e["message"] for e in primary.entries() if e["type"] == "message"]
    )


def test_branch_entries_carry_no_marker(primary):
    """Nothing on disk says "a sub-agent wrote this" (docs/LANE-REMOVAL.md §4).

    The branch's identity (``branch.lane``) is in-memory, for routing its live output;
    the durable record of the branch is the SUBTREE it forms, which is exactly what a
    user's fork of the same shape leaves behind too."""
    branch = open_branch(primary, primary.cursor, label="evaluate")
    branch.append_message(_msg("user", "branch work"))

    assert all("branchOf" not in e for e in primary.entries())
    assert branch.lane, "the lane still exists — in memory, as a routing key"


def test_a_three_way_fork_and_three_sub_agents_are_INDISTINGUISHABLE(primary):
    """The §1 asymmetry, gone. Three sub-agents off one node and a three-way fork off
    one node produce the same tree, so every reader must treat them the same way.

    Before this change they did not: the sub-agents' entries carried ``branchOf`` and
    the fork's did not, so ``resolve_cursor``, ``Session.messages``, ``subtree_text``
    and the picker's counts all answered differently for two logs that are structurally
    identical. This test builds both and asserts the entries are equal modulo the
    generated ids and timestamps."""

    def _message_shape(entries):
        """Every message entry as (depth, text) — the tree modulo generated ids."""
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

    # A) three sub-agents, all rooted at the same node
    subs = InMemorySessionLog()
    subs.append_message(_msg("user", "shared prefix"))
    fork_point = subs.cursor
    for label in ("A", "B", "C"):
        open_branch(subs, fork_point, label=label).append_message(_msg("assistant", label))

    # B) a three-way fork: the user navigates back to the same node and answers again
    forks = InMemorySessionLog()
    forks.append_message(_msg("user", "shared prefix"))
    root = forks.cursor
    for label in ("A", "B", "C"):
        forks.append_navigate(root)
        forks.append_message(_msg("assistant", label))

    assert _message_shape(subs.entries()) == _message_shape(forks.entries()), "identical trees"

    # ...and, the point: identical answers from every reader of them.
    assert resolve_cursor(subs.entries()) == subs.entries()[-1]["id"]
    assert resolve_cursor(forks.entries()) == forks.entries()[-1]["id"]

    subs_tree = ConversationTree(subs.entries(), resolve_cursor(subs.entries()))
    forks_tree = ConversationTree(forks.entries(), resolve_cursor(forks.entries()))
    assert (
        _texts(subs_tree.context_for())
        == _texts(forks_tree.context_for())
        == [
            "shared prefix",
            "C",
        ]
    )
    assert subs_tree.subtree_text(fork_point) == forks_tree.subtree_text(root)


def test_two_branches_from_one_parent_get_distinct_lanes(primary):
    """The fan-out shape — several evaluators over one result. Deriving the lane id from
    parent_id would give them one identity on the live ``branch_event`` channel, and a
    frontend would interleave two sub-agents' tokens into one render lane."""
    a = open_branch(primary, primary.cursor, label="evaluator A")
    b = open_branch(primary, primary.cursor, label="evaluator B")
    assert a.lane != b.lane

    a.append_message(_msg("user", "from A"))
    b.append_message(_msg("user", "from B"))

    assert _context_of(a) == ["shared prefix", "shared reply", "from A"]
    assert _context_of(b) == ["shared prefix", "shared reply", "from B"]


def test_a_branch_append_landing_last_DOES_become_the_resolved_cursor(primary):
    """The guarantee τ dropped, made explicit (docs/LANE-REMOVAL.md §2).

    ``resolve_cursor`` is pi's rule again — last entry wins — so a crash right after a
    sub-agent's write leaves the next load parked on that branch's leaf. That is
    accepted: the transcript is visible, the tree browser moves you, nothing is
    corrupted. The LIVE cursor is unaffected, because ``append_at`` never moves the
    leaf of the view that did not write."""
    tip = primary.cursor
    branch = open_branch(primary, tip, label="evaluate")
    landed_last = branch.append_message(_msg("assistant", "branch write, landed LAST"))

    entries = primary.entries()
    assert entries[-1]["id"] == landed_last, "precondition: a branch entry really is last"

    assert resolve_cursor(entries) == landed_last, "last entry wins, whoever wrote it"
    assert primary.cursor == tip, "but the live primary cursor never moved"


def test_a_human_can_still_navigate_INTO_a_branch(primary):
    """A cursor can point anywhere in the tree; the browser must still work."""
    branch = open_branch(primary, primary.cursor, label="evaluate")
    inside = branch.append_message(_msg("assistant", "inside the branch"))

    primary.append_navigate(inside)  # an untagged, primary navigate AT a branch entry

    assert primary.cursor == inside
    assert resolve_cursor(primary.entries()) == inside
    assert "inside the branch" in _context_of(primary)


def test_open_branch_at_a_dangling_parent_raises(primary):
    with pytest.raises(ValueError, match="no such entry"):
        open_branch(primary, "does-not-exist", label="evaluate")


def test_a_branch_can_be_rooted_before_the_first_entry(primary):
    """parent_id=None is legal — a branch with no inherited context at all."""
    branch = open_branch(primary, None, label="from scratch")
    branch.append_message(_msg("user", "no inherited context"))
    assert _context_of(branch) == ["no inherited context"]


def test_summarize_branch_collects_THE_WHOLE_NAMED_SUBTREE(primary):
    """``subtree_text`` is bounded by descendants of the node the caller named, and by
    nothing else (docs/LANE-REMOVAL.md §6.2).

    A sub-agent that ran off a node inside the region IS part of what happened there, so
    summarizing that region summarizes it too. The old rule asked who wrote each entry
    and stopped at the branch — which silently returned a different region than the one
    the caller asked for, and fought the case where an extension deliberately builds one
    node out of several sibling branches."""
    abandoned = primary.append_message(_msg("user", "an abandoned line of thought"))
    primary.append_message(_msg("assistant", "more of the abandoned branch"))

    # a sub-agent runs off an entry INSIDE the abandoned region
    sub = open_branch(primary, abandoned, label="sub-agent")
    sub.append_message(_msg("user", "the sub-agent's notes"))
    sub.append_message(_msg("assistant", "sub-agent scratch work"))

    text = ConversationTree(primary.entries(), primary.cursor).subtree_text(abandoned)

    assert "an abandoned line of thought" in text
    assert "more of the abandoned branch" in text
    assert "the sub-agent's notes" in text, "a descendant is a descendant"
    assert "sub-agent scratch work" in text


def test_subtree_text_reaches_DOWN_but_never_SIDEWAYS(primary):
    """The bound that does the work: neither the line ABOVE ``from_id`` nor a subtree
    hanging off a DIFFERENT node is a descendant of it, so neither is collected —
    whoever wrote them. A sibling branch is exactly the shape the containment rule was
    trying to exclude, and the structural bound excludes it for free."""
    left = primary.append_message(_msg("user", "the region being summarized"))
    primary.append_message(_msg("assistant", "inside the region"))

    # a sibling subtree, rooted ABOVE `left` — elsewhere, not below.
    elsewhere = open_branch(primary, None, label="unrelated")
    elsewhere.append_message(_msg("user", "SOMEWHERE ELSE ENTIRELY"))

    text = ConversationTree(primary.entries(), primary.cursor).subtree_text(left)

    assert "the region being summarized" in text and "inside the region" in text
    assert "SOMEWHERE ELSE ENTIRELY" not in text
    assert "shared prefix" not in text, "and it never reaches up the ancestor line"


def test_summarizing_the_sub_agents_own_branch_still_works(primary):
    """Naming the branch root is how a spawner reads its sub-agent's verdict back."""
    branch = open_branch(primary, primary.cursor, label="sub-agent")
    root = branch.append_message(_msg("user", "the sub-agent's question"))
    branch.append_message(_msg("assistant", "the sub-agent's VERDICT"))

    text = ConversationTree(primary.entries(), primary.cursor).subtree_text(root)

    assert "the sub-agent's question" in text
    assert "the sub-agent's VERDICT" in text
