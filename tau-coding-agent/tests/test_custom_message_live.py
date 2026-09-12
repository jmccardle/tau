"""An extension's durable message reaches the transcript when it is appended.

Reference: docs/EXTENSION-LOCKS.md §9.1. ``api.send_message`` writes a
``customMessage`` node to the tree and the turn's streaming events say nothing
about it, so a display built from those events had no way to learn it existed.
The symptoms were three, all one cause:

* called from a TOOL, the note appeared only after the window was next rebuilt;
* called from a COMMAND handler, ``TauApp.messages`` was never re-read either, so
  it was gone at the next rebuild and came back only after a restart;
* what looked like the note arriving was the command's RETURN VALUE, rendered as
  display-only ``system`` chrome by ``_render_command_output``.

The fix is one channel (``custom_message``) and one re-read
(``TauApp._resync_working_list``). These tests drive the real ``TauApp`` over a
real ``TauBackend`` and ``AgentSession``, because every hop between the append
and the widget is where this went wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tau_agent_core.conversation_tree import ConversationTree
from tau_coding_agent import extension_ui, transcript

_EXAMPLE = str(Path(__file__).resolve().parents[2] / "examples" / "45_holy_grail.py")

_NOTE = """
def register(api):
    async def note(args, ctx):
        api.send_message({"customType": "probe", "content": f"note {args}".strip()})
    api.register_command("note", {"description": "append a note", "handler": note})
"""


def _roles(app: Any) -> list[str]:
    return [box.role for box in app.query(transcript.MessageBox)]


def _texts(app: Any) -> list[str]:
    return [str(box._content) for box in app.query(transcript.MessageBox)]


async def test_a_command_appended_message_is_mounted_and_kept(
    make_app: Any, wait_for_workers_settled: Any, tmp_path: Path
) -> None:
    """The command path: mounted at once, and still there after a window rebuild."""
    ext = tmp_path / "note_ext.py"
    ext.write_text(_NOTE)
    app = make_app(extension_paths=[str(ext)])
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        await app._dispatch_extension_command("note", "one")
        await pilot.pause()

        assert [m.get("role") for m in app.messages] == ["system", "custom"]
        assert _roles(app) == ["custom"]
        assert "note one" in _texts(app)[0]

        await app._reload_transcript()
        await pilot.pause()
        assert _roles(app) == ["custom"], "the note must survive a window rebuild"


async def test_the_command_return_value_is_separate_chrome(
    make_app: Any, wait_for_workers_settled: Any, tmp_path: Path
) -> None:
    """A handler that appends AND returns produces two boxes, and they differ.

    This is what made the original report confusing: the ``system`` box is the
    return value, never in ``messages``; the ``custom`` box is the durable node.

    The chrome lands FIRST because ``send_message`` is synchronous and its
    announcement is a task the loop runs at the next await, while
    ``_render_command_output`` mounts inline. Pinned rather than fixed: the
    chrome is transient (the next window rebuild drops it) so its position is
    not durable state, and the alternative is yielding to the loop before
    rendering, which is a sleep dressed as ordering.
    """
    ext = tmp_path / "both_ext.py"
    ext.write_text(
        "def register(api):\n"
        "    async def both(args, ctx):\n"
        "        api.send_message({'customType': 'probe', 'content': 'durable'})\n"
        "        return 'chrome'\n"
        "    api.register_command('both', {'description': 'x', 'handler': both})\n"
    )
    app = make_app(extension_paths=[str(ext)])
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        await app._dispatch_extension_command("both", "")
        await pilot.pause()

        assert _roles(app) == ["system", "custom"]
        assert _texts(app) == ["chrome", "durable"]
        assert [m.get("role") for m in app.messages] == ["system", "custom"]


async def test_a_turn_edge_appended_message_is_mounted(
    make_app: Any, wait_for_workers_settled: Any
) -> None:
    """The tool path, through ``examples/45_holy_grail.py``'s own ``user_turn_end``."""
    app = make_app(extension_paths=[_EXAMPLE])
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        session = app.current_backend.agent_session
        tools = {tool.name: tool for tool in session._build_turn_tools()}
        await tools["knights_of_ni"].execute(tool_call_id="c1", args={}, signal=None)
        assert _roles(app) == [], "the tool records; only the turn edge raises"

        await session._run_user_turn_end([])
        await pilot.pause()

        assert _roles(app) == ["custom"]
        assert "you have said" in _texts(app)[0]


