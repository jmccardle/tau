"""The OpenAI-compatible provider: chat completions over SSE (llama.cpp, vLLM, OpenAI).

Walk to request in `messages`, response to parts and deltas in `_Stream`, one per reply. The
request carries what the walk, the tools, and `ModelConfig` say: `model`, `messages`, `stream`,
`tools` when there are any, what the model's settings set (`_typed_body`), then `extra_body` last.
"""

import hashlib
import os
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

import httpx

from tau._config import ConfigError, ModelConfig
from tau._core import Node, Part, Stop
from tau._events import CallArgs, CallNamed, Delta, TextDelta, ThinkingDelta
from tau._protocols import Finish, Rendered, SampleError, Tool
from tau._providers._walk import (
    AssistantTurn,
    SystemTurn,
    ToolResults,
    UserTurn,
    bearing,
    call_markup,
    stop_note,
    system_note,
    turns,
)
from tau._providers._wire import (
    UNREACHED,
    Connection,
    Json,
    count,
    extra_body_problems,
    finish,
    json_object,
    nested_counts,
    note_usage,
    option_problems,
    plain,
    rendered,
    sent_stamp,
)

_OPTIONS = ("temperature", "top_p", "reasoning_effort", "include_usage", "extra_body")
_OWNED_KEYS = ("model", "messages", "stream", "tools", "n")  # `extra_body` may not set these

STOPS: dict[str, Stop] = {
    "stop": Stop.End,
    "tool_calls": Stop.Tools,
    "function_call": Stop.Tools,
    "length": Stop.Length,
    "content_filter": Stop.End,
}


# Walk to request


def _assistant(turn: AssistantTurn) -> Json:
    """Thinking is not sent back: this API takes none. Calls are `tool_calls` only when
    the turn is runnable; otherwise they are text (`model.md` 10.6)."""
    texts: list[str] = []
    wire_calls: list[Json] = []
    for part in turn.parts:
        match part:
            case Part.Text(text=text):
                texts.append(text)
            case Part.Call() if turn.runnable:
                arguments = part.args_raw if part.args_raw.strip() else "{}"
                function = {"name": part.tool, "arguments": arguments}
                wire_calls.append({"id": part.id, "type": "function", "function": function})
            case Part.Call():
                texts.append(call_markup(part))
            case _:
                pass
    if turn.note is not None:
        texts.append(stop_note(turn.note))
    message: Json = {"role": "assistant", "content": "\n".join(texts) if texts else None}
    if wire_calls:
        message["tool_calls"] = wire_calls
    elif not texts:
        message["content"] = ""
    return message


