"""The Gemini provider: `streamGenerateContent` over SSE (Google AI Studio and proxies of it).

Walk to request in `contents`, response to parts and deltas in `_Stream`, one per reply. The
request carries `systemInstruction` and `contents` from the walk, `tools` when there are any,
`generationConfig` (`maxOutputTokens` from `ModelConfig`, then the options in `_OPTIONS`),
`safetySettings`, then `extra_body` last. The key travels in the `x-goog-api-key` header and
never in the URL.
"""

import json
import os
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import cast
from urllib.parse import quote

import httpx

from tau._config import ConfigError, ModelConfig
from tau._core import BadReplay, Node, Part, Replay, Stop
from tau._events import CallArgs, CallNamed, Delta, TextDelta, ThinkingDelta
from tau._protocols import Finish, SampleError, Tool, replay_for
from tau._providers._walk import (
    AssistantTurn,
    Messages,
    SystemTurn,
    ToolResults,
    UserTurn,
    call_markup,
    call_object,
    stop_note,
    turns,
)
from tau._providers._wire import (
    Connection,
    Json,
    count,
    delay,
    extra_body_problems,
    finish,
    json_object,
    meta_text,
    note_usage,
    option_problems,
    plain,
)

NAME = "gemini"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_OPTIONS = ("temperature", "top_p", "top_k", "thinking", "safety_settings", "extra_body")
_THINKING = ("include_thoughts", "budget", "level")
_OWNED_KEYS = ("contents", "systemInstruction", "tools")  # `extra_body` may not set these
_DATA_KEYS = ("text", "functionCall")  # what a part carries; a part sets one
_METADATA_KEYS = ("partMetadata", "mediaResolution", "mediaProcessing", "speechMetadata")
_PART_KEYS = {*_DATA_KEYS, "thought", "thoughtSignature", *_METADATA_KEYS}

# Finish reasons that end a reply. `STOP` is `End`, or `Tools` when the reply made calls.
# Every other reason (OTHER, MALFORMED_FUNCTION_CALL, MISSING_THOUGHT_SIGNATURE, ...) fails the
# sample: the model did not finish a reply.
STOPS: dict[str, Stop] = {
    "STOP": Stop.End,
    "MAX_TOKENS": Stop.Length,
    "SAFETY": Stop.End,
    "RECITATION": Stop.End,
    "LANGUAGE": Stop.End,
    "BLOCKLIST": Stop.End,
    "PROHIBITED_CONTENT": Stop.End,
    "SPII": Stop.End,
    "IMAGE_SAFETY": Stop.End,
    "IMAGE_PROHIBITED_CONTENT": Stop.End,
    "IMAGE_RECITATION": Stop.End,
    "ESCALATION": Stop.End,
    "CONTINUATION": Stop.Length,
}


# Walk to request


def _signature(part: Part) -> str | None:
    """The signature this provider may send back for `part`: only Gemini's own, and only as
    this version wrote it (replay it cannot understand counts as unsigned)."""
    data = replay_for(part, NAME)
    signature = None if data is None else data.get("thoughtSignature")
    return signature if isinstance(signature, str) and signature else None


def _signed(wire: Json, signature: str | None) -> Json:
    if signature is not None:
        wire["thoughtSignature"] = signature
    return wire


def _call_part(call: Part.Call) -> Json:
    """A runnable call as a `functionCall` part. Gemini wants the arguments as an object, so
    `args_raw` is parsed here. The `id` goes back only when the API gave one (it is in the replay
    data); an id this provider made up, or another provider's, is never sent."""
    args = call_object(call)
    if args is None:
        raise SampleError(f"call {call.id} ({call.tool}): arguments are not a JSON object")
    function: Json = {"name": call.tool, "args": args}
    wire_id = _wire_id(call)
    if wire_id is not None:
        function["id"] = wire_id
    return _signed({"functionCall": function}, _signature(call))


def _wire_id(call: Part.Call) -> str | None:
    data = replay_for(call, NAME)
    wire_id = None if data is None else data.get("id")
    return wire_id if isinstance(wire_id, str) and wire_id else None


