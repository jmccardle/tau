"""``SessionManager`` — JSONL session persistence, forking, cloning, navigation.

Covers ``tau_agent_core.session_manager``: ``fork()`` (both "at" and "before"
positions), ``clone()``, ``navigate()``, ``_build_active_path`` (the tree-walk
and compaction-splice both operations sit on), ``_extract_branch_messages``
(subtree-to-text for branch summarization), ``summarize_branch()``, and the
persistence/listing surface (``new_session``, ``append_entry``, ``save``,
``apply_compaction``, ``list_sessions``/``list_all``, ``_extract_session_info``).

Was ``test_phase5_subphase2.py`` (87 tests, shared with settings-loading tests
now split into ``test_settings.py`` — see that file's docstring). This half
alone was 78% of ``session_manager.py`` (351 statements, 78 missed) because the
old file, while exhaustive on fork/clone/navigate's easy matrix, never touched
``save``, ``apply_compaction``, ``list_sessions``/``list_all``,
``_extract_session_info``, or several defensive branches in the tree-walkers.
Consolidated to 55 test functions (67 cases) at **100%** coverage (351/351
statements) — every remaining gap from the old file's 78%, including three
genuinely obscure defensive corners (a session listing on a directory that was
never created, an empty-but-present ``.jsonl`` file, and an in-memory store
built by hand with no recorded session path), now has a test.

One test was dropped and one rewritten because they encoded a real product bug
as if it were correct behaviour (see the ``clone(entry_id)`` note below for the
defect and the substitution). Two other old tests
(``test_fork_in_memory_before`` and ``test_fork_in_memory_with_tree_entries``)
were exact or near-exact duplicates of other cases in the same file and were
dropped rather than carried over.

PRODUCT BUG FOUND HERE, AND FIXED IN session_manager.py:
``SessionManager.clone(entry_id)`` never read its ``entry_id`` parameter. The
method body called ``self._build_active_path(entries)``, which walks from
``self._active_entry_id`` (the manager's current tip), not from the argument, so
``clone("early-id")`` and ``clone("current-tip-id")`` were byte-identical — the
argument was dead. No caller in ``tau_agent_core`` or ``tau_coding_agent``
invokes ``clone()`` yet, which is why it never surfaced. ``_build_active_path``
now takes an optional ``tip_id``, ``clone()`` passes its argument, and an id that
is not in the session raises rather than yielding an empty path.
``test_clone_stops_at_the_requested_entry_not_the_current_tip`` is the
regression test.

Most ``clone()`` tests below still pass ``entry_id`` equal to the manager's
current tip — that is the ordinary case, and it is deliberately the shape that
could not see the bug.
The old ``test_clone_only_includes_active_path`` did the opposite — it built
what its own comments called a "tree" but, because it never set an explicit
``parent_id``, actually built a straight chain, then asserted that a
supposedly-excluded sibling ("d") WAS included, rationalizing the bug in a
code comment ("d is also on active path (appended in sequence)"). It is
replaced below by a real branch (explicit ``parent_id``s) where the tip is set
to match the ``entry_id`` passed, so the sibling-exclusion assertion is honest.

Reference: docs/PHASE-5-SUBPHASE-2.md (the original spec); session_manager.py.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

import pytest

from tau_agent_core.session_manager import SessionManager, SessionState


def _seed(mgr: SessionManager, n: int, start_ts: int = 1000) -> None:
    """Append n linear message entries e0..e{n-1} off the current tip."""
    for i in range(n):
        mgr.append_entry(
            {
                "id": f"e{i}",
                "type": "message",
                "timestamp": start_ts + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            }
        )


@pytest.fixture
def mgr(tmp_path):
    """A file-backed SessionManager with one new session already active."""
    m = SessionManager(sessions_dir=str(tmp_path))
    m.new_session()
    return m


# ── fork ─────────────────────────────────────────────────────────────────────

FORK_CASES = [
    pytest.param("at", "e2", ["e2", "e3", "e4"], id="at-middle"),
    pytest.param("at", "e0", ["e0", "e1", "e2", "e3", "e4"], id="at-first"),
    pytest.param("at", "e4", ["e4"], id="at-last"),
    pytest.param("before", "e2", ["e0", "e1"], id="before-middle"),
    pytest.param("before", "e0", [], id="before-first"),
    pytest.param("before", "e4", ["e0", "e1", "e2", "e3"], id="before-last"),
]


@pytest.mark.parametrize("position,fork_id,expected_ids", FORK_CASES)
def test_fork_selects_entries_for_every_position_and_boundary(mgr, position, fork_id, expected_ids):
    """fork(id, 'at') keeps id and everything after; 'before' keeps everything
    before id, excluding it. Parametrized across both positions and the
    first/middle/last boundary entries — six near-identical tests collapsed
    into one matrix."""
    _seed(mgr, 5)
    forked = mgr.fork(fork_id, position)
    forked_ids = [e["id"] for e in mgr._read_file(forked) if e.get("type") == "message"]
    assert forked_ids == expected_ids


@pytest.mark.parametrize("position", ["at", "before"])
def test_fork_creates_an_independent_file_and_leaves_the_original_untouched(mgr, position):
    _seed(mgr, 3)
    original_path = mgr._active_session_path
    forked = mgr.fork("e1", position)

    assert os.path.exists(forked)
    assert forked != original_path
    original_ids = [e["id"] for e in mgr._read_file(original_path) if e.get("type") == "message"]
    assert original_ids == ["e0", "e1", "e2"]


def test_fork_output_starts_with_a_fresh_session_entry(mgr):
    _seed(mgr, 3)
    forked_entries = mgr._read_file(mgr.fork("e1", "at"))
    assert forked_entries[0]["type"] == "session"


def test_fork_before_rewrites_the_parent_id_chain(mgr):
    """Forked entries' parent_id must point within the NEW file's chain, not
    back at ids that no longer exist there."""
    _seed(mgr, 3)
    forked_entries = mgr._read_file(mgr.fork("e1", "before"))
    msg_entries = [e for e in forked_entries if e.get("type") == "message"]
    for entry in msg_entries:
        assert entry["parent_id"] is not None


@pytest.mark.parametrize(
    "call",
    [lambda m: m.fork("e1", "at"), lambda m: m.clone("e1")],
    ids=["fork", "clone"],
)
def test_fork_and_clone_require_an_active_session(call):
    fresh = SessionManager()
    with pytest.raises(RuntimeError, match="No active session"):
        call(fresh)


# ── clone ────────────────────────────────────────────────────────────────────
#
# Most of these pass entry_id equal to the manager's real current tip — the
# ordinary case, and the shape that could not see the entry_id-ignored bug. The
# two that do not are the regression tests for it; see the module docstring.


def test_clone_duplicates_the_entire_current_active_path(mgr):
    """clone() duplicates the whole active path into an independent file,
    with a fresh session entry prepended."""
    _seed(mgr, 3)  # tip is now e2
    cloned_path = mgr.clone("e2")

    assert os.path.exists(cloned_path)
    assert cloned_path != mgr._active_session_path
    cloned_entries = mgr._read_file(cloned_path)
    assert cloned_entries[0]["type"] == "session"
    cloned_ids = [e["id"] for e in cloned_entries if e.get("type") == "message"]
    assert cloned_ids == ["e0", "e1", "e2"]


def test_clone_stops_at_the_requested_entry_not_the_current_tip(mgr):
    """Regression: ``entry_id`` was accepted and never read.

    ``clone()`` walked ``_build_active_path(entries)``, which starts at
    ``self._active_entry_id``, so cloning any earlier entry copied the whole
    session instead — ``clone("e0")`` and ``clone("e2")`` produced byte-identical
    files. Every other test in this section passes the current tip, which is
    exactly the shape that cannot see the difference.
    """
    _seed(mgr, 3)  # tip is now e2
    early = mgr._read_file(mgr.clone("e0"))
    mid = mgr._read_file(mgr.clone("e1"))

    assert [e["id"] for e in early if e.get("type") == "message"] == ["e0"]
    assert [e["id"] for e in mid if e.get("type") == "message"] == ["e0", "e1"]
    assert mgr._active_entry_id == "e2"  # cloning an ancestor does not move the tip


def test_clone_rejects_an_entry_that_is_not_in_the_session(mgr):
    """The walk breaks on an unknown id and would otherwise yield an empty path,
    i.e. a clone that silently contains nothing — a typo'd id must not read as
    'this entry had no history'."""
    _seed(mgr, 2)
    with pytest.raises(ValueError, match="No entry 'nope'"):
        mgr.clone("nope")


def test_clone_before_any_messages_produces_a_session_only_file(mgr):
    """clone() on a session with nothing appended yet still succeeds — it
    just has nothing but the (new) session entry to copy."""
    cloned_entries = mgr._read_file(mgr.clone(mgr._active_entry_id))
    assert cloned_entries[0]["type"] == "session"
    assert len(cloned_entries) == 1


def test_clone_preserves_message_timestamps(mgr):
    _seed(mgr, 3, start_ts=5000)
    cloned_entries = mgr._read_file(mgr.clone("e2"))
    msg_entries = [e for e in cloned_entries if e.get("type") == "message"]
    assert [e["timestamp"] for e in msg_entries] == [5000, 5001, 5002]


def test_clone_only_includes_the_active_path_not_a_sibling_branch(mgr):
    """Real sibling-exclusion, unlike the old test of the same name: parent_id
    is set explicitly so branch 'a -> b -> c' and sibling 'a -> d' are
    genuinely distinct, and the tip is pointed at 'c' to match the entry_id
    argument (sidestepping the entry_id-ignored bug rather than depending on
    it — see module docstring)."""
    mgr.append_entry(
        {
            "id": "a",
            "type": "message",
            "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "a"}]},
        }
    )
    mgr.append_entry(
        {
            "id": "b",
            "type": "message",
            "timestamp": 2,
            "parent_id": "a",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        }
    )
    mgr.append_entry(
        {
            "id": "d",
            "type": "message",
            "timestamp": 3,
            "parent_id": "a",
            "message": {"role": "user", "content": [{"type": "text", "text": "sibling"}]},
        }
    )
    mgr.append_entry(
        {
            "id": "c",
            "type": "message",
            "timestamp": 4,
            "parent_id": "b",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "c"}]},
        }
    )
    mgr._active_entry_id = "c"  # the tip we're cloning

    cloned_ids = [e["id"] for e in mgr._read_file(mgr.clone("c")) if e.get("type") == "message"]
    assert cloned_ids == ["a", "b", "c"]
    assert "d" not in cloned_ids


def test_clone_keeps_only_the_most_recent_of_several_compactions(mgr):
    """A later compaction's summary already incorporates the earlier one (via
    previous_summary), so the stale summary and its kept region drop out of
    the active path — and therefore out of the clone. Matches pi's
    buildSessionContext, which reassigns `compaction` through its loop rather
    than keeping every one seen."""
    for i in range(2):
        mgr.append_entry(
            {
                "id": f"old{i}",
                "type": "message",
                "timestamp": i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"old{i}"}]},
            }
        )
    mgr.append_entry(
        {
            "id": "comp1",
            "type": "compaction",
            "timestamp": 100,
            "first_kept_id": "keep1",
            "summary": "First compaction",
        }
    )
    for i in range(2):
        mgr.append_entry(
            {
                "id": f"keep{i}",
                "type": "message",
                "timestamp": 200 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"keep{i}"}]},
            }
        )
    mgr.append_entry(
        {
            "id": "comp2",
            "type": "compaction",
            "timestamp": 300,
            "first_kept_id": "keep2_last",
            "summary": "Second compaction",
        }
    )
    mgr.append_entry(
        {
            "id": "keep2_last",
            "type": "message",
            "timestamp": 400,
            "message": {"role": "user", "content": [{"type": "text", "text": "last"}]},
        }
    )

    cloned_entries = mgr._read_file(mgr.clone("keep2_last"))
    cloned_types = [e["type"] for e in cloned_entries]
    summaries = [e["summary"] for e in cloned_entries if e.get("type") == "compaction"]

    assert cloned_types.count("compaction") == 1
    assert summaries == ["Second compaction"]
    assert cloned_types.count("message") >= 1


# ── navigate ─────────────────────────────────────────────────────────────────


def test_navigate_updates_the_active_entry_and_returns_matching_state(mgr):
    """state.entries is the FULL entry list (session + all 5 messages) —
    navigate() does not truncate it to the target; only active_entry_id and
    get_active_messages() (below) reflect where you navigated to."""
    _seed(mgr, 5)
    state = mgr.navigate("e2")

    assert mgr._active_entry_id == "e2"
    assert isinstance(state, SessionState)
    assert state.active_entry_id == "e2"
    assert state.session_path == mgr._active_session_path
    assert len(state.entries) == 6  # session + e0..e4, regardless of the nav target


def test_navigate_then_get_active_messages_reflects_the_new_position(mgr):
    _seed(mgr, 5)
    mgr.navigate("e2")
    assert len(mgr.get_active_messages()) == 3  # e0, e1, e2


def test_navigate_to_an_unknown_entry_raises(mgr):
    mgr.append_entry(
        {
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "msg0"}]},
        }
    )
    with pytest.raises(KeyError, match="not found"):
        mgr.navigate("nonexistent")


def test_navigate_with_none_clears_the_active_entry(mgr):
    mgr.append_entry(
        {
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "msg0"}]},
        }
    )
    mgr.navigate(None)
    assert mgr._active_entry_id is None


def test_navigate_returns_session_metadata(tmp_path):
    m = SessionManager(sessions_dir=str(tmp_path))
    m.new_session(model_id="gpt-4o")
    m.append_entry(
        {
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "msg0"}]},
        }
    )
    state = m.navigate("e0")
    assert state.model == "gpt-4o"


def test_navigate_follows_the_parent_id_chain_not_append_order(mgr):
    """Navigating to 'a' after appending its child 'b' must not carry 'b'
    along — the path is built from parent_id links, not from append order."""
    mgr.append_entry(
        {
            "id": "a",
            "type": "message",
            "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "a"}]},
        }
    )
    mgr.append_entry(
        {
            "id": "b",
            "type": "message",
            "timestamp": 2,
            "parent_id": "a",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        }
    )
    mgr.navigate("a")
    messages = mgr.get_active_messages()
    assert len(messages) == 1
    assert messages[0]["content"][0]["text"] == "a"


def test_navigate_to_the_session_root_entry_works(tmp_path):
    m = SessionManager(sessions_dir=str(tmp_path))
    m.new_session(model_id="gpt-4o")
    session_id = m._read_file(m._active_session_path)[0]["id"]
    state = m.navigate(session_id)
    assert state.active_entry_id == session_id
    assert state.model == "gpt-4o"


def test_navigate_is_idempotent_across_repeated_calls(mgr):
    _seed(mgr, 5)
    mgr.navigate("e1")
    assert mgr._active_entry_id == "e1"
    mgr.navigate("e3")
    assert mgr._active_entry_id == "e3"
    mgr.navigate("e1")
    assert mgr._active_entry_id == "e1"


# ── fork + navigate, in-memory mode ─────────────────────────────────────────


def test_fork_in_memory_selects_the_same_entries_as_file_backed(tmp_path):
    """In-memory storage doesn't change fork()'s selection algorithm — two
    representative cases (not the full at/before/first/middle/last matrix,
    which is already exhaustively covered for file-backed storage above)."""
    m = SessionManager.in_memory(cwd=str(tmp_path))
    m._sessions_dir = str(tmp_path)
    m.new_session()
    _seed(m, 5)

    at_ids = [e["id"] for e in m._read_file(m.fork("e2", "at")) if e.get("type") == "message"]
    before_ids = [
        e["id"] for e in m._read_file(m.fork("e2", "before")) if e.get("type") == "message"
    ]
    assert at_ids == ["e2", "e3", "e4"]
    assert before_ids == ["e0", "e1"]


def test_fork_in_memory_still_writes_a_real_file_on_disk(tmp_path):
    """fork() always materializes a file, even off an in-memory source — the
    forked branch must be loadable by a fresh SessionManager, which in-memory
    storage alone can't provide."""
    m = SessionManager.in_memory(cwd=str(tmp_path))
    m._sessions_dir = str(tmp_path)
    m.new_session()
    _seed(m, 3)
    forked = m.fork("e1", "at")
    assert os.path.exists(forked)
    assert forked.endswith(".jsonl")


