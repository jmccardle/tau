"""The OpenAI-compatible provider: chat completions over SSE (llama.cpp, vLLM, OpenAI).

Walk to request in `messages`, response to parts and deltas in `_Stream`, one per reply. The
request carries what the walk, the tools, and `ModelConfig` say: `model`, `messages`, `stream`,
`tools` when there are any, `max_tokens`, then the options (`_OPTIONS`), then `extra_body` last.
"""

import asyncio
import json
import math
import os
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

import httpx

from tau_next._config import ConfigError, ModelConfig
from tau_next._core import Body, Node, Outcome, Part, Stop
from tau_next.events import CallArgs, CallNamed, Delta, TextDelta, ThinkingDelta
from tau_next.protocols import Finish, ProviderClosed, SampleError, Tool

Json = dict[str, object]
HTTP_OK = 200
_OPTIONS = ("temperature", "top_p", "reasoning_effort", "include_usage", "extra_body")
_OWNED_KEYS = ("model", "messages", "stream", "tools", "n")  # `extra_body` may not set these

STOPS: dict[str, Stop] = {
    "stop": Stop.End,
    "tool_calls": Stop.Tools,
    "function_call": Stop.Tools,
    "length": Stop.Length,
    "content_filter": Stop.Refused,
}
STOP_NOTES: dict[Stop, str] = {
    Stop.Length: "length",
    Stop.Interrupted: "interrupted",
    Stop.Error: "error",
}


# Walk to request


def _is_batch(body: Body) -> bool:
    return (
        isinstance(body, Body.Assistant)
        and body.stop == Stop.Tools
        and any(isinstance(p, Part.Call) for p in body.parts)
    )


def _outcome_text(outcome: Outcome) -> str:
    match outcome:
        case Outcome.Ok(value=value):
            return value
        case Outcome.Err(error=error):
            return f"Error: {error}"
        case _:
            return "Interrupted"


def _call_text(call: Part.Call) -> str:
    return f"[tool call {call.id}: {call.tool} {call.args_raw}]"


def _assistant(body: Body.Assistant, answered: bool) -> Json:
    """Thinking is not sent back: this API takes none. Calls are `tool_calls` only when
    the reply is a batch with a `Results` after it; otherwise they are text (`model.md` 10.6)."""
    runnable = answered and _is_batch(body)
    texts: list[str] = []
    wire_calls: list[Json] = []
    for part in body.parts:
        match part:
            case Part.Text(text=text):
                texts.append(text)
            case Part.Call() if runnable:
                function = {"name": part.tool, "arguments": part.args_raw}
                wire_calls.append({"id": part.id, "type": "function", "function": function})
            case Part.Call():
                texts.append(_call_text(part))
            case _:
                pass
    note = STOP_NOTES.get(body.stop)
    if note is not None and any(isinstance(p, Part.Call) for p in body.parts) and not runnable:
        texts.append(f"[reply stopped: {note}]")
    message: Json = {"role": "assistant", "content": "\n".join(texts) if texts else None}
    if wire_calls:
        message["tool_calls"] = wire_calls
    elif not texts:
        message["content"] = ""
    return message


def _results(body: Body.Results, batch: Body | None) -> list[Json]:
    """One tool message per outcome, in call order, if `body` answers `batch`; else one
    user message, so the walk stays legal."""
    if batch is not None and isinstance(batch, Body.Assistant) and _is_batch(batch):
        want = [p.id for p in batch.parts if isinstance(p, Part.Call)]
        got = [call for call, _, _ in body.outcomes]
        if want != got:
            raise SampleError(f"a Results answers calls {got}, but its batch made {want}")
        return [
            {"role": "tool", "tool_call_id": call, "content": _outcome_text(outcome)}
            for call, outcome, _ in body.outcomes
        ]
    lines = [f"[tool result {call}: {_outcome_text(o)}]" for call, o, _ in body.outcomes]
    return [{"role": "user", "content": "\n".join(lines)}]


