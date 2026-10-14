"""Session export: a message list to Markdown or HTML, and a whole tree to HTML.

:func:`render_session_html` is the one that reads a session: every entry of every
branch, the default leaf's path as the main thread and each other branch folded
in place where it forks, written as a single file that loads nothing from the
network. :class:`MarkdownExporter` and :class:`HTMLExporter` take a flat message
list, which is what an SDK caller holding ``session.context`` has.

Reference: docs/HTML-EXPORT.md (why the page is static, how branches are laid out).
Reference: docs/SUBPHASE-0.0.md lines 260-340
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from tau_agent_core.conversation_tree import ConversationTree, TreeNode
from tau_agent_core.session_log import default_leaf, is_incomplete, session_name
from tau_llm.docs import agent_facing


@agent_facing(topic="export")
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


@agent_facing(topic="export")
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
                    text = block.get("thinking", "")
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


_COLLAPSE_LINES = 24
"""A tool result longer than this many lines starts folded."""

_BRANCH_PREVIEW_CHARS = 80
"""How much of a branch's first entry its fold label shows."""

_META_CUSTOM_TYPES = ("config", "agent_spec")
"""``customEntry`` kinds whose one-line preview says everything they hold."""

_META_KINDS = ("session_info", "model_change", "thinking_change", "navigate")
"""Bookkeeping kinds rendered as one line from :class:`TreeNode`'s preview."""

_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'"
)
"""The page's Content-Security-Policy: no script, no fetch, no ``<base>``, no form submit."""

_IMAGE_MIME = re.compile(r"image/[a-z0-9][a-z0-9.+-]*", re.IGNORECASE)
"""A ``mime_type`` safe to put in a ``data:`` URI: ``image/`` and a bare subtype."""

_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")
"""Standard base64 without whitespace, the form :class:`~tau_llm.types.ImageContent` stores."""

_HTML_STYLES = """\
:root {
  --bg: #fbfaf7; --fg: #1f2328; --muted: #6a737d; --rule: #d8d4cc;
  --card: #ffffff; --code: #f3f1ec; --user: #e9f3ea; --assistant: #eaf1fb;
  --tool: #fbf3e6; --error: #fbe9e9; --accent: #7a4fd0; --summary: #f1ecfa;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17181b; --fg: #e3e3e0; --muted: #9a9ea6; --rule: #34363c;
    --card: #1e2024; --code: #25272c; --user: #1d2a20; --assistant: #1c2533;
    --tool: #2c251a; --error: #3a1f22; --accent: #b39af0; --summary: #262033;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0 auto; max-width: 960px; padding: 24px 16px 64px;
  background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
h1 { font-size: 1.4em; margin: 0 0 8px; overflow-wrap: anywhere; }
.session-meta { color: var(--muted); font-size: 0.85em; margin: 0 0 24px; }
.session-meta dt { float: left; clear: left; width: 7em; }
.session-meta dd { margin: 0 0 0 7em; overflow-wrap: anywhere; }
.entry {
  margin: 10px 0; padding: 10px 14px; border-radius: 8px;
  background: var(--card); border: 1px solid var(--rule);
}
.entry > header {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline;
  font-size: 0.8em; color: var(--muted); margin-bottom: 4px;
}
.entry > header .role { font-weight: 600; color: var(--fg); }
.entry > header time { margin-left: auto; }
.user { background: var(--user); }
.assistant { background: var(--assistant); }
.tool { background: var(--tool); }
.tool.error { background: var(--error); }
.summary-card { background: var(--summary); }
.meta {
  margin: 4px 0; padding: 2px 14px; font-size: 0.8em; color: var(--muted);
  border-left: 2px solid var(--rule);
}
.badge {
  border: 1px solid currentColor; border-radius: 4px; padding: 0 4px; font-size: 0.9em;
}
.badge.leaf { color: var(--accent); }
.text { white-space: pre-wrap; overflow-wrap: anywhere; }
pre {
  background: var(--code); padding: 8px 10px; border-radius: 4px; margin: 6px 0;
  overflow-x: auto; font: 13px/1.4 ui-monospace, "SFMono-Regular", Menlo, monospace;
}
.call .name { font-weight: 600; font-family: ui-monospace, monospace; }
.call .id, .tool .id { color: var(--muted); font-size: 0.85em; }
details > summary { cursor: pointer; color: var(--muted); font-size: 0.85em; }
details.thinking .text { color: var(--muted); font-style: italic; }
details.branch {
  margin: 10px 0 10px 12px; padding-left: 12px; border-left: 2px dashed var(--accent);
}
details.branch > summary { color: var(--accent); }
img { max-width: 100%; height: auto; border-radius: 4px; display: block; margin: 6px 0; }
.notice { color: var(--muted); font-style: italic; }
"""


