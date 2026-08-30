"""The ``anthropic-messages`` wire protocol.

Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md — S1, S2, S4, S5, S6, O5.

The client is built on the official ``anthropic`` SDK (O5), so these tests stub
the SDK's streaming surface rather than HTTP: the contract this module owns is
"τ messages in, τ streaming events out", and the SDK owns the bytes.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from tau_llm.providers import get_api_factory, get_provider_spec, registered_apis
from tau_llm.providers import anthropic as anthropic_mod
from tau_llm.providers.anthropic import (
    AnthropicMessagesProvider,
    read_signature_payload,
    signature_payload,
)
from tau_llm.streaming import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_llm.tools import ToolSpec
from tau_llm.types import (
    AssistantMessage,
    ImageContent,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

# ── fixtures and fakes ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_warn_dedupe():
    anthropic_mod._WARNED_REPLAY_OVERRIDE.clear()
    anthropic_mod._WARNED_UNSIGNED_THINKING.clear()
    yield
    anthropic_mod._WARNED_REPLAY_OVERRIDE.clear()
    anthropic_mod._WARNED_UNSIGNED_THINKING.clear()


def _model(**overrides) -> Model:
    defaults = {
        "id": "claude-opus-5",
        "name": "Claude Opus 5",
        "api": "anthropic-messages",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "context_window": 1_000_000,
        "max_tokens": 4096,
    }
    defaults.update(overrides)
    return Model(**defaults)


def _provider() -> AnthropicMessagesProvider:
    return AnthropicMessagesProvider(api_key="sk-ant-test")


class _Tool:
    """The three members ToolSpec actually requires, and nothing else.

    The agent loop passes ``AgentTool`` wrappers rather than ``ToolDefinition``,
    which is why ToolSpec is a Protocol — so a provider test states the protocol
    rather than borrowing a concrete class with unrelated required fields.
    """

    def __init__(self, name: str, description: str, parameters: dict):
        self.name = name
        self.description = description
        self.parameters = parameters


def _tool(name="ls", description="list", parameters=None) -> ToolSpec:
    return _Tool(name, description, parameters if parameters is not None else {"type": "object"})


def _usage(input_tokens=10, output_tokens=5, cache_read=0, cache_write=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def _final(content=(), stop_reason="end_turn", usage=None, stop_details=None):
    return SimpleNamespace(
        id="msg_01",
        model="claude-opus-5",
        content=list(content),
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=usage if usage is not None else _usage(),
    )


class _FakeStream:
    def __init__(self, events, final):
        self._events = list(events)
        self._final = final

    def __aiter__(self):
        async def gen():
            for event in self._events:
                yield event

        return gen()

    async def get_final_message(self):
        return self._final


class _FakeStreamManager:
    def __init__(self, stream):
        self._stream = stream

    async def __aenter__(self):
        return self._stream

    async def __aexit__(self, *exc):
        return False


#: The keyword arguments ``anthropic``'s ``AsyncMessages.stream`` declares, as of
#: SDK 1.0.0 — copied from ``inspect.signature`` against the installed SDK.
#:
#: Duplicated here rather than imported because this module must pass with the
#: ``anthropic`` import BLOCKED (see the lazy-import test at the bottom of the
#: file), and because the permissive ``def stream(self, **kwargs)`` this replaced
#: is what let τ ship a provider that raised ``TypeError`` on every real call:
#: the SDK removed ``temperature``, ``top_p`` and ``top_k`` from this list and
#: declares no ``**kwargs``, and a stub that accepts anything cannot see that.
#: When the SDK's signature changes, update this tuple — that edit is the point.
SDK_STREAM_PARAMS = (
    "max_tokens",
    "messages",
    "model",
    "cache_control",
    "inference_geo",
    "metadata",
    "output_config",
    "output_format",
    "container",
    "service_tier",
    "stop_sequences",
    "system",
    "thinking",
    "tool_choice",
    "tools",
    "user_profile_id",
    "extra_headers",
    "extra_query",
    "extra_body",
    "timeout",
)

_UNSET = object()


def _strict_messages_class(on_call, params):
    """Build a ``messages`` stub whose ``stream`` declares exactly ``params``.

    The signature is generated rather than written as ``**kwargs`` so the stub
    REJECTS an undeclared keyword argument the way the real SDK does, and so
    ``inspect.signature`` reports the same parameter set the provider reads.
    """
    signature = ", ".join(f"{name}=_UNSET" for name in params)
    captured = ", ".join(f"{name!r}: {name}" for name in params)
    namespace = {"_UNSET": _UNSET, "_on_call": on_call}
    exec(  # noqa: S102 — a generated signature is the whole point of this stub
        f"def stream(self, *, {signature}):\n    return _on_call({{{captured}}})\n",
        namespace,
    )
    return type("_Messages", (), {"stream": namespace["stream"]})


class _FakeClient:
    """Records the request τ built, and replays a canned stream."""

    def __init__(self, events, final, params=SDK_STREAM_PARAMS):
        self.requests = []
        self.closed = False

        def on_call(kwargs):
            self.requests.append({k: v for k, v in kwargs.items() if v is not _UNSET})
            return _FakeStreamManager(_FakeStream(events, final))

        self.messages = _strict_messages_class(on_call, params)()

    async def close(self):
        self.closed = True


def _run(
    provider,
    model,
    messages,
    tools=None,
    options=None,
    events=(),
    final=None,
    params=SDK_STREAM_PARAMS,
):
    """Drive one completion and return ``(events, fake_client)``."""
    client = _FakeClient(events, final if final is not None else _final(), params)
    provider._client = client

    async def drive():
        stream = await provider.stream_chat(model, messages, tools, options)
        return [event async for event in stream]

    return asyncio.run(drive()), client


def _convert(provider, messages, model=None):
    return provider._convert_messages(messages, model or _model())


SIGNED = signature_payload("ErUBCkYIBRgCIkAx7")


# ── registration and dispatch (S5) ───────────────────────────────────────


class TestRegistration:
    def test_the_wire_protocol_is_registered(self):
        assert "anthropic-messages" in registered_apis()

    def test_the_factory_builds_this_provider(self):
        provider = get_api_factory("anthropic-messages")(
            provider_id="anthropic",
            name="Anthropic",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
        )
        assert isinstance(provider, AnthropicMessagesProvider)
        assert provider.id == "anthropic"
        assert provider.base_url == "https://api.anthropic.com"

    def test_the_vendor_carries_an_endpoint_and_a_credential_variable(self):
        spec = get_provider_spec("anthropic")
        assert spec is not None
        assert spec.api == "anthropic-messages"
        assert spec.base_url == "https://api.anthropic.com"
        assert spec.api_key_env == ("ANTHROPIC_API_KEY",)

    def test_bedrock_and_vertex_are_not_shipped(self):
        """They speak Anthropic-shaped protocols behind different auth. Each is
        the embedding application's own register_provider call."""
        assert get_provider_spec("bedrock") is None
        assert get_provider_spec("vertex") is None


