"""Step 1c — append-only compaction (SESSION-TREE-IMPLEMENTATION §2.3).

Compaction records its boundary by *appending* a summary entry (pi
``appendCompaction``), never by re-parenting + rewriting the log. These tests lock
in three invariants:

1. the underlying entry log is append-only — no earlier byte is mutated when a
   compaction is recorded (byte-prefix stability for a file-backed session);
2. the read-time fold (``SessionManager._build_active_path`` /
   ``ConversationTree.context_for``) splices the appended summary in correctly;
3. navigating *behind* the boundary restores the pre-compaction messages — the
   history is still addressable because nothing was deleted.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_manager import SessionManager


def _msg(entry_id: str, parent: str | None, role: str, text: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "message",
        "parentId": parent,
        "timestamp": f"2026-07-03T00:00:{int(entry_id[-2:]):02d}Z",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


# --- ConversationTree reads an append-as-tip compaction ---------------------


def _linear_then_appended_compaction() -> list[dict[str, Any]]:
    """The shape ``Session.append_compaction`` produces: the compaction is the LEAF
    (parentId = old tip); ``firstKeptId`` names an *ancestor* (e04)."""
    return [
        _msg("e01", None, "system", "sys"),
        _msg("e02", "e01", "user", "u1"),
        _msg("e03", "e02", "assistant", "a1"),
        _msg("e04", "e03", "user", "u2"),
        _msg("e05", "e04", "assistant", "a2"),
        {
            "id": "c06",
            "type": "compaction",
            "parentId": "e05",
            "timestamp": "2026-07-03T00:00:06Z",
            "summary": "SUMMARY",
            "firstKeptId": "e04",
            "tokensBefore": 0,
        },
    ]


def test_context_for_splices_tip_appended_compaction() -> None:
    entries = _linear_then_appended_compaction()
    tree = ConversationTree(entries, cursor="c06")
    msgs = tree.context_for()
    assert msgs[0] == {
        "role": "user",
        "content": [{"type": "text", "text": "[[Compaction summary: SUMMARY]]"}],
    }
    # sys/u1/a1 precede the boundary (e04) → dropped; the summary + kept region remain.
    assert [m["content"][0]["text"] for m in msgs[1:]] == ["u2", "a2"]


def test_navigate_behind_boundary_restores_pre_compaction_messages() -> None:
    entries = _linear_then_appended_compaction()
    tree = ConversationTree(entries, cursor="c06")
    # Behind the boundary the pre-compaction prefix is addressable again — nothing
    # was deleted by the append-only compaction.
    tree.navigate("e05")
    assert [m["content"][0]["text"] for m in tree.context_for()] == [
        "sys",
        "u1",
        "a1",
        "u2",
        "a2",
    ]


def test_branch_summary_appended_at_tip_is_inline_not_a_splice() -> None:
    # Decision 5 fix 2 (§5): a branch_summary is NOT a splice anchor — appended at the
    # tip it renders INLINE, dropping no prefix (unlike a compaction). The 1c version
    # of this test asserted branch_summary spliced *like* compaction via the now-removed
    # §2.4 unification; corrected here to the pi-verified behavior.
    branch = _linear_then_appended_compaction()[:-1] + [
        {
            "id": "c06",
            "type": "branch_summary",
            "parentId": "e05",
            "timestamp": "2026-07-03T00:00:06Z",
            "summary": "SUMMARY",
            "fromId": "e04",
        }
    ]
    msgs = ConversationTree(branch, cursor="c06").context_for()
    # Full linear prefix survives; the summary is appended inline (no drop-prefix).
    assert [m["content"][0]["text"] for m in msgs] == [
        "sys",
        "u1",
        "a1",
        "u2",
        "a2",
        "[[Branch summary: SUMMARY]]",
    ]


# --- SessionManager: append-only + byte-prefix stability --------------------


def _file_session(sessions_dir: str) -> SessionManager:
    mgr = SessionManager(sessions_dir=sessions_dir)
    path = mgr.new_session()
    mgr._active_session_path = path
    mgr.append_entry(_msg_snake("u1", "user", "old question"))
    mgr.append_entry(_msg_snake("a1", "assistant", "old answer"))
    mgr.append_entry(_msg_snake("u2", "user", "keep me"))
    return mgr


def _msg_snake(entry_id: str, role: str, text: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "message",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def test_apply_compaction_is_append_only_byte_prefix_stable(tmp_path) -> None:
    mgr = _file_session(str(tmp_path))
    path = mgr._active_session_path
    assert path is not None

    before = open(path, "rb").read()
    before_lines = before.splitlines()

    mgr.apply_compaction(
        first_kept_entry_id="u2",
        summary="OLD WORK SUMMARY",
        compacted_entry_ids=["u1", "a1"],
        tokens_saved=42,
    )

    after = open(path, "rb").read()
    after_lines = after.splitlines()
    # Append-only: every prior byte is untouched, exactly one line was added.
    assert after.startswith(before)
    assert after_lines[: len(before_lines)] == before_lines
    assert len(after_lines) == len(before_lines) + 1
    # The appended line is the compaction entry (the boundary marker).
    assert b'"type": "compaction"' in after_lines[-1]


def test_apply_compaction_splices_and_history_is_recoverable(tmp_path) -> None:
    mgr = _file_session(str(tmp_path))
    mgr.apply_compaction(
        first_kept_entry_id="u2", summary="OLD WORK SUMMARY", compacted_entry_ids=["u1", "a1"]
    )
    messages = mgr.get_active_messages()
    assert len(messages) == 2
    assert "[[Compaction summary: OLD WORK SUMMARY]]" in messages[0]["content"][0]["text"]
    assert messages[1]["content"][0]["text"] == "keep me"

    # Navigate behind the boundary → the compacted prefix is restored in full.
    mgr.navigate("a1")
    restored = mgr.get_active_messages()
    assert [m["content"][0]["text"] for m in restored] == ["old question", "old answer"]