def messages(walk: Sequence[Node], *, structured: bool) -> tuple[list[Json], list[list[int]]]:
    """The wire messages for a walk, and for each the node it came from. `Config` and `Custom`
    nodes never reach the model. The
    `System` nodes that open the walk are `system` messages (an empty one sends none); one later
    is a user message `[system note: ...]` (llama.cpp with the Qwen template answers HTTP 500 to a
    `system` message after the first turn). A batch whose call arguments are not a JSON object, or
    any batch when `structured` is false (no tools are offered), goes as text with its results
    (llama.cpp refuses the history otherwise, HTTP 500); no arguments are sent as `{}`."""
    out: list[Json] = []
    nodes: list[list[int]] = []
    spoken = False  # a turn other than `System` has been seen
    for node, turn in zip(bearing(walk), turns(walk, structured=structured), strict=True):
        made: list[Json] = []
        match turn:
            case SystemTurn(text=text) if spoken:
                made.append({"role": "user", "content": system_note(text)})
            case SystemTurn(text=""):  # no instruction: no message (as Anthropic and Gemini)
                pass
            case SystemTurn(text=text):
                made.append({"role": "system", "content": text})
            case UserTurn(text=text):
                made.append({"role": "user", "content": text})
            case AssistantTurn():
                made.append(_assistant(turn))
            case ToolResults(results=results):
                for r in results:
                    made.append({"role": "tool", "tool_call_id": r.id, "content": r.text})
        out += made
        nodes += [[node] for _ in made]
        spoken = spoken or not isinstance(turn, SystemTurn)
    return out, nodes


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

    def raw(self) -> str:
        """What a call dropped at a cut was, as the failure reports it."""
        return (
            f"tool call {self.index} (id {self.id!r}, name {self.tool!r}), "
            f"arguments so far {''.join(self.chunks)!r}"
        )

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
    """One response being read. `feed` takes a decoded chunk and returns its parts and deltas.
    What a `feed` built before it raised waits in `held` for `close`."""

    open: _Open | None = None
    held: list[Part | Delta] = field(default_factory=lambda: [])
    finish: str | None = None
    calls_done: set[int] = field(default_factory=lambda: set[int]())  # noqa: PLW0108 - typed for pyright
    meta: Json = field(default_factory=lambda: Json())  # noqa: PLW0108 - typed for pyright

    def _note(self, chunk: Json) -> None:
        """Keep what the response says about itself, only what a chunk actually sent."""
        for key in ("model", "id", "system_fingerprint"):
            value = chunk.get(key)
            if isinstance(value, str) and value:
                self.meta[key] = value
        timings = chunk.get("timings")  # llama.cpp: what its slot's cache held (D26 section 2)
        if isinstance(timings, dict):
            self.meta["timings"] = timings
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            raw = cast("Json", usage)
            note_usage(self.meta, raw, _usage)
            _note_prediction(self.meta, raw)
        _cached_from_timings(self.meta)

    def _end_open(self) -> None:
        """Complete the open part, if any, into `held`. A call with no id or name raises and
        stays open."""
        if self.open is None:
            return
        part = self.open.part()
        if self.open.kind == "call":
            self.calls_done.add(self.open.index)
        self.open = None
        self.held.append(part)

    def close(self) -> list[Part | Delta]:
        """What `feed` built and could not return, then the open text or thinking. An open call
        is dropped whole: its raw text goes in `meta["cut_off_call"]` (D1)."""
        out, self.held = self.held, []
        run, self.open = self.open, None
        if run is None:
            return out
        if run.kind == "call":
            self.meta["cut_off_call"] = run.raw()
        else:
            out.append(run.part())
        return out

    def _run(self, kind: str, text: object) -> None:
        if not isinstance(text, str) or not text:
            return
        if self.open is None or self.open.kind != kind:
            self._end_open()
            self.open = _Open(kind)
        self.open.chunks.append(text)
        self.held.append(ThinkingDelta(text) if kind == "thinking" else TextDelta(text))

    def _thinking(self, delta: Json) -> None:
        """Servers spell it `reasoning_content` or `reasoning`; vLLM sends both, equal."""
        said = [delta[k] for k in ("reasoning_content", "reasoning") if delta.get(k)]
        if len(said) == 2 and said[0] != said[1]:  # noqa: PLR2004 - two spellings
            raise SampleError(
                f"a delta carries different 'reasoning_content' and 'reasoning': {said}"
            )
        self._run("thinking", said[0] if said else None)

    def _call(self, raw: object) -> None:
        call = json_object(raw, "a tool call delta")
        index = call.get("index", 0)
        if not isinstance(index, int):
            raise SampleError(f"tool call index is not an integer: {index!r}")
        if self.open is None or self.open.kind != "call" or self.open.index != index:
            if index in self.calls_done:
                raise SampleError(f"tool call {index} continues after it was complete")
            self._end_open()
            self.open = _Open("call", index)
        function = json_object(call.get("function", {}), "a tool call's function")
        args = function.get("arguments")
        self.open.identify(call.get("id"), function.get("name"))
        if isinstance(args, str):
            self.open.chunks.append(args)
        if not self.open.named and self.open.id and self.open.tool:
            self.open.named = True
            self.held.append(CallNamed(index, self.open.id, self.open.tool))
            early = "".join(self.open.chunks)  # arguments that arrived before the name
            if early:
                self.held.append(CallArgs(index, early))
        elif self.open.named and isinstance(args, str) and args:
            self.held.append(CallArgs(index, args))

    @property
    def done(self) -> bool:
        return self.finish is not None

    def feed(self, chunk: Json) -> list[Part | Delta]:
        """The parts and deltas this chunk produced, in order: a part comes before the first
        delta of whatever follows it. If it raises, what it had built waits for `close`."""
        self._read(chunk)
        out, self.held = self.held, []
        return out

    def _read(self, chunk: Json) -> None:
        error = chunk.get("error")
        if error is not None:
            raise SampleError(f"the server sent an error in the stream: {error}")
        self._note(chunk)
        choices = chunk.get("choices")
        if choices is None and "usage" in chunk:
            return
        if not isinstance(choices, list):
            raise SampleError("a stream chunk has no 'choices' list")
        if not choices:
            return
        if len(cast("list[object]", choices)) > 1:
            raise SampleError("the stream carries more than one choice; this provider reads one")
        choice = json_object(cast("list[object]", choices)[0], "a choice")
        if choice.get("index", 0) != 0:
            raise SampleError("the stream carries a choice other than index 0")
        delta = json_object(choice.get("delta") or {}, "a delta")
        self._thinking(delta)
        self._run("text", delta.get("content"))
        refusal = delta.get("refusal")
        if isinstance(refusal, str) and refusal:
            self._run("text", refusal)
        raw_calls = delta.get("tool_calls")
        for raw in cast("list[object]", raw_calls) if isinstance(raw_calls, list) else []:
            self._call(raw)
        finish = choice.get("finish_reason")
        if isinstance(finish, str):
            self._end_open()  # a call with no id or name raises here, before the reply is done
            self.finish = finish

    def stop(self) -> Finish:
        return finish(self.finish, STOPS, "finish_reason", self.meta)


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


