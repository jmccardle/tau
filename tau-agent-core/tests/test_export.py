"""Session export to Markdown and HTML — ``tau_agent_core.export``.

This is the only suite that covers ``export.py``. It was previously named
``test_phase6_subphase2.py``, which read as spec-era archaeology and hid that
fact; the module is at 96% coverage and every one of those statements comes from
here.

Consolidated from 88 hand-written tests to 24 test functions (42 parametrized
cases), at identical coverage — 139 statements, the same 6 unreached. The old
shape was one
assertion per test over a shared export call — eight tests to establish that a
user/assistant exchange renders (heading, text, heading, text, order, count,
prefix, default config), six to assert individual CSS substrings, twelve to
assert that imports work. Those describe one behaviour each, not eighty-eight,
and splitting them meant a change to the export format lit up a screen of
failures that all said the same thing.

Two assertions were repaired rather than carried over:

* HTML escaping was tested as ``assert "&lt;" in result or "<script>" in result``
  — true whether or not escaping happens, so the one security-relevant behaviour
  in this module had no test at all. It is now asserted strictly.
* The HTML timestamp test asserted the literal ``"17:13:20"``, which is
  ``time.localtime`` output and therefore passes only in the author's timezone.
  It now asserts the rendered *shape*.

Reference: docs/PHASE-6-SUBPHASE-2.md (the original spec); export.py.
"""

from __future__ import annotations

import json
import re

import pytest

from tau_agent_core.export import (
    ExportConfig,
    HTMLExporter,
    MarkdownExporter,
    export_session,
)

TIMESTAMP = 1700000000000
TIMESTAMP_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _user(text: str, timestamp: int | None = None) -> dict:
    msg: dict = {"role": "user", "content": [{"type": "text", "text": text}]}
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return msg


def _assistant(
    text: str,
    tool_calls: list[dict] | None = None,
    thinking: str | None = None,
    timestamp: int | None = None,
) -> dict:
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


def _tool_result(tool_name: str, text: str, timestamp: int | None = None) -> dict:
    msg: dict = {
        "role": "toolResult",
        "tool_call_id": "c1",
        "tool_name": tool_name,
        "content": [{"type": "text", "text": text}],
    }
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return msg


def _tool_call(name: str, arguments: dict) -> dict:
    return {"type": "toolCall", "id": "c1", "name": name, "arguments": arguments}


CONVERSATION = [
    _user("What is Python?"),
    _assistant("Python is a programming language."),
    _user("Write a hello world script"),
    _assistant("", tool_calls=[_tool_call("write", {"path": "hello.py", "content": "x"})]),
    _tool_result("write", "File created"),
    _assistant("Created hello.py."),
]


# ── markdown ────────────────────────────────────────────────────────────────


def test_markdown_renders_an_exchange_in_order():
    """One test for what eight used to say: roles, text, order, and repetition."""
    result = export_session(
        [_user("first"), _assistant("reply1"), _user("second"), _assistant("reply2")],
        ExportConfig(format="markdown"),
    )
    assert result.startswith("# Session Export")
    assert result.count("### User") == 2
    assert result.count("### Assistant") == 2
    assert result.index("### User") < result.index("### Assistant")
    for text in ("first", "reply1", "second", "reply2"):
        assert text in result


def test_markdown_is_the_default_format():
    result = export_session([_user("hello")])
    assert "### User" in result
    assert "<!DOCTYPE html>" not in result


def test_markdown_renders_a_tool_call_and_its_result():
    result = export_session(
        [
            _assistant("", tool_calls=[_tool_call("bash", {"command": "ls -la", "flags": True})]),
            _tool_result("bash", "file1\nfile2"),
        ],
        ExportConfig(format="markdown", include_tool_calls=True),
    )
    assert "```tool" in result
    assert "bash: " in result
    assert '"command"' in result
    assert "ls -la" in result
    assert "true" in result  # arguments are JSON, so Python True lowercases
    assert "### Tool: bash" in result
    assert "file1" in result and "file2" in result


