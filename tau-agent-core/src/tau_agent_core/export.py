"""Export format types and exporters for τ-agent-core.

Phase 6 Subphase 0: Finalize the RPC protocol and export types.
Phase 6 Subphase 2: Implement session export to markdown and HTML formats.

These types define how session data is exported in various formats
(markdown, HTML) with configurable inclusion of tool calls, thinking blocks,
and timestamps.

Reference: docs/PHASE-6-SUBPHASE-0.md
Reference: docs/PHASE-6-SUBPHASE-2.md
Reference: docs/SUBPHASE-0.0.md lines 260-340
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Message types used by the exporters (from tau_ai.types)
# ---------------------------------------------------------------------------

# The exporters work with plain dicts representing messages, matching the
# tau_ai types.  This avoids a circular dependency between tau-agent-core
# and tau-ai at import time.


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
            raise ValueError(f"format must be 'markdown' or 'html', got: {self.format!r}")

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


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def _extract_text(content: list[dict]) -> str:
    """Extract text from a content list.

    Args:
        content: List of content blocks (dicts with 'type' and 'text' keys).

    Returns:
        All text blocks joined with a space.
    """
    return " ".join(c.get("text", "") for c in content if c.get("type") == "text")


def _format_timestamp(timestamp: int | None) -> str:
    """Format a timestamp (ms epoch) as a human-readable string.

    Args:
        timestamp: Millisecond epoch timestamp, or None.

    Returns:
        Formatted string like '2024-01-15 10:30:45', or empty string.
    """
    if timestamp is None:
        return ""
    try:
        dt = time.localtime(timestamp / 1000)
        return time.strftime("%Y-%m-%d %H:%M:%S", dt)
    except (OSError, OverflowError, ValueError):
        return str(timestamp)


# ---------------------------------------------------------------------------
# Markdown exporter
# ---------------------------------------------------------------------------


class MarkdownExporter:
    """Export session messages to Markdown format.

    Produces a readable Markdown document with sections for
    user, assistant, and (optionally) tool messages.
    """

    def export(self, messages: list[dict], config: ExportConfig) -> str:
        """Export a session's messages to Markdown.

        Args:
            messages: List of message dicts representing the session.
            config: Export configuration controlling what to include.

        Returns:
            Markdown formatted string.
        """
        lines = ["# Session Export", ""]
        for msg in messages:
            lines.append(self._export_message(msg, config))
            lines.append("")
        # Remove trailing blank lines
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    def _export_message(self, msg: dict, config: ExportConfig) -> str:
        """Export a single message to Markdown.

        Args:
            msg: A message dict with 'role' and 'content' keys.
            config: Export configuration.

        Returns:
            Markdown string for the message, or empty string if excluded.
        """
        role = msg.get("role", "")
        content = msg.get("content", [])
        timestamp = msg.get("timestamp")

        parts = []
        if config.include_timestamps and timestamp:
            parts.append(f"_Exported: {_format_timestamp(timestamp)}_")

        if role == "user":
            text = _extract_text(content)
            header = "### User"
            if config.include_timestamps and timestamp:
                header += f" ({_format_timestamp(timestamp)})"
            parts.append(header)
            parts.append("")
            parts.append(text)
            return "\n".join(parts)

        elif role == "assistant":
            assistant_parts = []
            for block in content:
                block_type = block.get("type", "")
                if block_type == "thinking" and config.include_thinking:
                    text = block.get("text", "")
                    assistant_parts.append(f"> 💭 {text}\n")
                elif block_type == "text":
                    assistant_parts.append(block.get("text", ""))
                elif block_type == "toolCall" and config.include_tool_calls:
                    name = block.get("name", "")
                    arguments = block.get("arguments", {})
                    assistant_parts.append(f"\n```tool\n{name}: {json.dumps(arguments)}\n```\n")
            result = "".join(assistant_parts).strip()
            header = "### Assistant"
            if config.include_timestamps and timestamp:
                header += f" ({_format_timestamp(timestamp)})"
            return "\n".join(list(parts) + [header, "", result] if parts else [header, "", result])

        elif role == "toolResult":
            if not config.include_tool_calls:
                return ""
            text = _extract_text(content)
            tool_name = msg.get("tool_name", "unknown")
            header = f"### Tool: {tool_name}"
            if config.include_timestamps and timestamp:
                header += f" ({_format_timestamp(timestamp)})"
            return "\n".join([header, "", "```", text, "```"])

        return ""


# ---------------------------------------------------------------------------
# HTML exporter
# ---------------------------------------------------------------------------


_HTML_STYLES = """\
  .message { margin: 1em 0; padding: 1em; border-radius: 8px; }
  .user { background: #e8f5e9; }
  .assistant { background: #e3f2fd; }
  .tool { background: #fff3e0; }
  pre { background: #f5f5f5; padding: 1em; border-radius: 4px; overflow-x: auto; }
"""


class HTMLExporter:
    """Export session messages to HTML format.

    Produces a styled HTML document with sections for
    user, assistant, and (optionally) tool messages.
    """

    def export(self, messages: list[dict], config: ExportConfig) -> str:
        """Export a session's messages to HTML.

        Args:
            messages: List of message dicts representing the session.
            config: Export configuration controlling what to include.

        Returns:
            HTML formatted string.
        """
        html = [
            "<!DOCTYPE html>",
            "<html>",
            "<head><title>τ Session Export</title>",
            "<style>",
            _HTML_STYLES.rstrip(),
            "</style>",
            "</head>",
            "<body>",
            "<h1>Session Export</h1>",
        ]
        for msg in messages:
            html.append(self._export_message(msg, config))
        html.extend(["</body>", "</html>"])
        return "\n".join(html)

    def _export_message(self, msg: dict, config: ExportConfig) -> str:
        """Export a single message to HTML.

        Args:
            msg: A message dict with 'role' and 'content' keys.
            config: Export configuration.

        Returns:
            HTML string for the message, or empty string if excluded.
        """
        role = msg.get("role", "")
        content = msg.get("content", [])
        timestamp = msg.get("timestamp")

        if role == "toolResult" and not config.include_tool_calls:
            return ""

        if role == "toolResult":
            role_class = "tool"
        elif role == "assistant":
            role_class = "assistant"
        else:
            role_class = role.replace("Result", "Tool")

        content_html = self._extract_html_content(content, config, role)
        timestamp_html = ""
        if config.include_timestamps and timestamp:
            timestamp_html = f'<div class="timestamp">{_format_timestamp(timestamp)}</div>'

        return (
            f'<div class="{role_class}">'
            f"<strong>{role}</strong>\n"
            f"{timestamp_html}\n"
            f"{content_html}\n"
            f"</div>"
        )

    def _extract_html_content(
        self,
        content: list[dict],
        config: ExportConfig,
        role: str,
    ) -> str:
        """Convert content blocks to HTML.

        Args:
            content: List of content block dicts.
            config: Export configuration.
            role: The message role (user, assistant, toolResult).

        Returns:
            HTML string for the content.
        """
        parts = []
        for block in content:
            block_type = block.get("type", "")
            if block_type == "text":
                text = block.get("text", "")
                if text:
                    parts.append(f"<p>{_html_escape(text)}</p>")
            elif block_type == "thinking" and config.include_thinking:
                text = block.get("text", "")
                parts.append(
                    f"<blockquote>"
                    f"<p><strong>💭 Thinking:</strong></p>"
                    f"<p>{_html_escape(text)}</p>"
                    f"</blockquote>"
                )
            elif block_type == "toolCall":
                name = block.get("name", "")
                arguments = block.get("arguments", {})
                args_str = json.dumps(arguments, indent=2)
                parts.append(
                    f"<pre><code>"
                    f"{_html_escape(f'{name}: {json.dumps(arguments)}')}\\n"
                    f"{_html_escape(args_str)}</code></pre>"
                )
        return "\n".join(parts) if parts else ""


def _html_escape(text: str) -> str:
    """Escape HTML special characters.

    Args:
        text: Raw string.

    Returns:
        HTML-safe string.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def export_session(
    messages: list[dict],
    config: ExportConfig | None = None,
) -> str:
    """Export a session's messages to a human-readable string.

    Supports exporting to markdown and HTML formats with configurable
    inclusion of tool calls, thinking blocks, and timestamps.

    Args:
        messages: List of message dicts representing the session.
            Each dict should have 'role' and 'content' keys,
            matching the tau_ai message types.
        config: Export configuration. Defaults to
            ExportConfig(format="markdown") if not provided.

    Returns:
        Formatted string in the requested export format.

    Raises:
        ValueError: If the export format in config is invalid.
    """
    if config is None:
        config = ExportConfig(format="markdown")

    exporters: dict[str, MarkdownExporter | HTMLExporter] = {
        "markdown": MarkdownExporter(),
        "html": HTMLExporter(),
    }
    exporter = exporters.get(config.format)
    if exporter is None:
        raise ValueError(f"Unknown export format: {config.format!r}")
    return exporter.export(messages, config)
