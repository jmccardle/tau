"""The Anthropic provider: the Messages API over SSE.

Walk to request in `conversation` and `AnthropicProvider._request`, response to parts and deltas
in `_Stream`, one per reply. The request carries `model`, `max_tokens`, `messages`, `stream`,
`system`, `tools` when there are any, the options (`_OPTIONS`), then `extra_body` last.

Replay data (`Replay("anthropic", data)`, `data` the text of a JSON object) is the block as the
API must get it back: `{"type": "thinking", "signature": ...}` on a `Thinking` part (its text is
the block's `thinking`), and `{"type": "redacted_thinking", "data": ...}` on a `Thinking("")`.
"""

import json
import os
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

import httpx

from tau._config import ConfigError, ModelConfig
from tau._core import BadReplay, Node, Part, Replay, Stop
from tau._events import CallArgs, CallNamed, Delta, TextDelta, ThinkingDelta
from tau._protocols import Finish, SampleError, Tool
from tau._providers._anthropic_request import breakpoints, conversation, tools_array
from tau._providers._wire import (
    Connection,
    Json,
    count,
    extra_body_problems,
    finish,
    is_count,
    json_object,
    note_usage,
    option_problems,
    plain,
)

NAME = "anthropic"
API_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"  # the `anthropic-version` header
MIN_BUDGET = 1024  # the smallest `budget_tokens` the API takes
_OPTIONS = (
    "thinking",
    "effort",
    "temperature",
    "top_p",
    "top_k",
    "prompt_caching",
    "extra_body",
)
_OWNED_KEYS = ("model", "messages", "system", "stream", "tools", "max_tokens")

STOPS: dict[str, Stop] = {
    "end_turn": Stop.End,
    "stop_sequence": Stop.End,
    "tool_use": Stop.Tools,
    "max_tokens": Stop.Length,
    "model_context_window_exceeded": Stop.Length,
    "pause_turn": Stop.Length,  # a server-tool loop paused: the reply is not finished
    "refusal": Stop.End,
}

# The HTTP status an `error` event's `type` stands for (api/errors): a stream error comes after a
# 200, so its status is the one the same failure would have had before the stream began.
ERROR_STATUS: dict[str, int] = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "billing_error": 402,
    "permission_error": 403,
    "not_found_error": 404,
    "conflict_error": 409,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "timeout_error": 504,
    "overloaded_error": 529,
}


# Response to parts


@dataclass
class _Block:
    """The content block being accumulated."""

    kind: str  # "text" | "thinking" | "redacted_thinking" | "tool_use"
    index: int
    call: int = 0  # the number of this call among the reply's calls
    id: str = ""
    tool: str = ""
    data: str = ""  # a redacted block's `data`
    signature: str = ""
    chunks: list[str] = field(default_factory=lambda: [])

    def part(self) -> Part:
        try:
            return self._part()
        except BadReplay as e:
            raise SampleError(f"block {self.index} has replay data that cannot be kept: {e}") from e

    def _part(self) -> Part:
        text = "".join(self.chunks)
        match self.kind:
            case "text":
                return Part.Text(text)
            case "thinking":
                if not self.signature:
                    return Part.Thinking(text)
                data = json.dumps({"type": "thinking", "signature": self.signature})
                return Part.Thinking(text, Replay(NAME, data))
            case "redacted_thinking":
                data = json.dumps({"type": "redacted_thinking", "data": self.data})
                return Part.Thinking("", Replay(NAME, data))
            case _:
                return Part.Call(self.id, self.tool, text or "{}")


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise SampleError(f"{what} is not a string: {value!r}")
    return value


def _usage(raw: Json) -> Json | None:
    """The normalized `{"input", "output"}`, with `cached` and `written` when Anthropic reports
    them, from Anthropic's `usage`; `None` if it did not report input and output tokens or sent a
    malformed count. `input` is every prompt token: Anthropic's `input_tokens` counts only those
    after the last cache breakpoint, so the cache reads and the cache writes
    (`cache_creation_input_tokens`: prompt tokens that were processed and stored, not served from
    the cache) are added. `cached` is the reads alone, `written` the writes alone; a count the
    response leaves out is absent, not 0."""
    fresh, output = count(raw.get("input_tokens")), count(raw.get("output_tokens"))
    if fresh is None or output is None:
        return None
    reported: Json = {}
    for key, name in (
        ("cached", "cache_read_input_tokens"),
        ("written", "cache_creation_input_tokens"),
    ):
        if raw.get(name) is not None:
            n = count(raw[name])
            if n is None:
                return None
            reported[key] = n
    total = fresh + sum(cast("int", v) for v in reported.values())
    return {"input": total, "output": output, **reported}


