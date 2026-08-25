"""The google-generative-ai provider.

Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md — S6, S7, S8, O1, O2, O4.

The SDK is stubbed rather than imported. ``google-genai`` is the optional extra
``tau-llm[google]``, and a test suite that needed it would quietly make the extra
mandatory — the same discipline the Anthropic suite keeps.

Most of these assert decisions that came from a measurement recorded in
``docs/probe-results/README-gemini-2026-08-22.md``, not from taste. Where that is
so, the test says which measurement, because the defaults are only defensible
with it.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import pytest

from tau_llm.providers import get_provider_spec, registered_apis
from tau_llm.providers import google as google_provider
from tau_llm.providers.google import (
    API,
    GoogleGenerativeAIProvider,
    read_signature_payload,
    signature_payload,
)
from tau_llm.types import Model, ToolCall

SIGNATURE_B64 = base64.b64encode(b"thought-bytes").decode("ascii")


@pytest.fixture(autouse=True)
def _forget_warnings() -> None:
    google_provider._WARNED_FOREIGN_SIGNATURE.clear()
    google_provider._WARNED_UNSIGNED_TOOL_CALL.clear()


def _model(**overrides: Any) -> Model:
    fields: dict[str, Any] = {
        "id": "gemini-3.6-flash",
        "name": "Gemini 3.6 Flash",
        "api": API,
        "provider": "gemini",
        "base_url": "https://generativelanguage.googleapis.com",
        "context_window": 1_000_000,
        "max_tokens": 8192,
    }
    fields.update(overrides)
    return Model(**fields)


def _provider() -> GoogleGenerativeAIProvider:
    return GoogleGenerativeAIProvider(api_key="k")


class _Tool:
    """The three members ToolSpec requires, and nothing else."""

    def __init__(self) -> None:
        self.name = "get_temperature"
        self.description = "Temperature in a city."
        self.parameters = {"type": "object", "properties": {"city": {"type": "string"}}}


def _assistant_with_calls(*calls: ToolCall) -> dict[str, Any]:
    return {"role": "assistant", "content": [c.model_dump() for c in calls]}


# ──────────────────────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────────────────────


def test_the_wire_protocol_is_registered() -> None:
    assert API in registered_apis()


def test_the_vendor_is_registered_as_gemini() -> None:
    """`gemini`, not `google` — that is what config.json entries already say.

    The shipped config template has carried ``"backend": "gemini"`` since before
    any Google client existed. Registering `google` would have left those broken.
    """
    spec = get_provider_spec("gemini")

    assert spec is not None
    assert spec.api == API
    assert spec.base_url == "https://generativelanguage.googleapis.com"


def test_gemini_api_key_is_preferred_over_google_api_key() -> None:
    """GOOGLE_API_KEY is set for many Google services; the specific name wins."""
    spec = get_provider_spec("gemini")

    assert spec is not None
    assert spec.api_key_env == ("GEMINI_API_KEY", "GOOGLE_API_KEY")


# ──────────────────────────────────────────────────────────────────────────
# The signature payload (S8)
# ──────────────────────────────────────────────────────────────────────────


def test_a_signature_round_trips_through_the_payload() -> None:
    assert read_signature_payload(signature_payload(SIGNATURE_B64)) == SIGNATURE_B64


def test_the_payload_is_json_safe() -> None:
    """The whole reason τ stores base64 text rather than the SDK's bytes.

    ``provider_signature`` is persisted to session JSONL. Bytes are not JSON, and
    a resumed Gemini 3 session that lost its signature fails its next turn.
    """
    import json

    assert json.loads(json.dumps(signature_payload(SIGNATURE_B64)))


def test_another_vendors_payload_reads_as_absent() -> None:
    """Not an exception: the caller decides whether absence is fatal, and for
    Gemini 3 that depends on the call's POSITION in the step."""
    assert read_signature_payload({"anthropic": {"signature": "x"}}) == ""


def test_a_non_dict_signature_reads_as_absent() -> None:
    assert read_signature_payload("a-bare-string") == ""


# ──────────────────────────────────────────────────────────────────────────
# O4 — where the signature goes, and where it must not
# ──────────────────────────────────────────────────────────────────────────


def test_the_first_tool_call_carries_its_signature() -> None:
    call = ToolCall(
        id="c1", name="read", arguments={}, provider_signature=signature_payload(SIGNATURE_B64)
    )
    _, contents = _provider()._convert_messages([_assistant_with_calls(call)], _model())

    assert contents[0]["parts"][0]["thought_signature"] == SIGNATURE_B64