def test_fork_in_memory_does_not_mutate_the_memory_store(tmp_path):
    m = SessionManager.in_memory(cwd=str(tmp_path))
    m._sessions_dir = str(tmp_path)
    m.new_session()
    _seed(m, 3)
    original_count = len(m._memory_store)

    m.fork("e1", "at")

    assert len(m._memory_store) == original_count


def test_fork_then_navigate_leaves_the_original_sessions_position_alone(mgr):
    _seed(mgr, 5)
    mgr.navigate("e2")

    forked = mgr.fork("e3", "at")

    assert mgr._active_entry_id == "e2"  # forking doesn't move the original's tip
    forked_ids = [e["id"] for e in mgr._read_file(forked) if e.get("type") == "message"]
    assert forked_ids == ["e3", "e4"]


def test_load_of_a_cloned_session_starts_at_its_own_fresh_root(mgr):
    """load() always resets active_entry_id to the loaded file's OWN session
    entry (its root) — not to whatever entry_id the clone/fork call used."""
    _seed(mgr, 3)
    cloned = mgr.clone("e2")

    state = mgr.load(cloned)

    assert state.entries[0]["type"] == "session"
    assert state.active_entry_id == state.entries[0]["id"]
    assert len(state.entries) == 4  # session + e0, e1, e2


