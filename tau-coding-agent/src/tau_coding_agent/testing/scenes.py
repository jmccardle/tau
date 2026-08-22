"""Named app states worth looking at, for screenshots and visual regression.

A scene puts a sandboxed ``Parley`` into ONE known, settled state and nothing
more. Rendering it is somebody else's job (:mod:`tau_coding_agent.testing.render`),
and so is deciding what to do with the result (:mod:`tau_coding_agent.devshot`, a
snapshot test).

Two rules the scenes follow, both learned the hard way:

* **Host modals in the real app.** A modal composed inside a throwaway ``App``
  loses ``CSS_PATH = "parley.tcss"`` and renders full-screen — a screenshot that
  flatly contradicts what a user sees.
* **No live data.** No clocks, no random ids, no absolute paths, no hostnames.
  Everything a scene shows is written here, so two runs produce the same pixels.

Reference: docs/textual-headless-testing.md
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator

__all__ = [
    "SCENES",
    "Scene",
    "arrange_scene",
    "get_scene",
    "open_scene",
    "scene_names",
    "stage_scene",
]


# ---------------------------------------------------------------------------
# Fixture data (frozen — see the "no live data" rule above)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = "You are tau, a coding agent. Be concise."

_LONG_REQUEST = (
    "The streaming accumulator drops tool-call argument fragments instead of "
    "concatenating them, so any call whose arguments span more than one chunk "
    "arrives as invalid JSON. Fix it and add a regression test."
)

_REASONING = (
    "The provider streams delta.tool_calls[].function.arguments as fragments, one "
    "piece per chunk. _Accumulator assigns instead of appending, so the last "
    "fragment wins and the JSON is truncated. I need to look at the accumulator "
    "and then at the test that should have caught this."
)

_LONG_ANSWER = """## What I changed

`_Accumulator.add_tool_call_delta` assigned the incoming fragment over the stored
one. It now appends, which is what the OpenAI wire format requires.

```python
# before
call.arguments = delta.arguments
# after
call.arguments += delta.arguments
```

Three consequences worth knowing about:

1. A call whose arguments arrive in one chunk behaves exactly as before, so no
   cloud-provider transcript changes.
2. A local server that streams aggressively — vLLM and Ollama both do — now
   produces parseable JSON where it used to produce a truncated object.
3. The parser raises on a *complete* invalid payload rather than fabricating
   `{"raw": <string>}`, so a genuinely malformed call surfaces instead of
   reaching the tool as a plausible-looking argument dict.

