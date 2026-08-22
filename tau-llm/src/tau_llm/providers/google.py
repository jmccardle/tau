"""The Google Generative AI wire protocol (``generateContent``), for τ.

Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md — S3, S5, S6, S7, S8, O1, O2, O4.

Emits the same τ streaming events as every other provider, so nothing above
``tau_llm.client`` can tell which wire ran.

WHY THE OFFICIAL SDK (O6, decided the way O5 was)
--------------------------------------------------
Built on ``google-genai`` rather than on httpx, as an optional extra
``tau-llm[google]`` imported lazily. O5 chose the SDK for Anthropic on a drift
argument that was, at the time, a prediction. Here it is a measurement:

* ``thought_signature`` on a ``functionCall`` was OPTIONAL on Gemini 2.5 and is
  VALIDATED on Gemini 3 — a change that turns working code into 400s.
* Google's own two documentation pages currently contradict each other on
  whether the field exists on function calls at all (see O4's "documentation
  trap").
* Seven independent frameworks shipped the wrong behaviour through that change.

A hand-rolled client is exactly what those seven had. The SDK also models
``Part.thought_signature`` and ``FunctionResponse.parts`` directly, so τ is not
re-deriving a wire format whose rules moved twice.

WHAT THIS MODULE DOES NOT DO
-----------------------------
* **No constrained decoding** (S6). ``generateContent`` has ``response_schema``,
  but τ's constraint contract is decode-level; a schema on the response is not
  the same promise, and returning an unconstrained generation as a constrained
  one is the failure S6 exists to prevent.
* **No compat detection** (S7). ``detect_compat``/``resolve_compat`` describe
  OpenAI-endpoint quirks. Nothing here imports them, and
  ``tests/test_compat_is_openai_only.py`` pins that.
* **No model-name table** (O2). Capabilities are ``Model`` fields whose defaults
  encode a measurement. Nothing here matches on a model id.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, cast

from tau_llm.models import clamp_thinking_level
from tau_llm.providers.base import Provider
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
    Usage,
)

if TYPE_CHECKING:  # pragma: no cover — import cost, not behaviour
    from google.genai import Client as GoogleClient

_logger = logging.getLogger(__name__)

#: The wire protocol id this module implements, as it appears in ``Model.api``.
API = "google-generative-ai"

#: Top-level key under which a Google replay token rides on
#: ``ToolCall.provider_signature`` (S8).
SIGNATURE_NAMESPACE = "google"

#: τ's stop-reason vocabulary, named so the mapping below is checked rather than
#: assembled from bare strings — an unmapped reason must reach the ``error``
#: branch, and a typo that silently produced ``"stop"`` is the exact failure that
#: branch exists to prevent.
StopReason = Literal["stop", "length", "toolUse", "error", "aborted"]

#: Google ``finish_reason`` → τ ``AssistantMessage.stop_reason``.
#:
#: ``SAFETY``/``RECITATION``/``BLOCKLIST``/``PROHIBITED_CONTENT`` map to
#: ``error`` for the reason Anthropic's ``refusal`` does: they arrive on a
#: successful HTTP response with little or no content, so reporting ``stop``
#: would hand the caller an empty successful answer and ``ctx.complete()`` would
#: not raise.
_STOP_REASONS: dict[str, StopReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "error",
    "RECITATION": "error",
    "BLOCKLIST": "error",
    "PROHIBITED_CONTENT": "error",
    "SPII": "error",
    "MALFORMED_FUNCTION_CALL": "error",
}

#: Warn-once bookkeeping, keyed by model id. Each condition persists for a whole
#: session, so warning per request would warn on every turn.
_WARNED_FOREIGN_SIGNATURE: set[str] = set()
_WARNED_UNSIGNED_TOOL_CALL: set[str] = set()


def signature_payload(thought_signature: str) -> dict[str, Any]:
    """Wrap a base64 thought signature for ``ToolCall.provider_signature``.

    Base64 TEXT, not bytes, even though the SDK's field is ``Optional[bytes]``.
    ``provider_signature`` is persisted to session JSONL, and bytes are not JSON;
    a resumed Gemini 3 session that lost its signature fails its next turn with a
    400. The conversion to bytes happens at the SDK boundary and nowhere else.
    """
    return {SIGNATURE_NAMESPACE: {"thought_signature": thought_signature}}


def read_signature_payload(signature: Any) -> str:
    """The base64 thought signature inside a ``provider_signature``, or "".

    Returns "" for a payload minted by another vendor rather than raising: the
    caller decides whether an absent signature is fatal, and for Gemini 3 that
    depends on the block's position (only the first call in a step needs one).
    """
    if not isinstance(signature, dict):
        return ""
    inner = signature.get(SIGNATURE_NAMESPACE)
    if not isinstance(inner, dict):
        return ""
    value = inner.get("thought_signature", "")
    return value if isinstance(value, str) else ""


class GoogleGenerativeAIProvider(Provider):
    """Speaks Google's ``generateContent`` API on behalf of τ.

    One instance is bound to one endpoint and one credential, like every
    ``Provider`` — ``tau_llm.client`` pools them by (vendor, api, base_url, key
    hash), so this class must never mutate per-call state onto ``self``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url or "https://generativelanguage.googleapis.com"
        self.request_timeout = request_timeout
        self._client: GoogleClient | None = None

    # ── transport ────────────────────────────────────────────────────────

    def _get_client(self) -> GoogleClient:
        """Build (once) the SDK client this instance streams through.

        The import is here rather than at module scope so the extra is genuinely
        optional: ``import tau_llm`` must work without it, and a missing extra
        must name the extra rather than surfacing as a bare ``ModuleNotFoundError``
        from an unrelated import.
        """
        if self._client is None:
            try:
                from google.genai import Client
            except ModuleNotFoundError as exc:  # pragma: no cover — env-dependent
                raise ModuleNotFoundError(
                    "The google-generative-ai wire protocol needs the official SDK, "
                    "which τ declares as an optional extra. Install it with: "
                    "pip install 'ffwf-tau-llm[google]'"
                ) from exc

            # cast: the SDK types these as TypedDicts, which mypy will not accept
            # from a dict[str, Any] built at runtime. It accepts them fine.
            self._client = Client(
                api_key=self.api_key, http_options=cast(Any, self._http_options())
            )
        return self._client

    def _http_options(self) -> dict[str, Any]:
        """Transport settings, including a non-default endpoint if one was set.

        ``base_url`` is only forwarded when it differs from Google's own, so an
        ordinary install does not pin an endpoint the SDK would otherwise pick
        (and keep current) for itself.
        """
        options: dict[str, Any] = {}
        if self.base_url and self.base_url != "https://generativelanguage.googleapis.com":
            options["base_url"] = self.base_url
        if self.request_timeout is not None:
            # The SDK counts this in milliseconds; τ carries seconds everywhere.
            options["timeout"] = int(self.request_timeout * 1000)
        return options

    async def aclose(self) -> None:
        """Release the SDK client.

        Idempotent: ``client.aclose_providers`` may close an instance that never
        issued a request, and may close the same instance twice.
        """
        client, self._client = self._client, None
        if client is not None:
            aclose = getattr(getattr(client, "aio", None), "aclose", None)
            if aclose is not None:
                await aclose()

    # ── signatures ───────────────────────────────────────────────────────

    def _on_foreign_signature(self, model: Model, namespaces: str) -> None:
        """A tool call carrying another vendor's replay token.

        Same three-way shape as the OpenAI writer's S8 guard: raise under
        ``strict_reasoning_formats``, else warn once and drop. Dropping is right
        here — a foreign token is not a Google signature, and sending one would
        be worse than sending none.
        """
        detail = (
            f"tool call replayed to {model.id!r} carries a {namespaces!r} signature, "
            "not a Google one. It was minted by another provider (models mixed "
            "within one session, or a resumed session), and Google would reject or "
            "ignore it."
        )
        if model.strict_reasoning_formats:
            raise ValueError(
                f"{detail} Refusing to continue because "
                "models.<name>.strict_reasoning_formats is set."
            )
        if model.id not in _WARNED_FOREIGN_SIGNATURE:
            _WARNED_FOREIGN_SIGNATURE.add(model.id)
            _logger.warning(
                "%s Replaying the call without it. Set "
                "models.<name>.strict_reasoning_formats to raise instead. "
                "(docs/ANTHROPIC-GOOGLE-CLIENTS.md S8)",
                detail,
            )

    def _on_unsigned_tool_call(self, model: Model) -> None:
        """The first tool call of a replayed step has no thought signature.

        On Gemini 3 this request will fail with 400 ("Function call ... is
        missing a thought_signature"). τ warns BEFORE sending rather than letting
        the caller read Google's message cold, because the cause is upstream —
        the call was made by a different model, or by a τ version that did not
        persist the field, or an extension synthesised it.

        Not raised by default: the same conversation is legal on Gemini 2.5,
        where the field is optional, and τ cannot tell the two apart without a
        model-name table O2 removed on purpose.
        """
        detail = (
            f"replaying a tool call to {model.id!r} with no Google thought signature. "
            "Gemini 3 validates this and will reject the request; Gemini 2.5 will "
            "accept it. The call was not produced by this model, or predates τ "
            "persisting the field."
        )
        if model.strict_reasoning_formats:
            raise ValueError(
                f"{detail} Refusing to continue because "
                "models.<name>.strict_reasoning_formats is set."
            )
        if model.id not in _WARNED_UNSIGNED_TOOL_CALL:
            _WARNED_UNSIGNED_TOOL_CALL.add(model.id)
            _logger.warning("%s (docs/ANTHROPIC-GOOGLE-CLIENTS.md O4)", detail)

    # ── message conversion ───────────────────────────────────────────────

    def _convert_messages(self, messages: list, model: Model) -> tuple[str, list[dict[str, Any]]]:
        """τ messages → (system instruction, Google ``contents``).

        Google carries the system prompt outside ``contents``, so ``role:
        "system"`` messages are lifted out and joined. Everything else becomes a
        ``user`` or ``model`` content.
        """
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)

            if role == "system":
                text = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
                if isinstance(text, str) and text:
                    system_parts.append(text)
                continue

            if role == "toolResult":
                self._append_tool_result(contents, msg, model)
                continue

            if role == "assistant":
                parts = self._assistant_parts(msg, model)
                # An assistant turn that serialises to nothing is dropped rather
                # than sent empty: Google rejects a content with no parts, and an
                # empty turn carries no information anyway.
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue

            parts = self._user_parts(msg)
            if parts:
                contents.append({"role": "user", "parts": parts})

        return "\n\n".join(system_parts), contents

    def _user_parts(self, msg: Any) -> list[dict[str, Any]]:
        """A user message's content as Google parts."""
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
        if isinstance(content, str):
            return [{"text": content}] if content else []

        parts: list[dict[str, Any]] = []
        for block in content or []:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if btype == "text":
                text = block.get("text", "") if isinstance(block, dict) else block.text
                if text:
                    parts.append({"text": text})
            elif btype == "image":
                if isinstance(block, dict):
                    parts.append(_inline_data(block.get("mime_type", ""), block.get("data", "")))
                elif isinstance(block, ImageContent):
                    parts.append(_inline_data(block.mime_type, block.data))
        return parts

    def _assistant_parts(self, msg: Any, model: Model) -> list[dict[str, Any]]:
        """An assistant turn as Google parts, signatures placed per O4.

        The signature rule is positional and this is the only place that knows
        the positions: **only the first ``functionCall`` part of the turn carries
        a ``thought_signature``**. Google says parallel calls after the first omit
        it, so copying one onto each would be inventing signatures for calls that
        never had them.
        """
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
        if isinstance(content, str):
            return [{"text": content}] if content else []

        include_reasoning = model.reasoning_replay != "off"
        parts: list[dict[str, Any]] = []
        seen_function_call = False

        for block in content or []:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)

            if btype == "text":
                text = block.get("text", "") if isinstance(block, dict) else block.text
                if text:
                    parts.append({"text": text})

            elif btype == "thinking":
                # Thinking parts DO follow reasoning_replay: Google calls
                # returning their signatures "recommended", with no validation
                # error. This is the discretionary case the knob was designed
                # for — unlike the functionCall signature below. (O4)
                if not include_reasoning:
                    continue
                thinking = block.get("thinking", "") if isinstance(block, dict) else block.thinking
                if thinking:
                    parts.append({"text": thinking, "thought": True})

            elif btype == "toolCall":
                part = self._function_call_part(block, model, first=not seen_function_call)
                seen_function_call = True
                parts.append(part)

        return parts

    def _function_call_part(self, block: Any, model: Model, *, first: bool) -> dict[str, Any]:
        """One ``functionCall`` part, with its signature if it is the first.

        ``first`` is not a style choice. Google validates the signature on the
        first call of each step and expects the rest to omit it.
        """
        if isinstance(block, dict):
            call_id = block.get("id", "")
            name = block.get("name", "")
            args = block.get("arguments", {})
            raw_signature = block.get("provider_signature") or {}
        else:
            call_id = block.id
            name = block.name
            args = block.arguments
            raw_signature = block.provider_signature

        call: dict[str, Any] = {"name": name, "args": args}
        if model.requires_tool_call_id and call_id:
            call["id"] = _sanitise_id(call_id)

        part: dict[str, Any] = {"function_call": call}

        if not first:
            # Later parallel calls carry no signature by protocol. Nothing to
            # warn about, and nothing to send.
            return part

        signature = read_signature_payload(raw_signature)
        if signature:
            part["thought_signature"] = signature
        elif raw_signature:
            self._on_foreign_signature(model, ",".join(sorted(raw_signature)))
        else:
            self._on_unsigned_tool_call(model)
        return part

    def _append_tool_result(self, contents: list[dict[str, Any]], msg: Any, model: Model) -> None:
        """Add a ``functionResponse``, merging into the previous user turn.

        Google has no tool role, and pi merges consecutive results into ONE user
        turn (``google-shared.ts:261``). τ does the same, for the reason the
        Anthropic client does: splitting the results of a parallel call across
        several turns teaches the model to stop making parallel calls.
        """
        name = msg.get("toolName") if isinstance(msg, dict) else getattr(msg, "toolName", "")
        call_id = msg.get("toolCallId") if isinstance(msg, dict) else getattr(msg, "toolCallId", "")
        is_error = msg.get("isError") if isinstance(msg, dict) else getattr(msg, "isError", False)
        output = msg.get("output") if isinstance(msg, dict) else getattr(msg, "output", "")

        text, images = _split_tool_output(output)
        # The documented key pair: "output" for success, "error" for failure.
        payload_key = "error" if is_error else "output"
        value = text or ("(see attached image)" if images else "")

        response: dict[str, Any] = {"name": name, "response": {payload_key: value}}
        if model.requires_tool_call_id and call_id:
            response["id"] = _sanitise_id(call_id)

        nested = images and model.supports_multimodal_function_response
        if nested:
            response["parts"] = [_inline_data(m, d) for m, d in images]

        part = {"function_response": response}
        if contents and contents[-1]["role"] == "user" and _holds_tool_results(contents[-1]):
            contents[-1]["parts"].append(part)
        else:
            contents.append({"role": "user", "parts": [part]})

        if images and not nested:
            # The conservative branch (O2 default): a separate user turn, which
            # every model accepts. pi does the same below Gemini 3.
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {"text": "Tool result image:"},
                        *[_inline_data(m, d) for m, d in images],
                    ],
                }
            )

    def _convert_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        """τ tools → one Google ``Tool`` holding every declaration."""
        declarations = [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in tools
        ]
        return [{"function_declarations": declarations}]

    def _thinking_config(self, model: Model, options: dict) -> dict[str, Any]:
        """``thinking_config``, or nothing when no reasoning was asked for.

        ``include_thoughts`` is what makes Google return thought summaries at
        all; without it a reasoning model still reasons but τ sees none of it,
        and ``ThinkingDeltaEvent`` would never fire.
        """
        level = options.get("reasoning")
        if not level or not model.reasoning:
            return {}
        if clamp_thinking_level(model, level) == "off":
            return {"thinking_config": {"include_thoughts": False, "thinking_budget": 0}}
        return {"thinking_config": {"include_thoughts": True}}

    # ── streaming ────────────────────────────────────────────────────────

    async def stream_chat(
        self,
        model: Model,
        messages: list,
        tools: list[ToolSpec] | None = None,
        options: dict | None = None,
    ) -> AsyncIterator[Any]:
        """Stream a completion from Google's ``generateContent``.

        Returns an async iterator of the same τ streaming events every other
        provider produces, so ``tau_llm.client`` and everything above it cannot
        tell which wire ran.
        """
        options = options or {}

        api_key = self.api_key or options.get("api_key")
        if not api_key:
            raise ValueError(
                f"No API key for provider: {getattr(model, 'provider', 'google')}. "
                "Set GEMINI_API_KEY, pass api_key=..., or configure it in "
                "~/.tau/config.json."
            )
        self.api_key = api_key

        if options.get("constraints") is not None and options["constraints"].has_constraint():
            # S6. `response_schema` exists but is not τ's contract: it shapes the
            # response, it does not constrain decoding, and a caller who asked
            # for a constrained generation would receive an unconstrained one
            # described as constrained.
            raise ValueError(
                f"Model {model.id!r} speaks {API!r}, which has no decode-constraint "
                "parameter. A constrained call cannot be honoured on this wire. "
                "(Refusing to send it anyway: the result would be an unconstrained "
                "generation returned as if it were constrained.)"
            )

        system, contents = self._convert_messages(messages, model)

        config: dict[str, Any] = {"max_output_tokens": model.max_tokens}
        if system:
            config["system_instruction"] = system
        if tools:
            config["tools"] = self._convert_tools(tools)
            # Automatic function calling OFF, explicitly. The SDK can run the
            # tool loop itself — calling Python callables and feeding results
            # back — and τ owns that loop: the agent loop executes tools, emits
            # tool_execution_start/end, and enforces permissions. An SDK that
            # quietly did it instead would bypass all of it.
            #
            # MEASURED: `_extra_utils.should_disable_afc()` returns False for a
            # config without this flag and True with it, so AFC is ON by default
            # and this line is what turns it off — not belt and braces. Passing
            # declarations rather than callables happens to leave it nothing to
            # execute, but "inert because of how we call it" is not "off".
            #
            # The SDK still logs its "direct use of AFC is not recommended"
            # warning on the first streamed tool request either way: it is
            # emitted once per process before the disable flag is consulted. The
            # warning is therefore not a signal about this setting.
            config["automatic_function_calling"] = {"disable": True}
        config.update(self._thinking_config(model, options))
        # Model.extra_body is the operator's escape hatch for anything this
        # module does not model; per-call options win over it, as on every other
        # path. Transport-only and τ-internal keys never reach the wire.
        config.update(model.extra_body)
        config.update(
            {
                k: v
                for k, v in options.items()
                if k
                not in (
                    "api_key",
                    "reasoning",
                    "abort_signal",
                    "constraints",
                    "request_timeout",
                    "stream",
                )
            }
        )

        client = self._get_client()
        abort_signal = options.get("abort_signal")

        async def event_generator() -> AsyncIterator[Any]:
            state = _StreamState(model=model, provider=self)
            try:
                stream = await client.aio.models.generate_content_stream(
                    model=model.id,
                    contents=cast(Any, _encode_signatures(contents)),
                    config=cast(Any, config),
                )
                async for chunk in stream:
                    if abort_signal is not None and getattr(abort_signal, "aborted", False):
                        yield DoneEvent(
                            final=state.build_message(stop_reason="aborted"), usage=state.usage
                        )
                        return

                    for event in state.consume(chunk):
                        yield event

                final_msg = state.finalize()

                for pos, tc in enumerate(final_msg.get_tool_calls()):
                    yield ToolCallDeltaEvent(
                        delta={
                            "index": pos,
                            "id": tc.id,
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        },
                        partial=final_msg,
                    )

                yield DoneEvent(final=final_msg, usage=final_msg.usage)
                return

            except Exception as exc:
                # Name the model and the endpoint as well as the fault: a fleet
                # behind one τ config can have several, and the answer is not in
                # the exception.
                yield ErrorEvent(
                    message=(
                        f"Streaming error from model {model.id!r} at "
                        f"{self.base_url!r}: {type(exc).__name__}: {exc}"
                    )
                )
                return

        return event_generator()


