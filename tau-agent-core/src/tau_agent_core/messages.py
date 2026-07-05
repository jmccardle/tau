"""τ-agent-core messages: agent-level custom messages + the LLM wire conversion.

Port of the ``custom`` → ``user`` mapping in pi's
``packages/agent/src/harness/messages.ts`` (``convertToLlm``). An extension may
inject durable *custom* nodes (E5 §3.1 / S29): the ``before_agent_start`` hook's
``message`` becomes a ``customMessage`` tree entry whose stored message carries
``role == "custom"`` so the TUI / tree browser render it as extension-origin —
NOT a literal user turn — while the WIRE must serialize it to an LLM-acceptable
``role``. pi maps ``custom`` → ``user``; τ mirrors that here.

τ's other agent-level roles (``branchSummary`` / ``compactionSummary`` in pi) are
tree entry KINDS in τ, already rendered to ``user`` messages by
:class:`~tau_agent_core.conversation_tree.ConversationTree`, so the only
agent-level role reaching the loop as a message is ``custom``; this module
therefore ports just that one case of ``convertToLlm``.

Reference: EXTENSIONS-E5-WIRING.md §1.1, §3.1 (durable ``before_agent_start``);
pi messages.ts ``convertToLlm`` (custom→user), ``createCustomMessage``.
"""

from __future__ import annotations

from typing import Any

# The agent-level role an extension-injected custom node carries in the tree /
# render (pi ``CustomMessage.role``). Serialized to ``"user"`` on the wire.
CUSTOM_ROLE = "custom"


def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize a custom message's ``content`` to a block list.

    A plain string becomes a single text block; a block list passes through
    (pi ``convertToLlm``: ``typeof content === "string" ? [text] : content``)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def create_custom_message(
    custom_type: str,
    content: Any,
    display: bool = True,
    details: Any = None,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build an agent-level custom message dict (pi ``createCustomMessage``).

    The stored, durable form of an extension-injected node: ``role == "custom"``
    plus its ``customType`` / ``content`` (normalized to blocks) / ``display`` /
    optional ``details``. It is persisted inside a ``customMessage`` tree entry
    and threaded to the loop as-is; :func:`convert_to_llm` maps it to a ``user``
    message for the provider.
    """
    message: dict[str, Any] = {
        "role": CUSTOM_ROLE,
        "customType": custom_type,
        "content": _content_to_blocks(content),
        "display": display,
    }
    if details is not None:
        message["details"] = details
    if timestamp is not None:
        message["timestamp"] = timestamp
    return message


def convert_to_llm(messages: list[Any]) -> list[Any]:
    """Map every ``custom`` message to a ``user`` message; pass the rest through.

    The τ port of pi ``convertToLlm`` for the one agent-level role that reaches
    the loop as a message (see the module docstring). Applied at the wire boundary
    (``agent_loop._stream_response``) so the provider — which rejects a ``custom``
    role — receives an LLM-acceptable ``user`` message, while the persisted /
    rendered node keeps its extension-origin ``role``. Non-``custom`` entries
    (pydantic ``UserMessage`` / ``AssistantMessage`` / dicts) are returned
    untouched.
    """
    converted: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            converted.append(message)
            continue
        if message.get("role") != CUSTOM_ROLE:
            converted.append(message)
            continue
        user_message: dict[str, Any] = {
            "role": "user",
            "content": _content_to_blocks(message.get("content", [])),
        }
        if message.get("timestamp") is not None:
            user_message["timestamp"] = message["timestamp"]
        converted.append(user_message)
    return converted