async def test_the_ni_command_leaves_exactly_one_box(
    make_app: Any, wait_for_workers_settled: Any
) -> None:
    """``/ni`` returns nothing, so the note is not also echoed as chrome."""
    app = make_app(extension_paths=[_EXAMPLE])
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        await app._dispatch_extension_command("ni", "")
        await pilot.pause()

        assert _roles(app) == ["custom"]


_HIDDEN_NOTE = """
def register(api):
    async def quiet(args, ctx):
        api.send_message(
            {"customType": "probe", "content": "not for the transcript", "display": False}
        )
    api.register_command("quiet", {"description": "append a hidden note", "handler": quiet})
"""


async def test_a_hidden_note_is_on_the_tree_and_not_in_the_transcript(
    make_app: Any, wait_for_workers_settled: Any, tmp_path: Path
) -> None:
    """``display: False`` is obeyed by the transcript and by nothing else.

    Reference: docs/EXTENSION-MESSAGES.md §2. The key was stored and read by
    nobody, so an extension asking for a node the reader should not see got one
    the reader saw. The node stays on the tree, stays in ``app.messages``, and
    stays hidden across a window rebuild.
    """
    ext = tmp_path / "quiet_ext.py"
    ext.write_text(_HIDDEN_NOTE)
    app = make_app(extension_paths=[str(ext)])
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        await app._dispatch_extension_command("quiet", "")
        await pilot.pause()

        assert _roles(app) == [], "a hidden note must mount no transcript box"
        assert [m.get("role") for m in app.messages] == ["system", "custom"]

        await app._reload_transcript()
        await pilot.pause()
        assert _roles(app) == [], "and must stay hidden through a window rebuild"
        assert list(app.query(transcript.ExchangeBox)) == [], (
            "a span of only hidden nodes must not leave an empty exchange behind"
        )


async def test_the_tree_browser_still_draws_a_hidden_note(
    make_app: Any, wait_for_workers_settled: Any, tmp_path: Path
) -> None:
    """The guard is at the transcript's call sites, not in add_persisted_message.

    ``TreeDetailPane._render_entry`` calls that same method, so a guard inside it
    would have hidden the node from the one surface that must keep showing it.
    """
    ext = tmp_path / "quiet_ext.py"
    ext.write_text(_HIDDEN_NOTE)
    app = make_app(extension_paths=[str(ext)])
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        await app._dispatch_extension_command("quiet", "")
        await pilot.pause()

        session = app.current_session
        tree = ConversationTree(session.entries(), session.cursor)
        rows = [node.preview for node in tree.browse() if node.kind == "customMessage"]
        assert rows == ["probe (hidden): not for the transcript"]


async def test_bracketed_text_is_not_eaten_by_rich_markup(
    make_app: Any, wait_for_workers_settled: Any, tmp_path: Path
) -> None:
    """An extension's own words are shown, not parsed.

    ``Static`` interprets Rich console markup by default, which silently deletes
    a ``[bracketed]`` word — and ``examples/30_permission_gate.py`` puts a shell
    command in an ask body.
    """
    ext = tmp_path / "brackets_ext.py"
    ext.write_text(
        "def register(api):\n"
        "    async def gate(args, ctx):\n"
        "        api.request_user_action('Blocked: rm -rf [build]', lock=True, release='gate')\n"
        "    api.register_command('gate', {'description': 'x', 'handler': gate})\n"
    )
    app = make_app(extension_paths=[str(ext)])
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        await app._dispatch_extension_command("gate", "")
        await pilot.pause()

        row = app.query_one(extension_ui.ExtensionRequestBox)
        rendered = row.render()
        assert "[build]" in str(rendered), rendered
