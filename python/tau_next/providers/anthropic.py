"""The Anthropic provider: the Messages API over SSE.

Walk to request in `conversation` and `AnthropicProvider.request`, response to parts and deltas
in `_Stream`, one per reply. The request carries `model`, `max_tokens`, `messages`, `stream`,
`system`, `tools` when there are any, the options (`_OPTIONS`), then `extra_body` last.

Replay data (`Replay("anthropic", data)`, `data` the text of a JSON object) is the block as the
API must get it back: `{"type": "thinking", "signature": ...}` on a `Thinking` part (its text is
the block's `thinking`), and `{"type": "redacted_thinking", "data": ...}` on a `Thinking("")`.
"""

import asyncio
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

import httpx

from tau_next._config import ConfigError, ModelConfig
from tau_next._core import BadReplay, Node, Part, Replay, Stop
from tau_next.events import CallArgs, CallNamed, Delta, TextDelta, ThinkingDelta
from tau_next.protocols import Finish, ProviderClosed, SampleError, Tool
from tau_next.providers._anthropic_request import breakpoints, conversation, tools_array
from tau_next.providers._wire import (
    HTTP_OK,
    Json,
    is_count,
    is_number,
    non_finite,
    plain,
    retry_after,
    sse_data,
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
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_THINKING_TYPES = ("adaptive", "enabled", "disabled", "between_tools")
_DISPLAYS = ("summarized", "omitted")

STOPS: dict[str, Stop] = {
    "end_turn": Stop.End,
    "stop_sequence": Stop.End,
    "tool_use": Stop.Tools,
    "max_tokens": Stop.Length,
    "model_context_window_exceeded": Stop.Length,
    "pause_turn": Stop.Length,  # a server-tool loop paused: the reply is not finished
    "refusal": Stop.Refused,
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


def _json(value: object, what: str) -> Json:
    if not isinstance(value, dict):
        raise SampleError(f"{what} is not a JSON object: {value!r}")
    return cast("Json", value)


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise SampleError(f"{what} is not a string: {value!r}")
    return value


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and is_count(value) else None


def _usage(raw: Json) -> Json | None:
    """The normalized `{"input", "output", "cached"}` from Anthropic's `usage`, or `None` if it
    did not report input and output tokens or sent a malformed count. `input` is every prompt
    token: Anthropic's `input_tokens` counts only those after the last cache breakpoint, so the
    cache reads and the cache writes (`cache_creation_input_tokens`: prompt tokens that were
    processed and stored, not served from the cache) are added. `cached` is the reads alone."""
    fresh, output = _count(raw.get("input_tokens")), _count(raw.get("output_tokens"))
    if fresh is None or output is None:
        return None
    parts = [raw.get("cache_read_input_tokens"), raw.get("cache_creation_input_tokens")]
    read, written = (0 if p is None else _count(p) for p in parts)
    if read is None or written is None:
        return None
    return {"input": fresh + read + written, "output": output, "cached": read}


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
        self.usage.update(cast("Json", raw))
        self.meta["usage_raw"] = dict(self.usage)
        normal = _usage(self.usage)
        if normal is None:
            self.meta.pop("usage", None)
        else:
            self.meta["usage"] = normal

    def meta_text(self) -> str | None:
        return json.dumps(self.meta) if self.meta else None

    def _start(self, event: Json) -> list[Part | Delta]:
        message = _json(event.get("message"), "message_start's message")
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
        block = _json(event.get("content_block"), "a content block")
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
        delta = _json(event.get("delta"), "a delta")
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

    def _end(self, _event: Json) -> list[Part | Delta]:
        """Complete the open block. A call with no arguments streamed gets `{}` as its
        arguments, as a delta first, so the part equals its deltas."""
        block, self.open = self.open, None
        if block is None:
            raise SampleError("a content_block_stop with no block open")
        out: list[Part | Delta] = []
        if block.kind == "tool_use" and not block.chunks:
            block.chunks.append("{}")
            out.append(CallArgs(block.call, "{}"))
        out.append(block.part())
        return out

    def _message_delta(self, event: Json) -> list[Part | Delta]:
        if self.open is not None:
            raise SampleError(f"the message ended with content block {self.open.index} open")
        delta = _json(event.get("delta"), "message_delta's delta")
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
            if self.stop_reason is not None:
                return []  # the reply is done; an error costs only the usage still to come
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
        redacted block arrives whole; a call forming at a cut is not a part."""
        block, self.open = self.open, None
        if block is None or block.kind == "tool_use":
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
        if self.stop_reason is None:
            raise SampleError("the stream ended without a stop_reason")
        stop = STOPS.get(self.stop_reason)
        if stop is None:
            raise SampleError(f"unknown stop_reason {self.stop_reason!r}")
        return Finish(stop, self.stop_reason, self.meta_text())


def stream_error(event: Json) -> SampleError:
    """The `SampleError` for an `error` event, with the status that error type stands for."""
    fields = _json(event.get("error"), "an error event's error")
    kind = fields.get("type")
    message = fields.get("message")
    status = ERROR_STATUS.get(kind) if isinstance(kind, str) else None
    return SampleError(
        f"the server sent an error in the stream: {kind}: {message}",
        status=status,
    )


def _error_text(status: int, body: bytes) -> str:
    """`HTTP <status>: <error type>: <message>`, else the body."""
    text = body.decode("utf-8", errors="replace").strip()
    try:
        parsed: object = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        error = cast("Json", parsed).get("error")
        if isinstance(error, dict):
            fields = cast("Json", error)
            text = f"{fields.get('type', 'error')}: {fields.get('message', text)}"
    return f"HTTP {status}: {text[:500]}"


async def _events(response: httpx.Response) -> AsyncIterator[Json]:
    """The decoded events. The body is read to its end, so the connection is pooled."""
    async for data in sse_data(response):
        try:
            yield _json(json.loads(data), "a stream event")
        except ValueError as e:
            raise SampleError(f"malformed SSE data {data[:200]!r}: {e}") from e


# Options


def _beta_problems(thinking: object, where: str) -> list[str]:
    """Settings of a `thinking` object that need an `anthropic-beta` header, which this
    provider does not send: `display` `updates` and `block_binding` (beta
    `thinking-display-updates-2026-08-18` and `thinking-binding-controls-2026-08-01`)."""
    if not isinstance(thinking, Mapping):
        return []
    fields = cast("Mapping[str, object]", thinking)
    problems: list[str] = []
    if fields.get("display") == "updates":
        problems.append(f"{where} display 'updates' needs a beta header this provider cannot send")
    if "block_binding" in fields:
        problems.append(f"{where} block_binding needs a beta header this provider cannot send")
    return problems


def _thinking_problems(value: object, max_tokens: int | None) -> list[str]:
    if not isinstance(value, Mapping):
        return ["option 'thinking' must be an object"]
    fields = cast("Mapping[str, object]", value)
    kind = fields.get("type")
    if kind not in _THINKING_TYPES:
        return [f"option 'thinking' needs a 'type' of {list(_THINKING_TYPES)}"]
    allowed = {"type"}
    if kind in ("adaptive", "enabled"):
        allowed.add("display")
    if kind == "enabled":
        allowed.add("budget_tokens")
    problems = [
        f"option 'thinking' of type {kind!r} may not set {k!r}" for k in fields if k not in allowed
    ]
    beta = _beta_problems(fields, "option 'thinking'")
    problems += beta
    if "block_binding" in fields:
        problems = [p for p in problems if "may not set 'block_binding'" not in p]
    if "display" in fields and fields["display"] not in (*_DISPLAYS, "updates"):
        problems.append(f"option 'thinking' display must be one of {list(_DISPLAYS)}")
    if kind == "enabled":
        budget = fields.get("budget_tokens")
        if not is_count(budget) or cast("int", budget) < MIN_BUDGET:
            problems.append(
                f"option 'thinking' of type 'enabled' needs budget_tokens >= {MIN_BUDGET}"
            )
        elif max_tokens is not None and cast("int", budget) >= max_tokens:
            problems.append("option 'thinking' budget_tokens must be less than max_tokens")
    return problems


def _typed_keys(model: ModelConfig) -> list[str]:
    """The request keys the model's own settings already set."""
    options = model.options
    keys = [k for k in ("thinking", "temperature", "top_p", "top_k") if k in options]
    if "effort" in options:
        keys.append("output_config")
    return keys


def _extra_body_problems(model: ModelConfig, extra: object) -> list[str]:
    if not isinstance(extra, Mapping):
        return ["option 'extra_body' must be an object"]
    fields = cast("Mapping[str, object]", extra)
    return (
        [
            f"option 'extra_body' may not set {k!r}; the provider owns it"
            for k in fields
            if k in _OWNED_KEYS
        ]
        + [
            f"option 'extra_body' sets {k!r}, which the model's settings already set"
            for k in _typed_keys(model)
            if k in fields
        ]
        + [f"option 'extra_body' holds {bad!r}, which is not JSON" for bad in non_finite(fields)]
        + _beta_problems(fields.get("thinking"), "option 'extra_body' thinking")
        + (
            ["option 'extra_body' sets 'context_management', which needs a beta header"]
            if "context_management" in fields
            else []
        )
    )


def _option_problems(model: ModelConfig) -> list[str]:
    options = model.options
    problems = [
        f"unknown option {key!r}; known options are {list(_OPTIONS)}"
        for key in options
        if key not in _OPTIONS
    ]
    problems += [
        f"option {key!r} must be a number"
        for key in ("temperature", "top_p")
        if key in options and not is_number(options[key])
    ]
    if "top_k" in options and not is_count(options["top_k"]):
        problems.append("option 'top_k' must be a non-negative integer")
    if "effort" in options and options["effort"] not in _EFFORTS:
        problems.append(f"option 'effort' must be one of {list(_EFFORTS)}")
    if "prompt_caching" in options and not isinstance(options["prompt_caching"], bool):
        problems.append("option 'prompt_caching' must be true or false")
    if "thinking" in options:
        problems += _thinking_problems(options["thinking"], model.max_tokens)
    if "extra_body" in options:
        problems += _extra_body_problems(model, options["extra_body"])
    return problems


class AnthropicProvider:
    """Samples the Anthropic Messages API (`/v1/messages`) for the `ModelConfig` the driver
    resolved. `base_url` is the API's root, `https://api.anthropic.com` when unset (a proxy
    that speaks the same API may be named; give no `/v1`).

    One `httpx.AsyncClient` serves every sample (httpx pools connections per host): made on the
    first sample, closed by `aclose()`, after which a sample fails with `ProviderClosed`. A
    client belongs to the event loop that made it: a sample on another loop drops it unawaited
    and makes a new one, so a program may call `asyncio.run` once per command. `transport`
    replaces the network (a test's `httpx.MockTransport`); `environ` is where `api_key_env` is
    looked up, at sample time.

    `check` requires a key (`api_key_env`) and `max_tokens` (the API requires it; there is no
    default), and reads the options in `_OPTIONS`, refusing any other: `thinking` (an object as
    the API takes it: `type` `adaptive`, `enabled` with `budget_tokens`, `disabled`, or
    `between_tools`, and for the first two a `display` of `summarized` or `omitted`; which a model
    accepts is the API's to say), `effort` (`low` to `max`, sent as `output_config.effort`),
    `temperature`, `top_p` (numbers), `top_k` (an integer), `prompt_caching` (a boolean,
    default true: mark the system text and the last message for caching), and `extra_body` (an
    object merged into the request body last; it may not set `model`, `messages`, `system`,
    `stream`, `tools`, `max_tokens`, or a key the model's settings set).
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._transport = transport
        self._environ = os.environ if environ is None else environ
        self._timeout = timeout or httpx.Timeout(10.0, read=300.0)
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None  # the loop `_http` belongs to
        self._closed = False

    @property
    def name(self) -> str:
        return NAME

    def check(self, model: ModelConfig) -> list[str]:
        """Problems with the model's settings as this provider reads them."""
        problems: list[str] = []
        if model.provider != NAME:
            problems.append(f"provider is {model.provider!r}, not {NAME!r}")
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
        self._closed = True
        client, self._http = self._http, None
        if client is not None and self._loop is asyncio.get_running_loop():
            await client.aclose()
        # a client of another loop is dropped: that loop is gone, and it cannot be awaited here

    def _client(self) -> httpx.AsyncClient:
        if self._closed:
            raise ProviderClosed("this provider was closed; build a new one to sample again")
        loop = asyncio.get_running_loop()
        if self._http is not None and self._loop is not loop:
            self._http = None  # its loop is gone (a REPL runs `asyncio.run` per command)
        if self._http is None:
            self._http = httpx.AsyncClient(transport=self._transport, timeout=self._timeout)
            self._loop = loop
        return self._http

    def request(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> tuple[str, dict[str, str], Json]:
        """The URL, headers, and JSON body of the request for this sample.

        Raises `SampleError` if the model's settings fail `check` (options are checked again
        here, so a caller that skipped the driver cannot send an ill-typed request) or its key
        cannot be read.
        """
        problems = self.check(model)
        if problems:
            raise SampleError(f"model {model.model!r}: " + "; ".join(problems))
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
        if "thinking" in options:
            body["thinking"] = plain(options["thinking"])
        if "effort" in options:
            body["output_config"] = {"effort": options["effort"]}
        for name in ("temperature", "top_p", "top_k"):
            if name in options:
                body[name] = options[name]
        if "extra_body" in options:
            body.update(cast("Json", plain(options["extra_body"])))
        base = API_URL if model.base_url is None else model.base_url.rstrip("/")
        return base + "/v1/messages", headers, body

    @staticmethod
    async def _refuse(response: httpx.Response) -> None:
        """Raise the `SampleError` for an error response, with its status and `Retry-After`
        taken before the body is read (a body that fails to read still has them)."""
        status = response.status_code
        wait = retry_after(response.headers.get("retry-after"))
        try:
            text = _error_text(status, await response.aread())
        except httpx.HTTPError as e:
            raise SampleError(
                f"HTTP {status}: the error body could not be read ({type(e).__name__}: {e})",
                status=status,
                retry_after=wait,
            ) from e
        raise SampleError(text, status=status, retry_after=wait)

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        """Yield deltas as they arrive and each part once complete (before the first delta of
        whatever follows it), then the `Finish`. A failure raises `SampleError` (with `status`
        and `retry_after` for an HTTP error, and a `status` for an `error` event of a known
        type) after yielding every part the stream had finished. Once `stop_reason` has arrived
        the reply is done: the stream is still read for the final `usage`, and a reset, an
        `error` event, or malformed SSE after it costs only the usage not yet received."""
        url, headers, body = self.request(walk, model, tools)
        client = self._client()
        reply = _Stream()
        error: SampleError | None = None
        try:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                request_id = response.headers.get("request-id")
                if request_id:
                    reply.meta["request_id"] = request_id  # what support asks for
                if response.status_code != HTTP_OK:
                    await self._refuse(response)
                async for event in _events(response):
                    for item in reply.feed(event):
                        yield item
        except httpx.HTTPError as e:
            kind = ProviderClosed if self._closed else SampleError
            error = kind(f"{type(e).__name__}: {e}")
        except SampleError as e:
            error = e
        if error is None or reply.stop_reason is not None:
            try:
                finish = reply.stop()
            except SampleError as e:
                error = e
            else:
                yield finish
                return
        assert error is not None
        error.meta = reply.meta_text()
        for part in reply.close():
            yield part
        raise error
