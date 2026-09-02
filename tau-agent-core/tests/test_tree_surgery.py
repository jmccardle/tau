"""The pure plan algebra behind the browser's branch/copy/paste gestures.

``tau_agent_core.tree_surgery`` is what decides the shape of a branch before
anything is appended (TREE-BROWSER-AS-EDITOR.md §6, §7). These tests pin the four
decisions that make it more than a list comprehension:

* the keep/copy split is read off the tree, so a selection that is already an
  ancestor chain mints nothing (§6.3 case A, the only form that preserves identity);
* the order is the tree's, not the reader's clicking order;
* a mark drags its tool group with it, so half a tool call cannot be selected; and
* a plan that would compose a malformed prefix is refused with the reason, before
  the first append.

The durable half is exercised in ``tau-coding-agent/tests/test_tree_branch_and_paste.py``
— this file never touches a ``SessionLog``.
"""

from __future__ import annotations

from typing import Any

import pytest

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.tree_surgery import (
    admission_reason,
    branch_refusal_reason,
    copy_of,
    plan_branch,
    plan_paste,
    planned_messages,
    paste_refusal_reason,
    selection_order,
    tool_group,
)


def _msg(entry_id: str, parent: str | None, role: str, text: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "message",
        "parentId": parent,
        "timestamp": f"2026-08-30T00:00:{int(entry_id[-2:]):02d}Z",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _call(entry_id: str, parent: str | None, call_id: str, name: str) -> dict[str, Any]:
    """An assistant message that makes one tool call."""
    return {
        "id": entry_id,
        "type": "message",
        "parentId": parent,
        "timestamp": f"2026-08-30T00:00:{int(entry_id[-2:]):02d}Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": call_id, "name": name, "arguments": {}}],
        },
    }


def _result(entry_id: str, parent: str | None, call_id: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "message",
        "parentId": parent,
        "timestamp": f"2026-08-30T00:00:{int(entry_id[-2:]):02d}Z",
        "message": {
            "role": "toolResult",
            "tool_call_id": call_id,
            "content": [{"type": "text", "text": "ok"}],
        },
    }


def _linear() -> list[dict[str, Any]]:
    """``sys → u1 → a1 → u2 → a2 → u3``."""
    return [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _msg("e04", "e03", "user", "u2"),
        _msg("e05", "e04", "assistant", "a2"),
        _msg("e06", "e05", "user", "u3"),
    ]


def _tree(entries: list[dict[str, Any]], cursor: str | None = None) -> ConversationTree:
    return ConversationTree(entries, cursor or entries[-1]["id"])


# --- order ------------------------------------------------------------------


def test_selection_is_ordered_by_the_tree_not_by_the_caller() -> None:
    tree = _tree(_linear())
    assert selection_order(tree, ["e05", "e02", "e04"]) == ("e02", "e04", "e05")
    # Same answer whatever order they arrive in — the point of the function.
    assert selection_order(tree, ["e04", "e05", "e02"]) == ("e02", "e04", "e05")


def test_an_unknown_mark_is_named_rather_than_dropped() -> None:
    tree = _tree(_linear())
    with pytest.raises(ValueError, match="marked entries not in this tree: nope"):
        selection_order(tree, ["e02", "nope"])


# --- the keep/copy split (§6.3) ---------------------------------------------


def test_a_contiguous_selection_mints_nothing() -> None:
    """§6.3 case A. ``u2 → a2 → u3`` is already a real ancestor chain, so the branch
    is a cursor move (plus an elide) and every id keeps its identity."""
    tree = _tree(_linear())
    plan = plan_branch(tree, ["e04", "e05", "e06"], drop_context=True)
    assert plan.keeps == ("e04", "e05", "e06")
    assert plan.copies == ()
    assert plan.mints == 0
    assert plan.attach == "e06"
    assert plan.elide_from == "e04"


def test_a_gap_in_the_selection_starts_the_copies() -> None:
    """The shape the elide cannot express (§6.1): keep ``u1``, skip ``a1`` and
    ``u2``, then continue with ``a2``. Everything after the first gap is copied."""
    tree = _tree(_linear())
    plan = plan_branch(tree, ["e02", "e05", "e06"], drop_context=False)
    assert plan.keeps == ("e02",)
    assert plan.copies == ("e05", "e06")
    assert plan.attach == "e02"
    assert plan.elide_from is None  # no elide was asked for


def test_the_planned_messages_are_what_the_model_would_see() -> None:
    tree = _tree(_linear())
    plan = plan_branch(tree, ["e02", "e05"], drop_context=True)
    texts = [m["content"][0]["text"] for m in planned_messages(tree, plan)]
    # The system prompt survives the planned elide, exactly as it survives a real
    # one (conversation_tree.is_system_message); u1 is kept; a2 is the copy.
    assert texts == ["sys", "u1", "a2"]


