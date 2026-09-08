"""τ-llm providers.openai: OpenAI-completions provider.

Reference: PHASE-1-SUBPHASE-2.md, Phase 1 Subphase 2 — OpenAI Provider Implementation.

Implements OpenAICompletionsProvider, the only concrete provider. It:
1. Converts τ Message list to OpenAI API format
2. Converts τ tool specs (ToolSpec) to OpenAI function_call format
3. Converts OpenAI API responses back to τ AssistantMessage
4. Handles all error cases

Usage:
    provider = OpenAICompletionsProvider()
    stream = await provider.stream_chat(
        model=Model(id="gpt-4o", ...),
        messages=[UserMessage(content=[TextContent(text="hello")])],
    )
    async for event in stream:
        if event.type == "text_delta":
            print(event.delta, end="")
        elif event.type == "done":
            print(f"\nUsage: {event.usage}")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Literal

import httpx

from tau_llm import grammar as grammar_mod
from tau_llm.compat import ResolvedCompat, resolve_compat
from tau_llm.constraints import ConstraintViolation
from tau_llm.providers.base import Provider, split_tool_result_content
from tau_llm.json_parse import (
    parse_json_with_repair_info,
    parse_streaming_json,
)
from tau_llm.models import clamp_thinking_level
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
    Usage,
    UserMessage,
)

_logger = logging.getLogger(__name__)

_WARNED_FOREIGN_SIGNATURES: set[str] = set()

_WARNED_FOREIGN_TOOL_SIGNATURES: set[str] = set()

_MAX_ERROR_BODY_CHARS = 4000

_TRUNCATION_HINT = (
    "The model hit the output cap τ sent for it (max_tokens={max_tokens}); "
    "raise `max_tokens` on this model in ~/.tau/config.json, or lower the "
    "reasoning budget so the answer fits under it."
)


def _truncate_error_text(text: str, max_chars: int = _MAX_ERROR_BODY_CHARS) -> str:
    """Bound an error body, saying how much was dropped rather than eliding silently."""
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated {len(text) - max_chars} chars]"


def _describe_exception(e: BaseException) -> str:
    """Compose a NEVER-EMPTY description of an exception.

    ``str(httpx.ReadTimeout())`` is ``""``, and so is ``ConnectError()`` and
    ``RemoteProtocolError()`` — the three ways a dropped connection actually
    reaches this provider. Interpolating that into ``f"Streaming error: {e}"``
    produced ``RuntimeError: Streaming error: `` with nothing after the colon:
    an error report that names neither what failed nor where.

    So the exception TYPE always leads. ``ReadTimeout`` alone already tells an
    operator the connection stalled mid-body rather than never opening, which is
    a different fix (raise ``request_timeout`` vs. check the URL).

    HTTP status and response body are appended when the exception carries them
    — the shape of pi's ``normalizeProviderError``/``formatProviderError``
    (utils/error-body.ts:38-135), adapted to httpx rather than transliterated:
    httpx puts the status and body on ``e.response``, so one probe replaces
    pi's four SDK-specific field orders.

    This function must not raise. It runs only on the failure path, and an
    exception here would replace a bad error message with no error message.
    """
    parts = [type(e).__name__]

    detail = str(e).strip()
    if detail:
        parts.append(f": {detail}")

    try:
        response = getattr(e, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None
        body = response.text.strip() if response is not None else ""
    except Exception:  # pragma: no cover - defensive: never fail while reporting
        status, body = None, ""

    if status is not None:
        parts.append(f" [HTTP {status}]")
    if body and body not in detail:
        parts.append(f" body={_truncate_error_text(body)!r}")

    return "".join(parts)


@dataclass
class _ToolCallAccumulator:
    """Accumulates a single tool call across delta events.

    ``name`` and ``arguments_parts`` are accumulated by concatenation: OpenAI
    streams them as incremental fragments, one piece per chunk.
    """

    id: str = ""
    name: str = ""
    index: int | None = None
    arguments_parts: list[str] = field(default_factory=list)
    saw_anthropic_shape: bool = False


@dataclass
class _Accumulator:
    """Internal accumulator for building AssistantMessage during streaming.

    Accumulates text deltas, tool call arguments, and metadata
    across streaming events until the response is complete.

    Tool calls are kept in first-seen order (``tool_calls``) and indexed by both
    OpenAI stream ``index`` and tool-call ``id`` so that follow-up argument
    fragments — which carry only ``index`` — route to the right call.
    """

    text_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    thinking_signature: str = ""
    tool_calls: list[_ToolCallAccumulator] = field(default_factory=list)
    by_index: dict[int, _ToolCallAccumulator] = field(default_factory=dict)
    by_id: dict[str, _ToolCallAccumulator] = field(default_factory=dict)
    has_tool_calls: bool = False
    has_text: bool = False
    has_thinking: bool = False
    response_id: str | None = None


@dataclass
class _TransportState:
    """What a transport hands the SHARED finalize tail.

    The streaming and non-streaming transports (:meth:`~OpenAICompletionsProvider.
    _stream_transport` / ``_complete_transport``) differ only in how they fill an
    ``_Accumulator`` and this record; everything after them — the final message
    build, the constraint verification, the closing tool-call deltas, the
    ``DoneEvent`` — is one piece of code reading these fields. Passing them out
    through a mutable record rather than a return value is what lets a transport be
    an async generator (it must yield events as it goes) without a second finalize
    site, which is the defect this design exists to avoid.
    """

    usage_data: dict[str, Any] = field(default_factory=dict)
    timings_data: dict[str, Any] = field(default_factory=dict)
    stop_reason: Literal["stop", "length", "toolUse", "error", "aborted"] | None = None
    # The transport already yielded an ErrorEvent and there is nothing to finalize.
    failed: bool = False


_RESERVED_BODY_KEYS = frozenset({"model", "messages", "stream", "stream_options", "tools"})

_CONSTRAINT_BODY_KEYS = frozenset({"grammar", "json_schema", "response_format"})


def _guard_body_keys(source: str, keys: Iterable[str], *, model_id: str) -> None:
    """Reject transport and constraint fields arriving through a caller-supplied dict."""
    keyset = set(keys)

    reserved = _RESERVED_BODY_KEYS & keyset
    if reserved:
        hint = ""
        if "stream" in reserved:
            hint = (
                " Streaming mode is chosen by `Model.stream` "
                "(models.<name>.stream in ~/.tau/config.json) or the per-call "
                "`stream` option, not by a request-body key: τ has to KNOW which "
                "transport it is reading, and a body key would change the wire "
                "format underneath the SSE parser."
            )
        raise ValueError(
            f"Model {model_id!r}: {source} may not set τ transport fields "
            f"{sorted(reserved)}; it is for server decode/cache knobs "
            f"(cache_prompt, min_p, samplers, …).{hint}"
        )

    constraint = _CONSTRAINT_BODY_KEYS & keyset
    if constraint:
        raise ValueError(
            f"Model {model_id!r}: {source} may not set decode-constraint fields "
            f"{sorted(constraint)}. Pass a DecodeConstraints instead — it is the only "
            "path that capability-checks the model, refuses to collide with tools, and "
            "VERIFIES the output. Smuggled past it, a constraint the server drops "
            "(or silently overrides) comes back as an unconstrained generation "
            "masquerading as a constrained one."
        )


def _resolve_stream_mode(per_call: Any, model: Model) -> bool:
    """Decide whether this call streams. Precedence, narrowest first.

    ``options["stream"]`` (this call) → ``Model.stream`` (config) → ``True``.
    The same tiering as ``request_timeout``, and for the same reason: the mode is
    a property of the ENDPOINT (a gateway that does not implement SSE), which is
    configured per model, while a single call may still need the other mode.

    Fail-Early on a non-bool. The model tier arrives pre-validated (pydantic
    types the field), so this guard is really about the PER-CALL option, which no
    schema sees: ``stream="false"`` is truthy in Python, and coercing it would
    keep streaming against a backend that cannot stream — surfacing as an
    unreadable response body rather than as the bad argument it is. ``bool`` only;
    ``0``/``1`` are refused too, because accepting them means accepting ``2``.
    """
    value = per_call if per_call is not None else model.stream
    if not isinstance(value, bool):
        source = "the per-call `stream` option" if per_call is not None else "`Model.stream`"
        raise ValueError(
            f"Model {model.id!r}: {source} must be a bool (True = SSE streaming, "
            f"False = one buffered completion), got {value!r} ({type(value).__name__})."
        )
    return value


def _merge_thinking_fragment(
    payload: dict[str, Any], fragment: dict[str, Any], *, model_id: str
) -> None:
    """Merge a `thinking_level_map` body fragment into the request payload.

    Guarded like every other caller-supplied body dict. A fragment is a THIRD door
    into the payload alongside `Model.extra_body` and per-call options, and a door
    the constraint gates do not watch is a door around them — see `_guard_body_keys`
    for the three live reproductions that argument rests on.

    Merged one level deep rather than assigned, for the nested case: a model whose
    "off" fragment is ``{"chat_template_kwargs": {"enable_thinking": false}}`` and
    whose `extra_body` sets other `chat_template_kwargs` must get both. A flat
    assignment would silently drop the ones already there, which is the same
    silent-loss failure the fragment shape exists to fix one level up.

    The fragment wins on a key-by-key collision. Asking for a thinking level is an
    explicit, per-call act; `extra_body` is the model's static default. This matches
    what the string path has always done — `payload["reasoning_effort"] = …` has
    always overwritten whatever `extra_body` put there.
    """
    _guard_body_keys("thinking_level_map fragment", fragment.keys(), model_id=model_id)
    for key, value in fragment.items():
        existing = payload.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged = dict(existing)
            merged.update(value)
            payload[key] = merged
        else:
            payload[key] = value


def _message_text(message: AssistantMessage) -> str:
    """The assistant message's plain text — exactly what a constraint constrains.

    Thinking blocks and tool calls are excluded: a grammar constrains the *content*
    channel, and a reasoning model's ``reasoning_content`` is not part of it.

    NOT stripped. Whitespace inside a grammar is significant — ``fixed("yes ")``
    really does force the trailing space (verified live against llguidance) — so
    stripping here would mangle a correctly-constrained output into one that fails
    its own membership check, and τ would raise a ConstraintViolation blaming the
    server for damage τ itself did.
    """
    return "".join(b.text for b in message.content if isinstance(b, TextContent))


def _apply_constraints(
    payload: dict[str, Any],
    model: Model,
    constraints: Any,
    *,
    has_tools: bool,
) -> None:
    """Map a ``DecodeConstraints`` onto the request payload, gating first.

    Two gates, both Fail-Early, both verified live against llama.cpp master
    (CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §0.1):

    1. **Capability.** A constraint against a model that declares no
       ``grammar_dialect`` raises. The alternative is shipping a param that OpenAI
       would 400 on and that other servers silently IGNORE — and a silently-ignored
       grammar means an *unconstrained* generation returned as constrained, which is
       fabricated data.

    2. **Tools.** A constraint alongside a declared tools array raises unless
       ``tool_choice="none"``. The server rejects ``grammar`` + tools with a 400, but
       accepts ``json_schema`` + tools with a **200 while silently disabling tool
       calling** — the schema grammar wins and the model invents a schema-shaped
       answer instead of calling the tool. τ is the only line of defence for that
       case, so the raise covers both constraint kinds.
    """
    if constraints is None:
        return

    # tool_choice / extra_body ride along even with no actual decode constraint.
    if constraints.tool_choice is not None:
        payload["tool_choice"] = constraints.tool_choice
    if constraints.extra_body:
        _guard_body_keys(
            "DecodeConstraints.extra_body", constraints.extra_body.keys(), model_id=model.id
        )
        payload.update(constraints.extra_body)

    if not constraints.has_constraint():
        return

    if model.grammar_dialect is None:
        raise ValueError(
            f"Model {model.id!r} declares no grammar support, so a decode constraint "
            f"cannot be honoured. Set models.<name>.grammar to 'llguidance' or 'gbnf'. "
            "(Refusing to send it anyway: a server that ignores the constraint would "
            "return an unconstrained generation as if it were constrained.)"
        )

    if has_tools and payload.get("tool_choice") != "none":
        raise ValueError(
            f"Model {model.id!r}: a decode constraint cannot be combined with tools "
            "(the server's tool grammar and the constraint grammar collide). "
            "llama-server 400s on grammar+tools, and — worse — accepts "
            "json_schema+tools while silently disabling tool calling. "
            'Pass tool_choice="none" to constrain a turn that declares tools.'
        )

    grammar_text: str
    if constraints.choices is not None:
        grammar_text = grammar_mod.choice(*constraints.choices)
    elif constraints.grammar is not None:
        grammar_text = constraints.grammar
    else:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": constraints.json_schema},
        }
        return

    if "chat_template_kwargs" not in payload:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    # Header only for llguidance (never double-prefix; never prefix a gbnf grammar).
    if model.grammar_dialect == "llguidance":
        grammar_text = grammar_mod.with_header(grammar_text)
    payload["grammar"] = grammar_text


def _looks_anthropic_shaped(tc: Any) -> bool:
    """True when a tool-call object carries the Anthropic keys instead of OpenAI's.

    The OpenAI schema nests everything under ``function``; the Anthropic tool_use
    schema puts ``name`` and ``input`` at the top level. A gateway that leaks its
    upstream schema onto an OpenAI-compatible endpoint produces the second where
    the first belongs.

    Read-only by design. This predicate decides ONE thing on its own — whether
    the nameless-tool-call error adds a sentence naming the shape it saw. It never
    decides to translate: that is ``compat.tool_call_schema``, which an operator
    states. A `function`-less delta carrying neither key is not this shape but an
    ordinary streaming fragment (index plus an arguments piece), so it is False.
    """
    if not isinstance(tc, dict) or tc.get("function"):
        return False
    return "name" in tc or "input" in tc


def _tool_call_from_anthropic_shape(tc: dict, *, model_id: str, base_url: str) -> dict:
    """Rewrite one Anthropic-shaped tool call into the OpenAI shape.

    Reached only when the operator set ``compat.tool_call_schema="anthropic"`` for
    this model AND :func:`_looks_anthropic_shaped` recognises the object, so an
    ordinary argument fragment on a compat-enabled model passes through untouched.

    Translation, not repair. Every field the OpenAI schema requires must be
    derivable from what arrived, and this raises when one is not — a call whose
    name is blank, or that carries no argument payload at all, is as unroutable
    here as it is in ``_build_final_message``, and inventing ``{}`` for it would
    execute a tool with arguments the model never chose. The point of the compat
    field is to read a KNOWN-different schema, not to lower the bar.

    ``input`` wins over ``text`` when both are present: ``input`` is the parsed
    object and ``text`` is the gateway's own re-serialisation of it. Re-encoding
    the dict to JSON so the finalize path can decode it again is deliberate — one
    finalize path with the Fail-Early guards on it is worth a round trip.

    Returns: a NEW dict in OpenAI shape, carrying over every key it did not
    consume (``id`` above all — the Anthropic and OpenAI schemas spell that one
    the same). The caller's object is not mutated: the streaming path compares
    the two by identity to tell a translated call from an untranslated one, which
    is what decides whether the nameless-call error mentions this field.
    """
    name = tc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"Tool call {tc.get('id')!r} from model {model_id!r} at {base_url!r} arrived "
            f"in the Anthropic tool_use shape (compat.tool_call_schema='anthropic') but "
            f"its `name` is {name!r}. There is nothing to route this call to."
        )

    if "input" in tc:
        raw_input = tc["input"]
        if not isinstance(raw_input, dict):
            raise ValueError(
                f"Tool call {tc.get('id')!r} ({name!r}) from model {model_id!r} at "
                f"{base_url!r} arrived in the Anthropic tool_use shape with an `input` of "
                f"type {type(raw_input).__name__}, expected an object: {raw_input!r}"
            )
        arguments = json.dumps(raw_input)
    elif isinstance(tc.get("text"), str) and tc["text"].strip():
        arguments = tc["text"]
    else:
        raise ValueError(
            f"Tool call {tc.get('id')!r} ({name!r}) from model {model_id!r} at {base_url!r} "
            f"arrived in the Anthropic tool_use shape with neither an `input` object nor a "
            f"`text` payload, so its arguments are not on the wire. A call that takes no "
            f'arguments still sends `"input": {{}}`; τ will not substitute one. '
            f"Received keys: {sorted(tc)!r}"
        )

    _logger.debug(
        "translating Anthropic-shaped tool call %r (%s) to the OpenAI schema "
        "(compat.tool_call_schema='anthropic' on model %r)",
        tc.get("id"),
        name,
        model_id,
    )
    normalized = {k: v for k, v in tc.items() if k not in ("name", "input", "text", "type")}
    normalized["type"] = "function"
    normalized["function"] = {"name": name, "arguments": arguments}
    return normalized


def _resolve_tool_call_block(
    accum: _Accumulator, tc_delta: dict, fallback_index: int
) -> _ToolCallAccumulator:
    """Find or create the accumulator for a streaming tool-call delta.

    OpenAI sends ``id``+``name`` only on a call's first delta; later deltas carry
    only ``index`` plus an arguments fragment. Resolve by ``index`` first (the
    stable key across fragments), then by ``id`` — mirroring pi's
    ``ensureToolCallBlock``. ``fallback_index`` (the position within this chunk's
    ``tool_calls`` array) is used only when the server omits ``index``.
    """
    raw_index = tc_delta.get("index")
    index = raw_index if isinstance(raw_index, int) else fallback_index
    tc_id = tc_delta.get("id") or ""

    block: _ToolCallAccumulator | None = None
    if index is not None and index in accum.by_index:
        block = accum.by_index[index]
    if block is None and tc_id and tc_id in accum.by_id:
        block = accum.by_id[tc_id]

    if block is None:
        block = _ToolCallAccumulator(id=tc_id, index=index)
        accum.tool_calls.append(block)
        if index is not None:
            accum.by_index[index] = block
        if tc_id:
            accum.by_id[tc_id] = block
        return block

    if index is not None and block.index is None:
        block.index = index
        accum.by_index[index] = block
    if tc_id and not block.id:
        block.id = tc_id
        accum.by_id[tc_id] = block
    return block


def _extract_reasoning(delta: dict) -> tuple[str, str]:
    """Return ``(text, field_name)`` for the first non-empty reasoning field.

    OpenAI-compatible servers disagree on the field name: llama.cpp / vLLM /
    DeepSeek emit ``reasoning_content``, OpenRouter and some others emit
    ``reasoning``, a few use ``reasoning_text``. Try them in priority order and
    use the first non-empty one (mirrors pi ``openai-completions.ts``: the
    ``reasoningFields`` loop). Empirically required — Qwen3 on llama.cpp emits
    ``reasoning_content``, which the old single-field read dropped entirely.

    The field name is returned too (the ``thinkingSignature``) so a follow-up
    turn can replay the reasoning under the exact field the model's chat
    template reads. ``("", "")`` when no reasoning is present.
    """
    for field_name in ("reasoning_content", "reasoning", "reasoning_text"):
        value = delta.get(field_name)
        if isinstance(value, str) and value:
            return value, field_name
    return "", ""


def _consolidate_text_and_thinking(accum: _Accumulator) -> list[Any]:
    """Return ``[thinking?, text?]`` — each a SINGLE consolidated block.

    OpenAI streams text and reasoning as many small fragments; ``accum`` keeps
    one fragment per delta. pi keeps a single accumulating block per kind
    (``openai-completions.ts:172``). Emitting one block per fragment instead
    (a) bloats persistence to hundreds of blocks per message, and (b) makes the
    backend's reasoning suffix-diff re-emit the whole trace on every tool-call
    ``message_update`` (the "reasoning shown N×" bug). Join the fragments into
    one block each; thinking precedes the answer, matching the stream order and
    pi. Shared by the partial and final builders so they can't drift.
    """
    blocks: list[Any] = []
    if accum.thinking_parts:
        blocks.append(
            ThinkingContent(
                type="thinking",
                thinking="".join(accum.thinking_parts),
                thinking_signature=accum.thinking_signature,
            )
        )
    if accum.text_parts:
        blocks.append(TextContent(type="text", text="".join(accum.text_parts)))
    return blocks


def _usage_from_openai(data: dict, timings: dict[str, Any] | None = None) -> Usage:
    """Map an OpenAI-style usage dict onto τ's :class:`Usage`.

    OpenAI/llama.cpp use ``prompt_tokens`` / ``completion_tokens`` /
    ``total_tokens``; τ uses ``input_tokens`` / ``output_tokens`` /
    ``total_tokens``. A bare ``Usage(**data)`` would silently drop the prompt/
    completion counts (pydantic ignores the unknown keys) and report 0. When the
    server omits ``total_tokens`` we compute it from input+output rather than
    fabricate — the real number, including a real zero.

    ``prompt_tokens`` INCLUDES both ``prompt_tokens_details.cached_tokens`` and
    ``prompt_tokens_details.cache_write_tokens``, so both are subtracted out of
    ``input_tokens`` — the three partition the prompt rather than overlapping,
    and overlapping made ``compute_cost_usd`` bill the cached span twice
    (docs/PROMPT-CACHING.md §3 has the measured field names per endpoint).
    ``total_tokens`` is unaffected — it comes from the server.

    ``cache_reported`` is whether either key was PRESENT, which llama.cpp's
    silence and a gateway's honest 0 do not otherwise distinguish.

    ``timings`` is llama.cpp's per-completion telemetry block, a TOP-LEVEL
    sibling of ``usage`` on the final SSE chunk (not nested inside it). When
    non-empty it lands verbatim — keys unfiltered, unrenamed — on
    ``Usage.extra["timings"]``; stock builds omit ``n_ff_total`` and τ never
    fabricates it.
    """
    prompt_tokens = int(data.get("prompt_tokens") or 0)
    output_tokens = int(data.get("completion_tokens") or 0)
    total = int(data.get("total_tokens") or 0) or (prompt_tokens + output_tokens)
    details = data.get("prompt_tokens_details") or {}
    cache_read = int(details.get("cached_tokens") or 0)
    cache_write = int(details.get("cache_write_tokens") or 0)
    reported = "cached_tokens" in details or "cache_write_tokens" in details
    input_tokens = max(0, prompt_tokens - cache_read - cache_write)
    extra: dict[str, Any] = {}
    if timings:
        extra["timings"] = dict(timings)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cache_reported=reported,
        total_tokens=total,
        extra=extra,
    )


_CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}


def _markable_block_list(content: Any) -> list[dict] | None:
    """The content as a block list a breakpoint can attach to, or None.

    A bare string is wrapped in one text block — measured 2026-09-06 to change no
    token count on api.openai.com, LiteLLM or llama.cpp. ``None`` (an assistant
    message that is only ``tool_calls``) and an empty list have no block to mark.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else None
    if isinstance(content, list) and content and all(isinstance(b, dict) for b in content):
        return list(content)
    return None


