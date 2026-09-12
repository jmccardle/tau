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

from tau_llm.docs import agent_facing

CUSTOM_ROLE = "custom"


@agent_facing(topic="sessions")
def last_assistant_text(messages: list[dict[str, Any]]) -> str | None:
    """The most recent assistant message's text in ``messages``, or ``None``.

    A pure function over a message list, so a caller holding a transcript can ask
    without holding a session. :meth:`~tau_agent_core.agent_session.AgentSession
    .get_last_assistant_text` is this applied to the session's active path, and is
    what the ``get_last_assistant_text`` capability performs.

    Two rules the shape is not obvious about.

    A message that was aborted before it produced a single block —
    ``stop_reason == "aborted"`` AND empty ``content`` — is skipped as though it
    never happened, so an abort-and-retry does not hide the last real answer. An
    aborted message that DID say something is not skipped; its text still counts.

    Only ``type == "text"`` blocks contribute, concatenated in order with no
    separator. A thinking block is not the answer, and a tool-call block has no
    text to give.

    Args:
        messages: The transcript, oldest first.

    Returns:
        The concatenated text, stripped, or ``None``. ``None`` covers both "no
        assistant message yet" and "the last one carried no text" (a pure
        tool-call turn); the two are deliberately not distinguished, and a caller
        that must tell them apart looks at ``messages`` itself.
    """
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        if message.get("stop_reason") == "aborted" and not message.get("content"):
            continue
        text = "".join(
            block.get("text", "")
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return text.strip() or None
    return None


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
    visible_to_model: bool = True,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build an agent-level custom message dict (pi ``createCustomMessage``).

    The stored, durable form of an extension-injected node: ``role == "custom"``
    plus its ``customType`` / ``content`` (normalized to blocks) / ``display`` /
    optional ``details``. It is persisted inside a ``customMessage`` tree entry
    and threaded to the loop as-is; :func:`convert_to_llm` maps it to a ``user``
    message for the provider.

    ``visible_to_model`` (E6 §2 / S38, decision D-E6-1) records whether the node
    reaches the LLM. ``True`` (the ``before_agent_start`` case) → remapped
    custom→user on the wire; ``False`` (the ``api.send_message`` display-only
    default) → the node still persists and renders on the path but
    :func:`convert_to_llm` DROPS it, so the model never sees it. Stored so the
    distinction survives a reload; an older node without the key reads back as
    ``True`` (its historical behaviour), never fabricated.
    """
    message: dict[str, Any] = {
        "role": CUSTOM_ROLE,
        "customType": custom_type,
        "content": _content_to_blocks(content),
        "display": display,
        "visibleToModel": visible_to_model,
    }
    if details is not None:
        message["details"] = details
    if timestamp is not None:
        message["timestamp"] = timestamp
    return message


@agent_facing(topic="messages")
def is_displayed(message: dict[str, Any]) -> bool:
    """Whether a head should draw this message in its TRANSCRIPT.

    The reader of the ``display`` key :func:`create_custom_message` writes and
    ``api.send_message`` accepts. Until this existed the key was stored and read by
    nobody, so an extension that asked for a hidden node got a visible one — the
    silent-success failure the repo's Fail-Early rule names.

    Scope is the transcript alone. A hidden node is still on the tree, still on the
    active path, still reaches the model when ``visibleToModel`` says so, and the
    tree browser still draws its row and its detail pane — ``display`` says "do not
    put this in the running conversation a reader is following", not "conceal it".
    See ``docs/EXTENSION-MESSAGES.md`` §2.

    Args:
        message: A stored message dict. Any role; only ``custom`` carries the key.

    Returns:
        ``False`` only for a ``custom`` message whose ``display`` is literally
        ``False``. A message with no key, or any other role, is displayed — an
        older node predates the key and never meant to be hidden.
    """
    if message.get("role") != CUSTOM_ROLE:
        return True
    return message.get("display", True) is not False


def convert_to_llm(messages: list[Any]) -> list[Any]:
    """Map every ``custom`` message to a ``user`` message; pass the rest through.

    The τ port of pi ``convertToLlm`` for the one agent-level role that reaches
    the loop as a message (see the module docstring). Applied at the wire boundary
    (``agent_loop._stream_response``) so the provider — which rejects a ``custom``
    role — receives an LLM-acceptable ``user`` message, while the persisted /
    rendered node keeps its extension-origin ``role``. Non-``custom`` entries
    (pydantic ``UserMessage`` / ``AssistantMessage`` / dicts) are returned
    untouched.

    A ``custom`` node marked ``visibleToModel: False`` (the ``api.send_message``
    display-only default, E6 §2 / S38 / D-E6-1) is DROPPED here rather than
    remapped: it stays on the durable path (persisted, rendered in the transcript
    and tree) but is explicitly excluded from what the model sees. This is the
    sanctioned invariant exception — persisted == rendered, but NOT sent — and the
    only place the exclusion happens (the on-disk path is otherwise the wire). A
    node without the key is treated as visible (its historical behaviour).
    """
    converted: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            converted.append(message)
            continue
        if message.get("role") != CUSTOM_ROLE:
            converted.append(message)
            continue
        if message.get("visibleToModel", True) is False:
            # Display-only extension node: on the path, off the wire.
            continue
        user_message: dict[str, Any] = {
            "role": "user",
            "content": _content_to_blocks(message.get("content", [])),
        }
        if message.get("timestamp") is not None:
            user_message["timestamp"] = message["timestamp"]
        converted.append(user_message)
    return converted