# ── the signature payload (S4) ───────────────────────────────────────────


class TestSignaturePayload:
    def test_a_plain_signature_round_trips(self):
        payload = signature_payload("abc123")
        assert read_signature_payload(payload) == {"redacted": False, "signature": "abc123"}

    def test_a_redacted_payload_carries_data_and_no_signature(self):
        payload = signature_payload(data="encrypted-blob")
        inner = read_signature_payload(payload)
        assert inner == {"redacted": True, "data": "encrypted-blob"}

    def test_an_openai_field_name_is_not_an_anthropic_payload(self):
        """A bare str is the OpenAI meaning of the field. Reading it as an
        Anthropic payload is exactly the confusion S4 exists to prevent."""
        assert read_signature_payload("reasoning_content") is None

    def test_another_providers_payload_is_not_ours(self):
        assert read_signature_payload({"google": {"thoughtSignature": "x"}}) is None


# ── message conversion ───────────────────────────────────────────────────


class TestSystemPrompt:
    def test_a_system_message_becomes_the_system_parameter(self):
        """Anthropic has no system ROLE — it is a top-level parameter."""
        system, messages = _convert(
            _provider(),
            [
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "hi"},
            ],
        )
        assert system == "You are terse."
        assert [m["role"] for m in messages] == ["user"]

    def test_several_system_messages_join(self):
        system, _ = _convert(
            _provider(),
            [
                {"role": "system", "content": "first"},
                {"role": "system", "content": "second"},
                {"role": "user", "content": "hi"},
            ],
        )
        assert system == "first\n\nsecond"

    def test_no_system_message_means_no_system_parameter(self):
        provider = _provider()
        events, client = _run(provider, _model(), [{"role": "user", "content": "hi"}])
        assert "system" not in client.requests[0]


