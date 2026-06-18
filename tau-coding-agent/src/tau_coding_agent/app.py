"""ParleyApp stub: TUI application entry point.

This is a fork of the parley.py TUI stub from the τ project.
It defines the main application class that the TUI uses to interact
with τ-agent-core via AgentSession.

Reference: PHASE-4-SUBPHASE-0.md — ParleyApp stub contract
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AppLayout:
    """Layout configuration for the TUI app.

    Attributes:
        width: Terminal width (0 = auto-detect)
        height: Terminal height (0 = auto-detect)
        theme: Theme name for styling
    """

    width: int = 0
    height: int = 0
    theme: str = "default"


@dataclass
class ParleyApp:
    """Stub for the TUI application class.

    This is the main application class that the TUI uses. It wraps
    AgentSession and manages the widget hierarchy. The full implementation
    will use Textual for the UI framework.

    During Phase 4 Subphase 0, this is a stub — just importable with
    the correct signature. The full TUI implementation comes later.

    Attributes:
        session: The AgentSession instance this app controls
        layout: Layout configuration
        ready: Whether the app has been initialized
    """

    session: object | None = None  # AgentSession
    layout: AppLayout = field(default_factory=AppLayout)
    ready: bool = False

    def __init__(self, session=None, layout=None):
        """Initialize the ParleyApp.

        Args:
            session: AgentSession instance to control
            layout: Optional AppLayout configuration
        """
        self.session = session
        self.layout = layout or AppLayout()
        self.ready = False

    async def start(self):
        """Start the TUI application."""
        self.ready = True

    async def stop(self):
        """Stop the TUI application."""
        self.ready = False
        if self.session is not None:
            self.session = None