def messages(walk: Sequence[Node]) -> list[Json]:
    """The wire messages for a walk. `Config` and `Custom` nodes never reach the model."""
    bearing = [n.body for n in walk if not isinstance(n.body, Body.Config | Body.Custom)]
    out: list[Json] = []
    for i, body in enumerate(bearing):
        following = bearing[i + 1] if i + 1 < len(bearing) else None
        previous = bearing[i - 1] if i > 0 else None
        match body:
            case Body.System(text=text):
                out.append({"role": "system", "content": text})
            case Body.User(text=text):
                out.append({"role": "user", "content": text})
            case Body.Assistant():
                out.append(_assistant(body, isinstance(following, Body.Results)))
            case Body.Results():
                out += _results(body, previous)
            case _:
                raise SampleError(f"a body this provider cannot send: {body}")
    return out


# Response to parts


@dataclass
class _Open:
    """The part being accumulated: a thinking or text run, or one call."""

    kind: str  # "thinking" | "text" | "call"
    index: int = 0
    id: str = ""
    tool: str = ""
    named: bool = False  # `CallNamed` has been yielded
    chunks: list[str] = field(default_factory=lambda: [])

    def identify(self, id_: object, tool: object) -> None:
        """Take the call's id and name from a delta. Until the call is announced they are
        assigned (a server may repeat them); after, a repeat is ignored and a different one
        means two calls were merged (a server that sends no `index`) or a name was split."""
        for what, new in (("id", id_), ("name", tool)):
            if not isinstance(new, str) or not new:
                continue
            old = self.id if what == "id" else self.tool
            if self.named and old != new:
                raise SampleError(
                    f"tool call {self.index} changed its {what} from {old!r} to {new!r} "
                    "after it was announced"
                )
            if what == "id":
                self.id = new
            else:
                self.tool = new

    def part(self) -> Part:
        text = "".join(self.chunks)
        if self.kind == "thinking":
            return Part.Thinking(text)
        if self.kind == "text":
            return Part.Text(text)
        if not self.id or not self.tool:
            raise SampleError(
                f"tool call {self.index} has no id or no name "
                f"(id {self.id!r}, name {self.tool!r}, arguments so far {text!r})"
            )
        return Part.Call(self.id, self.tool, text)