def _esc(value: object) -> str:
    """``value`` as text that HTML reads back verbatim, quotes included."""
    return html.escape(str(value), quote=True)


def _raw_html(label: str, payload: object) -> str:
    """A folded JSON dump of something no rule here knows how to draw.

    Lossless rather than skipped: a kind this module has never heard of still
    reaches the reader, labelled for what it is.
    """
    dumped = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    return (
        f'<details class="raw"><summary>{_esc(label)}</summary><pre>{_esc(dumped)}</pre></details>'
    )


def _blocks(content: Any) -> list[Any]:
    """A message's ``content`` as a block list.

    A bare string is one text block and ``None`` is none; anything else is one
    block, so a malformed ``content`` is drawn raw rather than dropped.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return [] if content is None else [content]


def _image_html(block: Mapping[str, Any]) -> str:
    """An ``image`` block as an inline ``data:`` image, so the page fetches nothing.

    One whose ``mime_type`` is not ``image/<subtype>`` or whose ``data`` is not
    base64 is reported in a line instead of embedded.
    """
    mime = block.get("mime_type")
    data = block.get("data")
    if not (isinstance(mime, str) and _IMAGE_MIME.fullmatch(mime)):
        return f'<p class="notice">image block τ cannot draw (mime_type={_esc(repr(mime))})</p>'
    if not (isinstance(data, str) and _BASE64.fullmatch(data)):
        return f'<p class="notice">{_esc(mime)} image block τ cannot draw (data is not base64)</p>'
    return f'<img alt="{_esc(mime)} image" src="data:{_esc(mime)};base64,{_esc(data)}">'


def _block_html(block: Any, config: ExportConfig) -> str:
    """One content block as HTML, or ``""`` when ``config`` excludes its kind."""
    if not isinstance(block, Mapping):
        return _raw_html("content block that is not an object", block)
    kind = block.get("type")
    if kind == "text":
        text = str(block.get("text", ""))
        return f'<div class="text">{_esc(text)}</div>' if text else ""
    if kind == "thinking":
        if not config.include_thinking:
            return ""
        text = str(block.get("thinking", ""))
        return (
            f'<details class="thinking"><summary>💭 reasoning · {len(text):,} chars</summary>'
            f'<div class="text">{_esc(text)}</div></details>'
        )
    if kind == "toolCall":
        if not config.include_tool_calls:
            return ""
        arguments = json.dumps(block.get("arguments", {}), indent=2, ensure_ascii=False)
        return (
            f'<div class="call"><span class="name">{_esc(block.get("name", "?"))}</span> '
            f'<span class="id">{_esc(block.get("id", ""))}</span>'
            f"<pre>{_esc(arguments)}</pre></div>"
        )
    if kind == "image":
        return _image_html(block)
    return _raw_html(f"{kind} block", block)


def _tool_output_html(block: Any, config: ExportConfig) -> str:
    """A tool result's text block as preformatted output, folded when it is long."""
    if not (isinstance(block, Mapping) and block.get("type") == "text"):
        return _block_html(block, config)
    text = str(block.get("text", ""))
    lines = text.count("\n") + 1
    pre = f"<pre>{_esc(text)}</pre>"
    if lines <= _COLLAPSE_LINES:
        return pre
    return f"<details><summary>output · {lines:,} lines</summary>{pre}</details>"