def _model_parts(turn: AssistantTurn) -> list[Json]:
    """Parts in reply order. Text goes as it is (an empty one only when it carries a
    signature). A `Thinking` part goes only when Gemini signed it: unsigned thinking is never
    sent (`model.md` 10.4). Calls are `functionCall` parts when the turn is runnable, else text
    (`model.md` 10.6)."""
    out: list[Json] = []
    for part in turn.parts:
        match part:
            case Part.Text(text=text):
                signature = _signature(part)
                if text or signature is not None:
                    out.append(_signed({"text": text}, signature))
            case Part.Thinking(text=text):
                signature = _signature(part)
                if signature is not None:
                    out.append(_signed({"text": text, "thought": True}, signature))
            case Part.Call() if turn.runnable:
                out.append(_call_part(part))
            case Part.Call():
                out.append({"text": call_markup(part)})
            case _:
                pass
    if turn.note is not None:
        out.append({"text": stop_note(turn.note)})
    return out


def _responses(results: ToolResults, calls: Sequence[Part.Call]) -> list[Json]:
    """The `functionResponse` parts of one user content, in the batch's call order (Gemini
    matches parallel responses by order, and by `id` where the API gave one)."""
    parts: list[Json] = []
    for result, call in zip(results.results, calls, strict=True):
        response: Json = {("error" if result.error else "result"): result.text}
        function: Json = {"name": result.tool, "response": response}
        wire_id = _wire_id(call)
        if wire_id is not None:
            function["id"] = wire_id
        parts.append({"functionResponse": function})
    return parts


def contents(walk: Sequence[Node], *, structured: bool) -> tuple[str | None, list[Json]]:
    """The system instruction text and the wire `contents` for a walk.

    The `System` nodes before any other node are the instruction (blank-line separated; `None`
    if there are none). A `System` node later in the walk is a user turn reading
    `[system note: <text>]`: `systemInstruction` is one string for the whole request, and a
    later node means "from here on". A reply with nothing to send (only unsigned thinking, cut
    off; no parts) is left out and its neighbours merge. A batch whose call arguments are not a
    JSON object (`functionCall.args` must be one), or any batch when `structured` is false (no
    tools are offered), goes as text with its results. A blank `User` or `System` turn is left
    out. `Config` and `Custom` nodes never reach the model."""
    messages = Messages("parts")
    calls: list[Part.Call] = []  # the previous reply's calls
    for turn in turns(walk, structured=structured):
        match turn:
            case SystemTurn(text=text):
                if text.strip():
                    messages.system(text, lambda note: {"text": note})
            case UserTurn(text=text):
                messages.add("user", [{"text": text}] if text.strip() else [])
            case AssistantTurn():
                messages.add("model", _model_parts(turn))
                calls = [p for p in turn.parts if isinstance(p, Part.Call)]
            case ToolResults():
                messages.add("user", _responses(turn, calls))
    return messages.instruction, messages.out


def _tools(tools: Sequence[Tool]) -> list[Json]:
    """The `tools` array: one item holding every tool as a function declaration.
    `parametersJsonSchema` takes the tool's JSON Schema as it is (`parameters` is a narrower
    OpenAPI subset)."""
    declarations = [
        {"name": t.name, "description": t.description, "parametersJsonSchema": dict(t.parameters)}
        for t in tools
    ]
    return [{"functionDeclarations": declarations}]


# Response to parts


@dataclass
class _Open:
    """The text or thinking run being accumulated. A signature ends it (see `_Stream.text`)."""

    kind: str  # "thinking" | "text"
    chunks: list[str] = field(default_factory=lambda: [])
    signature: str | None = None

    def part(self) -> Part:
        text = "".join(self.chunks)
        replay = None if self.signature is None else _replay({"thoughtSignature": self.signature})
        return Part.Thinking(text, replay) if self.kind == "thinking" else Part.Text(text, replay)


def _replay(data: Json) -> Replay:
    """The replay data for a part. Server text that cannot be one (a lone-surrogate escape in
    a signature, say) fails this sample, not core's constructor."""
    try:
        return Replay(NAME, json.dumps(data))
    except BadReplay as e:
        raise SampleError(f"the response holds data that cannot be kept for replay: {e}") from e


