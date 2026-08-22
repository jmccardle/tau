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
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Literal

import httpx

from tau_llm import grammar as grammar_mod
from tau_llm.compat import resolve_compat
from tau_llm.constraints import ConstraintViolation
from tau_llm.providers.base import Provider
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

#: Payload shapes already warned about by :meth:`_on_foreign_thinking_signature`.
#: Process-wide and deliberately unbounded-in-theory: it is keyed by the sorted
#: top-level keys of a thinking-signature dict, so its size is the number of
#: distinct provider payload shapes τ has seen, not the number of messages.
_WARNED_FOREIGN_SIGNATURES: set[str] = set()

#: The same, for :meth:`_on_foreign_tool_signature`. A separate set so a warned
#: thinking payload does not silence a tool-call one that happens to share a
#: vendor namespace: they are different fields with different consequences.
_WARNED_FOREIGN_TOOL_SIGNATURES: set[str] = set()

# Cap on how much of an upstream error body is quoted into an error message.
# Same number and same reason as pi's MAX_PROVIDER_ERROR_BODY_CHARS
# (utils/error-body.ts:16): enough to carry a real gateway error page's useful
# head, bounded so an HTML 502 does not become the whole transcript.
_MAX_ERROR_BODY_CHARS = 4000


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

    # httpx.HTTPStatusError and friends carry the response; a transport error
    # does not. Attribute access is guarded because these are properties on some
    # httpx exception types and can raise when unset.
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
    # The field name reasoning streamed on (``reasoning_content`` etc.), captured
    # from the first reasoning delta so a follow-up turn can replay it under the
    # same field. Fragments within one completion always share a field.
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
    # llama.cpp's per-completion telemetry, a TOP-LEVEL sibling of `usage` in both
    # transports (final SSE chunk / response body).
    timings_data: dict[str, Any] = field(default_factory=dict)
    stop_reason: Literal["stop", "length", "toolUse", "error", "aborted"] | None = None
    # The transport already yielded an ErrorEvent and there is nothing to finalize.
    failed: bool = False


# Request-body fields τ owns and no caller-supplied dict may overwrite. These carry the
# transport contract (what we send, and that we get a usage chunk back), not server
# decode settings. See _guard_body_keys.
_RESERVED_BODY_KEYS = frozenset({"model", "messages", "stream", "stream_options", "tools"})

# Request-body fields that EXPRESS A DECODE CONSTRAINT. They may reach the wire only
# through DecodeConstraints — never through Model.extra_body, per-call body options, or
# DecodeConstraints.extra_body.
#
# This is not tidiness. Every one of these was live-reproduced against llama-server
# (CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §0.2):
#
#   * A `grammar` smuggled in via extra_body skips BOTH gates in _apply_constraints —
#     which returns early when `constraints is None` — so it is never capability-checked
#     and its output is never verified. Against a server that ignores the key, that is an
#     unconstrained generation returned as a constrained one: fabricated data.
#   * A `response_format` alongside a real DecodeConstraints(grammar=...) is worse. The
#     server guards `grammar` + top-level `json_schema` with a loud 500, but
#     `response_format` parses on a different path and SILENTLY WINS: a grammar restricted
#     to include|exclude came back as `{"verdict": "REJECT"}`. The constraint was dropped
#     and nothing said so.
#   * `tools` reaching the payload as a body option (rather than the `tools` argument)
#     leaves has_tools=False, so the tools gate never fires — re-opening the
#     json_schema-silently-disables-tool-calling hole the gate exists to close.
#
# So: one door in, and it is the door with the gates on it.
_CONSTRAINT_BODY_KEYS = frozenset({"grammar", "json_schema", "response_format"})