@dataclass
class _Stream:
    """One response being read. `feed` takes a decoded chunk and returns its parts and deltas."""

    open: _Open | None = None
    finish: str | None = None
    calls_done: set[int] = field(default_factory=lambda: set[int]())  # noqa: PLW0108 - typed for pyright
    meta: Json = field(default_factory=lambda: Json())  # noqa: PLW0108 - typed for pyright

    def _note(self, chunk: Json) -> None:
        """Keep what the response says about itself, only what a chunk actually sent."""
        for key in ("model", "id", "system_fingerprint"):
            value = chunk.get(key)
            if isinstance(value, str) and value:
                self.meta[key] = value
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            raw = cast("Json", usage)
            self.meta["usage_raw"] = raw
            normal = _usage(raw)
            if normal is not None:
                self.meta["usage"] = normal

    def close(self) -> list[Part | Delta]:
        """Complete the open part, if any."""
        if self.open is None:
            return []
        done, self.open = self.open, None
        if done.kind == "call":
            self.calls_done.add(done.index)
        return [done.part()]

    def _run(self, kind: str, text: object) -> list[Part | Delta]:
        if not isinstance(text, str) or not text:
            return []
        out = [] if self.open is not None and self.open.kind == kind else self.close()
        if self.open is None:
            self.open = _Open(kind)
        self.open.chunks.append(text)
        out.append(ThinkingDelta(text) if kind == "thinking" else TextDelta(text))
        return out

    def _thinking(self, delta: Json) -> list[Part | Delta]:
        """Servers spell it `reasoning_content` or `reasoning`; vLLM sends both, equal."""
        said = [delta[k] for k in ("reasoning_content", "reasoning") if delta.get(k)]
        if len(said) == 2 and said[0] != said[1]:  # noqa: PLR2004 - two spellings
            raise SampleError(
                f"a delta carries different 'reasoning_content' and 'reasoning': {said}"
            )
        return self._run("thinking", said[0] if said else None)

    def _call(self, raw: object) -> list[Part | Delta]:
        call = _json(raw, "a tool call delta")
        index = call.get("index", 0)
        if not isinstance(index, int):
            raise SampleError(f"tool call index is not an integer: {index!r}")
        out: list[Part | Delta] = []
        if self.open is None or self.open.kind != "call" or self.open.index != index:
            if index in self.calls_done:
                raise SampleError(f"tool call {index} continues after it was complete")
            out = self.close()
            self.open = _Open("call", index)
        function = _json(call.get("function", {}), "a tool call's function")
        args = function.get("arguments")
        self.open.identify(call.get("id"), function.get("name"))
        if isinstance(args, str):
            self.open.chunks.append(args)
        if not self.open.named and self.open.id and self.open.tool:
            self.open.named = True
            out.append(CallNamed(index, self.open.id, self.open.tool))
            held = "".join(self.open.chunks)  # arguments that arrived before the name
            if held:
                out.append(CallArgs(index, held))
        elif self.open.named and isinstance(args, str) and args:
            out.append(CallArgs(index, args))
        return out

    def meta_text(self) -> str | None:
        return json.dumps(self.meta) if self.meta else None

    def feed(self, chunk: Json) -> list[Part | Delta]:
        """The parts and deltas this chunk produced, in order: a part comes before the first
        delta of whatever follows it."""
        error = chunk.get("error")
        if error is not None and self.finish is None:
            raise SampleError(f"the server sent an error in the stream: {error}")
        self._note(chunk)
        if self.finish is not None:
            return []  # the reply is done; only the meta fields and usage are still read
        choices = chunk.get("choices")
        if choices is None and "usage" in chunk:
            return []
        if not isinstance(choices, list):
            raise SampleError("a stream chunk has no 'choices' list")
        if not choices:
            return []
        choice = _json(cast("list[object]", choices)[0], "a choice")
        for other in cast("list[object]", choices):
            if _json(other, "a choice").get("index", 0) != 0:
                raise SampleError(
                    "the stream carries more than one choice; this provider reads one"
                )
        delta = _json(choice.get("delta") or {}, "a delta")
        out = self._thinking(delta)
        out += self._run("text", delta.get("content"))
        raw_calls = delta.get("tool_calls")
        for raw in cast("list[object]", raw_calls) if isinstance(raw_calls, list) else []:
            out += self._call(raw)
        finish = choice.get("finish_reason")
        if isinstance(finish, str):
            out += self.close()  # a cut-off call raises here, before the reply counts as done
            self.finish = finish
        return out

    def stop(self) -> Finish:
        if self.finish is None:
            raise SampleError("the stream ended without a finish_reason")
        stop = STOPS.get(self.finish)
        if stop is None:
            raise SampleError(f"unknown finish_reason {self.finish!r}")
        return Finish(stop, self.finish, self.meta_text())


def _tools(tools: Sequence[Tool]) -> list[Json]:
    """The `tools` array: each tool as a function the model may call."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": dict(t.parameters),
            },
        }
        for t in tools
    ]


def _json(value: object, what: str) -> Json:
    if not isinstance(value, dict):
        raise SampleError(f"{what} is not a JSON object: {value!r}")
    return cast("Json", value)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(raw: Json) -> Json | None:
    """The normalized `{"input", "output", "cached"}` from OpenAI's `usage`, or `None` if the
    server did not report prompt and completion tokens, or sent a malformed cached count. A
    server that sends no cached count had none to report, so `cached` is 0."""
    tokens, output = _count(raw.get("prompt_tokens")), _count(raw.get("completion_tokens"))
    if tokens is None or output is None:
        return None
    details = raw.get("prompt_tokens_details")  # servers send null for "no details"
    if details is None:
        return {"input": tokens, "output": output, "cached": 0}
    if not isinstance(details, dict):
        return None
    fields = cast("Json", details)
    if "cached_tokens" not in fields:
        return {"input": tokens, "output": output, "cached": 0}
    cached = _count(fields["cached_tokens"])
    return None if cached is None else {"input": tokens, "output": output, "cached": cached}


def _retry_after(value: str | None) -> float | None:
    """Seconds to wait from a `Retry-After` header: whole seconds as sent, an HTTP-date as
    seconds from now (0 if past); `None` for no header or anything else."""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        seconds = float(text)
        return seconds if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _plain(value: object) -> object:
    """`value` as plain JSON: `ModelConfig` options are read-only mappings and tuples."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in cast("Mapping[str, object]", value).items()}
    if isinstance(value, tuple):
        return [_plain(v) for v in cast("tuple[object, ...]", value)]
    return value


