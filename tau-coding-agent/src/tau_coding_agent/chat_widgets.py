"""Collapsible chat widgets: reasoning regions, paired tool call+result boxes,
and exchange grouping.

Built on Textual's ``Collapsible`` (validated for deep nesting with no interior
scrollbars in ``tests/test_nested_collapsible_spike.py``). Collapse/expand is
the framework's reactive→CSS-display mechanism — widgets mount once as content
streams in; toggling never re-mounts the DOM.

Design (from the reasoning/TUI discussion):
- ``ReasoningRegion`` — a completion's reasoning, streamed live, collapsible and
  rendered distinctly from the answer (pi renders reasoning dim+italic).
- ``ToolBox`` — a tool call paired with its result in ONE collapsible: the
  collapsed title is the one-line call signature; the body holds args + result.
- ``ExchangeBox`` — groups one user→answer exchange's steps under a summary line
  ("N tools · X tok · M:SS"); the final answer streams inside it.

These have NO dependency on the Parley app module, so they import cleanly.
"""

from __future__ import annotations

import json

from textual.widget import Widget
from textual.widgets import Collapsible, Markdown


# ──────────────────────────────────────────────────────────────────────────
# Formatting helpers (shared by the live and reload paths)
# ──────────────────────────────────────────────────────────────────────────

def format_tool_summary(name: str, arguments: object) -> str:
    """A one-line call signature, ``name(key=val, …)``, truncated for the
    collapsed title row. Values are shortened individually and the whole
    argument list is capped so the line stays scannable."""
    inner = ""
    if isinstance(arguments, dict) and arguments:
        parts = []
        for key, value in arguments.items():
            text = value if isinstance(value, str) else json.dumps(value, default=str)
            text = text.replace("\n", " ")
            if len(text) > 40:
                text = text[:39] + "…"
            parts.append(f"{key}={text}")
        inner = ", ".join(parts)
        if len(inner) > 60:
            inner = inner[:59] + "…"
    return f"{name}({inner})"


def format_tokens(n: int) -> str:
    """102700 → '102.7k'; small counts stay exact."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def format_duration(seconds: float) -> str:
    """186.0 → '3:06'."""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


# ──────────────────────────────────────────────────────────────────────────
# Widgets
# ──────────────────────────────────────────────────────────────────────────

class ReasoningRegion(Collapsible):
    """A collapsible reasoning/thinking block that streams live.

    Kept distinct from the answer so it can be reviewed and collapsed
    independently. Expanded by default (matches pi, which shows reasoning by
    default); the streaming state machine may collapse it once answer/tool
    content begins.
    """

    def __init__(self, *, collapsed: bool = False) -> None:
        self._md = Markdown("")
        super().__init__(self._md, title="Thinking…", collapsed=collapsed)
        self.add_class("reasoning-region")
        self._text = ""

    def on_mount(self) -> None:
        # Flush any text set before the inner Markdown was mounted (the region is
        # mounted lazily on the first reasoning delta, so set_text/append can run
        # in the window before compose finishes — that early update would be lost).
        if self._text:
            self._md.update(self._text)

    def set_text(self, text: str) -> None:
        self._text = text
        if self._md.is_mounted:
            self._md.update(text)

    def append(self, delta: str) -> None:
        self.set_text(self._text + delta)

    @property
    def text(self) -> str:
        return self._text

    def mark_done(self, seconds: float | None = None) -> None:
        """Freeze the title once reasoning is complete."""
        self.title = "Thought" if seconds is None else f"Thought for {format_duration(seconds)}"


class ToolBox(Collapsible):
    """A tool call paired with its result in ONE collapsible.

    Collapsed (the default — matching pi's default-collapsed tool output) shows
    just the call signature; expanded shows the arguments and, once it arrives,
    the result. The collapsed title gains a ✓/✗ status mark when the result
    lands, so the one-liner reads as call + outcome.
    """

    def __init__(self, name: str, arguments: object, tool_call_id: str = "") -> None:
        self.tool_name = name
        self.tool_call_id = tool_call_id
        self._summary = format_tool_summary(name, arguments)
        self._args_md = Markdown(self._args_block(arguments))
        self._result_md = Markdown("")
        self._result_md.display = False  # hidden until a result arrives
        super().__init__(self._args_md, self._result_md, title=self._summary, collapsed=True)
        self.add_class("tool-box")
        self.has_result = False

    @staticmethod
    def _args_block(arguments: object) -> str:
        return "```json\n" + json.dumps(arguments, indent=2, default=str) + "\n```"

    def set_result(self, result_text: str, is_error: bool = False) -> None:
        mark = "✗" if is_error else "✓"
        self.title = f"{mark} {self._summary}"
        body = result_text if len(result_text) <= 2000 else result_text[:2000] + "\n…(truncated)"
        self._result_md.update(f"```\n{body}\n```")
        self._result_md.display = True
        self.has_result = True
        if is_error:
            self.add_class("box-error")


class ExchangeBox(Collapsible):
    """Groups one user→answer exchange's steps (reasoning, tool calls, the final
    answer) under a single summary line.

    Expanded by default so streaming is visible; the title shows ``Working…``
    while the exchange runs and a ``N tools · X tok · M:SS`` summary once it
    finishes. Steps are mounted into the collapsible body as they arrive.
    """

    def __init__(self, *, collapsed: bool = False) -> None:
        super().__init__(title="Working…", collapsed=collapsed)
        self.add_class("exchange-box")
        self._tool_count = 0

    def add_step(self, widget: Widget) -> None:
        """Mount a step widget into the exchange body, in arrival order.

        The exchange is mounted (and its ``Collapsible.Contents`` composed)
        before any step is added — the caller awaits the exchange mount — so the
        body container is always present here.
        """
        self.query_one(Collapsible.Contents).mount(widget)
        if isinstance(widget, ToolBox):
            self._tool_count += 1

    async def add_step_async(self, widget: Widget) -> None:
        """Like :meth:`add_step` but awaits the mount.

        The reload path builds an exchange synchronously — it writes reasoning,
        text and tool *results* into a step right after mounting it — so it must
        wait for the step (and its slots) to compose before touching them. The
        live path is network-paced and doesn't need to wait, so it uses the
        fire-and-forget :meth:`add_step`.
        """
        await self.query_one(Collapsible.Contents).mount(widget)
        if isinstance(widget, ToolBox):
            self._tool_count += 1

    @property
    def tool_count(self) -> int:
        return self._tool_count

    def set_summary(self, *, tools: int, tokens: int, seconds: float | None = None) -> None:
        """Finalize the title with the exchange's stats. Token count of 0 is
        shown as 0 — we never hide a real value behind a branch.

        ``seconds`` is omitted on the reload path: wall-clock duration is not
        persisted, so a reconstructed exchange shows ``N tools · X tok`` without
        a fabricated time (Fail-Early)."""
        label = f"{tools} tool" + ("" if tools == 1 else "s")
        parts = [label, f"{format_tokens(tokens)} tok"]
        if seconds is not None:
            parts.append(format_duration(seconds))
        self.title = "✓ " + " · ".join(parts)