class TestToolResultImages:
    """An image returned by a tool rides INSIDE the ``tool_result`` block.

    That is where the Messages API takes it, and where pi puts it
    (``anthropic-messages.ts`` ``convertContentBlocks``). This client used to
    collect the text blocks and drop the images with no error and no
    placeholder, so a vision model got the tool's prose and nothing to look at.

    No separate user turn, unlike the OpenAI client: because the image stays in
    the block, a parallel call's results are never split and the ordering
    question that client has to solve does not arise here.
    """

    @staticmethod
    def _messages(content):
        return [
            {"role": "user", "content": "look"},
            {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "t1", "name": "read", "arguments": {}}],
            },
            {"role": "toolResult", "tool_call_id": "t1", "content": content},
        ]

    def test_an_image_reaches_the_wire(self):
        _, messages = _convert(
            _provider(),
            self._messages(
                [
                    {"type": "text", "text": "[image: a.png]"},
                    {"type": "image", "mime_type": "image/png", "data": "AAA"},
                ]
            ),
        )
        block = messages[-1]["content"][0]

        assert block["content"][0] == {"type": "text", "text": "[image: a.png]"}
        assert block["content"][1] == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "AAA"},
        }

    def test_an_image_with_no_text_carries_only_the_image(self):
        _, messages = _convert(
            _provider(),
            self._messages([{"type": "image", "mime_type": "image/png", "data": "AAA"}]),
        )
        content = messages[-1]["content"][0]["content"]

        assert len(content) == 1
        assert content[0]["type"] == "image"

    def test_a_text_only_result_still_sends_a_plain_string(self):
        """Every result but a handful. The wire shape for text must not change
        because images became possible."""
        _, messages = _convert(
            _provider(),
            self._messages([{"type": "text", "text": "a.py"}, {"type": "text", "text": "b.py"}]),
        )

        assert messages[-1]["content"][0]["content"] == "a.py b.py"


class TestToolResults:
    def test_a_tool_result_becomes_a_user_message(self):
        _, messages = _convert(
            _provider(),
            [
                {"role": "user", "content": "list files"},
                {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "t1", "name": "ls", "arguments": {}}],
                },
                {"role": "toolResult", "tool_call_id": "t1", "content": "a.py"},
            ],
        )
        assert messages[-1]["role"] == "user"
        block = messages[-1]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "t1"
        assert block["content"] == "a.py"

    def test_parallel_results_land_in_ONE_user_message(self):
        """Splitting the results of a parallel call across several messages
        teaches the model to stop making parallel calls."""
        _, messages = _convert(
            _provider(),
            [
                {"role": "user", "content": "read both"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "id": "t1", "name": "read", "arguments": {}},
                        {"type": "toolCall", "id": "t2", "name": "read", "arguments": {}},
                    ],
                },
                {"role": "toolResult", "tool_call_id": "t1", "content": "one"},
                {"role": "toolResult", "tool_call_id": "t2", "content": "two"},
            ],
        )
        assert len(messages) == 3
        assert [b["tool_use_id"] for b in messages[-1]["content"]] == ["t1", "t2"]

    def test_a_result_never_merges_into_a_real_user_turn(self):
        _, messages = _convert(
            _provider(),
            [
                {"role": "user", "content": "hello"},
                {"role": "toolResult", "tool_call_id": "t1", "content": "out"},
            ],
        )
        assert len(messages) == 2
        assert messages[0]["content"][0]["text"] == "hello"

    def test_an_error_result_carries_is_error(self):
        _, messages = _convert(
            _provider(),
            [
                ToolResultMessage(
                    tool_call_id="t1",
                    tool_name="read",
                    content=[TextContent(text="no such file")],
                    is_error=True,
                    timestamp=0,
                )
            ],
        )
        assert messages[0]["content"][0]["is_error"] is True


class TestAssistantBlocks:
    def test_a_tool_call_becomes_tool_use_with_its_id(self):
        _, messages = _convert(
            _provider(),
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "toolu_1",
                            "name": "read",
                            "arguments": {"path": "a.py"},
                        }
                    ],
                }
            ],
        )
        assert messages[0]["content"][0] == {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "read",
            "input": {"path": "a.py"},
        }

    def test_thinking_leads_the_block_list(self):
        _, messages = _convert(
            _provider(),
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "answer"},
                        {
                            "type": "thinking",
                            "thinking": "thought",
                            "thinking_signature": SIGNED,
                        },
                    ],
                }
            ],
        )
        assert [b["type"] for b in messages[0]["content"]] == ["thinking", "text"]

    def test_a_signed_thinking_block_replays_with_its_signature(self):
        _, messages = _convert(
            _provider(),
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "thought", "thinking_signature": SIGNED},
                        {"type": "text", "text": "answer"},
                    ],
                }
            ],
        )
        assert messages[0]["content"][0] == {
            "type": "thinking",
            "thinking": "thought",
            "signature": "ErUBCkYIBRgCIkAx7",
        }

    def test_a_redacted_block_replays_on_its_own_block_type(self):
        _, messages = _convert(
            _provider(),
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "",
                            "thinking_signature": signature_payload(data="blob"),
                        },
                        {"type": "text", "text": "answer"},
                    ],
                }
            ],
        )
        assert messages[0]["content"][0] == {"type": "redacted_thinking", "data": "blob"}

    def test_a_pydantic_assistant_message_converts_the_same_way(self):
        message = AssistantMessage(
            content=[
                ThinkingContent(thinking="thought", thinking_signature=SIGNED),
                TextContent(text="answer"),
                ToolCall(id="t1", name="ls", arguments={"path": "."}),
            ],
            api="anthropic-messages",
            provider="anthropic",
            model="claude-opus-5",
            stop_reason="toolUse",
            timestamp=0,
        )
        _, messages = _convert(_provider(), [message])
        assert [b["type"] for b in messages[0]["content"]] == ["thinking", "text", "tool_use"]

    def test_an_image_becomes_a_base64_source_block(self):
        _, messages = _convert(
            _provider(),
            [
                UserMessage(
                    content=[ImageContent(data="QUJD", mime_type="image/png")],
                    timestamp=0,
                )
            ],
        )
        assert messages[0]["content"][0] == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
        }

    def test_a_data_uri_prefix_is_stripped(self):
        _, messages = _convert(
            _provider(),
            [
                UserMessage(
                    content=[
                        ImageContent(data="data:image/png;base64,QUJD", mime_type="image/png")
                    ],
                    timestamp=0,
                )
            ],
        )
        assert messages[0]["content"][0]["source"]["data"] == "QUJD"


