"""τ-agent-core compaction utilities — file-op tracking + conversation serialization.

Faithful port of pi's ``packages/agent/src/harness/compaction/utils.ts``, adapted
to τ's message representation: pi operates on typed ``AgentMessage`` objects;
τ carries messages as plain dicts (``{"role": ..., "content": ...}``) on the
active path, so the helpers here read dict fields instead of class attributes.

Reference: pi packages/agent/src/harness/compaction/utils.ts
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Tool-result text longer than this is truncated in the summarization transcript
# so a single huge tool output cannot dominate the prompt (pi: utils.ts:74).
TOOL_RESULT_MAX_CHARS = 2000


@dataclass
class FileOperations:
    """File paths touched across a compaction range.

    Mirrors pi's ``FileOperations`` (utils.ts:5): three disjoint-by-intent sets
    that ``compute_file_lists`` later collapses into read-only vs modified.
    """

    read: set[str] = field(default_factory=set)
    written: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)


def create_file_ops() -> FileOperations:
    """Create an empty file-operation accumulator (pi: createFileOps)."""
    return FileOperations()


def extract_file_ops_from_message(message: dict[str, Any], file_ops: FileOperations) -> None:
    """Record file paths from an assistant message's tool calls.

    Only assistant messages carry tool calls; everything else is ignored. A
    ``read``/``write``/``edit`` tool call with a string ``path`` argument adds
    that path to the matching set. Mirrors pi's extractFileOpsFromMessage
    (utils.ts:24).
    """
    if message.get("role") != "assistant":
        return
    content = message.get("content")
    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "toolCall":
            continue
        args = block.get("arguments")
        if not isinstance(args, dict):
            continue
        path = args.get("path")
        if not isinstance(path, str) or not path:
            continue

        name = block.get("name")
        if name == "read":
            file_ops.read.add(path)
        elif name == "write":
            file_ops.written.add(path)
        elif name == "edit":
            file_ops.edited.add(path)


def compute_file_lists(file_ops: FileOperations) -> tuple[list[str], list[str]]:
    """Collapse accumulated ops into sorted ``(read_files, modified_files)``.

    A file that was both read and modified counts only as modified (pi:
    computeFileLists, utils.ts:54).
    """
    modified = file_ops.edited | file_ops.written
    read_files = sorted(f for f in file_ops.read if f not in modified)
    modified_files = sorted(modified)
    return read_files, modified_files


def format_file_operations(read_files: list[str], modified_files: list[str]) -> str:
    """Render file lists as ``<read-files>`` / ``<modified-files>`` summary tags.

    Returns an empty string when both lists are empty; otherwise a leading
    blank-line-separated block to append to a summary (pi: formatFileOperations,
    utils.ts:62).
    """
    sections: list[str] = []
    if read_files:
        sections.append("<read-files>\n" + "\n".join(read_files) + "\n</read-files>")
    if modified_files:
        sections.append("<modified-files>\n" + "\n".join(modified_files) + "\n</modified-files>")
    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


def _safe_json_stringify(value: Any) -> str:
    """JSON-encode a tool-call argument value, never raising (pi: safeJsonStringify)."""
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return "[unserializable]"


def _truncate_for_summary(text: str, max_chars: int) -> str:
    """Clip overlong text and note how much was dropped (pi: truncateForSummary)."""
    if len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    return f"{text[:max_chars]}\n\n[... {dropped} more characters truncated]"


def _text_from_content(content: Any) -> str:
    """Concatenate the text of a string-or-block-list content value.

    τ user/toolResult content is either a plain string or a list of content
    blocks (``{"type": "text", "text": ...}`` and friends); pull just the text.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def serialize_conversation(messages: list[dict[str, Any]]) -> str:
    """Render messages as a plain-text transcript for the summarization prompt.

    Faithful port of pi's serializeConversation (utils.ts:91), reading τ message
    dicts. Roles are labelled ``[User]`` / ``[Assistant]`` / ``[Assistant
    thinking]`` / ``[Assistant tool calls]`` / ``[Tool result]``; tool results
    are truncated to ``TOOL_RESULT_MAX_CHARS``.
    """
    parts: list[str] = []

    for msg in messages:
        role = msg.get("role")
        if role == "user":
            text = _text_from_content(msg.get("content"))
            if text:
                parts.append(f"[User]: {text}")
        elif role == "assistant":
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls: list[str] = []

            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(str(block.get("text", "")))
                    elif btype == "thinking":
                        thinking_parts.append(str(block.get("thinking", "")))
                    elif btype == "toolCall":
                        args = block.get("arguments")
                        args_dict = args if isinstance(args, dict) else {}
                        args_str = ", ".join(
                            f"{k}={_safe_json_stringify(v)}" for k, v in args_dict.items()
                        )
                        tool_calls.append(f"{block.get('name', '')}({args_str})")

            if thinking_parts:
                parts.append("[Assistant thinking]: " + "\n".join(thinking_parts))
            if text_parts:
                parts.append("[Assistant]: " + "\n".join(text_parts))
            if tool_calls:
                parts.append("[Assistant tool calls]: " + "; ".join(tool_calls))
        elif role == "toolResult":
            text = _text_from_content(msg.get("content"))
            if text:
                parts.append("[Tool result]: " + _truncate_for_summary(text, TOOL_RESULT_MAX_CHARS))

    return "\n\n".join(parts)
