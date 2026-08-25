"""Appearance rules, asserted against the composited screen rather than the DOM.

A widget query tells you a widget exists. It does not tell you the widget fits on
the screen, that its text is readable, or that the word on it is the word you
meant. These tests read the same character grid a user looks at
(``tau_coding_agent.testing.render.render_text``) and the same measured layout the
compositor produced, over the scene set in
:mod:`tau_coding_agent.testing.scenes`.

Reference: docs/textual-headless-testing.md
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tau_coding_agent
from rich.style import Style
from textual.app import App
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Collapsible, Static, Tree

from tau_coding_agent.app import (
    ChatDisplay,
    ChatListItem,
    ChatSidebar,
    ExtensionPanel,
    ExtensionPanelHost,
    MessageBox,
    Parley,
    SessionTreeModal,
)
from tau_coding_agent.chat_widgets import (
    ExchangeBox,
    MarkdownLineFormatter,
    ReasoningRegion,
    ToolBox,
)
from tau_coding_agent.testing.render import render_text
from tau_coding_agent.testing.scenes import SCENES, get_scene, open_scene
from tau_coding_agent.themes import install_themes

SIZES = [(120, 40), (80, 24)]

#: The scenes that put a ``ModalScreen`` on the stack. Written down rather than
#: discovered so the modal rules below cannot quietly become no-ops: a scene that
#: stops opening its dialog, or a new modal scene nobody added here, fails
#: :func:`_dialogs` instead of passing an empty loop.
MODAL_SCENES = ("tree-modal", "tree-modal-branch", "tree-mode-modal", "prompt-editor")

#: The chat column's floor, in columns of ChatDisplay content (its region less its
#: own ``padding: 1 2``), held whatever else is on screen.
#:
#: 40 there is 36 columns of wrapped prose once a MessageBox has spent its border
#: and its one column of padding per side. Below that a sentence breaks every five
#: or six words and the transcript reads as a column of fragments rather than as
#: text — and 36 is already the narrowest thing anyone should have to read a diff
#: explanation in. It is also within ten columns of the 50 the chat gets at 80x24
#: with nothing else open, which is the other half of the rule: a secondary surface
#: opening beside the chat is allowed to cost the reader something, not everything.
#:
#: Before the responsive widths this was FOUR at 80x24 with a panel open — a fixed
#: 30-column sidebar plus a fixed 40-column panel left the chat 8 columns, half of
#: them ChatDisplay padding.
CHAT_MIN_COLUMNS = 40


def _rows(app) -> list[str]:
    return render_text(app).splitlines()


def _dialogs(app, scene) -> list[Widget]:
    """The dialog container of every modal that *scene* has open.

    A ``ModalScreen`` is always the size of the terminal — it is a screen. The
    thing that can be the wrong size is the container it composes, so that is what
    the rules below measure.
    """
    modals = [screen for screen in app.screen_stack if isinstance(screen, ModalScreen)]
    assert bool(modals) == (scene.name in MODAL_SCENES), (
        f"scene {scene.name!r} {'has no' if not modals else 'has a'} modal screen, "
        f"which contradicts MODAL_SCENES — update it, or the modal rules assert nothing"
    )
    return [dialog for modal in modals for dialog in modal.children]


# ---------------------------------------------------------------------------
# Content rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scene", SCENES, ids=lambda s: s.name)
async def test_no_scene_says_parley(scene) -> None:
    """The fork's name is fine in the code; it is not the name of this program.

    Asserted over the rendered screen, not the source, because that is the only
    place the distinction is real — ``app.Parley`` may keep its class name.
    """
    async with open_scene(scene, (120, 40)) as (app, _pilot):
        assert "parley" not in render_text(app).lower()


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("scene", SCENES, ids=lambda s: s.name)
async def test_no_scene_overflows_the_screen(scene, size) -> None:
    """No rendered row is wider than the terminal, at either size.

    This is the scrollbar rule, and only that. A container that answers an
    oversized child by growing a horizontal scrollbar spends a column on the bar
    and the reader loses the text under it — which shows up here, because the row
    still reaches the screen edge. That is the failure this caught in the tree
    browser.

    It says nothing about a widget being too BIG. The compositor clips whatever
    falls outside the screen, so an off-screen dialog produces rows that are too
    SHORT, or no rows at all, and sails past every assertion here. The modal rules
    below measure regions against ``app.size`` for exactly that reason.
    """
    async with open_scene(scene, size) as (app, _pilot):
        for index, row in enumerate(_rows(app)):
            assert len(row) <= size[0], f"row {index} is {len(row)} cols on a {size[0]}-col screen"


# ---------------------------------------------------------------------------
# Modal dialogs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("scene", SCENES, ids=lambda s: s.name)
async def test_no_dialog_extends_past_the_screen(scene, size) -> None:
    """Every dialog fits inside the terminal it is drawn on.

    Measured, not rendered: a dialog laid out at 80x25 on an 80x24 screen is not
    an error and not a long row — the compositor simply drops the twenty-fifth
    line, taking the Save and Cancel buttons with it, and the screen above still
    looks plausible. ``#prompt-editor-dialog`` (80x25) and ``#tree-mode-dialog``
    (60x26) both shipped that way with the suite green.

    Containment, rather than width and height alone, because a centred dialog can
    also overflow by being placed badly rather than by being too large.
    """
    async with open_scene(scene, size) as (app, _pilot):
        width, height = app.size
        for dialog in _dialogs(app, scene):
            region = dialog.region
            assert (region.x, region.y) >= (0, 0), f"{dialog.id} starts off-screen at {region}"
            assert region.right <= width, (
                f"{dialog.id} reaches column {region.right} on a {width}-col screen"
            )
            assert region.bottom <= height, (
                f"{dialog.id} reaches row {region.bottom} on a {height}-row screen"
            )


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("scene", SCENES, ids=lambda s: s.name)
async def test_every_dialog_is_centred(scene, size) -> None:
    """A dialog sits in the middle of the terminal, not in its top-left corner.

    Textual gives a ``ModalScreen`` no alignment of its own, so every dialog here
    anchored at 0,0 — the ``align: center middle`` rules in the stylesheet were on
    the button rows *inside* the dialogs, which centres the buttons within a
    corner-pinned box. Asserted as equal margins on both axes, with a one-cell
    tolerance because an odd amount of leftover space cannot be split evenly.
    """
    async with open_scene(scene, size) as (app, _pilot):
        width, height = app.size
        for dialog in _dialogs(app, scene):
            region = dialog.region
            assert abs(region.x - (width - region.right)) <= 1, (
                f"{dialog.id} has {region.x} columns to its left and "
                f"{width - region.right} to its right"
            )
            assert abs(region.y - (height - region.bottom)) <= 1, (
                f"{dialog.id} has {region.y} rows above it and {height - region.bottom} below"
            )


# ---------------------------------------------------------------------------
# The tree browser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_tree_browser_fills_the_screen(size) -> None:
    """The dialog was a fixed 90x30: dead space on a wide terminal, clipped off
    the top and bottom of an 80x24 one."""
    async with open_scene(get_scene("tree-modal"), size) as (app, _pilot):
        dialog = app.screen.query_one("#tree-browser-dialog")
        assert (dialog.region.width, dialog.region.height) == size
        assert (dialog.region.x, dialog.region.y) == (0, 0)


async def test_tree_browser_elides_long_labels() -> None:
    """``Tree`` renders one unwrapped line per node, so a long preview has to be
    cut somewhere. Cut it visibly, with a marker the reader can see."""
    async with open_scene(get_scene("tree-modal"), (80, 24)) as (app, _pilot):
        tree = app.screen.query_one("#tree-browser-tree", Tree)
        labels = [str(node.label) for node in tree.root.children]
        assert labels, "the scene should have mounted at least one node"
        modal = app.screen
        assert isinstance(modal, SessionTreeModal)
        for _widget_node, full, _depth, _has_children in modal._rows:
            assert len(full) > 0
        # At least one preview in this scene is longer than an 80-column row, so
        # at least one label must carry the marker.
        rendered = render_text(app)
        assert "…" in rendered


async def test_tree_browser_has_no_horizontal_scrollbar() -> None:
    """Elision replaces sideways scrolling: reading a sentence by scrolling a
    tree row is not reading it."""
    async with open_scene(get_scene("tree-modal"), (80, 24)) as (app, _pilot):
        tree = app.screen.query_one("#tree-browser-tree", Tree)
        assert tree.virtual_size.width <= tree.content_size.width


# ---------------------------------------------------------------------------
# The tree browser's indentation counts forks, not messages (§2)
# ---------------------------------------------------------------------------


class _ModalHarness(App):
    """Host one modal, so a tree shape can be asserted without the whole Parley app.

    The scene set gives the browser one real conversation; these tests need a
    conversation of a chosen SHAPE (40 unbranched entries, or exactly one fork), so
    they build the log and push the modal themselves. Same harness as
    ``test_session_tree_browser``.

    It loads Parley's stylesheet. Component-class styling (§3's zone classes) has
    no ``DEFAULT_CSS`` behind it — every colour lives in ``parley.tcss`` — so a
    harness without it resolves every zone to an empty ``Style`` and a test that
    asserts a row IS painted would pass against a renderer that paints nothing.
    Resolved from the installed package rather than a path relative to this file,
    so it keeps working from an installed wheel.

    Loading the sheet means loading a theme (docs/PLAN-0.9.4.md §6): every colour
    in ``parley.tcss`` is a ``$tau-*`` variable a theme supplies, so a bare ``App``
    with the sheet and no palette does not render wrong — it fails to parse, with
    ``UnresolvedVariableError: $tau-bg``. ``install_themes`` is the same call
    ``Parley.__init__`` makes, and passing no name gets the default, which is what
    these tests have always been asserting against.
    """

    CSS_PATH = str(Path(tau_coding_agent.__file__).with_name("parley.tcss"))

    def __init__(self, modal) -> None:
        super().__init__()
        install_themes(self)
        self._modal = modal

    def on_mount(self) -> None:
        self.push_screen(self._modal)


def _linear_tree(length: int):
    """A ``ConversationTree`` over one unbranched chain of ``length`` messages."""
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    ids = [log.append_message({"role": "user", "content": f"m{i}"}) for i in range(length)]
    return ConversationTree(log.entries(), log.cursor), ids


def _widget_depth(tree: Tree, entry_id: str) -> int:
    """Widget nesting level of the row whose ``data`` is ``entry_id``.

    Counted from the hidden root, so a top-level row is 0. This is the number
    ``_relabel`` spends ``guide_depth`` cells on per level — the quantity §2 is
    about — and it is deliberately NOT the ``parentId`` depth.
    """
    node = next(n for n in _widget_rows(tree.root) if n.data == entry_id)
    depth = -1
    walk = node
    while walk.parent is not None:
        depth += 1
        walk = walk.parent
    return depth


def _widget_rows(root):
    for child in root.children:
        yield child
        yield from _widget_rows(child)


async def test_a_long_unbranched_chain_does_not_indent() -> None:
    """§2, the defect this replaced: one ``parentId`` level used to cost one widget
    level, so a 25-message conversation ran ``_relabel``'s available width to zero
    and the rows overflowed (TREE-BROWSER-AS-EDITOR.md §1.1).

    A chain with no forks in it now has nothing to indent *for*: every entry is a
    sibling of the one before, and the deepest row is as shallow as the first.
    """
    view, ids = _linear_tree(40)
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        assert [_widget_depth(tree, entry_id) for entry_id in ids] == [0] * len(ids)
        # …and the rows are all still there, in order — this flattens nesting, not
        # the walk.
        assert [n.data for n in _widget_rows(tree.root)] == ids
        # The depths `_relabel` sizes labels with are the WIDGET depths.
        assert {depth for _node, _label, depth, _kids in harness.screen._rows} == {0}


async def test_a_fork_is_what_creates_a_widget_level() -> None:
    """The other half of §2: a level of indentation now means "a branch happened
    here", which is the only thing worth spending the row's width on."""
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    root = log.append_message({"role": "user", "content": "root"})
    a = log.append_message({"role": "assistant", "content": "a"})
    # A second child of `root`, so `root` is the one fork in the log. `append_at`
    # rather than a navigate: it writes exactly one entry and does not move the leaf,
    # so the shape under test is only the fork.
    b = log.append_at(root, "message", {"message": {"role": "assistant", "content": "b"}})
    a2 = log.append_message({"role": "user", "content": "a2"})
    view = ConversationTree(log.entries(), log.cursor)

    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        assert _widget_depth(tree, root) == 0
        assert _widget_depth(tree, a) == 1
        assert _widget_depth(tree, b) == 1
        # `a2` continues `a` with no branch of its own, so it is a SIBLING of `a`,
        # not a level deeper: the second level was bought by the fork, and one fork
        # buys exactly one.
        assert _widget_depth(tree, a2) == 1
        # The fork is the widget parent of everything below it, so collapsing it
        # folds the branching subtree away — the unit §5.2 binds `left` to.
        fork = next(n for n in _widget_rows(tree.root) if n.data == root)
        assert {n.data for n in fork.children} == {a, a2, b}


