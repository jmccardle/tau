"""τ-llm Anthropic provider: the ``anthropic-messages`` wire protocol.

Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md — S1, S2, S4, S5, S6, O5.
pi parity: ``~/Development/pi/packages/ai/src/api/anthropic-messages.ts``.

This is the second wire protocol τ speaks. §1 of the design doc governs how it
relates to the first: **keep the pipeline OpenAI-shaped, overload minimally for
Anthropic peculiarities.** τ's internal message model, its event vocabulary and
its finalize contract stay as they are, and this module adapts itself to them.

**What this client is built on (O5).** The official ``anthropic`` SDK, declared
as the optional extra ``tau-llm[anthropic]`` and imported lazily inside
:meth:`AnthropicMessagesProvider._get_client` so that ``import tau_llm`` — and a
τ install that never talks to Anthropic — does not require it. That diverges from
``providers/openai.py``, which speaks its wire over ``httpx`` directly. The
reason is drift, not convenience: the Messages request surface moved repeatedly
across 2025-2026 (``budget_tokens`` went from required to rejected, effort moved
into ``output_config``, ``thinking.display`` changed default), and each of those
is a silent behaviour change for a hand-rolled client that does not track it. The
SDK also owns SSE parsing, block accumulation and signature round-tripping, which
is most of what a hand-rolled client would be.

The cost is real and is accepted: a second HTTP stack, with its own pooling and
retries, living under ``client.aclose_providers``. :meth:`aclose` closes it.

**What this client deliberately does NOT do.**

* No grammar path (S6). ``Model.grammar_dialect`` defaults to ``None``, which
  already means "raise on a constraint-carrying call", and this provider never
  sets it. The Messages API exposes no decode-constraint parameter, so a
  best-effort implementation would reintroduce exactly the silent-ignore failure
  the gate exists to prevent.
* No compat detection (S7). ``tau_llm.compat`` answers OpenAI-wire questions.
  This module never imports it, which is the whole of the gate.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from functools import lru_cache
from typing import TYPE_CHECKING, Any, AsyncIterator

from tau_llm.models import clamp_thinking_level
from tau_llm.providers.base import Provider, split_tool_result_content
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
    UserMessage,
)

if TYPE_CHECKING:  # pragma: no cover — import cost, not behaviour
    from anthropic import AsyncAnthropic

_logger = logging.getLogger(__name__)

#: The wire protocol id this module implements, as it appears in ``Model.api``.
API = "anthropic-messages"

#: Top-level key under which an Anthropic thinking payload rides on
#: ``ThinkingContent.thinking_signature`` (S4). The dict form exists precisely so
#: that this blob never reaches the OpenAI writer's ``result[signature] = …``
#: line, where it would be written as a JSON field name.
SIGNATURE_NAMESPACE = "anthropic"

#: Anthropic ``stop_reason`` → τ ``AssistantMessage.stop_reason``.
#:
#: ``refusal`` maps to ``error`` on purpose. A refusal arrives as HTTP 200 with a
#: ``stop_details`` category and little or no content; reporting it as ``stop``
#: would hand a caller an empty successful answer, and ``ctx.complete()`` would
#: not raise. ``error`` is the τ stop_reason that ``completion.py`` turns into a
#: ``CompletionFailed`` the caller can see (Fail-Early).
_STOP_REASONS: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "toolUse",
    "refusal": "error",
}

#: Warn-once bookkeeping, keyed by model id. Both conditions below persist for a
#: whole session, so warning per request would warn on every turn.
_WARNED_REPLAY_OVERRIDE: set[str] = set()
_WARNED_UNSIGNED_THINKING: set[str] = set()

#: Per-call option keys that configure THIS PROVIDER rather than the request:
#: a transport credential, a τ-internal thinking level, a cancellation handle, a
#: constraint object, an HTTP-client setting, and a transport mode. Same list the
#: OpenAI provider strips (``openai.py:1552``); none of them may reach the wire.
_INTERNAL_OPTION_KEYS = frozenset(
    {
        "api_key",
        "reasoning",
        "abort_signal",
        "constraints",
        "request_timeout",
        "stream",
    }
)


def _accepted_stream_params(stream: Any) -> frozenset[str] | None:
    """Return the keyword arguments this SDK's ``messages.stream`` declares.

    ``None`` means "accepts anything": the signature has a ``**kwargs``, so
    there is nothing to route around.

    The OpenAI provider builds a JSON body and posts it, so a key it does not
    recognise is the SERVER's business and the server answers. This provider
    calls a typed Python method instead, and ``anthropic`` 1.0.0 both removed
    ``temperature``/``top_p``/``top_k`` from that signature and declares no
    ``**kwargs`` — so splatting one in is a ``TypeError`` raised inside τ before
    any request exists, naming neither the model nor the fix.

    Asking the INSTALLED SDK what it accepts is what lets this module answer in
    its own words, and it stays correct across the SDK drift that this module's
    docstring cites as the reason for depending on the SDK at all. The callable
    τ is about to invoke is passed in, rather than a class imported here, so the
    answer describes that object and nothing else.
    """
    # Cache against the underlying function: a bound method is a fresh object on
    # every attribute access, so the method itself is not a usable cache key.
    return _accepted_params_of(getattr(stream, "__func__", stream))


@lru_cache(maxsize=8)
def _accepted_params_of(function: Any) -> frozenset[str] | None:
    params = inspect.signature(function).parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return None
    return frozenset(
        p.name
        for p in params
        if p.name != "self" and p.kind is not inspect.Parameter.VAR_POSITIONAL
    )


def signature_payload(signature: str = "", *, data: str = "") -> dict[str, Any]:
    """Build the ``thinking_signature`` payload for an Anthropic thinking block.

    Exactly one of ``signature`` (a plain thinking block) or ``data`` (a
    ``redacted_thinking`` block, whose opaque payload rides on a different field
    of a different block type) is populated. Returning one namespaced dict for
    both is what lets ``redacted_thinking`` round-trip without τ growing a
    fourth content-block type (S4).
    """
    inner: dict[str, Any] = {"redacted": bool(data)}
    if data:
        inner["data"] = data
    else:
        inner["signature"] = signature
    return {SIGNATURE_NAMESPACE: inner}


def read_signature_payload(signature: Any) -> dict[str, Any] | None:
    """Return the Anthropic payload inside a ``thinking_signature``, or None.

    None means "this block has nothing this provider can replay": no signature
    at all, a bare ``str`` field name from the OpenAI path, or a dict payload
    belonging to some other provider. All three are the S2 case.
    """
    if not isinstance(signature, dict):
        return None
    inner = signature.get(SIGNATURE_NAMESPACE)
    return inner if isinstance(inner, dict) else None


class AnthropicMessagesProvider(Provider):
    """Speaks the Anthropic Messages API on behalf of τ.

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
        self.base_url = base_url or "https://api.anthropic.com"
        self.request_timeout = request_timeout
        self._client: AsyncAnthropic | None = None

    # ── transport ────────────────────────────────────────────────────────

    def _get_client(self) -> AsyncAnthropic:
        """Build (once) the SDK client this instance streams through.

        The import is here rather than at module scope so that the extra is
        genuinely optional: ``import tau_llm`` must work without it, and the
        error a user gets for a missing extra must name the extra rather than
        surfacing as a bare ``ModuleNotFoundError`` from an unrelated import.
        """
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ModuleNotFoundError as exc:  # pragma: no cover — env-dependent
                raise ModuleNotFoundError(
                    "The anthropic-messages wire protocol needs the official SDK, "
                    "which τ declares as an optional extra. Install it with: "
                    "pip install 'ffwf-tau-llm[anthropic]'"
                ) from exc

            self._client = AsyncAnthropic(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.request_timeout,
            )
        return self._client

    async def aclose(self) -> None:
        """Release the SDK client's connection pool.

        Idempotent: ``client.aclose_providers`` may close an instance that never
        issued a request, and may close the same instance twice.
        """
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    # ── message conversion ───────────────────────────────────────────────

    def _replay_scope(self, model: Model) -> None:
        """Warn when ``Model.reasoning_replay`` says something Anthropic cannot do.

        S1. Anthropic requires the CURRENT tool-use sequence's thinking blocks to
        travel back with their signatures, and discards thinking from prior
        turns. That is exactly what τ's default ``"turn"`` already does, so the
        default needs no change. The other two settings are not honoured:

        * ``"off"`` would strip signatures Anthropic requires inside the current
          tool loop, which breaks the request rather than trimming it.
        * ``"all"`` sends prior-turn thinking that the API discards — wasteful,
          not wrong.

        Neither is obeyed. Choosing Anthropic is an express act; setting
        ``reasoning_replay="off"`` alongside it is a mistake rather than an
        instruction. This is the one place in τ where a stated config value is
        deliberately overridden, which is why it is logged with its reason and
        recorded in the design doc — so it is not later mistaken for a bug.
        """
        if model.reasoning_replay == "turn":
            return
        detail = (
            f"model {model.id!r} sets reasoning_replay={model.reasoning_replay!r}, which the "
            "Anthropic Messages API cannot honour: thinking blocks from the current tool-use "
            "sequence must be replayed WITH their signatures, and blocks from prior turns are "
            "discarded by the API regardless. Using 'turn' behaviour instead"
        )
        if model.strict_reasoning_formats:
            raise ValueError(
                f"{detail}. Refusing to continue because "
                "models.<name>.strict_reasoning_formats is set."
            )
        if model.id not in _WARNED_REPLAY_OVERRIDE:
            _WARNED_REPLAY_OVERRIDE.add(model.id)
            _logger.warning("%s (docs/ANTHROPIC-GOOGLE-CLIENTS.md S1).", detail)

    def _on_unsigned_thinking(self, model: Model) -> None:
        """Handle a thinking block with no Anthropic signature (S2).

        Reachable four legitimate ways: an aborted stream, a block persisted
        before signatures were captured, a message an extension synthesised, or a
        session that changed models mid-conversation. Raising would cost the user
        the whole session over a provider quirk; converting the block to text
        costs a signature the API was going to reject anyway, and leaves the data
        structure able to return to the model that produced it.
        """
        detail = (
            f"thinking block for model {model.id!r} carries no Anthropic signature, so it "
            "cannot be replayed as a thinking block. Sending it as text instead"
        )
        if model.strict_reasoning_formats:
            raise ValueError(
                f"{detail}. Refusing to continue because "
                "models.<name>.strict_reasoning_formats is set."
            )
        if model.id not in _WARNED_UNSIGNED_THINKING:
            _WARNED_UNSIGNED_THINKING.add(model.id)
            _logger.warning("%s (docs/ANTHROPIC-GOOGLE-CLIENTS.md S2).", detail)

    def _convert_messages(self, messages: list, model: Model) -> tuple[str, list[dict[str, Any]]]:
        """Convert τ messages to ``(system, messages)`` for the Messages API.

        Three shape differences from the OpenAI path drive everything here:

        1. **System is a parameter, not a role.** τ carries a system prompt as a
           dict message with ``role: "system"`` (the OpenAI converter's
           pass-through branch). Those are lifted out and joined.
        2. **Tool results are user content.** Anthropic has no ``tool`` role; a
           result is a ``tool_result`` block inside a user message. Consecutive
           results MUST land in ONE user message — splitting the results of a
           parallel tool call across several messages teaches the model to stop
           making parallel calls.
        3. **Thinking is a first-class block with a signature**, not a sibling
           field keyed by name. See :meth:`_replay_scope` and
           :meth:`_on_unsigned_thinking`.
        """
        self._replay_scope(model)

        system_parts: list[str] = []
        out: list[dict[str, Any]] = []

        # Reasoning-replay boundary, computed exactly as the OpenAI converter
        # computes it: the last USER turn. A tool result is not a user turn, so
        # the whole in-progress assistant/tool sequence sits after this index —
        # which is what makes "turn" replay the current tool loop's thinking and
        # drop everything older.
        last_user_idx = -1
        for i, msg in enumerate(messages):
            if isinstance(msg, UserMessage) or (
                isinstance(msg, dict) and msg.get("role") == "user"
            ):
                last_user_idx = i

        for i, msg in enumerate(messages):
            role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
            include_reasoning = i > last_user_idx

            if role == "system":
                text = msg.get("content", "") if isinstance(msg, dict) else ""
                if isinstance(text, list):
                    text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
                if text:
                    system_parts.append(str(text))
                continue

            if role in ("toolResult", "tool"):
                block = self._tool_result_block(msg)
                # Merge into the previous user message when that message is
                # itself a run of tool results (see point 2 above).
                if out and out[-1]["role"] == "user" and _is_tool_result_run(out[-1]):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue

            if role == "assistant":
                blocks = self._assistant_blocks(msg, model, include_reasoning)
                # An assistant turn that serialises to nothing cannot be sent —
                # the API rejects empty content. It only arises when the message
                # held prior-turn thinking and nothing else, which carries no
                # information the model needs back.
                if not blocks:
                    _logger.debug("dropping assistant message %d: no blocks survived conversion", i)
                    continue
                out.append({"role": "assistant", "content": blocks})
                continue

            out.append({"role": "user", "content": self._user_blocks(msg)})

        return "\n\n".join(system_parts), out

    def _user_blocks(self, msg: Any) -> list[dict[str, Any]]:
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if isinstance(content, str):
            return [{"type": "text", "text": content}]

        blocks: list[dict[str, Any]] = []
        for block in content or []:
            if isinstance(block, TextContent):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                blocks.append(_image_block(block.mime_type, block.data))
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    blocks.append({"type": "text", "text": block.get("text", "")})
                elif btype == "image":
                    blocks.append(_image_block(block.get("mime_type", ""), block.get("data", "")))
            elif isinstance(block, str):
                blocks.append({"type": "text", "text": block})
        return blocks or [{"type": "text", "text": ""}]

    def _assistant_blocks(
        self, msg: Any, model: Model, include_reasoning: bool
    ) -> list[dict[str, Any]]:
        """Convert one assistant message's content to Anthropic blocks.

        Thinking comes first, matching both the stream order and what the API
        expects of a replayed tool-use turn.
        """
        content = msg.get("content", []) if isinstance(msg, dict) else getattr(msg, "content", [])
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else []

        thinking: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []

        for block in content or []:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "thinking":
                    converted = self._thinking_block(
                        block.get("thinking", ""),
                        block.get("thinking_signature", ""),
                        model,
                        include_reasoning,
                    )
                    _place(converted, thinking, rest)
                elif btype == "text":
                    if block.get("text"):
                        rest.append({"type": "text", "text": block["text"]})
                elif btype == "toolCall":
                    rest.append(
                        {
                            "type": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input": block.get("arguments", {}) or {},
                        }
                    )
            elif isinstance(block, ThinkingContent):
                converted = self._thinking_block(
                    block.thinking, block.thinking_signature, model, include_reasoning
                )
                _place(converted, thinking, rest)
            elif isinstance(block, TextContent):
                if block.text:
                    rest.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCall):
                rest.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.arguments or {},
                    }
                )

        return thinking + rest

    def _thinking_block(
        self, thinking: str, signature: Any, model: Model, include_reasoning: bool
    ) -> dict[str, Any] | None:
        """One thinking block, as Anthropic wants it — or None to drop it.

        Dropped when the replay scope excludes it: this is a PRIOR turn's
        chain-of-thought, which the API discards anyway. Sending it as text
        instead would inject the model's private reasoning into the visible
        conversation, which is a different thing from replaying it.
        """
        if not include_reasoning:
            return None

        payload = read_signature_payload(signature)
        if payload is None:
            # S2 — no usable signature. Text keeps the turn intact.
            if not thinking:
                return None
            self._on_unsigned_thinking(model)
            return {"type": "text", "text": thinking}

        if payload.get("redacted"):
            return {"type": "redacted_thinking", "data": payload.get("data", "")}
        return {
            "type": "thinking",
            "thinking": thinking,
            "signature": payload.get("signature", ""),
        }

    def _tool_result_block(self, msg: Any) -> dict[str, Any]:
        """One tool result as an Anthropic ``tool_result`` block.

        Images ride INSIDE the block, which is where the Messages API takes them:
        ``content`` is either a string or a list of ``text``/``image`` blocks, and
        an ``image`` block carries ``source: {type: "base64", media_type, data}``.
        pi does the same (``anthropic-messages.ts`` ``convertContentBlocks``), so
        no separate user turn is needed here and the parallel-call run is never
        split — the problem the OpenAI client has to work around does not exist
        on this wire.

        This used to collect text and drop every image silently, which meant a
        vision model was handed the tool's prose and nothing to look at.

        Args:
            msg: A ``ToolResultMessage`` or its ``model_dump()``ed dict.

        Returns:
            The ``tool_result`` block. ``content`` stays a plain string when
            there are no images, so text-only results are unchanged on the wire.
        """
        if isinstance(msg, dict):
            tool_call_id = msg.get("tool_call_id", "")
            is_error = bool(msg.get("is_error", False))
            content = msg.get("content", "")
        else:
            tool_call_id = getattr(msg, "tool_call_id", "")
            is_error = bool(getattr(msg, "is_error", False))
            content = getattr(msg, "content", "")

        parts, images = split_tool_result_content(content)
        text = " ".join(p for p in parts if p)

        payload: Any
        if images:
            payload = [{"type": "text", "text": text}] if text else []
            payload += [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                }
                for mime, data in images
            ]
        else:
            payload = text

        return {
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": payload,
            "is_error": is_error,
        }

    def _convert_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        """τ tool specs → Anthropic tool definitions.

        The only rename is ``parameters`` → ``input_schema``; the JSON Schema
        itself travels unchanged.
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in tools
        ]

    # ── request ──────────────────────────────────────────────────────────

    def _thinking_params(self, model: Model, options: dict) -> dict[str, Any]:
        """Map τ's reasoning level onto the Messages API's thinking controls.

        τ's level enum predates this API and is OpenAI-shaped, so the mapping is
        stated rather than inferred:

        * a level other than ``"off"`` → ``thinking={"type": "adaptive"}`` plus
          ``output_config={"effort": level}``. Adaptive is the only on-mode the
          current models accept; the fixed ``budget_tokens`` budget is removed
          and returns a 400, so τ never sends one.
        * ``"off"`` → ``thinking={"type": "disabled"}``, which is what the
          operator asked for. It is sent rather than silently upgraded to
          adaptive; some models reject it at the highest effort settings, and a
          400 naming that is better than τ quietly running a mode nobody asked
          for (Fail-Early).
        * no ``reasoning`` option, or a model that does not declare reasoning →
          nothing sent, and the API's own default applies.

        ``Model.thinking_level_map`` still overrides the effort string per model,
        which is how an operator reaches an effort name τ's enum does not carry.
        """
        requested = options.get("reasoning")
        if requested is None or not getattr(model, "reasoning", False):
            return {}

        clamped = clamp_thinking_level(model, requested)
        mapped = (model.thinking_level_map or {}).get(clamped, clamped)
        if clamped == "off":
            return {"thinking": {"type": "disabled"}}
        if not isinstance(mapped, str):
            raise ValueError(
                f"Model {model.id!r}: thinking_level_map[{clamped!r}] must be a string "
                f"effort level on the anthropic-messages wire; got {mapped!r}."
            )
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": mapped}}

    async def stream_chat(
        self,
        model: Model,
        messages: list,
        tools: list[ToolSpec] | None = None,
        options: dict | None = None,
    ) -> AsyncIterator[Any]:
        """Stream a completion from the Anthropic Messages API.

        Returns an async iterator of the same τ streaming events the OpenAI
        provider produces — ``TextDeltaEvent``, ``ThinkingDeltaEvent``,
        ``ToolCallDeltaEvent``, ``DoneEvent``, ``ErrorEvent`` — so
        ``tau_llm.client`` and everything above it cannot tell which wire ran.
        """
        options = options or {}

        api_key = self.api_key or options.get("api_key")
        if not api_key:
            raise ValueError(
                f"No API key for provider: {getattr(model, 'provider', 'anthropic')}. "
                "Set ANTHROPIC_API_KEY, pass api_key=..., or configure it in "
                "~/.tau/config.json."
            )
        self.api_key = api_key

        if options.get("constraints") is not None and options["constraints"].has_constraint():
            # S6. Not a "not yet" — the Messages API exposes no decode-constraint
            # parameter at all, so there is nothing to send and no way to verify
            # a constraint held. Raising here says so; a best-effort attempt
            # would return an unconstrained generation as a constrained one.
            raise ValueError(
                f"Model {model.id!r} speaks {API!r}, which has no decode-constraint "
                "parameter. A constrained call cannot be honoured on this wire. "
                "(Refusing to send it anyway: the result would be an unconstrained "
                "generation returned as if it were constrained.)"
            )

        system, anthropic_messages = self._convert_messages(messages, model)

        request: dict[str, Any] = {
            "model": model.id,
            "max_tokens": model.max_tokens,
            "messages": anthropic_messages,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = self._convert_tools(tools)
        request.update(self._thinking_params(model, options))

        client = self._get_client()
        accepted = _accepted_stream_params(client.messages.stream)

        # Model.extra_body is the operator's escape hatch for anything this
        # module does not model; per-call options win over it, as on the OpenAI
        # path. Transport-only and τ-internal keys never reach the wire.
        #
        # Both dicts are SPLIT rather than splatted. A key the SDK declares is
        # passed as that keyword argument, which is what keeps the per-call-wins
        # precedence intact — the SDK merges `extra_body` over the named
        # arguments, so a key sitting in `extra_body` would beat the per-call
        # option meant to override it. A key the SDK does not declare goes into
        # `extra_body`, where it lands in the JSON body and the SERVER decides,
        # which is the same contract the OpenAI path gives `Model.extra_body`.
        extra_body: dict[str, Any] = {}
        if accepted is not None:
            for key, value in model.extra_body.items():
                (request if key in accepted else extra_body)[key] = value
        else:
            request.update(model.extra_body)

        per_call = {k: v for k, v in options.items() if k not in _INTERNAL_OPTION_KEYS}
        # `extra_body` is itself one of the SDK's named parameters, so a per-call
        # one would REPLACE the model's rather than override it key-by-key.
        extra_body.update(per_call.pop("extra_body", {}))

        if accepted is not None:
            # Fail-Early. τ will not guess that an undeclared keyword argument
            # belongs in the body: `Model.extra_body` is the express way to say
            # so, and it is one line of config away. The alternative — quietly
            # rerouting it — turns a caller's `temperature` into a 400 from a
            # model that removed the parameter, which reads as a τ bug.
            unknown = sorted(k for k in per_call if k not in accepted)
            if unknown:
                raise ValueError(
                    f"Model {model.id!r} speaks {API!r}, and the installed anthropic "
                    f"SDK's messages.stream() does not accept {unknown} as keyword "
                    "argument(s). The Messages API removed temperature, top_p and "
                    "top_k on the current models (they return 400) and the SDK "
                    "dropped them from its signature. If this endpoint does accept "
                    "them, send them explicitly via models.<name>.extra_body in "
                    f"~/.tau/config.json. Accepted here: {sorted(accepted)}."
                )
        request.update(per_call)
        if extra_body:
            request["extra_body"] = extra_body

        abort_signal = options.get("abort_signal")

        async def event_generator() -> AsyncIterator[Any]:
            state = _StreamState(model=model, provider=self)
            try:
                async with client.messages.stream(**request) as stream:
                    async for event in stream:
                        if abort_signal is not None and getattr(abort_signal, "aborted", False):
                            yield DoneEvent(
                                final=state.build_message(stop_reason="aborted"),
                                usage=state.usage,
                            )
                            return

                        # Branch on ``event.type`` directly rather than through a
                        # local: it is the union's Literal discriminator, so this
                        # is what narrows the SDK's event type to the member that
                        # actually carries ``.text`` / ``.thinking`` /
                        # ``.signature``. Every other member — the raw
                        # message/content-block events, citations, and the
                        # ``input_json`` fragments the SDK accumulates into the
                        # final message for us — needs nothing here.
                        if event.type == "text":
                            state.text_parts.append(event.text)
                            yield TextDeltaEvent(delta=event.text, partial=state.build_message())
                        elif event.type == "thinking":
                            state.thinking_parts.append(event.thinking)
                            yield ThinkingDeltaEvent(
                                delta=event.thinking, partial=state.build_message()
                            )
                        elif event.type == "signature":
                            state.signature = event.signature

                    final = await stream.get_final_message()

                final_msg = state.finalize(final)

                # One tool-call delta per call, derived from the finished
                # message. The SDK accumulates ``input_json`` fragments into the
                # final block, so — unlike the OpenAI path — there is no partial
                # argument buffer to re-parse here.
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

    def __init__(self, model: Model, provider: AnthropicMessagesProvider) -> None:
        self.model = model
        self.provider = provider
        self.text_parts: list[str] = []
        self.thinking_parts: list[str] = []
        self.signature: str = ""
        self.usage = Usage()

    def _blocks(self) -> list[Any]:
        """``[thinking?, text?]`` — one consolidated block each.

        One block per kind rather than one per fragment, matching the OpenAI
        path: a block per fragment bloats persistence and makes the TUI's
        reasoning suffix-diff re-emit the whole trace on every update.
        """
        blocks: list[Any] = []
        if self.thinking_parts:
            blocks.append(
                ThinkingContent(
                    thinking="".join(self.thinking_parts),
                    thinking_signature=signature_payload(self.signature),
                )
            )
        if self.text_parts:
            blocks.append(TextContent(text="".join(self.text_parts)))
        return blocks

    def build_message(self, stop_reason: str = "stop") -> AssistantMessage:
        return AssistantMessage(
            content=self._blocks(),
            api=API,
            provider=self.model.provider,
            model=self.model.id,
            usage=self.usage,
            stop_reason=stop_reason,  # type: ignore[arg-type]
            timestamp=int(time.time() * 1000),
        )

    def finalize(self, final: Any) -> AssistantMessage:
        """Build the τ message from the SDK's completed ``Message``.

        The SDK's accumulation is authoritative here, not this object's: it has
        the parsed tool inputs and the per-block signatures, which the delta
        events do not carry block-by-block.
        """
        blocks: list[Any] = []
        for block in final.content:
            btype = getattr(block, "type", "")
            if btype == "thinking":
                blocks.append(
                    ThinkingContent(
                        thinking=block.thinking,
                        thinking_signature=signature_payload(block.signature or ""),
                    )
                )
            elif btype == "redacted_thinking":
                # No readable text exists for a redacted block — the payload is
                # the whole of it, and inventing a placeholder would be a lie
                # about what the model said.
                blocks.append(
                    ThinkingContent(
                        thinking="",
                        thinking_signature=signature_payload(data=block.data),
                    )
                )
            elif btype == "text":
                blocks.append(TextContent(text=block.text))
            elif btype == "tool_use":
                if not str(block.name).strip():
                    raise ValueError(
                        f"Tool call {block.id!r} arrived with no tool name "
                        f"(model {self.model.id!r} at {self.provider.base_url!r}). "
                        "Refusing to execute a nameless call."
                    )
                blocks.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        usage = _usage_from_anthropic(final.usage)
        raw_stop = getattr(final, "stop_reason", None)
        stop_reason = _STOP_REASONS.get(raw_stop or "", "stop")
        error_message = None
        if raw_stop == "refusal":
            details = getattr(final, "stop_details", None)
            category = getattr(details, "category", None)
            explanation = getattr(details, "explanation", None)
            error_message = f"the model declined this request (category={category!r})" + (
                f": {explanation}" if explanation else ""
            )
        elif raw_stop is not None and raw_stop not in _STOP_REASONS:
            # An unmapped stop_reason is a wire the client does not fully know —
            # `pause_turn`, say, which means the turn is resumable and NOT
            # finished. Reporting it as a clean stop would hand a caller a
            # truncated answer that looks complete.
            stop_reason = "error"
            error_message = (
                f"unhandled Anthropic stop_reason {raw_stop!r}; τ cannot tell whether this "
                "turn is complete, so it is reported as an error rather than as a stop."
            )

        return AssistantMessage(
            content=blocks,
            api=API,
            provider=self.model.provider,
            model=getattr(final, "model", self.model.id),
            response_id=getattr(final, "id", None),
            usage=usage,
            stop_reason=stop_reason,  # type: ignore[arg-type]
            error_message=error_message,
            timestamp=int(time.time() * 1000),
        )


def _usage_from_anthropic(usage: Any) -> Usage:
    """Map the SDK's usage onto τ's :class:`Usage`.

    Anthropic reports cache reads and cache writes as separate counters and
    excludes both from ``input_tokens``. τ's ``total_tokens`` is computed here
    rather than read, because the API reports no total.
    """
    if usage is None:
        return Usage()
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        total_tokens=input_tokens + output_tokens + cache_read + cache_write,
    )


def _image_block(mime_type: str, data: str) -> dict[str, Any]:
    """An Anthropic base64 image block.

    τ stores image data already base64-encoded, optionally behind a ``data:``
    URI prefix (``ImageContent.data``); the prefix is stripped because the API
    wants the payload alone.
    """
    if data.startswith("data:"):
        data = data.split(",", 1)[1] if "," in data else data
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mime_type, "data": data},
    }


def _is_tool_result_run(message: dict[str, Any]) -> bool:
    """True when this user message holds only ``tool_result`` blocks.

    Guards the merge in :meth:`_convert_messages`: results join a preceding run
    of results, never a real user turn that happens to sit next to them.
    """
    content = message.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _place(
    block: dict[str, Any] | None, thinking: list[dict[str, Any]], rest: list[dict[str, Any]]
) -> None:
    """File a converted thinking block: thinking blocks lead, text follows."""
    if block is None:
        return
    (thinking if block["type"] in ("thinking", "redacted_thinking") else rest).append(block)
