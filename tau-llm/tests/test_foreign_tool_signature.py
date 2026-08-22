"""S8 — a tool call's provider signature must not reach the OpenAI wire.

Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md S8, O4.

``ToolCall.provider_signature`` exists because Gemini 3 VALIDATES the token: the
first ``functionCall`` part of each step must carry its ``thought_signature`` or
the request fails with 400. That makes it a field of the tool call rather than
reasoning content, and it therefore survives into session JSONL and into any
later turn — including one addressed to a different provider.

So these tests pin the boundary in both directions:

* the tool call still replays (id, name, arguments intact) — the CALL is what the
  conversation needs;
* the signature does not, under any key — it is what the *other* wire needs, and
  a writer with nowhere to put it must drop it rather than invent a home.

The mirror of ``test_foreign_thinking_signature.py``, which pins S4 for thinking
blocks. Kept as its own file because the failure modes differ: a misplaced
thinking signature produces a meaningless request, while a misplaced tool-call
signature could reach the TOOL as an argument.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from tau_llm.providers import openai as openai_provider
from tau_llm.providers.openai import OpenAICompletionsProvider
from tau_llm.types import AssistantMessage, ToolCall

GOOGLE_SIGNATURE = {"google": {"thought_signature": "Cs4BAdHtim9nOtc"}}


@pytest.fixture(autouse=True)
def _forget_warnings() -> None:
    """Clear the once-per-shape warning sets.

    They are process-global by design (a foreign block stays in the replayed
    context and would otherwise re-warn every turn), which makes them order-
    dependent across tests unless cleared.
    """
    openai_provider._WARNED_FOREIGN_TOOL_SIGNATURES.clear()
    openai_provider._WARNED_FOREIGN_SIGNATURES.clear()


def _provider() -> OpenAICompletionsProvider:
    return OpenAICompletionsProvider(api_key="k", base_url="https://example.invalid/v1")


def _assistant(blocks: list[Any]) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=blocks,
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
    )


def _tool_call(**overrides: Any) -> ToolCall:
    fields: dict[str, Any] = {
        "id": "call_1",
        "name": "read",
        "arguments": {"path": "/tmp/x"},
    }
    fields.update(overrides)
    return ToolCall(**fields)


def _convert(message: AssistantMessage, *, strict: bool = False) -> dict[str, Any]:
    converted = _provider()._convert_messages_to_openai([message], "turn", strict)
    return dict(converted[0])


# ──────────────────────────────────────────────────────────────────────────
# The default: no signature at all
# ──────────────────────────────────────────────────────────────────────────


def test_a_tool_call_without_a_signature_is_unchanged() -> None:
    """The overwhelmingly common case must not have moved."""
    result = _convert(_assistant([_tool_call()]))

    assert result["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"path": "/tmp/x"})},
        }
    ]


def test_provider_signature_defaults_to_an_empty_dict() -> None:
    """Default-empty, so every existing construction site keeps working."""
    assert _tool_call().provider_signature == {}


def test_an_empty_signature_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """Only a POPULATED signature is a foreign one.

    Without this, every tool call in every session would warn.
    """
    with caplog.at_level(logging.WARNING):
        _convert(_assistant([_tool_call()]))

    assert "tool call carries" not in caplog.text
    assert not openai_provider._WARNED_FOREIGN_TOOL_SIGNATURES


# ──────────────────────────────────────────────────────────────────────────
# A foreign signature: the call survives, the signature does not
# ──────────────────────────────────────────────────────────────────────────


def test_the_tool_call_still_replays() -> None:
    """Dropping the signature must not drop the call.

    The id/name/arguments are what the conversation needs to stay coherent — the
    tool result that follows is matched by that id.
    """
    result = _convert(_assistant([_tool_call(provider_signature=GOOGLE_SIGNATURE)]))

    assert result["tool_calls"][0]["id"] == "call_1"
    assert result["tool_calls"][0]["function"]["name"] == "read"
    assert json.loads(result["tool_calls"][0]["function"]["arguments"]) == {"path": "/tmp/x"}


def test_the_signature_appears_nowhere_in_the_request() -> None:
    """The point of S8. Serialise the whole message and search it.

    Asserted against the SERIALISED form rather than specific keys: a future
    change that put the payload under some new field would pass a key-by-key
    check and still leak.
    """
    result = _convert(_assistant([_tool_call(provider_signature=GOOGLE_SIGNATURE)]))

    assert "thought_signature" not in json.dumps(result)
    assert "Cs4BAdHtim9nOtc" not in json.dumps(result)


def test_the_signature_does_not_reach_the_tool_arguments() -> None:
    """The failure this file exists for, stated on its own.

    ``arguments`` is the one field on a tool call that is both free-form and
    ACTED ON. A signature smuggled in there would be passed to the tool as a
    parameter it never declared.
    """
    result = _convert(_assistant([_tool_call(provider_signature=GOOGLE_SIGNATURE)]))

    assert json.loads(result["tool_calls"][0]["function"]["arguments"]) == {"path": "/tmp/x"}


def test_a_dict_block_carrying_a_signature_is_guarded_too() -> None:
    """The reload path. A resumed session replays dicts, not pydantic blocks.

    This is the path that actually matters for S8: the signature is persisted to
    session JSONL, so it comes back as a plain dict on the next run.
    """
    block = {
        "type": "toolCall",
        "id": "call_1",
        "name": "read",
        "arguments": {"path": "/tmp/x"},
        "provider_signature": GOOGLE_SIGNATURE,
    }
    result = _convert(_assistant([block]))

    assert "thought_signature" not in json.dumps(result)
    assert result["tool_calls"][0]["id"] == "call_1"


# ──────────────────────────────────────────────────────────────────────────
# Warning and strict behaviour
# ──────────────────────────────────────────────────────────────────────────


def test_it_warns_naming_the_namespace(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _convert(_assistant([_tool_call(provider_signature=GOOGLE_SIGNATURE)]))

    assert "google" in caplog.text
    assert "tool call" in caplog.text


def test_it_warns_once_per_payload_shape(caplog: pytest.LogCaptureFixture) -> None:
    """A foreign call stays in the replayed context, so re-warning would be endless."""
    message = _assistant([_tool_call(provider_signature=GOOGLE_SIGNATURE)])
    with caplog.at_level(logging.WARNING):
        _convert(message)
        _convert(message)
        _convert(message)

    assert len([r for r in caplog.records if "tool call carries" in r.message]) == 1


def test_a_different_namespace_warns_separately(caplog: pytest.LogCaptureFixture) -> None:
    """Keyed by shape, so a second vendor is genuinely new information."""
    with caplog.at_level(logging.WARNING):
        _convert(_assistant([_tool_call(provider_signature=GOOGLE_SIGNATURE)]))
        _convert(_assistant([_tool_call(provider_signature={"acme": {"sig": "x"}})]))

    assert len([r for r in caplog.records if "tool call carries" in r.message]) == 2


def test_strict_reasoning_formats_raises_instead(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ValueError, match="strict_reasoning_formats"):
        _convert(_assistant([_tool_call(provider_signature=GOOGLE_SIGNATURE)]), strict=True)


def test_strict_names_the_namespace_in_the_error() -> None:
    """The operator has to know WHICH provider's token was refused."""
    with pytest.raises(ValueError, match="google"):
        _convert(_assistant([_tool_call(provider_signature=GOOGLE_SIGNATURE)]), strict=True)


def test_strict_does_not_raise_without_a_signature() -> None:
    """strict must not turn ordinary tool calls into errors."""
    result = _convert(_assistant([_tool_call()]), strict=True)

    assert result["tool_calls"][0]["id"] == "call_1"


# ──────────────────────────────────────────────────────────────────────────
# Persistence — the reason the field is on the block and not on the message
# ──────────────────────────────────────────────────────────────────────────


def test_the_signature_survives_a_model_dump_round_trip() -> None:
    """A resumed Gemini 3 session whose signature was lost 400s on its next turn.

    That is why this rides on the block, which is what session JSONL persists,
    rather than on any in-memory-only structure.
    """
    original = _tool_call(provider_signature=GOOGLE_SIGNATURE)

    restored = ToolCall(**original.model_dump())

    assert restored.provider_signature == GOOGLE_SIGNATURE