async def test_a_long_chain_still_fills_the_row_at_80_columns() -> None:
    """What §1.1 was really about, measured on the composited screen: depth used to
    eat the label, so the deep rows overflowed and the tree grew a scrollbar."""
    view, ids = _linear_tree(40)
    modal = SessionTreeModal(view)
    harness = _ModalHarness(modal)
    async with harness.run_test(size=(80, 24)) as pilot:
        for _ in range(10):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        # ``scrollable_content_region``, not ``content_size``: the second does not
        # subtract the vertical scrollbar, and comparing against it is how this
        # test sat green over a tree that really was two cells too wide (§4 item
        # 1). ``_linear_tree``'s previews are eight characters, so nothing here
        # reaches the limit either way — the width bug has its own test below.
        assert tree.virtual_size.width <= tree.scrollable_content_region.size.width
        # No row was reduced to the too-narrow marker: at 80 columns and zero
        # indentation there is plenty of room for a preview.
        assert all(str(node.label) != "…" for node, _label, _depth, _kids in modal._rows)


def _wide_tree(turns: int):
    """A chain whose previews are far longer than any terminal is wide."""
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    for i in range(turns):
        log.append_message({"role": "user", "content": f"question {i} " + "x" * 200})
        log.append_message({"role": "assistant", "content": f"answer {i} " + "y" * 200})
    return ConversationTree(log.entries(), log.cursor)


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_the_rows_leave_room_for_the_vertical_scrollbar(size) -> None:
    """§4 item 1: the labels were sized against a width the scrollbar was using.

    ``Widget.content_size`` is ``region.shrink(styles.gutter)`` — border and
    padding. It does NOT subtract the scrollbar; ``scrollable_content_region``
    does. So every label on a tree tall enough to scroll came out two cells too
    long, the rows overflowed, and the tree grew a horizontal scrollbar showing
    two cells of nothing — which then cost a row of height as well.

    Asserted as the reported symptom (a horizontal scrollbar that should not be
    there) rather than as the arithmetic, because the arithmetic is not what the
    reader sees and a later change to it would still have to keep this true.
    """
    modal = SessionTreeModal(_wide_tree(40))
    harness = _ModalHarness(modal)
    async with harness.run_test(size=size) as pilot:
        for _ in range(10):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        assert tree.show_vertical_scrollbar, "the fixture is tall enough to scroll"
        assert tree.show_horizontal_scrollbar is False
        assert tree.max_scroll_x == 0
        assert tree.virtual_size.width <= tree.scrollable_content_region.size.width


# ---------------------------------------------------------------------------
# _elide's floor
# ---------------------------------------------------------------------------


def test_elide_marks_a_column_too_narrow_to_shorten_into() -> None:
    """§2: below the minimum width ``_elide`` returns a marker, not the input.

    Returning the input was the Fail-Early inversion — the one function whose job
    is to stop a row overflowing answered an impossible width by producing the
    overflow. One cell of ``…`` is a visible bug; a 60-cell row in a 1-cell column
    is a horizontal scrollbar across the whole browser.
    """
    from tau_coding_agent.app import _ELIDE_MIN_WIDTH, _ELIDE_TOO_NARROW, _elide

    for width in (-3, 0, _ELIDE_MIN_WIDTH - 1):
        assert _elide("a very long preview line", width) == _ELIDE_TOO_NARROW
    assert len(_ELIDE_TOO_NARROW) == 1


def test_elide_still_cuts_visibly_at_and_above_the_floor() -> None:
    """The floor is a floor, not a new behaviour: at the minimum width the marker
    plus one character is exactly what fits, and above it nothing changed."""
    from tau_coding_agent.app import _ELIDE_MIN_WIDTH, _elide

    assert _elide("abcdef", _ELIDE_MIN_WIDTH) == "a…"
    assert _elide("abcdef", 4) == "abc…"
    # Short enough to fit is returned whole, marker and all absent.
    assert _elide("ab", 6) == "ab"
    assert _elide("abcdef", 6) == "abcdef"


# ---------------------------------------------------------------------------
# The extension panel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_panel_body_table_fits_the_panel(size) -> None:
    """The panel's table is laid out to the width the panel actually has.

    ``render_panel_body`` used to pad every column to its widest cell with no
    knowledge of the panel. The row came out wider than the body ``Static``, which
    soft-wrapped it mid-row and scattered the table into unreadable fragments —
    visible here as more body rows than the table has, and as a column name that
    is not on the header line.

    A header may be *elided* rather than shown whole: since the panel became a
    share of the terminal (``#ext-panel-host: 30%``, floor 26) an 80-column screen
    gives four columns 26 to share, and every name is cut. That is the table doing
    its job. What this test still forbids is a column leaving the header line
    altogether, by wrapping onto the next row or by being dropped.
    """
    async with open_scene(get_scene("ext-surfaces"), size) as (app, _pilot):
        panel = app.query_one(ExtensionPanel)
        table = panel._spec["body"]
        region = panel.query_one(".ext-panel-body").content_region
        assert region.width > 0, "the panel body should have been laid out"
        screen = _rows(app)
        lines = [
            screen[y][region.x : region.x + region.width]
            for y in range(region.y, region.y + region.height)
        ]
        # A header, a rule, one line per row. A grid too wide for the panel wraps,
        # and needs nearly twice that.
        assert len(lines) == len(table["rows"]) + 2
        for index, line in enumerate(lines):
            assert len(line.rstrip()) <= region.width, f"body line {index} overflows"
        # Every column reaches the header line — whole, or as a prefix carrying the
        # ellipsis. A count short of the column list means one wrapped off the line
        # or was dropped, which is the failure this catches; a name the reader can
        # see is truncated is still a name the reader can see.
        shown = lines[0].split()
        assert len(shown) == len(table["columns"]), (
            f"header line {lines[0]!r} carries {len(shown)} of {len(table['columns'])} columns"
        )
        for drawn, column in zip(shown, table["columns"], strict=True):
            assert column.startswith(drawn.rstrip("…")), (
                f"header {drawn!r} is neither {column!r} nor a prefix of it"
            )


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_every_panel_action_button_fits_the_panel(size) -> None:
    """Every action a panel declares is on screen, whole, and inside the panel.

    Textual's ``Button`` carries ``width: auto; min-width: 16``, so two actions
    wanted 33 columns however wide the panel was. That was invisible while
    ``#ext-panel-host`` was a fixed 40 and became a defect the moment it became a
    share of the terminal: the ``Horizontal`` does not wrap, it clips, so the
    second button was cut in half at 120x40 and pushed off the screen entirely at
    80x24 — silently, because a widget drawn past its container still reports the
    size it asked for. That is why this reads the composited screen and the
    measured regions rather than querying for the buttons, which found them
    present the whole time.
    """
    async with open_scene(get_scene("ext-surfaces"), size) as (app, _pilot):
        panel = app.query_one(ExtensionPanel)
        host = app.query_one(ExtensionPanelHost).region
        buttons = panel.query(".ext-panel-action")
        labels = [action["label"] for action in panel._spec["actions"]]
        assert len(buttons) == len(labels), "the panel dropped an action before layout"

        row = _rows(app)[buttons[0].region.y]
        for button, label in zip(buttons, labels, strict=True):
            region = button.region
            assert region.width > 0 and region.height == 1, (
                f"{label!r} is {region.width}x{region.height}; a wrapped label is two rows"
            )
            assert host.x <= region.x and region.right <= host.right, (
                f"{label!r} at {region} is outside the panel {host}"
            )
            assert label in row, f"{label!r} is not on the action row: {row!r}"