def _stream_signature(part: Json) -> str | None:
    """The `thoughtSignature` of a streamed part: `None` for none, a `SampleError` for a
    value that is not a non-empty string."""
    signature = part.get("thoughtSignature")
    if signature is not None and not (isinstance(signature, str) and signature):
        raise SampleError(f"a part's thoughtSignature is not a string: {signature!r}")
    return signature


def _stream_error(error: object) -> SampleError:
    body = cast("Json", error) if isinstance(error, dict) else Json()
    code = body.get("code")
    status = code if isinstance(code, int) and not isinstance(code, bool) else None
    return SampleError(
        f"the server sent an error in the stream: {error}",
        status=status,
        retry_after=delay(body),
    )


def _usage(raw: Json) -> Json | None:
    """The normalized `{"input", "output", "cached"}` from `usageMetadata`, or `None` without a
    prompt count or with a malformed one. `input` is `promptTokenCount` (it includes the cached
    tokens). `output` is `candidatesTokenCount` plus `thoughtsTokenCount`: thinking is billed as
    output but reported apart. A count the API leaves out is 0 (proto3 omits zeros)."""
    prompt = count(raw.get("promptTokenCount"))
    if prompt is None:
        return None
    counts: list[int] = []
    for key in ("candidatesTokenCount", "thoughtsTokenCount", "cachedContentTokenCount"):
        value = count(raw.get(key, 0))
        if value is None:
            return None
        counts.append(value)
    candidates, thoughts, cached = counts
    return {"input": prompt, "output": candidates + thoughts, "cached": cached}