class TestReasoningReplay:
    """S1 — τ's 'turn' scope is already what Anthropic requires, and the other
    two settings are overridden rather than obeyed."""

    def _two_turns(self):
        """Indices: 0 user, 1 assistant(prior), 2 user, 3 assistant(current)."""
        return [
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "old", "thinking_signature": SIGNED},
                    {"type": "text", "text": "first answer"},
                ],
            },
            {"role": "user", "content": "second"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "new", "thinking_signature": SIGNED},
                    {"type": "text", "text": "second answer"},
                ],
            },
        ]

    def test_the_current_turns_thinking_is_replayed(self):
        _, messages = _convert(_provider(), self._two_turns())
        assert messages[-1]["content"][0]["thinking"] == "new"

    def test_a_prior_turns_thinking_is_dropped_not_downgraded_to_text(self):
        """The API discards prior-turn thinking anyway. Sending it as text would
        inject the model's private reasoning into the visible conversation."""
        _, messages = _convert(_provider(), self._two_turns())
        prior = messages[1]
        assert [b["type"] for b in prior["content"]] == ["text"]
        assert "old" not in str(prior)

    def test_thinking_inside_a_tool_loop_survives_across_tool_results(self):
        """The whole assistant/tool sequence sits after the last USER message, so
        'turn' keeps the in-progress chain-of-thought."""
        _, messages = _convert(
            _provider(),
            [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "step one", "thinking_signature": SIGNED},
                        {"type": "toolCall", "id": "t1", "name": "ls", "arguments": {}},
                    ],
                },
                {"role": "toolResult", "tool_call_id": "t1", "content": "a.py"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "step two", "thinking_signature": SIGNED},
                        {"type": "text", "text": "done"},
                    ],
                },
            ],
        )
        replayed = [
            b["thinking"]
            for m in messages
            for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert replayed == ["step one", "step two"]

    def test_reasoning_replay_off_is_overridden_with_a_warning(self, caplog):
        """The one place in τ where a stated config value is deliberately not
        obeyed. 'off' would strip signatures the API requires inside the current
        tool loop, which breaks the request rather than trimming it."""
        model = _model(reasoning_replay="off")
        with caplog.at_level(logging.WARNING, logger="tau_llm.providers.anthropic"):
            _, messages = _convert(_provider(), self._two_turns(), model)
        assert messages[-1]["content"][0]["thinking"] == "new"  # 'turn' behaviour ran
        assert "reasoning_replay" in caplog.text
        assert "S1" in caplog.text

    def test_reasoning_replay_all_is_overridden_too(self, caplog):
        """'all' is wasteful rather than wrong — the API discards prior-turn
        thinking — but it is still not what τ sends."""
        model = _model(reasoning_replay="all")
        with caplog.at_level(logging.WARNING, logger="tau_llm.providers.anthropic"):
            _, messages = _convert(_provider(), self._two_turns(), model)
        assert [b["type"] for b in messages[1]["content"]] == ["text"]
        assert caplog.records

    def test_the_override_warns_once_per_model(self, caplog):
        model = _model(reasoning_replay="off")
        provider = _provider()
        with caplog.at_level(logging.WARNING, logger="tau_llm.providers.anthropic"):
            for _ in range(3):
                _convert(provider, self._two_turns(), model)
        assert len(caplog.records) == 1

    def test_the_default_scope_warns_about_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="tau_llm.providers.anthropic"):
            _convert(_provider(), self._two_turns(), _model())
        assert not caplog.records

    def test_strict_turns_the_override_into_a_raise(self):
        model = _model(reasoning_replay="off", strict_reasoning_formats=True)
        with pytest.raises(ValueError, match="strict_reasoning_formats"):
            _convert(_provider(), self._two_turns(), model)