class _StreamState:
    """Accumulates one completion so a partial message can be built per delta."""

    def __init__(self, model: Model, provider: GoogleGenerativeAIProvider) -> None:
        self.model = model
        self.provider = provider
        self.text_parts: list[str] = []
        self.thinking_parts: list[str] = []
        self.tool_calls: list[ToolCall] = []
        self.usage = Usage()
        self.finish_reason = ""
        self.error_message = ""
        #: Signature for the NEXT functionCall part, retained across deltas.
        #: pi keeps `retainThoughtSignature` for the same reason: some backends
        #: send the signature only on a part's first delta, and a later delta's
        #: absent one must not overwrite it.
        self._pending_signature = ""

    def consume(self, chunk: Any) -> list[Any]:
        """Fold one streamed chunk in, returning the τ events it produced."""
        events: list[Any] = []

        usage = getattr(chunk, "usage_metadata", None)
        if usage is not None:
            self.usage = _usage_from_google(usage)

        for candidate in getattr(chunk, "candidates", None) or []:
            reason = getattr(candidate, "finish_reason", None)
            if reason:
                self.finish_reason = getattr(reason, "name", None) or str(reason)

            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                events.extend(self._consume_part(part))

        return events

    def _consume_part(self, part: Any) -> list[Any]:
        events: list[Any] = []

        signature = getattr(part, "thought_signature", None)
        if signature:
            self._pending_signature = _b64(signature)

        call = getattr(part, "function_call", None)
        if call is not None:
            self.tool_calls.append(
                ToolCall(
                    id=getattr(call, "id", "") or f"call_{len(self.tool_calls)}",
                    name=getattr(call, "name", "") or "",
                    arguments=dict(getattr(call, "args", None) or {}),
                    # Only the first call of the step carries one; the retained
                    # value is consumed here so a later call does not inherit it.
                    provider_signature=(
                        signature_payload(self._pending_signature)
                        if self._pending_signature
                        else {}
                    ),
                )
            )
            self._pending_signature = ""
            return events

        text = getattr(part, "text", None)
        if not text:
            return events

        if getattr(part, "thought", False):
            self.thinking_parts.append(text)
            events.append(ThinkingDeltaEvent(delta=text, partial=self.build_message()))
        else:
            self.text_parts.append(text)
            events.append(TextDeltaEvent(delta=text, partial=self.build_message()))
        return events

    def _blocks(self) -> list[Any]:
        blocks: list[Any] = []
        thinking = "".join(self.thinking_parts)
        if thinking:
            blocks.append(ThinkingContent(thinking=thinking))
        text = "".join(self.text_parts)
        if text:
            blocks.append(TextContent(text=text))
        blocks.extend(self.tool_calls)
        return blocks

    def build_message(self, stop_reason: StopReason = "stop") -> AssistantMessage:
        return AssistantMessage(
            role="assistant",
            content=self._blocks(),
            api=API,
            provider=self.provider.id or "google",
            model=self.model.id,
            usage=self.usage,
            stop_reason=stop_reason,
            timestamp=int(time.time()),
        )

    def finalize(self) -> AssistantMessage:
        """The finished message, with the stop reason mapped.

        An UNMAPPED finish reason becomes ``error``, not ``stop``. Google adds
        them over time, and guessing ``stop`` for one τ has never seen returns a
        truncated or blocked answer as a complete one.
        """
        stop_reason: StopReason | None
        if self.tool_calls and self.finish_reason == "STOP":
            stop_reason = "toolUse"
        else:
            stop_reason = _STOP_REASONS.get(self.finish_reason)

        if stop_reason is None:
            stop_reason = "error"
            self.error_message = (
                f"unmapped Google finish_reason {self.finish_reason!r} — treated as an "
                "error rather than a completed turn, because τ cannot tell whether the "
                "answer is whole."
            )
        elif stop_reason == "error" and not self.error_message:
            self.error_message = f"Google stopped generation: {self.finish_reason}"

        message = self.build_message(stop_reason=stop_reason)
        if self.error_message:
            message.error_message = self.error_message
        return message


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _b64(signature: Any) -> str:
    """The SDK's ``bytes`` signature as base64 text for persistence.

    Already-text signatures pass through: the REST wire carries base64, and a
    caller replaying a persisted message hands back exactly what τ stored.
    """
    if isinstance(signature, bytes):
        return base64.b64encode(signature).decode("ascii")
    return str(signature)