def _extra_body_problems(model: ModelConfig, extra: object) -> list[str]:
    if not isinstance(extra, Mapping):
        return ["option 'extra_body' must be an object"]
    fields = cast("Mapping[str, object]", extra)
    taken = _typed_keys(model)
    return (
        [
            f"option 'extra_body' may not set {k!r}; the provider owns it"
            for k in fields
            if k in _OWNED_KEYS
        ]
        + [
            f"option 'extra_body' sets {k!r}, which the model's settings already set"
            for k in taken
            if k in fields
        ]
        + [f"option 'extra_body' holds {bad!r}, which is not JSON" for bad in _non_finite(fields)]
    )


def _typed_keys(model: ModelConfig) -> list[str]:
    """The request keys the model's own settings already set."""
    keys = [k for k in ("temperature", "top_p", "reasoning_effort") if k in model.options]
    if model.max_tokens is not None:
        keys.append("max_tokens")
    if model.options.get("include_usage", True):
        keys.append("stream_options")
    return keys


def _non_finite(value: object) -> list[float]:
    """Every NaN or infinity inside `value`."""
    if isinstance(value, float):
        return [] if math.isfinite(value) else [value]
    if isinstance(value, Mapping):
        return [b for v in cast("Mapping[str, object]", value).values() for b in _non_finite(v)]
    if isinstance(value, tuple | list):
        return [b for v in cast("tuple[object, ...]", value) for b in _non_finite(v)]
    return []


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _error_text(status: int, body: bytes) -> str:
    """`HTTP <status>: <the server's error message, else its body>`."""
    text = body.decode("utf-8", errors="replace").strip()
    try:
        parsed: object = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        error = cast("Json", parsed).get("error")
        if isinstance(error, dict):
            text = str(cast("Json", error).get("message", text))
    return f"HTTP {status}: {text[:500]}"


async def _events(response: httpx.Response) -> AsyncIterator[str]:
    """The `data` of each SSE event: its `data:` lines joined by newlines."""
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line:
            if line.startswith("data:"):
                lines.append(line.removeprefix("data:").removeprefix(" "))
            continue
        if lines:
            yield "\n".join(lines)
            lines = []
    if lines:
        yield "\n".join(lines)


async def _chunks(response: httpx.Response) -> AsyncIterator[Json]:
    """The decoded chunks up to `[DONE]`. The body is read to its end, so the connection goes
    back to the pool; whatever follows `[DONE]` is not looked at."""
    done = False
    async for data in _events(response):
        done = done or data == "[DONE]"
        if done:
            continue
        try:
            yield _json(json.loads(data), "a stream chunk")
        except ValueError as e:
            raise SampleError(f"malformed SSE data {data[:200]!r}: {e}") from e