# A cell too wide for its column is elided rather than dropped; that the marker is
# actually drawn is asserted at the unit level, in test_extension_panel.py, where
# the width is fixed by the test instead of by whatever the panel's CSS gives it.


# ---------------------------------------------------------------------------
# Density
# ---------------------------------------------------------------------------


async def test_a_collapsed_collapsible_is_one_row() -> None:
    """One collapsed tool call, reasoning region, or exchange = one line.

    Textual's ``Collapsible`` default spends three rows on a collapsed one (a
    ``border-top: hkey`` rule, the title, a ``padding-bottom: 1``); nested three
    deep that is most of a screen of chrome around a few title lines.
    """
    async with open_scene(get_scene("tools"), (120, 40)) as (app, _pilot):
        collapsibles = [
            *app.query(ExchangeBox),
            *app.query(ToolBox),
        ]
        assert collapsibles, "the tools scene should have mounted collapsibles"
        for box in collapsibles:
            if not box.collapsed or box.region.height == 0:
                continue
            assert box.region.height == 1, f"{box} is {box.region.height} rows collapsed"


async def test_message_box_spends_no_row_on_vertical_padding() -> None:
    """The border already separates a message from its neighbours."""
    async with open_scene(get_scene("answer"), (120, 40)) as (app, _pilot):
        boxes = list(app.query(MessageBox))
        assert boxes
        for box in boxes:
            padding = box.styles.padding
            assert (padding.top, padding.bottom) == (0, 0)


async def test_chat_text_gets_most_of_the_column() -> None:
    """Chrome per side was six columns: display padding 2, box border 1, box
    padding 1, Markdown padding 2. Anything near that again is a regression."""
    async with open_scene(get_scene("answer"), (120, 40)) as (app, _pilot):
        display = app.query_one(ChatDisplay)
        body = app.query_one(".message-content")
        lost = display.region.width - body.content_size.width
        assert lost <= 8, f"{lost} columns of chrome between the display and the text"


async def test_an_expanded_collapsible_indents_its_contents_by_one_column() -> None:
    """Nesting has to be visible when the enclosing border is not.

    A reasoning region or tool box inside an answer sat at the same left offset a
    sibling of that answer would, so the ``▼`` markers of two different depths
    lined up. One column of LEFT padding breaks the alignment; the other three
    sides stay at zero, because a row of padding is what the density pass removed.
    """
    async with open_scene(get_scene("tools-expanded"), (120, 40)) as (app, _pilot):
        contents = [
            box.query_one(Collapsible.Contents)
            for box in (*app.query(ExchangeBox), *app.query(ToolBox), *app.query(ReasoningRegion))
        ]
        assert contents, "the expanded scene should have mounted collapsibles"
        for region in contents:
            padding = region.styles.padding
            assert (padding.top, padding.right, padding.bottom, padding.left) == (0, 0, 0, 1)


async def test_an_expanded_collapsibles_body_starts_one_column_in() -> None:
    """The same rule measured on the composited screen rather than read off the
    stylesheet: a collapsible's content really does begin one column right of the
    collapsible's own left edge, so the indent survives layout."""
    async with open_scene(get_scene("tools-expanded"), (120, 40)) as (app, _pilot):
        boxes = [
            box
            for box in (*app.query(ExchangeBox), *app.query(ToolBox), *app.query(ReasoningRegion))
            if not box.collapsed and box.region.width
        ]
        assert boxes, "the expanded scene should have mounted open collapsibles"
        for box in boxes:
            contents = box.query_one(Collapsible.Contents)
            assert contents.content_region.x == box.region.x + 1, box


# ---------------------------------------------------------------------------
# Markdown block spacing
# ---------------------------------------------------------------------------

#: Every Textual markdown block widget that ships a vertical margin or padding of
#: its own (read off ``textual/widgets/_markdown.py``): the headers
#: (``margin: 2 0 1 0``, or ``1 0`` from H3 down), ``MarkdownParagraph``
#: (``0 0 1 0``), ``MarkdownFence`` (``1 0``, plus ``padding: 1 2`` on its inner
#: Label), ``MarkdownBlockQuote`` (``1 0``), the two list widgets (``0 0 1 0``),
#: ``MarkdownTable`` (``margin-bottom: 1``) and ``MarkdownHorizontalRule``
#: (``padding-top: 1; margin-bottom: 1``).
SPACED_MARKDOWN_BLOCKS = (
    "MarkdownH1",
    "MarkdownH2",
    "MarkdownH3",
    "MarkdownH4",
    "MarkdownH5",
    "MarkdownH6",
    "MarkdownParagraph",
    "MarkdownFence",
    "MarkdownBlockQuote",
    "MarkdownBulletList",
    "MarkdownOrderedList",
    "MarkdownListItem",
    "MarkdownTable",
    "MarkdownHorizontalRule",
)

#: One assistant answer exercising every block type listed above, so the rule can
#: be asserted over widgets that are actually mounted rather than over the
#: stylesheet text.
EVERY_BLOCK = """\
# H1
## H2
### H3
#### H4
##### H5
###### H6

A paragraph.

Another paragraph.

```python
a = 1
```

> A block quote.

- bullet one
- bullet two

1. ordered one
2. ordered two

| a | b |
| - | - |
| 1 | 2 |

---

The last paragraph.
"""


async def _mount_answer(app, pilot, text: str):
    """Reload the display with one user turn and one assistant answer, and return
    the answer's rendered body (the ``Markdown`` holding its blocks)."""
    display = app.query_one(ChatDisplay)
    await display.reload_messages(
        [
            {"role": "user", "content": "?"},
            {"role": "assistant", "content": [{"type": "text", "text": text}]},
        ]
    )
    for _ in range(4):
        await pilot.pause()
    answers = [b for b in app.query(MessageBox) if b.role == "assistant"]
    assert answers, "the answer should have mounted"
    return answers[-1].query_one(".message-content")


async def test_markdown_blocks_spend_no_row_on_vertical_spacing() -> None:
    """Textual's markdown blocks each carry their own vertical margin — two rows
    above a heading, one below a paragraph, one either side of a code fence. Once
    the message box stopped spending rows on padding, those were the only blank
    rows left inside a message, and on a 24-row terminal a four-block answer spent
    a quarter of the screen on them. Horizontal values are deliberately untouched.
    """
    async with open_scene(get_scene("empty"), (120, 40)) as (app, pilot):
        body = await _mount_answer(app, pilot, EVERY_BLOCK)
        seen: set[str] = set()
        for block in body.walk_children():
            name = type(block).__name__
            if name not in SPACED_MARKDOWN_BLOCKS:
                continue
            seen.add(name)
            margin, padding = block.styles.margin, block.styles.padding
            assert (margin.top, margin.bottom) == (0, 0), f"{name} margin {margin}"
            assert (padding.top, padding.bottom) == (0, 0), f"{name} padding {padding}"
        assert len(seen) >= 8, f"only exercised {sorted(seen)}"


async def test_a_code_fence_spends_no_row_on_its_label_padding() -> None:
    """``MarkdownFence``'s own margin is not the whole story: the Label inside it
    carries ``padding: 1 2``, a blank row above and below the code itself. The two
    columns stay — they are what makes the block read as code."""
    async with open_scene(get_scene("empty"), (120, 40)) as (app, pilot):
        body = await _mount_answer(app, pilot, "text\n\n```python\na = 1\n```\n")
        labels = list(body.query("MarkdownFence > Label"))
        assert labels, "the answer should have mounted a fence"
        for label in labels:
            padding = label.styles.padding
            assert (padding.top, padding.bottom) == (0, 0)
            assert (padding.left, padding.right) == (2, 2)