@dataclass
class _Stream:
    """One response being read. `feed` takes a decoded event and returns its parts and deltas."""

    open: _Block | None = None
    stop_reason: str | None = None
    calls: int = 0
    usage: Json = field(default_factory=lambda: Json())  # noqa: PLW0108 - typed for pyright
    meta: Json = field(default_factory=lambda: Json())  # noqa: PLW0108 - typed for pyright

    def _note_usage(self, raw: object) -> None:
        """`usage` objects accumulate: `message_start` opens them, each `message_delta` carries
        the cumulative counts, so a later field replaces an earlier one."""
        if not isinstance(raw, dict):
            return
        self.usage.update({k: v for k, v in cast("Json", raw).items() if v is not None})
        note_usage(self.meta, dict(self.usage), _usage)

    @property
    def done(self) -> bool:
        return self.stop_reason is not None

    def _start(self, event: Json) -> list[Part | Delta]:
        message = json_object(event.get("message"), "message_start's message")
        for key in ("id", "model"):
            if isinstance(message.get(key), str):
                self.meta[key] = message[key]
        self._note_usage(message.get("usage"))
        return []

    def _begin(self, event: Json) -> list[Part | Delta]:
        index = event.get("index")
        if not isinstance(index, int):
            raise SampleError(f"a content block has no integer index: {index!r}")
        if self.open is not None:
            raise SampleError(f"content block {index} began while block {self.open.index} was open")
        block = json_object(event.get("content_block"), "a content block")
        kind = _str(block.get("type"), "a content block's type")
        self.open = _Block(kind, index)
        out: list[Part | Delta] = []
        match kind:
            case "text":
                out += self._append(TextDelta, block.get("text"))
            case "thinking":
                out += self._append(ThinkingDelta, block.get("thinking"))
                self.open.signature = _str(block.get("signature", ""), "a signature")
            case "redacted_thinking":
                self.open.data = _str(block.get("data"), "redacted thinking's data")
            case "tool_use":
                if block.get("input"):
                    raise SampleError(f"tool_use block {index} began with input: {block['input']}")
                self.open.call = self.calls
                self.calls += 1
                self.open.id = _str(block.get("id"), "a tool_use id")
                self.open.tool = _str(block.get("name"), "a tool_use name")
                out.append(CallNamed(self.open.call, self.open.id, self.open.tool))
            case _:
                raise SampleError(
                    f"content block {index} is a {kind!r}, which this provider does not read"
                )
        return out

    def _append(
        self, make: type[TextDelta] | type[ThinkingDelta], text: object
    ) -> list[Part | Delta]:
        """Add streamed text to the open block, as a delta (an empty string is not one)."""
        assert self.open is not None
        if not isinstance(text, str):
            raise SampleError(f"{self.open.kind} block {self.open.index} has no text: {text!r}")
        if not text:
            return []
        self.open.chunks.append(text)
        return [make(text)]

    def _delta(self, event: Json) -> list[Part | Delta]:
        block = self.open
        if block is None or event.get("index") != block.index:
            raise SampleError(
                f"a delta for block {event.get('index')!r}, which is not the open one"
            )
        delta = json_object(event.get("delta"), "a delta")
        kind = delta.get("type")
        if kind == "text_delta" and block.kind == "text":
            return self._append(TextDelta, delta.get("text"))
        if kind == "thinking_delta" and block.kind == "thinking":
            return self._append(ThinkingDelta, delta.get("thinking"))
        if kind == "signature_delta" and block.kind == "thinking":
            block.signature = _str(delta.get("signature"), "a signature")
            return []
        if kind == "input_json_delta" and block.kind == "tool_use":
            args = _str(delta.get("partial_json"), "partial_json")
            if args:
                block.chunks.append(args)
            return [CallArgs(block.call, args)] if args else []
        raise SampleError(f"a {kind!r} delta in a {block.kind} block")

    def _end(self, event: Json) -> list[Part | Delta]:
        """Complete the open block. A call with no arguments streamed gets `{}` as its
        arguments, as a delta first, so the part equals its deltas."""
        block = self.open
        if block is None:
            raise SampleError("a content_block_stop with no block open")
        if event.get("index") != block.index:
            raise SampleError(
                f"a content_block_stop for block {event.get('index')!r}, "
                f"but block {block.index} is the open one"
            )
        out: list[Part | Delta] = []
        if block.kind == "tool_use" and not block.chunks:
            block.chunks.append("{}")
            out.append(CallArgs(block.call, "{}"))
        out.append(block.part())  # if it raises, the block stays open for `close`
        self.open = None
        return out

    def _message_delta(self, event: Json) -> list[Part | Delta]:
        if self.open is not None:
            raise SampleError(f"the message ended with content block {self.open.index} open")
        delta = json_object(event.get("delta"), "message_delta's delta")
        reason = delta.get("stop_reason")
        if isinstance(reason, str):
            self.stop_reason = reason
        if delta.get("stop_details") is not None:
            self.meta["stop_details"] = delta["stop_details"]
        self._note_usage(event.get("usage"))
        return []

    def feed(self, event: Json) -> list[Part | Delta]:
        """The parts and deltas this event produced, in order: a part comes before the first
        delta of whatever follows it. Event types it does not know are skipped (the API adds
        them over time)."""
        kind = event.get("type")
        if kind == "error":
            raise stream_error(event)
        handlers: dict[str, Callable[[Json], list[Part | Delta]]] = {
            "message_start": self._start,
            "content_block_start": self._begin,
            "content_block_delta": self._delta,
            "content_block_stop": self._end,
            "message_delta": self._message_delta,
        }
        handler = handlers.get(kind) if isinstance(kind, str) else None
        return [] if handler is None else handler(event)

    def close(self) -> list[Part | Delta]:
        """The open block as the part it had become, if it is one. Text and thinking are kept as
        far as they arrived, thinking unsigned (a signed part cut off is unsigned, D15); a
        redacted block arrives whole. A call forming at a cut is dropped whole: its raw text goes
        in `meta["cut_off_call"]` (D1)."""
        block, self.open = self.open, None
        if block is None:
            return []
        if block.kind == "tool_use":
            args = "".join(block.chunks)
            self.meta["cut_off_call"] = (
                f"tool_use {block.id} {block.tool}, arguments so far {args!r}"
            )
            return []
        if block.kind == "redacted_thinking":
            try:
                return [block.part()]
            except SampleError:
                return []  # the failure being raised already says the stream broke
        if not block.chunks:
            return []
        text = "".join(block.chunks)
        return [Part.Thinking(text) if block.kind == "thinking" else Part.Text(text)]

    def stop(self) -> Finish:
        return finish(self.stop_reason, STOPS, "stop_reason", self.meta)


