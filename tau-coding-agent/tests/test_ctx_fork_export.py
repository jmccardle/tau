"""E3-ctx / step S19 — ``ExtensionContext.fork(mode="export")`` over a file log.

The in-memory arm of ``fork`` is covered in tau-agent-core; the export arm needs a
concrete file-backed ``session_store.Session`` (the only ``SessionLog`` with a
``fork`` classmethod), so it lives here where importing it is natural.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tau_ai.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_coding_agent.session_store import Session


@pytest.fixture(autouse=True)
def _isolate_tau_dir(tmp_path, monkeypatch):
    """Redirect the default sessions base (``TAU_DIR``) so an export fork — which
    calls ``Session.fork`` with the default ``base_dir`` — writes under tmp_path
    instead of the real ``~/.tau``."""
    monkeypatch.setattr("tau_coding_agent.session_store.TAU_DIR", tmp_path / "tau")


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="gpt-4o",
        api="openai-completions",
        provider="openai",
        base_url="http://localhost",
        context_window=128000,
        max_tokens=4096,
    )


def _session_on(tmp_path) -> tuple[AgentSession, Session]:
    live = Session.create(str(tmp_path), "gpt-4o", "openai", base_dir=tmp_path)
    agent = AgentSession(
        session_log=live,
        model=_model(),
        compaction_settings=CompactionSettings(enabled=False),
    )
    return agent, live


async def test_fork_export_writes_a_new_file_and_leaves_source_untouched(tmp_path):
    agent, live = _session_on(tmp_path)
    live.append_message({"role": "user", "content": "hello"})
    live.append_message({"role": "assistant", "content": "hi"})
    source_entries_before = live.entries()
    ctx = agent._extension_api.context

    new_path = await ctx.fork(mode="export")

    # A NEW file distinct from the source, on disk.
    assert isinstance(new_path, str)
    assert new_path != str(live.path)
    assert Path(new_path).exists()

    # The source log is never touched (append-only fork copies, §5.5).
    assert live.entries() == source_entries_before

    # The fork is a self-contained copy whose header parent is the source id,
    # carrying the same message entries as the source (plus the init entries
    # Session.create writes — model_change / session_info).
    forked = Session.load(Path(new_path))
    assert forked.parent == live.id
    assert [e["type"] for e in forked.entries() if e["type"] == "message"] == [
        "message",
        "message",
    ]
    assert forked.entries() == source_entries_before


async def test_fork_export_positions_cursor_at_entry_id(tmp_path):
    agent, live = _session_on(tmp_path)
    live.append_message({"role": "user", "content": "u0"})
    first_asst = live.append_message({"role": "assistant", "content": "a0"})
    live.append_message({"role": "user", "content": "u1"})
    live.append_message({"role": "assistant", "content": "a1"})
    ctx = agent._extension_api.context

    new_path = await ctx.fork(first_asst, mode="export")

    forked = Session.load(Path(new_path))
    # The fork copied every entry, then a navigate positioned its cursor at the
    # requested branch point, so its active context is truncated there.
    assert forked.cursor == first_asst
    assert len(forked.context) == 2
    # Source cursor is unchanged.
    assert live.cursor != first_asst
