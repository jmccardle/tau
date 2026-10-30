"""The walk as an Anthropic request: `system`, `messages`, `tools`, and the cache marks.

Split from `anthropic.py` (which reads the response) to keep both under the size ratchet. Private:
the shapes may change with the next provider.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from tau_next._core import Node, Part
from tau_next.protocols import SampleError, Tool, replay_for
from tau_next.providers._walk import (
    AssistantTurn,
    CallResult,
    SystemTurn,
    ToolResults,
    UserTurn,
    call_markup,
    stop_note,
    turns,
)
from tau_next.providers._wire import Json, plain

NAME = "anthropic"
_UNSIGNED_BLOCKS = ("thinking", "redacted_thinking")  # `cache_control` is refused on these
_CACHE = {"type": "ephemeral"}


def _object_args(call: Part.Call) -> Json | None:
    """The `input` object of a call: its `args_raw` parsed (no arguments is `{}`); `None` if
    `args_raw` is not a JSON object, which `tool_use.input` must be (a call cut off, or one
    another provider let through)."""
    if not call.args_raw.strip():
        return {}
    try:
        parsed: object = json.loads(call.args_raw)
    except ValueError:
        return None
    return cast("Json", parsed) if isinstance(parsed, dict) else None


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
    (unsigned, cut off, or another provider's), which is never sent (`model.md` 10.4). Strings
    in the parsed data are exact; the signature and redacted data are always strings."""
    data = replay_for(part, NAME)
    if data is None:
        return None
    kind = data.get("type")
    if kind == "thinking" and isinstance(data.get("signature"), str):
        return {"type": "thinking", "thinking": part.text, "signature": data["signature"]}
    if kind == "redacted_thinking" and isinstance(data.get("data"), str):
        return {"type": "redacted_thinking", "data": data["data"]}
    raise SampleError(f"a thinking part carries replay data this provider did not write: {data}")


def _text(text: str) -> Json:
    return {"type": "text", "text": text}


def _assistant(turn: AssistantTurn) -> list[Json]:
    """The content blocks of a reply, in reply order, so thinking stays ahead of the calls it
    led to. Calls are `tool_use` only when the turn is runnable (and the caller has checked
    `_sendable`); otherwise they are text (`model.md` 10.6). The API refuses empty text, so a
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
                args = _object_args(part)
                assert args is not None, "an unsendable batch must be sent as text"
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


def _sendable(turn: AssistantTurn, must_open_with_thinking: bool) -> bool:
    """Whether a runnable batch can go as `tool_use` blocks. Not if a call's arguments are not
    an object, nor, when `must_open_with_thinking`, if its blocks do not open with thinking:
    with manual extended thinking (`thinking.type` `enabled`) the final assistant turn must
    start with a thinking block (docs, "Turn structure in manual mode"), and a batch from
    another provider has none. Such a batch goes as text, as do its results, so the walk stays
    sendable (a failure here would fail every later sample on the branch, `model.md` 10.7)."""
    calls = [p for p in turn.parts if isinstance(p, Part.Call)]
    if any(_object_args(c) is None for c in calls):
        return False
    if must_open_with_thinking:
        blocks = _assistant(turn)
        return bool(blocks) and blocks[0]["type"] in _UNSIGNED_BLOCKS
    return True


def _result_markup(result: CallResult) -> str:
    """A result as `outcome_markup` writes it (`CallResult.text` is its `outcome_text`)."""
    return f"[tool result {result.id}: {result.text}]"


def _system_note(text: str) -> str:
    """A `System` node after the walk's start, which the API's one `system` cannot hold."""
    return f"[system: {text}]"


def conversation(
    walk: Sequence[Node], manual_thinking: bool = False
) -> tuple[str | None, list[Json]]:
    """The top-level `system` text and the `messages` for a walk. Consecutive turns of one role
    are one message (the API wants roles to alternate), and a reply with nothing to send (empty,
    or only thinking that is not ours to send) is left out. `System` nodes that open the walk
    make `system`; one later is a user message `[system: ...]`. `Config` and `Custom` nodes never
    reach the model. `manual_thinking` is whether the request sets `thinking.type` `enabled`."""
    system: list[str] = []
    out: list[Json] = []
    all_turns = turns(walk)
    last = max((i for i, t in enumerate(all_turns) if isinstance(t, AssistantTurn)), default=-1)
    results_as_text = False

    def add(role: str, blocks: list[Json]) -> None:
        if out and out[-1]["role"] == role:
            cast("list[Json]", out[-1]["content"]).extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for i, turn in enumerate(all_turns):
        match turn:
            case SystemTurn(text=text):
                if out:
                    add("user", [_text(_system_note(text))])
                else:
                    system.append(text)
            case UserTurn(text=text):
                add("user", [_text(text)])
            case AssistantTurn():
                shown = turn
                if turn.runnable and not _sendable(turn, manual_thinking and i == last):
                    shown = replace(turn, runnable=False)
                    results_as_text = True
                blocks = _assistant(shown)
                if blocks:
                    add("assistant", blocks)
            case ToolResults(results=results) if results_as_text:
                add("user", [_text("\n".join(_result_markup(r) for r in results))])
                results_as_text = False
            case ToolResults(results=results):
                add("user", [_tool_result(r) for r in results])
    return ("\n\n".join(system) if system else None), out


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


def breakpoints(system: str | None, messages: list[Json]) -> object:
    """Mark the prefix to cache: everything through the system text (tools come before it), and
    everything through the last message. A request builds on the last one's prefix, which
    stays within the API's 20-block lookback unless one turn adds that many blocks. Returns the
    `system` value: a block list when it carries a mark."""
    if messages:
        last = cast("list[Json]", messages[-1]["content"])[-1]
        if last["type"] not in _UNSIGNED_BLOCKS:
            last["cache_control"] = dict(_CACHE)
    if system is None:
        return None
    return [{"type": "text", "text": system, "cache_control": dict(_CACHE)}]
