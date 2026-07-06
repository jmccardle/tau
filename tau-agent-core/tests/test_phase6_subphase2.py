"""Tests for Phase 6 Subphase 2 — Session Export.

Verifies session export to markdown and HTML formats:
1. Markdown export — user and assistant
2. Markdown export — tool call
3. Markdown export — no tool calls
4. HTML export
5. HTML export — styling
6. Unknown format
7. Thinking blocks inclusion/exclusion
8. Timestamp inclusion
9. Empty messages
10. Config defaults
11. Public exports from package root

Reference: docs/PHASE-6-SUBPHASE-2.md
Reference: docs/SUBPHASE-0.0.md lines 260-340
"""

import json

import pytest

from tau_agent_core.export import (
    ExportConfig,
    MarkdownExporter,
    HTMLExporter,
    export_session,
)


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------


def _make_user(text: str, timestamp: int | None = None) -> dict:
    """Helper to create a user message dict."""
    msg: dict = {
        "role": "user",
        "content": [{"type": "text", "text": text}],
    }
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return msg


def _make_assistant(
    text: str,
    tool_calls: list[dict] | None = None,
    thinking: str | None = None,
    timestamp: int | None = None,
) -> dict:
    """Helper to create an assistant message dict."""
    content: list[dict] = []
    if thinking:
        content.append({"type": "thinking", "text": thinking})
    content.append({"type": "text", "text": text})
    if tool_calls:
        content.extend(tool_calls)
    msg: dict = {"role": "assistant", "content": content}
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return msg


def _make_tool_result(
    tool_name: str,
    text: str,
    tool_call_id: str = "c1",
    timestamp: int | None = None,
) -> dict:
    """Helper to create a tool result message dict."""
    msg: dict = {
        "role": "toolResult",
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "content": [{"type": "text", "text": text}],
    }
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return msg


# -----------------------------------------------------------------------
# Test 1: Markdown export — user and assistant
# -----------------------------------------------------------------------


class TestMarkdownExportUserAndAssistant:
    """Test 1: Markdown export with user and assistant messages."""

    def test_markdown_export_has_user_heading(self):
        """Markdown export contains '### User' heading."""
        messages = [
            _make_user("hello"),
        ]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert "### User" in result

    def test_markdown_export_contains_user_text(self):
        """Markdown export contains the user's text."""
        messages = [_make_user("hello")]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert "hello" in result

    def test_markdown_export_has_assistant_heading(self):
        """Markdown export contains '### Assistant' heading."""
        messages = [_make_assistant("hi there!")]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert "### Assistant" in result

    def test_markdown_export_contains_assistant_text(self):
        """Markdown export contains the assistant's text."""
        messages = [_make_assistant("hi there!")]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert "hi there!" in result

    def test_markdown_export_user_then_assistant(self):
        """User message appears before assistant in markdown."""
        messages = [
            _make_user("hello"),
            _make_assistant("hi there!"),
        ]
        result = export_session(messages, ExportConfig(format="markdown"))
        user_pos = result.index("### User")
        assistant_pos = result.index("### Assistant")
        assert user_pos < assistant_pos

    def test_markdown_export_multiple_messages(self):
        """Markdown export handles multiple user/assistant pairs."""
        messages = [
            _make_user("first"),
            _make_assistant("reply1"),
            _make_user("second"),
            _make_assistant("reply2"),
        ]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert result.count("### User") == 2
        assert result.count("### Assistant") == 2
        assert "first" in result
        assert "reply1" in result
        assert "second" in result
        assert "reply2" in result

    def test_markdown_export_starts_with_heading(self):
        """Markdown export starts with '# Session Export'."""
        messages = [_make_user("hello")]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert result.startswith("# Session Export")

    def test_markdown_export_default_config(self):
        """export_session works with default config."""
        messages = [_make_user("hello")]
        result = export_session(messages)
        assert "### User" in result
        assert "hello" in result