def test_a_parallel_second_call_carries_none() -> None:
    """Protocol, not style. Google signs only the first functionCall part of a
    step and expects the rest to omit it, so copying it onto each would be
    inventing signatures for calls that never had one."""
    first = ToolCall(
        id="c1", name="read", arguments={}, provider_signature=signature_payload(SIGNATURE_B64)
    )
    second = ToolCall(
        id="c2", name="read", arguments={}, provider_signature=signature_payload("other")
    )
    _, contents = _provider()._convert_messages([_assistant_with_calls(first, second)], _model())

    assert "thought_signature" in contents[0]["parts"][0]
    assert "thought_signature" not in contents[0]["parts"][1]


def test_a_missing_signature_warns_before_google_rejects_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """τ names the upstream cause; Google's 400 only names the symptom."""
    call = ToolCall(id="c1", name="read", arguments={})
    with caplog.at_level(logging.WARNING):
        _provider()._convert_messages([_assistant_with_calls(call)], _model())

    assert "thought signature" in caplog.text
    assert "Gemini 3" in caplog.text


def test_a_missing_signature_warns_once_per_model(caplog: pytest.LogCaptureFixture) -> None:
    call = ToolCall(id="c1", name="read", arguments={})
    message = _assistant_with_calls(call)
    with caplog.at_level(logging.WARNING):
        _provider()._convert_messages([message], _model())
        _provider()._convert_messages([message], _model())

    assert len([r for r in caplog.records if "thought signature" in r.message]) == 1


def test_a_foreign_signature_is_dropped_not_forwarded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sending another vendor's token is worse than sending none."""
    call = ToolCall(
        id="c1", name="read", arguments={}, provider_signature={"anthropic": {"signature": "x"}}
    )
    with caplog.at_level(logging.WARNING):
        _, contents = _provider()._convert_messages([_assistant_with_calls(call)], _model())

    assert "thought_signature" not in contents[0]["parts"][0]
    assert "anthropic" in caplog.text


def test_strict_raises_on_a_foreign_signature() -> None:
    call = ToolCall(
        id="c1", name="read", arguments={}, provider_signature={"anthropic": {"signature": "x"}}
    )
    with pytest.raises(ValueError, match="strict_reasoning_formats"):
        _provider()._convert_messages(
            [_assistant_with_calls(call)], _model(strict_reasoning_formats=True)
        )


def test_reasoning_replay_off_does_not_drop_the_tool_signature() -> None:
    """The heart of O4.

    ``reasoning_replay`` governs chain-of-thought. This token is validated by the
    API, so letting the knob drop it would 400 every multi-turn tool
    conversation on Gemini 3 — including under τ's own default of "turn".
    """
    call = ToolCall(
        id="c1", name="read", arguments={}, provider_signature=signature_payload(SIGNATURE_B64)
    )
    _, contents = _provider()._convert_messages(
        [_assistant_with_calls(call)], _model(reasoning_replay="off")
    )

    assert contents[0]["parts"][0]["thought_signature"] == SIGNATURE_B64


def test_reasoning_replay_off_does_drop_thinking_parts() -> None:
    """The other half of the split: thinking IS discretionary, and Google says so
    (returning those signatures is "recommended", with no validation error)."""
    message = {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "deliberating"},
            {"type": "text", "text": "done"},
        ],
    }
    _, contents = _provider()._convert_messages([message], _model(reasoning_replay="off"))

    assert [p.get("text") for p in contents[0]["parts"]] == ["done"]


def test_thinking_is_replayed_by_default() -> None:
    message = {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "deliberating"}],
    }
    _, contents = _provider()._convert_messages([message], _model())

    assert contents[0]["parts"][0] == {"text": "deliberating", "thought": True}


# ──────────────────────────────────────────────────────────────────────────
# O2 — the measured defaults
# ──────────────────────────────────────────────────────────────────────────


def test_a_tool_call_id_is_sent_by_default() -> None:
    """MEASURED: accepted by every model tried, including one pi says takes no id.

    The permissive branch is the safe one — the opposite of what O2 assumed.
    """
    assert _model().requires_tool_call_id is True

    message = {
        "role": "toolResult",
        "toolName": "read",
        "toolCallId": "call_1",
        "output": "ok",
        "isError": False,
    }
    _, contents = _provider()._convert_messages([message], _model())

    assert contents[0]["parts"][0]["function_response"]["id"] == "call_1"


def test_the_id_can_be_turned_off_per_model() -> None:
    """The override O2 asked for. No table consults a model name to set it."""
    message = {"role": "toolResult", "toolName": "read", "toolCallId": "c", "output": "ok"}
    _, contents = _provider()._convert_messages([message], _model(requires_tool_call_id=False))

    assert "id" not in contents[0]["parts"][0]["function_response"]