The regression test drives the accumulator with a fragmented call and asserts on
the parsed arguments, not on the raw string.
"""


def _messages_answer_only() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "Which file parses the streaming tool calls?"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "`tau-llm/src/tau_llm/providers/openai.py` — the `_Accumulator` "
                        "class inside `stream_chat`."
                    ),
                }
            ],
            "usage": {"total_tokens": 412},
        },
    ]


def _messages_with_tools() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _LONG_REQUEST},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": _REASONING},
                {"type": "text", "text": "Let me read the accumulator first."},
                {
                    "type": "toolCall",
                    "id": "call_1",
                    "name": "read",
                    "arguments": {
                        "path": "tau-llm/src/tau_llm/providers/openai.py",
                        "offset": 340,
                        "limit": 40,
                    },
                },
                {
                    "type": "toolCall",
                    "id": "call_2",
                    "name": "grep",
                    "arguments": {"pattern": "arguments", "path": "tau-llm/tests"},
                },
            ],
            "usage": {"total_tokens": 1840},
        },
        {
            "role": "toolResult",
            "tool_call_id": "call_1",
            "tool_name": "read",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "363:                    call.arguments = delta.arguments\n"
                        "364:                    call.name = delta.name or call.name"
                    ),
                }
            ],
        },
        {
            "role": "toolResult",
            "tool_call_id": "call_2",
            "tool_name": "grep",
            "content": [
                {
                    "type": "text",
                    "text": "tau-llm/tests/test_openai_provider.py:118: assert call.arguments == ...",
                }
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": _LONG_ANSWER}],
            "usage": {"total_tokens": 2310},
        },
    ]


_SESSION_NAMES = [
    "fix tool-call argument accumulation",
    "port the compaction anchor from pi",
    "why does the sidebar freeze on a large catalog",
    "docs: rewrite the four package docs",
    "add a NATS bus extension",
    "session tree browser: second pick for elide",
]


def _seed_sessions(home: Path) -> None:
    """Write a handful of named sessions into the sandbox's session dir.

    Ticking a fake clock rather than reading the real one, because the sidebar
    lists newest-first and the real one is not fine-grained enough to order this
    loop. ``session_store._now_iso`` has *millisecond* resolution; six sessions
    written back to back land inside one millisecond, so ``list_sessions``'
    sort finds every key equal and the order falls through to ``Path.glob`` —
    to inode order, which differs between runs on the same machine. The sidebar
    then shows these six names shuffled, which is fine for a screenshot and
    fatal for a snapshot.

    The anchor is still the real clock, and deliberately: the sidebar groups by
    recency, and "Today" is the group this scene is meant to show.
    """
    import tau_coding_agent.session_store as store
    from tau_coding_agent.session_store import FileSessionCatalog

    # One second per write, ending just before now — comfortably coarser than the
    # millisecond the timestamps are truncated to, and comfortably inside today.
    ticks = count(-4 * len(_SESSION_NAMES))
    start = datetime.now(timezone.utc)

    def _fake_now_iso() -> str:
        when = start + timedelta(seconds=next(ticks))
        return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"

    catalog = FileSessionCatalog(home / "sessions")
    real_now_iso = store._now_iso
    store._now_iso = _fake_now_iso
    try:
        for index, name in enumerate(_SESSION_NAMES):
            session = catalog.create(os.getcwd(), "m", "openai", name=name)
            session.append_message({"role": "user", "content": f"turn {index}"})
            session.append_message(
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
            )
            session.shutdown()
    finally:
        store._now_iso = real_now_iso


_BRANCH_SUMMARY = (
    "Abandoned branch: tried patching the parser instead of the accumulator; "
    "the fragments were already lost by then.\n\n"
    "The parser only ever sees the string the accumulator hands it, so by the "
    "time a fragment is missing there is nothing left to recover — which is why "
    "the fix moved one layer up."
)


def _tree_session() -> Any:
    """The session log the tree scene browses.

    A ``ConversationTree`` over real log entries rather than hand-built
    ``TreeNode``s, because the browser now has two views of the same node — the
    row (a one-line ``preview``) and the detail pane (the full body) — and only a
    real entry carries both. The turns are the ones ``_messages_with_tools``
    renders in the transcript, so the pane and the chat show the same
    conversation; ``n5`` adds the one thing a linear transcript cannot, an
    abandoned branch, and its summary runs past one line so the pane visibly says
    more than the row it sits beside.
    """
    from tau_agent_core.conversation_tree import ConversationTree

    system, user, tool_turn, read_result, grep_result, answer = _messages_with_tools()
    entries: list[dict[str, Any]] = [
        {"id": "n1", "parentId": None, "type": "message", "timestamp": 1, "message": system},
        {"id": "n2", "parentId": "n1", "type": "message", "timestamp": 2, "message": user},
        {"id": "n3", "parentId": "n2", "type": "message", "timestamp": 3, "message": tool_turn},
        {
            "id": "n5",
            "parentId": "n2",
            "type": "branch_summary",
            "timestamp": 4,
            "summary": _BRANCH_SUMMARY,
        },
        {"id": "r1", "parentId": "n3", "type": "message", "timestamp": 5, "message": read_result},
        {"id": "r2", "parentId": "r1", "type": "message", "timestamp": 6, "message": grep_result},
        {"id": "n4", "parentId": "r2", "type": "message", "timestamp": 7, "message": answer},
    ]
    return ConversationTree(entries, cursor="n4")


_PANEL_SPEC = {
    "title": "Fleet",
    "table": {
        "columns": ["agent", "state", "turns", "tokens"],
        "rows": [
            ["reviewer-1", "running", "4", "12.1k"],
            ["reviewer-2", "waiting", "0", "0"],
            ["implementer", "done", "9", "38.4k"],
        ],
    },
    "actions": [
        {"label": "Refresh", "command": "fleet", "args": "refresh"},
        {"label": "Stop all", "command": "fleet", "args": "stop"},
    ],
}


# ---------------------------------------------------------------------------
# Scene definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scene:
    """One named app state.

    ``seed`` runs against the sandbox directory *before* the app is constructed
    (so the sidebar's mount-time catalog fetch sees it). ``arrange`` runs after
    the first frame has settled, and may await the pilot.
    """

    name: str
    description: str
    arrange: Callable[[Any, Any], Awaitable[None]] | None = None
    seed: Callable[[Path], None] | None = None
    config: dict[str, Any] = field(default_factory=dict)


async def _noop(app: Any, pilot: Any) -> None:
    return None


async def _open_sidebar(app: Any, pilot: Any) -> None:
    """ctrl+b, then wait for the seeded session list to actually be on screen.

    The sidebar mounts CLOSED since §8, so the scene that exists to show it has
    to open it — through the same ``action_toggle_sidebar`` a user presses, not
    by writing ``display`` behind the app's back, or the scene would be a picture
    of a state the app cannot reach.

    The wait is the ``_settle_tree`` rule applied to a different deferral: the
    catalog listing runs on a THREAD worker, and while the sidebar is collapsed
    ``ChatSidebar._apply_sessions`` records the result and skips the render
    (``_render_pending``). Which side of the toggle that lands on decides which
    of the two catch-up paths draws the rows, and both are one more event-loop
    turn away. A fixed pause count would be a guess about how many; this waits
    for the rows and raises if they never come.
    """
    from tau_coding_agent.app import ChatListItem

    app.action_toggle_sidebar()
    for _ in range(30):
        await pilot.pause()
        if len(app.query(ChatListItem)) == len(_SESSION_NAMES):
            # One more, so the mounted rows have been laid out and composited.
            await pilot.pause()
            return
    raise AssertionError(
        f"the sidebar never listed the {len(_SESSION_NAMES)} seeded sessions "
        f"(showing {len(app.query(ChatListItem))})"
    )


async def _load_answer(app: Any, pilot: Any) -> None:
    from tau_coding_agent.app import ChatDisplay

    display = app.query_one(ChatDisplay)
    await display.reload_messages(_messages_answer_only())
    await pilot.pause()


async def _load_tools(app: Any, pilot: Any) -> None:
    from tau_coding_agent.app import ChatDisplay

    display = app.query_one(ChatDisplay)
    await display.reload_messages(_messages_with_tools())
    await pilot.pause()


async def _load_tools_expanded(app: Any, pilot: Any) -> None:
    from tau_coding_agent.chat_widgets import ExchangeBox, ReasoningRegion, ToolBox

    await _load_tools(app, pilot)
    for box in app.query(ExchangeBox):
        box.collapsed = False
    await pilot.pause()
    for box in list(app.query(ToolBox)) + list(app.query(ReasoningRegion)):
        box.collapsed = False
    await pilot.pause()
    await pilot.pause()


async def _open_tree_modal(app: Any, pilot: Any) -> None:
    from tau_coding_agent.app import SessionTreeModal

    tree = _tree_session()
    app.push_screen(SessionTreeModal(tree.tree(), resolve_entry=tree.entry))
    await _settle_tree(app, pilot, "n4")


async def _settle_tree(app: Any, pilot: Any, node_id: str, ticks: int = 30) -> None:
    """Pause until the browser's cursor AND its detail pane are on ``node_id``.

    A fixed pause count is a guess about how many event-loop turns a chain of
    ``call_after_refresh`` callbacks needs — the modal defers its initial cursor
    move, its relabel and the pane's first draw, and the pane then defers its own
    scroll positioning behind that. A guess that is right on an idle machine is
    wrong on a loaded one, and a scene that settles differently under load is a
    snapshot that flakes for reasons that have nothing to do with the code under
    test. So wait for the condition and raise if it never arrives.

    The pane is only required to have caught up when it is *drawn*: below
    ``SessionTreeModal.DETAIL_MIN_HEIGHT`` there is no pane to wait for, and the
    same scene is rendered on short terminals.
    """
    from textual.widgets import Tree

    from tau_coding_agent.app import TreeDetailPane

    # One pause before querying: `push_screen` is queued, so the modal's widgets
    # do not exist until the pump has run at least once.
    await pilot.pause()
    # `app.query_one` searches the DEFAULT screen, not the top of the stack, so a
    # widget inside a pushed modal is only reachable through `app.screen`.
    tree = app.screen.query_one("#tree-browser-tree", Tree)
    pane = app.screen.query_one(TreeDetailPane)

    def _settled() -> bool:
        cursor = tree.cursor_node
        if cursor is None or cursor.data != node_id:
            return False
        return not pane.display or pane.shown_id == node_id

    for _ in range(ticks):
        await pilot.pause()
        if _settled():
            # One more, so the pane's deferred scroll positioning lands too.
            await pilot.pause()
            return
    raise AssertionError(
        f"the tree browser never settled on {node_id!r} "
        f"(cursor={tree.cursor_node and tree.cursor_node.data!r}, pane={pane.shown_id!r})"
    )


async def _open_tree_modal_at_branch(app: Any, pilot: Any) -> None:
    """The tree browser with the cursor moved off the leaf, onto the fork.

    The leaf shows the pane's ordinary case (a previous message above the
    selection). ``n2`` shows the two it cannot: a node with more than one child,
    and a ``⋯`` row that has something to count in both directions.
    """
    from textual.widgets import Tree

    await _open_tree_modal(app, pilot)
    tree = app.screen.query_one("#tree-browser-tree", Tree)
    node = next(n for n in tree.root.children[0].children if n.data == "n2")
    tree.move_cursor(node)
    await _settle_tree(app, pilot, "n2")


async def _open_tree_mode_modal(app: Any, pilot: Any) -> None:
    from tau_coding_agent.app import TreeModeModal

    app.push_screen(TreeModeModal())
    await pilot.pause()
    await pilot.pause()


async def _open_prompt_editor(app: Any, pilot: Any) -> None:
    from tau_coding_agent.app import SystemPromptEditor

    app.push_screen(SystemPromptEditor(_SYSTEM_PROMPT))
    await pilot.pause()
    await pilot.pause()


async def _open_ext_surfaces(app: Any, pilot: Any) -> None:
    from tau_agent_core.extension_types import validate_panel_spec
    from tau_coding_agent.app import ExtensionPanelHost, ExtensionStatusBar

    await _load_answer(app, pilot)
    app.query_one(ExtensionPanelHost).set_panel("fleet", validate_panel_spec(_PANEL_SPEC))
    status = app.query_one(ExtensionStatusBar)
    status.set_slot("budget", "budget 38.4k / 200k")
    status.set_slot("gate", "permission gate: ask")
    await pilot.pause()
    await pilot.pause()


#: A plausible model entry for the two scenes whose chat column is EMPTY, so the
#: empty pane's ``model``/endpoint rows show something a reader recognizes.
#: ``sandbox.DEFAULT_CONFIG``'s ``m`` / ``openai`` is right for a widget test and
#: wrong for a published screenshot — it reads as a bug rather than as a local
#: server. Still entirely fictional: no scene may name a reachable host.
_EMPTY_PANE_CONFIG: dict[str, Any] = {
    "models": {
        "local-llm": {
            "backend": "openai",
            "model": "qwen3-coder-30b",
            "base_url": "http://localhost:8080/v1",
            "api_key": "not-needed",
        }
    },
    "default_model": "local-llm",
    "system_prompt": "sys",
}

SCENES: tuple[Scene, ...] = (
    Scene("empty", "Fresh app, no saved sessions.", _noop, config=_EMPTY_PANE_CONFIG),
    Scene(
        "sidebar",
        "Sidebar (ctrl+b) populated with named sessions, grouped by recency.",
        _open_sidebar,
        seed=_seed_sessions,
        config=_EMPTY_PANE_CONFIG,
    ),
    Scene("answer", "One user question and a short text-only answer.", _load_answer),
    Scene(
        "tools",
        "A full exchange (reasoning, two tool calls, results, long answer), collapsed.",
        _load_tools,
    ),
    Scene(
        "tools-expanded",
        "The same exchange with every collapsible open.",
        _load_tools_expanded,
    ),
    Scene("tree-modal", "The /tree browser over a branching tree.", _open_tree_modal),
    Scene(
        "tree-modal-branch",
        "The /tree browser with the cursor on the fork, both fold rows drawn.",
        _open_tree_modal_at_branch,
    ),
    Scene("tree-mode-modal", "The mode chooser shown after picking a node.", _open_tree_mode_modal),
    Scene("prompt-editor", "The system-prompt editor modal.", _open_prompt_editor),
    Scene(
        "ext-surfaces",
        "An extension panel plus two status-bar slots, over a loaded chat.",
        _open_ext_surfaces,
    ),
)


@contextmanager
def stage_scene(scene: Scene) -> Iterator[Any]:
    """Sandbox ``~/.tau`` into a temp dir, run *scene*'s seed, and yield the app.

    Everything :func:`open_scene` does *except* run the app — split out because
    not every caller may start it. ``pytest-textual-snapshot`` runs the app
    itself (``App.run`` with its own auto-pilot, so it can export the frame and
    exit), so it needs a constructed app and a live sandbox around it, which is
    exactly this. The sandbox must stay entered for the whole run: the app reads
    its config and writes its sessions while it is up.

    The caller must not leave this block before the app has stopped.
    """
    from tau_coding_agent.testing.sandbox import build_parley, sandbox_tau_home

    home = Path(tempfile.mkdtemp(prefix="tau-scene-"))
    try:
        with sandbox_tau_home(home):
            if scene.seed is not None:
                scene.seed(home)
            app = build_parley(home, config=scene.config or None)
            # The "no live data" rule applies to motion too: a frame captured
            # mid-animation differs from the same frame captured after it. The
            # documented switch is TEXTUAL_ANIMATIONS=none, which `devshot` sets
            # before it imports textual — but under pytest that lever is already
            # gone. `pytest-textual-snapshot` is a setuptools-entrypoint plugin
            # that imports `textual.app` at module scope, so `textual.constants`
            # (which reads the variable exactly once) is imported before the
            # first conftest.py line runs. `App.__init__` copying that constant
            # into `self.animation_level` is the only thing the variable feeds,
            # so setting the attribute here is the same switch, thrown late
            # enough to still work.
            app.animation_level = "none"
            yield app
    finally:
        shutil.rmtree(home, ignore_errors=True)


async def arrange_scene(scene: Scene, app: Any, pilot: Any) -> None:
    """Settle the first frame, run *scene*'s arrange step, and still the cursors.

    The caller supplies the running app, because the two callers start it
    differently (``open_scene`` with ``App.run_test``, a snapshot test with
    ``App.run`` under ``pytest-textual-snapshot``'s auto-pilot). Everything after
    that has to be identical or a screenshot and an assertion stop describing the
    same frame.

    Stilling the cursors is the "no live data" rule applied to the one clock a
    scene cannot help mounting: a focused ``Input``/``TextArea`` blinks on a
    ~0.5 s timer, so the same scene captured twice differs by one white cell in
    the input box, depending only on how long the arrange step happened to take.
    ``cursor_blink = False`` parks it *visible* rather than freezing it at
    whatever phase the run reached — a still frame, not a lucky one. It runs
    after ``arrange`` because an arrange step may push a modal that brings its
    own editor (``prompt-editor`` does).

    ``notify_style_update`` is not decoration. ``TextArea`` caches rendered
    strips, and the cursor enters that cache key only as
    ``selection.end if (self._cursor_visible and self.cursor_blink ...) else
    None`` (textual ``_text_area.py``, ``render_line``). Switching
    ``cursor_blink`` off makes that element ``None`` — the *same* key a
    blink-off frame was already cached under, so the stale cursor-less strip is
    returned for a cursor that ``_draw_cursor`` now says is on. It only bites
    when the arrange step outlives one blink interval, which is why it showed up
    on the slowest scene and nowhere else: ``tools-expanded`` straddled 0.5 s
    and captured two different frames roughly one run in six.
    ``notify_style_update`` is the public method that drops those strips.
    """
    from textual.widgets import Input, TextArea

    await pilot.pause()
    if scene.arrange is not None:
        await scene.arrange(app, pilot)
    for editor in list(app.query(Input)) + list(app.query(TextArea)):
        editor.cursor_blink = False
        editor.notify_style_update()
        editor.refresh()
    await pilot.pause()


@asynccontextmanager
async def open_scene(
    scene: Scene, size: tuple[int, int] = (120, 40)
) -> AsyncIterator[tuple[Any, Any]]:
    """Run *scene* at *size* and yield ``(app, pilot)`` with the frame settled.

    The one place a scene is turned into a running app, shared by the ``devshot``
    CLI and the appearance tests so a screenshot and an assertion are always
    looking at the same thing.
    """
    with stage_scene(scene) as app:
        async with app.run_test(size=size) as pilot:
            await arrange_scene(scene, app, pilot)
            yield app, pilot


def scene_names() -> list[str]:
    return [scene.name for scene in SCENES]


def get_scene(name: str) -> Scene:
    """Look up a scene by name, or raise with the list of valid names."""
    for scene in SCENES:
        if scene.name == name:
            return scene
    raise KeyError(f"unknown scene {name!r}; known scenes: {', '.join(scene_names())}")