def _encode_signatures(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn stored base64 signatures back into the ``bytes`` the SDK expects.

    Done in one pass at the boundary so that everything above it — the
    converter, the session log, ``ToolCall.provider_signature`` — deals only in
    JSON-safe text.
    """
    encoded: list[dict[str, Any]] = []
    for content in contents:
        parts = []
        for part in content["parts"]:
            signature = part.get("thought_signature")
            if isinstance(signature, str):
                part = {**part, "thought_signature": base64.b64decode(signature)}
            parts.append(part)
        encoded.append({**content, "parts": parts})
    return encoded


def _sanitise_id(call_id: str) -> str:
    """Google accepts ``[a-zA-Z0-9_-]`` ids up to 64 characters.

    Same normalisation pi applies (``google-shared.ts:133``). τ's own ids are
    already safe; an id minted by another provider, or by an extension, may not
    be, and an id Google rejects fails the whole request.
    """
    cleaned = "".join(c if c.isalnum() or c in "_-" else "_" for c in call_id)
    return cleaned[:64]


def _holds_tool_results(content: dict[str, Any]) -> bool:
    return any("function_response" in part for part in content.get("parts", []))


def _inline_data(mime_type: str, data: str) -> dict[str, Any]:
    return {"inline_data": {"mime_type": mime_type, "data": data}}


def _split_tool_output(output: Any) -> tuple[str, list[tuple[str, str]]]:
    """A tool result's output as (text, [(mime_type, data), ...])."""
    if isinstance(output, str):
        return output, []

    text_parts: list[str] = []
    images: list[tuple[str, str]] = []
    for block in output or []:
        btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.get("text", "") if isinstance(block, dict) else block.text)
        elif btype == "image":
            if isinstance(block, dict):
                images.append((block.get("mime_type", ""), block.get("data", "")))
            else:
                images.append((block.mime_type, block.data))
    return "".join(text_parts), images


def _usage_from_google(usage: Any) -> Usage:
    """Google usage → τ ``Usage``.

    ``thoughts_token_count`` is part of output for billing, and Google reports
    ``candidates_token_count`` without it, so the two are added rather than
    letting a reasoning turn under-report what it cost.
    """
    prompt = getattr(usage, "prompt_token_count", 0) or 0
    candidates = getattr(usage, "candidates_token_count", 0) or 0
    thoughts = getattr(usage, "thoughts_token_count", 0) or 0
    cached = getattr(usage, "cached_content_token_count", 0) or 0
    total = getattr(usage, "total_token_count", 0) or 0
    return Usage(
        input_tokens=prompt,
        output_tokens=candidates + thoughts,
        cache_read_tokens=cached,
        total_tokens=total or (prompt + candidates + thoughts),
    )