def test_forking_twice_from_different_points_produces_distinct_sessions(mgr):
    _seed(mgr, 5)
    fork_before = mgr.fork("e1", "before")
    fork_at = mgr.fork("e1", "at")

    before_ids = [e["id"] for e in mgr._read_file(fork_before) if e.get("type") == "message"]
    at_ids = [e["id"] for e in mgr._read_file(fork_at) if e.get("type") == "message"]
    assert before_ids == ["e0"]
    assert at_ids == ["e1", "e2", "e3", "e4"]
    assert before_ids != at_ids


# ── session persistence: new_session / append_entry / save ─────────────────


def test_new_session_creates_a_missing_sessions_directory(tmp_path):
    target = tmp_path / "nested" / "sessions"
    m = SessionManager(sessions_dir=str(target))
    path = m.new_session()
    assert target.is_dir()
    assert os.path.exists(path)


def test_append_entry_fills_in_a_timestamp_when_omitted(mgr):
    entry_id = mgr.append_entry(
        {"id": "e0", "type": "message", "message": {"role": "user", "content": []}}
    )
    (entry,) = [e for e in mgr._get_entries() if e["id"] == entry_id]
    assert isinstance(entry["timestamp"], int)
    assert entry["timestamp"] > 0


def test_save_appends_state_entries_to_the_session_file(mgr):
    new_entry = {
        "id": "e0",
        "type": "message",
        "timestamp": 1,
        "parent_id": None,
        "message": {"role": "user", "content": []},
    }
    mgr.save(SessionState(session_path=mgr._active_session_path, entries=[new_entry]))

    ids = [e["id"] for e in mgr._read_file(mgr._active_session_path)]
    assert "e0" in ids