class TestUnsignedThinking:
    """S2 — a thinking block can reach this converter with no signature: an
    aborted stream, a block persisted before signatures were captured, a message
    an extension synthesised, or a session that changed models."""

    def _message(self, signature):
        return [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "thought", "thinking_signature": signature},
                    {"type": "text", "text": "answer"},
                ],
            }
        ]

    def test_an_unsigned_block_becomes_text_rather_than_raising(self):
        _, messages = _convert(_provider(), self._message(""))
        assert messages[0]["content"] == [
            {"type": "text", "text": "thought"},
            {"type": "text", "text": "answer"},
        ]

    def test_an_openai_signature_is_also_unsigned_here(self):
        """A str signature is a field name for the OpenAI wire. It means nothing
        to Anthropic, so it takes the same path."""
        _, messages = _convert(_provider(), self._message("reasoning_content"))
        assert messages[0]["content"][0] == {"type": "text", "text": "thought"}

    def test_it_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="tau_llm.providers.anthropic"):
            _convert(_provider(), self._message(""))
        assert "no Anthropic signature" in caplog.text
        assert "S2" in caplog.text

    def test_strict_raises_instead(self):
        model = _model(strict_reasoning_formats=True)
        with pytest.raises(ValueError, match="strict_reasoning_formats"):
            _convert(_provider(), self._message(""), model)

    def test_an_empty_unsigned_block_is_simply_dropped(self):
        """Nothing to preserve, so nothing to warn about."""
        _, messages = _convert(
            _provider(),
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "", "thinking_signature": ""},
                        {"type": "text", "text": "answer"},
                    ],
                }
            ],
        )
        assert messages[0]["content"] == [{"type": "text", "text": "answer"}]


class TestToolConversion:
    def test_parameters_becomes_input_schema(self):
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        tools = _provider()._convert_tools(
            [_tool(name="read", description="Read a file", parameters=schema)]
        )
        assert tools == [{"name": "read", "description": "Read a file", "input_schema": schema}]


# ── streaming ────────────────────────────────────────────────────────────


class TestStreaming:
    def test_text_deltas_become_text_delta_events(self):
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            events=[
                SimpleNamespace(type="text", text="Hel"),
                SimpleNamespace(type="text", text="lo"),
            ],
            final=_final(content=[SimpleNamespace(type="text", text="Hello")]),
        )
        deltas = [e.delta for e in events if isinstance(e, TextDeltaEvent)]
        assert deltas == ["Hel", "lo"]

    def test_thinking_deltas_become_thinking_delta_events(self):
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            events=[
                SimpleNamespace(type="thinking", thinking="I should "),
                SimpleNamespace(type="thinking", thinking="answer."),
                SimpleNamespace(type="signature", signature="sig-1"),
                SimpleNamespace(type="text", text="Hello"),
            ],
            final=_final(content=[SimpleNamespace(type="text", text="Hello")]),
        )
        assert [e.delta for e in events if isinstance(e, ThinkingDeltaEvent)] == [
            "I should ",
            "answer.",
        ]

    def test_the_partial_message_consolidates_fragments_into_one_block(self):
        """One block per KIND, not one per fragment — a block per fragment bloats
        persistence and makes the TUI re-emit the whole reasoning trace."""
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            events=[
                SimpleNamespace(type="text", text="a"),
                SimpleNamespace(type="text", text="b"),
            ],
            final=_final(content=[SimpleNamespace(type="text", text="ab")]),
        )
        partial = [e for e in events if isinstance(e, TextDeltaEvent)][-1].partial
        assert len(partial.content) == 1
        assert partial.content[0].text == "ab"

    def test_the_stream_ends_with_a_done_event_carrying_the_final_message(self):
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            events=[SimpleNamespace(type="text", text="Hello")],
            final=_final(content=[SimpleNamespace(type="text", text="Hello")]),
        )
        done = events[-1]
        assert isinstance(done, DoneEvent)
        assert done.final.content[0].text == "Hello"
        assert done.final.api == "anthropic-messages"
        assert done.final.provider == "anthropic"
        assert done.final.response_id == "msg_01"

    def test_a_transport_failure_becomes_an_error_event(self):
        provider = _provider()

        class _Exploding:
            class messages:  # noqa: N801 — mirrors the SDK's attribute name
                @staticmethod
                def stream(**kwargs):
                    raise RuntimeError("connection reset")

        provider._client = _Exploding()

        async def drive():
            stream = await provider.stream_chat(_model(), [{"role": "user", "content": "hi"}])
            return [event async for event in stream]

        events = asyncio.run(drive())
        assert isinstance(events[-1], ErrorEvent)
        assert "connection reset" in events[-1].message
        # The endpoint and the model are named: a fleet behind one config can
        # have several, and the answer is not in the exception.
        assert "claude-opus-5" in events[-1].message
        assert "api.anthropic.com" in events[-1].message


