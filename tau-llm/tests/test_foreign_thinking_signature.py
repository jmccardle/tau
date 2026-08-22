"""``thinking_signature`` is ``str | dict``, and the OpenAI writer refuses a dict.

The word "signature" means four different things across the three APIs τ cares
about. On the OpenAI path it is a FIELD NAME — the streaming field the reasoning
arrived on, which τ then uses as a dictionary key to replay the reasoning to the
same model. On Anthropic it is an opaque cryptographic signature over the
thinking text. Those are not interchangeable, and the failure when they mix is
silent: an Anthropic signature used as a key writes a crypto blob where a JSON
field name belongs, producing a valid-looking request that means nothing.

So the field widens to ``str | dict[str, Any]`` and the OpenAI writer branches:
a ``str`` keeps today's meaning exactly, a ``dict`` means the block came from
another provider and takes the warn-and-degrade path (raise under
``Model.strict_reasoning_formats``).

Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md S2, S3, S4.
"""

import logging

import pytest

from tau_llm.providers import openai as openai_mod
from tau_llm.providers.openai import OpenAICompletionsProvider
from tau_llm.types import AssistantMessage, Model, TextContent, ThinkingContent

# An Anthropic-shaped payload: a cryptographic signature over the thinking text,
# plus the redacted-vs-plain distinction that Anthropic carries on a separate
# block type. S4 gives it a home here rather than adding a τ block type.
ANTHROPIC_SIGNATURE = {"anthropic": {"signature": "ErUBCkYIBRgCIkAx7", "redacted": False}}


@pytest.fixture(autouse=True)
def _clear_warn_dedupe():
    """The warn-once set is process-wide, so a test that expects a warning must
    not depend on which other test ran first."""
    openai_mod._WARNED_FOREIGN_SIGNATURES.clear()
    yield
    openai_mod._WARNED_FOREIGN_SIGNATURES.clear()


def _provider():
    return OpenAICompletionsProvider(api_key="sk-test")


def _make_model(**overrides) -> Model:
    defaults = {
        "id": "gpt-4o",
        "name": "GPT-4o",
        "api": "openai-completions",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "context_window": 128000,
        "max_tokens": 4096,
    }
    defaults.update(overrides)
    return Model(**defaults)


def _assistant(*content) -> AssistantMessage:
    return AssistantMessage(
        content=list(content),
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
    )


def _dict_message(signature):
    """The persisted/reload path — content is a block-list dict."""
    return {
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": "The user wants the file listed.",
                "thinking_signature": signature,
            }
        ],
    }


class TestStrSignatureUnchanged:
    """The str case is today's behaviour, byte for byte. Widening the type must
    not disturb any existing config or any persisted session."""

    def test_str_signature_still_replays_under_that_field(self):
        msg = _provider()._convert_messages_to_openai([_dict_message("reasoning_content")])[0]
        assert msg["reasoning_content"] == "The user wants the file listed."

    def test_empty_signature_still_falls_back_to_thinking_as_content(self):
        msg = _provider()._convert_messages_to_openai([_dict_message("")])[0]
        assert msg["content"] == "The user wants the file listed."
        assert "reasoning_content" not in msg

    def test_pydantic_block_with_str_signature_replays(self):
        message = _assistant(
            ThinkingContent(thinking="thought", thinking_signature="reasoning"),
            TextContent(text="answer"),
        )
        msg = _provider()._convert_messages_to_openai([message])[0]
        assert msg["reasoning"] == "thought"
        assert msg["content"] == "answer"


class TestDictSignatureDegrades:
    """A dict signature is another provider's payload. The OpenAI writer keeps
    the reasoning as text and never uses the payload as a key."""

    def test_dict_signature_is_never_used_as_a_key(self):
        msg = _provider()._convert_messages_to_openai([_dict_message(ANTHROPIC_SIGNATURE)])[0]
        # The blob must not appear as a field name, nor nested anywhere the
        # request would carry it.
        assert "anthropic" not in msg
        assert all(isinstance(key, str) for key in msg)
        assert "ErUBCkYIBRgCIkAx7" not in repr(msg)

    def test_dict_signature_keeps_the_thinking_as_content(self):
        """S2's path: the turn is not dropped to an empty message. This block has
        no text and no tool call, so the thinking carries it."""
        msg = _provider()._convert_messages_to_openai([_dict_message(ANTHROPIC_SIGNATURE)])[0]
        assert msg["content"] == "The user wants the file listed."

    def test_dict_signature_alongside_text_leaves_text_carrying_the_turn(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "thought",
                        "thinking_signature": ANTHROPIC_SIGNATURE,
                    },
                    {"type": "text", "text": "the answer"},
                ],
            }
        ]
        msg = _provider()._convert_messages_to_openai(messages)[0]
        assert msg["content"] == "the answer"
        assert "anthropic" not in msg

    def test_pydantic_block_with_dict_signature_degrades_too(self):
        """The live path, not just the reload path — ThinkingContent accepts the
        wider type, so the pydantic branch must guard as well."""
        message = _assistant(
            ThinkingContent(thinking="thought", thinking_signature=ANTHROPIC_SIGNATURE)
        )
        msg = _provider()._convert_messages_to_openai([message])[0]
        assert msg["content"] == "thought"
        assert "anthropic" not in msg

    def test_it_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="tau_llm.providers.openai"):
            _provider()._convert_messages_to_openai([_dict_message(ANTHROPIC_SIGNATURE)])
        assert "anthropic" in caplog.text
        assert "strict_reasoning_formats" in caplog.text

    def test_it_warns_only_once_per_payload_shape(self, caplog):
        """A foreign block stays in the replayed context for the rest of the
        session, so warning per conversion would warn on every turn."""
        provider = _provider()
        with caplog.at_level(logging.WARNING, logger="tau_llm.providers.openai"):
            for _ in range(4):
                provider._convert_messages_to_openai([_dict_message(ANTHROPIC_SIGNATURE)])
        assert len(caplog.records) == 1


class TestStrictReasoningFormats:
    """The same condition raises when the operator asked for that."""

    def test_strict_raises_on_a_dict_signature(self):
        with pytest.raises(ValueError, match="strict_reasoning_formats"):
            _provider()._convert_messages_to_openai(
                [_dict_message(ANTHROPIC_SIGNATURE)],
                "turn",
                True,
            )

    def test_strict_does_not_raise_on_a_str_signature(self):
        msg = _provider()._convert_messages_to_openai(
            [_dict_message("reasoning_content")], "turn", True
        )[0]
        assert msg["reasoning_content"] == "The user wants the file listed."

    def test_model_default_is_permissive(self):
        assert _make_model().strict_reasoning_formats is False

    def test_model_carries_the_flag(self):
        assert _make_model(strict_reasoning_formats=True).strict_reasoning_formats is True