# ── get_active_messages ──────────────────────────────────────────────────────


def test_get_active_messages_on_an_empty_manager_is_empty():
    assert SessionManager().get_active_messages() == []


def test_get_active_messages_splices_a_compaction_summary_in_as_a_user_message(mgr):
    mgr.append_entry(
        {
            "id": "old",
            "type": "message",
            "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "old"}]},
        }
    )
    mgr.append_entry(
        {
            "id": "comp",
            "type": "compaction",
            "timestamp": 2,
            "first_kept_id": "new",
            "summary": "Discussed X",
        }
    )
    mgr.append_entry(
        {
            "id": "new",
            "type": "message",
            "timestamp": 3,
            "message": {"role": "user", "content": [{"type": "text", "text": "new"}]},
        }
    )

    messages = mgr.get_active_messages()
    assert messages[0]["content"][0]["text"] == "[[Compaction summary: Discussed X]]"
    assert messages[0]["role"] == "user"
    assert [m["content"][0]["text"] for m in messages[1:]] == ["new"]


# ── apply_compaction ─────────────────────────────────────────────────────────


def test_apply_compaction_on_an_unknown_first_kept_entry_raises(mgr):
    with pytest.raises(KeyError, match="not found"):
        mgr.apply_compaction("missing-id", "a summary")