class TestFinalMessage:
    def test_a_tool_use_block_becomes_a_tool_call(self):
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "list"}],
            final=_final(
                content=[
                    SimpleNamespace(type="tool_use", id="toolu_1", name="ls", input={"path": "."})
                ],
                stop_reason="tool_use",
            ),
        )
        done = events[-1]
        calls = done.final.get_tool_calls()
        assert len(calls) == 1
        assert (calls[0].id, calls[0].name, calls[0].arguments) == ("toolu_1", "ls", {"path": "."})
        assert done.final.stop_reason == "toolUse"

    def test_each_tool_call_also_emits_one_delta_event(self):
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "list"}],
            final=_final(
                content=[
                    SimpleNamespace(type="tool_use", id="t1", name="ls", input={}),
                    SimpleNamespace(type="tool_use", id="t2", name="grep", input={"q": "x"}),
                ],
                stop_reason="tool_use",
            ),
        )
        deltas = [e for e in events if isinstance(e, ToolCallDeltaEvent)]
        assert [d.delta["id"] for d in deltas] == ["t1", "t2"]
        assert deltas[1].delta["function"]["arguments"] == '{"q": "x"}'

    def test_a_nameless_tool_call_is_refused_rather_than_dispatched(self):
        """A nameless call is unroutable: the agent loop would miss on `""` and
        report `Unknown tool: `, blaming the model for a wire-contract violation.
        The finalize path refuses it, and the refusal surfaces as an ErrorEvent —
        the same shape the OpenAI provider gives the same fault."""
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "list"}],
            final=_final(
                content=[SimpleNamespace(type="tool_use", id="t1", name="", input={})],
                stop_reason="tool_use",
            ),
        )
        assert isinstance(events[-1], ErrorEvent)
        assert "no tool name" in events[-1].message
        assert not any(isinstance(e, DoneEvent) for e in events)

    def test_a_thinking_block_keeps_its_signature_for_the_next_turn(self):
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            final=_final(
                content=[
                    SimpleNamespace(type="thinking", thinking="thought", signature="sig-9"),
                    SimpleNamespace(type="text", text="answer"),
                ]
            ),
        )
        block = events[-1].final.content[0]
        assert read_signature_payload(block.thinking_signature)["signature"] == "sig-9"

    def test_a_redacted_block_keeps_its_payload_and_claims_no_text(self):
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            final=_final(content=[SimpleNamespace(type="redacted_thinking", data="blob")]),
        )
        block = events[-1].final.content[0]
        assert block.thinking == ""
        assert read_signature_payload(block.thinking_signature) == {
            "redacted": True,
            "data": "blob",
        }

    def test_a_finished_message_round_trips_back_onto_the_wire(self):
        """The point of the payload: what comes off the stream can be replayed."""
        provider = _provider()
        events, _ = _run(
            provider,
            _model(),
            [{"role": "user", "content": "hi"}],
            final=_final(
                content=[
                    SimpleNamespace(type="thinking", thinking="thought", signature="sig-9"),
                    SimpleNamespace(type="tool_use", id="t1", name="ls", input={}),
                ],
                stop_reason="tool_use",
            ),
        )
        final_msg = events[-1].final
        _, messages = _convert(provider, [{"role": "user", "content": "hi"}, final_msg])
        assert messages[-1]["content"][0] == {
            "type": "thinking",
            "thinking": "thought",
            "signature": "sig-9",
        }


class TestStopReasons:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("end_turn", "stop"),
            ("stop_sequence", "stop"),
            ("max_tokens", "length"),
            ("tool_use", "toolUse"),
        ],
    )
    def test_mapping(self, raw, expected):
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            final=_final(content=[SimpleNamespace(type="text", text="x")], stop_reason=raw),
        )
        assert events[-1].final.stop_reason == expected

    def test_a_refusal_is_an_error_not_a_clean_stop(self):
        """A refusal arrives as HTTP 200 with little or no content. Reporting it
        as 'stop' would hand the caller an empty successful answer."""
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            final=_final(
                content=[],
                stop_reason="refusal",
                stop_details=SimpleNamespace(category="cyber", explanation="declined"),
            ),
        )
        final_msg = events[-1].final
        assert final_msg.stop_reason == "error"
        assert "cyber" in final_msg.error_message

    def test_an_unmapped_stop_reason_is_an_error_not_a_guess(self):
        """`pause_turn` means the turn is RESUMABLE, not finished. Calling it a
        stop would hand back a truncated answer that looks complete."""
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            final=_final(
                content=[SimpleNamespace(type="text", text="x")], stop_reason="pause_turn"
            ),
        )
        final_msg = events[-1].final
        assert final_msg.stop_reason == "error"
        assert "pause_turn" in final_msg.error_message


