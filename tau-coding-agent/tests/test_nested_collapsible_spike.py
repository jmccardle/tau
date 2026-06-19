"""Spike: nested Collapsibles inside a VerticalScroll must grow to natural
height with NO interior scrollbars — only the outer message area scrolls.

This is the layout gate for the Exchange -> tool-Collapsible -> reasoning-
Collapsible design. We assert it headlessly via Pilot so the guarantee is
reproducible (Textual 8.2.7).

Requirement (from design discussion): exchanges, message boxes, and reasoning
content can each be as tall as they like; the scrollbar belongs to the message
area alone.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Markdown, Static


def _tall(lines: int, tag: str) -> Static:
    """A Static of a known, deterministic line count (auto height)."""
    return Static("\n".join(f"{tag} line {i}" for i in range(lines)))


class SpikeApp(App[None]):
    # Mirror the real setup: the message log is the only thing that scrolls.
    CSS = """
    #log { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="log"):
            # 2 exchanges, each ~43 rows of content -> far exceeds any window,
            # forcing the outer scroll to absorb it (and exposing any inner clip).
            for e in range(2):
                with Collapsible(title=f"Exchange {e}", collapsed=False, id=f"exchange-{e}"):
                    with Collapsible(title="Thinking", collapsed=False, id=f"reasoning-{e}"):
                        yield _tall(15, f"reason{e}")
                    yield Markdown(f"# Answer {e}\n\n" + "\n\n".join(f"para {i}" for i in range(6)))
                    with Collapsible(title="read(path=…)", collapsed=False, id=f"tool-{e}"):
                        yield _tall(15, f"tool{e}")


async def test_only_outer_message_area_scrolls():
    app = SpikeApp()
    # Small window so the ~86 rows of content cannot possibly fit.
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        log = app.query_one("#log", VerticalScroll)

        # 1) The message area scrolls.
        assert log.show_vertical_scrollbar is True, "outer log should scroll"
        assert log.max_scroll_y > 0, "outer log should have scrollable overflow"

        # 2) Natural height propagated through every nested Collapsible — nothing
        #    clipped. If any inner level had capped its height, the outer virtual
        #    height would collapse toward the 20-row window.
        assert log.virtual_size.height >= 60, (
            f"content appears clipped: virtual height {log.virtual_size.height}"
        )

        # 3) No interior scrollbars on any content widget or collapsible.
        for w in list(app.query(Collapsible)) + list(app.query(Static)) + list(app.query(Markdown)):
            assert w.show_vertical_scrollbar is False, f"{w!r} has an interior scrollbar"
            assert w.show_horizontal_scrollbar is False, f"{w!r} has an interior scrollbar"

        # 4) Each nested collapsible expanded to its full content height.
        for cid, min_h in [("reasoning-0", 15), ("tool-0", 15), ("reasoning-1", 15)]:
            c = app.query_one(f"#{cid}", Collapsible)
            assert c.size.height >= min_h, f"{cid} clipped to height {c.size.height}"


async def test_collapsing_an_inner_collapsible_shrinks_not_scrolls():
    """Collapsing a level hides its body (height shrinks); it never becomes an
    interior scroll region. Confirms the collapse mechanism is display-toggle."""
    app = SpikeApp()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        tool = app.query_one("#tool-0", Collapsible)
        full_h = tool.size.height
        assert full_h >= 15

        tool.collapsed = True
        await pilot.pause()
        assert tool.size.height < full_h, "collapsing should shrink the widget"
        assert tool.show_vertical_scrollbar is False
