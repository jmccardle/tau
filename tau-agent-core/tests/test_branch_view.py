"""BranchView — a second cursor over one entry log (C2/W14).

The load-bearing claims of the C2 design (JMFTS-INTEGRATION-PLAN.md §9.2) are that
context isolation and cursor discipline fall out of the EXISTING fold, for free. "For
free" is a claim about behaviour, so it gets tested rather than asserted.
"""

from __future__ import annotations

import pytest

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import (
    LANE_KEY,
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


def test_branch_entries_are_lane_tagged(primary):
    branch = open_branch(primary, primary.cursor, label="evaluate")
    branch.append_message(_msg("user", "branch work"))

    tagged = [e for e in primary.entries() if LANE_KEY in e]
    assert len(tagged) == 1
    assert tagged[0][LANE_KEY] == branch.lane
    assert all(LANE_KEY not in e for e in primary.entries() if e["id"] not in {tagged[0]["id"]})


def test_two_branches_from_one_parent_get_distinct_lanes(primary):
    """The fan-out shape — several evaluators over one result. Deriving the lane id from
    parent_id would tag them identically and make them indistinguishable in the log."""
    a = open_branch(primary, primary.cursor, label="evaluator A")
    b = open_branch(primary, primary.cursor, label="evaluator B")
    assert a.lane != b.lane

    a.append_message(_msg("user", "from A"))
    b.append_message(_msg("user", "from B"))

    lanes = {e[LANE_KEY] for e in primary.entries() if LANE_KEY in e}
    assert lanes == {a.lane, b.lane}
    assert _context_of(a) == ["shared prefix", "shared reply", "from A"]
    assert _context_of(b) == ["shared prefix", "shared reply", "from B"]


def test_a_branch_append_landing_last_does_not_move_the_primary_cursor_on_reload(primary):
    """THE cursor-discipline test. A sub-agent's write landing last before a crash must
    not make the next load resume inside the branch — that would silently continue the
    primary conversation from a sub-agent's lane."""
    tip = primary.cursor
    branch = open_branch(primary, tip, label="evaluate")
    branch.append_message(_msg("assistant", "branch write, landed LAST"))

    entries = primary.entries()
    assert LANE_KEY in entries[-1], "precondition: a branch entry really is last"

    assert resolve_cursor(entries) == tip, "the primary cursor ignores branch entries"
    assert primary.cursor == tip


def test_a_human_can_still_navigate_INTO_a_branch(primary):
    """The deliberate asymmetry: the lane marker gates which entries may DEFINE the
    cursor, not what a cursor may POINT AT. The tree browser must still work."""
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


def test_summarize_branch_does_not_vacuum_up_a_sub_agents_lane(primary):
    """subtree_text feeds the "summarize branch" prompt. Its BFS descends into every
    child — and a sub-agent branch is, structurally, just another child subtree. Without
    lane containment, summarizing an abandoned primary branch would pull the sub-agent's
    private transcript into a summary that then goes to the model on the PRIMARY path:
    exactly the context leak the lane design exists to prevent."""
    abandoned = primary.append_message(_msg("user", "an abandoned line of thought"))
    primary.append_message(_msg("assistant", "more of the abandoned branch"))

    # a sub-agent runs off an entry INSIDE the abandoned branch
    sub = open_branch(primary, abandoned, label="sub-agent")
    sub.append_message(_msg("user", "SUB-AGENT PRIVATE NOTES"))
    sub.append_message(_msg("assistant", "sub-agent scratch work"))

    text = ConversationTree(primary.entries(), primary.cursor).subtree_text(abandoned)

    assert "an abandoned line of thought" in text
    assert "more of the abandoned branch" in text
    assert "SUB-AGENT PRIVATE NOTES" not in text, "the sub-agent's lane leaked into the summary"
    assert "sub-agent scratch work" not in text


def test_summarizing_the_sub_agents_own_branch_still_works(primary):
    """The containment is by LANE, not a blanket "skip tagged entries" — otherwise the
    fold step (spawner reads its branch's verdict) could never summarize a branch."""
    branch = open_branch(primary, primary.cursor, label="sub-agent")
    root = branch.append_message(_msg("user", "the sub-agent's question"))
    branch.append_message(_msg("assistant", "the sub-agent's VERDICT"))

    text = ConversationTree(primary.entries(), primary.cursor).subtree_text(root)

    assert "the sub-agent's question" in text
    assert "the sub-agent's VERDICT" in text