def test_an_unsafe_id_is_sanitised() -> None:
    """An id Google rejects fails the whole request, and τ's ids are not the only
    ones that reach here — extensions and other providers mint them too."""
    message = {"role": "toolResult", "toolName": "read", "toolCallId": "a b/c:d", "output": "x"}
    _, contents = _provider()._convert_messages([message], _model())

    assert contents[0]["parts"][0]["function_response"]["id"] == "a_b_c_d"


def test_a_long_id_is_truncated_to_64_chars() -> None:
    message = {"role": "toolResult", "toolName": "r", "toolCallId": "x" * 200, "output": "y"}
    _, contents = _provider()._convert_messages([message], _model())

    assert len(contents[0]["parts"][0]["function_response"]["id"]) == 64


def test_images_go_in_a_separate_turn_by_default() -> None:
    """The conservative branch, which O2 already called clearly safe.

    Only one model was measured accepting the nested form, and one permissive
    data point does not earn a permissive default when this always works.
    """
    assert _model().supports_multimodal_function_response is False

    message = {
        "role": "toolResult",
        "toolName": "screenshot",
        "toolCallId": "c1",
        "output": [{"type": "image", "mime_type": "image/png", "data": "AAA"}],
    }
    _, contents = _provider()._convert_messages([message], _model())

    assert "parts" not in contents[0]["parts"][0]["function_response"]
    assert contents[1]["parts"][1]["inline_data"]["data"] == "AAA"


def test_images_nest_when_the_model_says_it_supports_it() -> None:
    message = {
        "role": "toolResult",
        "toolName": "screenshot",
        "toolCallId": "c1",
        "output": [{"type": "image", "mime_type": "image/png", "data": "AAA"}],
    }
    _, contents = _provider()._convert_messages(
        [message], _model(supports_multimodal_function_response=True)
    )

    assert contents[0]["parts"][0]["function_response"]["parts"][0]["inline_data"]["data"] == "AAA"
    assert len(contents) == 1


# ──────────────────────────────────────────────────────────────────────────
# Message conversion
# ──────────────────────────────────────────────────────────────────────────


def test_system_messages_are_lifted_out_of_contents() -> None:
    """Google carries the system prompt outside ``contents``."""
    messages = [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "hi"},
    ]
    system, contents = _provider()._convert_messages(messages, _model())

    assert system == "Be terse."
    assert [c["role"] for c in contents] == ["user"]


def test_several_system_messages_are_joined() -> None:
    messages = [
        {"role": "system", "content": "One."},
        {"role": "system", "content": "Two."},
    ]
    system, _ = _provider()._convert_messages(messages, _model())

    assert system == "One.\n\nTwo."


def test_consecutive_tool_results_merge_into_one_turn() -> None:
    """Splitting a parallel call's results across turns teaches the model to stop
    making parallel calls. pi merges for the same reason."""
    messages = [
        {"role": "toolResult", "toolName": "read", "toolCallId": "c1", "output": "a"},
        {"role": "toolResult", "toolName": "read", "toolCallId": "c2", "output": "b"},
    ]
    _, contents = _provider()._convert_messages(messages, _model())

    assert len(contents) == 1
    assert len(contents[0]["parts"]) == 2


def test_an_error_result_uses_the_error_key() -> None:
    message = {
        "role": "toolResult",
        "toolName": "read",
        "toolCallId": "c1",
        "output": "boom",
        "isError": True,
    }
    _, contents = _provider()._convert_messages([message], _model())

    assert contents[0]["parts"][0]["function_response"]["response"] == {"error": "boom"}


def test_an_empty_assistant_turn_is_dropped() -> None:
    """Google rejects a content with no parts, and the turn carries nothing."""
    messages = [{"role": "assistant", "content": []}, {"role": "user", "content": "hi"}]
    _, contents = _provider()._convert_messages(messages, _model())

    assert [c["role"] for c in contents] == ["user"]


def test_tools_become_one_declaration_list() -> None:
    converted = _provider()._convert_tools([_Tool()])

    assert len(converted) == 1
    assert converted[0]["function_declarations"][0]["name"] == "get_temperature"