def _apply_cache_control(messages: list[dict]) -> list[dict]:
    """Place Anthropic ephemeral cache breakpoints on the system message and the tail.

    Rewrites ``messages`` into a new list; the input is not mutated. Two of the
    four available breakpoints are used, both at the default 5-minute TTL, which
    a τ agent loop refreshes on every request (docs/PROMPT-CACHING.md §4).

    The tail marker is what makes a tool loop read its own prior writes, and the
    tail of a τ loop is a ``role: "tool"`` message. Measured 2026-09-06 against
    LiteLLM → Claude Haiku 4.5 on one 21058-token request: system marker alone
    read 10736 tokens, system plus tail read 17721 and wrote only the 3331-token
    delta. When the last message carries no markable block the walk goes backward
    to the nearest one that does.

    Emitted only for a model declaring ``prompt_cache_dialect: "anthropic"``,
    because the key reaches the server inside ``messages`` where no operator
    override can remove it — see docs/PROMPT-CACHING.md §5 for the four
    OpenAI-compatible servers measured to ignore it and why that is still not a
    safe default.
    """
    out = [dict(m) for m in messages]
    targets: list[int] = []

    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "system":
            targets.append(i)
            break

    for i in range(len(out) - 1, -1, -1):
        if _markable_block_list(out[i].get("content")) is not None:
            if i not in targets:
                targets.append(i)
            break

    for i in targets:
        blocks = _markable_block_list(out[i].get("content"))
        if blocks is None:
            continue
        blocks[-1] = {**blocks[-1], "cache_control": dict(_CACHE_CONTROL_EPHEMERAL)}
        out[i]["content"] = blocks

    return out