def stream_error(event: Json) -> SampleError:
    """The `SampleError` for an `error` event, with the status that error type stands for."""
    error = event.get("error")
    if not isinstance(error, dict):
        return SampleError(f"the server sent an error in the stream: {error!r}", status=500)
    fields = cast("Json", error)
    kind = fields.get("type")
    message = fields.get("message")
    status = ERROR_STATUS.get(kind) if isinstance(kind, str) else None
    return SampleError(
        f"the server sent an error in the stream: {kind}: {message}",
        status=status,
    )


# Options


def _thinking_problems(value: object, max_tokens: int | None) -> list[str]:
    """Types only: the API judges the values. The settings that need an `anthropic-beta` header,
    which this provider does not send, are refused (they can never work here): `display`
    `updates` and `block_binding` (betas `thinking-display-updates-2026-08-18` and
    `thinking-binding-controls-2026-08-01`)."""
    if not isinstance(value, Mapping):
        return ["option 'thinking' must be an object"]
    fields = cast("Mapping[str, object]", value)
    kind = fields.get("type")
    if not (isinstance(kind, str) and kind):
        return ["option 'thinking' needs a 'type' string"]
    problems: list[str] = []
    if "display" in fields and not isinstance(fields["display"], str):
        problems.append("option 'thinking' display must be a string")
    if fields.get("display") == "updates":
        problems.append(
            "option 'thinking' display 'updates' needs a beta header this provider cannot send"
        )
    if "block_binding" in fields:
        problems.append(
            "option 'thinking' block_binding needs a beta header this provider cannot send"
        )
    if "budget_tokens" in fields and not is_count(fields["budget_tokens"]):
        problems.append("option 'thinking' budget_tokens must be a non-negative integer")
    elif kind == "enabled":
        budget = fields.get("budget_tokens")
        if not is_count(budget) or cast("int", budget) < MIN_BUDGET:
            problems.append(
                f"option 'thinking' of type 'enabled' needs budget_tokens >= {MIN_BUDGET}"
            )
        elif max_tokens is not None and cast("int", budget) >= max_tokens:
            problems.append("option 'thinking' budget_tokens must be less than max_tokens")
    return problems


def _typed_body(model: ModelConfig) -> Json:
    """The request keys the model's own settings set, with their values."""
    options = model.options
    body: Json = {}
    if "thinking" in options:
        body["thinking"] = plain(options["thinking"])
    if "effort" in options:
        body["output_config"] = {"effort": options["effort"]}
    body.update({k: options[k] for k in ("temperature", "top_p", "top_k") if k in options})
    return body


def _extra_body_problems(model: ModelConfig, extra: object) -> list[str]:
    problems = extra_body_problems(extra, _OWNED_KEYS, _typed_body(model))
    if isinstance(extra, Mapping):
        fields = cast("Mapping[str, object]", extra)
        if "thinking" in fields:
            problems.append(
                "option 'extra_body' may not set 'thinking'; the 'thinking' option covers it"
            )
        if "context_management" in fields:
            problems.append(
                "option 'extra_body' sets 'context_management', which needs a beta header"
            )
    return problems