@dataclass
class _Stream:
    """One response being read. `feed` takes a decoded chunk and returns its parts and deltas.
    What a `feed` built before it raised waits in `held` for `close`."""

    open: _Open | None = None
    held: list[Part | Delta] = field(default_factory=lambda: [])
    calls: int = 0
    call_ids: set[str] = field(default_factory=lambda: set[str]())  # noqa: PLW0108 - typed for pyright
    finish: str | None = None
    blocked: str | None = None  # `promptFeedback.blockReason`
    meta: Json = field(default_factory=lambda: Json())  # noqa: PLW0108 - typed for pyright

    def _note(self, chunk: Json) -> None:
        """Keep what the response says about itself, only what a chunk actually sent."""
        for source, key in (("modelVersion", "model"), ("responseId", "id")):
            value = chunk.get(source)
            if isinstance(value, str) and value:
                self.meta[key] = value
        usage = chunk.get("usageMetadata")
        if isinstance(usage, dict):
            note_usage(self.meta, cast("Json", usage), _usage)
        feedback = chunk.get("promptFeedback")
        if isinstance(feedback, dict):
            self.meta["promptFeedback"] = feedback
            reason = cast("Json", feedback).get("blockReason")
            if isinstance(reason, str) and reason:
                self.blocked = reason

    def _end_run(self) -> list[Part | Delta]:
        """Complete the open run, if it has text or a signature."""
        if self.open is None:
            return []
        done, self.open = self.open, None
        return [done.part()] if done.chunks or done.signature is not None else []

    def close(self) -> list[Part | Delta]:
        """What `feed` built and could not return, then the open run. A call arrives whole, so
        none is ever cut off."""
        out, self.held = self.held, []
        return out + self._end_run()

    def _text(self, part: Json) -> list[Part | Delta]:
        """Consecutive text (or thinking) parts are one run. A `thoughtSignature` ends the run:
        it comes on the last chunk of the text it signs (often with no text of its own), so
        the run up to and including that chunk is one saved part carrying it, and the text after
        it is a new part. Without signatures a whole run is one part."""
        thought = part.get("thought", False)
        if not isinstance(thought, bool):
            raise SampleError(f"a part's thought is not a boolean: {thought!r}")
        kind = "thinking" if thought else "text"
        text = part.get("text", "")
        signature = _stream_signature(part)
        if not isinstance(text, str):
            raise SampleError(f"a part's text is not a string: {text!r}")
        if not text and signature is None:
            return []  # live streams send an empty text part; it ends no run
        out = self._end_run() if self.open is not None and self.open.kind != kind else []
        if text:
            if self.open is None:
                self.open = _Open(kind)
            self.open.chunks.append(text)
            out.append(ThinkingDelta(text) if kind == "thinking" else TextDelta(text))
        if signature is not None:
            if self.open is None:
                self.open = _Open(kind)
            self.open.signature = signature
            out += self._end_run()
        return out

    def _call(self, part: Json) -> list[Part | Delta]:
        """A call arrives whole: `CallNamed`, one `CallArgs`, then the part. Its id is the API's
        when it sent one; otherwise `gemini-` and a fresh random UUID, which is unique across
        sessions and restarts with nothing to coordinate (a counter would repeat). The API's id
        is kept in the replay data as `id` and is sent back; a made-up one is marked
        `synthesized_id` there and never sent."""
        function = json_object(part["functionCall"], "a functionCall")
        name = function.get("name")
        if not (isinstance(name, str) and name):
            raise SampleError(f"a functionCall has no name: {function}")
        args = json_object(function.get("args", {}), "a functionCall's args")
        wire_id = function.get("id")
        if wire_id is not None and not (isinstance(wire_id, str) and wire_id):
            raise SampleError(f"a functionCall's id is not a string: {wire_id!r}")
        call_id = wire_id or f"gemini-{uuid.uuid4().hex}"
        if call_id in self.call_ids:
            raise SampleError(f"two calls in one reply have the id {call_id!r}")
        self.call_ids.add(call_id)
        replay: Json = {"id": wire_id} if wire_id else {"synthesized_id": True}
        signature = _stream_signature(part)
        if signature is not None:
            replay["thoughtSignature"] = signature
        out = self._end_run()
        index, self.calls = self.calls, self.calls + 1
        args_raw = json.dumps(args, ensure_ascii=False)
        out += [CallNamed(index, call_id, name), CallArgs(index, args_raw)]
        out.append(Part.Call(call_id, name, args_raw, _replay(replay)))
        return out

    def _parts(self, raw: object) -> None:
        if raw is None:
            return
        if not isinstance(raw, list):
            raise SampleError(f"a candidate's content.parts is not a list: {raw!r}")
        for item in cast("list[object]", raw):
            part = json_object(item, "a part")
            extra = set(part) - _PART_KEYS
            if extra:
                raise SampleError(f"a part this provider cannot read: {sorted(extra)}")
            data = [k for k in _DATA_KEYS if k in part]
            if len(data) > 1:
                raise SampleError(f"a part sets more than one of {data}")
            self.held += self._call(part) if "functionCall" in part else self._text(part)

    @property
    def done(self) -> bool:
        return self.finish is not None or self.blocked is not None

    def feed(self, chunk: Json) -> list[Part | Delta]:
        """The parts and deltas this chunk produced, in order: a part comes before the first
        delta of whatever follows it. If it raises, what it had built waits for `close`."""
        self._read(chunk)
        out, self.held = self.held, []
        return out

    def _read(self, chunk: Json) -> None:
        error = chunk.get("error")
        if error is not None:
            raise _stream_error(error)
        self._note(chunk)
        candidates = chunk.get("candidates")
        if self.blocked is not None or not candidates:
            return  # a blocked prompt has no reply; a usage-only chunk has no candidates
        if not isinstance(candidates, list) or len(cast("list[object]", candidates)) != 1:
            raise SampleError(
                "the stream carries other than one candidate; this provider reads one"
            )
        candidate = json_object(cast("list[object]", candidates)[0], "a candidate")
        if candidate.get("index", 0) != 0:
            raise SampleError("the stream carries a candidate other than index 0")
        content = json_object(candidate.get("content") or {}, "a candidate's content")
        self._parts(content.get("parts"))
        message = candidate.get("finishMessage")
        if isinstance(message, str) and message:
            self.meta["finishMessage"] = message
        finish = candidate.get("finishReason")
        if isinstance(finish, str) and finish:
            self.held += self._end_run()
            self.finish = finish

    def stop(self) -> Finish:
        if self.blocked is not None:
            return Finish(Stop.End, self.blocked, meta_text(self.meta))
        done = finish(self.finish, STOPS, "finishReason", self.meta)
        return replace(done, stop=Stop.Tools) if done.stop == Stop.End and self.calls else done


# Options


