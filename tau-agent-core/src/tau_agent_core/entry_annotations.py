"""Entry annotations: the blocks an ``annotate`` hook reads, and its failure type.

docs/ENTRY-ANNOTATIONS.md is the design record. An extension's ``annotate``
handler returns JSON for each message entry the loop writes, and τ stores it at
``ext[<extension name>]`` on that entry, in the write that records it.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Literal, TypedDict

from tau_llm.docs import agent_facing

EntryKind = Literal["user", "assistant", "toolResult", "custom"]
"""The ``kind`` of an ``annotate`` event: the role of the message being written."""

ENTRY_KINDS: tuple[str, ...] = ("user", "assistant", "toolResult", "custom")

BlockKind = Literal[
    "user", "assistant_text", "custom", "thinking", "tool_call", "tool_result", "details"
]


@agent_facing(topic="sessions", since="0.15.0")
class Block(TypedDict):
    """One text block of a message entry, as :func:`entry_blocks` derives it.

    Attributes:
        block: The index into ``message.content``, or ``"details"``.
        kind: What the text is (docs/ENTRY-ANNOTATIONS.md §6).
        text: The text offsets index, in code points.
        content_hash: SHA-256 hex of ``text``'s UTF-8 bytes.
    """

    block: int | str
    kind: BlockKind
    text: str
    content_hash: str


_TEXT_KIND: dict[str, BlockKind] = {
    "user": "user",
    "assistant": "assistant_text",
    "toolResult": "tool_result",
    "custom": "custom",
}


def _block(index: int | str, kind: BlockKind, text: str) -> Block:
    """A :class:`Block` over ``text``, hashed."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Block(block=index, kind=kind, text=text, content_hash=digest)


@agent_facing(topic="sessions", since="0.15.0")
def entry_blocks(kind: str, message: dict[str, Any]) -> list[Block]:
    """The text blocks of a message entry, in content order, then ``details``.

    A ``str`` content is one text block at index 0. An image yields nothing.
    A tool call is ``json.dumps(arguments, ensure_ascii=False)`` in its stored
    key order, and a non-``None`` ``details`` is rendered the same way as
    block ``"details"`` (docs/ENTRY-ANNOTATIONS.md §6).

    Args:
        kind: ``"user"``, ``"assistant"``, ``"toolResult"`` or ``"custom"``.
        message: The message dict as it is stored.

    Returns:
        One :class:`Block` per text, thinking or tool-call block, and ``details``.

    Raises:
        ValueError: ``kind`` is not an entry kind, or a content block has a type
            this kind of message does not carry.
    """
    if kind not in _TEXT_KIND:
        raise ValueError(f"entry_blocks: unknown entry kind {kind!r}")
    content = message.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    blocks: list[Block] = []
    for index, part in enumerate(content):
        part_type = part.get("type")
        if part_type == "text":
            blocks.append(_block(index, _TEXT_KIND[kind], part["text"]))
        elif part_type == "image":
            continue
        elif part_type == "thinking" and kind == "assistant":
            blocks.append(_block(index, "thinking", part["thinking"]))
        elif part_type == "toolCall" and kind == "assistant":
            rendered = json.dumps(part["arguments"], ensure_ascii=False)
            blocks.append(_block(index, "tool_call", rendered))
        else:
            raise ValueError(f"entry_blocks: a {kind!r} message has a {part_type!r} block")
    details = message.get("details")
    if details is not None:
        blocks.append(_block("details", "details", json.dumps(details, ensure_ascii=False)))
    return blocks


@agent_facing(topic="extensions", since="0.15.0")
class AnnotationError(RuntimeError):
    """An ``annotate`` handler raised or returned a value that is not JSON.

    Raised after the entry is written with ``ext[<extension>] = {"error": …}``,
    and the turn ends on it (docs/ENTRY-ANNOTATIONS.md §4.3).

    Attributes:
        extension: The failing extension's name, the ``ext`` key.
        error: ``"<ExceptionType>: <message>"``, as recorded on the entry.
    """

    def __init__(self, extension: str, error: str) -> None:
        """Name the failing extension and its recorded error."""
        super().__init__(f"extension {extension!r} annotate hook failed: {error}")
        self.extension = extension
        self.error = error


def json_value(value: Any, where: str = "$") -> Any:
    """A fresh copy of ``value`` when it is JSON, for storing on an entry.

    Args:
        value: What a handler returned.
        where: The path to ``value``, for the error message.

    Returns:
        ``value`` rebuilt from dicts, lists, str, int, finite float, bool and None.

    Raises:
        TypeError: something in ``value`` is not one of those, or a key is not a str.
        ValueError: a float is NaN or infinite.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{where} is {value!r}, which JSON cannot hold")
        return value
    if isinstance(value, list):
        return [json_value(item, f"{where}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"{where} has a {type(key).__name__} key {key!r}; JSON keys are str"
                )
            out[key] = json_value(item, f"{where}.{key}")
        return out
    raise TypeError(f"{where} is a {type(value).__name__}, which is not JSON")