class OpenAIProvider:
    """Samples an OpenAI-compatible `/chat/completions` endpoint for the `ModelConfig` the
    driver resolved.

    One `httpx.AsyncClient` serves every sample (httpx pools connections per host): made on
    the first sample, closed by `aclose()`, after which a sample fails with `ProviderClosed`.
    A client belongs to the event loop that made it: a sample on another loop drops it
    unawaited and makes a new one, so a program may call `asyncio.run` once per command.
    `transport` replaces the network (a test's `httpx.MockTransport`); `environ` is where
    `api_key_env` is looked up, at sample time.

    Options (`check` refuses any other): `temperature` and `top_p` (numbers),
    `reasoning_effort` (a string), `include_usage` (a boolean, default true: ask the server for
    token counts in the stream; some reject it), and `extra_body` (an object merged into the
    request body last, for a server's own parameters such as llama.cpp's `cache_prompt`; it may
    not set `model`, `messages`, `stream`, or `tools`).
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
        return "openai"

    def check(self, model: ModelConfig) -> list[str]:
        """This provider needs a `base_url`, and reads the options in `_OPTIONS`."""
        problems: list[str] = []
        if model.provider != self.name:
            problems.append(f"provider is {model.provider!r}, not {self.name!r}")
        if model.base_url is None:
            problems.append("needs a base_url")
        options = model.options
        problems += [
            f"unknown option {key!r}; known options are {list(_OPTIONS)}"
            for key in options
            if key not in _OPTIONS
        ]
        problems += [
            f"option {key!r} must be a number"
            for key in ("temperature", "top_p")
            if key in options and not _is_number(options[key])
        ]
        effort = options.get("reasoning_effort")
        if "reasoning_effort" in options and not (isinstance(effort, str) and effort):
            problems.append("option 'reasoning_effort' must be a non-empty string")
        if "include_usage" in options and not isinstance(options["include_usage"], bool):
            problems.append("option 'include_usage' must be true or false")
        if "extra_body" in options:
            problems += _extra_body_problems(model, options["extra_body"])
        return problems

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
        """The URL, headers, and JSON body of the request for this sample. `tools` become the
        `tools` array, omitted when there are none; `extra_body` is merged last.

        Raises `SampleError` if the model's key cannot be read.
        """
        try:
            key = model.api_key(self._environ)
        except ConfigError as e:
            raise SampleError(str(e)) from e
        headers = {} if key is None else {"Authorization": f"Bearer {key}"}
        body: Json = {
            "model": model.model,
            "messages": messages(walk),
            "stream": True,
        }
        options = model.options
        if options.get("include_usage", True):
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = _tools(tools)
        if model.base_url is None:
            raise SampleError(f"model {model.model!r} has no base_url, which this provider needs")
        if model.max_tokens is not None:
            body["max_tokens"] = model.max_tokens
        for key in ("temperature", "top_p", "reasoning_effort"):
            if key in options:
                body[key] = options[key]
        if "extra_body" in options:
            body.update(cast("Json", _plain(options["extra_body"])))
        return model.base_url.rstrip("/") + "/chat/completions", headers, body

    @staticmethod
    async def _refuse(response: httpx.Response) -> None:
        """Raise the `SampleError` for an error response, with its status and `Retry-After`
        taken before the body is read (a body that fails to read still has them)."""
        status = response.status_code
        wait = _retry_after(response.headers.get("retry-after"))
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
        and `retry_after` for an HTTP error) after yielding every part the stream had finished
        or begun; it carries the meta fields that had arrived. Once `finish_reason` has arrived the
        reply is done: the stream is
        still read for `usage`, but content and tool-call deltas after it are ignored, and an
        error chunk, a reset, malformed SSE, or a missing `[DONE]` after it costs only the
        meta not yet received, never the reply."""
        url, headers, body = self.request(walk, model, tools)
        client = self._client()
        reply = _Stream()
        error: SampleError | None = None
        try:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code != HTTP_OK:
                    await self._refuse(response)
                async for chunk in _chunks(response):
                    for item in reply.feed(chunk):
                        yield item
        except httpx.HTTPError as e:
            kind = ProviderClosed if self._closed else SampleError
            error = kind(f"{type(e).__name__}: {e}")
        except SampleError as e:
            error = e
        if error is None or reply.finish is not None:
            try:
                finish = reply.stop()
            except SampleError as e:
                error = e
            else:
                yield finish
                return
        assert error is not None
        error.meta = reply.meta_text()
        try:
            leftover = reply.close()
        except SampleError as cut_off:
            # A cut-off call with no id or name is not a part; the failure records what arrived.
            raise type(error)(
                f"{error}; {cut_off}",
                reply.meta_text(),
                status=error.status,
                retry_after=error.retry_after,
            ) from error
        for part in leftover:
            yield part
        raise error
