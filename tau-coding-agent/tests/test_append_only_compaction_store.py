"""Step 1c — append-only compaction end-to-end over the file-backed store.

Ties the authoritative live store (``session_store.Session``, whose
``append_compaction`` was already append-only, §2.3) to the read-time fold
(``ConversationTree.context_for``, step 1a). Asserts the acceptance invariants:

- recording a compaction leaves the ``.jsonl`` byte-prefix stable (append-only —
  no earlier line mutated), adding exactly one entry;
- ``context_for`` splices the appended summary at read time and drops the
  pre-boundary prefix;
- navigating behind the boundary restores the pre-compaction messages, because
  nothing was deleted.
"""

from __future__ import annotations

from tau_agent_core.conversation_tree import ConversationTree

from tau_coding_agent.session_store import Session

CWD = "/home/john/proj"


def _session_with_history(base_dir) -> tuple[Session, str, str]:
    """A file-backed session with three turns; returns (session, keep_id, behind_id)."""
    session = Session.create(CWD, "local-llm", "openai", base_dir=base_dir)
    session.append_message({"role": "user", "content": "old question"})
    behind_id = session.append_message({"role": "assistant", "content": "old answer"})
    keep_id = session.append_message({"role": "user", "content": "keep me"})
    return session, keep_id, behind_id


def test_append_compaction_is_byte_prefix_stable(tmp_path) -> None:
    session, keep_id, _ = _session_with_history(tmp_path)
    assert session.path is not None

    before = session.path.read_bytes()
    session.append_compaction("SUMMARY", first_kept_id=keep_id, tokens_before=100)
    after = session.path.read_bytes()

    # Append-only: the whole prior file is a byte-prefix of the new file, and
    # exactly one line (the compaction marker) was added.
    assert after.startswith(before)
    assert len(after.splitlines()) == len(before.splitlines()) + 1
    assert b'"type": "compaction"' in after.splitlines()[-1]


def test_context_for_splices_appended_compaction(tmp_path) -> None:
    session, keep_id, _ = _session_with_history(tmp_path)
    session.append_compaction("SUMMARY", first_kept_id=keep_id, tokens_before=100)

    # After append_compaction the leaf is the compaction entry (pi appendCompaction).
    tree = ConversationTree(session.entries(), cursor=session._leaf_id)
    msgs = tree.context_for()

    assert msgs[0] == {
        "role": "user",
        "content": [{"type": "text", "text": "[[Compaction summary: SUMMARY]]"}],
    }
    # "old question" / "old answer" precede the boundary → dropped; "keep me" kept.
    assert msgs[1] == {"role": "user", "content": "keep me"}
    assert len(msgs) == 2


def test_navigate_behind_boundary_restores_pre_compaction(tmp_path) -> None:
    session, keep_id, behind_id = _session_with_history(tmp_path)
    session.append_compaction("SUMMARY", first_kept_id=keep_id, tokens_before=100)

    tree = ConversationTree(session.entries(), cursor=session._leaf_id)
    # Behind the boundary the compacted prefix is addressable again — the append
    # deleted nothing.
    tree.navigate(behind_id)
    restored = tree.context_for()
    assert restored == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]


def test_reloaded_session_resolves_cursor_to_compaction_and_splices(tmp_path) -> None:
    session, keep_id, _ = _session_with_history(tmp_path)
    session.append_compaction("SUMMARY", first_kept_id=keep_id, tokens_before=100)
    assert session.path is not None

    # A fresh load resolves the cursor from the last entry (the compaction) and the
    # fold produces the same spliced context — the append-only log round-trips.
    reloaded = Session.load(session.path)
    tree = ConversationTree(reloaded.entries(), cursor=reloaded._leaf_id)
    msgs = tree.context_for()
    assert msgs[0]["content"][0]["text"] == "[[Compaction summary: SUMMARY]]"
    assert msgs[1] == {"role": "user", "content": "keep me"}