def _image_turn(mime: str, data: str) -> dict:
    """ONE tool-result image as its own user turn.

    One image per turn, never several in one turn. MEASURED 2026-08-28 against
    llama.cpp (``b1637-9c7a7553``, Qwen3.8-27B-Q4_0, vision on) with two tool
    results, a red circle holding "7" and a blue square holding "K":

    * both images in ONE user turn under a single label — 3/3 runs answered
      "alpha.png: red circle K | beta.png: NO IMAGE". The model saw one image and
      attributed it to both files. This is the shape pi uses
      (``openai-completions.ts:1380``), so τ diverges here deliberately.
    * one image per user turn — 3/3 correct, with two images and again with
      three. Labelling each turn with its filename changed nothing, so it is the
      turn boundary doing the work, not the text.

    The label is constant because the measurement says the filename is not what
    carries the association, and the tool result's own text already names the
    file.
    """
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "Tool result image:"},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
        ],
    }


class OpenAICompletionsProvider(Provider):
    """Provider for OpenAI-compatible APIs (OpenAI, Ollama, vLLM, etc.).

    This is the only concrete provider in τ. It converts τ types to/from
    OpenAI API format and handles streaming responses.

    Reference: PHASE-1-SUBPHASE-2.md, "Implementation Outline" section.
    """

    DEFAULT_BASE_URL: str = "https://api.openai.com/v1"

    DEFAULT_TIMEOUT_SECONDS: float = 300.0
    DEFAULT_CONNECT_TIMEOUT_SECONDS: float = 10.0

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        request_timeout: float | httpx.Timeout | None = None,
    ) -> None:
        """Initialize the OpenAI provider.

        Args:
            api_key: API key. If None, falls back to the OPENAI_API_KEY env var.
                May remain None here; it is resolved and *required* at request
                time in ``stream_chat``. Local servers that need no real auth
                must still pass a truthy sentinel (e.g. ``"not-needed"``).
            base_url: Custom API base URL. Defaults to OpenAI production URL.
            request_timeout: Completion timeout. A number is seconds and keeps
                the default connect timeout; an ``httpx.Timeout`` sets every
                phase explicitly. None keeps ``DEFAULT_TIMEOUT_SECONDS``.
        """
        import os

        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url or self.DEFAULT_BASE_URL
        self.request_timeout: httpx.Timeout = self._resolve_timeout(request_timeout)
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def _resolve_timeout(cls, value: float | httpx.Timeout | None) -> httpx.Timeout:
        """Normalize a caller-supplied timeout to an ``httpx.Timeout``.

        Fail-Early: a value that cannot mean a duration raises instead of
        quietly reverting to the default. A timeout silently ignored is exactly
        the failure this knob exists to fix — the operator would tune a number,
        see no change, and conclude the hang is elsewhere.

        ``bool`` is rejected explicitly: it is an ``int`` subclass, so
        ``request_timeout=True`` would otherwise arrive as a 1-second timeout.
        """
        if value is None:
            return httpx.Timeout(
                cls.DEFAULT_TIMEOUT_SECONDS, connect=cls.DEFAULT_CONNECT_TIMEOUT_SECONDS
            )
        if isinstance(value, httpx.Timeout):
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"request_timeout must be a number of seconds or an httpx.Timeout, "
                f"got {value!r} ({type(value).__name__})"
            )
        if value <= 0:
            raise ValueError(
                f"request_timeout must be a positive number of seconds, got {value!r}. "
                "(httpx spells 'no timeout' as None, which here means 'use the "
                f"default of {cls.DEFAULT_TIMEOUT_SECONDS}s' — pass "
                "httpx.Timeout(None) if an unbounded wait is really wanted.)"
            )
        return httpx.Timeout(float(value), connect=cls.DEFAULT_CONNECT_TIMEOUT_SECONDS)

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.request_timeout,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client, if one was ever built.

        Explicit teardown counterpart to ``_get_client``'s lazy construction.
        Nothing in this provider calls this on its own — callers that pool
        providers (``tau_llm.client``'s provider pool) own the decision of when
        a provider's connections are no longer needed and must call this
        themselves (docs/PROVIDER-LIFETIME.md §6.3: "closed explicitly, not by
        GC"). Idempotent: closing an already-closed/never-built client is a
        no-op, so a caller need not track whether ``_get_client`` ever ran.
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _convert_messages_to_openai(
        self,
        messages: list,
        reasoning_replay: str = "turn",
        strict_reasoning_formats: bool = False,
        multimodal_tool_results: bool = False,
    ) -> list[dict]:
        """Convert τ messages to OpenAI API message format.

        Conversion rules:
        - UserMessage → {"role": "user", "content": [...]}
          - TextContent → {"type": "text", "text": ...}
          - ImageContent → {"type": "image_url", "image_url": {"url": "data:{mime};base64,{data}"}}
        - AssistantMessage → {"role": "assistant", "content": ..., "tool_calls": ...}
          - Text-only content → {"content": "..."}
          - Tool calls in content → {"tool_calls": [...]}
        - ToolResultMessage → {"role": "tool", "tool_call_id": ..., "content": ...}
        - ThinkingContent → included in content field (OpenAI handles as text)

        Reference: SUBPHASE-0.0.md, "1. Messages" section.

        Args:
            messages: List of τ message objects.

        Returns:
            List of OpenAI-format message dicts.
        """
        openai_messages: list[dict] = []

        last_user_idx = -1
        for i, msg in enumerate(messages):
            if isinstance(msg, UserMessage) or (
                isinstance(msg, dict) and msg.get("role") == "user"
            ):
                last_user_idx = i

        def _replay_for(index: int) -> bool:
            if reasoning_replay == "all":
                return True
            if reasoning_replay == "off":
                return False
            return index > last_user_idx  # "turn"

        pending_images: list[tuple[str, str]] = []

        def flush_images() -> None:
            openai_messages.extend(_image_turn(mime, data) for mime, data in pending_images)
            pending_images.clear()

        for i, msg in enumerate(messages):
            include_reasoning = _replay_for(i)
            tool_result: tuple[str, Any] | None = None
            if isinstance(msg, ToolResultMessage):
                tool_result = (msg.tool_call_id, msg.content)
            elif isinstance(msg, dict) and msg.get("role") in ("toolResult", "tool"):
                tool_result = (msg.get("tool_call_id", ""), msg.get("content", ""))
            elif (
                not isinstance(msg, (UserMessage, AssistantMessage, dict))
                and hasattr(msg, "model_dump")
                and msg.model_dump().get("role") in ("toolResult", "tool")
            ):
                d = msg.model_dump()
                tool_result = (d.get("tool_call_id", ""), d.get("content", ""))

            if tool_result is not None:
                tool_message, images = self._tool_result_message(
                    tool_result[0], tool_result[1], multimodal_tool_results
                )
                openai_messages.append(tool_message)
                pending_images.extend(images)
                continue

            # Anything that is not a tool result ends the run.
            flush_images()

            if isinstance(msg, UserMessage):
                openai_messages.append(self._convert_user_message(msg))
            elif isinstance(msg, AssistantMessage):
                openai_messages.append(
                    self._convert_assistant_message(
                        msg, include_reasoning, strict_reasoning_formats
                    )
                )
            elif isinstance(msg, dict):
                openai_messages.append(
                    self._convert_message_dict(msg, include_reasoning, strict_reasoning_formats)
                )
            else:
                # Try to convert via model_dump
                if hasattr(msg, "model_dump"):
                    openai_messages.append(
                        self._convert_message_dict(
                            msg.model_dump(), include_reasoning, strict_reasoning_formats
                        )
                    )
                else:
                    openai_messages.append({"role": "user", "content": str(msg)})

        flush_images()

        return openai_messages

    def _convert_user_message(self, msg: UserMessage) -> dict:
        """Convert UserMessage to OpenAI format."""
        content = msg.content
        if isinstance(content, str):
            return {"role": "user", "content": [{"type": "text", "text": content}]}

        blocks: list[dict] = []
        for block in content:
            if isinstance(block, TextContent):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                b64_data = self._encode_image(block)
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{block.mime_type};base64,{b64_data}"},
                    }
                )
            elif isinstance(block, dict):
                blocks.append(block)

        return {"role": "user", "content": blocks if blocks else ""}

    def _encode_image(self, img: ImageContent) -> str:
        """Encode image data as base64 string.

        The data field is assumed to be already base64-encoded.
        We only strip the data: URI prefix if present.
        """
        if img.data.startswith("data:"):
            # Already has data URI prefix, strip it
            data_part = img.data.split(",", 1)[1] if "," in img.data else img.data
            return data_part
        # Return the data as-is (assumed to be base64-encoded)
        return img.data

    def _assistant_content_to_openai(
        self,
        blocks: list,
        include_reasoning: bool = True,
        strict_reasoning_formats: bool = False,
    ) -> dict:
        """Convert an assistant message's content blocks to OpenAI format.

        Accepts either τ pydantic blocks (``TextContent``/``ThinkingContent``/
        ``ToolCall``) or the persisted dict shape (``{"type": "text"|"thinking"|
        "toolCall", ...}``), so the live and the reload/follow-up paths converge
        on one conversion. Produces the OpenAI assistant shape: text joined into a
        plain-string ``content`` and ``toolCall`` blocks hoisted into a
        ``tool_calls`` array.

        ``thinking``/``toolCall`` are NOT valid OpenAI ``content[].type`` values —
        shipping the raw block list is exactly the "HTTP 400 unsupported
        content[].type" failure on a follow-up turn, where the context carries the
        prior assistant message as a block-list dict. So thinking is not emitted as
        content when there's text or a tool call (the call carries the turn, and
        reasoning is regenerated by the model — pi sends it only via a separate
        field); a thinking-only turn falls back to thinking-as-string so it isn't
        empty. Fragments are concatenated with no separator so a legacy many-block
        message reconstructs faithfully. Mirrors pi convertMessages' assistant
        branch (openai-completions.ts:835)."""
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        thinking_signature: str | dict[str, Any] = ""
        tool_calls: list[dict] = []
        for block in blocks:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "thinking":
                    thinking_parts.append(block.get("thinking", ""))
                    if not thinking_signature:
                        thinking_signature = block.get("thinking_signature", "")
                elif btype == "toolCall":
                    if block.get("provider_signature"):
                        self._on_foreign_tool_signature(
                            block["provider_signature"], strict_reasoning_formats
                        )
                    tool_calls.append(
                        {
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("arguments", {})),
                            },
                        }
                    )
            elif isinstance(block, TextContent):
                text_parts.append(block.text)
            elif isinstance(block, ThinkingContent):
                thinking_parts.append(block.thinking)
                if not thinking_signature:
                    thinking_signature = block.thinking_signature
            elif isinstance(block, ToolCall):
                if block.provider_signature:
                    self._on_foreign_tool_signature(
                        block.provider_signature, strict_reasoning_formats
                    )
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.arguments),
                        },
                    }
                )

        if not isinstance(thinking_signature, str):
            self._on_foreign_thinking_signature(thinking_signature, strict_reasoning_formats)
            thinking_signature = ""

        result: dict[str, Any] = {"role": "assistant"}
        text = "".join(text_parts)
        thinking = "\n".join(p for p in thinking_parts if p)

        if text:
            result["content"] = text
        elif tool_calls:
            result["content"] = ""
        elif thinking and (not thinking_signature or not include_reasoning):
            result["content"] = thinking
        else:
            result["content"] = ""

        if tool_calls:
            result["tool_calls"] = tool_calls

        if thinking and thinking_signature and include_reasoning:
            result[thinking_signature] = thinking
        return result

    def _on_foreign_thinking_signature(
        self, signature: dict[str, Any], strict_reasoning_formats: bool
    ) -> None:
        """Handle a thinking signature this writer cannot use as a field name.

        Raises under ``strict_reasoning_formats``; otherwise warns once per
        payload shape per process and returns, leaving the caller to drop the
        signature. Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md S2/S4.
        """
        origin = ",".join(sorted(signature)) or "<empty>"
        detail = (
            f"thinking block carries a {origin!r} signature payload, which the "
            "OpenAI-completions writer cannot replay — a dict signature is a "
            "provider-peculiar blob, not the field name this writer replays under. "
            "The block almost certainly came from another provider (models mixed "
            "within one session, or a message an extension synthesised)."
        )
        if strict_reasoning_formats:
            raise ValueError(
                f"{detail} Refusing to continue because "
                "models.<name>.strict_reasoning_formats is set."
            )
        if origin not in _WARNED_FOREIGN_SIGNATURES:
            _WARNED_FOREIGN_SIGNATURES.add(origin)
            _logger.warning(
                "%s Keeping the reasoning as text content and not replaying it under "
                "a signature field. Set models.<name>.strict_reasoning_formats to "
                "raise instead.",
                detail,
            )

    def _on_foreign_tool_signature(
        self, signature: dict[str, Any], strict_reasoning_formats: bool
    ) -> None:
        """Handle a tool call carrying another wire's replay token.

        The OpenAI tool_calls schema has no field for one, so the token is
        DROPPED — the tool call itself still replays, with its id, name and
        arguments intact, because the call is what the conversation needs and the
        signature is what the other wire needs.

        Same shape as :meth:`_on_foreign_thinking_signature`: raise under
        ``strict_reasoning_formats``, else warn once per payload shape.

        This is not a hypothetical. Gemini 3 REQUIRES the token on replay, so a
        session that switches from a Google model to an OpenAI-compatible one
        carries tool calls that still hold it. Forwarding it — under an invented
        key, or inside ``arguments`` where it would reach the tool — would send a
        valid-looking request that means something else. Reference:
        docs/ANTHROPIC-GOOGLE-CLIENTS.md S8.
        """
        origin = ",".join(sorted(signature)) or "<empty>"
        detail = (
            f"tool call carries a {origin!r} replay signature, which the "
            "OpenAI-completions writer has nowhere to put — the tool_calls schema "
            "has no such field. The call was almost certainly made by another "
            "provider (models mixed within one session, or a resumed session)."
        )
        if strict_reasoning_formats:
            raise ValueError(
                f"{detail} Refusing to continue because "
                "models.<name>.strict_reasoning_formats is set."
            )
        if origin not in _WARNED_FOREIGN_TOOL_SIGNATURES:
            _WARNED_FOREIGN_TOOL_SIGNATURES.add(origin)
            _logger.warning(
                "%s Replaying the tool call without it. Set "
                "models.<name>.strict_reasoning_formats to raise instead.",
                detail,
            )

    def _convert_assistant_message(
        self,
        msg: AssistantMessage,
        include_reasoning: bool = True,
        strict_reasoning_formats: bool = False,
    ) -> dict:
        """Convert a pydantic AssistantMessage to OpenAI format (text + tool_calls).

        ``include_reasoning`` carries the per-message reasoning-replay scope
        (:meth:`_convert_messages_to_openai`); False drops this message's replayed
        chain-of-thought. ``strict_reasoning_formats`` carries
        ``Model.strict_reasoning_formats``.
        """
        return self._assistant_content_to_openai(
            list(msg.content), include_reasoning, strict_reasoning_formats
        )

    def _tool_result_message(
        self, tool_call_id: str, content: Any, multimodal_tool_results: bool
    ) -> tuple[dict, list[tuple[str, str]]]:
        """One tool result as its ``role: "tool"`` message, plus its homeless images.

        Returns the tool message and the images that still need a turn of their
        own. The caller — :meth:`_convert_messages_to_openai` — holds those until
        the whole run of consecutive tool results has been emitted, because a
        ``user`` message between two ``tool`` messages splits the run answering
        one assistant's parallel ``tool_calls``. OpenAI's schema says a ``tool``
        message responds to a preceding message with ``tool_calls``, and the
        split shape is the one that reading disallows.

        Measured 2026-08-28: llama.cpp (``b1637-9c7a7553``) and a glm-5.2
        endpoint both ACCEPT the split shape and answer correctly, so this is not
        a bug either of them will report. It is the reading that costs nothing to
        satisfy, and satisfying it is what lets the images be emitted one per
        turn — which the same measurement showed is required for the model to
        attribute each image to the right file. See :func:`_image_turn`.

        When ``multimodal_tool_results`` is set the image nests in the tool
        message and no image comes back. Measured 2026-08-28 against llama.cpp
        (Qwen3.8-27B, vision on): the nested form is accepted and described
        correctly, 3/3, even though that build's ``/props`` reports
        ``chat_template_caps.supports_typed_content: false``. The default stays
        False because one permissive data point does not earn a permissive
        default when the fallback always works.

        Text-only results — every result but a handful — return exactly what they
        always did: one message, content space-joined.

        Args:
            tool_call_id: The id of the call this result answers.
            content: The result's content, in any shape
                :func:`~tau_llm.providers.base.split_tool_result_content` reads.
            multimodal_tool_results: :attr:`Model.supports_multimodal_function_response`
                — whether this endpoint takes a block list as a tool message's content.

        Returns:
            A ``(tool_message, images)`` pair. ``images`` is empty unless the
            caller has to place them itself.
        """
        parts, images = split_tool_result_content(content)
        text = " ".join(p for p in parts if p)
        if not images:
            return {"role": "tool", "tool_call_id": tool_call_id, "content": text}, []

        if multimodal_tool_results:
            blocks = [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
                for mime, data in images
            ]
            return (
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": ([{"type": "text", "text": text}] if text else []) + blocks,
                },
                [],
            )
        return (
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": text or "(see attached image)",
            },
            images,
        )

    def _convert_message_dict(
        self,
        d: dict,
        include_reasoning: bool = True,
        strict_reasoning_formats: bool = False,
    ) -> dict:
        """Convert a generic dict message to OpenAI format.

        Converts list-type content to a string. ``include_reasoning`` carries
        the per-message reasoning-replay scope (this is the persisted/reload path,
        so it is the one that actually accretes stale reasoning on follow-up turns).
        ``strict_reasoning_formats`` carries ``Model.strict_reasoning_formats``.

        Tool results do NOT come here. :meth:`_convert_messages_to_openai` takes
        them before the dispatch, because their images have to be held until the
        run of consecutive results ends, and a per-message converter cannot see
        where a run ends. This method once had a ``toolResult`` branch that
        dropped every image; it is gone rather than left as a second, quieter
        answer to the same question.
        """
        role = d.get("role", "")
        content = d.get("content", "")

        if role == "assistant":
            if isinstance(content, list):
                return self._assistant_content_to_openai(
                    content, include_reasoning, strict_reasoning_formats
                )
            return {"role": "assistant", "content": content}
        elif role == "user":
            return {"role": "user", "content": content}
        else:
            return {"role": role, "content": content}

    def _convert_tools_to_openai(self, tools: list[ToolSpec]) -> list[dict]:
        """Convert τ tool definitions to OpenAI function format.

        Conversion:
        ToolSpec.parameters → functions[].parameters (JSON Schema)
        ToolSpec.description → functions[].description
        ToolSpec.name → functions[].name

        Reference: PHASE-1-SUBPHASE-2.md, "Tools → OpenAI" section.

        Args:
            tools: Anything satisfying :class:`~tau_llm.tools.ToolSpec`. In
                production these are ``tau_agent_core`` ``AgentTool`` wrappers,
                NOT ``ToolDefinition`` — see ToolSpec for why the annotation is a
                Protocol rather than a concrete class.

        Returns:
            List of OpenAI-format tool dicts.
        """
        openai_tools: list[dict] = []

        for tool in tools:
            openai_tool: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            openai_tools.append(openai_tool)

        return openai_tools

    def _map_finish_reason(
        self, reason: str | None
    ) -> Literal["stop", "length", "toolUse", "error", "aborted"]:
        """Map OpenAI finish_reason to τ stop_reason."""
        mapping: dict[str | None, Literal["stop", "length", "toolUse", "error", "aborted"]] = {
            "stop": "stop",
            "length": "length",
            "tool_calls": "toolUse",
            "content_filter": "stop",
            None: "stop",
        }
        return mapping.get(reason, "stop")

    def _make_text_event(
        self, text: str, accum: _Accumulator, partial: AssistantMessage
    ) -> TextDeltaEvent:
        """Create a TextDeltaEvent from accumulated text."""
        return TextDeltaEvent(
            type="text_delta",
            delta=text,
            partial=partial,
        )

    def _make_thinking_event(
        self, reasoning: str, accum: _Accumulator, partial: AssistantMessage
    ) -> ThinkingDeltaEvent:
        """Create a ThinkingDeltaEvent from a reasoning fragment."""
        return ThinkingDeltaEvent(
            type="thinking_delta",
            delta=reasoning,
            partial=partial,
        )

    def _make_toolcall_event(
        self, delta: dict, accum: _Accumulator, partial: AssistantMessage
    ) -> ToolCallDeltaEvent:
        """Create a ToolCallDeltaEvent from tool call delta."""
        return ToolCallDeltaEvent(
            type="toolcall_delta",
            delta=delta,
            partial=partial,
        )

    def _build_partial_message(self, accum: _Accumulator, model: Model) -> AssistantMessage:
        """Build a partial AssistantMessage from the current accumulation state."""
        content_blocks: list[Any] = _consolidate_text_and_thinking(accum)

        for tc in accum.tool_calls:
            args_dict = parse_streaming_json("".join(tc.arguments_parts))
            content_blocks.append(ToolCall(id=tc.id, name=tc.name, arguments=args_dict))

        return AssistantMessage(
            api=model.api,
            provider=model.provider,
            content=content_blocks,
            model=model.id,
            response_id=accum.response_id,
            usage=Usage(),
            stop_reason="stop",
            timestamp=int(time.time() * 1000),
        )

    def _build_final_message(
        self,
        accum: _Accumulator,
        model: Model,
        usage: Usage,
        stop_reason: Literal["stop", "length", "toolUse", "error", "aborted"] | None = None,
    ) -> AssistantMessage:
        """Build the final AssistantMessage with accumulated data."""
        content_blocks: list[Any] = _consolidate_text_and_thinking(accum)

        aborted = stop_reason == "aborted"
        truncated = stop_reason == "length"
        incomplete = aborted or truncated
        dropped_partial = 0

        repairs = 0
        had_tool_call_with_args = False
        for tc in accum.tool_calls:
            if not tc.name.strip():
                if incomplete:
                    dropped_partial += 1
                    continue
                if tc.saw_anthropic_shape:
                    diagnosis = (
                        "The call arrived in the Anthropic tool_use shape — a top-level "
                        "`name`/`input` and no `function` object — so the name is on the "
                        "wire under keys the OpenAI schema does not use. This is a "
                        "gateway leaking its upstream schema; the fix belongs there. To "
                        "read it anyway, state the endpoint's shape: set "
                        '`compat.tool_call_schema: "anthropic"` on this model in '
                        "~/.tau/config.json (with `stream: false` if its streamed "
                        "responses drop the name too)."
                    )
                else:
                    diagnosis = (
                        "The provider or gateway never populated `function.name` on any "
                        "chunk for this call, which violates the OpenAI tool-calling wire "
                        "contract — a tool call must name the function to invoke."
                    )
                raise ValueError(
                    f"Tool call {tc.id!r} arrived with no function name "
                    f"(model {model.id!r} at {self.base_url!r}). {diagnosis} Refusing to "
                    "execute a nameless call. Arguments received: "
                    f"{''.join(tc.arguments_parts)!r}"
                )
            args_str = "".join(tc.arguments_parts)
            if args_str.strip():
                had_tool_call_with_args = True
                try:
                    args_dict, repaired = parse_json_with_repair_info(args_str)
                except Exception as exc:
                    if incomplete:
                        _logger.warning(
                            "dropping tool call %r (%s) from model %r: arguments were "
                            "cut off (stop_reason=%r) after %d chars and will not "
                            "decode. %s",
                            tc.id,
                            tc.name or "<unnamed>",
                            model.id,
                            stop_reason,
                            len(args_str),
                            _TRUNCATION_HINT.format(max_tokens=model.max_tokens)
                            if truncated
                            else "The call was never issued.",
                        )
                        dropped_partial += 1
                        continue
                    raise ValueError(
                        f"Tool call {tc.id!r} ({tc.name!r}) from model {model.id!r} at "
                        f"{self.base_url!r} sent arguments that are not valid JSON, "
                        f"and the stream reported a COMPLETE generation "
                        f"(stop_reason={stop_reason!r}), so they are not merely cut off.\n"
                        f"  {type(exc).__name__}: {exc}\n"
                        f"  {len(args_str)} chars received: "
                        f"{_truncate_error_text(args_str)!r}"
                    ) from exc
                if repaired:
                    repairs += 1
                if not isinstance(args_dict, dict):
                    if incomplete:
                        dropped_partial += 1
                        continue
                    raise ValueError(
                        f"Tool call {tc.id!r} ({tc.name!r}) arguments did not decode "
                        f"to a JSON object: {args_str!r}"
                    )
            elif incomplete:
                dropped_partial += 1
                continue
            else:
                args_dict = {}
            content_blocks.append(ToolCall(id=tc.id, name=tc.name, arguments=args_dict))

        if had_tool_call_with_args:
            usage = usage.model_copy(update={"extra": {**usage.extra, "repairs": repairs}})

        if dropped_partial:
            usage = usage.model_copy(
                update={"extra": {**usage.extra, "dropped_partial_tool_calls": dropped_partial}}
            )

        # Determine stop_reason: use explicit value, or fall back to heuristic
        if stop_reason is None:
            if accum.has_tool_calls:
                stop_reason = "toolUse"
            else:
                stop_reason = "stop"

        return AssistantMessage(
            api=model.api,
            provider=model.provider,
            content=content_blocks,
            model=model.id,
            response_id=accum.response_id,
            usage=usage,
            stop_reason=stop_reason,
            timestamp=int(time.time() * 1000),
        )

    async def stream_chat(
        self,
        model: Model,
        messages: list,
        tools: list[ToolSpec] | None = None,
        options: dict | None = None,
    ) -> AsyncIterator[Any]:
        """Stream chat completions from OpenAI-compatible API.

        Converts τ messages to OpenAI format, streams the response,
        and produces τ streaming events.

        Two transports serve this one contract (PLAN-0.9.3 §4.1): SSE
        (``stream: true``, the default) and a single buffered completion
        (``stream: false``), selected by the per-call ``stream`` option, then
        ``Model.stream``. Which one ran is NOT observable from here: the
        buffered response is adapted into the same delta events and finalized by
        the same ``_build_final_message`` — one construction site, so the
        Fail-Early guards on it (a nameless tool call, an argument buffer that
        will not decode) cover both.

        Reference: PHASE-1-SUBPHASE-2.md, "Streaming event production" section.

        Args:
            model: The Model to use for the request.
            messages: List of τ message objects.
            tools: Optional list of tool definitions.
            options: Optional provider-specific options (temperature, max_tokens,
                ``stream``, ``request_timeout``, …).

        Returns:
            An async iterator of typed streaming events — TextDeltaEvent,
            ThinkingDeltaEvent, ToolCallDeltaEvent, DoneEvent, ErrorEvent. The
            client wraps it once in ``AssistantMessageEventStream`` (streaming.py),
            the single stream type τ-agent-core consumes.
        """
        if options is None:
            options = {}

        api_key = self.api_key or options.get("api_key")
        if not api_key:
            raise ValueError(
                f"No API key for provider: {getattr(model, 'provider', 'openai')}. "
                "Set OPENAI_API_KEY, pass api_key=..., or configure it in "
                '~/.tau/config.json (use "not-needed" for a local server).'
            )
        self.api_key = api_key

        # Convert τ messages to OpenAI format
        openai_messages = self._convert_messages_to_openai(
            messages,
            model.reasoning_replay,
            model.strict_reasoning_formats,
            model.supports_multimodal_function_response,
        )
        if model.prompt_cache and model.prompt_cache_dialect == "anthropic":
            openai_messages = _apply_cache_control(openai_messages)

        # Convert tools to OpenAI format
        openai_tools = None
        if tools:
            openai_tools = self._convert_tools_to_openai(tools)

        abort_signal = options.get("abort_signal")
        body_options = {
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
        _guard_body_keys("extra_body", model.extra_body.keys(), model_id=model.id)
        _guard_body_keys("per-call options", body_options.keys(), model_id=model.id)

        stream_mode = _resolve_stream_mode(options.get("stream"), model)
        compat = resolve_compat(model)

        payload: dict[str, Any] = {
            "model": model.id,
            "messages": openai_messages,
            "stream": stream_mode,
            **model.extra_body,
            **body_options,
        }
        if stream_mode and compat.supports_usage_in_streaming:
            payload["stream_options"] = {"include_usage": True}
        if openai_tools:
            payload["tools"] = openai_tools

        if not any(key in payload for key in ("max_tokens", "max_completion_tokens")):
            payload[compat.max_tokens_field] = model.max_tokens

        _apply_constraints(payload, model, options.get("constraints"), has_tools=bool(openai_tools))

        requested = options.get("reasoning")
        if requested is not None and getattr(model, "reasoning", False):
            clamped = clamp_thinking_level(model, requested)
            tlm = model.thinking_level_map or {}
            if clamped != "off":
                mapped = tlm.get(clamped, clamped)
            else:
                mapped = tlm.get("off")
            if isinstance(mapped, dict):
                _merge_thinking_fragment(payload, mapped, model_id=model.id)
            elif isinstance(mapped, str):
                payload["reasoning_effort"] = mapped

        client = self._get_client()
        accum = _Accumulator()

        _per_call = options.get("request_timeout")
        _configured = _per_call if _per_call is not None else model.request_timeout
        request_timeout = self._resolve_timeout(
            _configured if _configured is not None else self.request_timeout
        )

        async def event_generator() -> AsyncIterator[Any]:
            try:
                state = _TransportState()
                transport = (
                    self._stream_transport(
                        client,
                        payload,
                        request_timeout,
                        accum,
                        state,
                        model,
                        compat,
                        abort_signal,
                    )
                    if stream_mode
                    else self._complete_transport(
                        client,
                        payload,
                        request_timeout,
                        accum,
                        state,
                        model,
                        compat,
                        abort_signal,
                    )
                )
                async for event in transport:
                    yield event
                if state.failed:
                    return

                usage_data = state.usage_data
                timings_data = state.timings_data
                final_stop_reason = state.stop_reason

                usage_obj = _usage_from_openai(usage_data, timings_data)
                final_msg = self._build_final_message(accum, model, usage_obj, final_stop_reason)

                constraints = options.get("constraints") if options else None
                if constraints is not None and constraints.has_constraint():
                    text = _message_text(final_msg)
                    if final_msg.stop_reason == "stop":
                        constraints.verify_output(text)
                    elif final_msg.stop_reason == "length":
                        raise ConstraintViolation(
                            "constrained generation hit the token limit before the "
                            f"constraint completed (stop_reason='length'): {text!r}. "
                            "The output is a PREFIX of a constrained answer, not a "
                            "constrained answer. Raise max_tokens.",
                            text,
                        )

                if accum.has_tool_calls:
                    for pos, tc_block in enumerate(final_msg.get_tool_calls()):
                        tc_delta = {
                            "index": pos,
                            "id": tc_block.id,
                            "function": {
                                "name": tc_block.name,
                                "arguments": json.dumps(tc_block.arguments),
                            },
                        }
                        yield ToolCallDeltaEvent(
                            type="toolcall_delta",
                            delta=tc_delta,
                            partial=final_msg,
                        )

                # Yield done event
                yield DoneEvent(
                    type="done",
                    final=final_msg,
                    usage=usage_obj,
                )
                return

            except ConstraintViolation:
                raise
            except Exception as e:
                described = _describe_exception(e)
                if self.base_url in described:
                    message = f"Streaming error: {described}"
                else:
                    message = (
                        f"Streaming error from model {model.id!r} at {self.base_url!r}: {described}"
                    )
                error_event = ErrorEvent(type="error", message=message, is_error=True)
                yield error_event
                return

        return event_generator()

    def _error_event_from_response(self, response: Any, model: Model) -> ErrorEvent:
        """Build the ErrorEvent for a non-200, from an ALREADY-READ body.

        Shared by both transports so a gateway's 502 reads the same either way.
        The streaming caller must ``await response.aread()`` first (a streaming
        response's body is not read yet); the buffered caller already has it.
        """
        error_body: Any = None
        try:
            error_body = response.json()
        except Exception:
            pass
        error_msg = ""
        if isinstance(error_body, dict):
            err = error_body.get("error")
            if isinstance(err, dict):
                error_msg = str(err.get("message") or "")
            elif isinstance(err, str):
                error_msg = err
        if not error_msg:
            error_msg = _truncate_error_text(response.text.strip())
        return ErrorEvent(
            type="error",
            message=(
                f"HTTP {response.status_code} from model {model.id!r} at "
                f"{self.base_url!r}: {error_msg or '(empty response body)'}"
            ),
            is_error=True,
        )

    def _apply_tool_call_schema(self, tc: dict, compat: ResolvedCompat, model: Model) -> dict:
        """Return ``tc`` in the OpenAI tool-call schema, translating only if told to.

        The default (`compat.tool_call_schema == "openai"`) returns the caller's
        object unchanged — including when it is visibly Anthropic-shaped. τ reads
        the schema the endpoint promised and reports the endpoint that breaks it;
        the error in ``_build_final_message`` names the shape and the config field
        that would accept it. Translating on sight instead would make a gateway bug
        invisible to the operator who has to get it fixed.
        """
        if compat.tool_call_schema == "anthropic" and _looks_anthropic_shaped(tc):
            return _tool_call_from_anthropic_shape(tc, model_id=model.id, base_url=self.base_url)
        return tc

    async def _stream_transport(
        self,
        client: Any,
        payload: dict[str, Any],
        request_timeout: httpx.Timeout,
        accum: _Accumulator,
        state: _TransportState,
        model: Model,
        compat: ResolvedCompat,
        abort_signal: Any,
    ) -> AsyncIterator[Any]:
        """SSE transport: read `data:` frames and yield a delta event per fragment."""
        async with client.stream(
            "POST", "/chat/completions", json=payload, timeout=request_timeout
        ) as response:
            if response.status_code != 200:
                await response.aread()
                yield self._error_event_from_response(response, model)
                state.failed = True
                return

            # Read SSE lines as they arrive (no full-body buffering).
            async for line in response.aiter_lines():
                if abort_signal is not None and abort_signal.is_aborted():
                    state.stop_reason = "aborted"
                    break
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    _logger.debug("skipping undecodable SSE frame: %r", data_str)
                    continue

                if not isinstance(chunk, dict):
                    _logger.debug(
                        "skipping non-object SSE frame (%s): %r",
                        type(chunk).__name__,
                        data_str,
                    )
                    continue

                if chunk.get("id"):
                    accum.response_id = chunk["id"]
                chunk_usage = chunk.get("usage")
                if chunk_usage:
                    state.usage_data = chunk_usage
                chunk_timings = chunk.get("timings")
                if chunk_timings:
                    state.timings_data = chunk_timings

                choices = chunk.get("choices") or []
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta") or {}
                finish_reason = choice.get("finish_reason")
                choice_usage = choice.get("usage")
                if choice_usage:
                    state.usage_data = choice_usage

                # Process text delta
                text = delta.get("content", "") or ""
                if text:
                    accum.text_parts.append(text)
                    accum.has_text = True
                    partial = self._build_partial_message(accum, model)
                    yield self._make_text_event(text, accum, partial)

                reasoning, reasoning_field = _extract_reasoning(delta)
                if reasoning:
                    accum.thinking_parts.append(reasoning)
                    if not accum.thinking_signature:
                        accum.thinking_signature = reasoning_field
                    accum.has_thinking = True
                    partial = self._build_partial_message(accum, model)
                    yield self._make_thinking_event(reasoning, accum, partial)

                deltas = delta.get("tool_calls") or []
                for i, raw_tc in enumerate(deltas):
                    if not isinstance(raw_tc, dict):
                        raise ValueError(
                            f"Model {model.id!r} at {self.base_url!r} streamed a "
                            f"non-object tool-call delta: {raw_tc!r}"
                        )
                    tc_delta = self._apply_tool_call_schema(raw_tc, compat, model)
                    block = _resolve_tool_call_block(accum, tc_delta, i)
                    if tc_delta is raw_tc and _looks_anthropic_shaped(raw_tc):
                        block.saw_anthropic_shape = True
                    func = tc_delta.get("function") or {}
                    tc_name = func.get("name") or ""
                    if tc_name:
                        block.name += tc_name
                    tc_args = func.get("arguments") or ""
                    if tc_args:
                        block.arguments_parts.append(tc_args)
                    accum.has_tool_calls = True

                    partial = self._build_partial_message(accum, model)
                    yield self._make_toolcall_event(tc_delta, accum, partial)

                if finish_reason:
                    state.stop_reason = self._map_finish_reason(finish_reason)

    async def _complete_transport(
        self,
        client: Any,
        payload: dict[str, Any],
        request_timeout: httpx.Timeout,
        accum: _Accumulator,
        state: _TransportState,
        model: Model,
        compat: ResolvedCompat,
        abort_signal: Any,
    ) -> AsyncIterator[Any]:
        """Buffered transport: one `stream: false` completion, ADAPTED into deltas.

        PLAN-0.9.3 §4.1. A τ divergence from pi, which is streaming-only — see
        ``Model.stream`` for why it exists (OpenAI-shaped gateways that do not
        implement SSE) and what it costs.

        The whole message arrives at once, so each channel produces exactly ONE
        delta event — the same shape a cloud provider's single-chunk stream
        produces, which the pipeline above already handles. Fields are read
        through the SAME helpers as the streaming path (``_extract_reasoning``,
        ``_resolve_tool_call_block``, ``_map_finish_reason``, ``_usage_from_openai``)
        and the accumulator is filled the same way, so the caller's finalize tail —
        with the nameless-tool-call and undecodable-arguments guards on it — is
        reached identically. There is deliberately no second message builder here:
        a divergent finalize path is the defect this repo has been removing.

        Fail-Early on a malformed body. A buffered response has no "maybe the next
        chunk carries it" excuse: if `choices` is empty or `message` is not an
        object, the completion did not happen, and returning an empty
        AssistantMessage would report that as the model having said nothing.
        """
        if abort_signal is not None and abort_signal.is_aborted():
            state.stop_reason = "aborted"
            return

        response = await client.post("/chat/completions", json=payload, timeout=request_timeout)
        if response.status_code != 200:
            yield self._error_event_from_response(response, model)
            state.failed = True
            return

        body = response.json()
        if not isinstance(body, dict):
            raise ValueError(
                f"Non-streaming completion from model {model.id!r} at {self.base_url!r} "
                f"decoded to {type(body).__name__}, not a JSON object: "
                f"{_truncate_error_text(response.text.strip())!r}"
            )

        if body.get("id"):
            accum.response_id = body["id"]
        usage = body.get("usage")
        if usage:
            state.usage_data = usage
        timings = body.get("timings")
        if timings:
            state.timings_data = timings

        choices = body.get("choices") or []
        if not choices:
            raise ValueError(
                f"Non-streaming completion from model {model.id!r} at {self.base_url!r} "
                f"returned no choices: {_truncate_error_text(response.text.strip())!r}. "
                "A buffered response carries the whole completion or none of it, so "
                "there is no later chunk this could arrive in."
            )
        if len(choices) > 1:
            _logger.warning(
                "model %r returned %d choices; τ reads the first and ignores the rest",
                model.id,
                len(choices),
            )

        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError(
                f"Non-streaming completion from model {model.id!r} at {self.base_url!r} "
                f"returned a choice with no `message` object: {choice!r}"
            )

        finish_reason = choice.get("finish_reason")
        if finish_reason:
            state.stop_reason = self._map_finish_reason(finish_reason)

        reasoning, reasoning_field = _extract_reasoning(message)
        if reasoning:
            accum.thinking_parts.append(reasoning)
            accum.thinking_signature = reasoning_field
            accum.has_thinking = True
            partial = self._build_partial_message(accum, model)
            yield self._make_thinking_event(reasoning, accum, partial)

        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError(
                f"Non-streaming completion from model {model.id!r} at {self.base_url!r} "
                f"returned `message.content` of type {type(content).__name__}, "
                f"expected a string or null: {content!r}"
            )
        if content:
            accum.text_parts.append(content)
            accum.has_text = True
            partial = self._build_partial_message(accum, model)
            yield self._make_text_event(content, accum, partial)

        tool_calls = message.get("tool_calls") or []
        for i, raw_tc in enumerate(tool_calls):
            if not isinstance(raw_tc, dict):
                raise ValueError(
                    f"Non-streaming completion from model {model.id!r} at "
                    f"{self.base_url!r} returned a non-object tool call: {raw_tc!r}"
                )
            tc = self._apply_tool_call_schema(raw_tc, compat, model)
            block = _resolve_tool_call_block(accum, tc, i)
            if tc is raw_tc and _looks_anthropic_shaped(raw_tc):
                block.saw_anthropic_shape = True
            func = tc.get("function") or {}
            tc_name = func.get("name") or ""
            if tc_name:
                block.name += tc_name
            tc_args = func.get("arguments") or ""
            if tc_args:
                block.arguments_parts.append(tc_args)
            accum.has_tool_calls = True

            partial = self._build_partial_message(accum, model)
            yield self._make_toolcall_event(tc, accum, partial)

        if abort_signal is not None and abort_signal.is_aborted():
            state.stop_reason = "aborted"