class TestUsage:
    def test_cache_counters_map_and_the_total_is_computed(self):
        """Anthropic reports no total, and excludes both cache counters from
        input_tokens."""
        events, _ = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            final=_final(
                content=[SimpleNamespace(type="text", text="x")],
                usage=_usage(input_tokens=100, output_tokens=20, cache_read=50, cache_write=10),
            ),
        )
        usage = events[-1].usage
        assert usage.input_tokens == 100
        assert usage.output_tokens == 20
        assert usage.cache_read_tokens == 50
        assert usage.cache_write_tokens == 10
        assert usage.total_tokens == 180


# ── the request τ builds ─────────────────────────────────────────────────


class TestRequest:
    def test_max_tokens_comes_from_the_model(self):
        _, client = _run(_provider(), _model(max_tokens=1234), [{"role": "user", "content": "hi"}])
        assert client.requests[0]["max_tokens"] == 1234

    def test_no_thinking_parameter_without_a_reasoning_option(self):
        _, client = _run(_provider(), _model(), [{"role": "user", "content": "hi"}])
        assert "thinking" not in client.requests[0]
        assert "output_config" not in client.requests[0]

    def test_a_reasoning_level_sends_adaptive_thinking_and_an_effort(self):
        """budget_tokens is removed on the current models and returns a 400, so
        adaptive is the only on-mode τ sends."""
        _, client = _run(
            _provider(),
            _model(reasoning=True),
            [{"role": "user", "content": "hi"}],
            options={"reasoning": "high"},
        )
        assert client.requests[0]["thinking"] == {"type": "adaptive"}
        assert client.requests[0]["output_config"] == {"effort": "high"}

    def test_reasoning_off_disables_thinking_rather_than_silently_upgrading(self):
        _, client = _run(
            _provider(),
            _model(reasoning=True),
            [{"role": "user", "content": "hi"}],
            options={"reasoning": "off"},
        )
        assert client.requests[0]["thinking"] == {"type": "disabled"}

    def test_a_non_reasoning_model_sends_nothing(self):
        _, client = _run(
            _provider(),
            _model(reasoning=False),
            [{"role": "user", "content": "hi"}],
            options={"reasoning": "high"},
        )
        assert "thinking" not in client.requests[0]

    def test_transport_and_internal_options_never_reach_the_wire(self):
        _, client = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            options={
                "api_key": "sk-ant-test",
                "reasoning": None,
                "abort_signal": object(),
                "request_timeout": 30,
                "stream": True,
            },
        )
        request = client.requests[0]
        for leaked in ("api_key", "reasoning", "abort_signal", "request_timeout", "stream"):
            assert leaked not in request

    def test_a_sampling_option_the_sdk_dropped_names_itself_and_the_escape_hatch(self):
        # The SDK removed temperature/top_p/top_k from messages.stream(), so
        # splatting one in is a TypeError raised inside τ before any request
        # exists. τ answers in its own words instead, and points at the one
        # place an operator can still send it.
        with pytest.raises(ValueError) as exc:
            _run(
                _provider(),
                _model(),
                [{"role": "user", "content": "hi"}],
                options={"temperature": 0.5},
            )
        message = str(exc.value)
        assert "temperature" in message
        assert "extra_body" in message

    def test_extra_body_reaches_the_wire_split_by_what_the_sdk_declares(self):
        _, client = _run(
            _provider(),
            _model(extra_body={"stop_sequences": ["STOP"], "top_k": 40}),
            [{"role": "user", "content": "hi"}],
        )
        request = client.requests[0]
        # Declared by the SDK, so it rides as that keyword argument and a
        # per-call option can still override it.
        assert request["stop_sequences"] == ["STOP"]
        # Undeclared, so it rides in the body, where the SERVER answers for it.
        assert request["extra_body"] == {"top_k": 40}

    def test_a_per_call_option_still_beats_model_extra_body(self):
        _, client = _run(
            _provider(),
            _model(extra_body={"stop_sequences": ["FROM_CONFIG"]}),
            [{"role": "user", "content": "hi"}],
            options={"stop_sequences": ["FROM_CALL"]},
        )
        assert client.requests[0]["stop_sequences"] == ["FROM_CALL"]

    def test_an_sdk_that_accepts_anything_is_left_alone(self):
        # A stub — or a future SDK — whose stream() declares **kwargs has
        # nothing to route around, so nothing is rerouted and nothing raises.
        client = _FakeClient((), _final())

        class _Permissive:
            def stream(_self, **kwargs):
                client.requests.append(kwargs)
                return _FakeStreamManager(_FakeStream((), _final()))

        client.messages = _Permissive()
        provider = _provider()
        provider._client = client

        async def drive():
            stream = await provider.stream_chat(
                _model(extra_body={"top_k": 40}),
                [{"role": "user", "content": "hi"}],
                None,
                {"temperature": 0.5},
            )
            return [event async for event in stream]

        asyncio.run(drive())
        request = client.requests[0]
        assert request["temperature"] == 0.5
        assert request["top_k"] == 40
        assert "extra_body" not in request

    def test_tools_ride_as_input_schema(self):
        _, client = _run(
            _provider(),
            _model(),
            [{"role": "user", "content": "hi"}],
            tools=[_tool()],
        )
        assert client.requests[0]["tools"][0]["input_schema"] == {"type": "object"}


