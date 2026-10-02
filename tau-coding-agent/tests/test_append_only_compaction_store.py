"""Step 1c — append-only compaction end-to-end over the file-backed store.

Ties the file store (``session_store.Session``) to the read-time fold
(``ConversationTree.context_for``, step 1a). Asserts the acceptance invariants:

- recording a compaction leaves the ``.jsonl`` byte-prefix stable (append-only —
  no earlier line mutated), adding exactly one entry;
- ``context_for`` splices the appended summary at read time and drops the
  pre-boundary prefix;
- moving a cursor behind the boundary restores the pre-compaction messages,
  because nothing was deleted.
"""

from __future__ import annotations

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.cursor import Cursor
from tau_agent_core.session_log import default_leaf

from tau_coding_agent.session_store import Session

_PROV = {
    "summarizer_model_id": "test-summarizer",
    "summary_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    "covered_entries": 1,
    "covered_tokens": 50,
    "config_id": None,
}


CWD = "/srv/proj"


async def _session_with_history(base_dir) -> tuple[Session, Cursor, str, str]:
    """A file-backed session with three turns: (session, cursor, keep_id, behind_id)."""
    session = Session.create(CWD, "local-llm", "openai", base_dir=base_dir)
    cursor = Cursor.newest(session)
    await cursor.append_message({"role": "user", "content": "old question"})
    behind_id = await cursor.append_message({"role": "assistant", "content": "old answer"})
    keep_id = await cursor.append_message({"role": "user", "content": "keep me"})
    return session, cursor, keep_id, behind_id


async def test_append_compaction_is_byte_prefix_stable(tmp_path) -> None:
    session, cursor, keep_id, _ = await _session_with_history(tmp_path)
    assert session.path is not None

    before = session.path.read_bytes()
    await cursor.append_compaction("SUMMARY", first_kept_id=keep_id, tokens_before=100, **_PROV)
    after = session.path.read_bytes()

    assert after.startswith(before)
    assert len(after.splitlines()) == len(before.splitlines()) + 1
    assert b'"type": "compaction"' in after.splitlines()[-1]


async def test_context_for_splices_appended_compaction(tmp_path) -> None:
    session, cursor, keep_id, _ = await _session_with_history(tmp_path)
    await cursor.append_compaction("SUMMARY", first_kept_id=keep_id, tokens_before=100, **_PROV)

    msgs = cursor.context()

    assert msgs[0] == {
        "role": "user",
        "content": [{"type": "text", "text": "[[Compaction summary: SUMMARY]]"}],
    }
    # "old question" / "old answer" precede the boundary → dropped; "keep me" kept.
    assert msgs[1] == {"role": "user", "content": "keep me"}
    assert len(msgs) == 2


async def test_moving_behind_boundary_restores_pre_compaction(tmp_path) -> None:
    session, cursor, keep_id, behind_id = await _session_with_history(tmp_path)
    await cursor.append_compaction("SUMMARY", first_kept_id=keep_id, tokens_before=100, **_PROV)

    cursor.move(behind_id)
    restored = cursor.context()
    assert restored == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]


async def test_reloaded_session_opens_at_the_compaction_and_splices(tmp_path) -> None:
    session, cursor, keep_id, _ = await _session_with_history(tmp_path)
    await cursor.append_compaction("SUMMARY", first_kept_id=keep_id, tokens_before=100, **_PROV)
    assert session.path is not None

    reloaded = Session.load(session.path)
    msgs = Cursor.newest(reloaded).context()
    assert msgs[0]["content"][0]["text"] == "[[Compaction summary: SUMMARY]]"
    assert msgs[1] == {"role": "user", "content": "keep me"}


async def test_context_property_is_the_spliced_fold_not_the_linear_messages(tmp_path) -> None:
    """``Session.context`` (the render/model seed, §2.6) is the spliced fold at the
    default leaf; ``Session.messages`` (the raw linear fold) is not.

    This is the property both TUI resume (app.py) and headless resume (headless.py)
    now seed from — the fix for a compacted session rendering its dropped history.
    """
    session, cursor, keep_id, _ = await _session_with_history(tmp_path)
    await cursor.append_compaction("SUMMARY", first_kept_id=keep_id, tokens_before=100, **_PROV)

    # The raw linear fold still contains the dropped prefix and no summary — the bug.
    assert {"role": "user", "content": "old question"} in session.messages
    assert {"role": "assistant", "content": "old answer"} in session.messages
    assert all("SUMMARY" not in str(m.get("content")) for m in session.messages)

    ctx = session.context
    entries = session.entries()
    assert ctx == ConversationTree(entries, default_leaf(entries)).context_for()
    assert ctx[0]["content"] == [{"type": "text", "text": "[[Compaction summary: SUMMARY]]"}]
    assert {"role": "user", "content": "keep me"} in ctx
    assert {"role": "user", "content": "old question"} not in ctx
    assert {"role": "assistant", "content": "old answer"} not in ctx
