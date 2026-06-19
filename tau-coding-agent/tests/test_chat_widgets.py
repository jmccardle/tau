"""Unit tests for the collapsible chat widgets (reasoning, tool, exchange).

Driven through a real Textual app via Pilot so mount/compose/reactive behavior
is exercised the way the live TUI will use them.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Markdown

from tau_coding_agent.chat_widgets import (
    ExchangeBox,
    ReasoningRegion,
    ToolBox,
    format_duration,
    format_tokens,
    format_tool_summary,
)
from tau_coding_agent.app import MessageBox


class _Host(App[None]):
    """Mounts a single widget supplied at construction into a scroll area."""

    def __init__(self, widget) -> None:
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="log"):
            yield self._widget


# ── pure formatters ────────────────────────────────────────────────────────

def test_format_tool_summary():
    assert format_tool_summary("read", {"path": "main.py"}) == "read(path=main.py)"
    assert format_tool_summary("now", {}) == "now()"
    # long values are truncated
    s = format_tool_summary("bash", {"command": "x" * 100})
    assert s.startswith("bash(command=") and "…" in s and len(s) < 90


def test_format_tokens_and_duration():
    assert format_tokens(0) == "0"
    assert format_tokens(999) == "999"
    assert format_tokens(102700) == "102.7k"
    assert format_duration(6) == "0:06"
    assert format_duration(186) == "3:06"


# ── ReasoningRegion ─────────────────────────────────────────────────────────

async def test_reasoning_region_streams_and_finishes():
    region = ReasoningRegion()
    async with _Host(region).run_test() as pilot:
        await pilot.pause()
        assert region.collapsed is False  # expanded by default (matches pi)
        region.append("Let me ")
        region.append("think.")
        await pilot.pause()
        assert region.text == "Let me think."
        region.mark_done(12)
        await pilot.pause()
        assert region.title == "Thought for 0:12"


async def test_reasoning_region_collapses_without_scrollbar():
    region = ReasoningRegion()
    async with _Host(region).run_test() as pilot:
        await pilot.pause()
        region.set_text("\n".join(f"line {i}" for i in range(40)))
        await pilot.pause()
        assert region.show_vertical_scrollbar is False
        region.collapsed = True
        await pilot.pause()
        assert region.collapsed is True


# ── ToolBox ─────────────────────────────────────────────────────────────────

async def test_tool_box_pairs_call_and_result():
    box = ToolBox("read", {"path": "main.py"}, tool_call_id="call_1")
    async with _Host(box).run_test() as pilot:
        await pilot.pause()
        # Collapsed by default, title is the call signature.
        assert box.collapsed is True
        assert box.title == "read(path=main.py)"
        assert box.has_result is False

        box.set_result("file contents here", is_error=False)
        await pilot.pause()
        assert box.has_result is True
        assert box.title == "✓ read(path=main.py)"
        assert box._result_md.display is True


async def test_tool_box_error_marks_title_and_class():
    box = ToolBox("bash", {"command": "false"})
    async with _Host(box).run_test() as pilot:
        await pilot.pause()
        box.set_result("non-zero exit", is_error=True)
        await pilot.pause()
        assert box.title == "✗ bash(command=false)"
        assert box.has_class("box-error")


# ── ExchangeBox ─────────────────────────────────────────────────────────────

async def test_exchange_box_groups_steps_and_summarizes():
    exchange = ExchangeBox()
    async with _Host(exchange).run_test() as pilot:
        await pilot.pause()
        assert exchange.collapsed is False  # expanded while streaming
        assert exchange.title == "Working…"

        r = ReasoningRegion()
        t1 = ToolBox("read", {"path": "a.py"})
        t2 = ToolBox("bash", {"command": "ls"})
        exchange.add_step(r)
        exchange.add_step(t1)
        exchange.add_step(t2)
        await pilot.pause()

        # Steps live in the collapsible body, in order.
        contents = exchange.query_one(Collapsible.Contents)
        kids = [w for w in contents.children]
        assert kids == [r, t1, t2]
        assert exchange.tool_count == 2

        exchange.set_summary(tools=2, tokens=102700, seconds=186)
        await pilot.pause()
        assert exchange.title == "✓ 2 tools · 102.7k tok · 3:06"


async def test_exchange_summary_shows_zero_tokens_not_hidden():
    """Fail-early, not error-hiding: a real 0 token count is shown, not omitted."""
    exchange = ExchangeBox()
    async with _Host(exchange).run_test() as pilot:
        await pilot.pause()
        exchange.set_summary(tools=1, tokens=0, seconds=5)
        await pilot.pause()
        assert exchange.title == "✓ 1 tool · 0 tok · 0:05"


async def test_nested_exchange_has_no_interior_scrollbars():
    """The whole point of the spike, now with the real widgets: deep nesting in
    a scroll area must not create interior scrollbars."""
    exchange = ExchangeBox()
    async with _Host(exchange).run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        r = ReasoningRegion()
        exchange.add_step(r)
        await pilot.pause()
        r.set_text("\n".join(f"reason {i}" for i in range(30)))
        t = ToolBox("read", {"path": "big.py"})
        exchange.add_step(t)
        await pilot.pause()
        t.set_result("\n".join(f"out {i}" for i in range(30)))
        t.collapsed = False
        await pilot.pause()

        for w in list(exchange.query(Collapsible)) + list(exchange.query(Markdown)):
            assert w.show_vertical_scrollbar is False, f"{w!r} scrolls"
        # The outer log is the only scroller.
        log = pilot.app.query_one("#log", VerticalScroll)
        assert log.show_vertical_scrollbar is True


# ── MessageBox as the universal host (one message → one widget) ──────────────

async def test_message_box_plain_text_is_unchanged():
    box = MessageBox("user", "hello")
    async with _Host(box).run_test() as pilot:
        await pilot.pause()
        assert box.role == "user"
        assert box.content_text == "hello"
        assert box.reasoning is None
        assert box.tool_boxes == {}


async def test_reasoning_renders_when_set_before_mount_completes():
    """Regression: reasoning set on a lazily-mounted region must still RENDER,
    not just store the string. The inner Markdown update was lost in the window
    before the region's compose finished, so the content showed blank."""
    box = MessageBox("assistant", "answer")
    async with _Host(box).run_test() as pilot:
        await pilot.pause()
        region = box.ensure_reasoning()
        region.set_text("the reasoning content")  # set immediately, pre-mount
        await pilot.pause()
        md = list(region.query(Markdown))[0]
        # A non-empty Markdown parses into >= 1 block child; the bug left it at 0.
        assert len(list(md.children)) > 0, "reasoning Markdown rendered empty"
        assert region.text == "the reasoning content"