def test_a_tool_call_with_no_arguments_still_renders():
    result = export_session(
        [_assistant("", tool_calls=[_tool_call("ls", {})])],
        ExportConfig(format="markdown", include_tool_calls=True),
    )
    assert "ls: " in result


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_include_tool_calls_false_omits_the_call_and_its_result(fmt):
    """Excluding tool calls must drop the *whole* tool region, not just the call."""
    result = export_session(
        [
            _user("run ls"),
            _assistant("", tool_calls=[_tool_call("bash", {"command": "ls"})]),
            _tool_result("bash", "secret output"),
        ],
        ExportConfig(format=fmt, include_tool_calls=False),
    )
    assert "bash" not in result
    assert "secret output" not in result
    assert "```tool" not in result
    assert "run ls" in result  # the rest of the conversation survives


# ── html ────────────────────────────────────────────────────────────────────


def test_html_renders_a_well_formed_document():
    result = export_session(CONVERSATION, ExportConfig(format="html"))
    assert "<!DOCTYPE html>" in result
    for tag in ("html", "head", "body"):
        assert result.count(f"<{tag}>") == 1
        assert result.count(f"</{tag}>") == 1
    assert "<style>" in result
    assert "Session Export" in result
    for role in ("user", "assistant", "tool"):
        assert f'class="{role}"' in result
    assert "What is Python?" in result
    json.dumps(result)  # the export is transportable as JSON


def test_html_carries_its_own_stylesheet():
    """Self-contained output: no external CSS, so the rules must be inline.

    Asserted as a set rather than one test per selector — the point is that the
    document styles every role it emits, not that any single rule exists.
    """
    result = export_session([_user("hi")], ExportConfig(format="html"))
    for rule in (".user", ".assistant", ".tool", "background", "pre", "border-radius"):
        assert rule in result


def test_html_escapes_user_supplied_markup():
    """The one security-relevant behaviour in this module.

    The predecessor asserted ``"&lt;" in result or "<script>" in result``, which is
    true whether escaping happens or not — so this was untested until now.
    """
    result = export_session(
        [_user('<script>alert("xss")</script>')],
        ExportConfig(format="html"),
    )
    assert "<script>" not in result
    assert "&lt;script&gt;" in result
    assert "&quot;xss&quot;" in result


# ── format validation ───────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["json", "xml", "pdf", "txt", "unknown", ""])
def test_only_markdown_and_html_are_accepted(fmt):
    """Fail-Early at construction — never silently fall back to a default format."""
    with pytest.raises(ValueError, match="format must be"):
        ExportConfig(format=fmt)


# ── thinking blocks ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["markdown", "html"])
@pytest.mark.parametrize("text", ["answer", ""])
def test_thinking_is_included_by_default(fmt, text):
    """Also covers the thinking-only message (``text=""``), which has no text block."""
    result = export_session(
        [_assistant(text, thinking="let me think")],
        ExportConfig(format=fmt),
    )
    assert "💭" in result
    assert "let me think" in result


@pytest.mark.parametrize("fmt", ["markdown", "html"])
@pytest.mark.parametrize("text", ["answer", ""])
def test_thinking_is_dropped_when_disabled(fmt, text):
    result = export_session(
        [_assistant(text, thinking="let me think")],
        ExportConfig(format=fmt, include_thinking=False),
    )
    assert "💭" not in result
    assert "let me think" not in result


# ── timestamps ──────────────────────────────────────────────────────────────


def test_timestamps_are_rendered_when_enabled():
    result = export_session(
        [_user("hello", timestamp=TIMESTAMP)],
        ExportConfig(format="markdown", include_timestamps=True),
    )
    assert "Exported:" in result


def test_html_timestamps_are_rendered_in_local_time():
    """Asserted by shape, not by literal.

    The predecessor asserted ``"17:13:20"``, but ``_format_timestamp`` uses
    ``time.localtime``, so that test only passed in the timezone it was written in.
    """
    result = export_session(
        [_user("hello", timestamp=TIMESTAMP)],
        ExportConfig(format="html", include_timestamps=True),
    )
    assert TIMESTAMP_SHAPE.search(result)


