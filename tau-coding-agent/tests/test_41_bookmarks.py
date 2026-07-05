"""Tests for ``examples/41_bookmarks.py`` — labeled tree waypoints (S64 row 1).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S64. No pi original (τ-native —
see the module docstring for why).

Proves:

* ``/bookmark <label>`` records the CURRENT active-path leaf (via
  ``ext_kit.state.active_cursor``, not the raw last log entry) as a durable
  ``customEntry`` waypoint (``ext_kit.state.TreeStore``, S56);
* ``/bookmarks`` reports every waypoint through the S46 command-output channel;
* ``/goto <label>`` moves the session cursor via ``ctx.navigate`` — zero LLM
  calls — to exactly the bookmarked entry;
* bookmarking after a ``navigate`` away from the tip names the entry the user
  is actually looking at, not the ``navigate`` bookkeeping entry itself (the
  reason ``active_cursor`` exists instead of ``ctx.entries()[-1]``);
* RELOAD-INVARIANCE: a fresh extension instance over a reloaded on-disk
  ``Session`` reports the exact same bookmarks.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tau_agent_core.agent_session import AgentSession
from tau_ai.types import Model

from tau_coding_agent.session_store import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "41_bookmarks.py"
_spec = importlib.util.spec_from_file_location("bookmarks_41_example", _PATH)
assert _spec is not None and _spec.loader is not None
bookmarks_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bookmarks_mod
_spec.loader.exec_module(bookmarks_mod)


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _session_with_bookmarks(tmp_path: Path) -> tuple[AgentSession, Session]:
    live = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    agent = AgentSession(session_log=live, model=_model(), extensions=[])
    bookmarks_mod.bookmarks_extension(agent._bind_extension_api("examples/41_bookmarks.py"))
    return agent, live


async def test_extension_registers_all_three_commands(tmp_path):
    agent, _live = _session_with_bookmarks(tmp_path)
    for name in ("bookmark", "bookmarks", "goto"):
        assert agent._registry.get_command(name) is not None


async def test_bookmark_with_no_history_reports_and_does_not_crash(tmp_path):
    agent, _live = _session_with_bookmarks(tmp_path)
    result = await agent.run_extension_command("bookmark", "start")
    assert result.handled is True
    assert result.output == "Nothing to bookmark yet — start a conversation first."


async def test_bookmark_requires_a_label(tmp_path):
    agent, live = _session_with_bookmarks(tmp_path)
    live.append_message(_msg("user", "hi"))
    result = await agent.run_extension_command("bookmark", "   ")
    assert result.output == "Usage: /bookmark <label>"


async def test_bookmark_records_current_leaf_and_bookmarks_lists_it(tmp_path):
    agent, live = _session_with_bookmarks(tmp_path)
    live.append_message(_msg("user", "hello"))
    leaf_id = live.append_message(_msg("assistant", "hi there"))

    bookmark_result = await agent.run_extension_command("bookmark", "greeting")
    assert bookmark_result.output == f"Bookmarked 'greeting' at {leaf_id}"

    list_result = await agent.run_extension_command("bookmarks", "")
    assert list_result.output == f"greeting -> {leaf_id}"


async def test_bookmarks_empty_report(tmp_path):
    agent, _live = _session_with_bookmarks(tmp_path)
    result = await agent.run_extension_command("bookmarks", "")
    assert result.output == "No bookmarks yet. Use /bookmark <label> to create one."


async def test_rebookmarking_a_label_moves_it_not_duplicates(tmp_path):
    agent, live = _session_with_bookmarks(tmp_path)
    first_id = live.append_message(_msg("user", "u0"))
    await agent.run_extension_command("bookmark", "here")
    second_id = live.append_message(_msg("assistant", "a0"))
    await agent.run_extension_command("bookmark", "here")

    result = await agent.run_extension_command("bookmarks", "")
    assert result.output == f"here -> {second_id}"
    assert first_id != second_id


async def test_goto_moves_the_cursor_to_the_bookmarked_entry(tmp_path):
    agent, live = _session_with_bookmarks(tmp_path)
    branch_point = live.append_message(_msg("user", "before"))
    await agent.run_extension_command("bookmark", "before-point")
    live.append_message(_msg("assistant", "after"))
    assert live.cursor != branch_point

    goto_result = await agent.run_extension_command("goto", "before-point")
    assert goto_result.output == f"Jumped to bookmark 'before-point' ({branch_point})"
    assert live.cursor == branch_point


async def test_goto_unknown_label_reports_error(tmp_path):
    agent, live = _session_with_bookmarks(tmp_path)
    live.append_message(_msg("user", "hi"))
    result = await agent.run_extension_command("goto", "nope")
    assert result.output == "No bookmark named 'nope'"


async def test_goto_requires_a_label(tmp_path):
    agent, _live = _session_with_bookmarks(tmp_path)
    result = await agent.run_extension_command("goto", "")
    assert result.output == "Usage: /goto <label>"


async def test_bookmark_after_navigate_names_the_target_not_the_navigate_entry(tmp_path):
    """The reason ``active_cursor`` exists instead of ``ctx.entries()[-1]``: once
    the user has navigated away from the tip, the last RAW entry is the
    ``navigate`` bookkeeping node itself — a bookmark must name where the user
    actually is (the navigate's target), not that node."""
    agent, live = _session_with_bookmarks(tmp_path)
    root_id = live.append_message(_msg("user", "root"))
    live.append_message(_msg("assistant", "branch A"))
    live.append_navigate(root_id)  # cursor now sits at root_id, last raw entry is "navigate"

    result = await agent.run_extension_command("bookmark", "back-at-root")
    assert result.output == f"Bookmarked 'back-at-root' at {root_id}"


async def test_bookmarks_survive_reload(tmp_path):
    """Reload-invariance: a fresh AgentSession/extension over the reloaded
    on-disk Session reports the exact same bookmarks."""
    agent, live = _session_with_bookmarks(tmp_path)
    leaf_id = live.append_message(_msg("user", "hello"))
    await agent.run_extension_command("bookmark", "greeting")
    session_path = live.path
    assert session_path is not None

    reloaded_log = Session.load(session_path)
    reloaded_agent = AgentSession(session_log=reloaded_log, model=_model(), extensions=[])
    bookmarks_mod.bookmarks_extension(
        reloaded_agent._bind_extension_api("examples/41_bookmarks.py")
    )

    result = await reloaded_agent.run_extension_command("bookmarks", "")
    assert result.output == f"greeting -> {leaf_id}"