async def test_consecutive_paragraphs_are_no_longer_separated() -> None:
    """The accepted CONSEQUENCE of zeroing every vertical margin, pinned so it
    cannot change silently.

    With markdown's own paragraph spacing gone, two prose paragraphs abut: the
    only remaining cue is the first one's last line ending short of the right
    margin, which is exactly what an ordinary wrapped line looks like. Recorded
    here as the measured fact it is — if a later change reintroduces a blank row
    between paragraphs, that is a decision to make deliberately, and this test is
    where it gets made.
    """
    async with open_scene(get_scene("empty"), (120, 40)) as (app, pilot):
        body = await _mount_answer(app, pilot, "First paragraph.\n\nSecond.\n\nThird.\n")
        paragraphs = list(body.children)
        assert [type(p).__name__ for p in paragraphs] == ["MarkdownParagraph"] * 3
        tops = [p.region.y for p in paragraphs]
        heights = [p.region.height for p in paragraphs]
        assert tops[1] == tops[0] + heights[0]
        assert tops[2] == tops[1] + heights[1]


# ---------------------------------------------------------------------------
# Responsive side columns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("scene_name", ["answer", "ext-surfaces"])
async def test_the_chat_column_holds_its_floor(scene_name, size) -> None:
    """The chat keeps :data:`CHAT_MIN_COLUMNS` whether or not a panel is open.

    Both side columns used to be fixed counts — a 30-column sidebar and a
    40-column panel — which is a fixed 70-column tax on a terminal of any size.
    At 80x24 with a panel open that left the chat, the primary content, four
    usable columns.
    """
    async with open_scene(get_scene(scene_name), size) as (app, _pilot):
        display = app.query_one(ChatDisplay)
        assert display.content_size.width >= CHAT_MIN_COLUMNS, (
            f"{scene_name} at {size[0]}x{size[1]} leaves the chat "
            f"{display.content_size.width} columns"
        )


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_the_side_columns_are_a_share_of_the_width(size) -> None:
    """Neither side column is a fixed count, and both stay within their bounds.

    The bounds are the point of the percentages: a share alone would make the
    sidebar 50 columns on a 200-column terminal (nothing there is 50 columns wide)
    and 20 on an 80-column one (narrower than the words in a session name).

    ctrl+b first, because §8 mounts the sidebar closed and a hidden widget has a
    zero-width region — which would satisfy an upper bound by not being there.
    """
    async with open_scene(get_scene("ext-surfaces"), size) as (app, pilot):
        host = app.query_one(ExtensionPanelHost)
        assert host.display, "the ext-surfaces scene should have a panel open"
        assert 26 <= host.region.width <= 44
        await pilot.press("ctrl+b")
        sidebar = app.query_one(ChatSidebar)
        assert sidebar.display
        assert 24 <= sidebar.region.width <= 32
        # The two old fixed widths, in one assertion: they cannot both be right at
        # two terminal sizes.
        assert (sidebar.region.width, host.region.width) != (30, 40)


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize(
    "scene_name", [s.name for s in SCENES if s.name != "sidebar"], ids=lambda n: n
)
async def test_no_scene_starts_with_the_sidebar_open(scene_name, size) -> None:
    """The startup state, asserted everywhere except the scene that opens it.

    docs/SESSION-UX-REDESIGN.md §8 / decision 4: the sidebar mounts CLOSED. The
    picker and the command palette are the canonical way to reach a saved
    session, so the list is something you ask for (ctrl+b) rather than a quarter
    of the first screen.

    Swept over every scene rather than asserted once on ``empty``, because the
    default is written in two places that have to agree — ``#sidebar``'s
    ``display: none`` in parley.tcss and ``Parley._sidebar_open`` — and a scene
    that pushes a modal or opens a panel is exactly where a stray write to
    ``sidebar.display`` would come from.
    """
    async with open_scene(get_scene(scene_name), size) as (app, _pilot):
        assert not app.query_one(ChatSidebar).display


async def test_a_panel_does_not_touch_the_sidebar_the_user_opened() -> None:
    """An extension panel opening or closing is not a vote on the sidebar.

    It used to be: below :data:`Parley.SIDE_COLUMNS_MIN_WIDTH` the sidebar was
    hidden automatically to keep the chat readable beside a panel. That rule only
    ever decided the case where the user had expressed no preference — an
    explicit ctrl+b won over it by design — and §8 makes "no preference" mean
    closed. So a visible sidebar is now always one that was asked for, and the
    panel does not get to revoke it (nor to bring it back when it closes).
    """
    async with open_scene(get_scene("ext-surfaces"), (80, 24)) as (app, pilot):
        sidebar = app.query_one(ChatSidebar)
        host = app.query_one(ExtensionPanelHost)
        assert host.display, "the ext-surfaces scene should have a panel open"
        assert not sidebar.display

        await pilot.press("ctrl+b")
        assert sidebar.display, "ctrl+b beside a panel on a narrow terminal is honored"

        host.set_panel("fleet", None)
        await pilot.pause()
        await pilot.pause()
        assert not host.display
        assert sidebar.display, "closing the panel left the user's choice alone"


async def test_the_sidebar_stays_open_on_a_narrow_terminal() -> None:
    """Nothing shrinks the sidebar back out of existence once it is open."""
    async with open_scene(get_scene("sidebar"), (80, 24)) as (app, _pilot):
        assert app.query_one(ChatSidebar).display


async def test_ctrl_b_is_the_only_thing_that_opens_the_sidebar() -> None:
    """An explicit toggle wins, in both directions, at any width.

    A user who asks for the sidebar next to a panel on an 80-column terminal gets
    it (and pays for it in chat columns — see
    :func:`test_side_columns_min_width_is_where_the_floor_is`), and one who
    closes it keeps it closed. Both directions write ``#sidebar``'s display
    through ``_apply_side_columns`` for exactly this reason — an inline style set
    by one of them would otherwise be invisible to the other.
    """
    async with open_scene(get_scene("ext-surfaces"), (80, 24)) as (app, pilot):
        sidebar = app.query_one(ChatSidebar)
        assert not sidebar.display  # closed by default (§8)
        await pilot.press("ctrl+b")
        assert sidebar.display
        await pilot.press("ctrl+b")
        assert not sidebar.display


async def test_side_columns_min_width_is_where_the_floor_is() -> None:
    """The breakpoint is measured, not chosen: it is the narrowest width at which
    both side columns still leave the chat :data:`CHAT_MIN_COLUMNS`.

    Asserted from both sides, so the constant cannot drift away from the CSS it
    was derived from. If a percentage in parley.tcss changes, one of these two
    halves fails and names the direction.

    The app no longer *acts* on the number (§8 — the sidebar starts closed and
    ctrl+b is honored at any width), so both halves reach the two-column layout
    the way a user does: by opening the sidebar next to the panel. What the
    constant now marks is the width below which that request costs the chat its
    floor, which is a price worth keeping measured even though nobody is stopped
    from paying it.
    """
    fits = Parley.SIDE_COLUMNS_MIN_WIDTH
    async with open_scene(get_scene("ext-surfaces"), (fits, 30)) as (app, pilot):
        await pilot.press("ctrl+b")
        assert app.query_one(ChatSidebar).display
        assert app.query_one(ChatDisplay).content_size.width >= CHAT_MIN_COLUMNS

    # One column narrower, the same request drops the chat under the floor.
    async with open_scene(get_scene("ext-surfaces"), (fits - 1, 30)) as (app, pilot):
        await pilot.press("ctrl+b")
        assert app.query_one(ChatSidebar).display
        assert app.query_one(ChatDisplay).content_size.width < CHAT_MIN_COLUMNS


# ---------------------------------------------------------------------------
# The session list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_a_session_entry_is_one_row_plus_its_rule(size) -> None:
    """Two rows per entry: the name, and the line under it.

    A session name is a sentence in a ~20-column column, so the default wrap spent
    two or three rows on one entry — and with no boundary between entries the list
    stopped reading as a list at all.
    """
    async with open_scene(get_scene("sidebar"), size) as (app, _pilot):
        items = list(app.query(ChatListItem))
        assert items, "the sidebar scene should have mounted session entries"
        for item in items:
            assert item.region.height == 2, f"{item} is {item.region.height} rows"


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_a_cut_session_name_says_so(size) -> None:
    """Elision, not a silent clip: the reader can see there is more name.

    Same contract as the tree browser's ``_elide`` — every scene name in the
    fixture set is longer than the column at either size, so the marker has to be
    on screen.
    """
    async with open_scene(get_scene("sidebar"), size) as (app, _pilot):
        assert "…" in render_text(app)


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_session_entries_have_a_visible_boundary(size) -> None:
    """The rule under each entry is where one session ends and the next begins."""
    async with open_scene(get_scene("sidebar"), size) as (app, _pilot):
        items = list(app.query(ChatListItem))
        assert items
        for item in items:
            bottom = item.styles.border[2]
            assert bottom and bottom[0], f"{item} has no bottom rule"


# ---------------------------------------------------------------------------
# MarkdownLineFormatter
# ---------------------------------------------------------------------------


FENCED = "intro\n```python\na = 1\nb = 2\n```\noutro\n"


def test_formatter_doubles_verbatim_newlines() -> None:
    assert MarkdownLineFormatter("verbatim").feed("one\ntwo\n") == "one\n\ntwo\n\n"


def test_formatter_leaves_markdown_newlines_alone() -> None:
    """A model that hard-wraps its prose at 80 columns used to get a blank line
    between every wrapped line, so one sentence rendered as several paragraphs."""
    wrapped = "The accumulator assigned the incoming fragment over the stored\none.\n"
    assert MarkdownLineFormatter("markdown").feed(wrapped) == wrapped


def test_formatter_passes_a_whole_markdown_document_through_unchanged() -> None:
    """Markdown's own blank lines already say where the paragraphs are."""
    doc = "## Heading\n\nA paragraph that\nwraps.\n\n```py\nx = 1\n```\n\nDone.\n"
    assert MarkdownLineFormatter("markdown").feed(doc) == doc


