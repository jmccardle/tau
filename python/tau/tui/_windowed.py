"""The window of a long walk, drawn and moved (D23 section 7): the part of the transcript that is
not about nodes but about which of them are on screen.

In the manner of τ 0.15's tau-coding-agent/src/tau_coding_agent/transcript.py (tag v0.15.0: the
render cap, the trim at the tail, the slide at an edge), over a walk of nodes: the bounds are
`tau.tui._window`'s. `WindowedWalk` keeps the whole walk, a `Block` for each thing drawn, and the
rows that say how many nodes are above and below the window. A subclass says how a node is drawn
(`_draw`) and what to forget of a block that leaves (`_forget`, `_reset`).

Scrolling against the top or bottom edge (the wheel, or Ctrl+Up and Ctrl+Down) slides the window
one turn along the walk. A node added while the window is slid back is counted, not drawn, until
the window is slid to it or this client sends (`extend(jump=True)`). A turn that falls out of the
window at the head is removed when the reader is at the tail, and held until they come back if not.
"""

import asyncio
from dataclasses import dataclass

from textual import events
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from tau._core import Node
from tau.tui._window import tail_start, turn_starts, window_end


def _row(text: str) -> Static:
    """A line saying what the window leaves out."""
    return Static(text, classes="chat-fold", markup=False)


def _at_bottom(view: VerticalScroll) -> bool:
    return view.scroll_y >= view.max_scroll_y - 1


@dataclass
class Block:
    """What one node drew: `index` is its place in the walk (a record beside the walk, a
    `sample_error`, takes its parent's), `node` its id, `widget` what it mounted (`None` if it
    mounted nothing), `line` its plain text."""

    index: int
    node: int
    widget: Widget | None
    line: str


