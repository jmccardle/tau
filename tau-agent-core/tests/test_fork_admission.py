"""ConversationTree.fork_admission_reason — the concrete admission check
docs/SUBMISSION-LIFECYCLE.md's "fork" strategy requires: a fork point must be a
TURN-COMPLETE entry, or forking there hands the second agent a prefix ending in an
assistant message that declares tool calls with no results, which most providers
reject outright.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.conversation_tree import ConversationTree


def _user(entry_id: str, parent: str | None, text: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "message",
        "parentId": parent,
        "timestamp": "2026-07-31T00:00:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant_text(entry_id: str, parent: str | None, text: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "message",
        "parentId": parent,
        "timestamp": "2026-07-31T00:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant_tool_call(
    entry_id: str, parent: str | None, *calls: tuple[str, str]
) -> dict[str, Any]:
    """An assistant message whose content is one ``toolCall`` block per
    ``(tool_call_id, name)`` pair in ``calls`` — no text block, mirroring a
    real tool-only turn."""
    return {
        "id": entry_id,
        "type": "message",
        "parentId": parent,
        "timestamp": "2026-07-31T00:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "toolCall", "id": tool_call_id, "name": name, "arguments": {}}
                for tool_call_id, name in calls
            ],
        },
    }


def _tool_result(entry_id: str, parent: str | None, tool_call_id: str, name: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "message",
        "parentId": parent,
        "timestamp": "2026-07-31T00:00:00.000Z",
        "message": {
            "role": "toolResult",
            "tool_call_id": tool_call_id,
            "tool_name": name,
            "content": [{"type": "text", "text": "ok"}],
        },
    }


def test_fork_before_root_is_always_safe():
    tree = ConversationTree([], None)
    assert tree.fork_admission_reason(None) is None


def test_unknown_fork_point_raises():
    entries = [_user("e01", None, "hi")]
    tree = ConversationTree(entries, "e01")
    try:
        tree.fork_admission_reason("nope")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "nope" in str(exc)


def test_forking_at_a_plain_user_turn_is_safe():
    entries = [_user("e01", None, "hi")]
    tree = ConversationTree(entries, "e01")
    assert tree.fork_admission_reason("e01") is None


def test_forking_at_a_completed_assistant_text_turn_is_safe():
    entries = [
        _user("e01", None, "hi"),
        _assistant_text("e02", "e01", "hello back"),
    ]
    tree = ConversationTree(entries, "e02")
    assert tree.fork_admission_reason("e02") is None


def test_forking_at_a_completed_tool_turn_is_safe():
    """assistant(toolCall) -> toolResult, all present on the path: complete."""
    entries = [
        _user("e01", None, "read the file"),
        _assistant_tool_call("e02", "e01", ("call_1", "read")),
        _tool_result("e03", "e02", "call_1", "read"),
    ]
    tree = ConversationTree(entries, "e03")
    assert tree.fork_admission_reason("e03") is None


def test_forking_directly_at_the_assistant_tool_call_entry_is_rejected():
    """The precise scenario the spec names: the fork point IS the assistant
    message with unresolved tool calls. Ancestors-only means the toolResult
    (its own descendant) can never rescue this pick, regardless of whether the
    result exists elsewhere in entries()."""
    entries = [
        _user("e01", None, "read the file"),
        _assistant_tool_call("e02", "e01", ("call_1", "read")),
        _tool_result("e03", "e02", "call_1", "read"),
    ]
    tree = ConversationTree(entries, "e03")
    reason = tree.fork_admission_reason("e02")
    assert reason is not None
    assert "call_1" in reason
    assert "read" in reason
    assert "not turn-complete" in reason


def test_forking_at_a_crash_truncated_leaf_is_rejected():
    """No toolResult exists ANYWHERE — the real-world scenario: a crash between
    persisting the assistant message and its tool result(s) (agent_session's
    _persist_loop_messages appends one entry at a time)."""
    entries = [
        _user("e01", None, "read the file"),
        _assistant_tool_call("e02", "e01", ("call_1", "read")),
    ]
    tree = ConversationTree(entries, "e02")
    reason = tree.fork_admission_reason("e02")
    assert reason is not None
    assert "call_1" in reason


def test_partial_multi_tool_call_is_rejected():
    """Three tool calls, only one result landed before the crash: still incomplete."""
    entries = [
        _user("e01", None, "go"),
        _assistant_tool_call("e02", "e01", ("c1", "read"), ("c2", "grep"), ("c3", "ls")),
        _tool_result("e03", "e02", "c1", "read"),
    ]
    tree = ConversationTree(entries, "e03")
    reason = tree.fork_admission_reason("e03")
    assert reason is not None
    assert "c2" in reason and "c3" in reason
    assert "c1" not in reason, "c1 already has its result — only the gap is named"


def test_all_multi_tool_call_results_present_is_safe():
    entries = [
        _user("e01", None, "go"),
        _assistant_tool_call("e02", "e01", ("c1", "read"), ("c2", "grep")),
        _tool_result("e03", "e02", "c1", "read"),
        _tool_result("e04", "e03", "c2", "grep"),
    ]
    tree = ConversationTree(entries, "e04")
    assert tree.fork_admission_reason("e04") is None


def test_a_later_assistant_turn_resets_what_counts_as_pending():
    """A second, later, fully-resolved tool turn must not be haunted by an
    EARLIER turn's bookkeeping -- only the most recent assistant message's own
    tool calls can still be outstanding by the time the fold reaches the target."""
    entries = [
        _user("e01", None, "go"),
        _assistant_tool_call("e02", "e01", ("c1", "read")),
        _tool_result("e03", "e02", "c1", "read"),
        _assistant_text("e04", "e03", "done with step 1"),
        _user("e05", "e04", "now step 2"),
        _assistant_tool_call("e06", "e05", ("c2", "grep")),
        _tool_result("e07", "e06", "c2", "grep"),
    ]
    tree = ConversationTree(entries, "e07")
    assert tree.fork_admission_reason("e07") is None