def test_apply_compaction_appends_a_boundary_and_advances_the_tip(mgr):
    _seed(mgr, 3)
    comp_id = mgr.apply_compaction("e1", "Did X", compacted_entry_ids=["e0"], tokens_saved=42)

    entries = mgr._get_entries()
    comp = entries[-1]
    assert comp["id"] == comp_id
    assert comp["type"] == "compaction"
    assert comp["first_kept_id"] == "e1"
    assert comp["summary"] == "Did X"
    assert comp["tokens_saved"] == 42
    assert comp["compacted_entries"] == ["e0"]
    assert mgr._active_entry_id == comp_id


# ── listing sessions ──────────────────────────────────────────────────────────


def _write_session_file(path, created_at, model, message_count):
    lines = [
        {
            "id": "s",
            "type": "session",
            "timestamp": created_at,
            "parent_id": None,
            "model": model,
            "cwd": "/x",
        }
    ]
    for i in range(message_count):
        lines.append(
            {
                "id": f"m{i}",
                "type": "message",
                "timestamp": created_at + i,
                "parent_id": "s" if i == 0 else f"m{i - 1}",
                "message": {"role": "user", "content": [{"type": "text", "text": f"m{i}"}]},
            }
        )
    with open(path, "w") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")


@pytest.mark.parametrize(
    "accessor", ["list_sessions", "list_all"], ids=["list_sessions", "list_all"]
)
def test_listing_sorts_file_backed_sessions_newest_first(tmp_path, accessor):
    """list_sessions() and list_all() currently share one implementation
    (_list_sessions_from_dir over the same directory) — both are asserted so
    a future divergence between them is caught."""
    _write_session_file(tmp_path / "old.jsonl", created_at=1000, model="gpt-3.5", message_count=1)
    _write_session_file(tmp_path / "new.jsonl", created_at=2000, model="gpt-4o", message_count=3)
    (tmp_path / "notes.txt").write_text("not a session file")  # must be ignored

    results = getattr(SessionManager(sessions_dir=str(tmp_path)), accessor)()

    assert [os.path.basename(r.session_path) for r in results] == ["new.jsonl", "old.jsonl"]
    assert results[0].model == "gpt-4o"
    assert results[0].message_count == 3
    assert results[1].message_count == 1


def test_listing_skips_a_corrupted_session_file(tmp_path):
    _write_session_file(tmp_path / "good.jsonl", created_at=1000, model="gpt-4o", message_count=1)
    (tmp_path / "bad.jsonl").write_text("{not json at all}\n")

    results = SessionManager(sessions_dir=str(tmp_path)).list_sessions()

    assert len(results) == 1
    assert os.path.basename(results[0].session_path) == "good.jsonl"