def _assistant_meta(message: Mapping[str, Any]) -> str:
    """The assistant line a reader checks first: model, how it stopped, any error."""
    parts = [str(message["model"])] if message.get("model") else []
    stop = message.get("stop_reason")
    if stop and stop not in ("stop", "toolUse"):
        parts.append(f"stop: {stop}")
    usage = message.get("usage")
    if isinstance(usage, Mapping) and (usage.get("input_tokens") or usage.get("output_tokens")):
        parts.append(f"in {usage.get('input_tokens', 0):,} / out {usage.get('output_tokens', 0):,}")
    return " · ".join(_esc(p) for p in parts)


def _message_html(
    message: Mapping[str, Any],
    config: ExportConfig,
    *,
    anchor: str | None = None,
    stamp: str = "",
    badges: Sequence[str] = (),
) -> str:
    """One message as a ``<section>``, or ``""`` when ``config`` leaves nothing of it.

    Args:
        message: A message dict in ``tau_llm`` shape (snake_case fields).
        config: What to include.
        anchor: The section's ``id``, so an entry can be linked to.
        stamp: Already-formatted time to show, or ``""``.
        badges: Already-escaped badge HTML for the header.
    """
    role = message.get("role")
    blocks = _blocks(message.get("content"))
    extra = ""
    if role == "toolResult":
        if not config.include_tool_calls:
            return ""
        error = bool(message.get("is_error"))
        css = "tool error" if error else "tool"
        label = f"Tool result · {_esc(message.get('tool_name', '?'))}"
        label += f' <span class="id">{_esc(message.get("tool_call_id", ""))}</span>'
        if error:
            label += ' <span class="badge">error</span>'
        body = "".join(_tool_output_html(b, config) for b in blocks)
    elif role == "system":
        is_text = [isinstance(b, Mapping) and b.get("type") == "text" for b in blocks]
        text = "".join(str(b.get("text", "")) for b, t in zip(blocks, is_text, strict=True) if t)
        css, label = "system", "System prompt"
        body = (
            f"<details><summary>{len(text):,} chars</summary>"
            f'<div class="text">{_esc(text)}</div></details>'
        )
        body += "".join(
            _block_html(b, config) for b, t in zip(blocks, is_text, strict=True) if not t
        )
    else:
        if role == "user":
            css, label = "user", "User"
        elif role == "assistant":
            css, label = "assistant", "Assistant"
            extra = _assistant_meta(message)
        elif role == "custom":
            css = "custom"
            label = f"Extension · {_esc(message.get('customType') or 'custom')}"
            if message.get("display", True) is False:
                label += ' <span class="badge">hidden from transcript</span>'
        else:
            css, label = "unknown", f"{_esc(role)} message"
        body = "".join(_block_html(b, config) for b in blocks)
        if role == "assistant" and message.get("error_message"):
            body += f'<p class="notice">error: {_esc(message["error_message"])}</p>'
        dropped_calls = any(isinstance(b, Mapping) and b.get("type") == "toolCall" for b in blocks)
        if role == "assistant" and not body and dropped_calls and not config.include_tool_calls:
            return ""
    id_attr = f' id="{_esc(anchor)}"' if anchor else ""
    head = [f'<span class="role">{label}</span>', *badges]
    if extra:
        head.append(f"<span>{extra}</span>")
    if stamp:
        head.append(f"<time>{_esc(stamp)}</time>")
    return f'<section class="entry {css}"{id_attr}><header>{"".join(head)}</header>{body}</section>'


def _page(title: str, preamble: str, body: Sequence[str]) -> str:
    """The document shell: inline stylesheet, a CSP that forbids every fetch, no script."""
    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="color-scheme" content="light dark">',
            f"<title>{_esc(title)}</title>",
            "<style>",
            _HTML_STYLES.rstrip(),
            "</style>",
            "</head>",
            "<body>",
            f"<h1>{_esc(title)}</h1>",
            preamble,
            *body,
            "</body>",
            "</html>",
        ]
    )