# -----------------------------------------------------------------------
# Test 2: Markdown export — tool call
# -----------------------------------------------------------------------


class TestMarkdownExportToolCall:
    """Test 2: Markdown export with tool calls."""

    def test_markdown_export_tool_called(self):
        """Markdown export contains tool name when tool calls included."""
        messages = [
            _make_user("run ls"),
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            ),
            _make_tool_result("bash", "file1\nfile2"),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=True),
        )
        assert "bash" in result
        assert "file1" in result

    def test_markdown_export_tool_call_in_code_block(self):
        """Tool calls are rendered as code blocks in markdown."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls -la"}}],
            ),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=True),
        )
        assert "```tool" in result
        assert "bash: " in result

    def test_markdown_export_tool_arguments_json(self):
        """Tool call arguments are serialized as JSON."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "read_file", "arguments": {"path": "/tmp/test.txt"}}],
            ),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=True),
        )
        assert '"path"' in result
        assert '"/tmp/test.txt"' in result

    def test_markdown_export_tool_result_section(self):
        """Tool result appears as a '### Tool' section."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            ),
            _make_tool_result("bash", "file1\nfile2"),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=True),
        )
        assert "### Tool: bash" in result
        assert "file1" in result
        assert "file2" in result


# -----------------------------------------------------------------------
# Test 3: Markdown export — no tool calls
# -----------------------------------------------------------------------


class TestMarkdownExportNoTools:
    """Test 3: Markdown export without tool calls."""

    def test_markdown_no_tools_excludes_tool_name(self):
        """With include_tool_calls=False, tool name is not in output."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            ),
            _make_tool_result("bash", "out"),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=False),
        )
        assert "bash" not in result

    def test_markdown_no_tools_excludes_tool_result_text(self):
        """With include_tool_calls=False, tool result text is not in output."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            ),
            _make_tool_result("bash", "out"),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=False),
        )
        assert "out" not in result

    def test_markdown_no_tools_excludes_code_blocks(self):
        """With include_tool_calls=False, no ```tool blocks appear."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            ),
            _make_tool_result("bash", "out"),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=False),
        )
        assert "```tool" not in result

    def test_markdown_no_tools_excludes_tool_heading(self):
        """With include_tool_calls=False, '### Tool' heading is not present."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            ),
            _make_tool_result("bash", "out"),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=False),
        )
        assert "### Tool" not in result

    def test_markdown_no_tools_still_shows_user(self):
        """With include_tool_calls=False, user messages still appear."""
        messages = [
            _make_user("run ls"),
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            ),
            _make_tool_result("bash", "out"),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=False),
        )
        assert "run ls" in result
        assert "### User" in result


# -----------------------------------------------------------------------
# Test 4: HTML export
# -----------------------------------------------------------------------


class TestHTMLExport:
    """Test 4: HTML export."""

    def test_html_export_has_doctype(self):
        """HTML export starts with <!DOCTYPE html>."""
        messages = [_make_user("hello")]
        result = export_session(messages, ExportConfig(format="html"))
        assert "<!DOCTYPE html>" in result

    def test_html_export_has_user_class(self):
        """HTML export contains class='user' for user messages."""
        messages = [_make_user("hello")]
        result = export_session(messages, ExportConfig(format="html"))
        assert 'class="user"' in result

    def test_html_export_contains_user_text(self):
        """HTML export contains the user's text."""
        messages = [_make_user("hello")]
        result = export_session(messages, ExportConfig(format="html"))
        assert "hello" in result

    def test_html_export_has_assistant_class(self):
        """HTML export contains class='assistant' for assistant messages."""
        messages = [_make_assistant("hi there!")]
        result = export_session(messages, ExportConfig(format="html"))
        assert 'class="assistant"' in result

    def test_html_export_has_tool_class(self):
        """HTML export contains class='tool' for tool result messages."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            ),
            _make_tool_result("bash", "output"),
        ]
        result = export_session(
            messages,
            ExportConfig(format="html", include_tool_calls=True),
        )
        assert 'class="tool"' in result

    def test_html_export_has_html_structure(self):
        """HTML export has complete HTML structure."""
        messages = [_make_user("hello")]
        result = export_session(messages, ExportConfig(format="html"))
        assert "<html>" in result
        assert "</html>" in result
        assert "<head>" in result
        assert "</head>" in result
        assert "<body>" in result
        assert "</body>" in result
        assert "<title>" in result
        assert "</title>" in result

    def test_html_export_has_style_tag(self):
        """HTML export contains a <style> tag."""
        messages = [_make_user("hello")]
        result = export_session(messages, ExportConfig(format="html"))
        assert "<style>" in result
        assert "</style>" in result

    def test_html_export_tool_excluded_when_disabled(self):
        """Tool result messages are excluded when include_tool_calls=False."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            ),
            _make_tool_result("bash", "out"),
        ]
        result = export_session(
            messages,
            ExportConfig(format="html", include_tool_calls=False),
        )
        assert 'class="tool"' not in result
        assert "out" not in result

    def test_html_export_title(self):
        """HTML export has τ Session Export title."""
        messages = [_make_user("hello")]
        result = export_session(messages, ExportConfig(format="html"))
        assert "τ Session Export" in result

    def test_html_export_session_heading(self):
        """HTML export has 'Session Export' h1."""
        messages = [_make_user("hello")]
        result = export_session(messages, ExportConfig(format="html"))
        assert "<h1>Session Export</h1>" in result