def _usage(raw: Json) -> Json | None:
    """The normalized `{"input", "output"}`, with `cached`, `written` and `reasoning` when the
    server reports them, from OpenAI's `usage`; `None` if the server did not report prompt and
    completion tokens, or sent a malformed count. `cached` is `prompt_tokens_details.
    cached_tokens` and `written` is `prompt_tokens_details.cache_write_tokens` (a LiteLLM proxy
    sends both for an Anthropic model, `0` on a miss); `reasoning` is `completion_tokens_details.
    reasoning_tokens`, the part of `output` that was thinking. A server that sends no count has
    not said it read nothing, so the key is absent, not 0 (D24 section 3: a cache notice tells a
    miss from silence)."""
    tokens, output = count(raw.get("prompt_tokens")), count(raw.get("completion_tokens"))
    if tokens is None or output is None:
        return None
    usage: Json = {"input": tokens, "output": output}
    nested = (
        ("cached", "prompt_tokens_details", "cached_tokens"),
        ("written", "prompt_tokens_details", "cache_write_tokens"),
        ("reasoning", "completion_tokens_details", "reasoning_tokens"),
    )
    return usage if nested_counts(usage, raw, nested) else None


def _note_prediction(meta: Json, raw: Json) -> None:
    """Keep OpenAI's prediction counts (`completion_tokens_details.accepted_prediction_tokens`
    and `rejected_prediction_tokens`) in `meta["prediction"]`, when the server sends any."""
    details = raw.get("completion_tokens_details")
    if not isinstance(details, dict):
        return
    fields = cast("Json", details)
    kept = {
        key: fields[f"{key}_prediction_tokens"]
        for key in ("accepted", "rejected")
        if count(fields.get(f"{key}_prediction_tokens")) is not None
    }
    if kept:
        meta["prediction"] = kept


def _cached_from_timings(meta: Json) -> None:
    """llama.cpp says what it read from its cache in `timings.cache_n`, not in `usage`: it is
    the reply's `cached` when `usage` has none and it is a count no larger than `input`."""
    usage, timings = meta.get("usage"), meta.get("timings")
    if not isinstance(usage, dict) or not isinstance(timings, dict) or "cached" in usage:
        return
    read = count(cast("Json", timings).get("cache_n"))
    if read is not None and read <= cast("int", cast("Json", usage)["input"]):
        cast("Json", usage)["cached"] = read


def _props_facts(props: Json) -> Json:
    """The drift markers in llama.cpp's `/props`: `build_info`, the context of a slot (`n_ctx`,
    the window the server really has) and a digest of its chat template."""
    facts: Json = {}
    if isinstance(props.get("build_info"), str):
        facts["build_info"] = props["build_info"]
    settings = props.get("default_generation_settings")
    window = cast("Json", settings).get("n_ctx") if isinstance(settings, dict) else None
    if count(window) is not None:
        facts["n_ctx"] = window
    template = props.get("chat_template")
    if isinstance(template, str) and template:
        facts["template_sha256"] = hashlib.sha256(template.encode()).hexdigest()
    return facts


def _model_facts(listed: object, model: str) -> Json:
    """The drift markers in llama.cpp's `/v1/models`: the `meta` of the model named `model`, or
    of the only model listed: its file's `size` (as `model_size`), `n_params` and `n_vocab`."""
    data = cast("Json", listed).get("data") if isinstance(listed, dict) else None
    models = (
        [cast("Json", m) for m in cast("list[object]", data) if isinstance(m, dict)]
        if (isinstance(data, list))
        else []
    )
    named = [m for m in models if m.get("id") == model]
    chosen = named[0] if named else models[0] if len(models) == 1 else None
    meta = chosen.get("meta") if chosen is not None else None
    if not isinstance(meta, dict):
        return {}
    fields = cast("Json", meta)
    wanted = (("model_size", "size"), ("n_params", "n_params"), ("n_vocab", "n_vocab"))
    return {key: fields[name] for key, name in wanted if count(fields.get(name)) is not None}


def _typed_body(model: ModelConfig) -> Json:
    """The request keys the model's own settings set, with their values."""
    options = model.options
    body: Json = {
        k: options[k] for k in ("temperature", "top_p", "reasoning_effort") if k in options
    }
    if model.max_tokens is not None:
        body["max_tokens"] = model.max_tokens
    if options.get("include_usage", True):
        body["stream_options"] = {"include_usage": True}
    return body