@agent_facing(topic="export")
class HTMLExporter:
    """Export a flat message list as one self-contained HTML page.

    The flat counterpart of :func:`render_session_html`, for a caller holding
    messages rather than a session's entries; both draw a message the same way.
    """

    def export(self, messages: list[dict], config: ExportConfig) -> str:
        """Render ``messages`` in order.

        Args:
            messages: Message dicts in ``tau_llm`` shape.
            config: What to include. ``include_timestamps`` shows each message's
                epoch-ms ``timestamp`` in local time.

        Returns:
            The HTML document.
        """
        sections = []
        for message in messages:
            stamp = ""
            if config.include_timestamps and message.get("timestamp"):
                stamp = _format_timestamp(message["timestamp"])
            sections.append(_message_html(message, config, stamp=stamp))
        return _page("τ session export", "", [s for s in sections if s])


def _summary_html(entry: Mapping[str, Any], node: TreeNode, head: str) -> str:
    """A ``compaction``/``branch_summary``/``elide`` as a card naming what it covers."""
    kind = entry.get("type")
    label = {"compaction": "Compaction", "branch_summary": "Branch summary"}.get(str(kind), "Elide")
    summary = entry.get("summary")
    body = f'<div class="text">{_esc(summary)}</div>' if summary else ""
    return (
        f'<section class="entry summary-card" id="e-{_esc(entry["id"])}">'
        f'<header><span class="role">{label}</span><span>{_esc(node.preview)}</span>{head}</header>'
        f"{body}</section>"
    )


def _entry_html(entry: dict[str, Any], node: TreeNode, config: ExportConfig, is_leaf: bool) -> str:
    """One tree entry as HTML, by kind; an unknown kind is drawn raw, never dropped."""
    badges = []
    if is_incomplete(entry):
        badges.append('<span class="badge">interrupted</span>')
    if is_leaf:
        badges.append('<span class="badge leaf">session continues here</span>')
    stamp = str(entry.get("timestamp", "")) if config.include_timestamps else ""
    head = "".join(badges) + (f"<time>{_esc(stamp)}</time>" if stamp else "")
    kind = entry.get("type")
    anchor = f"e-{entry['id']}"
    if kind in ("message", "customMessage"):
        message = entry.get("message")
        if not isinstance(message, Mapping):
            return (
                f'<div class="meta" id="{_esc(anchor)}">{_raw_html(f"{kind} entry", entry)}</div>'
            )
        return _message_html(message, config, anchor=anchor, stamp=stamp, badges=badges)
    if kind in ("compaction", "branch_summary", "elide"):
        return _summary_html(entry, node, head)
    line = f"{_esc(kind)} · {_esc(node.preview)}" if node.preview else _esc(kind)
    raw = ""
    if kind == "customEntry" and entry.get("customType") not in _META_CUSTOM_TYPES:
        raw = _raw_html("data", entry.get("data"))
    elif kind != "customEntry" and kind not in _META_KINDS:
        raw = _raw_html("entry", entry)
    return f'<div class="meta" id="{_esc(anchor)}">{line} {head}{raw}</div>'


def _branch_label(node: TreeNode, size: int) -> str:
    """The fold label of a side branch: how big it is and how it starts."""
    preview = node.preview or f"({node.kind})"
    if len(preview) > _BRANCH_PREVIEW_CHARS:
        preview = preview[: _BRANCH_PREVIEW_CHARS - 1] + "…"
    noun = "entry" if size == 1 else "entries"
    return f"⑂ branch · {size:,} {noun} · {_esc(preview)}"


def _session_meta(
    header: Mapping[str, Any] | None, entry_count: int, tips: int, leaf: str | None
) -> str:
    """The definition list under the title: identity from the header, shape from the tree."""
    rows: list[tuple[str, object]] = []
    if header is not None:
        for key, label in (("id", "session"), ("cwd", "cwd"), ("timestamp", "created")):
            if header.get(key):
                rows.append((label, header[key]))
        if header.get("parent"):
            rows.append(("forked from", header["parent"]))
    rows.append(("entries", f"{entry_count:,} in {tips:,} branch{'es' if tips != 1 else ''}"))
    if leaf is not None:
        rows.append(("main thread", f"ends at {leaf}"))
    items = "".join(f"<dt>{_esc(k)}</dt><dd>{_esc(v)}</dd>" for k, v in rows)
    return f'<dl class="session-meta">{items}</dl>'