def _guard_body_keys(source: str, keys: Iterable[str], *, model_id: str) -> None:
    """Reject transport and constraint fields arriving through a caller-supplied dict."""
    keyset = set(keys)

    reserved = _RESERVED_BODY_KEYS & keyset
    if reserved:
        # "stream" has a supported knob now (Model.stream / the per-call `stream`
        # option), so say where it lives rather than only that this door is shut —
        # a raise that names no alternative reads as "unsupported".
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
        # Highest precedence: per-call over Model.extra_body over τ defaults. Guarded
        # too — otherwise DecodeConstraints(json_schema=..., extra_body={"grammar": ...})
        # slips BOTH onto the wire while satisfying the exactly-one-of validator, and the
        # server picks a winner silently.
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
        # choices and grammar.choice() compile to the SAME grammar — which is why they
        # must verify the same way (see Grammar in tau_llm.grammar).
        grammar_text = grammar_mod.choice(*constraints.choices)
    elif constraints.grammar is not None:
        grammar_text = constraints.grammar
    else:
        # json_schema → OpenAI-style response_format. llguidance consumes JSON Schema
        # natively, so the SERVER compiles it; τ does not reimplement that compiler.
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": constraints.json_schema},
        }
        return

    # A grammar/choices-constrained call must not THINK. A user grammar applies from
    # the very first generated token, and a reasoning model's first token belongs to
    # its thinking — so llama-server dutifully forces the constrained answer into the
    # REASONING channel and returns `{"content": "", "reasoning_content": "include"}`.
    # The constraint held perfectly; the answer just is not where anyone reads it.
    #
    # τ reads `content`, sees "", and raises ConstraintViolation — which looks exactly
    # like "the server dropped the grammar" and is the opposite of what happened. Every
    # grammar-constrained call against a thinking model produced an empty verdict this way.
    #
    # Thinking is also pointless here: the answer is grammar-forced, so there is
    # nothing for reasoning to decide. Turn it off unless the caller has explicitly
    # taken control of the chat-template kwargs.
    #
    # json_schema → response_format is DELIBERATELY left thinking-enabled: it returns
    # above without reaching here. Its grammar is template-built and reasoning-aware
    # (llama.cpp upstream #20223), so it already works with thinking ON, and forcing it
    # off would be an unwarranted workaround.
    if "chat_template_kwargs" not in payload:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    # Header only for llguidance (never double-prefix; never prefix a gbnf grammar).
    if model.grammar_dialect == "llguidance":
        grammar_text = grammar_mod.with_header(grammar_text)
    payload["grammar"] = grammar_text


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

    ``timings`` is llama.cpp's per-completion telemetry block, a TOP-LEVEL
    sibling of ``usage`` on the final SSE chunk (not nested inside it). When
    non-empty it lands verbatim — keys unfiltered, unrenamed — on
    ``Usage.extra["timings"]``; stock builds omit ``n_ff_total`` and τ never
    fabricates it.
    """
    input_tokens = int(data.get("prompt_tokens") or 0)
    output_tokens = int(data.get("completion_tokens") or 0)
    total = int(data.get("total_tokens") or 0) or (input_tokens + output_tokens)
    details = data.get("prompt_tokens_details") or {}
    cache_read = int(details.get("cached_tokens") or 0)
    extra: dict[str, Any] = {}
    if timings:
        extra["timings"] = dict(timings)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        total_tokens=total,
        extra=extra,
    )


class OpenAICompletionsProvider(Provider):
    """Provider for OpenAI-compatible APIs (OpenAI, Ollama, vLLM, etc.).

    This is the only concrete provider in τ. It converts τ types to/from
    OpenAI API format and handles streaming responses.

    Reference: PHASE-1-SUBPHASE-2.md, "Implementation Outline" section.
    """

    # Single source of truth for the default endpoint, shared with
    # ``tau_llm.client``'s provider pool: the pool must resolve the SAME default
    # a bare ``base_url=None`` would fall back to here, or two calls that mean
    # the same server ("explicit https://api.openai.com/v1" vs "omitted") would
    # key to two different cache entries and lose the keep-alive win for no
    # reason (see docs/PROVIDER-LIFETIME.md §5).
    DEFAULT_BASE_URL: str = "https://api.openai.com/v1"

    # How long one completion may take, and how long its TCP connect may take.
    # Split because they answer different questions: connect failing is a wrong
    # or unreachable endpoint (seconds), while a completion legitimately takes
    # minutes on a local server decoding a long reasoning trace. Both are
    # overridable per provider (constructor) and per call
    # (``options["request_timeout"]``) — see ``_resolve_timeout``.
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

        # No fabricated fallback (Fail-Early): a missing key must surface as a
        # clear "No API key" error at request time, not a bogus key that the
        # upstream server rejects with a confusing 401. Mirrors pi, which throws
        # "No API key for provider" rather than inventing one
        # (openai-completions.ts:141).
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url or self.DEFAULT_BASE_URL
        # Validated HERE, not at first request: a bogus timeout in config is a
        # configuration error, and the construction site is where the operator
        # can still be told which value was wrong before a request is in flight.
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

    # ──────────────────────────────────────────────────────────────────
    # Conversion: τ → OpenAI
    # ──────────────────────────────────────────────────────────────────

    def _convert_messages_to_openai(
        self,
        messages: list,
        reasoning_replay: str = "turn",
        strict_reasoning_formats: bool = False,
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

        # Reasoning-replay scope (Model.reasoning_replay). "turn" replays a
        # message's chain-of-thought only when it belongs to the in-progress turn
        # — i.e. sits AFTER the last user message — so within-turn reasoning
        # survives across tool calls while stale cross-turn reasoning is dropped.
        # "all" replays every message's reasoning (pi-faithful); "off" replays
        # none. The boundary is computed once here since a per-message converter
        # can't see the whole list.
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

        for i, msg in enumerate(messages):
            include_reasoning = _replay_for(i)
            if isinstance(msg, UserMessage):
                openai_messages.append(self._convert_user_message(msg))
            elif isinstance(msg, AssistantMessage):
                openai_messages.append(
                    self._convert_assistant_message(
                        msg, include_reasoning, strict_reasoning_formats
                    )
                )
            elif isinstance(msg, ToolResultMessage):
                openai_messages.append(self._convert_tool_result(msg))
            elif isinstance(msg, dict):
                # Convert via _convert_message_dict to handle toolResult → tool,
                # content list → string, etc.
                openai_messages.append(
                    self._convert_message_dict(msg, include_reasoning, strict_reasoning_formats)
                )
            else:
                # Try to convert via model_dump
                if hasattr(msg, "model_dump"):
                    d = msg.model_dump()
                    openai_messages.append(
                        self._convert_message_dict(d, include_reasoning, strict_reasoning_formats)
                    )
                else:
                    openai_messages.append({"role": "user", "content": str(msg)})

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
        # str: the field name to replay under. dict: a payload from another
        # provider — see _foreign_signature below.
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

        # A dict signature is not a field name — it is another provider's opaque
        # payload (Anthropic's cryptographic thinking signature, say), and this is
        # the OpenAI writer. Using it as a key below would NOT raise; it would put
        # a blob where a field name belongs and send a valid-looking request that
        # means nothing. So drop it to the empty signature, which routes the
        # thinking through the same branch as a block that never had one: kept as
        # text content, never replayed under a key.
        # Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md S2/S4.
        if not isinstance(thinking_signature, str):
            self._on_foreign_thinking_signature(thinking_signature, strict_reasoning_formats)
            thinking_signature = ""

        result: dict[str, Any] = {"role": "assistant"}
        text = "".join(text_parts)
        # pi joins thinking blocks with "\n" when replaying them (one block after
        # consolidation; >1 only for legacy fragment-per-block messages).
        thinking = "\n".join(p for p in thinking_parts if p)

        if text:
            result["content"] = text
        elif tool_calls:
            # The tool call carries the turn — reasoning goes in its own field
            # (below), never as content.
            result["content"] = ""
        elif thinking and (not thinking_signature or not include_reasoning):
            # Thinking-only turn we won't (or can't) replay under a signature
            # field — keep it as content so the turn isn't dropped to an empty
            # message (Fail-Early; also the legacy no-signature case). ``off``/
            # out-of-turn scope only *suppresses the replay*, never the message.
            result["content"] = thinking
        else:
            result["content"] = ""

        if tool_calls:
            result["tool_calls"] = tool_calls

        # Replay reasoning to the SAME model under the exact field it streamed on
        # (the captured signature), so a multi-step turn keeps its chain-of-thought
        # — the chat template renders it into the per-turn <think> slot instead of
        # an empty block. Only when we actually captured the field (never guessed,
        # Fail-Early) AND the reasoning-replay scope allows it for this message
        # (Model.reasoning_replay; ``include_reasoning`` False drops the stale
        # cross-turn trace). Mirrors pi convertMessages (openai-completions.ts:874),
        # scoped by the τ knob.
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
        # Warn once per payload shape: a single foreign block would otherwise
        # re-warn on every turn for the rest of the session, since the message
        # stays in the replayed context.
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

    def _convert_tool_result(self, msg: ToolResultMessage) -> dict:
        """Convert ToolResultMessage to OpenAI tool role format."""
        # Join all text content blocks
        content_parts: list[str] = []
        for block in msg.content:
            if isinstance(block, TextContent):
                content_parts.append(block.text)
            elif isinstance(block, str):
                content_parts.append(block)

        result: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": msg.tool_call_id,
            "content": " ".join(content_parts),
        }

        return result

    def _convert_message_dict(
        self,
        d: dict,
        include_reasoning: bool = True,
        strict_reasoning_formats: bool = False,
    ) -> dict:
        """Convert a generic dict message to OpenAI format.

        Handles toolResult → tool role conversion, extracts tool_call_id,
        and converts list-type content to a string. ``include_reasoning`` carries
        the per-message reasoning-replay scope (this is the persisted/reload path,
        so it is the one that actually accretes stale reasoning on follow-up turns).
        ``strict_reasoning_formats`` carries ``Model.strict_reasoning_formats``.
        """
        role = d.get("role", "")
        content = d.get("content", "")

        if role == "assistant":
            # Persisted assistant content is a block list (text/thinking/toolCall).
            # Convert it like the pydantic path so a follow-up turn doesn't ship the
            # raw blocks the API rejects (HTTP 400 unsupported content[].type); a
            # plain-string body (older chats) passes straight through.
            if isinstance(content, list):
                return self._assistant_content_to_openai(
                    content, include_reasoning, strict_reasoning_formats
                )
            return {"role": "assistant", "content": content}
        elif role == "user":
            return {"role": "user", "content": content}
        elif role in ("toolResult", "tool"):
            # Extract tool_call_id
            tool_call_id = d.get("tool_call_id", "")

            # Convert list-type content to string (e.g. [{"type": "text", "text": "..."}])
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif "content" in block:
                            # Nested content block
                            text_parts.append(str(block["content"]))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = " ".join(text_parts)

            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content if isinstance(content, str) else "",
            }
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

    # ──────────────────────────────────────────────────────────────────
    # Conversion: OpenAI → τ
    # ──────────────────────────────────────────────────────────────────

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

    # ──────────────────────────────────────────────────────────────────
    # Streaming: event production
    # ──────────────────────────────────────────────────────────────────

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
            # Display path: arguments may still be mid-stream, so parse
            # leniently (best-effort, {} until enough has arrived).
            args_dict = parse_streaming_json("".join(tc.arguments_parts))
            content_blocks.append(ToolCall(id=tc.id, name=tc.name, arguments=args_dict))

        return AssistantMessage(
            # The vendor that actually answered, not the vendor whose wire format
            # it speaks. These were hardcoded, so a reply from Groq or a private
            # gateway was labelled "openai" in the transcript and in every export.
            api=model.api,
            provider=model.provider,
            content=content_blocks,
            model=model.id,
            response_id=accum.response_id,
            usage=Usage(),
            stop_reason="stop",
            timestamp=0,
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

        # Repair count over the COMPLETE tool-arg buffers only (not the
        # display-only partial-buffer path in parse_streaming_json, where a
        # repair is normal/expected). This is the only grammar-agnostic signal
        # for "did the model actually emit malformed JSON": a constrained tool
        # call should repair zero times.
        repairs = 0
        had_tool_call_with_args = False
        for tc in accum.tool_calls:
            # A tool call MUST name the function to invoke. Checked before the
            # arguments because it is the more fundamental half of the contract:
            # arguments without a name are unroutable no matter how well they parse.
            #
            # This is not hypothetical. An OpenAI-compatible gateway was observed
            # streaming tool-call chunks that never populate `function.name` for
            # some deployments while populating it for others on the same gateway
            # with byte-identical payloads (PLAN-0.9.3.md §4.2). τ transcribed the
            # empty name faithfully, `self._tools.get("")` missed in the agent
            # loop, and the run reported `Unknown tool: ` — which blames the model
            # for a wire-contract violation by the gateway, and then burned the
            # full max_turns budget repeating it.
            #
            # So: raise here, at the point where the fault is still attributable.
            # The finalize path already raises on an argument buffer that will not
            # decode (just below); the name was the one field it skipped. A second,
            # dead finalize path did carry a name check — and DROPPED such calls
            # silently, which is the same violation wearing a different hat. It has
            # since been deleted; this is now the only place a tool call is built.
            if not tc.name.strip():
                raise ValueError(
                    f"Tool call {tc.id!r} arrived with no function name "
                    f"(model {model.id!r} at {self.base_url!r}). The provider or "
                    "gateway never populated `function.name` on any chunk for this "
                    "call, which violates the OpenAI tool-calling wire contract — "
                    "a tool call must name the function to invoke. Refusing to "
                    "execute a nameless call. Arguments received: "
                    f"{''.join(tc.arguments_parts)!r}"
                )
            args_str = "".join(tc.arguments_parts)
            # Authoritative path: the stream is complete, so the arguments must
            # be valid JSON. A complete-but-unparseable payload is a real error
            # — raise (surfaced as an ErrorEvent) rather than fabricate args.
            if args_str.strip():
                had_tool_call_with_args = True
                args_dict, repaired = parse_json_with_repair_info(args_str)
                if repaired:
                    repairs += 1
                if not isinstance(args_dict, dict):
                    raise ValueError(
                        f"Tool call {tc.id!r} ({tc.name!r}) arguments did not decode "
                        f"to a JSON object: {args_str!r}"
                    )
            else:
                args_dict = {}
            content_blocks.append(ToolCall(id=tc.id, name=tc.name, arguments=args_dict))

        # Only a message with at least one NON-EMPTY tool-arg buffer has a
        # repair count to report. A message with no tool calls (or only
        # empty-argument ones) has nothing measured — `repairs: 0` there would
        # be a lie by omission-of-context, not a real "zero repairs" datum.
        if had_tool_call_with_args:
            usage = usage.model_copy(update={"extra": {**usage.extra, "repairs": repairs}})

        # Determine stop_reason: use explicit value, or fall back to heuristic
        if stop_reason is None:
            if accum.has_tool_calls:
                stop_reason = "toolUse"
            else:
                stop_reason = "stop"

        return AssistantMessage(
            # The vendor that actually answered, not the vendor whose wire format
            # it speaks. These were hardcoded, so a reply from Groq or a private
            # gateway was labelled "openai" in the transcript and in every export.
            api=model.api,
            provider=model.provider,
            content=content_blocks,
            model=model.id,
            response_id=accum.response_id,
            usage=usage,
            stop_reason=stop_reason,
            timestamp=0,
        )

    # ──────────────────────────────────────────────────────────────────
    # Main interface: stream_chat
    # ──────────────────────────────────────────────────────────────────

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

        # Resolve the API key (Fail-Early). The key may arrive via the
        # constructor (client.py builds the provider with options["api_key"]) or
        # directly in `options` when stream_chat is called without going through
        # client.py. A genuinely missing key must raise a clear error here rather
        # than send a bogus "Bearer None"/fake key that the server rejects as a
        # confusing 401. Local servers pass a truthy sentinel ("not-needed"),
        # which satisfies this check. Mirrors pi (openai-completions.ts:141).
        api_key = self.api_key or options.get("api_key")
        if not api_key:
            raise ValueError(
                f"No API key for provider: {getattr(model, 'provider', 'openai')}. "
                "Set OPENAI_API_KEY, pass api_key=..., or configure it in "
                '~/.tau/config.json (use "not-needed" for a local server).'
            )
        # Ensure the cached HTTP client's Authorization header uses the resolved
        # key (it may have come from options rather than the constructor).
        self.api_key = api_key

        # Convert τ messages to OpenAI format
        openai_messages = self._convert_messages_to_openai(
            messages, model.reasoning_replay, model.strict_reasoning_formats
        )

        # Convert tools to OpenAI format
        openai_tools = None
        if tools:
            openai_tools = self._convert_tools_to_openai(tools)

        # Build request payload. Ask for usage on the stream: without
        # `stream_options.include_usage` many OpenAI-compatible servers (notably
        # llama.cpp) never emit the trailing usage chunk, so token counts come
        # back as 0. pi sends this unconditionally (openai-completions.ts:522).
        # Placed before `**body_options` so an explicit caller override still wins.
        # `api_key` is a transport credential, not a request-body field;
        # `reasoning` is a τ-internal level (converted to `reasoning_effort`
        # below); `abort_signal` is a τ-internal cancellation handle (polled in the
        # stream loop); `request_timeout` is an HTTP-client setting — strip them all
        # so threading them through `options` never leaks them into the JSON body
        # (a non-serializable object would 400/raise).
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
                # A TRANSPORT MODE, not a body field. Stripped here (like
                # `request_timeout`) so that the value a caller passes selects which
                # code path reads the response, and the `stream` that reaches the
                # wire is always the one τ resolved and knows how to parse. It stays
                # in _RESERVED_BODY_KEYS, so `extra_body`/`DecodeConstraints` still
                # cannot smuggle it in as a raw key — those dicts do not go through
                # this strip and are guarded below.
                "stream",
            )
        }
        # Both caller-supplied dicts are guarded, not just Model.extra_body: a `tools`
        # or `grammar` key smuggled through per-call options bypasses the constraint
        # gates just as effectively as one in static config, and the per-call path is
        # the easier one to reach.
        _guard_body_keys("extra_body", model.extra_body.keys(), model_id=model.id)
        _guard_body_keys("per-call options", body_options.keys(), model_id=model.id)

        stream_mode = _resolve_stream_mode(options.get("stream"), model)
        # What THIS endpoint wants on the wire: the operator's `Model.compat` over
        # what τ infers from provider/base_url. Resolved once, read twice below.
        compat = resolve_compat(model)

        payload: dict[str, Any] = {
            "model": model.id,
            "messages": openai_messages,
            "stream": stream_mode,
            # Below **body_options: a per-call option always wins over the model's
            # static default.
            **model.extra_body,
            **body_options,
        }
        if stream_mode and compat.supports_usage_in_streaming:
            # `stream_options` is meaningful only ON a stream: OpenAI rejects it
            # alongside `stream: false`, and a buffered response carries `usage` in
            # the body unconditionally, so there is nothing to ask for. Assigned
            # after the spreads rather than merged into the literal because it is a
            # reserved key — neither caller dict can carry one past _guard_body_keys.
            #
            # `compat.supports_usage_in_streaming` is the escape for a gateway that
            # rejects the key. It costs the turn's token counts (a `usage` block
            # that never arrives reads as zero), which is why it is opt-out and
            # never inferred: τ asks for usage unless an operator says it cannot.
            payload["stream_options"] = {"include_usage": True}
        if openai_tools:
            payload["tools"] = openai_tools

        # `Model.max_tokens` is a REQUIRED field on every Model, and until this it
        # was never placed on the wire — declared and not consulted, the same defect
        # class that got the `settings` parameter removed from `create_agent_session`
        # ("a parameter whose only behaviour is rejection still advertises a
        # capability that does not exist"). The symptom is silent and expensive:
        # verified against llama-server, a Model with `max_tokens=512` produced a
        # slot reporting `n_predict = -1`, so generation ran unbounded against an
        # n_ctx of 262144 and one turn decoded ~120k tokens before anyone noticed.
        #
        # Skipped when the caller has already named a cap under either spelling —
        # via `Model.extra_body` or per-call options — which is the caller having
        # taken control, rather than sending two conflicting caps.
        #
        # WHICH spelling is a property of the endpoint: OpenAI's o-series and
        # gpt-5 family reject `max_tokens` and want `max_completion_tokens`, while
        # llama.cpp, vLLM and the classic Chat Completions API want `max_tokens`.
        # That used to be the fixed classic key with a comment explaining the
        # hazard; it is now `compat.max_tokens_field`, inferred from the endpoint
        # and overridable per model. Unrecognised endpoints still get `max_tokens`,
        # so nothing that worked before changes.
        if not any(key in payload for key in ("max_tokens", "max_completion_tokens")):
            payload[compat.max_tokens_field] = model.max_tokens

        _apply_constraints(payload, model, options.get("constraints"), has_tools=bool(openai_tools))

        # Reasoning / thinking effort. The requested level arrives as the
        # τ-internal `reasoning` option; clamp it to what the model supports,
        # then map "off" → don't send (pi: streamSimple clamp at
        # openai-completions.ts:441-442, default "openai" thinkingFormat send at
        # :620-628). Only sent when the model declares reasoning support
        # (Fail-Early: never send `reasoning_effort` to a non-reasoning model,
        # which would 400). pi additionally gates on a per-provider
        # `compat.supportsReasoningEffort` auto-detected from the URL; τ has no
        # such machinery, so `Model.reasoning` is the single gate.
        requested = options.get("reasoning")
        if requested is not None and getattr(model, "reasoning", False):
            clamped = clamp_thinking_level(model, requested)
            tlm = model.thinking_level_map or {}
            if clamped != "off":
                mapped = tlm.get(clamped, clamped)
            else:
                # "off" is the level whose default meaning is "ask for nothing", so
                # it sends nothing unless the map names a concrete value for it.
                mapped = tlm.get("off")
            if isinstance(mapped, dict):
                _merge_thinking_fragment(payload, mapped, model_id=model.id)
            elif isinstance(mapped, str):
                payload["reasoning_effort"] = mapped

        client = self._get_client()
        accum = _Accumulator()

        # Per-call timeout override. Resolved (and validated) here rather than
        # mutating `self`: providers are POOLED and shared across models by
        # (provider, base_url, key hash) in client.py, so a per-call value stored
        # on the instance would silently retime every other caller's requests.
        # Passing it to `client.stream(...)` keeps it scoped to this request and
        # leaves the pool key untouched.
        # Precedence, narrowest first: this call's option, then the model's own
        # ``request_timeout`` from config, then the provider default. The model
        # tier is what makes the knob usable — a slow local model and a flaky
        # gateway want different patience, and both are configured per-model.
        _per_call = options.get("request_timeout")
        _configured = _per_call if _per_call is not None else model.request_timeout
        request_timeout = self._resolve_timeout(
            _configured if _configured is not None else self.request_timeout
        )

        async def event_generator() -> AsyncIterator[Any]:
            try:
                # The transport fills `accum` and `state`, yielding delta events as
                # it goes; everything after it is transport-agnostic. Which one runs
                # is the ONLY difference between streaming and non-streaming mode —
                # in particular the final message is built once, below, by the
                # finalize path with the Fail-Early guards on it.
                state = _TransportState()
                transport = (
                    self._stream_transport(
                        client, payload, request_timeout, accum, state, model, abort_signal
                    )
                    if stream_mode
                    else self._complete_transport(
                        client, payload, request_timeout, accum, state, model, abort_signal
                    )
                )
                async for event in transport:
                    yield event
                if state.failed:
                    return

                usage_data = state.usage_data
                timings_data = state.timings_data
                final_stop_reason = state.stop_reason

                # Stream ended ([DONE] or closed). Emit the final message with
                # whatever usage (and timings) arrived, including a trailing
                # usage-only chunk. `_usage_from_openai({}, {})` already yields
                # an all-zero, extra-less Usage identical to `Usage()`, so no
                # separate empty-data branch is needed.
                usage_obj = _usage_from_openai(usage_data, timings_data)
                final_msg = self._build_final_message(accum, model, usage_obj, final_stop_reason)

                # Verify the constraint actually held (§4.3). This is the single choke
                # point for BOTH streaming and complete_simple, so no constrained result
                # can be returned unverified.
                #
                # Read stop_reason off the MESSAGE, not off `final_stop_reason`: a server
                # that closes the SSE without a finish_reason leaves the local None while
                # _build_final_message heuristically reports "stop". Gating on the local
                # would then skip verification on a message that claims a clean stop.
                #
                # "length" is a HARD failure for a constrained generation, not a
                # different-failure-already-visible-elsewhere. Nothing downstream inspects
                # stop_reason — ctx.complete() raises only on error/aborted — so a
                # truncated `{"verdict": "include", "confidence": 0.` would be handed to a
                # caller as a successful constrained result and json.loads'd. Truncation
                # means the constraint did not complete; say so.
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

                # Emit one final tool-call delta per call, derived from the
                # already-parsed ToolCall blocks on final_msg (no re-parse).
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
                # NOT a transport error. A violation means the server returned an
                # UNCONSTRAINED generation while we asked for a constrained one — a
                # correctness failure the caller must be able to catch by type (and
                # whose `.output` it needs). Laundering it into a generic
                # "Streaming error" string would bury exactly the information that
                # makes it actionable. Re-raise; the stream wrapper preserves it.
                raise
            except Exception as e:
                # Name the model and the endpoint as well as the fault. Most
                # failures that reach here are transport failures, and the first
                # question about one is always "which server?" — a fleet behind one
                # τ config can have several, and the answer is not in the
                # exception. See `_describe_exception` for why `str(e)` alone is
                # not enough to build a message from.
                #
                # Unless the exception already said so: τ's own validation raises
                # (a nameless tool call, an undecodable argument buffer) name the
                # endpoint themselves, and repeating it would print the same URL
                # twice in one sentence. This is pi's `messageCarriesBody` test
                # (utils/error-body.ts:84) applied to the endpoint instead of the
                # body — same idea, don't double-print what the message carries.
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

    # ──────────────────────────────────────────────────────────────────
    # Transports. Each fills an `_Accumulator` + a `_TransportState` and yields
    # delta events; neither builds a final message (see stream_chat's tail).
    # ──────────────────────────────────────────────────────────────────

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
            # `error` is an object on OpenAI and most gateways, but
            # some send a bare string. Neither shape may reach
            # `.get()` unguarded — the string one used to raise
            # AttributeError into the handler below, replacing a
            # real HTTP status with an opaque "Streaming error".
            err = error_body.get("error")
            if isinstance(err, dict):
                error_msg = str(err.get("message") or "")
            elif isinstance(err, str):
                error_msg = err
        if not error_msg:
            # No parseable error message: quote the raw body rather
            # than repeat the status code back as its own
            # explanation. A proxy's HTML 502 page says which hop
            # failed; `HTTP 502: HTTP 502` says nothing twice.
            error_msg = _truncate_error_text(response.text.strip())
        return ErrorEvent(
            type="error",
            message=(
                f"HTTP {response.status_code} from model {model.id!r} at "
                f"{self.base_url!r}: {error_msg or '(empty response body)'}"
            ),
            is_error=True,
        )

    async def _stream_transport(
        self,
        client: Any,
        payload: dict[str, Any],
        request_timeout: httpx.Timeout,
        accum: _Accumulator,
        state: _TransportState,
        model: Model,
        abort_signal: Any,
    ) -> AsyncIterator[Any]:
        """SSE transport: read `data:` frames and yield a delta event per fragment."""
        # `client.stream(...)` keeps the HTTP body OPEN and yields SSE
        # lines as they arrive. `client.post(...)` (the old call) buffered
        # the WHOLE response before returning, so every reasoning/text delta
        # only surfaced in one burst at the end — the "reasoning invisible
        # until complete" bug. pi streams the fetch body the same way.
        async with client.stream(
            "POST", "/chat/completions", json=payload, timeout=request_timeout
        ) as response:
            if response.status_code != 200:
                # A streaming response's body is not read yet; pull it in
                # so the provider's error message can be surfaced.
                await response.aread()
                yield self._error_event_from_response(response, model)
                state.failed = True
                return

            # Read SSE lines as they arrive (no full-body buffering).
            async for line in response.aiter_lines():
                # Cooperative cancellation: an abort raised mid-completion
                # stops the stream here (exiting `async with` closes the
                # connection) instead of draining the whole response. The
                # partial accumulated so far is finalized with an
                # "aborted" stop_reason. pi aborts the fetch the same way.
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

                # A frame that decodes to something other than an object is
                # not a completion chunk. Proxies and gateways emit `data: []`
                # or a bare scalar as a keepalive, and `chunk.get(...)` below
                # would raise AttributeError into the broad handler at the
                # bottom of stream_chat's generator — turning a keepalive into a
                # failed turn. pi skips the same shape (openai-completions.ts:510).
                #
                # Logged rather than dropped in silence: this path is also
                # where genuinely malformed output from a broken gateway lands,
                # and "the model said nothing" with no record of why is the
                # failure mode this whole section exists to remove. Debug level
                # because the expected case is a keepalive, which is normal.
                if not isinstance(chunk, dict):
                    _logger.debug(
                        "skipping non-object SSE frame (%s): %r",
                        type(chunk).__name__,
                        data_str,
                    )
                    continue

                # Usage and id can arrive in a trailing chunk whose `choices`
                # is empty (llama.cpp; OpenAI stream_options.include_usage).
                # Read them BEFORE the empty-choices guard or the token counts
                # are silently dropped.
                if chunk.get("id"):
                    accum.response_id = chunk["id"]
                chunk_usage = chunk.get("usage")
                if chunk_usage:
                    state.usage_data = chunk_usage
                chunk_timings = chunk.get("timings")
                if chunk_timings:
                    state.timings_data = chunk_timings

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
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

                # Process reasoning/thinking delta — first non-empty of
                # reasoning_content / reasoning / reasoning_text, yielded live.
                # (Previously accumulated but never yielded, so reasoning never
                # streamed to the UI.)
                reasoning, reasoning_field = _extract_reasoning(delta)
                if reasoning:
                    accum.thinking_parts.append(reasoning)
                    if not accum.thinking_signature:
                        accum.thinking_signature = reasoning_field
                    accum.has_thinking = True
                    partial = self._build_partial_message(accum, model)
                    yield self._make_thinking_event(reasoning, accum, partial)

                # Process tool call deltas. OpenAI streams name and arguments
                # as incremental FRAGMENTS, one piece per chunk — concatenate
                # them. Route each fragment to its call by stream `index`
                # (falling back to `id`), since follow-up argument fragments
                # carry only the index.
                deltas = delta.get("tool_calls", [])
                for i, tc_delta in enumerate(deltas):
                    block = _resolve_tool_call_block(accum, tc_delta, i)
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

                # Record finish, but DON'T return yet: usage may arrive in a
                # later chunk (servers emit finish_reason and usage in separate
                # chunks). Returning here would drop that usage.
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
        # Cancellation, as far as this transport can honour it: a buffered request
        # is one round trip with nothing to poll between, so the abort is checked
        # at the two points that exist. Before the request, an already-aborted turn
        # is not sent at all. After it, the completion is real and PAID FOR — its
        # text and its usage are kept and reported, marked "aborted" exactly as the
        # streaming path marks a turn whose abort landed after the last delta. What
        # cannot be reproduced is stopping mid-completion; see Model.stream.
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
        # llama.cpp's `timings` sits top-level on a buffered response too, exactly
        # as it does on the final SSE chunk — same key, same shape, same handling.
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
            # τ never sends `n`, so >1 means a caller asked for it through body
            # options (or the server ignored the default). Only the first is used —
            # the streaming path reads choices[0] too — but say so rather than drop
            # the rest in silence.
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

        # Thinking BEFORE text: that is the order a reasoning model streams in and
        # the order `_consolidate_text_and_thinking` puts the blocks in, so a
        # consumer diffing partials sees the same progression either way.
        reasoning, reasoning_field = _extract_reasoning(message)
        if reasoning:
            accum.thinking_parts.append(reasoning)
            accum.thinking_signature = reasoning_field
            accum.has_thinking = True
            partial = self._build_partial_message(accum, model)
            yield self._make_thinking_event(reasoning, accum, partial)

        content = message.get("content")
        if content is not None and not isinstance(content, str):
            # OpenAI's non-streaming `message.content` is a string or null. Anything
            # else is a wire-contract violation, and guessing which field of a list
            # of blocks holds the answer is how a silently-wrong transcript starts.
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

        # Tool calls arrive COMPLETE — name and arguments in one piece each, and
        # every parallel call present at once. Routed through the same resolver as
        # the streaming fragments (so the id/index bookkeeping is identical) and
        # appended whole; concatenating one fragment is concatenating one fragment.
        tool_calls = message.get("tool_calls") or []
        for i, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                raise ValueError(
                    f"Non-streaming completion from model {model.id!r} at "
                    f"{self.base_url!r} returned a non-object tool call: {tc!r}"
                )
            block = _resolve_tool_call_block(accum, tc, i)
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
