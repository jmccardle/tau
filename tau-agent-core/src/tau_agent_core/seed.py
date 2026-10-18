"""Seeds: the messages a branch is opened with in place of a user prompt.

A seeded branch hangs where its context should end and starts from messages the
spawner wrote, typically a note and an injected tool call with its result, so a
sub-agent continues an agent turn instead of answering a new user message
(docs/SEEDED-BRANCHES.md).
"""

from __future__ import annotations

import secrets
import string
import time
from typing import Any

from tau_llm.docs import agent_facing
from tau_llm.types import AssistantMessage, ToolResultMessage, UserMessage

from tau_agent_core.messages import CUSTOM_ROLE, create_custom_message
from tau_agent_core.tree_surgery import admission_reason

#: Injected call ids are nine of these: Mistral, Anthropic and OpenAI all accept that.
TOOL_CALL_ID_ALPHABET = string.ascii_letters + string.digits
TOOL_CALL_ID_LENGTH = 9

#: The ``api``, ``provider`` and ``model`` of an injected assistant message.
SEED_ORIGIN = "tau-seed"

_VALIDATORS: dict[str, type[UserMessage] | type[AssistantMessage] | type[ToolResultMessage]] = {
    "user": UserMessage,
    "assistant": AssistantMessage,
    "toolResult": ToolResultMessage,
}


def _now() -> int:
    """The epoch-ms stamp a seed message gets when it carries none."""
    return int(time.time() * 1000)


@agent_facing(topic="extensions")
def new_tool_call_id() -> str:
    """A fresh id for an injected tool call (docs/SEEDED-BRANCHES.md §5).

    Returns:
        Nine characters drawn from ``[a-zA-Z0-9]`` by :mod:`secrets`.
    """
    return "".join(secrets.choice(TOOL_CALL_ID_ALPHABET) for _ in range(TOOL_CALL_ID_LENGTH))


@agent_facing(topic="extensions")
def seed_tool_call(
    name: str,
    arguments: dict[str, Any],
    result: str | list[dict[str, Any]],
    *,
    is_error: bool = False,
) -> list[dict[str, Any]]:
    """An injected tool call and its answer, ready for ``spawn_branch(seed=…)``.

    Args:
        name: The tool's name. It need not be a tool the branch may call.
        arguments: The call's arguments.
        result: The result's text, or its content blocks.
        is_error: Whether the result reports a failure.

    Returns:
        ``[assistant, toolResult]`` sharing a :func:`new_tool_call_id`; the
        assistant names :data:`SEED_ORIGIN` as its api, provider and model.
    """
    call_id = new_tool_call_id()
    content = [{"type": "text", "text": result}] if isinstance(result, str) else list(result)
    stamp = _now()
    call = AssistantMessage.model_validate(
        {
            "content": [{"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}],
            "api": SEED_ORIGIN,
            "provider": SEED_ORIGIN,
            "model": SEED_ORIGIN,
            "stop_reason": "toolUse",
            "timestamp": stamp,
        }
    )
    answer = ToolResultMessage.model_validate(
        {
            "tool_call_id": call_id,
            "tool_name": name,
            "content": content,
            "is_error": is_error,
            "timestamp": stamp,
        }
    )
    return [call.model_dump(), answer.model_dump()]


def normalise_seed(seed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each seed element in the form the tree stores, or a ``ValueError``.

    Accepted: a ``user``, ``assistant`` or ``toolResult`` message dict, checked by
    its ``tau_llm`` model; and an extension note, ``{customType, content,
    display?, details?}`` with or without ``role: "custom"``, built by
    :func:`~tau_agent_core.messages.create_custom_message`. A missing
    ``timestamp`` is the time of the call.

    Args:
        seed: The caller's elements, in order.

    Returns:
        The stored message dicts, in order.

    Raises:
        ValueError: an element is not a dict, has another role, or fails its
            model's validation; the message names its index.
    """
    out: list[dict[str, Any]] = []
    for index, element in enumerate(seed):
        if not isinstance(element, dict):
            raise ValueError(f"seed[{index}] is a {type(element).__name__}, not a message dict")
        role = element.get("role")
        if role in (None, CUSTOM_ROLE) and "customType" in element:
            if "content" not in element:
                raise ValueError(f"seed[{index}] is a note with no 'content'")
            out.append(
                create_custom_message(
                    custom_type=str(element["customType"]),
                    content=element["content"],
                    display=bool(element.get("display", True)),
                    details=element.get("details"),
                    timestamp=element.get("timestamp") or _now(),
                )
            )
            continue
        validator = _VALIDATORS.get(str(role))
        if validator is None:
            raise ValueError(
                f"seed[{index}] has role {role!r}; a seed holds 'user', 'assistant', "
                "'toolResult' messages and {customType, content} notes"
            )
        try:
            message = validator.model_validate({"timestamp": _now(), **element})
        except Exception as exc:
            raise ValueError(f"seed[{index}] is not a valid {role!r} message: {exc}") from exc
        out.append(message.model_dump())
    return out


def _call_ids(messages: list[dict[str, Any]]) -> list[str]:
    """Every tool call id the assistant messages in ``messages`` declare, in order."""
    return [
        str(block.get("id"))
        for message in messages
        if message.get("role") == "assistant"
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "toolCall"
    ]


def check_seed(
    context: list[dict[str, Any]], seed: list[dict[str, Any]], *, prompted: bool
) -> None:
    """Refuse a seed the branch's model could not be sent (docs/SEEDED-BRANCHES.md §6).

    Args:
        context: The model-input context at the branch point.
        seed: The normalised seed (:func:`normalise_seed`).
        prompted: Whether a user prompt follows the seed.

    Raises:
        ValueError: the seed is empty; a call id repeats within the seed or one
            already on the context; :func:`~tau_agent_core.tree_surgery.admission_reason`
            refuses context + seed (it names the call); or, unprompted, the seed
            ends on an assistant message the model would be asked to continue.
    """
    if not seed:
        raise ValueError("spawn: seed is empty; pass the messages the branch starts from")
    seen = set(_call_ids(context))
    for call_id in _call_ids(seed):
        if call_id in seen:
            raise ValueError(
                f"spawn: tool call id {call_id!r} in the seed is already used on this "
                "path; one id must name one call"
            )
        seen.add(call_id)
    reason = admission_reason([*context, *seed])
    if reason is not None:
        raise ValueError(f"spawn: the branch's context would be invalid: {reason}")
    if not prompted and seed[-1].get("role") == "assistant":
        raise ValueError(
            "spawn: the seed ends on an assistant message, so the model would be asked "
            "to continue its own reply; end it on a tool result or a note"
        )