class TestRefusals:
    def test_a_missing_api_key_raises_before_any_request(self):
        provider = AnthropicMessagesProvider(api_key=None)

        async def drive():
            return await provider.stream_chat(_model(), [{"role": "user", "content": "hi"}])

        with pytest.raises(ValueError, match="No API key"):
            asyncio.run(drive())

    def test_a_constrained_call_raises_rather_than_running_unconstrained(self):
        """S6 — the Messages API has no decode-constraint parameter, so a
        best-effort attempt would return an unconstrained generation as if it
        were constrained."""

        class _Constraints:
            def has_constraint(self):
                return True

        provider = _provider()

        async def drive():
            return await provider.stream_chat(
                _model(),
                [{"role": "user", "content": "hi"}],
                options={"constraints": _Constraints()},
            )

        with pytest.raises(ValueError, match="no decode-constraint"):
            asyncio.run(drive())

    def test_the_module_never_imports_the_openai_compat_machinery(self):
        """S7 — tau_llm.compat answers OpenAI-wire-only questions. The gate is
        that this module does not participate."""
        source = anthropic_mod.__file__ or ""
        assert source
        with open(source) as handle:
            text = handle.read()
        assert "resolve_compat" not in text
        assert "detect_compat" not in text


class TestTheExtraIsOptional:
    """O5 — the SDK is `tau-llm[anthropic]`, not a hard dependency.

    Everything up to the first request must work without it: importing
    ``tau_llm``, importing this provider module, registering the wire protocol,
    resolving a Model through the registry, and constructing a provider. Only
    ``_get_client`` needs the SDK, and only then.
    """

    def test_the_sdk_is_never_imported_at_module_scope(self):
        """A module-scope import would make `import tau_llm` require the extra.

        Checked against the source rather than ``sys.modules``: the SDK may
        legitimately be installed in the environment running these tests, so its
        presence there proves nothing about where this module imports it.
        """
        with open(anthropic_mod.__file__) as handle:
            lines = handle.read().splitlines()
        at_module_scope = [
            line for line in lines if line.startswith(("import anthropic", "from anthropic"))
        ]
        assert not at_module_scope, (
            f"{at_module_scope} sits at module scope, so `import tau_llm` would require "
            "the optional extra. Import the SDK inside _get_client instead."
        )

    def test_constructing_a_provider_needs_no_sdk(self):
        """The factory runs at dispatch time, before any request."""
        provider = get_api_factory("anthropic-messages")(
            provider_id="anthropic",
            name="Anthropic",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
        )
        assert provider._client is None

    def test_a_missing_sdk_names_the_extra(self, monkeypatch):
        """Not a bare ModuleNotFoundError from an unrelated import."""
        import builtins

        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name == "anthropic":
                raise ModuleNotFoundError("No module named 'anthropic'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        with pytest.raises(ModuleNotFoundError, match=r"tau-llm\[anthropic\]"):
            _provider()._get_client()

    def test_the_stub_signature_matches_the_installed_sdk(self):
        """The one test that keeps ``SDK_STREAM_PARAMS`` honest.

        Every other test in this file drives a stub. If the tuple the stub is
        generated from drifts from the real ``AsyncMessages.stream``, the suite
        goes back to proving nothing about what the SDK accepts — which is the
        state that let the ``temperature`` TypeError ship. Skipped rather than
        failed when the optional extra is absent: the rest of this module is
        required to pass without it.
        """
        pytest.importorskip("anthropic")
        from anthropic import AsyncAnthropic

        declared = anthropic_mod._accepted_stream_params(
            AsyncAnthropic(api_key="sk-ant-test").messages.stream
        )
        assert declared == frozenset(SDK_STREAM_PARAMS), (
            "the installed anthropic SDK's messages.stream() signature has moved; "
            f"update SDK_STREAM_PARAMS. Added: {sorted(declared - set(SDK_STREAM_PARAMS))}, "
            f"removed: {sorted(set(SDK_STREAM_PARAMS) - declared)}."
        )


class TestLifetime:
    def test_aclose_closes_the_sdk_client_and_is_idempotent(self):
        provider = _provider()
        client = _FakeClient([], _final())
        provider._client = client
        asyncio.run(provider.aclose())
        assert client.closed is True
        asyncio.run(provider.aclose())  # a second close must not raise

    def test_aclose_on_an_unused_provider_is_a_no_op(self):
        asyncio.run(_provider().aclose())