async def test_message_box_hosts_a_reasoning_region():
    box = MessageBox("assistant", "")
    async with _Host(box).run_test() as pilot:
        await pilot.pause()
        assert box.reasoning is None
        region = box.ensure_reasoning()
        await pilot.pause()
        assert isinstance(region, ReasoningRegion)
        assert box.ensure_reasoning() is region  # idempotent — mounted once
        assert region in list(box.query(ReasoningRegion))
        region.append("thinking…")
        await pilot.pause()
        assert region.text == "thinking…"


async def test_message_box_hosts_tools_and_folds_results_by_id():
    box = MessageBox("assistant", "Let me check.")
    async with _Host(box).run_test() as pilot:
        await pilot.pause()
        t1 = box.add_tool_call("read", {"path": "a.py"}, "call_1")
        t2 = box.add_tool_call("bash", {"command": "ls"}, "call_2")
        await pilot.pause()
        assert isinstance(t1, ToolBox) and isinstance(t2, ToolBox)
        assert set(box.tool_boxes) == {"call_1", "call_2"}
        # Result folds into the right box, matched by id (even out of order).
        assert box.set_tool_result("call_2", "a\nb\nc", is_error=False) is True
        await pilot.pause()
        assert t2.has_result is True
        assert t1.has_result is False
        # Unknown id → False; nothing is fabricated.
        assert box.set_tool_result("call_999", "x") is False


async def test_message_box_reasoning_above_text_above_tools():
    """Document order inside the box is reasoning → text → tools."""
    box = MessageBox("assistant", "answer")
    async with _Host(box).run_test() as pilot:
        await pilot.pause()
        box.ensure_reasoning()
        box.add_tool_call("read", {"path": "a.py"}, "c1")
        await pilot.pause()
        children = list(box.children)
        ri = next(i for i, c in enumerate(children) if c.has_class("message-reasoning"))
        ci = next(i for i, c in enumerate(children) if c.has_class("message-content"))
        ti = next(i for i, c in enumerate(children) if c.has_class("message-tools"))
        assert ri < ci < ti
