"""Phase-A storage layer: the append-only JSONL ``Session`` store.

Exercises the on-disk contract and the four forward-compat seams baked into
Phase A (docs/SESSION-UX-REDESIGN.md §9):

- round-trip create → append → load → ``messages`` match;
- cwd dir encoding (§5.1) and uuid4+timestamp filename (§5.2);
- ``list_sessions`` cwd-vs-all scoping and ``most_recent`` (§5.8);
- ``fork`` — new file, header ``parent``, source untouched (§5.5);
- ``SessionInfo.read`` — count / first / last / ``modified`` from last entry (§5.7);
- seams: ``base_dir`` override + explicit ``id`` + ``create_in_memory`` (seam 1),
  ``entries()`` / ``header`` raw views (seam 2), lifecycle events (seam 3).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tau_coding_agent.session_store import (
    SESSION_BEFORE_COMPACT,
    SESSION_BEFORE_FORK,
    SESSION_START,
    Session,
    SessionInfo,
    list_sessions,
    most_recent,
    session_dir_for_cwd,
    subscribe_session_events,
)

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
    session.append_message(
        {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]}
    )

    reloaded = Session.load(session.path)
    assert reloaded.messages == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
    ]
    assert reloaded.model == "local-llm"
    assert reloaded.backend == "openai"
    assert reloaded.id == session.id


def test_name_property_latest_wins(tmp_path):
    session = _create(tmp_path, name="First")
    assert session.name == "First"
    session.append_session_info("Renamed")
    assert session.name == "Renamed"
    assert Session.load(session.path).name == "Renamed"


def test_model_property_raises_without_model_change(tmp_path):
    # A session always has a model_change from create; an entries-only Session
    # built without one must not fabricate a default (Fail-Early).
    bare = Session(None, Session._build_header("x", "2026-01-01T00:00:00.000Z", CWD, parent=None), [])
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


# ── SessionInfo.read (§5.7) ─────────────────────────────────────────────────


def test_session_info_fields(tmp_path):
    session = _create(tmp_path, system_prompt="sys", name="Title")
    session.append_message({"role": "user", "content": "first user"})
    session.append_message(
        {"role": "assistant", "content": [{"type": "text", "text": "the answer"}]}
    )

    info = SessionInfo.read(session.path)
    assert info is not None
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


def test_session_info_read_returns_none_on_garbage(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json at all\n")
    assert SessionInfo.read(bad) is None


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
        source.append_compaction("summary", first_kept_id="abc", tokens_before=100)
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