def test_an_elide_that_would_hide_nothing_is_not_planned() -> None:
    """The no-op refusal ``TauBackend.elide_span`` makes, made here instead — with
    the plan simply not carrying an elide, because the reader asked for an end state
    ("only these messages") that already holds."""
    tree = _tree(_linear())
    plan = plan_branch(tree, ["e02", "e03"], drop_context=True)
    assert plan.elide_from is None
    assert plan.hidden == 0


# --- tool pairing (the mark expansion) --------------------------------------


def _with_tool_call() -> list[dict[str, Any]]:
    return [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _call("e03", "e02", "call-1", "read"),
        _result("e04", "e03", "call-1"),
        _msg("e05", "e04", "assistant", "a1"),
    ]


def test_marking_an_assistant_pulls_in_its_tool_results() -> None:
    tree = _tree(_with_tool_call())
    assert tool_group(tree, "e03") == frozenset({"e03", "e04"})


def test_marking_a_tool_result_pulls_in_the_call_that_made_it() -> None:
    tree = _tree(_with_tool_call())
    assert tool_group(tree, "e04") == frozenset({"e03", "e04"})


def test_an_ordinary_message_is_its_own_group() -> None:
    tree = _tree(_with_tool_call())
    assert tool_group(tree, "e02") == frozenset({"e02"})


def test_a_pair_split_across_branches_pulls_in_one_result() -> None:
    """The same call answered on two branches — a re-run after a fork. The group is
    one line's worth, not both: a branch carrying two results for one call is a
    different malformed prefix from the one the pairing exists to prevent."""
    entries = _with_tool_call()
    entries.append(_result("e06", "e03", "call-1"))  # a second answer, on a fork
    tree = _tree(entries, cursor="e05")
    group = tool_group(tree, "e03")
    assert len(group) == 2
    assert "e03" in group


# --- refusals ---------------------------------------------------------------


def test_half_a_tool_call_is_refused_with_the_reason() -> None:
    """The backstop behind the mark expansion. A caller that assembles the ids
    itself — the RPC surface, a test, a future head — still cannot commit this."""
    tree = _tree(_with_tool_call())
    reason = branch_refusal_reason(tree, ["e02", "e03"], drop_context=False)
    assert reason is not None
    assert "unanswered tool call" in reason


def test_a_result_without_its_call_is_refused() -> None:
    tree = _tree(_with_tool_call())
    reason = branch_refusal_reason(tree, ["e02", "e04"], drop_context=False)
    assert reason is not None
    assert "no tool call before it" in reason


def test_a_structural_entry_cannot_be_branched_from() -> None:
    entries = _linear()
    entries.append(
        {
            "id": "e07",
            "type": "elide",
            "parentId": "e06",
            "timestamp": "2026-08-30T00:00:07Z",
            "firstKeptId": "e04",
        }
    )
    tree = _tree(entries, cursor="e07")
    reason = branch_refusal_reason(tree, ["e02", "e07"], drop_context=False)
    assert reason is not None
    assert "'elide' entry" in reason


def test_the_system_prompt_cannot_be_copied_into_the_middle_of_a_branch() -> None:
    """It would sit beside the one every fold carries anyway. Marking it FIRST is a
    different thing — the branch hangs off it — and stays legal."""
    tree = _tree(_linear())
    reason = branch_refusal_reason(tree, ["e02", "e01"], drop_context=False)
    # e01 is the system prompt and e02 is its child, so ordering puts e01 first and
    # this selection is contiguous: both are keeps, and it is allowed.
    assert reason is None

    # Here e01 lands among the copies: e04 is not its child, so the chain breaks at
    # e02 and everything after is minted.
    entries = _linear()
    entries.append(_msg("e07", "e06", "system", "a second system message"))
    tree = _tree(entries, cursor="e07")
    reason = branch_refusal_reason(tree, ["e02", "e07"], drop_context=False)
    assert reason is not None
    assert "second one in the middle of the branch" in reason


def test_a_legal_selection_is_refused_for_nothing() -> None:
    tree = _tree(_with_tool_call())
    assert branch_refusal_reason(tree, ["e02", "e03", "e04"], drop_context=True) is None


def test_admission_reason_reads_a_bare_message_list() -> None:
    """It is the plan-level counterpart of ``fork_admission_reason`` and takes
    messages, so a caller composing a sequence by hand can ask the same question."""
    assert admission_reason([{"role": "user", "content": "hi"}]) is None
    assert (
        admission_reason(
            [
                {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "c1", "name": "read"}],
                }
            ]
        )
        is not None
    )


# --- copies -----------------------------------------------------------------