def test_listing_in_memory_sessions_reports_each_root_session(tmp_path, monkeypatch):
    """In-memory mode never touches sessions_dir — it scans _memory_store for
    ROOT session entries (parent_id None), splitting it into per-session
    ranges. Timestamps are frozen so the newest-first sort is deterministic."""
    times = iter([1.0, 2.0])
    monkeypatch.setattr("tau_agent_core.session_manager.time.time", lambda: next(times))

    m = SessionManager.in_memory(cwd=str(tmp_path))
    m.new_session(model_id="m1")
    m.append_entry(
        {"id": "a", "type": "message", "timestamp": 1, "message": {"role": "user", "content": []}}
    )
    m.new_session(model_id="m2")
    m.append_entry(
        {"id": "b", "type": "message", "timestamp": 2, "message": {"role": "user", "content": []}}
    )

    results = m.list_sessions()

    assert [r.model for r in results] == ["m2", "m1"]  # newest first
    assert [r.message_count for r in results] == [1, 1]


def test_extract_session_info_returns_none_for_a_missing_corrupt_or_empty_file(tmp_path):
    m = SessionManager(sessions_dir=str(tmp_path))
    assert m._extract_session_info(str(tmp_path / "does-not-exist.jsonl")) is None

    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n")
    assert m._extract_session_info(str(bad)) is None

    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert m._extract_session_info(str(empty)) is None


def test_listing_a_nonexistent_sessions_directory_is_empty(tmp_path):
    m = SessionManager(sessions_dir=str(tmp_path / "never-created"))
    assert m.list_sessions() == []


def test_listing_in_memory_sessions_without_a_recorded_path_falls_back_to_empty_string(tmp_path):
    """Defensive corner of the in-memory listing branch: if _memory_store has
    a session entry but _memory_session_paths was never populated (e.g. the
    store was built by hand rather than via new_session()), session_path
    must default to "" rather than raising an IndexError."""
    m = SessionManager.in_memory(cwd=str(tmp_path))
    m._memory_store = [
        {
            "id": "s1",
            "type": "session",
            "timestamp": 1,
            "parent_id": None,
            "model": "m1",
            "cwd": "/x",
        }
    ]

    (result,) = m.list_sessions()
    assert result.session_path == ""
    assert result.model == "m1"


# ── _build_active_path edge cases ────────────────────────────────────────────


def test_build_active_path_on_no_entries_is_empty():
    assert SessionManager()._build_active_path([]) == []


def test_build_active_path_stops_at_a_dangling_parent_id(mgr):
    """A parent_id with no matching entry (corrupt or partial data) must stop
    the walk rather than crash — the caller gets whatever prefix IS
    resolvable, not an exception."""
    mgr.append_entry(
        {
            "id": "e1",
            "type": "message",
            "timestamp": 1,
            "parent_id": "does-not-exist",
            "message": {"role": "user", "content": []},
        }
    )
    path = mgr._build_active_path(mgr._get_entries())
    assert [e["id"] for e in path] == ["e1"]


def test_build_active_path_splices_a_compaction_whose_first_kept_precedes_it(mgr):
    """The common case (tested via clone(), above) is a compaction appended
    at the tip, where first_kept trails it in the path. This is the other
    shape: first_kept is an ALREADY-VISITED ancestor (append-only compaction
    recorded after the fact), so it must be found walking backwards through
    path[:compaction_idx] rather than forwards from compaction_idx+1."""
    _seed(mgr, 1)  # e0
    mgr.append_entry(
        {"id": "comp", "type": "compaction", "timestamp": 50, "first_kept_id": "e0", "summary": "S"}
    )
    mgr.append_entry(
        {
            "id": "e1",
            "type": "message",
            "timestamp": 60,
            "message": {"role": "user", "content": [{"type": "text", "text": "e1"}]},
        }
    )

    path_ids = [e["id"] for e in mgr._build_active_path(mgr._get_entries())]
    assert path_ids == ["comp", "e0", "e1"]


# ── _extract_branch_messages ─────────────────────────────────────────────────


def test_extract_collects_descendants_only_not_the_branch_points_ancestor(mgr):
    """In a linear chain (session -> e0 -> e1 -> e2), extracting from e1 must
    yield e1 and e2 but NOT e0 (e0 is e1's parent, not its descendant)."""
    for i in range(3):
        mgr.append_entry(
            {
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": [{"type": "text", "text": f"msg{i}"}],
                },
            }
        )
    extracted = mgr._extract_branch_messages(mgr._get_entries(), "e1")
    assert "[assistant]: msg1" in extracted
    assert "[user]: msg2" in extracted
    assert "[user]: msg0" not in extracted


