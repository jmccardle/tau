"""The OpenAI-compatible provider: chat completions over SSE (llama.cpp, vLLM, OpenAI).

Walk to request in `messages`, response to parts in `_Stream`, one per reply. The request carries
only what the walk, the tools, and `ModelConfig` say: `model`, `messages`, `stream`, and
`tools` when there are any.
"""

import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

import httpx

from tau_next._config import ConfigError, ModelConfig
from tau_next._core import Body, Node, Outcome, Part, Stop
from tau_next.protocols import Finish, SampleError, Tool

Json = dict[str, object]
HTTP_OK = 200

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
    chunks: list[str] = field(default_factory=lambda: [])

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
    """One response being read. `feed` takes a decoded chunk and returns the parts it completed."""

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
            self.meta["usage"] = usage

    def close(self) -> list[Part]:
        """Complete the open part, if any."""
        if self.open is None:
            return []
        done, self.open = self.open, None
        if done.kind == "call":
            self.calls_done.add(done.index)
        return [done.part()]

    def _run(self, kind: str, delta: object) -> list[Part]:
        if not isinstance(delta, str) or not delta:
            return []
        out = [] if self.open is not None and self.open.kind == kind else self.close()
        if self.open is None:
            self.open = _Open(kind)
        self.open.chunks.append(delta)
        return out

    def _call(self, raw: object) -> list[Part]:
        call = _json(raw, "a tool call delta")
        index = call.get("index", 0)
        if not isinstance(index, int):
            raise SampleError(f"tool call index is not an integer: {index!r}")
        out: list[Part] = []
        if self.open is None or self.open.kind != "call" or self.open.index != index:
            if index in self.calls_done:
                raise SampleError(f"tool call {index} continues after it was complete")
            out = self.close()
            self.open = _Open("call", index)
        function = _json(call.get("function", {}), "a tool call's function")
        if isinstance(call.get("id"), str):
            self.open.id = cast("str", call["id"])
        if isinstance(function.get("name"), str):
            self.open.tool += cast("str", function["name"])
        if isinstance(function.get("arguments"), str):
            self.open.chunks.append(cast("str", function["arguments"]))
        return out

    def meta_text(self) -> str | None:
        return json.dumps(self.meta) if self.meta else None

    def feed(self, chunk: Json) -> list[Part]:
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
        delta = _json(choice.get("delta") or {}, "a delta")
        out = self._run("thinking", delta.get("reasoning_content"))
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


class OpenAIProvider:
    """Samples an OpenAI-compatible `/chat/completions` endpoint for the `ModelConfig` the
    driver resolved.

    `transport` replaces the network (a test's `httpx.MockTransport`); `environ` is where
    `api_key_env` is looked up, at sample time.
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

    def request(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> tuple[str, dict[str, str], Json]:
        """The URL, headers, and JSON body of the request for this sample. `tools` become the
        `tools` array, omitted when there are none.

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
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = _tools(tools)
        return model.base_url.rstrip("/") + "/chat/completions", headers, body

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncIterator[Part | Finish]:
        """Yield each part once complete, then the `Finish`. A failure raises `SampleError`
        after yielding every part the stream had finished or begun; it carries the meta fields
        that had arrived. Once `finish_reason` has arrived the reply is done: the stream is
        still read for `usage`, but content and tool-call deltas after it are ignored, and an
        error chunk, a reset, malformed SSE, or a missing `[DONE]` after it costs only the
        meta not yet received, never the reply."""
        url, headers, body = self.request(walk, model, tools)
        reply = _Stream()
        error: SampleError | None = None
        try:
            async with (
                httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client,
                client.stream("POST", url, headers=headers, json=body) as response,
            ):
                if response.status_code != HTTP_OK:
                    raise SampleError(_error_text(response.status_code, await response.aread()))
                async for data in _events(response):
                    if data == "[DONE]":
                        break
                    try:
                        chunk = _json(json.loads(data), "a stream chunk")
                    except ValueError as e:
                        raise SampleError(f"malformed SSE data {data[:200]!r}: {e}") from e
                    for part in reply.feed(chunk):
                        yield part
        except httpx.HTTPError as e:
            error = SampleError(f"{type(e).__name__}: {e}")
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
            raise SampleError(f"{error}; {cut_off}", reply.meta_text()) from error
        for part in leftover:
            yield part
        raise error