def test_formatter_leaves_fenced_code_alone() -> None:
    """The blank-line-between-every-code-line bug, in one assertion."""
    out = MarkdownLineFormatter("verbatim").feed(FENCED)
    assert "a = 1\nb = 2" in out
    assert "a = 1\n\nb = 2" not in out


def test_formatter_does_not_pad_the_fence_delimiters() -> None:
    """A doubled newline after the opener would make the first code line blank."""
    out = MarkdownLineFormatter("verbatim").feed(FENCED)
    assert "```python\na = 1" in out
    assert "b = 2\n```" in out


def test_formatter_reopens_prose_after_the_fence() -> None:
    assert MarkdownLineFormatter("verbatim").feed(FENCED).endswith("outro\n\n")


def test_formatter_tracks_fences_for_either_source() -> None:
    """``in_fence`` is the same reading whichever source the body came from —
    a markdown formatter rewrites nothing but still knows where it is."""
    for source in ("verbatim", "markdown"):
        formatter = MarkdownLineFormatter(source)
        formatter.feed("intro\n```python\n")
        assert formatter.in_fence is True, source
        formatter.feed("a = 1\n```\n")
        assert formatter.in_fence is False, source


@pytest.mark.parametrize("source", ["verbatim", "markdown"])
@pytest.mark.parametrize("split", range(1, len(FENCED)))
def test_formatter_is_split_independent(split: int, source: str) -> None:
    """The property ``MessageBox.append_content_delta`` depends on: streaming a
    body in two pieces produces the same document as formatting it whole, for
    EVERY split point — including mid-newline and mid-fence-marker."""
    streamed = MarkdownLineFormatter(source)
    piecewise = streamed.feed(FENCED[:split]) + streamed.feed(FENCED[split:])
    assert piecewise == MarkdownLineFormatter(source).feed(FENCED)


# ---------------------------------------------------------------------------
# Which source each caller declares
# ---------------------------------------------------------------------------


async def test_an_assistant_answer_renders_as_markdown() -> None:
    """The defect this fixes, at scene level: the ``tools`` answer is markdown
    hard-wrapped at 80 columns, and the doubling split its first sentence into
    two paragraphs — '…assigned the incoming fragment over the stored' / 'one. It
    now appends…'. One sentence is one paragraph."""
    async with open_scene(get_scene("tools"), (120, 40)) as (app, _pilot):
        answers = [b for b in app.query(MessageBox) if b.role == "assistant" and b.content_text]
        assert answers, "the tools scene should have mounted an assistant answer"
        body = answers[-1].query_one(".message-content")
        blocks = [type(child).__name__ for child in body.children]
        # `_LONG_ANSWER` is: H2, paragraph, fence, paragraph, ordered list,
        # paragraph. A FOURTH paragraph is the first one, split in half at the
        # hard wrap: "…over the stored" / "one. It now appends…".
        assert blocks == [
            "MarkdownH2",
            "MarkdownParagraph",
            "MarkdownFence",
            "MarkdownParagraph",
            "MarkdownOrderedList",
            "MarkdownParagraph",
        ], blocks


async def test_a_tool_result_keeps_its_line_breaks() -> None:
    """The rule the doubling exists for, and the reason it is split by source and
    never by a look at the text: a tool result's newlines ARE its structure."""
    async with open_scene(get_scene("empty"), (120, 40)) as (app, pilot):
        display = app.query_one(ChatDisplay)
        display.add_persisted_message(
            {
                "role": "toolResult",
                "tool_call_id": "call_1",
                "tool_name": "grep",
                "content": [{"type": "text", "text": "alpha.py:1: one\nbeta.py:2: two"}],
            }
        )
        for _ in range(4):
            await pilot.pause()
        rows = _rows(app)
        assert any("alpha.py:1: one" in row for row in rows)
        assert any("beta.py:2: two" in row for row in rows)


# ---------------------------------------------------------------------------
# Zones: per-row styling from the four selection sets (§3, §5.3)
# ---------------------------------------------------------------------------


def _forked_tree():
    """``m0 → m1 → m2`` with a second child ``b1`` hanging off ``m0``.

    The smallest shape that separates the cursor's ancestor chain from a row that
    is not on it: the cursor opens on ``m2``, so ``m0``/``m1`` are path rows and
    ``b1`` is not. Returns the view plus the four ids.
    """
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    m0 = log.append_message({"role": "user", "content": "m0"})
    m1 = log.append_message({"role": "assistant", "content": "m1"})
    m2 = log.append_message({"role": "user", "content": "m2"})
    # `append_at` writes one entry and does NOT move the leaf, so the cursor stays
    # on m2 and b1 is genuinely off the cursor's path.
    b1 = log.append_at(m0, "message", {"message": {"role": "assistant", "content": "b1"}})
    return ConversationTree(log.entries(), log.cursor), m0, m1, m2, b1


def _rendered_row(tree, entry_id):
    """``render_label``'s output for ``entry_id``, with null base styles.

    Calls the hook §3 chose, so what comes back is the zone styling alone rather
    than the zone styling composited with whatever the cursor and hover happened
    to be doing on that frame.
    """
    from rich.style import Style

    node = next(n for n in _widget_rows(tree.root) if n.data == entry_id)
    return tree.render_label(node, Style(), Style())


def _row_span_styles(tree, entry_id):
    """The ZONE styles on the row for ``entry_id`` — the spans that run to its end.

    A zone covers the whole label (``render_label`` stylizes ``split`` →
    ``len(text.plain)``), and the hover trace covers the whole row. The one span
    that stops short is the type tag, which is not a zone, and it is filtered out
    here because it can resolve to the same ``Style`` as a zone — ``tree--kind-user``
    and ``tree--zone-summary`` both borrow ``$tau-role-user``, deliberately, and a
    style-only comparison cannot tell those two apart. See ``_row_tag_styles``.
    """
    text = _rendered_row(tree, entry_id)
    return [span.style for span in text.spans if span.end == len(text.plain)]


def _row_tag_styles(tree, entry_id):
    """The styles on the row's type tag — the spans that stop before its end."""
    text = _rendered_row(tree, entry_id)
    return [span.style for span in text.spans if span.end < len(text.plain)]


async def test_a_row_on_the_cursors_path_is_painted_and_one_off_it_is_not() -> None:
    """§3's ``tree--zone-path``, which §2 made necessary.

    Textual highlights the hovered row's ancestry through its indentation guides,
    and fork-nesting left a 30-message run with no rails between its siblings —
    §2 records that cost and calls §3 "the replacement, not an embellishment". So
    the ancestor chain is drawn per row, from the set, and this is the assertion
    that it actually reaches the rows.

    Both halves, because either alone passes on a bug: a renderer that paints
    every row passes the first, and one that paints none passes the second.
    """
    from tau_coding_agent.app import ZoneTree

    view, m0, m1, m2, b1 = _forked_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        assert tree.zones.cursor == m2, "the browser should open on the current leaf"
        assert {m0, m1, m2} == tree.zones.path
        assert b1 not in tree.zones.path

        path_style = tree.get_component_rich_style("tree--zone-path", partial=True)
        assert path_style != Style(), "the stylesheet defines no tree--zone-path"
        assert path_style in _row_span_styles(tree, m0)
        assert path_style in _row_span_styles(tree, m1)
        assert path_style not in _row_span_styles(tree, b1)
        # The cursor row is deliberately left alone: its own style is resolved
        # with `partial=False` and would lose its foreground to a zone colour.
        assert path_style not in _row_span_styles(tree, m2)


async def test_space_marks_a_row_and_two_marks_report_their_common_ancestor() -> None:
    """§5.3's set 2, and the lowest common ancestor it exists to give.

    ``space`` is this implementation's key, not the document's. The ancestor is
    the part §5.3 promises: ``_parent_of`` "gives the lowest common ancestor for
    free", and ``m0`` is what ``m2`` and ``b1`` — one on each side of the only
    fork — have in common.
    """
    from tau_coding_agent.app import ZoneTree

    view, m0, _m1, m2, b1 = _forked_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        for target in (m2, b1):
            tree.move_cursor(next(n for n in _widget_rows(tree.root) if n.data == target))
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()

        assert tree.zones.marked == frozenset({m2, b1})
        summary = str(harness.screen.query_one("#tree-browser-marks", Static).content)
        assert "2 nodes marked" in summary
        assert f"common ancestor {m0}" in summary

        # A marked row that is NOT the cursor is painted with the marked zone.
        marked_style = tree.get_component_rich_style("tree--zone-marked", partial=True)
        assert marked_style in _row_span_styles(tree, m2)


async def test_a_selection_total_says_it_is_an_estimate() -> None:
    """§5.3's last paragraph, which is a Fail-Early rule about numbers.

    ``compaction.estimate_tokens`` is a ~4-chars-per-token heuristic. The only
    measured figure in a session is ``usage.input_tokens`` on an assistant message
    (``agent_loop.py:819``), and it measures one request rather than an arbitrary
    selection. So a selection total may only state an estimate and must say which
    — a bare number here would be read as measured, which is the swallowed gap the
    repo's Fail-Early rule exists to stop.
    """
    import re

    from tau_coding_agent.app import ZoneTree

    view, _m0, _m1, m2, _b1 = _forked_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        tree.move_cursor(next(n for n in _widget_rows(tree.root) if n.data == m2))
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()

        summary = str(harness.screen.query_one("#tree-browser-marks", Static).content)
        assert re.search(r"~\d+ tokens \(estimate\)", summary), summary
        # …and there is no OTHER token figure on the line that a reader could take
        # for a measurement.
        assert summary.count("tokens") == 1