async def test_automatic_function_calling_is_disabled_whenever_tools_are_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """τ owns the agent loop; the SDK must not run one of its own.

    MEASURED: the SDK's own ``should_disable_afc()`` answers False for a config
    without this flag, so AFC is ON by default. Left on, the SDK can execute
    tools and feed results back itself — bypassing τ's tool execution, its
    tool_execution_start/end events, and its permission checks.

    Pinned because nothing else would notice: τ passes declarations rather than
    callables, so AFC currently has nothing to execute and the bug would be
    silent until the day something passes a callable.
    """
    captured: dict[str, Any] = {}

    class _Models:
        async def generate_content_stream(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            raise RuntimeError("stop here — the request body is what this test reads")

    class _Client:
        aio = type("A", (), {"models": _Models()})()

    provider = _provider()
    monkeypatch.setattr(provider, "_get_client", lambda: _Client())

    stream = await provider.stream_chat(
        _model(), [{"role": "user", "content": "hi"}], tools=[_Tool()]
    )
    # The provider turns any exception into an ErrorEvent, so drain it.
    async for _ in stream:
        pass

    assert captured["config"]["automatic_function_calling"] == {"disable": True}


async def test_no_afc_key_is_sent_when_there_are_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request with no tools has no loop to hijack, and sending the key anyway
    would put a tools-only setting on every plain completion."""
    captured: dict[str, Any] = {}

    class _Models:
        async def generate_content_stream(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            raise RuntimeError("stop here")

    class _Client:
        aio = type("A", (), {"models": _Models()})()

    provider = _provider()
    monkeypatch.setattr(provider, "_get_client", lambda: _Client())

    stream = await provider.stream_chat(_model(), [{"role": "user", "content": "hi"}])
    async for _ in stream:
        pass

    assert "automatic_function_calling" not in captured["config"]


# ──────────────────────────────────────────────────────────────────────────
# Refusals
# ──────────────────────────────────────────────────────────────────────────


async def test_a_missing_key_raises() -> None:
    provider = GoogleGenerativeAIProvider(api_key=None)

    with pytest.raises(ValueError, match="No API key"):
        await provider.stream_chat(_model(), [{"role": "user", "content": "hi"}])


async def test_a_constrained_call_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """S6. response_schema exists but is not τ's decode-level contract, and
    returning an unconstrained generation as a constrained one is the failure."""

    class _Constraints:
        def has_constraint(self) -> bool:
            return True

    with pytest.raises(ValueError, match="decode-constraint"):
        await _provider().stream_chat(
            _model(), [{"role": "user", "content": "hi"}], options={"constraints": _Constraints()}
        )


def test_the_module_never_imports_compat() -> None:
    """S7, stated here as well as in the repo-wide AST test, because this is the
    module that would be tempted: a non-OpenAI wire with its own quirks.

    Checked against the module's IMPORTS rather than its text — the docstring
    names ``detect_compat`` to explain why it is absent, and a substring search
    would read that explanation as the violation.
    """
    import ast

    source = google_provider.__file__ or ""
    assert source
    with open(source) as handle:
        tree = ast.parse(handle.read())

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert "tau_llm.compat" not in imported
    assert "detect_compat" not in imported
    assert "resolve_compat" not in imported


def test_the_extra_is_genuinely_optional() -> None:
    """Constructing a provider must not need the SDK.

    ``import tau_llm`` works without ``tau-llm[google]``; only a request needs it,
    and the error then names the extra.
    """
    provider = GoogleGenerativeAIProvider(api_key="k")

    assert provider.base_url == "https://generativelanguage.googleapis.com"


# ──────────────────────────────────────────────────────────────────────────
# Streaming state
# ──────────────────────────────────────────────────────────────────────────


class _Part:
    def __init__(self, **fields: Any) -> None:
        self.text = fields.get("text")
        self.thought = fields.get("thought", False)
        self.thought_signature = fields.get("thought_signature")
        self.function_call = fields.get("function_call")


class _Call:
    def __init__(self, name: str, args: dict, call_id: str = "") -> None:
        self.name = name
        self.args = args
        self.id = call_id


class _Chunk:
    def __init__(self, parts: list[_Part], finish_reason: str = "") -> None:
        content = type("C", (), {"parts": parts})()
        candidate = type("D", (), {"content": content, "finish_reason": finish_reason})()
        self.candidates = [candidate]
        self.usage_metadata = None


def _state() -> Any:
    return google_provider._StreamState(model=_model(), provider=_provider())


def test_text_parts_become_text_deltas() -> None:
    state = _state()
    events = state.consume(_Chunk([_Part(text="hello")]))

    assert events[0].delta == "hello"


def test_thought_parts_become_thinking_deltas() -> None:
    state = _state()
    events = state.consume(_Chunk([_Part(text="pondering", thought=True)]))

    assert type(events[0]).__name__ == "ThinkingDeltaEvent"


def test_a_streamed_signature_lands_on_the_following_call() -> None:
    """The signature arrives on its own part before the call it belongs to."""
    state = _state()
    state.consume(_Chunk([_Part(thought_signature=b"sig")]))
    state.consume(_Chunk([_Part(function_call=_Call("read", {"path": "/x"}, "c1"))]))

    assert read_signature_payload(state.tool_calls[0].provider_signature) == base64.b64encode(
        b"sig"
    ).decode("ascii")


def test_a_second_call_does_not_inherit_the_first_signature() -> None:
    """Consuming it is what stops a parallel call claiming a signature it never
    had — the write side of the same rule the converter enforces."""
    state = _state()
    state.consume(_Chunk([_Part(thought_signature=b"sig")]))
    state.consume(_Chunk([_Part(function_call=_Call("read", {}, "c1"))]))
    state.consume(_Chunk([_Part(function_call=_Call("read", {}, "c2"))]))

    assert state.tool_calls[1].provider_signature == {}


def test_tool_use_is_reported_when_calls_are_present() -> None:
    state = _state()
    state.consume(_Chunk([_Part(function_call=_Call("read", {}, "c1"))], finish_reason="STOP"))

    assert state.finalize().stop_reason == "toolUse"


def test_max_tokens_maps_to_length() -> None:
    state = _state()
    state.consume(_Chunk([_Part(text="cut")], finish_reason="MAX_TOKENS"))

    assert state.finalize().stop_reason == "length"


def test_a_safety_stop_is_an_error() -> None:
    """HTTP 200 with little content. Reporting `stop` would hand the caller an
    empty successful answer and ctx.complete() would not raise."""
    state = _state()
    state.consume(_Chunk([], finish_reason="SAFETY"))
    final = state.finalize()

    assert final.stop_reason == "error"
    assert "SAFETY" in (final.error_message or "")


def test_an_unmapped_finish_reason_is_an_error_not_a_stop() -> None:
    """Google adds these over time. Guessing `stop` returns a possibly truncated
    answer as a complete one."""
    state = _state()
    state.consume(_Chunk([_Part(text="?")], finish_reason="SOMETHING_NEW_IN_2027"))
    final = state.finalize()

    assert final.stop_reason == "error"
    assert "SOMETHING_NEW_IN_2027" in (final.error_message or "")


# ──────────────────────────────────────────────────────────────────────────
# The SDK boundary
# ──────────────────────────────────────────────────────────────────────────


def test_stored_base64_becomes_bytes_for_the_sdk() -> None:
    """Text everywhere above; bytes only at the boundary, in one pass."""
    contents = [{"role": "model", "parts": [{"thought_signature": SIGNATURE_B64}]}]

    encoded = google_provider._encode_signatures(contents)

    assert encoded[0]["parts"][0]["thought_signature"] == b"thought-bytes"


def test_encoding_does_not_mutate_the_caller_s_contents() -> None:
    """The converter's output may be reused; the boundary must not scribble on it."""
    contents = [{"role": "model", "parts": [{"thought_signature": SIGNATURE_B64}]}]

    google_provider._encode_signatures(contents)

    assert contents[0]["parts"][0]["thought_signature"] == SIGNATURE_B64


def test_usage_counts_thinking_tokens_as_output() -> None:
    """Google reports candidates_token_count WITHOUT thoughts, so a reasoning
    turn would otherwise under-report what it cost."""
    usage = type(
        "U",
        (),
        {
            "prompt_token_count": 10,
            "candidates_token_count": 5,
            "thoughts_token_count": 7,
            "cached_content_token_count": 2,
            "total_token_count": 22,
        },
    )()

    converted = google_provider._usage_from_google(usage)

    assert converted.output_tokens == 12
    assert converted.cache_read_tokens == 2
    # prompt_token_count 10 INCLUDES the 2 cached, so input_tokens is the uncached
    # 8. Left at 10 the pair would count the cached span twice (pi subtracts too,
    # google-generative-ai.ts:227). The server's total is untouched.
    assert converted.input_tokens == 8
    assert converted.total_tokens == 22


def test_usage_never_reports_a_negative_input_when_the_whole_prompt_was_cached() -> None:
    usage = type(
        "U",
        (),
        {
            "prompt_token_count": 10,
            "candidates_token_count": 4,
            "thoughts_token_count": 0,
            "cached_content_token_count": 10,
            "total_token_count": 14,
        },
    )()

    converted = google_provider._usage_from_google(usage)

    assert (converted.input_tokens, converted.cache_read_tokens) == (0, 10)
