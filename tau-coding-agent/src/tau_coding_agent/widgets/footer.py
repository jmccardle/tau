"""FooterData: Data contract for the TUI footer widget.

This dataclass represents the data for the footer widget in the TUI.
It maps from model configuration and session state to widget-renderable data.

Reference: PHASE-4-SUBPHASE-0.md — FooterData Contract
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FooterData:
    """Data for the footer widget.

    Attributes:
        model: Name of the current LLM model
        tokens: Number of tokens used (None if not tracked)
        context_percent: Percentage of context window used (0.0–1.0)
        thinking_level: Current thinking/reasoning level ("off", "low", "high")
        session_name: Human-readable name for the current session
    """

    model: str
    tokens: int | None = None
    context_percent: float | None = None
    thinking_level: str = "off"
    session_name: str | None = None
