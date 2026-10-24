"""``source: "agent"`` on the file store: written only by a sub-agent, kept on reload and fork.

docs/STEP-DEBUGGER.md §3.7.2 (Built, M1a).
"""

from __future__ import annotations

import json
from unittest.mock import patch

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import default_leaf
from tau_coding_agent.session_store import FileSessionCatalog, Session

from test_entry_annotations_store import _fake, _model


async def test_a_human_session_writes_no_source_field(tmp_path) -> None:
    store = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    session = AgentSession(session_log=store, model=_model())
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
        await session.prompt("go")
        await session.prompt("again")

    text = store.path.read_text(encoding="utf-8")
    assert '"source"' not in text
    assert all("source" not in json.loads(line) for line in text.splitlines())


async def test_a_childs_source_survives_reload_and_fork(tmp_path) -> None:
    store = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    session = AgentSession(session_log=store, model=_model())
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
        await session.prompt("go")
        main_leaf = session.cursor.leaf
        assert (await session.spawn(main_leaf, "child", tools=[])).ok

    reloaded = Session.load(store.path)
    stamped = [e for e in reloaded.entries() if e.get("source") == "agent"]
    assert [e["message"]["role"] for e in stamped] == ["user", "assistant"]
    lines = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert sum(1 for line in lines if line.get("source") == "agent") == 3, "the open line too"
    assert default_leaf(reloaded.entries()) == main_leaf

    child_leaf = reloaded.entries()[-1]["id"]
    forked = FileSessionCatalog(base_dir=tmp_path).fork(reloaded, "/tmp", at=child_leaf)
    copied = Session.load(forked.path).entries()
    assert [e.get("source") for e in copied][-2:] == ["agent", "agent"]
    assert default_leaf(copied) == main_leaf
