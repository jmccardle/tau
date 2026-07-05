"""Tests for ``examples/38_todo.py`` — tree-backplane todo state + ``/todos`` (S62).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S62. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/todo.ts``.

Proves:

* the ``todo`` tool's list/add/toggle/clear logic, backed entirely by
  ``ext_kit.state.TreeStore`` — no side database;
* state is reconstructed FRESH from the active path on every mutating call (no
  stale in-memory cache), so it is automatically branch-correct (the S56
  active-path guarantee, exercised here through this extension's own usage);
* ``todo_extension`` actually registers the tool + the ``/todos`` command on a
  real ``AgentSession``, and ``/todos`` returns its report through the S46
  command-output channel (``ExtensionCommandResult.output``), not a UI popup;
* RELOAD-INVARIANCE: mutate via the tool, reload a real on-disk ``Session``
  (à la S56's own on-disk proof), and a fresh extension instance over the
  reloaded session reports the exact same state.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionAPI
from tau_ai.types import Model
from tau_coding_agent.session_store import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "38_todo.py"
_spec = importlib.util.spec_from_file_location("todo_38_example", _PATH)
assert _spec is not None and _spec.loader is not None
todo_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = todo_mod
_spec.loader.exec_module(todo_mod)


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


def _session(tmp_path: Path) -> Session:
    return Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)


def _api_for(store: Session) -> ExtensionAPI:
    session = AgentSession(session_log=store, model=_model(), extensions=[])
    return ExtensionAPI(session=session)


async def _call(store, action: str, **params: Any) -> dict[str, Any]:
    params = {"action": action, **params}
    return await todo_mod._todo_execute("call-1", params, None, None, None, store=store)


# ── tool logic: list / add / toggle / clear ──────────────────────────────────


async def test_list_on_empty_state(tmp_path):
    store = todo_mod.TreeStore(_api_for(_session(tmp_path)), todo_mod.TODO_CUSTOM_TYPE)
    result = await _call(store, "list")
    assert result["content"][0]["text"] == "No todos"
    assert result["details"]["todos"] == []
    # A pure list makes NO durable record.
    assert len(store.load()) == 0


async def test_add_then_list_reflects_it(tmp_path):
    store = todo_mod.TreeStore(_api_for(_session(tmp_path)), todo_mod.TODO_CUSTOM_TYPE)
    added = await _call(store, "add", text="buy milk")
    assert added["content"][0]["text"] == "Added todo #1: buy milk"
    assert added["details"]["todos"] == [{"id": 1, "text": "buy milk", "done": False}]

    listed = await _call(store, "list")
    assert listed["content"][0]["text"] == "[ ] #1: buy milk"


async def test_add_requires_text(tmp_path):
    store = todo_mod.TreeStore(_api_for(_session(tmp_path)), todo_mod.TODO_CUSTOM_TYPE)
    result = await _call(store, "add")
    assert "text required" in result["content"][0]["text"]
    assert result["details"]["error"] == "text required"
    assert len(store.load()) == 0  # error path makes no record


async def test_toggle_flips_done_and_persists(tmp_path):
    store = todo_mod.TreeStore(_api_for(_session(tmp_path)), todo_mod.TODO_CUSTOM_TYPE)
    await _call(store, "add", text="walk dog")

    toggled = await _call(store, "toggle", id=1)
    assert toggled["content"][0]["text"] == "Todo #1 completed"
    assert toggled["details"]["todos"] == [{"id": 1, "text": "walk dog", "done": True}]

    toggled_back = await _call(store, "toggle", id=1)
    assert toggled_back["content"][0]["text"] == "Todo #1 uncompleted"


async def test_toggle_requires_id(tmp_path):
    store = todo_mod.TreeStore(_api_for(_session(tmp_path)), todo_mod.TODO_CUSTOM_TYPE)
    result = await _call(store, "toggle")
    assert "id required" in result["content"][0]["text"]


async def test_toggle_unknown_id_reports_not_found(tmp_path):
    store = todo_mod.TreeStore(_api_for(_session(tmp_path)), todo_mod.TODO_CUSTOM_TYPE)
    await _call(store, "add", text="a")
    result = await _call(store, "toggle", id=999)
    assert result["content"][0]["text"] == "Todo #999 not found"


async def test_clear_empties_and_resets_next_id(tmp_path):
    store = todo_mod.TreeStore(_api_for(_session(tmp_path)), todo_mod.TODO_CUSTOM_TYPE)
    await _call(store, "add", text="a")
    await _call(store, "add", text="b")
    cleared = await _call(store, "clear")
    assert cleared["content"][0]["text"] == "Cleared 2 todos"

    added_after_clear = await _call(store, "add", text="fresh")
    assert added_after_clear["details"]["todos"] == [{"id": 1, "text": "fresh", "done": False}]


async def test_unknown_action_reports_error_and_makes_no_record(tmp_path):
    store = todo_mod.TreeStore(_api_for(_session(tmp_path)), todo_mod.TODO_CUSTOM_TYPE)
    result = await _call(store, "bogus")
    assert "Unknown action" in result["content"][0]["text"]
    assert len(store.load()) == 0


# ── branch correctness: TreeStore's active-path guarantee, exercised here ────


async def test_state_is_branch_correct(tmp_path):
    """Navigating the cursor away from a mutation makes that mutation invisible —
    exactly the S56 active-path guarantee, proven through this extension's own
    read (``_current_state``), not just the underlying store."""
    session_log = _session(tmp_path)
    api = _api_for(session_log)
    store = todo_mod.TreeStore(api, todo_mod.TODO_CUSTOM_TYPE)

    root_id = session_log.append_message({"role": "user", "content": "root"})
    await _call(store, "add", text="on-main")

    session_log.append_navigate(root_id)
    fresh_store = todo_mod.TreeStore(api, todo_mod.TODO_CUSTOM_TYPE)
    todos, next_id = todo_mod._current_state(fresh_store)
    assert todos == []
    assert next_id == 1


# ── extension wiring: registers on a real AgentSession, /todos via S46 ──────


def _session_with_todo(tmp_path: Path) -> AgentSession:
    session = AgentSession(session_log=_session(tmp_path), model=_model(), extensions=[])
    todo_mod.todo_extension(session._bind_extension_api("examples/38_todo.py"))
    return session


async def test_extension_registers_tool_and_command(tmp_path):
    session = _session_with_todo(tmp_path)
    assert session._registry.get_command("todos") is not None
    tool_names = [t.name for t in session._registry.get_all_tools()]
    assert "todo" in tool_names


async def test_todos_command_reports_via_output_channel(tmp_path):
    session = _session_with_todo(tmp_path)
    execute = session._registry._tools["todo"]["execute"]

    empty_report = await session.run_extension_command("todos", "")
    assert empty_report.handled is True
    assert empty_report.output == "No todos yet. Ask the agent to add some!"

    await execute("call-1", {"action": "add", "text": "buy milk"}, None, None, None)
    await execute("call-2", {"action": "add", "text": "walk dog"}, None, None, None)
    await execute("call-3", {"action": "toggle", "id": 1}, None, None, None)

    report = await session.run_extension_command("todos", "")
    assert report.handled is True
    assert "1/2 completed" in report.output
    assert "#1 buy milk" in report.output
    assert "#2 walk dog" in report.output


# ── RELOAD-INVARIANCE: real on-disk Session reload (S56 style) ─────────────


async def test_todo_state_survives_ondisk_reload(tmp_path):
    session_log = _session(tmp_path)
    api = _api_for(session_log)
    store = todo_mod.TreeStore(api, todo_mod.TODO_CUSTOM_TYPE)

    await _call(store, "add", text="buy milk")
    await _call(store, "add", text="walk dog")
    await _call(store, "toggle", id=2)

    reloaded_log = Session.load(session_log.path)
    reloaded_api = _api_for(reloaded_log)
    reloaded_store = todo_mod.TreeStore(reloaded_api, todo_mod.TODO_CUSTOM_TYPE)
    todos, next_id = todo_mod._current_state(reloaded_store)

    assert todos == [
        {"id": 1, "text": "buy milk", "done": False},
        {"id": 2, "text": "walk dog", "done": True},
    ]
    assert next_id == 3

    # And the /todos report over the reloaded session matches too.
    reloaded_session = AgentSession(session_log=reloaded_log, model=_model(), extensions=[])
    todo_mod.todo_extension(reloaded_session._bind_extension_api("examples/38_todo.py"))
    report = await reloaded_session.run_extension_command("todos", "")
    assert "1/2 completed" in report.output
    assert "#2 walk dog" in report.output