@pytest.mark.parametrize("config", [{}, {"include_timestamps": False}])
def test_timestamps_are_off_by_default_and_when_disabled(config):
    result = export_session(
        [_user("hello", timestamp=TIMESTAMP)],
        ExportConfig(format="markdown", **config),
    )
    assert "Exported:" not in result
    assert str(TIMESTAMP) not in result


def test_a_message_without_a_timestamp_exports_cleanly():
    """Timestamps enabled but absent must not blank the message or raise."""
    result = export_session(
        [_user("hello")],
        ExportConfig(format="markdown", include_timestamps=True),
    )
    assert "### User" in result
    assert "hello" in result


# ── edge cases ──────────────────────────────────────────────────────────────


def test_an_empty_session_still_produces_a_document():
    assert "# Session Export" in export_session([], ExportConfig(format="markdown"))
    html = export_session([], ExportConfig(format="html"))
    assert "<!DOCTYPE html>" in html and "</html>" in html


def test_empty_unicode_and_very_long_text_survive_the_round_trip():
    long_text = "x" * 10000
    result = export_session(
        [_user(""), _user("你好 世界 🌍"), _user("こんにちは"), _user(long_text)],
        ExportConfig(format="markdown"),
    )
    assert result.count("### User") == 4  # the empty message still gets a heading
    for text in ("你好", "世界", "🌍", "こんにちは", long_text):
        assert text in result
    result.encode("utf-8")


# ── ExportConfig ────────────────────────────────────────────────────────────


def test_config_round_trips_through_to_dict_and_from_dict():
    original = ExportConfig(
        format="html",
        include_tool_calls=False,
        include_thinking=False,
        include_timestamps=True,
    )
    assert ExportConfig.from_dict(original.to_dict()) == original


def test_from_dict_fills_in_the_defaults():
    config = ExportConfig.from_dict({"format": "markdown"})
    assert (config.include_tool_calls, config.include_thinking, config.include_timestamps) == (
        True,
        True,
        False,
    )


@pytest.mark.parametrize(
    "fmt,is_markdown,is_html", [("markdown", True, False), ("html", False, True)]
)
def test_the_format_predicates_agree_with_the_format(fmt, is_markdown, is_html):
    config = ExportConfig(format=fmt)
    assert config.is_markdown() is is_markdown
    assert config.is_html() is is_html


def test_repr_shows_the_settings():
    """The config is user-facing (it comes from ~/.tau config), so repr must read."""
    text = repr(ExportConfig(format="markdown", include_tool_calls=False))
    assert "markdown" in text
    assert "include_tool_calls=False" in text


# ── public surface ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["export_session", "MarkdownExporter", "HTMLExporter", "ExportConfig"]
)
def test_the_export_api_is_public_from_the_package_root(name):
    """Twelve import-smoke tests collapsed into the property they were checking."""
    import tau_agent_core
    import tau_agent_core.export

    assert name in tau_agent_core.__all__
    assert getattr(tau_agent_core, name) is getattr(tau_agent_core.export, name)


@pytest.mark.parametrize(
    "exporter,marker", [(MarkdownExporter, "### User"), (HTMLExporter, "<!DOCTYPE html>")]
)
def test_the_exporters_can_be_driven_directly(exporter, marker):
    """``export_session`` is a convenience; the exporter classes are public too."""
    fmt = "markdown" if exporter is MarkdownExporter else "html"
    result = exporter().export([_user("hi")], ExportConfig(format=fmt))
    assert marker in result
    assert "hi" in result


# ── integration ─────────────────────────────────────────────────────────────


def test_everything_enabled_renders_every_region():
    result = export_session(
        [
            _user("hello", timestamp=TIMESTAMP),
            _assistant(
                "answer",
                thinking="let me think",
                tool_calls=[_tool_call("bash", {"command": "ls"})],
            ),
            _tool_result("bash", "output", timestamp=TIMESTAMP + 1000),
        ],
        ExportConfig(
            format="markdown",
            include_tool_calls=True,
            include_thinking=True,
            include_timestamps=True,
        ),
    )
    for marker in ("Exported:", "💭", "let me think", "```tool", "output"):
        assert marker in result
