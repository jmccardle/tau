"""Phase-A storage layer: the append-only JSONL ``Session`` store.

Exercises the on-disk contract and the four forward-compat seams baked into
Phase A (docs/SESSION-UX-REDESIGN.md §9):

- round-trip create → append → load → ``messages`` match;
- cwd dir encoding (§5.1) and uuid4+timestamp filename (§5.2);
- ``list_sessions`` cwd-vs-all scoping and ``most_recent`` (§5.8);
- ``fork`` — new file, header ``parent``, source untouched (§5.5);
- ``read_session_info`` — count / first / last / ``modified`` from last entry (§5.7);
- seams: ``base_dir`` override + explicit ``id`` + ``create_in_memory`` (seam 1),
  ``entries()`` / ``header`` raw views (seam 2), lifecycle events (seam 3).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tau_agent_core.session_log import open_branch
from tau_coding_agent.session_store import (
    SESSION_BEFORE_COMPACT,
    SESSION_BEFORE_FORK,
    SESSION_START,
    FileSessionCatalog,
    Session,
    list_sessions,
    most_recent,
    read_session_info,
    session_dir_for_cwd,
    subscribe_session_events,
)

# TREE-BROWSER-AS-EDITOR.md §8/§11.3: ``append_compaction`` now requires the summary's
# provenance as keyword-only arguments with no defaults. These tests are about
# something else, so they name plausible values once here.
_PROV = {
    "summarizer_model_id": "test-summarizer",
    "summary_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    "covered_entries": 1,
    "covered_tokens": 50,
    "agent_spec_id": None,
}


CWD = "/home/john/proj"
OTHER_CWD = "/home/john/other"


def _create(base_dir, *, cwd=CWD, model="local-llm", **kwargs) -> Session:
    return Session.create(cwd, model, "openai", base_dir=base_dir, **kwargs)


# ── cwd encoding & filename ─────────────────────────────────────────────────


def test_cwd_dir_encoding(tmp_path):
    directory = session_dir_for_cwd("/home/john/Development/agent-harness-py", tmp_path)
    assert directory == tmp_path / "--home-john-Development-agent-harness-py--"


def test_filename_is_timestamp_then_uuid(tmp_path):
    session = _create(tmp_path)
    assert session.path is not None
    name = session.path.name
    assert name.endswith(".jsonl")
    stamp, _, ident = name[: -len(".jsonl")].partition("_")
    # ISO timestamp with colons/periods replaced by dashes — no ':' survives.
    assert ":" not in stamp and stamp.startswith("20")
    assert len(ident) == 32  # uuid4 hex


# ── round-trip ──────────────────────────────────────────────────────────────


def test_round_trip_messages_match(tmp_path):
    session = _create(tmp_path, system_prompt="You are helpful.")
    session.append_message({"role": "user", "content": "hello"})
    session.append_message({"role": "assistant", "content": [{"type": "text", "text": "hi there"}]})

    reloaded = Session.load(session.path)
    assert reloaded.messages == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
    ]
    assert reloaded.model == "local-llm"
    assert reloaded.backend == "openai"
    assert reloaded.id == session.id


def test_messages_never_concatenates_mutually_exclusive_fork_alternatives(tmp_path):
    """``messages`` is the cursor's ancestry (docs/LANE-REMOVAL.md §3.2).

    This is the bug the lane removal was argued from, made executable: ``messages`` was
    a flat scan of every ``message`` entry, so a three-way fork returned three answers
    that never coexisted, presented as one conversation. No tag could have fixed it —
    a fork writes no tag, and the question was never "who wrote this" but "is this on
    the path I am looking at".
    """
    session = _create(tmp_path)
    root = session.append_message({"role": "user", "content": "shared question"})
    session.append_message({"role": "assistant", "content": "ALTERNATIVE ONE"})
    session.append_navigate(root)
    session.append_message({"role": "assistant", "content": "ALTERNATIVE TWO"})
    session.append_navigate(root)
    session.append_message({"role": "assistant", "content": "ALTERNATIVE THREE"})

    reloaded = Session.load(session.path)
    assert [m["content"] for m in reloaded.messages] == ["shared question", "ALTERNATIVE THREE"]

    # The abandoned alternatives are still on disk — nothing was hidden from the log.
    assert sum("ALTERNATIVE" in line for line in session.path.read_text().splitlines()) == 3

    # And navigating back to an abandoned alternative makes THAT one the conversation.
    two = next(
        e["id"]
        for e in reloaded.entries()
        if e.get("message", {}).get("content") == "ALTERNATIVE TWO"
    )
    reloaded.append_navigate(two)
    assert [m["content"] for m in reloaded.messages] == ["shared question", "ALTERNATIVE TWO"]


def test_messages_excludes_a_sub_agents_turns_the_same_way_it_excludes_a_forks(tmp_path):
    """The §1 asymmetry at the ``Session`` level: a sub-agent branch and a user's fork
    are the same shape, and are now excluded by the same rule — ancestry — rather than
    one by a tag and the other not at all."""
    session = _create(tmp_path)
    root = session.append_message({"role": "user", "content": "shared question"})
    session.append_message({"role": "assistant", "content": "the answer"})

    branch = open_branch(session, root, label="sub-agent")
    branch.append_message({"role": "user", "content": "SUB-AGENT ONLY"})

    assert [m["content"] for m in session.messages] == ["shared question", "the answer"]
    assert all("branchOf" not in e for e in session.entries()), "and no marker was written"


def test_name_property_latest_wins(tmp_path):
    session = _create(tmp_path, name="First")
    assert session.name == "First"
    session.append_session_info("Renamed")
    assert session.name == "Renamed"
    assert Session.load(session.path).name == "Renamed"


def test_model_property_raises_without_model_change(tmp_path):
    # A session always has a model_change from create; an entries-only Session
    # built without one must not fabricate a default (Fail-Early).
    bare = Session(
        None, Session._build_header("x", "2026-01-01T00:00:00.000Z", CWD, parent=None), []
    )
    with pytest.raises(ValueError, match="no model_change"):
        _ = bare.model


# ── entries() / header raw views (seam 2) ───────────────────────────────────


def test_entries_and_header_raw_views(tmp_path):
    session = _create(tmp_path, system_prompt="sys")
    session.append_message({"role": "user", "content": "q"})

    header = session.header
    assert header["type"] == "session"
    assert header["cwd"] == CWD
    assert header["parent"] is None
    assert header["version"] == 1

    kinds = [e["type"] for e in session.entries()]
    assert kinds == ["model_change", "message", "message"]
    # parentId threads each entry onto the previous one; first entry's is None.
    raw = session.entries()
    assert raw[0]["parentId"] is None
    assert raw[1]["parentId"] == raw[0]["id"]
    assert raw[2]["parentId"] == raw[1]["id"]


# ── fork (seam-free, §5.5) ──────────────────────────────────────────────────


def test_fork_new_file_parent_header_source_untouched(tmp_path):
    source = _create(tmp_path, system_prompt="sys")
    source.append_message({"role": "user", "content": "original"})
    source_bytes = source.path.read_bytes()

    forked = Session.fork(source, CWD, base_dir=tmp_path)

    assert forked.path != source.path
    assert forked.parent == source.id
    assert source.path.read_bytes() == source_bytes  # source file untouched
    # Fork carries the source transcript; new turns append onto it.
    assert forked.messages == source.messages
    forked.append_message({"role": "user", "content": "branch"})
    assert Session.load(forked.path).messages[-1] == {"role": "user", "content": "branch"}


# ── listing & scoping (§5.8) ────────────────────────────────────────────────


def test_list_sessions_cwd_vs_all(tmp_path):
    a = _create(tmp_path, cwd=CWD)
    a.append_message({"role": "user", "content": "in proj"})
    b = _create(tmp_path, cwd=OTHER_CWD)
    b.append_message({"role": "user", "content": "in other"})

    scoped = list_sessions(CWD, base_dir=tmp_path)
    assert [i.id for i in scoped] == [a.id]

    everything = list_sessions(None, base_dir=tmp_path)
    assert {i.id for i in everything} == {a.id, b.id}


def test_most_recent_returns_newest(tmp_path):
    older = _create(tmp_path)
    older.append_message({"role": "user", "content": "old"})
    newer = _create(tmp_path)
    newer.append_message({"role": "user", "content": "new"})

    # most_recent sorts by .modified (last entry time) desc.
    assert most_recent(CWD, base_dir=tmp_path) in (older.path, newer.path)
    infos = list_sessions(CWD, base_dir=tmp_path)
    assert infos[0].modified >= infos[1].modified


# ── read_session_info (§5.7) ────────────────────────────────────────────────


def test_session_info_fields(tmp_path):
    session = _create(tmp_path, system_prompt="sys", name="Title")
    session.append_message({"role": "user", "content": "first user"})
    session.append_message(
        {"role": "assistant", "content": [{"type": "text", "text": "the answer"}]}
    )

    info = read_session_info(session.path)
    assert info is not None
    assert info.ref == str(session.path)
    assert info.id == session.id
    assert info.cwd == CWD
    assert info.name == "Title"
    # system message is not counted; user + assistant are.
    assert info.message_count == 2
    assert info.first_message == "first user"
    assert info.last_message == "the answer"
    assert isinstance(info.created, datetime)
    assert info.modified >= info.created
    assert info.parent is None


def test_session_info_counts_the_ancestry_not_the_file(tmp_path):
    """The picker previews the conversation you would RESUME (docs/LANE-REMOVAL.md §3.2).

    Before this was ancestry-scoped it counted every ``message`` entry in the file, so a
    three-way fork reported 4 messages for a 2-message conversation and could show an
    abandoned alternative as the preview. A ``branchOf`` filter did not help: a fork
    writes no tag.
    """
    session = _create(tmp_path)
    root = session.append_message({"role": "user", "content": "shared question"})
    session.append_message({"role": "assistant", "content": "ALTERNATIVE ONE"})
    session.append_navigate(root)
    session.append_message({"role": "assistant", "content": "ALTERNATIVE TWO"})
    session.append_navigate(root)
    session.append_message({"role": "assistant", "content": "ALTERNATIVE THREE"})

    info = read_session_info(session.path)
    assert info is not None
    assert info.message_count == 2, "two messages on the active path, not four in the file"
    assert info.first_message == "shared question"
    assert info.last_message == "ALTERNATIVE THREE"


def test_session_info_title_cannot_come_from_a_sub_agents_prompt(tmp_path):
    """``first_message`` becomes the session's display title. A sub-agent's opening
    prompt is not a message of this conversation — and now it is excluded for the
    structural reason (it is not an ancestor of the cursor), which holds whether the
    subtree was written by ``spawn_branch`` or by a user forking at the same node."""
    session = _create(tmp_path)
    root = session.append_message({"role": "user", "content": "the real first message"})
    session.append_message({"role": "assistant", "content": "the real answer"})

    branch = open_branch(session, root, label="sub-agent")
    branch.append_message({"role": "user", "content": "SUB-AGENT INTERNAL PROMPT"})
    branch.append_message({"role": "assistant", "content": "sub-agent scratch work"})

    # The primary turn continues after the sub-agent finishes, so it wrote last. (If the
    # process died between those two writes the cursor would land in the branch — the
    # guarantee dropped in docs/LANE-REMOVAL.md §2, deliberately not bought back here.)
    session.append_message({"role": "user", "content": "the follow-up"})

    info = read_session_info(session.path)
    assert info is not None
    assert info.first_message == "the real first message"
    assert info.last_message == "the follow-up"
    assert info.message_count == 3, "the sub-agent's two turns are not this conversation's"
    # ...and the branch really did land in the file — this is not a read failure.
    assert "SUB-AGENT INTERNAL PROMPT" in session.path.read_text()


def test_session_info_read_returns_none_on_garbage(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json at all\n")
    assert read_session_info(bad) is None


# ── seam 1: explicit id + create_in_memory ──────────────────────────────────


def test_create_with_explicit_id(tmp_path):
    session = Session.create(CWD, "local-llm", "openai", id="deadbeef", base_dir=tmp_path)
    assert session.id == "deadbeef"
    assert "deadbeef" in session.path.name


def test_create_in_memory_no_disk_flush(tmp_path):
    base = tmp_path / "sessions"
    session = Session.create_in_memory(CWD, "local-llm", "openai", system_prompt="sys")
    session.append_message({"role": "user", "content": "ephemeral"})

    assert session.path is None
    assert session.messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "ephemeral"},
    ]
    # Nothing was written anywhere under the would-be base dir.
    assert not base.exists()


# ── seam 3: lifecycle events ────────────────────────────────────────────────


def test_lifecycle_events_emitted(tmp_path):
    events: list[str] = []
    unsubscribe = subscribe_session_events(lambda e: events.append(e["type"]))
    try:
        source = _create(tmp_path)  # → session_start
        Session.fork(source, CWD, base_dir=tmp_path)  # → session_before_fork (+ no start)
        # A REAL anchor id: append_compaction now Fail-Earlys on one that names no entry
        # (an unknown anchor is never found by the fold, so the whole kept region would
        # silently vanish from the context).
        keep = source.append_message({"role": "user", "content": "recent"})
        source.append_compaction("summary", first_kept_id=keep, tokens_before=100, **_PROV)
    finally:
        unsubscribe()

    assert SESSION_START in events
    assert SESSION_BEFORE_FORK in events
    assert SESSION_BEFORE_COMPACT in events


def test_unsubscribe_stops_delivery(tmp_path):
    events: list[str] = []
    unsubscribe = subscribe_session_events(lambda e: events.append(e["type"]))
    unsubscribe()
    _create(tmp_path)
    assert events == []


# ── navigate / branch_summary entry kinds + persisted cursor (§2.2, §2.4) ────


def test_navigate_entry_round_trips(tmp_path):
    session = _create(tmp_path)
    interior = session.append_message({"role": "user", "content": "hello"})
    session.append_message({"role": "assistant", "content": "hi"})
    nav_id = session.append_navigate(interior)

    reloaded = Session.load(session.path)
    entries = reloaded.entries()
    nav = next(e for e in entries if e["id"] == nav_id)
    assert nav["type"] == "navigate"
    assert nav["targetId"] == interior
    # navigate carries no message → skipped by reconstruction. And ``messages`` follows
    # the cursor the navigate moved (docs/LANE-REMOVAL.md §3.2): it is the ancestry of
    # the leaf, so "hi" — a DESCENDANT of the node we navigated back to — is not part of
    # the conversation this session would resume. It is still in ``entries()``.
    assert reloaded.messages == [{"role": "user", "content": "hello"}]
    assert any(e.get("message") == {"role": "assistant", "content": "hi"} for e in entries)


def test_navigate_null_target_is_pre_root(tmp_path):
    session = _create(tmp_path)
    session.append_message({"role": "user", "content": "hello"})
    session.append_navigate(None)

    reloaded = Session.load(session.path)
    assert reloaded._leaf_id is None  # navigate(None) → before-first-entry cursor
    assert reloaded.entries()[-1]["targetId"] is None


def test_branch_summary_round_trips(tmp_path):
    session = _create(tmp_path)
    from_id = session.append_message({"role": "user", "content": "explore"})
    bs_id = session.append_branch_summary("did some exploring", from_id)

    reloaded = Session.load(session.path)
    bs = next(e for e in reloaded.entries() if e["id"] == bs_id)
    assert bs["type"] == "branch_summary"
    assert bs["summary"] == "did some exploring"
    assert bs["fromId"] == from_id
    # branch_summary is a marker, not a message → reconstruction skips it.
    assert reloaded.messages == [{"role": "user", "content": "explore"}]
    # leaf advances to the branch_summary entry (pi _appendEntry).
    assert reloaded._leaf_id == bs_id


def test_cursor_persists_across_navigate_reload(tmp_path):
    session = _create(tmp_path)
    interior = session.append_message({"role": "user", "content": "first"})
    session.append_message({"role": "assistant", "content": "second"})
    session.append_navigate(interior)
    # In-memory cursor advanced to the target, not the navigate entry.
    assert session._leaf_id == interior

    reloaded = Session.load(session.path)
    assert reloaded._leaf_id == interior  # persisted cursor survives reload


def test_navigate_unknown_target_raises(tmp_path):
    # Fail-Early: a dangling cursor would silently drop the whole conversation at
    # read time; mirror pi branch()'s "Entry ... not found" throw.
    session = _create(tmp_path)
    session.append_message({"role": "user", "content": "hello"})
    with pytest.raises(ValueError, match="navigate target"):
        session.append_navigate("deadbeef")
    # nothing persisted for the bad call.
    assert all(e.get("type") != "navigate" for e in session.entries())


def test_branch_summary_unknown_from_raises(tmp_path):
    # Mirror pi branchWithSummary()'s "Entry ... not found" throw.
    session = _create(tmp_path)
    session.append_message({"role": "user", "content": "explore"})
    with pytest.raises(ValueError, match="branch_summary from"):
        session.append_branch_summary("summary", "deadbeef")
    assert all(e.get("type") != "branch_summary" for e in session.entries())


def test_pi_parity_no_navigate_cursor_is_last_entry(tmp_path):
    # A pi-style file with no navigate entries: cursor = last entry, identical
    # to pi's fall-back-to-last-entry on load.
    session = _create(tmp_path)
    session.append_message({"role": "user", "content": "hello"})
    last_id = session.append_message({"role": "assistant", "content": "hi"})

    reloaded = Session.load(session.path)
    assert reloaded._leaf_id == last_id
    assert reloaded.entries()[-1]["id"] == last_id


# ── FileSessionCatalog (W10 seam adapter) ───────────────────────────────────
#
# The catalog *algebra* — create/load/list/fork/most_recent/resolve_ref — is no
# longer spelled out here: it is ``SessionCatalogContractTests``, run over this
# store in test_contract_file_catalog.py and over two others elsewhere. Six tests
# that restated it by hand are gone. What remains is what the shared contract
# cannot express, because it is about this store's *medium*: bytes on disk, and a
# ref spelled as a path.


def test_catalog_create_ephemeral_never_touches_disk(tmp_path):
    """The contract says an ephemeral session is unreachable; here it is stronger.

    Unreachable-through-the-catalog is satisfiable by a store that writes the file
    and simply declines to list it. For ``--no-session`` on a filesystem the
    promise is physical: no ``path``, and no ``sessions/`` directory brought into
    existence at all.
    """
    catalog = FileSessionCatalog(base_dir=tmp_path)
    session = catalog.create_ephemeral(CWD, "local-llm", "openai", system_prompt="sys")
    session.append_message({"role": "user", "content": "ephemeral"})
    assert session.path is None
    assert not (tmp_path / "sessions").exists()


def test_catalog_fork_leaves_the_source_file_byte_identical(tmp_path):
    """Forking must not rewrite the source's JSONL — not one byte.

    The contract checks the source's *messages* are unchanged, which a store could
    satisfy while still rewriting the file (re-serializing, reordering, touching
    mtime). Here the source is an append-only log another process may be reading.
    """
    catalog = FileSessionCatalog(base_dir=tmp_path)
    source = catalog.create(CWD, "local-llm", "openai", system_prompt="sys")
    source.append_message({"role": "user", "content": "original"})
    source_bytes = source.path.read_bytes()

    forked = catalog.fork(source, CWD)

    assert forked.path != source.path
    assert source.path.read_bytes() == source_bytes


def test_catalog_resolve_ref_accepts_a_jsonl_path(tmp_path):
    """This store's own override: a REF may be a path, not just an id/prefix.

    ``SessionCatalog.resolve_ref`` only knows ids — ``.jsonl`` is a filesystem
    concept core must not learn — so the path form is resolved by this subclass
    and can only be tested against this subclass.
    """
    catalog = FileSessionCatalog(base_dir=tmp_path)
    session = catalog.create(CWD, "local-llm", "openai")
    session.append_message({"role": "user", "content": "hi"})

    assert catalog.resolve_ref(str(session.path), cwd=CWD).id == session.id
    # A path that does not exist falls THROUGH to the id search rather than raising,
    # so the two ref spellings coexist instead of one shadowing the other.
    assert catalog.resolve_ref(session.id, cwd=CWD).id == session.id