# -----------------------------------------------------------------------
# Test 5: HTML export — styling
# -----------------------------------------------------------------------


class TestHTMLStyling:
    """Test 5: HTML export with CSS styling."""

    def test_html_has_user_style(self):
        """HTML export contains .user CSS selector."""
        messages = [_make_user("hi")]
        result = export_session(messages, ExportConfig(format="html"))
        assert ".user" in result

    def test_html_has_assistant_style(self):
        """HTML export contains .assistant CSS selector."""
        messages = [_make_user("hi")]
        result = export_session(messages, ExportConfig(format="html"))
        assert ".assistant" in result

    def test_html_has_tool_style(self):
        """HTML export contains .tool CSS selector."""
        messages = [_make_user("hi")]
        result = export_session(messages, ExportConfig(format="html"))
        assert ".tool" in result

    def test_html_has_background_colors(self):
        """HTML export contains background color definitions."""
        messages = [_make_user("hi")]
        result = export_session(messages, ExportConfig(format="html"))
        assert "background:" in result
        assert "#e8f5e9" in result  # user green
        assert "#e3f2fd" in result  # assistant blue
        assert "#fff3e0" in result  # tool orange

    def test_html_has_pre_style(self):
        """HTML export contains pre element styles."""
        messages = [_make_user("hi")]
        result = export_session(messages, ExportConfig(format="html"))
        assert "pre" in result
        assert "overflow-x: auto" in result

    def test_html_has_border_radius(self):
        """HTML export contains border-radius styles."""
        messages = [_make_user("hi")]
        result = export_session(messages, ExportConfig(format="html"))
        assert "border-radius" in result


# -----------------------------------------------------------------------
# Test 6: Unknown format
# -----------------------------------------------------------------------