async def test_nothing_marked_says_so_and_names_the_key() -> None:
    """The readout's resting state. It is the only feedback a mark on the row
    under the cursor produces (that row keeps its cursor styling), so it has to
    be legible before there is anything to report."""
    view, _m0, _m1, _m2, _b1 = _forked_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        summary = str(harness.screen.query_one("#tree-browser-marks", Static).content)
        assert "nothing marked" in summary
        assert "space" in summary


# ---------------------------------------------------------------------------
# The branch summary and the branch it looks back on (§4.3, step 4c)
# ---------------------------------------------------------------------------


def _abandoned_branch_tree():
    """``m0`` forked into an abandoned branch and a summary of it.

    ``m0 → b1 → b2`` is the branch that was walked and then left;
    ``append_branch_summary(m0)`` moves the leaf back to ``m0`` and appends ``s``
    there, so ``b1`` and ``s`` are SIBLINGS — which §1.2 says is already true in
    the log and §4.3 says a reader cannot see. ``m1`` continues under the summary
    and is the cursor, so the summary is on the cursor's path and the abandoned
    branch is not. Returns the view plus the five ids.
    """
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    m0 = log.append_message({"role": "user", "content": "m0"})
    b1 = log.append_message({"role": "assistant", "content": "b1"})
    b2 = log.append_message({"role": "user", "content": "b2"})
    s = log.append_branch_summary("tried b, went nowhere", m0)
    m1 = log.append_message({"role": "assistant", "content": "m1"})
    return ConversationTree(log.entries(), log.cursor), m0, b1, b2, s, m1


async def test_a_branch_summary_and_the_branch_it_summarizes_read_as_a_pair() -> None:
    """§4.3, the last open piece of §4.

    ``append_branch_summary`` already parents the summary at the branch point, so
    the summary and the abandoned branch's first message are siblings in the log —
    "only the rendering is missing" (§1.2). It could not be done in
    ``_preview_of``, which renders one line for one node; this is a relation
    between two rows, so it is zone work (§3).

    The pairing is carried by the two zones sharing a hue and differing in weight.
    Asserted as such rather than against two hex values: the stylesheet owns the
    colours and a theme swap should be able to move them, but not to break the
    relation into two unrelated marks.
    """
    from tau_coding_agent.app import ZoneTree

    view, m0, b1, b2, s, _m1 = _abandoned_branch_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        assert tree.zones.summary == frozenset({s})
        assert tree.zones.abandoned == frozenset({b1})

        summary_style = tree.get_component_rich_style("tree--zone-summary", partial=True)
        abandoned_style = tree.get_component_rich_style("tree--zone-abandoned", partial=True)
        assert summary_style != Style(), "the stylesheet defines no tree--zone-summary"
        assert abandoned_style != Style(), "the stylesheet defines no tree--zone-abandoned"
        # One hue says "these two are one relation"; the difference says which end.
        assert summary_style.color == abandoned_style.color
        assert summary_style != abandoned_style

        assert summary_style in _row_span_styles(tree, s)
        assert abandoned_style in _row_span_styles(tree, b1)
        # The summary is on the cursor's path, and the pair outranks `path` there:
        # `tree--zone-path` is true of a whole chain and would swallow the relation.
        path_style = tree.get_component_rich_style("tree--zone-path", partial=True)
        assert path_style not in _row_span_styles(tree, s)

        # Rows that are in neither half of the relation carry neither mark. `b2` is
        # deeper in the same abandoned branch — being NEAR the pair is not being in
        # it — and `m0` is the branch point both sides hang off.
        for unrelated in (b2, m0):
            assert summary_style not in _row_span_styles(tree, unrelated)
            assert abandoned_style not in _row_span_styles(tree, unrelated)


async def test_a_second_abandoned_branch_pairs_with_its_own_summary() -> None:
    """Two summaries under one branch point pair with one branch each.

    The rule is "the immediately preceding sibling", not "every sibling that is
    not a summary" — a branch point can be abandoned more than once, and the
    set-difference rule would blame the second summary for the first branch as
    well. ``b1, s1, c1, s2`` is the shape that tells the two rules apart.
    """
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    from tau_coding_agent.app import ZoneTree

    log = InMemorySessionLog()
    m0 = log.append_message({"role": "user", "content": "m0"})
    b1 = log.append_message({"role": "assistant", "content": "b1"})
    s1 = log.append_branch_summary("first attempt", m0)
    log.append_navigate(m0)
    c1 = log.append_message({"role": "assistant", "content": "c1"})
    s2 = log.append_branch_summary("second attempt", m0)
    view = ConversationTree(log.entries(), log.cursor)

    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        assert tree.zones.summary == frozenset({s1, s2})
        # b1 is s1's, c1 is s2's — and neither summary claims the other's branch.
        assert tree.zones.abandoned == frozenset({b1, c1})


# ---------------------------------------------------------------------------
# The hover divergence highlight (§3, step 5)
# ---------------------------------------------------------------------------


def _hover(tree, entry_id):
    """Put the pointer on ``entry_id``'s row by moving ``Tree.hover_line``.

    ``hover_line`` is the public reactive Textual's own ``_on_mouse_move`` writes
    (textual 8.2.7, ``_tree.py:655/1081``), so setting it runs exactly the watcher
    a real mouse would. ``test_a_real_mouse_move_reaches_the_divergence`` covers
    the pointer path itself; these tests want a chosen row, not a chosen cell.
    """
    node = next(n for n in _widget_rows(tree.root) if n.data == entry_id)
    tree.hover_line = node.line


async def test_hovering_off_the_cursors_path_splits_shared_history_from_divergent() -> None:
    """§3's "the divergence between the cursor's path and a hovered node", step 5.

    §2 flattened single-child runs into siblings, which took Textual's
    ``tree--guides-hover`` ancestry rails away — "§3 is the replacement, not an
    embellishment". ``tree--zone-path`` replaced the cursor's ancestry in step 3.
    This is the hover's, and it says more than the rails did: not merely which
    rows are the hovered node's ancestors, but where that ancestry stops agreeing
    with where the cursor is.

    Both halves are asserted painted AND asserted different, because a renderer
    that paints one style over the whole hovered chain passes either alone while
    saying nothing about the divergence.
    """
    from tau_coding_agent.app import ZoneTree

    view, m0, m1, m2, b1 = _forked_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        assert tree.zones.cursor == m2

        _hover(tree, b1)
        await pilot.pause()

        # m0 is the branch point: shared. b1 hangs off it: divergent. m1 is on the
        # cursor's path but not on the hovered node's, so it is in neither.
        assert tree.zones.hover_common == frozenset({m0})
        assert tree.zones.hover_divergent == frozenset({b1})

        common = tree.get_component_rich_style("tree--zone-hover-common", partial=True)
        divergent = tree.get_component_rich_style("tree--zone-hover-divergent", partial=True)
        assert common != Style(), "the stylesheet defines no tree--zone-hover-common"
        assert divergent != Style(), "the stylesheet defines no tree--zone-hover-divergent"
        assert common != divergent, "the two halves of the divergence read the same"

        assert common in _row_span_styles(tree, m0)
        assert divergent in _row_span_styles(tree, b1)
        assert common not in _row_span_styles(tree, m1)
        assert divergent not in _row_span_styles(tree, m1)

        # Layered, not substituted: m0 is still on the cursor's path and still
        # says so. This is why the shared half sets no colour of its own.
        path_style = tree.get_component_rich_style("tree--zone-path", partial=True)
        assert path_style in _row_span_styles(tree, m0)


async def test_hovering_on_the_cursors_path_reports_no_divergence() -> None:
    """An ancestor of the cursor diverges from it nowhere, and is painted nowhere.

    Its chain is a PREFIX of the cursor's, so the divergent tail is empty by
    construction. Painting the shared half on its own would draw a highlight whose
    only content is "you are already here" — which a reader coming from the case
    above would read as a divergence that is not there.
    """
    from tau_coding_agent.app import ZoneTree

    view, m0, m1, m2, b1 = _forked_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        assert tree.zones.cursor == m2

        # First establish that this tree CAN report a divergence, so the emptiness
        # below is the rule doing its job and not the wiring being absent.
        _hover(tree, b1)
        await pilot.pause()
        assert tree.zones.hover_divergent == frozenset({b1})

        _hover(tree, m1)
        await pilot.pause()
        assert tree.zones.hover_common == frozenset()
        assert tree.zones.hover_divergent == frozenset()

        common = tree.get_component_rich_style("tree--zone-hover-common", partial=True)
        divergent = tree.get_component_rich_style("tree--zone-hover-divergent", partial=True)
        for row in (m0, m1, b1):
            assert common not in _row_span_styles(tree, row)
            assert divergent not in _row_span_styles(tree, row)


async def test_moving_the_cursor_re_measures_the_divergence_from_where_it_now_is() -> None:
    """The divergence is a relation between two nodes; either end moving stales it.

    Hover ``b1`` from ``m2`` and the split is ``{m0} / {b1}``. Move the cursor ONTO
    ``b1`` without touching the mouse and there is nothing left to diverge from —
    a renderer that only recomputes on hover would still be painting the old
    answer.
    """
    from tau_coding_agent.app import ZoneTree

    view, m0, _m1, _m2, b1 = _forked_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        _hover(tree, b1)
        await pilot.pause()
        assert tree.zones.hover_common == frozenset({m0})

        tree.move_cursor(next(n for n in _widget_rows(tree.root) if n.data == b1))
        await pilot.pause()
        assert tree.zones.hover_common == frozenset()
        assert tree.zones.hover_divergent == frozenset()