class OpenAIProvider:
    """Samples an OpenAI-compatible `/chat/completions` endpoint for the `ModelConfig` the
    driver resolved.

    One client serves every sample (see `Connection`). `transport` replaces the network (a
    test's `httpx.MockTransport`); `environ` is where `api_key_env` is looked up, at sample
    time.

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
        self._environ = os.environ if environ is None else environ
        self._connection = Connection(transport, timeout)
        self._servers: dict[str, Json] = {}  # base_url -> what the server there says of itself
        self._templates: dict[str, str] = {}  # base_url -> its chat template

    def chat_template(self, base_url: str) -> str | None:
        """The chat template the llama.cpp server at `base_url` reported in `/props`, or `None`
        if no sample has asked it yet or it reported none."""
        return self._templates.get(base_url.rstrip("/"))

    async def _server(self, base: str, model: str, headers: Mapping[str, str]) -> Json:
        """What the server at `base` says of itself, asked once per `base` (D26 section 2): a
        llama.cpp server answers `GET /props` (its `build_info`, its slots' `n_ctx`, its chat
        template) and `GET /models` (the model's size, parameters and vocabulary). Any other
        server answers `/props` with a 404 or something that is not llama.cpp's, which is "not
        llama.cpp", not an error, and is not asked again. A server that could not be reached is
        asked again by the next sample."""
        if base in self._servers:
            return self._servers[base]
        props = await self._connection.get_json(base.removesuffix("/v1") + "/props", headers)
        if props is UNREACHED:
            return {}
        facts: Json = {}
        if isinstance(props, dict) and "build_info" in props:
            fields = cast("Json", props)
            facts.update(_props_facts(fields))
            template = fields.get("chat_template")
            if isinstance(template, str) and template:
                self._templates[base] = template
            listed = await self._connection.get_json(base + "/models", headers)
            facts.update(_model_facts(listed, model))
        self._servers[base] = facts
        return facts

    @property
    def name(self) -> str:
        """`"openai"`: what a model's `provider` setting calls it."""
        return "openai"

    def check(self, model: ModelConfig) -> list[str]:
        """This provider needs a `base_url`, and reads the options in `_OPTIONS`."""
        problems: list[str] = []
        if model.base_url is None:
            problems.append("needs a base_url")
        elif model.base_url.rstrip("/").endswith("/chat/completions"):
            problems.append(
                "base_url is the API's root (such as http://host:8100/v1): "
                "this provider adds /chat/completions itself"
            )
        options = model.options
        problems += option_problems(
            model, _OPTIONS, numbers=("temperature", "top_p"), flags=("include_usage",)
        )
        effort = options.get("reasoning_effort")
        if "reasoning_effort" in options and not (isinstance(effort, str) and effort):
            problems.append("option 'reasoning_effort' must be a non-empty string")
        if "extra_body" in options:
            problems += extra_body_problems(options["extra_body"], _OWNED_KEYS, _typed_body(model))
        return problems

    async def aclose(self) -> None:
        """Close the client and its connections. Later samples fail."""
        await self._connection.aclose()

    def render(self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]) -> Rendered:
        """The request `sample` would send, without sending it or reading the key (D26 section
        3.1): `tools` become the `tools` array, omitted when there are none, and `extra_body` is
        merged last. Each message is one node's (a `Results` node makes one per call). `check`
        has passed."""
        assert model.base_url is not None  # `check` required one
        wire, nodes = messages(walk, structured=bool(tools))
        body: Json = {"model": model.model, "messages": wire, "stream": True}
        if tools:
            body["tools"] = _tools(tools)
        body.update(_typed_body(model))
        if "extra_body" in model.options:
            body.update(cast("Json", plain(model.options["extra_body"])))
        url = model.base_url.rstrip("/") + "/chat/completions"
        return rendered(url, body, "messages", nodes)

    def _headers(self, model: ModelConfig) -> dict[str, str]:
        """The request's headers. Raises `SampleError` if the model's key cannot be read."""
        try:
            key = model.api_key(self._environ)
        except ConfigError as e:
            raise SampleError(str(e)) from e
        return {} if key is None else {"Authorization": f"Bearer {key}"}

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
        meta not yet received, never the reply. A `refusal` delta is text."""
        headers = self._headers(model)
        request = self.render(walk, model, tools)
        assert model.base_url is not None  # `check` required one
        server = await self._server(model.base_url.rstrip("/"), model.model, headers)
        reply = _Stream(meta={"sent": sent_stamp(request, walk)})
        if server:
            reply.meta["server"] = server
        body = cast("Json", request.body)
        async for item in self._connection.sample(request.url, headers, body, reply, end="[DONE]"):
            yield item