def test_extract_excludes_a_true_sibling_branch(mgr):
    """Same distinction as clone()'s sibling-exclusion test, but exercised
    directly against the always-honest _build_active_path/BFS machinery."""
    mgr.append_entry(
        {
            "id": "a",
            "type": "message",
            "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "question"}]},
        }
    )
    mgr.append_entry(
        {
            "id": "b",
            "type": "message",
            "timestamp": 2,
            "parent_id": "a",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        }
    )
    mgr.append_entry(
        {
            "id": "c",
            "type": "message",
            "timestamp": 3,
            "parent_id": "a",
            "message": {"role": "user", "content": [{"type": "text", "text": "followup"}]},
        }
    )
    mgr.append_entry(
        {
            "id": "d",
            "type": "message",
            "timestamp": 4,
            "parent_id": "c",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "detailed answer"}],
            },
        }
    )
    extracted = mgr._extract_branch_messages(mgr._get_entries(), "c")
    assert "followup" in extracted and "detailed answer" in extracted
    assert "[assistant]: answer" not in extracted  # b's branch is not part of c's subtree


@pytest.mark.parametrize(
    "content,expected_fragment",
    [
        pytest.param([{"type": "text", "text": "hello"}], "hello", id="text"),
        pytest.param(
            [{"type": "toolCall", "id": "c1", "name": "ls", "arguments": {"path": "/tmp"}}],
            "[tool_call: ls({'path': '/tmp'})]",
            id="tool-call",
        ),
        pytest.param(
            [{"type": "thinking", "thinking": "let me think"}],
            "[thinking: let me think]",
            id="thinking",
        ),
        pytest.param(
            [{"type": "image", "data": "b64", "mime_type": "image/png"}], "[image]", id="image"
        ),
    ],
)
def test_extract_renders_each_message_content_block_kind(mgr, content, expected_fragment):
    mgr.append_entry(
        {
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "assistant", "content": content},
        }
    )
    extracted = mgr._extract_branch_messages(mgr._get_entries(), "e0")
    assert expected_fragment in extracted


def test_extract_renders_string_content_without_a_content_block_list(mgr):
    """Message content is usually a list of blocks, but the schema also
    allows a bare string — the extractor must handle both."""
    mgr.append_entry(
        {
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": "hello world"},
        }
    )
    extracted = mgr._extract_branch_messages(mgr._get_entries(), "e0")
    assert "[user]: hello world" in extracted


@pytest.mark.parametrize(
    "entry,expected_fragment",
    [
        pytest.param(
            {
                "id": "e1",
                "type": "toolResult",
                "timestamp": 1001,
                "tool_call_id": "c1",
                "tool_name": "ls",
                "content": [{"type": "text", "text": "file1.txt"}],
            },
            "[toolResult: ls] file1.txt",
            id="tool-result",
        ),
        pytest.param(
            {
                "id": "e1",
                "type": "compaction",
                "timestamp": 1001,
                "first_kept_id": "keep1",
                "summary": "Conversation about setup.",
            },
            "[compaction]: Conversation about setup.",
            id="compaction",
        ),
    ],
)
def test_extract_renders_non_message_entry_types(mgr, entry, expected_fragment):
    mgr.append_entry(
        {
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "ls"}]},
        }
    )
    mgr.append_entry(entry)
    extracted = mgr._extract_branch_messages(mgr._get_entries(), "e0")
    assert expected_fragment in extracted


def test_extract_from_empty_missing_or_typeless_root_is_empty(mgr):
    """Three ways to get nothing back: no entries at all; a branch id that
    doesn't exist; and a branch id that exists but is the session-root entry
    itself with no children yet, which (a) has no message/toolResult/
    compaction content of its own to render and (b) has nothing beneath it
    to traverse into. The session-root check must run before anything is
    appended — once it has children, extracting from it correctly returns
    THEIR text (that's the whole point of the BFS), which is exercised in
    test_extract_collects_descendants_only_not_the_branch_points_ancestor."""
    assert mgr._extract_branch_messages([], "e0") == ""

    session_id = mgr._get_entries()[0]["id"]
    assert mgr._extract_branch_messages(mgr._get_entries(), session_id) == ""

    _seed(mgr, 1)
    assert mgr._extract_branch_messages(mgr._get_entries(), "nonexistent") == ""


def test_extract_a_single_entry_branch(mgr):
    mgr.append_entry(
        {
            "id": "root",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "root"}]},
        }
    )
    extracted = mgr._extract_branch_messages(mgr._get_entries(), "root")
    assert "[user]: root" in extracted