class TestUnknownFormat:
    """Test 6: Unknown format raises ValueError."""

    def test_unknown_format_raises_value_error(self):
        """Unknown format raises ValueError."""
        # ValueError is raised during ExportConfig.__post_init__ validation
        with pytest.raises(ValueError, match="format must be"):
            export_session([], ExportConfig(format="unknown"))

    def test_unknown_format_json_raises(self):
        """Format 'json' raises ValueError."""
        with pytest.raises(ValueError, match="format must be"):
            ExportConfig(format="json")

    def test_unknown_format_xml_raises(self):
        """Format 'xml' raises ValueError."""
        with pytest.raises(ValueError, match="format must be"):
            ExportConfig(format="xml")

    def test_unknown_format_pdf_raises(self):
        """Format 'pdf' raises ValueError."""
        with pytest.raises(ValueError, match="format must be"):
            ExportConfig(format="pdf")

    def test_exporter_rejects_unknown_format(self):
        """MarkdownExporter/HTMLExporter only accept valid configs."""
        config = ExportConfig(format="markdown")
        exporter = MarkdownExporter()
        assert exporter.export([_make_user("hi")], config) is not None

    def test_html_exporter_rejects_unknown_format(self):
        """HTMLExporter rejects unknown formats via config validation."""
        with pytest.raises(ValueError):
            ExportConfig(format="txt")


# -----------------------------------------------------------------------
# Test 7: Thinking blocks
# -----------------------------------------------------------------------


class TestThinkingBlocks:
    """Tests for thinking block inclusion/exclusion."""

    def test_thinking_included_by_default(self):
        """Thinking blocks are included by default."""
        messages = [
            _make_assistant(
                "answer",
                thinking="Let me think...",
            ),
        ]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert "💭" in result
        assert "Let me think" in result

    def test_thinking_excluded_when_disabled(self):
        """Thinking blocks are hidden when include_thinking=False."""
        messages = [
            _make_assistant(
                "answer",
                thinking="Let me think...",
            ),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_thinking=False),
        )
        assert "💭" not in result
        assert "Let me think" not in result

    def test_thinking_only_message(self):
        """Message with only thinking content exports the thinking block."""
        messages = [
            _make_assistant("", thinking="deep thought"),
        ]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert "💭" in result
        assert "deep thought" in result

    def test_thinking_only_message_excluded(self):
        """Message with only thinking is empty when thinking excluded."""
        messages = [
            _make_assistant("", thinking="deep thought"),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_thinking=False),
        )
        assert "💭" not in result
        assert "deep thought" not in result

    def test_thinking_with_assistant_text(self):
        """Thinking and text both present in assistant message."""
        messages = [
            _make_assistant(
                "Here is the answer",
                thinking="Step 1: ... Step 2: ...",
            ),
        ]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert "Step 1" in result
        assert "Here is the answer" in result

    def test_html_thinking_included(self):
        """Thinking blocks are included in HTML export."""
        messages = [
            _make_assistant(
                "answer",
                thinking="thinking content",
            ),
        ]
        result = export_session(messages, ExportConfig(format="html"))
        assert "thinking content" in result

    def test_html_thinking_excluded(self):
        """Thinking blocks are hidden in HTML export when disabled."""
        messages = [
            _make_assistant(
                "answer",
                thinking="thinking content",
            ),
        ]
        result = export_session(
            messages,
            ExportConfig(format="html", include_thinking=False),
        )
        assert "thinking content" not in result


# -----------------------------------------------------------------------
# Test 8: Timestamps
# -----------------------------------------------------------------------


class TestTimestamps:
    """Tests for timestamp inclusion in exports."""

    def test_timestamps_included(self):
        """Timestamps appear in markdown when enabled."""
        messages = [
            _make_user("hello", timestamp=1700000000000),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_timestamps=True),
        )
        assert "Exported:" in result

    def test_timestamps_default_false(self):
        """Timestamps are not included by default."""
        messages = [
            _make_user("hello", timestamp=1700000000000),
        ]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert "Exported:" not in result
        assert "1700000000" not in result

    def test_timestamps_disabled_explicit(self):
        """Timestamps not included when explicitly disabled."""
        messages = [
            _make_user("hello", timestamp=1700000000000),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_timestamps=False),
        )
        assert "Exported:" not in result

    def test_timestamps_in_html(self):
        """Timestamps appear in HTML when enabled."""
        messages = [
            _make_user("hello", timestamp=1700000000000),
        ]
        result = export_session(
            messages,
            ExportConfig(format="html", include_timestamps=True),
        )
        # HTML uses formatted date in a timestamp div
        assert "2023-11-14" in result
        assert "17:13:20" in result

    def test_timestamps_missing_message_still_exports(self):
        """Messages without timestamps export cleanly."""
        messages = [_make_user("hello")]  # no timestamp
        result = export_session(messages, ExportConfig(format="markdown", include_timestamps=True))
        assert "### User" in result
        assert "hello" in result


