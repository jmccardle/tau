"""A click on a widget that left the DOM since the last paint is a click on nothing, not a crash.
Textual finds the widget under the pointer from the last paint's map, and the transcript removes
widgets between paints (a streaming reply, a committed reply, a sliding window)."""

import sys
from pathlib import Path

import pytest
from fakes import FakeProvider
from harness import Harness, scenario
from textual import events
from textual.errors import NoWidget
from tuikit import running, say, shows, type_line

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows: the CI runner paints on another schedule, and this test reads the last "
    "paint's map between paints (GitHub runs 44756741622, 44767727045); held on Linux and macOS",
)


@scenario
async def test_a_removed_widget_is_not_under_the_pointer(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([say("a paragraph of the reply")]))
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "a paragraph of the reply")
        paragraph = app.query("MarkdownParagraph").first()  # Textual's block, by its CSS type name
        region = app.screen.find_widget(paragraph).region
        point = (region.x + 1, region.y)
        assert app.screen.get_widget_at(*point)[0] is paragraph
        await paragraph.remove()
        # Usually no repaint yet, and the last paint's map still holds the paragraph; a runner may
        # repaint first (Windows did), and then another widget is there. Either way, not this one.
        assert app.screen.get_widget_and_offset_at(*point)[0] is not paragraph
        try:
            under = app.screen.get_widget_at(*point)[0]
        except NoWidget:
            pass
        else:
            assert under is not paragraph, "the removed widget is still under the pointer"
        down = events.MouseDown(
            None, point[0], point[1], 0, 0, 1, False, False, False,
            screen_x=point[0], screen_y=point[1],
        )  # fmt: skip
        app.post_message(down)  # as the driver does, before the next paint
        await pilot.pause()
        await pilot.click(offset=point)  # the same point, after the repaint
    await h.close()