class WindowedWalk(VerticalScroll):
    """A scrolling container that draws `walk[_start:_end]` of the walk it holds."""

    def __init__(self, *, id: str, lock: asyncio.Lock) -> None:
        super().__init__(id=id)
        self._lock = lock  # the app's: one event, or one slide, draws at a time
        self._walk: list[Node] = []
        self._start = 0
        self._end = 0  # the window is walk[_start:_end]
        self._blocks: list[Block] = []
        self._earlier: Static | None = None
        self._later: Static | None = None
        self._sliding = False
        self._trim_deferred = False
        self._following = True  # whether the reader is at the tail, as they last left the scroll

    @property
    def tip(self) -> int | None:
        """The last node of the walk, drawn or not (`None` before the first `rebuild`)."""
        return self._walk[-1].id if self._walk else None

    @property
    def at_tail(self) -> bool:
        """Whether the window ends the walk."""
        return self._end == len(self._walk)

    @property
    def streaming(self) -> bool:
        """Whether a reply is being drawn live: the window does not move then."""
        raise NotImplementedError

    async def _draw(self, index: int) -> None:
        """Draw `walk[index]`, which follows the last block, with `place`."""
        raise NotImplementedError

    async def _shown(self) -> None:
        """The window was drawn (`_show`), at the tail or not."""

    def _reset(self) -> None:
        """The window is being redrawn: forget what was drawn."""
        raise NotImplementedError

    def _forget(self, node: int) -> None:
        """The block of `node` left the window at the head."""
        raise NotImplementedError

    async def extend(self, node: Node, *, jump: bool) -> None:
        """Take `node`, which follows the tip, onto the walk: drawn when the window ends the walk;
        else, if `jump`, by moving the window to the tail; else counted in the row below."""
        at_tail = self.at_tail
        self._walk.append(node)
        if at_tail:
            await self._draw(len(self._walk) - 1)
            self._end += 1
            await self._trim()
        elif jump:
            await self._show(tail_start(self._walk), len(self._walk))
        else:
            await self._sync_later()

    async def _show(self, start: int, end: int) -> None:
        """Draw `walk[start:end]` and nothing else."""
        await self.remove_children()
        self._blocks, self._earlier, self._later = [], None, None
        self._reset()
        self._start, self._end = start, end
        with self.app.batch_update():  # pyright: ignore[reportUnknownMemberType]
            if start > 0:
                self._earlier = _row(self._earlier_text)
                await self.mount(self._earlier)
            for i in range(start, end):
                await self._draw(i)
            if end < len(self._walk):
                self._later = _row(self._later_text)
                await self.mount(self._later)
        await self._shown()

    async def place(self, index: int, node: int, widget: Widget | None, line: str) -> None:
        if widget is not None:
            await self._mount(widget)
        self._blocks.append(Block(index, node, widget, line))

    def on_resize(self) -> None:
        """A reader who was following the tail still is when the transcript gets shorter (a
        question box opens above the editor): the tail stays in view."""
        if self._following:
            self.call_after_refresh(self.scroll_end, animate=False)

    async def _mount(self, widget: Widget) -> None:
        """Add a child at the end, and keep the tail in view if it was in view."""
        following = self._following
        await self.mount(widget)
        if following:
            self.scroll_end(animate=False)

    # The window

    @property
    def _earlier_text(self) -> str:
        return f"⋯ {self._start} earlier nodes (Ctrl+Up at the top shows them)"

    @property
    def _later_text(self) -> str:
        return f"⋯ {len(self._walk) - self._end} later nodes (Ctrl+Down at the bottom shows them)"

    async def _trim(self) -> None:
        """Drop the turns that have fallen out of the window at the head, when the reader is at the
        tail (removing the head under someone who scrolled up would move the page). Held until
        they come back, and not done while a reply is live."""
        if self.streaming or not self.at_tail:
            return
        new = tail_start(self._walk)
        if new <= self._start:
            return
        if not self._following:
            self._trim_deferred = True
            return
        self._trim_deferred = False
        kept: list[Block] = []
        for block in self._blocks:
            if block.index >= new:
                kept.append(block)
                continue
            self._forget(block.node)
            if block.widget is not None:
                await block.widget.remove()
        self._blocks, self._start = kept, new
        await self._sync_earlier()
        self.scroll_end(animate=False)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Scrolling decides whether the reader is following the tail (a resize or new content
        does not); one who comes back to it lets a held trim run."""
        super().watch_scroll_y(old_value, new_value)
        self._following = new_value >= self.max_scroll_y - 1
        if self._trim_deferred and self._following and not self._sliding:
            self._trim_deferred = False
            self.call_later(self._trim_when_free)

    async def _sync_earlier(self) -> None:
        if self._start == 0 and self._earlier is not None:
            await self._earlier.remove()
            self._earlier = None
        elif self._start > 0:
            if self._earlier is None:
                self._earlier = _row(self._earlier_text)
                await self.mount(self._earlier, before=0)
            else:
                self._earlier.update(self._earlier_text)

    async def _sync_later(self) -> None:
        after = len(self._walk) - self._end
        if after == 0 and self._later is not None:
            await self._later.remove()
            self._later = None
        elif after > 0:
            if self._later is None:
                self._later = _row(self._later_text)
                await self.mount(self._later)
            else:
                self._later.update(self._later_text)

    def slide(self, turns: int) -> bool:
        """Start moving the window `turns` turns along the walk (negative: towards the head), and
        say whether it can: not while a reply is live or another slide is running, and not past
        either end. The move is a task of its own; `text` shows it when it is done."""
        if self._sliding or self.streaming:
            return False
        if (turns < 0 and self._start == 0) or (turns > 0 and self.at_tail):
            return False
        self._sliding = True
        self.call_later(self._slide, turns)
        return True

    async def _trim_when_free(self) -> None:
        async with self._lock:
            await self._trim()

    async def _slide(self, turns: int) -> None:
        try:
            async with self._lock:
                if self.streaming:  # a reply began since the slide was asked for
                    return
                starts = turn_starts(self._walk)
                here = starts.index(self._start)
                start = starts[min(max(here + turns, 0), len(starts) - 1)]
                anchor = self._start if turns < 0 else self._end  # where the reader was looking
                await self._show(start, window_end(self._walk, start))
                widget = next(
                    (b.widget for b in self._blocks if b.index >= anchor and b.widget), None
                )
                if widget is not None:
                    self.call_after_refresh(self.scroll_to_widget, widget, animate=False, top=True)
        finally:
            self._sliding = False

    def scroll_page(self, direction: str) -> None:
        """Ctrl+Up or Ctrl+Down: a page, or at the edge the window slides a turn."""
        if direction == "up":
            if self.scroll_y > 0:
                self.scroll_page_up(animate=False)
            else:
                self.slide(-1)
        elif _at_bottom(self):
            self.slide(1)
        else:
            self.scroll_page_down(animate=False)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.scroll_y <= 0 and self.slide(-1):
            event.stop()
            return
        super()._on_mouse_scroll_up(event)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if _at_bottom(self) and self.slide(1):
            event.stop()
            return
        super()._on_mouse_scroll_down(event)