@agent_facing(topic="export", since="0.13.0")
def render_session_html(
    entries: list[dict[str, Any]],
    *,
    header: Mapping[str, Any] | None = None,
    leaf: str | None = None,
    config: ExportConfig | None = None,
) -> str:
    """A whole session tree as one HTML page that loads nothing from the network.

    Every entry appears once. The path to ``leaf`` is the main thread; at each
    fork the other children fold in place as ``<details>`` branches, each laid out
    by the same rule with its newest-written child continuing. Images are inline
    ``data:`` URIs, there is no script, and a Content-Security-Policy forbids
    every fetch. Message text is preformatted, not rendered Markdown.

    Args:
        entries: A log's ``entries()``, in append order.
        header: The session header (``ConversationSession.header``); ``None``
            omits the identity lines, which an ``InMemorySessionLog`` has none of.
        leaf: The entry whose path is the main thread; ``None`` is
            :func:`~tau_agent_core.session_log.default_leaf`, where a reopened
            session continues.
        config: What to include; defaults to ``ExportConfig(format="html")``.

    Returns:
        The HTML document.

    Raises:
        ValueError: ``config`` is not an HTML config, or ``leaf`` names no entry.

    See docs/HTML-EXPORT.md.
    """
    config = config if config is not None else ExportConfig(format="html")
    if not config.is_html():
        raise ValueError(f"render_session_html needs an html ExportConfig, got {config.format!r}")
    order = {str(e["id"]): index for index, e in enumerate(entries)}
    if leaf is not None and leaf not in order:
        raise ValueError(f"leaf {leaf!r} names no entry in this session")
    main_leaf = leaf if leaf is not None else default_leaf(entries)
    tree = ConversationTree(entries, main_leaf)
    roots = tree.tree()
    trunk = {str(e["id"]) for e in tree.path()} if main_leaf is not None else set()

    preorder: list[TreeNode] = []
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        preorder.append(node)
        stack.extend(reversed(node.children))
    newest: dict[str, int] = {}
    size: dict[str, int] = {}
    for node in reversed(preorder):
        newest[node.id] = max([order[node.id], *(newest[c.id] for c in node.children)])
        size[node.id] = 1 + sum(size[c.id] for c in node.children)

    def split(children: list[TreeNode], at_leaf: bool) -> tuple[TreeNode | None, list[TreeNode]]:
        """Which child continues this thread inline, and which fold as branches."""
        if at_leaf or not children:
            return None, list(children)
        on_trunk = [c for c in children if c.id in trunk]
        cont = on_trunk[0] if on_trunk else max(children, key=lambda c: newest[c.id])
        return cont, [c for c in children if c is not cont]

    body: list[str] = []
    work: list[str | TreeNode] = []

    def schedule(cont: TreeNode | None, sides: list[TreeNode]) -> None:
        """Push the continuation, then each side branch, so they pop in reading order."""
        if cont is not None:
            work.append(cont)
        for side in reversed(sides):
            work.append("</details>")
            work.append(side)
            work.append(
                f'<details class="branch"><summary>{_branch_label(side, size[side.id])}</summary>'
            )

    schedule(*split(roots, False))
    while work:
        item = work.pop()
        if isinstance(item, str):
            body.append(item)
            continue
        is_leaf = item.id == main_leaf
        rendered = _entry_html(tree.entry(item.id), item, config, is_leaf)
        if rendered:
            body.append(rendered)
        schedule(*split(item.children, is_leaf))

    tips = sum(1 for node in preorder if not node.children)
    title = session_name(entries) or (
        f"τ session {header['id']}" if header is not None and header.get("id") else "τ session"
    )
    return _page(title, _session_meta(header, len(entries), tips, main_leaf), body)


@agent_facing(topic="export")
def export_session(
    messages: list[dict],
    config: ExportConfig | None = None,
) -> str:
    """Export a flat message list as Markdown or HTML.

    For a whole session tree, use :func:`render_session_html`.

    Args:
        messages: Message dicts in ``tau_llm`` shape (``role``, ``content``).
        config: What to export; defaults to ``ExportConfig(format="markdown")``.

    Returns:
        The document in the requested format.

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
