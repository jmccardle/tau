"""The walk as an Anthropic request: `system`, `messages`, `tools`, and the cache marks.

Split from `anthropic.py` (which reads the response) to keep both under the size ratchet. Private:
the shapes may change with the next provider.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from tau._core import Node, Part
from tau.protocols import SampleError, Tool, replay_for
from tau.providers._walk import (
    AssistantTurn,
    CallResult,
    Messages,
    SystemTurn,
    ToolResults,
    Turn,
    UserTurn,
    call_markup,
    call_object,
    stop_note,
    turns,
)
from tau.providers._wire import Json, plain

NAME = "anthropic"
_SIGNED_BLOCKS = (
    "thinking",
    "redacted_thinking",
)  # blocks only the model may write; no `cache_control`
_CACHE = {"type": "ephemeral"}


_VALID_ID = re.compile(r"[a-zA-Z0-9_-]+")


def _wire_id(call_id: str) -> str:
    """The id as `tool_use.id` and `tool_use_id` spell it: the API takes `[a-zA-Z0-9_-]+`, and
    another provider's ids may not match. A valid id is sent as it is; any other has its bad
    characters replaced by `_` and a short hash of the original added, so the result depends on
    the id alone (the same in every request, and in `tool_use` and `tool_result`)."""
    if _VALID_ID.fullmatch(call_id):
        return call_id
    digest = hashlib.sha256(call_id.encode("utf-8", errors="surrogatepass")).hexdigest()[:8]
    return re.sub(r"[^a-zA-Z0-9_-]", "_", call_id) + "_" + digest


def _signed(part: Part.Thinking) -> Json | None:
    """The block to send for a thinking part: `None` for one with no Anthropic replay data
    this version understands (unsigned, cut off, another provider's, or a shape it did not
    write), which is never sent (`model.md` 10.4). Strings in the parsed data are exact."""
    data = replay_for(part, NAME)
    if data is None:
        return None
    kind = data.get("type")
    if kind == "thinking" and isinstance(data.get("signature"), str):
        return {"type": "thinking", "thinking": part.text, "signature": data["signature"]}
    if kind == "redacted_thinking" and isinstance(data.get("data"), str):
        return {"type": "redacted_thinking", "data": data["data"]}
    return None


def _text(text: str) -> Json:
    return {"type": "text", "text": text}


def _assistant(turn: AssistantTurn) -> list[Json]:
    """The content blocks of a reply, in reply order, so thinking stays ahead of the calls it
    led to. Calls are `tool_use` only when the turn is runnable (and the caller has checked
    `turns`); otherwise they are text (`model.md` 10.6). The API refuses empty text, so a
    blank part is left out."""
    blocks: list[Json] = []
    for part in turn.parts:
        match part:
            case Part.Thinking():
                block = _signed(part)
                if block is not None:
                    blocks.append(block)
            case Part.Text(text=text):
                if text.strip():
                    blocks.append(_text(text))
            case Part.Call() if turn.runnable:
                args = call_object(part)
                if args is None:
                    raise SampleError(
                        f"call {part.id} ({part.tool}) would go as tool_use but its arguments "
                        "are not a JSON object"
                    )
                blocks.append(
                    {"type": "tool_use", "id": _wire_id(part.id), "name": part.tool, "input": args}
                )
            case Part.Call():
                blocks.append(_text(call_markup(part)))
            case _:
                pass
    if turn.note is not None:
        blocks.append(_text(stop_note(turn.note)))
    return blocks


def _thinkless_loop(all_turns: list[Turn]) -> int | None:
    """Under manual extended thinking (`thinking.type` `enabled`) the docs make a tool-use loop
    one assistant turn, and the final assistant turn of the request must start with a thinking
    block. So when the walk ends in `tool_result` blocks, the loop in progress (everything after
    the last user message holding more than `tool_result`) must open with thinking in its first
    assistant message. Returns the index of that turn when it does not (a loop begun by another
    provider), and the loop's batches then go as text, as do their results, so the walk stays
    sendable (a failure here would fail every later sample on the branch, `model.md` 10.7). Only
    the walk decides, so the answer is the same for every request of the loop."""
    if not all_turns or not isinstance(all_turns[-1], ToolResults):
        return None
    start = 0
    spoken = False  # a turn other than `System` has been seen
    for i, turn in enumerate(all_turns):
        if (isinstance(turn, UserTurn) and turn.text.strip()) or (
            isinstance(turn, SystemTurn) and spoken
        ):
            start = i + 1
        spoken = spoken or not isinstance(turn, SystemTurn)
    for i in range(start, len(all_turns)):
        turn = all_turns[i]
        if isinstance(turn, AssistantTurn):
            blocks = _assistant(replace(turn, runnable=False))
            if blocks:
                return None if blocks[0]["type"] in _SIGNED_BLOCKS else i
    return None


def conversation(
    walk: Sequence[Node], *, manual_thinking: bool = False, has_tools: bool = True
) -> tuple[str | None, list[Json]]:
    """The top-level `system` text and the `messages` for a walk. Consecutive turns of one role
    are one message (the API wants roles to alternate); a reply with nothing to send (empty,
    or only thinking that is not ours to send), blank user text, and an empty system are left
    out. `System` nodes that open the walk make `system`; one later is a user message
    `[system note: ...]`. `Config` and `Custom` nodes never reach the model. `manual_thinking` is
    whether the request sets `thinking.type` `enabled` (see `_thinkless_loop`). `has_tools` is
    whether the request offers tools: the API refuses `tool_use` blocks without them, so with
    none every batch goes as text. A batch whose arguments are no object (`tool_use.input`
    must be one) goes as text too."""
    messages = Messages("content")
    for turn in turns(
        walk,
        structured=has_tools,
        text_from=_thinkless_loop if manual_thinking else None,
    ):
        match turn:
            case SystemTurn(text=text):
                messages.system(text, _text)
            case UserTurn(text=text):
                messages.add("user", [_text(text)] if text.strip() else [])
            case AssistantTurn():
                messages.add("assistant", _assistant(turn))
            case ToolResults(results=results):
                messages.add("user", [_tool_result(r) for r in results])
    system = messages.instruction
    return (system if system is not None and system.strip() else None), messages.out


def _tool_result(result: CallResult) -> Json:
    return {
        "type": "tool_result",
        "tool_use_id": _wire_id(result.id),
        "content": result.text,
        "is_error": result.error,
    }


def tools_array(tools: Sequence[Tool]) -> list[Json]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": cast("Json", plain(t.parameters)),
        }
        for t in tools
    ]


def _markable(block: Json) -> bool:
    """Whether `cache_control` may go on a block: not on thinking, nor on an empty one."""
    kind = block["type"]
    if kind in _SIGNED_BLOCKS:
        return False
    if kind == "text":
        return bool(str(block["text"]).strip())
    if kind == "tool_result":
        return bool(block["content"])
    return True


def breakpoints(system: str | None, messages: list[Json]) -> object:
    """Mark the prefix to cache: everything through the system text (tools come before it), and
    everything through the last message (its last block that may carry a mark). A request
    builds on the last one's prefix, which stays within the API's 20-block lookback unless one
    turn adds that many blocks. Returns the `system` value: a block list when it carries a
    mark, `None` for no system."""
    if messages:
        for block in reversed(cast("list[Json]", messages[-1]["content"])):
            if _markable(block):
                block["cache_control"] = dict(_CACHE)
                break
    if system is None:
        return None
    return [{"type": "text", "text": system, "cache_control": dict(_CACHE)}]