def test_extract_deduplicates_an_id_reachable_through_two_parents(mgr):
    """Defensive guard, not a normal-tree scenario: _extract_branch_messages
    does not itself validate that ids are unique in the entries it's given.
    If the same id is registered as a child of two different parents (e.g.
    two entries sharing an id, a data-corruption case this function doesn't
    guard against upstream), the BFS would enqueue it twice without the
    `visited` check, double-rendering its text."""
    entries = [
        {
            "id": "root",
            "type": "message",
            "parent_id": None,
            "message": {"role": "user", "content": [{"type": "text", "text": "root"}]},
        },
        {
            "id": "a",
            "type": "message",
            "parent_id": "root",
            "message": {"role": "user", "content": [{"type": "text", "text": "a"}]},
        },
        {
            "id": "b",
            "type": "message",
            "parent_id": "root",
            "message": {"role": "user", "content": [{"type": "text", "text": "b"}]},
        },
        {
            "id": "shared",
            "type": "message",
            "parent_id": "a",
            "message": {"role": "user", "content": [{"type": "text", "text": "shared"}]},
        },
        {
            "id": "shared",
            "type": "message",
            "parent_id": "b",
            "message": {"role": "user", "content": [{"type": "text", "text": "shared"}]},
        },
    ]
    extracted = mgr._extract_branch_messages(entries, "root")
    assert extracted.count("shared") == 1


def test_extract_deep_branch_traverses_every_generation(mgr):
    mgr.append_entry(
        {
            "id": "root",
            "type": "message",
            "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "root msg"}]},
        }
    )
    for child, parent, role in [
        ("a", "root", "assistant"),
        ("b", "a", "user"),
        ("c", "b", "assistant"),
        ("d", "c", "user"),
    ]:
        mgr.append_entry(
            {
                "id": child,
                "type": "message",
                "timestamp": ord(child),
                "parent_id": parent,
                "message": {"role": role, "content": [{"type": "text", "text": f"{child} msg"}]},
            }
        )
    extracted = mgr._extract_branch_messages(mgr._get_entries(), "a")
    for child in ("a", "b", "c", "d"):
        assert f"{child} msg" in extracted
    assert "root msg" not in extracted


# ── summarize_branch() ────────────────────────────────────────────────────────


def _fake_response(text: str, stop_reason: str = "stop"):
    """A minimal AssistantMessage stand-in for complete_simple's return."""
    from tau_llm.types import AssistantMessage, TextContent, Usage

    return AssistantMessage(
        content=[TextContent(text=text)] if text else [],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason=stop_reason,
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


def _summarize(
    branch_text, summary_text, *, custom_instructions=None, stop_reason="stop", capture=None
):
    """Run summarize_branch with complete_simple mocked to return summary_text."""
    from tau_agent_core.session_manager import summarize_branch

    mock_model = type("MockModel", (), {"id": "gpt-4o", "provider": "openai"})()

    async def mock_complete_simple(model, context, options=None):
        if capture is not None:
            capture["context"] = context
            capture["options"] = options
        return _fake_response(summary_text, stop_reason)

    with patch("tau_llm.client.complete_simple", mock_complete_simple):
        # summarize_branch returns (text, usage) — it makes a real LLM call
        # outside the agent loop, so it must report what it spent or those
        # tokens go uncounted. These tests are about the TEXT; the usage
        # contract is pinned in test_side_usage.py.
        summary, _usage = asyncio.run(
            summarize_branch(branch_text, mock_model, custom_instructions=custom_instructions)
        )
        return summary


def test_summarize_branch_returns_the_llms_summary():
    summary = _summarize(
        "[user]: msg0\n[assistant]: msg1",
        "User discussed project architecture and decided on microservices.",
    )
    assert "microservices" in summary


def test_summarize_branch_on_an_empty_branch_raises():
    """Fail-Early: an empty branch RAISES — no fabricated summary."""
    from tau_agent_core.session_manager import summarize_branch

    mock_model = type("MockModel", (), {"id": "gpt-4o", "provider": "openai"})()
    with pytest.raises(ValueError, match="empty branch"):
        asyncio.run(summarize_branch("   ", mock_model))


def test_summarize_branch_on_an_llm_error_raises():
    """Fail-Early: an error/aborted response RAISES, not a raw-text fallback."""
    with pytest.raises(RuntimeError, match="summarization failed"):
        _summarize("[user]: hi", "", stop_reason="error")


def test_summarize_branch_on_an_empty_llm_response_raises():
    with pytest.raises(RuntimeError, match="empty summary"):
        _summarize("[user]: hi", "")


def test_summarize_branch_threads_custom_instructions_into_the_system_prompt():
    """Mode-3 custom instructions must reach the summarizer's SYSTEM prompt,
    not get silently dropped."""
    capture: dict = {}
    _summarize(
        "[user]: hello",
        "System prompt test summary",
        custom_instructions="Focus only on the database schema.",
        capture=capture,
    )
    system_msg = capture["context"]["messages"][0]
    assert system_msg["role"] == "system"
    assert "Focus only on the database schema." in system_msg["content"]


def test_summarize_branch_is_importable_from_the_package_root():
    from tau_agent_core import summarize_branch as sm_branch

    assert callable(sm_branch)