def test_a_copy_is_an_ordinary_message_with_provenance() -> None:
    """§7.1: ``type: "message"`` deliberately, not a new kind — every existing walker
    treats a copy as what it is without being taught anything."""
    tree = _tree(_linear())
    kind, payload = copy_of(tree.entry("e04"))
    assert kind == "message"
    assert payload["copiedFrom"] == "e04"
    assert payload["message"]["content"][0]["text"] == "u2"


def test_a_copied_branch_summary_loses_its_branch_point() -> None:
    """``fromId`` names where the summary was written, which the copy is not."""
    entry = {
        "id": "e07",
        "type": "branch_summary",
        "parentId": "e02",
        "timestamp": "2026-08-30T00:00:07Z",
        "summary": "what that branch tried",
        "fromId": "e03",
    }
    kind, payload = copy_of(entry)
    assert kind == "branch_summary"
    assert payload["summary"] == "what that branch tried"
    assert payload["fromId"] is None
    assert payload["copiedFrom"] == "e07"


def test_the_copy_does_not_alias_the_original_message() -> None:
    """A shared dict would let an edit to one entry rewrite the other, and on the
    in-memory store both live in the same list."""
    tree = _tree(_linear())
    _kind, payload = copy_of(tree.entry("e04"))
    payload["message"]["content"][0]["text"] = "edited"
    assert tree.entry("e04")["message"]["content"][0]["text"] == "u2"


def test_a_structural_entry_cannot_be_copied() -> None:
    with pytest.raises(ValueError, match="'navigate' entry cannot be copied"):
        copy_of({"id": "e07", "type": "navigate", "targetId": "e02"})


# --- paste ------------------------------------------------------------------


def _forked() -> list[dict[str, Any]]:
    """``sys → u1 → a1``, with ``a1`` forking into ``u2`` and ``u3``."""
    return [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _msg("e04", "e03", "user", "u2"),
        _msg("e05", "e03", "user", "u3"),
    ]


def test_a_paste_plan_carries_the_whole_subtree_parents_first() -> None:
    tree = _tree(_forked(), cursor="e04")
    plan = plan_paste(tree, "e03", "e02")
    assert [m.source_id for m in plan.mints] == ["e03", "e04", "e05"]
    # The root hangs from the target; the children hang from their copied parent.
    assert plan.mints[0].parent_source_id is None
    assert plan.mints[1].parent_source_id == "e03"
    assert plan.mints[2].parent_source_id == "e03"
    assert plan.skipped == ()


def test_a_structural_entry_inside_the_subtree_is_left_out_and_counted() -> None:
    """Its children re-parent onto the nearest copied ancestor, so the copy is
    shorter than the original rather than broken — and the count is reported."""
    entries = _forked()
    entries.append(
        {
            "id": "e06",
            "type": "navigate",
            "parentId": "e03",
            "timestamp": "2026-08-30T00:00:06Z",
            "targetId": "e02",
        }
    )
    entries.append(_msg("e07", "e06", "assistant", "below the navigate"))
    tree = _tree(entries, cursor="e07")
    plan = plan_paste(tree, "e03", "e02")
    assert plan.skipped == ("e06",)
    hung = {m.source_id: m.parent_source_id for m in plan.mints}
    assert hung["e07"] == "e03"


def test_a_subtree_cannot_be_pasted_into_itself() -> None:
    tree = _tree(_forked(), cursor="e04")
    with pytest.raises(ValueError, match="into its own subtree"):
        plan_paste(tree, "e03", "e04")
    with pytest.raises(ValueError, match="into its own subtree"):
        plan_paste(tree, "e03", "e03")


def test_a_copied_result_whose_call_is_not_coming_with_it_is_refused() -> None:
    entries = _with_tool_call()
    tree = _tree(entries, cursor="e05")
    plan = plan_paste(tree, "e04", "e02")  # the result alone, onto a path without the call
    reason = paste_refusal_reason(tree, plan)
    assert reason is not None
    assert "neither in the copied subtree nor on the path" in reason


def test_a_copied_call_and_result_together_are_accepted() -> None:
    tree = _tree(_with_tool_call(), cursor="e05")
    plan = plan_paste(tree, "e03", "e02")
    assert paste_refusal_reason(tree, plan) is None


def test_a_copied_line_that_ends_on_an_unanswered_call_is_not_refused() -> None:
    """Deliberately allowed (see ``paste_refusal_reason``). A paste does not move
    the cursor, so no turn starts there — and refusing it would invent a rule the
    rest of the browser does not apply to the same shape."""
    entries = [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _call("e03", "e02", "call-1", "read"),
    ]
    tree = _tree(entries, cursor="e03")
    plan = plan_paste(tree, "e03", "e02")
    assert paste_refusal_reason(tree, plan) is None
