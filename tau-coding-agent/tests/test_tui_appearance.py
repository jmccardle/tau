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

import pytest
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Collapsible, Tree

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
        for _widget_node, full, _depth in modal._rows:
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
    """
    async with open_scene(get_scene("ext-surfaces"), size) as (app, _pilot):
        host = app.query_one(ExtensionPanelHost)
        assert host.display, "the ext-surfaces scene should have a panel open"
        assert 26 <= host.region.width <= 44
        sidebar = app.query_one(ChatSidebar)
        if sidebar.display:
            assert 24 <= sidebar.region.width <= 32
        # The two old fixed widths, in one assertion: they cannot both be right at
        # two terminal sizes.
        assert (sidebar.region.width, host.region.width) != (30, 40)


async def test_the_sidebar_yields_to_a_panel_on_a_narrow_terminal() -> None:
    """At 80 columns the sidebar, the panel and a readable chat do not all fit.

    Something has to give, and it is the sidebar: the panel is what an extension
    just put on screen, while the sidebar is navigation that ctrl+b brings back.
    Hiding the panel instead would mean an extension's ``ctx.ui.panel`` call
    silently did nothing.
    """
    async with open_scene(get_scene("ext-surfaces"), (80, 24)) as (app, _pilot):
        assert not app.query_one(ChatSidebar).display
    async with open_scene(get_scene("ext-surfaces"), (120, 40)) as (app, _pilot):
        assert app.query_one(ChatSidebar).display


async def test_the_sidebar_comes_back_when_the_panel_closes() -> None:
    """The rule runs in both directions — the sidebar yields for as long as the
    panel is up, not for the rest of the session."""
    async with open_scene(get_scene("ext-surfaces"), (80, 24)) as (app, pilot):
        sidebar = app.query_one(ChatSidebar)
        assert not sidebar.display
        app.query_one(ExtensionPanelHost).set_panel("fleet", None)
        await pilot.pause()
        await pilot.pause()
        assert sidebar.display


async def test_the_sidebar_stays_on_a_narrow_terminal_with_no_panel() -> None:
    """The sidebar yields to a PANEL, not to a narrow terminal by itself."""
    async with open_scene(get_scene("sidebar"), (80, 24)) as (app, _pilot):
        assert app.query_one(ChatSidebar).display


async def test_ctrl_b_overrides_the_responsive_default() -> None:
    """An explicit toggle wins, in both directions.

    The responsive rule is a default, not a policy: a user who asks for the
    sidebar next to a panel on an 80-column terminal gets it (and pays for it in
    chat columns), and one who hides it keeps it hidden. The two paths write
    ``#sidebar``'s display through the same method for exactly this reason — an
    inline style set by one of them would otherwise be invisible to the other.
    """
    async with open_scene(get_scene("ext-surfaces"), (80, 24)) as (app, pilot):
        sidebar = app.query_one(ChatSidebar)
        assert not sidebar.display  # hidden by the responsive default
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
    """
    fits = Parley.SIDE_COLUMNS_MIN_WIDTH
    async with open_scene(get_scene("ext-surfaces"), (fits, 30)) as (app, _pilot):
        assert app.query_one(ChatSidebar).display
        assert app.query_one(ChatDisplay).content_size.width >= CHAT_MIN_COLUMNS

    # One column narrower the sidebar hides. Ask for it back — the override path —
    # and the chat drops under the floor, which is why the breakpoint sits here.
    async with open_scene(get_scene("ext-surfaces"), (fits - 1, 30)) as (app, pilot):
        assert not app.query_one(ChatSidebar).display
        await pilot.press("ctrl+b")
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
