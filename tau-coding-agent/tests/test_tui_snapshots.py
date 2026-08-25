"""Visual regression: six scenes, one size, compared pixel-for-pixel against a
committed SVG of the composited screen.

``test_tui_appearance.py`` asserts *rules* — nothing overflows, a collapsed box
is one row, the word "parley" never reaches the screen. A rule only catches the
regression somebody already thought of. These tests catch the rest: any change
to a colour, a border glyph, a column of padding, or the order of two widgets
shows up as a diff against the reference, whether or not a rule covers it.

**The budget is the design.** Each reference SVG is roughly 50 KB of tracked
text, so this file deliberately holds six scenes at ONE terminal size rather
than the full nine-scene × two-size matrix ``test_tui_appearance.py`` sweeps.
Every scene here earns its slot by being the only view of some surface; the
three left out are each covered by one that is in (see the module-level notes on
``SNAPSHOT_SIZE`` and each test's docstring).

**Determinism is a requirement, not an aspiration.** A snapshot that differs
between two runs of the same code teaches developers to pass ``--snapshot-update``
without looking, which is worse than having no snapshot at all. Four things are
pinned, and only the first is obvious:

1. One fixed terminal size. Textual's layout is size-dependent everywhere, so a
   size matrix is a multiplier on both the byte budget and the failure surface.
2. Animation off. ``tau_coding_agent.testing.scenes.stage_scene`` sets
   ``app.animation_level = "none"`` per app rather than relying on
   ``TEXTUAL_ANIMATIONS``: under pytest the environment variable is already too
   late to matter, for the reason that function's comment gives.
3. Frozen scene data. Every string on these screens is written in ``scenes.py``
   — no clocks, no hostnames, no absolute paths, no live catalog.
4. A sandboxed ``~/.tau`` per test, so the developer's real config, real
   sessions, and real extensions cannot reach a frame.

To re-accept every reference after an intentional appearance change::

    pytest tau-coding-agent/tests/test_tui_snapshots.py --snapshot-update

and then *look at* ``snapshot_report.html`` before committing the result.

Reference: docs/textual-headless-testing.md
"""

from __future__ import annotations

from typing import Any, Callable

from textual.pilot import Pilot

from tau_coding_agent.testing.scenes import arrange_scene, get_scene, stage_scene

#: The one size every reference is captured at. 120x40 is ``devshot``'s default
#: and the wide half of ``test_tui_appearance.SIZES``: the sidebar, the chat
#: column, and an extension panel are all visible at once, which is the layout
#: with the most to regress. The narrow 80x24 case stays with the appearance
#: rules, which assert against it without storing a picture of it.
SNAPSHOT_SIZE = (120, 40)


def snapshot_scene(snap_compare: Callable[..., bool], name: str) -> bool:
    """Capture the named scene and compare it against its committed reference.

    ``snap_compare`` normally takes a *path* to a module that defines an app and
    imports it itself. That form cannot express a scene: a scene is an app plus a
    sandboxed ``~/.tau`` around it plus an arrange step that runs after the first
    frame settles. The fixture's other two inputs cover all three — it also
    accepts a constructed ``App``, and ``run_before`` runs inside its auto-pilot
    — so the scene is staged here and handed over already built.

    ``snap_compare`` drives the app with ``App.run()``, which owns its own event
    loop, so callers must be **synchronous** tests. The staging context stays
    open across the call because the sandbox has to outlive the app.
    """
    scene = get_scene(name)

    async def arrange(pilot: Pilot[Any]) -> None:
        # ``arrange_scene`` rather than a second copy of its three steps: it is
        # what ``open_scene`` runs, so an assertion in ``test_tui_appearance``
        # and a reference SVG here describe the same frame. It also stills the
        # blinking input cursor, which is the difference between a stable
        # snapshot and one that flips with the arrange step's wall time.
        await arrange_scene(scene, pilot.app, pilot)

    with stage_scene(scene) as app:
        return snap_compare(app, terminal_size=SNAPSHOT_SIZE, run_before=arrange)