def _typed_body(model: ModelConfig) -> Json:
    """The request keys the model's own settings set, with their values: `generationConfig`
    (merged with the `extra_body` one) and `safetySettings`."""
    options = model.options
    generation: Json = {}
    if model.max_tokens is not None:
        generation["maxOutputTokens"] = model.max_tokens
    for option, wire in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK")):
        if option in options:
            generation[wire] = options[option]
    if "thinking" in options:
        generation["thinkingConfig"] = _thinking_config(options["thinking"])
    body: Json = {}
    if generation:
        body["generationConfig"] = generation
    if "safety_settings" in options:
        body["safetySettings"] = plain(options["safety_settings"])
    return body


def _extra_problems(model: ModelConfig, extra: object) -> list[str]:
    typed = _typed_body(model)
    generation = cast("Json", typed.pop("generationConfig", {}))  # merged, not replaced
    problems = extra_body_problems(extra, _OWNED_KEYS, typed)
    wanted = (
        cast("Mapping[str, object]", extra).get("generationConfig")
        if isinstance(extra, Mapping)
        else None
    )
    if wanted is None:
        return problems
    if not isinstance(wanted, Mapping):
        return [*problems, "option 'extra_body' key 'generationConfig' must be an object"]
    if "candidateCount" in wanted:
        problems.append(
            "option 'extra_body' may not set generationConfig.candidateCount: one is read"
        )
    problems += [
        f"option 'extra_body' sets generationConfig.{k}, which the model's settings already set"
        for k in cast("Mapping[str, object]", wanted)
        if k in generation
    ]
    return problems


def _thinking_problems(thinking: object) -> list[str]:
    if not isinstance(thinking, Mapping):
        return ["option 'thinking' must be an object"]
    fields = cast("Mapping[str, object]", thinking)
    problems = [
        f"unknown key {key!r} in option 'thinking'; known keys are {list(_THINKING)}"
        for key in fields
        if key not in _THINKING
    ]
    if "include_thoughts" in fields and not isinstance(fields["include_thoughts"], bool):
        problems.append("option 'thinking' key 'include_thoughts' must be true or false")
    budget = fields.get("budget")
    if "budget" in fields and not (
        isinstance(budget, int) and not isinstance(budget, bool) and budget >= -1
    ):
        problems.append("option 'thinking' key 'budget' must be an integer >= -1 (-1: dynamic)")
    level = fields.get("level")
    if "level" in fields and not (isinstance(level, str) and level):
        problems.append("option 'thinking' key 'level' must be a non-empty string")
    if "budget" in fields and "level" in fields:
        problems.append("option 'thinking' sets both 'budget' and 'level'; the API takes one")
    return problems


def _safety_problems(settings: object) -> list[str]:
    if not isinstance(settings, tuple | list):
        return ["option 'safety_settings' must be a list"]
    problems: list[str] = []
    for i, item in enumerate(cast("tuple[object, ...]", settings)):
        if not isinstance(item, Mapping):
            problems.append(f"option 'safety_settings' item {i} must be an object")
            continue
        fields = cast("Mapping[str, object]", item)
        category, threshold = fields.get("category"), fields.get("threshold")
        if set(fields) != {"category", "threshold"} or not all(
            isinstance(v, str) and v for v in (category, threshold)
        ):
            problems.append(
                f"option 'safety_settings' item {i} must be exactly "
                "{category, threshold}, both non-empty strings"
            )
    return problems


