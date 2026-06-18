"""FooterData and FooterWidget: Footer data contract and Textual widget.

This module defines both the data contract (FooterData) and the
Textual widget (FooterWidget) for the session info bar.

Reference: PHASE-4-SUBPHASE-2.md — FooterWidget
Reference: SUBPHASE-0.0.md — FooterData contract
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from textual.widgets import Static

try:
    from textual.widgets import Static

    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


# ---------------------------------------------------------------------------
# FooterData — data contract
# ---------------------------------------------------------------------------


@dataclass
class FooterData:
    """Data for the footer widget.

    Attributes:
        model: Name of the current LLM model
        tokens: Number of tokens used (None if not tracked)
        context_percent: Percentage of context window used (0.0–100.0)
        thinking_level: Current thinking/reasoning level ("off", "low", "high")
        session_name: Human-readable name for the current session
    """

    model: str
    tokens: int | None = None
    context_percent: float | None = None
    thinking_level: str = "off"
    session_name: str | None = None


# ---------------------------------------------------------------------------
# FooterWidget — Textual widget
# ---------------------------------------------------------------------------


class FooterWidget(Static):
    """Widget for the session info bar.

    Shows model name, token count, context usage percentage, and session name.
    Updates dynamically as the agent progresses through a turn.

    Attributes:
        _data: The current FooterData.
        _displayed_text: The current text displayed in the footer.
    """

    CSS = """
    FooterWidget {
        width: 100%;
        height: auto;
        background: $boost;
        color: $text;
        text-align: center;
    }
    """

    def __init__(self, data: FooterData | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._data = data or FooterData(model="unknown")
        self._displayed_text = ""
        self._update_display()

    def _update_display(self) -> None:
        """Build and display the footer text from current data.

        This method builds the display string and calls Static.update()
        to set the widget's rendered content.
        """
        parts = [f"🤖 {self._data.model}"]
        if self._data.tokens is not None:
            parts.append(f"🔢 {self._data.tokens} tokens")
        if self._data.context_percent is not None:
            parts.append(f"📊 {self._data.context_percent}%")
        if self._data.session_name:
            parts.append(f"📝 {self._data.session_name}")
        if self._data.thinking_level and self._data.thinking_level != "off":
            parts.append(f"💡 {self._data.thinking_level}")
        self._displayed_text = " | ".join(parts)
        # Call Static's update() to set the rendered content
        super().update(self._displayed_text)

    def update(self, data: FooterData) -> None:
        """Update the footer with new data.

        Args:
            data: The new FooterData to render.
        """
        self._data = data
        self._update_display()

    @property
    def renderable(self) -> str:
        """Return the currently displayed text."""
        return self._displayed_text

    @property
    def content(self) -> str:
        """Return the currently displayed content (alias for renderable)."""
        return self._displayed_text