# ---------------------------------------------------------------------------
# The captured scenes
# ---------------------------------------------------------------------------


def test_sidebar_snapshot(snap_compare) -> None:
    """The whole app chrome, with a populated session list.

    Covers the header, the "+ New Chat" button, the recency-grouped chat list,
    the empty chat pane, the input box, and the footer bindings in one frame.
    Chosen over ``empty``, which is this same screen with the list replaced by
    one "No sessions yet" line — a strict subset, and the only part of it this
    scene does not show.

    The chat column here is EMPTY, so this is also the reference for
    :class:`~tau_coding_agent.app.ChatPlaceholder`: the τ, the tagline, the five
    configuration rows, and the tree hint. The tagline is stable across runs
    because ``Parley.__init__`` declares ``fun: bool = False`` as a literal and
    every scene constructs a ``Parley`` without passing it — NOT because of
    ``tagline.FUN_DEFAULT``, which is ``True``. So this comparison holds in a
    packaged tree exactly as it does in a checkout.
    """
    assert snapshot_scene(snap_compare, "sidebar")


def test_tools_snapshot(snap_compare) -> None:
    """A finished exchange with every collapsible shut.

    The density contract in its resting state: a one-row collapsed exchange
    summary above a long markdown answer, fenced code included. This is the
    screen a user actually spends the day looking at.
    """
    assert snapshot_scene(snap_compare, "tools")


def test_tools_expanded_snapshot(snap_compare) -> None:
    """The same exchange with every collapsible open.

    Not redundant with ``tools``: expanding reveals a different set of widgets
    (the reasoning region, per-call argument JSON, tool results) and scrolls the
    answer off the bottom, so neither frame contains the other.
    """
    assert snapshot_scene(snap_compare, "tools-expanded")


def test_tree_modal_snapshot(snap_compare) -> None:
    """The ``/tree`` browser — the full-screen modal shape.

    Also the only scene that renders a ``Tree``, and the only one carrying the
    elision marker that ``test_tui_appearance`` asserts the existence of but
    cannot show the placement of. Since the detail pane landed it is also the
    rows-plus-pane split, with the selected node at full strength and its
    neighbour dimmed — a contrast no assertion states as well as a frame does.
    """
    assert snapshot_scene(snap_compare, "tree-modal")


def test_tree_modal_branch_snapshot(snap_compare) -> None:
    """The browser with the cursor on the fork.

    The leaf frame above cannot show two things this one does: a node with a
    neighbour on BOTH sides, and both ``⋯`` rows carrying counts. It is also the
    only frame where the pane has scrolled, which is where "previous message
    still visible, top of the selection visible" is a real constraint rather
    than something the layout satisfies by accident.
    """
    assert snapshot_scene(snap_compare, "tree-modal-branch")


def test_prompt_editor_snapshot(snap_compare) -> None:
    """The system-prompt editor — the *centred* modal shape.

    The second of the two modal geometries, and the one that has to size itself
    against its content rather than the screen. Chosen over ``tree-mode-modal``,
    which is the same centred dialog wrapping a column of buttons; this one adds
    a ``TextArea`` and a horizontal button row, so it exercises more of the
    dialog CSS.
    """
    assert snapshot_scene(snap_compare, "prompt-editor")


def test_ext_surfaces_snapshot(snap_compare) -> None:
    """An extension panel and two status-bar slots over a loaded chat.

    Three surfaces no other scene renders at all — the panel host, its body
    grid, and the status bar — plus the narrowed chat column they squeeze the
    conversation into, which is the only place the chat layout is seen under
    width pressure. It embeds the ``answer`` scene, which is why ``answer`` is
    not separately captured.
    """
    assert snapshot_scene(snap_compare, "ext-surfaces")