def _option_problems(model: ModelConfig) -> list[str]:
    options = model.options
    problems = option_problems(
        model, _OPTIONS, numbers=("temperature", "top_p"), flags=("prompt_caching",)
    )
    if "top_k" in options and not is_count(options["top_k"]):
        problems.append("option 'top_k' must be a non-negative integer")
    if "effort" in options and not (isinstance(options["effort"], str) and options["effort"]):
        problems.append("option 'effort' must be a non-empty string")
    if "thinking" in options:
        problems += _thinking_problems(options["thinking"], model.max_tokens)
    if "extra_body" in options:
        problems += _extra_body_problems(model, options["extra_body"])
    return problems


class AnthropicProvider:
    """Samples the Anthropic Messages API (`/v1/messages`) for the `ModelConfig` the driver
    resolved. `base_url` is the API's root, `https://api.anthropic.com` when unset (a proxy
    that speaks the same API may be named; give no `/v1`).

    One client serves every sample (see `Connection`). `transport` replaces the network (a
    test's `httpx.MockTransport`); `environ` is where `api_key_env` is looked up, at sample
    time.

    `check` requires a key (`api_key_env`) and `max_tokens` (the API requires it; there is no
    default), and reads the options in `_OPTIONS`, refusing any other: `thinking` (an object as
    the API takes it: `type` a string, `budget_tokens` for `enabled`, `display`; the API judges
    the values, and `check` refuses only what needs a beta header this provider cannot send),
    `effort` (a string, sent as `output_config.effort`),
    `temperature`, `top_p` (numbers), `top_k` (an integer), `prompt_caching` (a boolean,
    default true: mark the system text and the last message for caching), and `extra_body` (an
    object merged into the request body last; it may not set `model`, `messages`, `system`,
    `stream`, `tools`, `max_tokens`, `thinking`, or a key the model's settings set).
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._connection = Connection(
            transport,
            timeout,
            meta_headers={"request-id": "request_id"},  # what support asks for
        )

    @property
    def name(self) -> str:
        """`"anthropic"`: what a model's `provider` setting calls it."""
        return NAME

    def check(self, model: ModelConfig) -> list[str]:
        """Problems with the model's settings as this provider reads them."""
        problems: list[str] = []
        if model.max_tokens is None:
            problems.append(
                "needs max_tokens: the Messages API requires it and there is no default"
            )
        if model.api_key_env is None and model.literal_api_key is None:
            problems.append("needs an API key: set api_key_env to the variable that holds it")
        if model.base_url is not None and model.base_url.rstrip("/").endswith("/v1"):
            problems.append("base_url is the API's root: this provider adds /v1/messages itself")
        return problems + _option_problems(model)

    async def aclose(self) -> None:
        """Close the client and its connections. Later samples fail."""
        await self._connection.aclose()

    def _request(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> tuple[str, dict[str, str], Json]:
        """The URL, headers, and JSON body of the request for this sample. `check` has passed
        (the driver runs it first).

        Raises `SampleError` if the model's key cannot be read.
        """
        try:
            key = model.api_key(self._environ)
        except ConfigError as e:
            raise SampleError(str(e)) from e
        assert key is not None  # `check` required one
        headers = {"x-api-key": key, "anthropic-version": API_VERSION}
        options = model.options
        thinking = options.get("thinking")
        manual = (
            isinstance(thinking, Mapping)
            and cast("Mapping[str, object]", thinking).get("type") == "enabled"
        )
        system, messages = conversation(walk, manual_thinking=manual, has_tools=bool(tools))
        system_value: object = system
        if options.get("prompt_caching", True):
            system_value = breakpoints(system, messages)
        body: Json = {
            "model": model.model,
            "max_tokens": model.max_tokens,
            "messages": messages,
            "stream": True,
        }
        if system_value is not None:
            body["system"] = system_value
        if tools:
            body["tools"] = tools_array(tools)
        body.update(_typed_body(model))
        if "extra_body" in options:
            body.update(cast("Json", plain(options["extra_body"])))
        base = API_URL if model.base_url is None else model.base_url.rstrip("/")
        return base + "/v1/messages", headers, body

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        """Yield deltas as they arrive and each part once complete (before the first delta of
        whatever follows it), then the `Finish`. A failure raises `SampleError` (with `status`
        and `retry_after` for an HTTP error, and a `status` for an `error` event of a known
        type) after yielding every part the stream had finished. Once `stop_reason` has arrived
        the reply is done: the stream is still read for the final `usage`, and a reset, an
        `error` event, or malformed SSE after it costs only the usage not yet received."""
        url, headers, body = self._request(walk, model, tools)
        async for item in self._connection.sample(url, headers, body, _Stream()):
            yield item
