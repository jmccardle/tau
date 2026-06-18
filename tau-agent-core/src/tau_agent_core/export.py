"""Export format types for τ-agent-core.

Phase 6 Subphase 0: Finalize the RPC protocol and export types.

These types define how session data is exported in various formats
(markdown, HTML) with configurable inclusion of tool calls, thinking blocks,
and timestamps.

Reference: docs/PHASE-6-SUBPHASE-0.md
Reference: docs/SUBPHASE-0.0.md lines 260-340
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ExportConfig:
    """Configuration for session export.

    Controls what content is included when exporting a session
    to markdown or HTML format.

    Attributes:
        format: Export format. "markdown" for plain text, "html" for HTML.
        include_tool_calls: Whether to include tool call results in the export.
        include_thinking: Whether to include thinking blocks in the export.
        include_timestamps: Whether to include timestamps in the export.
    """
    format: Literal["markdown", "html"]
    include_tool_calls: bool = True
    include_thinking: bool = True
    include_timestamps: bool = False

    def __post_init__(self) -> None:
        """Validate the export configuration after initialization."""
        if self.format not in ("markdown", "html"):
            raise ValueError(
                f"format must be 'markdown' or 'html', got: {self.format!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary.

        Returns:
            A dict with all export configuration fields.
        """
        return {
            "format": self.format,
            "include_tool_calls": self.include_tool_calls,
            "include_thinking": self.include_thinking,
            "include_timestamps": self.include_timestamps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExportConfig:
        """Create an ExportConfig from a dictionary.

        Args:
            data: Dict with export configuration keys.

        Returns:
            A new ExportConfig instance.
        """
        return cls(**data)

    def is_markdown(self) -> bool:
        """Check if this config is for markdown export."""
        return self.format == "markdown"

    def is_html(self) -> bool:
        """Check if this config is for HTML export."""
        return self.format == "html"

    def __repr__(self) -> str:
        """String representation for debugging."""
        return (
            f"ExportConfig(format={self.format!r}, "
            f"include_tool_calls={self.include_tool_calls}, "
            f"include_thinking={self.include_thinking}, "
            f"include_timestamps={self.include_timestamps})"
        )