async def test_a_real_mouse_move_reaches_the_divergence() -> None:
    """The pointer path, once, end to end.

    The tests above write ``hover_line`` so they can name a row. This one drives
    ``Pilot.hover``, which posts a ``MouseMove`` the screen resolves to a style
    with a ``line`` meta — the chain ``Tree._on_mouse_move`` reads. It is what
    proves the highlight is reachable with a mouse rather than only with the
    reactive.
    """
    from tau_coding_agent.app import ZoneTree

    view, m0, _m1, _m2, b1 = _forked_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test(size=(80, 24)) as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        row = next(n for n in _widget_rows(tree.root) if n.data == b1)
        # +1 for the tree's own border; a few cells in, so the pointer lands on
        # the row body rather than on the frame.
        await pilot.hover(tree, offset=(4, row.line + 1))
        await pilot.pause()
        assert tree.hover_line == row.line, "the pointer did not land on b1's row"
        assert tree.zones.hover_common == frozenset({m0})
        assert tree.zones.hover_divergent == frozenset({b1})


# ---------------------------------------------------------------------------
# PLAN-0.9.4 §4: turn groups, hidden `navigate` rows, and what Enter means.
#
# `plan_tree_rows` is pure, so the shape rules are tested against it directly
# rather than through a Pilot. What the widget build does with those rows is
# tested through the modal, once.
# ---------------------------------------------------------------------------


def _log_with_two_turns_forked_from_one_answer():
    """The structure the owner reported, built as it really happens.

    One answer ("no such file") is the fork point: the reader tried one follow-up,
    navigated back to that answer, and tried a different one. The `navigate` entry
    the second attempt appends is the row item 4 is about.
    """
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    ids = {}
    ids["q0"] = log.append_message({"role": "user", "content": "read /tmp/context_test"})
    ids["a0"] = log.append_message({"role": "assistant", "content": "No such file. Create one?"})
    ids["u1"] = log.append_message({"role": "user", "content": "Yes, write your favorite number."})
    ids["t1"] = log.append_message({"role": "toolResult", "content": "Wrote 1 lines"})
    ids["a1"] = log.append_message({"role": "assistant", "content": "Wrote `42`."})
    ids["nav"] = log.append_navigate(ids["a0"])
    ids["u2"] = log.append_message({"role": "user", "content": "Actually, check again!"})
    ids["t2"] = log.append_message({"role": "toolResult", "content": "42"})
    ids["a2"] = log.append_message({"role": "assistant", "content": "Whoops, it contains `42`."})
    return ConversationTree(log.entries(), log.cursor), ids


def _plan(view):
    from tau_coding_agent.app import plan_tree_rows

    return plan_tree_rows(view.tree())


def _shape(view) -> list[tuple[int, str, str]]:
    """(depth, role-or-kind, preview) per drawn row — the tree as a reader sees it."""
    return [(row.depth, row.node.role or row.node.kind, row.node.preview) for row in _plan(view)]


def test_a_user_message_owns_its_turn() -> None:
    """§4 item 3. The turn's tool traffic and answer hang off the message that
    asked for them, so `←` on the user row folds the whole turn away — which is
    what "there's rarely anything to fold" was about."""
    view, ids = _log_with_two_turns_forked_from_one_answer()
    rows = {row.node.id: row for row in _plan(view)}
    group = rows[ids["u2"]]
    assert rows[ids["t2"]].parent is not None
    assert _plan(view)[rows[ids["t2"]].parent].node.id == ids["u2"]
    assert rows[ids["t2"]].depth == group.depth + 1
    assert rows[ids["a2"]].depth == group.depth + 1


def test_the_next_user_message_is_a_sibling_not_a_child() -> None:
    """The half that keeps §2's bound: a turn group CLOSES at the next user
    message. Without this a hundred linear turns would be a hundred levels deep,
    which is the exact defect TREE-BROWSER-AS-EDITOR.md §2 removed."""
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    for i in range(30):
        log.append_message({"role": "user", "content": f"q{i}"})
        log.append_message({"role": "toolResult", "content": f"r{i}"})
        log.append_message({"role": "assistant", "content": f"a{i}"})
    rows = _plan(ConversationTree(log.entries(), log.cursor))
    assert len(rows) == 90
    assert max(row.depth for row in rows) == 1, "one level for the turn, and no more"
    users = [row for row in rows if row.node.role == "user"]
    assert {row.parent for row in users} == {None}, "every turn is a top-level row"


def test_a_turn_group_starts_collapsed_and_the_one_you_are_in_does_not() -> None:
    """§4 item 3, "start collapsed". The exception is the group the cursor row is
    in — a browser that opens without showing where you are has failed at its one
    job.

    Keyed off the WIDGET ancestry, not the ``parentId`` chain. In a linear
    conversation every earlier user message is a ``parentId`` ancestor of the
    cursor and none is a widget ancestor, so the data chain would leave every turn
    in the session open — the state item 3 asks to get out of.
    """
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    for i in range(5):
        log.append_message({"role": "user", "content": f"q{i}"})
        log.append_message({"role": "assistant", "content": f"a{i}"})
    rows = _plan(ConversationTree(log.entries(), log.cursor))
    users = [row for row in rows if row.node.role == "user"]
    assert [row.expanded for row in users] == [False, False, False, False, True]
    # Nothing that is not a turn group was folded: a fork the reader has not
    # touched still shows its branches.
    assert all(row.expanded for row in rows if row.node.role != "user")


def test_a_navigate_row_is_not_drawn_and_its_turn_moves_up_to_the_fork() -> None:
    """§4 item 4. The entry stays in the log and on the ancestry; it just stops
    costing a row between an answer and the turn that forked off it."""
    view, ids = _log_with_two_turns_forked_from_one_answer()
    rows = _plan(view)
    assert ids["nav"] not in {row.node.id for row in rows}
    assert view.entry(ids["nav"])["type"] == "navigate", "still in the log"
    # The second turn hangs off the ANSWER it forked from, which is what happened.
    by_id = {row.node.id: row for row in rows}
    assert rows[by_id[ids["u2"]].parent].node.id == ids["a0"]
    # …as does the first, so the fork reads as one.
    assert rows[by_id[ids["u1"]].parent].node.id == ids["a0"]
    assert by_id[ids["u1"]].depth == by_id[ids["u2"]].depth


def test_the_cursor_keeps_its_row_even_when_it_is_a_navigate() -> None:
    """The exception that is not tidiness: hiding the cursor would leave the
    reader with no `◀ current` row at all.

    Reached by pointing a ``ConversationTree`` at the navigate entry, because
    ``append_navigate`` moves the leaf to the navigate's TARGET rather than to the
    entry itself — so no store this repo ships puts the cursor here. The cursor is
    a constructor argument and a pi-imported log is not bound by that contract, so
    the guard is reachable and this is how.
    """
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    first = log.append_message({"role": "user", "content": "q"})
    log.append_message({"role": "assistant", "content": "a"})
    nav = log.append_navigate(first)
    assert log.cursor != nav, "the contract: the leaf advances to the TARGET"
    rows = _plan(ConversationTree(log.entries(), nav))
    assert nav in {row.node.id for row in rows}
    # …and it is gone again the moment it stops being the cursor.
    assert nav not in {row.node.id for row in _plan(ConversationTree(log.entries(), log.cursor))}


def test_a_navigate_that_forks_keeps_its_row() -> None:
    """The other exception. Two branches under one `navigate` drawn as one run is
    a shape the log does not have."""
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    root = log.append_message({"role": "user", "content": "q"})
    log.append_message({"role": "assistant", "content": "a"})
    nav = log.append_navigate(root)
    # ``append_at`` twice rather than ``append_message``: the second would parent
    # at the LEAF, which ``append_navigate`` just moved to the navigate's target.
    b1 = log.append_at(nav, "message", {"message": {"role": "user", "content": "b1"}})
    b2 = log.append_at(nav, "message", {"message": {"role": "user", "content": "b2"}})
    rows = _plan(ConversationTree(log.entries(), log.cursor))
    assert nav in {row.node.id for row in rows}
    by_id = {row.node.id: row for row in rows}
    assert rows[by_id[b1].parent].node.id == nav
    assert rows[by_id[b2].parent].node.id == nav


def test_the_drawn_rows_are_the_log_minus_the_hidden_ones_in_order() -> None:
    """The planner drops rows and re-parents them. It must not reorder them, and
    it must not lose one: a browser showing a conversation the log does not have
    is worse than a cluttered one."""
    view, ids = _log_with_two_turns_forked_from_one_answer()
    drawn = [row.node.id for row in _plan(view)]
    assert drawn == [ids[k] for k in ("q0", "a0", "u1", "t1", "a1", "u2", "t2", "a2")]


async def test_the_widget_tree_matches_the_plan() -> None:
    """The build is a transcription of the plan — asserted once, here, so the
    rules above can be tested without a terminal."""
    view, ids = _log_with_two_turns_forked_from_one_answer()
    modal = SessionTreeModal(view)
    harness = _ModalHarness(modal)
    async with harness.run_test() as pilot:
        await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        plan = _plan(view)
        assert [n.data for n in _widget_rows(tree.root)] == [row.node.id for row in plan]
        assert [_widget_depth(tree, row.node.id) for row in plan] == [row.depth for row in plan]
        assert [depth for _n, _l, depth, _k in modal._rows] == [row.depth for row in plan]
        folded = {n.data for n in _widget_rows(tree.root) if not n.is_expanded}
        assert folded == {ids["u1"]}, "the turn the cursor is not in"
        # The cursor is on screen: every widget ancestor of its row is open. The
        # walk stops one short of the widget root, which Textual builds collapsed
        # and draws anyway under ``show_root = False`` — the same exclusion
        # ``SessionTreeModal._hidden`` makes, for the same reason.
        walk = next(n for n in _widget_rows(tree.root) if n.data == ids["a2"]).parent
        while walk is not None and walk.parent is not None:
            assert walk.is_expanded
            walk = walk.parent