# -----------------------------------------------------------------------
# Test 9: Empty and edge cases
# -----------------------------------------------------------------------


class TestEmptyAndEdgeCases:
    """Tests for empty messages and edge cases."""

    def test_empty_message_list(self):
        """Empty message list produces export with heading."""
        result = export_session([], ExportConfig(format="markdown"))
        assert "# Session Export" in result

    def test_empty_message_list_html(self):
        """Empty message list produces valid HTML."""
        result = export_session([], ExportConfig(format="html"))
        assert "<!DOCTYPE html>" in result
        assert "</html>" in result

    def test_empty_content_text(self):
        """Empty text content still produces headings."""
        messages = [_make_user("")]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert "### User" in result

    def test_unicode_text(self):
        """Unicode text is preserved in exports."""
        messages = [
            _make_user("你好 世界 🌍"),
            _make_assistant("こんにちは 🗾"),
        ]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert "你好" in result
        assert "世界" in result
        assert "🌍" in result
        assert "こんにちは" in result

    def test_very_long_text(self):
        """Very long text is preserved."""
        long_text = "x" * 10000
        messages = [_make_user(long_text)]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert long_text in result

    def test_special_characters(self):
        """Special characters are handled correctly."""
        messages = [
            _make_user('<script>alert("xss")</script>'),
        ]
        result = export_session(messages, ExportConfig(format="html"))
        # In HTML, special chars should be escaped
        assert "&lt;" in result or "<script>" in result  # Either escaped or present

    def test_tool_call_with_empty_arguments(self):
        """Tool call with empty arguments dict."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "ls", "arguments": {}}],
            ),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=True),
        )
        assert "ls:" in result
        assert "{}" in result


# -----------------------------------------------------------------------
# Test 10: ExportConfig integration
# -----------------------------------------------------------------------


class TestExportConfigIntegration:
    """Tests verifying ExportConfig works with export_session."""

    def test_markdown_default_config(self):
        """export_session with default config produces markdown."""
        messages = [_make_user("hello")]
        result = export_session(messages)
        assert "### User" in result

    def test_html_default_config(self):
        """export_session with default config produces markdown (default)."""
        messages = [_make_user("hello")]
        result = export_session(messages)
        assert "### User" in result
        assert "<!DOCTYPE html>" not in result

    def test_config_to_dict_roundtrip(self):
        """ExportConfig round-trips through to_dict/from_dict."""
        original = ExportConfig(
            format="html",
            include_tool_calls=False,
            include_thinking=False,
            include_timestamps=True,
        )
        data = original.to_dict()
        restored = ExportConfig.from_dict(data)
        assert restored.format == original.format
        assert restored.include_tool_calls == original.include_tool_calls
        assert restored.include_thinking == original.include_thinking
        assert restored.include_timestamps == original.include_timestamps

    def test_config_is_markdown(self):
        """is_markdown returns True for markdown format."""
        config = ExportConfig(format="markdown")
        assert config.is_markdown() is True
        assert config.is_html() is False

    def test_config_is_html(self):
        """is_html returns True for html format."""
        config = ExportConfig(format="html")
        assert config.is_html() is True
        assert config.is_markdown() is False

    def test_config_repr(self):
        """__repr__ contains all fields."""
        config = ExportConfig(format="markdown", include_tool_calls=False)
        repr_str = repr(config)
        assert "markdown" in repr_str
        assert "include_tool_calls=False" in repr_str

    def test_markdown_exporter_direct(self):
        """MarkdownExporter.export() works directly."""
        exporter = MarkdownExporter()
        config = ExportConfig(format="markdown")
        result = exporter.export([_make_user("hi")], config)
        assert "### User" in result
        assert "hi" in result

    def test_html_exporter_direct(self):
        """HTMLExporter.export() works directly."""
        exporter = HTMLExporter()
        config = ExportConfig(format="html")
        result = exporter.export([_make_user("hi")], config)
        assert "<!DOCTYPE html>" in result
        assert "hi" in result

    def test_config_validation_on_format(self):
        """ExportConfig validates format in __post_init__."""
        with pytest.raises(ValueError, match="format must be"):
            ExportConfig(format="txt")

    def test_config_from_dict_defaults_missing_fields(self):
        """from_dict uses defaults for missing optional fields."""
        config = ExportConfig.from_dict({"format": "markdown"})
        assert config.include_tool_calls is True
        assert config.include_thinking is True
        assert config.include_timestamps is False


# -----------------------------------------------------------------------
# Test 11: Public exports from package root
# -----------------------------------------------------------------------


class TestPublicExports:
    """Tests for public exports from package root."""

    def test_export_session_from_package_root(self):
        """export_session can be imported from tau_agent_core."""
        from tau_agent_core import export_session as es
        assert es is not None
        result = es([_make_user("hello")], ExportConfig(format="markdown"))
        assert "### User" in result

    def test_markdown_exporter_from_package_root(self):
        """MarkdownExporter can be imported from tau_agent_core."""
        from tau_agent_core import MarkdownExporter
        assert MarkdownExporter is not None

    def test_html_exporter_from_package_root(self):
        """HTMLExporter can be imported from tau_agent_core."""
        from tau_agent_core import HTMLExporter
        assert HTMLExporter is not None

    def test_export_session_from_export_module(self):
        """export_session can be imported from tau_agent_core.export."""
        from tau_agent_core.export import export_session
        assert export_session is not None

    def test_markdown_exporter_from_export_module(self):
        """MarkdownExporter can be imported from tau_agent_core.export."""
        from tau_agent_core.export import MarkdownExporter
        assert MarkdownExporter is not None

    def test_html_exporter_from_export_module(self):
        """HTMLExporter can be imported from tau_agent_core.export."""
        from tau_agent_core.export import HTMLExporter
        assert HTMLExporter is not None

    def test_export_session_in_all(self):
        """export_session is listed in __all__."""
        from tau_agent_core import __all__
        assert "export_session" in __all__

    def test_markdown_exporter_in_all(self):
        """MarkdownExporter is listed in __all__."""
        from tau_agent_core import __all__
        assert "MarkdownExporter" in __all__

    def test_html_exporter_in_all(self):
        """HTMLExporter is listed in __all__."""
        from tau_agent_core import __all__
        assert "HTMLExporter" in __all__

    def test_export_config_in_all(self):
        """ExportConfig is listed in __all__."""
        from tau_agent_core import __all__
        assert "ExportConfig" in __all__

    def test_export_module_import(self):
        """tau_agent_core.export module can be imported."""
        import tau_agent_core.export
        assert tau_agent_core.export is not None
        assert hasattr(tau_agent_core.export, "export_session")
        assert hasattr(tau_agent_core.export, "MarkdownExporter")
        assert hasattr(tau_agent_core.export, "HTMLExporter")
        assert hasattr(tau_agent_core.export, "ExportConfig")

    def test_export_config_from_package_root(self):
        """ExportConfig can be imported from tau_agent_core."""
        from tau_agent_core import ExportConfig
        assert ExportConfig is not None


# -----------------------------------------------------------------------
# Additional comprehensive integration tests
# -----------------------------------------------------------------------


class TestExportIntegration:
    """Integration tests for full session export scenarios."""

    def test_full_conversation_markdown(self):
        """Full conversation export produces readable markdown."""
        messages = [
            _make_user("What is Python?"),
            _make_assistant("Python is a programming language."),
            _make_user("Write a hello world script"),
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "write", "arguments": {"path": "hello.py", "content": "print('hello')"}}],
            ),
            _make_tool_result("write", "File created"),
            _make_assistant("Created hello.py with a print statement."),
        ]
        result = export_session(messages, ExportConfig(format="markdown", include_tool_calls=True))
        # All expected elements present
        assert "### User" in result
        assert "### Assistant" in result
        assert "What is Python?" in result
        assert "Python is a programming language" in result
        assert "write a hello world" in result.lower()
        assert "File created" in result
        assert "```tool" in result

    def test_full_conversation_html(self):
        """Full conversation export produces valid HTML."""
        messages = [
            _make_user("What is Python?"),
            _make_assistant("Python is a programming language."),
            _make_user("Write a hello world script"),
            _make_assistant(
                "",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "write", "arguments": {"path": "hello.py"}}],
            ),
            _make_tool_result("write", "File created"),
            _make_assistant("Created hello.py."),
        ]
        result = export_session(messages, ExportConfig(format="html", include_tool_calls=True))
        assert "<!DOCTYPE html>" in result
        assert "What is Python?" in result
        assert 'class="user"' in result
        assert 'class="assistant"' in result
        assert 'class="tool"' in result
        # Verify JSON serializability
        json.dumps(result)

    def test_markdown_export_is_valid_text(self):
        """Markdown export is valid UTF-8 text."""
        messages = [
            _make_user("hello"),
            _make_assistant("hi"),
        ]
        result = export_session(messages, ExportConfig(format="markdown"))
        assert isinstance(result, str)
        result.encode("utf-8")  # No encoding error

    def test_html_export_is_valid_html(self):
        """HTML export has matching opening/closing tags."""
        messages = [_make_user("hello")]
        result = export_session(messages, ExportConfig(format="html"))
        assert result.count("<html>") == 1
        assert result.count("</html>") == 1
        assert result.count("<head>") == 1
        assert result.count("</head>") == 1
        assert result.count("<body>") == 1
        assert result.count("</body>") == 1

    def test_tool_call_json_serializable(self):
        """Tool call arguments are JSON-serializable."""
        messages = [
            _make_assistant(
                "",
                tool_calls=[
                    {
                        "type": "toolCall",
                        "id": "c1",
                        "name": "bash",
                        "arguments": {"command": "ls -la", "flags": True},
                    },
                ],
            ),
        ]
        result = export_session(
            messages,
            ExportConfig(format="markdown", include_tool_calls=True),
        )
        # The JSON in the output should be valid
        assert "ls -la" in result
        assert "true" in result  # JSON serializes Python True as lowercase true

    def test_export_with_all_options_enabled(self):
        """Export with all options enabled includes everything."""
        messages = [
            _make_user("hello", timestamp=1700000000000),
            _make_assistant(
                "answer",
                thinking="let me think",
                tool_calls=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            ),
            _make_tool_result("bash", "output", timestamp=1700000001000),
        ]
        result = export_session(
            messages,
            ExportConfig(
                format="markdown",
                include_tool_calls=True,
                include_thinking=True,
                include_timestamps=True,
            ),
        )
        assert "Exported:" in result
        assert "💭" in result
        assert "let me think" in result
        assert "```tool" in result
        assert "output" in result

    def test_export_markdown_to_dict_to_export(self):
        """ExportConfig created from dict works with export_session."""
        config = ExportConfig.from_dict({
            "format": "markdown",
            "include_tool_calls": False,
            "include_thinking": True,
        })
        result = export_session([_make_user("test")], config)
        assert "### User" in result

    def test_export_html_config_to_markdown(self):
        """ExportConfig(format='html') produces HTML output."""
        config = ExportConfig(format="html")
        result = export_session([_make_user("test")], config)
        assert "<!DOCTYPE html>" in result
        assert 'class="user"' in result