class GeminiProvider:
    """Samples Gemini's `streamGenerateContent` (`?alt=sse`) for the `ModelConfig` the driver
    resolved.

    One client serves every sample (see `Connection`). `transport` replaces the network (a
    test's `httpx.MockTransport`); `environ` is where `api_key_env` is looked up, at sample
    time.

    Settings: `base_url` defaults to the official endpoint; a proxy sets it. A key is required
    (`api_key_env` or `api_key`) and goes in the `x-goog-api-key` header. `max_tokens` is
    `maxOutputTokens`, which Gemini counts thinking into. Options (`check` refuses any other):
    `temperature`, `top_p`, `top_k` (numbers); `thinking`, an object of `include_thoughts` (bool:
    ask for thought summaries, which arrive as `Thinking`; without it none are requested),
    `budget` (integer, -1 dynamic, 0 off: Gemini 2.5) or `level` (a string: Gemini 3), not both;
    `safety_settings`, a list of `{category, threshold}` passed through, one per category; and
    `extra_body`, an object merged into the request last, for what the API has that these do
    not (it may not set `contents`, `systemInstruction` or `tools`, nor a key the model's
    settings set; its `generationConfig` merges into ours).
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._connection = Connection(transport, timeout)

    @property
    def name(self) -> str:
        return NAME

    def check(self, model: ModelConfig) -> list[str]:
        """Problems with the model's settings: no key named, an unknown or ill-typed option. An
        unset `api_key_env` variable is not one: `tau config check` warns of it, sampling fails."""
        problems: list[str] = []
        if model.api_key_env is None and model.literal_api_key is None:
            problems.append("needs an API key: set api_key_env (or api_key)")
        if model.model.startswith("models/"):
            problems.append(
                f"model {model.model!r} starts with 'models/': this provider adds it, "
                f"name the model {model.model.removeprefix('models/')!r}"
            )
        options = model.options
        problems += option_problems(model, _OPTIONS, numbers=("temperature", "top_p"))
        top_k = options.get("top_k")
        if "top_k" in options and not (isinstance(top_k, int) and not isinstance(top_k, bool)):
            problems.append("option 'top_k' must be an integer")
        if "thinking" in options:
            problems += _thinking_problems(options["thinking"])
        if "safety_settings" in options:
            problems += _safety_problems(options["safety_settings"])
        if "extra_body" in options:
            problems += _extra_problems(model, options["extra_body"])
        return problems

    async def aclose(self) -> None:
        """Close the client and its connections. Later samples fail."""
        await self._connection.aclose()

    def _request(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> tuple[str, dict[str, str], Json]:
        """The URL, headers, and JSON body of the request for this sample. `check` has passed
        (the driver runs it first).

        Raises `SampleError` for an unreadable key, a walk with nothing to send, or one the API
        would reject.
        """
        try:
            key = model.api_key(self._environ)
        except ConfigError as e:  # `tau config check` warns of an unset variable; sampling fails
            raise SampleError(str(e)) from e
        system, history = contents(walk, structured=bool(tools))
        if not history:
            raise SampleError("the walk has no user content to send")
        if history[-1]["role"] == "model":
            raise SampleError("the walk ends with a model turn, which the API refuses (HTTP 400)")
        body: Json = {}
        if system is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        body["contents"] = history
        if tools:
            body["tools"] = _tools(tools)
        extra = (
            cast("Json", plain(model.options["extra_body"]))
            if "extra_body" in model.options
            else {}
        )
        typed = _typed_body(model)
        generation = {
            **cast("Json", typed.pop("generationConfig", {})),
            **cast("Json", extra.pop("generationConfig", {})),
        }
        if generation:
            body["generationConfig"] = generation
        body.update(typed)
        body.update(extra)
        name = quote(model.model if "/" in model.model else f"models/{model.model}", safe="/")
        base = (model.base_url or DEFAULT_BASE_URL).rstrip("/")
        url = f"{base}/{name}:streamGenerateContent?alt=sse"
        return url, {"x-goog-api-key": cast("str", key)}, body

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        """Yield deltas as they arrive and each part once complete (before the first delta of
        whatever follows it), then the `Finish`. A failure raises `SampleError` (with `status`
        and `retry_after` for an HTTP error or an error event) after yielding every part the
        stream had finished or begun; it carries the meta that had arrived. Once `finishReason`
        has arrived the reply is done: the stream is still read for `usageMetadata`, and a failure
        after it costs only the meta not yet received. A prompt the API blocked (`promptFeedback`
        with a `blockReason`) ends as `End`, the reason in `finish_reason` and the block in
        `meta`."""
        url, headers, body = self._request(walk, model, tools)
        async for item in self._connection.sample(url, headers, body, _Stream()):
            yield item


def _thinking_config(thinking: object) -> Json:
    """`thinking` as `thinkingConfig`. Only its keys are looked at when `check` builds the
    typed body of a `thinking` it is about to refuse, so a non-object has none."""
    wire = {
        "include_thoughts": "includeThoughts",
        "budget": "thinkingBudget",
        "level": "thinkingLevel",
    }
    if not isinstance(thinking, Mapping):
        return {}
    return {wire[k]: v for k, v in cast("Mapping[str, object]", thinking).items() if k in wire}