async def test_a_row_nothing_hangs_from_has_no_expand_arrow() -> None:
    """Textual draws the toggle off ``allow_expand`` ALONE and never asks whether
    there are children (``Tree.render_label``, textual 8.2.7), so every assistant
    and tool row used to wear an arrow that clicked, toggled, and revealed
    nothing.

    ``has_children`` is a property of the PLAN, not of the entry: an assistant
    whose only child is a hidden ``navigate`` has a child in the log and none on
    screen, and it is the screen the arrow is a promise about.
    """
    view, ids = _log_with_two_turns_forked_from_one_answer()
    plan = {row.node.id: row for row in _plan(view)}
    # The two turn groups and the answer they fork from — these open something.
    assert {i for i, row in plan.items() if row.has_children} == {
        ids["q0"],
        ids["a0"],
        ids["u1"],
        ids["u2"],
    }
    # `a1` ends a turn and its only child is the hidden `navigate`.
    assert plan[ids["a1"]].has_children is False

    modal = SessionTreeModal(view)
    harness = _ModalHarness(modal)
    async with harness.run_test(size=(100, 30)) as pilot:
        for _ in range(8):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        for node in _widget_rows(tree.root):
            assert node.allow_expand is plan[node.data].has_children, node.label
        # …and on the screen the reader looks at: the drawn rows that carry an
        # arrow are exactly the ones with something under them.
        drawn = [
            line
            for line in render_text(harness).splitlines()
            if "assistant:" in line or "user:" in line
        ]
        arrowed = [line for line in drawn if "▼" in line or "▶" in line]
        assert len(drawn) > len(arrowed), "some rows are drawn with no arrow at all"
        assert all(("user:" in line or "No such file" in line) for line in arrowed), arrowed


async def test_a_row_with_no_arrow_gets_those_two_cells_for_its_preview() -> None:
    """The width arithmetic follows the toggle. ``_relabel`` charged every row for
    one, which on a row that has none is two characters of preview thrown away."""
    view = _wide_tree(6)
    modal = SessionTreeModal(view)
    harness = _ModalHarness(modal)
    async with harness.run_test(size=(80, 24)) as pilot:
        for _ in range(10):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        # The scrollbar's width is reserved whether or not the bar is showing —
        # see ``_relabel``. ``styles.scrollbar_size_vertical``, not the widget
        # property, which answers 0 when it is hidden.
        width = tree.content_size.width - tree.styles.scrollbar_size_vertical
        # The fixture's previews are 200 characters, so every row is elided and
        # its label length IS the budget it was given.
        seen = set()
        for node, _label, depth, has_children in modal._rows:
            toggle = tree.guide_depth if has_children else 0
            assert len(str(node.label)) == width - depth * tree.guide_depth - toggle
            seen.add(has_children)
        assert seen == {True, False}, "both kinds of row are in this fixture"
        assert tree.show_horizontal_scrollbar is False, "and it still fits"


async def test_opening_a_turn_does_not_bring_the_horizontal_scrollbar_back() -> None:
    """The scrollbar width is reserved whether the bar is there or not, and this
    is why.

    A fold changes how many ROWS the tree holds, which decides whether it has a
    vertical scrollbar, which is two cells of the width every label is sized
    against. Sizing against the CURRENT state means a tree that opens short and
    unscrolled gets full-width labels, and the first turn the reader opens puts a
    vertical scrollbar there and pushes every one of them over — the reported
    symptom, back again, by a gesture the turn groups introduced.

    The fixture is built for exactly that: a long first turn and a short last
    one, so only the short one is open at mount and the tree starts at four rows.
    """
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    log.append_message({"role": "user", "content": "question 0 " + "x" * 200})
    for j in range(30):
        log.append_message({"role": "toolResult", "content": f"r0.{j} " + "y" * 200})
    log.append_message({"role": "assistant", "content": "answer 0 " + "y" * 200})
    log.append_message({"role": "user", "content": "question 1 " + "x" * 200})
    log.append_message({"role": "assistant", "content": "answer 1 " + "y" * 200})

    harness = _ModalHarness(SessionTreeModal(ConversationTree(log.entries(), log.cursor)))
    async with harness.run_test(size=(80, 24)) as pilot:
        for _ in range(10):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        assert tree.show_vertical_scrollbar is False, "the fixture opens short"
        assert tree.show_horizontal_scrollbar is False

        for node in list(_widget_rows(tree.root)):
            if node.allow_expand:
                node.expand()
        for _ in range(10):
            await pilot.pause()

        assert tree.show_vertical_scrollbar is True, "…and is long once opened"
        assert tree.show_horizontal_scrollbar is False
        assert tree.max_scroll_x == 0


# ---------------------------------------------------------------------------
# The row's type tag, painted (PLAN-0.9.4 §4, "spans for style")
# ---------------------------------------------------------------------------


async def test_each_row_paints_its_type_tag_in_that_roles_colour() -> None:
    """`user:` is the user hue, `toolResult:` the tool hue, and they differ.

    Both halves matter: a renderer that paints every tag one colour passes an
    "is it painted?" assertion, and a renderer that paints nothing passes a
    "they are not the same colour" one.
    """
    from rich.style import Style

    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog
    from tau_coding_agent.app import ZoneTree

    log = InMemorySessionLog()
    user = log.append_message({"role": "user", "content": "ask"})
    assistant = log.append_message({"role": "assistant", "content": "answer"})
    tool = log.append_message({"role": "toolResult", "content": "42"})
    log.append_message({"role": "assistant", "content": "done"})

    harness = _ModalHarness(SessionTreeModal(ConversationTree(log.entries(), log.cursor)))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        for node in list(_widget_rows(tree.root)):
            if node.allow_expand:
                node.expand()
        for _ in range(4):
            await pilot.pause()

        hues = {
            role: tree.get_component_rich_style(f"tree--kind-{role}", partial=True)
            for role in ("user", "assistant", "tool")
        }
        assert Style() not in hues.values(), "the stylesheet defines no tree--kind-* rule"
        assert len(set(hues.values())) == 3, "three roles, three colours"

        assert hues["user"] in _row_tag_styles(tree, user)
        assert hues["assistant"] in _row_tag_styles(tree, assistant)
        assert hues["tool"] in _row_tag_styles(tree, tool)
        # …and each row wears only its own.
        assert hues["tool"] not in _row_tag_styles(tree, user)


async def test_the_tag_is_painted_and_the_preview_after_it_is_not() -> None:
    """The span stops at the colon.

    Painting the whole label would make the row one colour, which is what it
    already was — the point is that the left edge is scannable and the sentence
    is not shouting.
    """
    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog
    from tau_coding_agent.app import ZoneTree

    log = InMemorySessionLog()
    log.append_message({"role": "user", "content": "ask"})
    assistant = log.append_message({"role": "assistant", "content": "a much longer answer here"})
    # Not the leaf: the cursor row is deliberately left unpainted (see
    # ``ZoneTree.render_label``), so the row under test has to be an ordinary one.
    log.append_message({"role": "user", "content": "and again"})

    harness = _ModalHarness(SessionTreeModal(ConversationTree(log.entries(), log.cursor)))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        for node in list(_widget_rows(tree.root)):
            if node.allow_expand:
                node.expand()
        for _ in range(4):
            await pilot.pause()

        node = next(n for n in _widget_rows(tree.root) if n.data == assistant)
        text = tree.render_label(node, Style(), Style())
        hue = tree.get_component_rich_style("tree--kind-assistant", partial=True)
        span = next(s for s in text.spans if s.style == hue)
        assert text.plain[span.start : span.end] == "assistant:"


async def test_a_bookkeeping_row_does_not_borrow_a_conversation_colour() -> None:
    """A `navigate` that forks keeps its row (PLAN-0.9.4 §4) — and reads as
    bookkeeping rather than as a turn."""
    from rich.style import Style

    from tau_agent_core.conversation_tree import ConversationTree
    from tau_agent_core.session_log import InMemorySessionLog
    from tau_coding_agent.app import ZoneTree

    log = InMemorySessionLog()
    root = log.append_message({"role": "user", "content": "ask"})
    nav = log.append_navigate(root)
    # Two children, so the planner keeps the navigate's row.
    log.append_at(nav, "message", {"message": {"role": "assistant", "content": "one"}})
    log.append_at(nav, "message", {"message": {"role": "assistant", "content": "two"}})

    harness = _ModalHarness(SessionTreeModal(ConversationTree(log.entries(), root)))
    async with harness.run_test() as pilot:
        for _ in range(6):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        for node in list(_widget_rows(tree.root)):
            if node.allow_expand:
                node.expand()
        for _ in range(4):
            await pilot.pause()

        structural = tree.get_component_rich_style("tree--kind-structural", partial=True)
        assert structural != Style()
        assert structural in _row_tag_styles(tree, nav)
        for role in ("user", "assistant", "tool"):
            assert tree.get_component_rich_style(f"tree--kind-{role}", partial=True) != structural
