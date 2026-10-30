"""The Gemini provider: `streamGenerateContent` over SSE (Google AI Studio and proxies of it).

Walk to request in `contents`, response to parts and deltas in `_Stream`, one per reply. The
request carries `systemInstruction` and `contents` from the walk, `tools` when there are any,
`generationConfig` (`maxOutputTokens` from `ModelConfig`, then the options in `_OPTIONS`),
`safetySettings`, then `extra_body` last. The key travels in the `x-goog-api-key` header and
never in the URL.
"""

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

import httpx

from tau_next._config import ConfigError, ModelConfig
from tau_next._core import BadReplay, Node, Part, Replay, Stop
from tau_next.events import CallArgs, CallNamed, Delta, TextDelta, ThinkingDelta
from tau_next.protocols import Finish, ProviderClosed, SampleError, Tool, replay_for
from tau_next.providers._walk import (
    AssistantTurn,
    SystemTurn,
    ToolResults,
    UserTurn,
    call_markup,
    stop_note,
    turns,
)
from tau_next.providers.openai import (
    _count,  # pyright: ignore[reportPrivateUsage]
    _error_text,  # pyright: ignore[reportPrivateUsage]
    _events,  # pyright: ignore[reportPrivateUsage]
    _is_number,  # pyright: ignore[reportPrivateUsage]
    _non_finite,  # pyright: ignore[reportPrivateUsage]
    _plain,  # pyright: ignore[reportPrivateUsage]
    _retry_after,  # pyright: ignore[reportPrivateUsage]
)

Json = dict[str, object]
NAME = "gemini"
HTTP_OK = 200
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_OPTIONS = ("temperature", "top_p", "top_k", "thinking", "safety_settings", "extra_body")
_THINKING = ("include_thoughts", "budget", "level")
_OWNED_KEYS = ("contents", "systemInstruction", "tools")  # `extra_body` may not set these
_PART_KEYS = {"text", "thought", "thoughtSignature", "functionCall"}
_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")  # Google's `retryDelay`: "30s", "0.5s"

# Finish reasons that end a reply. `STOP` is `End`, or `Tools` when the reply made calls.
# Every other reason (OTHER, MALFORMED_FUNCTION_CALL, MISSING_THOUGHT_SIGNATURE, ...) fails the
# sample: the model did not finish a reply.
STOPS: dict[str, Stop] = {
    "STOP": Stop.End,
    "MAX_TOKENS": Stop.Length,
    "SAFETY": Stop.Refused,
    "RECITATION": Stop.Refused,
    "LANGUAGE": Stop.Refused,
    "BLOCKLIST": Stop.Refused,
    "PROHIBITED_CONTENT": Stop.Refused,
    "SPII": Stop.Refused,
    "IMAGE_SAFETY": Stop.Refused,
    "IMAGE_PROHIBITED_CONTENT": Stop.Refused,
    "IMAGE_RECITATION": Stop.Refused,
}


# Walk to request


def _signature(part: Part) -> str | None:
    """The signature this provider may send back for `part`: only Gemini's own."""
    data = replay_for(part, NAME)
    if data is None:
        return None
    signature = data.get("thoughtSignature")
    if signature is not None and not (isinstance(signature, str) and signature):
        raise SampleError(f"a part's replay data holds a malformed thoughtSignature: {data}")
    return signature


def _signed(wire: Json, signature: str | None) -> Json:
    if signature is not None:
        wire["thoughtSignature"] = signature
    return wire


def _call_part(call: Part.Call) -> Json:
    """A runnable call as a `functionCall` part. Gemini wants the arguments as an object, so
    `args_raw` is parsed here. The `id` goes back only when the API gave one (it is in the replay
    data); an id this provider made up, or another provider's, is never sent."""
    try:
        args = json.loads(call.args_raw)
    except ValueError as e:
        raise SampleError(f"call {call.id} ({call.tool}): arguments are not JSON: {e}") from e
    if not isinstance(args, dict):
        raise SampleError(f"call {call.id} ({call.tool}): arguments are not a JSON object")
    function: Json = {"name": call.tool, "args": args}
    wire_id = _wire_id(call)
    if wire_id is not None:
        function["id"] = wire_id
    return _signed({"functionCall": function}, _signature(call))


def _wire_id(call: Part.Call) -> str | None:
    data = replay_for(call, NAME)
    if data is None or "id" not in data:
        return None
    wire_id = data["id"]
    if not (isinstance(wire_id, str) and wire_id):
        raise SampleError(f"call {call.id}: replay data holds a malformed id: {data}")
    return wire_id


def _model_parts(turn: AssistantTurn, runnable: bool) -> list[Json]:
    """Parts in reply order. Text goes as it is (an empty one only when it carries a
    signature). A `Thinking` part goes only when Gemini signed it: unsigned thinking is never
    sent (`model.md` 10.4). Calls are `functionCall` parts when `runnable` (the turn is, and
    its arguments are objects), else text
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
            case Part.Call() if runnable:
                out.append(_call_part(part))
            case Part.Call():
                out.append({"text": call_markup(part)})
            case _:
                pass
    if turn.note is not None:
        out.append({"text": stop_note(turn.note)})
    return out


def _responses(results: ToolResults, calls: Sequence[Part.Call]) -> Json:
    """One user content of `functionResponse` parts, in the batch's call order (Gemini matches
    parallel responses by order, and by `id` where the API gave one)."""
    parts: list[Json] = []
    for result, call in zip(results.results, calls, strict=True):
        response: Json = {("error" if result.error else "result"): result.text}
        function: Json = {"name": result.tool, "response": response}
        wire_id = _wire_id(call)
        if wire_id is not None:
            function["id"] = wire_id
        parts.append({"functionResponse": function})
    return {"role": "user", "parts": parts}


def _args_are_objects(turn: AssistantTurn) -> bool:
    """True when every call's `args_raw` is a JSON object, which `functionCall.args` must be.
    One that is not (another provider's cut-off or empty call, saved with its result) makes the
    whole batch text: failing the sample would fail every later sample on the branch."""
    for part in turn.parts:
        if isinstance(part, Part.Call):
            try:
                if not isinstance(json.loads(part.args_raw), dict):
                    return False
            except ValueError:
                return False
    return True


def _results_as_text(results: ToolResults) -> Json:
    """A batch that went as text: its results as `[tool result <id>: <text>]` lines, which is what
    `outcome_markup` makes of the same outcomes (`CallResult.text` is already their text)."""
    lines = [f"[tool result {r.id}: {r.text}]" for r in results.results]
    return {"role": "user", "parts": [{"text": "\n".join(lines)}]}


def _merged(history: list[Json]) -> list[Json]:
    """Neighbouring contents of one role joined into one (Gemini wants roles to alternate once
    an empty reply has been dropped). Parts are only concatenated: a signed text part never
    merges with its neighbour."""
    out: list[Json] = []
    for content in history:
        parts = list(cast("list[Json]", content["parts"]))
        if out and out[-1]["role"] == content["role"]:
            cast("list[Json]", out[-1]["parts"]).extend(parts)
        else:
            out.append({"role": content["role"], "parts": parts})
    return out


def contents(walk: Sequence[Node]) -> tuple[str | None, list[Json]]:
    """The system instruction text and the wire `contents` for a walk.

    The `System` nodes before any other node are the instruction (blank-line separated; `None`
    if there are none). A `System` node later in the walk is a user turn reading
    `[system note: <text>]`: `systemInstruction` is one string for the whole request, and a
    later node means "from here on". A reply with nothing to send (only unsigned thinking, cut
    off; no parts) is left out and its neighbours merge. `Config` and `Custom` nodes never reach
    the model."""
    system: list[str] = []
    out: list[Json] = []
    calls: list[Part.Call] | None = None  # the previous reply's calls, if it went as calls
    for turn in turns(walk):
        match turn:
            case SystemTurn(text=text) if not out:
                system.append(text)
            case SystemTurn(text=text):
                out.append({"role": "user", "parts": [{"text": f"[system note: {text}]"}]})
            case UserTurn(text=text):
                out.append({"role": "user", "parts": [{"text": text}]})
            case AssistantTurn():
                runnable = turn.runnable and _args_are_objects(turn)
                parts = _model_parts(turn, runnable)
                if parts:
                    out.append({"role": "model", "parts": parts})
                calls = [p for p in turn.parts if isinstance(p, Part.Call)] if runnable else None
            case ToolResults():
                out.append(_results_as_text(turn) if calls is None else _responses(turn, calls))
    return ("\n\n".join(system) if system else None), _merged(out)


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


def _json(value: object, what: str) -> Json:
    if not isinstance(value, dict):
        raise SampleError(f"{what} is not a JSON object: {value!r}")
    return cast("Json", value)


def _delay(error: Json) -> float | None:
    """Seconds from the `RetryInfo` in an error object's `details`, if it has one."""
    details = error.get("details")
    for detail in cast("list[object]", details) if isinstance(details, list) else []:
        if isinstance(detail, dict):
            delay = cast("Json", detail).get("retryDelay")
            found = _DELAY.match(delay) if isinstance(delay, str) else None
            if found:
                return float(found.group(1))
    return None


def _stream_error(error: object) -> SampleError:
    body = cast("Json", error) if isinstance(error, dict) else Json()
    code = body.get("code")
    status = code if isinstance(code, int) and not isinstance(code, bool) else None
    return SampleError(
        f"the server sent an error in the stream: {error}",
        status=status,
        retry_after=_delay(body),
    )


def _usage(raw: Json) -> Json | None:
    """The normalized `{"input", "output", "cached"}` from `usageMetadata`, or `None` without a
    prompt count or with a malformed one. `input` is `promptTokenCount` (it includes the cached
    tokens). `output` is `candidatesTokenCount` plus `thoughtsTokenCount`: thinking is billed as
    output but reported apart. A count the API leaves out is 0 (proto3 omits zeros)."""
    prompt = _count(raw.get("promptTokenCount"))
    if prompt is None:
        return None
    counts: list[int] = []
    for key in ("candidatesTokenCount", "thoughtsTokenCount", "cachedContentTokenCount"):
        value = _count(raw.get(key, 0))
        if value is None:
            return None
        counts.append(value)
    candidates, thoughts, cached = counts
    return {"input": prompt, "output": candidates + thoughts, "cached": cached}


@dataclass
class _Stream:
    """One response being read. `feed` takes a decoded chunk and returns its parts and deltas."""

    open: _Open | None = None
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
            raw = cast("Json", usage)
            self.meta["usage_raw"] = raw
            normal = _usage(raw)
            if normal is None:
                self.meta.pop("usage", None)  # the latest report is the one that counts
            else:
                self.meta["usage"] = normal
        feedback = chunk.get("promptFeedback")
        if isinstance(feedback, dict):
            self.meta["promptFeedback"] = feedback
            reason = cast("Json", feedback).get("blockReason")
            if isinstance(reason, str) and reason:
                self.blocked = reason

    def close(self) -> list[Part | Delta]:
        """Complete the open run, if it has text or a signature."""
        if self.open is None:
            return []
        done, self.open = self.open, None
        return [done.part()] if done.chunks or done.signature is not None else []

    def _text(self, part: Json) -> list[Part | Delta]:
        """Consecutive text (or thinking) parts are one run. A `thoughtSignature` ends the run:
        it comes on the last chunk of the text it signs (often with no text of its own), so
        the run up to and including that chunk is one saved part carrying it, and the text after
        it is a new part. Without signatures a whole run is one part."""
        kind = "thinking" if part.get("thought") is True else "text"
        text = part.get("text", "")
        signature = part.get("thoughtSignature")
        if not isinstance(text, str):
            raise SampleError(f"a part's text is not a string: {text!r}")
        if signature is not None and not (isinstance(signature, str) and signature):
            raise SampleError(f"a part's thoughtSignature is not a string: {signature!r}")
        out = self.close() if self.open is not None and self.open.kind != kind else []
        if text:
            if self.open is None:
                self.open = _Open(kind)
            self.open.chunks.append(text)
            out.append(ThinkingDelta(text) if kind == "thinking" else TextDelta(text))
        if signature is not None:
            if self.open is None:
                self.open = _Open(kind)
            self.open.signature = signature
            out += self.close()
        return out

    def _call(self, part: Json) -> list[Part | Delta]:
        """A call arrives whole: `CallNamed`, one `CallArgs`, then the part. Its id is the API's
        when it sent one; otherwise `gemini-` and a fresh random UUID, which is unique across
        sessions and restarts with nothing to coordinate (a counter would repeat). The API's id
        is kept in the replay data as `id` and is sent back; a made-up one is marked
        `synthesized_id` there and never sent."""
        function = _json(part["functionCall"], "a functionCall")
        name = function.get("name")
        if not (isinstance(name, str) and name):
            raise SampleError(f"a functionCall has no name: {function}")
        args = _json(function.get("args", {}), "a functionCall's args")
        wire_id = function.get("id")
        if wire_id is not None and not (isinstance(wire_id, str) and wire_id):
            raise SampleError(f"a functionCall's id is not a string: {wire_id!r}")
        call_id = wire_id or f"gemini-{uuid.uuid4().hex}"
        if call_id in self.call_ids:
            raise SampleError(f"two calls in one reply have the id {call_id!r}")
        self.call_ids.add(call_id)
        replay: Json = {"id": wire_id} if wire_id else {"synthesized_id": True}
        signature = part.get("thoughtSignature")
        if signature is not None:
            if not (isinstance(signature, str) and signature):
                raise SampleError(f"a part's thoughtSignature is not a string: {signature!r}")
            replay["thoughtSignature"] = signature
        out = self.close()
        index, self.calls = self.calls, self.calls + 1
        args_raw = json.dumps(args, ensure_ascii=False)
        out += [CallNamed(index, call_id, name), CallArgs(index, args_raw)]
        out.append(Part.Call(call_id, name, args_raw, _replay(replay)))
        return out

    def _parts(self, raw: object) -> list[Part | Delta]:
        out: list[Part | Delta] = []
        for item in cast("list[object]", raw) if isinstance(raw, list) else []:
            part = _json(item, "a part")
            extra = set(part) - _PART_KEYS
            if extra:
                raise SampleError(f"a part this provider cannot read: {sorted(extra)}")
            out += self._call(part) if "functionCall" in part else self._text(part)
        return out

    def meta_text(self) -> str | None:
        return json.dumps(self.meta) if self.meta else None

    def feed(self, chunk: Json) -> list[Part | Delta]:
        """The parts and deltas this chunk produced, in order: a part comes before the first
        delta of whatever follows it."""
        error = chunk.get("error")
        if error is not None and self.finish is None:
            raise _stream_error(error)
        self._note(chunk)
        candidates = chunk.get("candidates")
        if self.finish is not None or not candidates:
            return []  # a prompt block or a usage-only chunk; after the finish only meta is read
        if not isinstance(candidates, list) or len(cast("list[object]", candidates)) != 1:
            raise SampleError(
                "the stream carries other than one candidate; this provider reads one"
            )
        candidate = _json(cast("list[object]", candidates)[0], "a candidate")
        if candidate.get("index", 0) != 0:
            raise SampleError("the stream carries a candidate other than index 0")
        content = _json(candidate.get("content") or {}, "a candidate's content")
        out = self._parts(content.get("parts"))
        message = candidate.get("finishMessage")
        if isinstance(message, str) and message:
            self.meta["finishMessage"] = message
        finish = candidate.get("finishReason")
        if isinstance(finish, str) and finish:
            out += self.close()
            self.finish = finish
        return out

    def stop(self) -> Finish:
        if self.blocked is not None:
            return Finish(Stop.Refused, self.blocked, self.meta_text())
        if self.finish is None:
            raise SampleError("the stream ended without a finishReason")
        stop = STOPS.get(self.finish)
        if stop is None:
            message = self.meta.get("finishMessage")
            raise SampleError(
                f"finishReason {self.finish!r} is not a finished reply"
                + (f": {message}" if message else "")
            )
        if stop == Stop.End and self.calls:
            stop = Stop.Tools
        return Finish(stop, self.finish, self.meta_text())


# Options


def _extra_problems(model: ModelConfig, extra: object) -> list[str]:
    if not isinstance(extra, Mapping):
        return ["option 'extra_body' must be an object"]
    fields = cast("Mapping[str, object]", extra)
    problems = [
        f"option 'extra_body' may not set {k!r}; the provider owns it"
        for k in fields
        if k in _OWNED_KEYS
    ]
    problems += [
        f"option 'extra_body' holds {bad!r}, which is not JSON" for bad in _non_finite(fields)
    ]
    if "safetySettings" in fields and "safety_settings" in model.options:
        problems.append("option 'extra_body' sets 'safetySettings', which 'safety_settings' sets")
    generation = fields.get("generationConfig")
    if generation is None:
        return problems
    if not isinstance(generation, Mapping):
        return [*problems, "option 'extra_body' key 'generationConfig' must be an object"]
    taken = _generation_keys(model)
    problems += [
        f"option 'extra_body' sets generationConfig.{k}, which the model's settings already set"
        for k in cast("Mapping[str, object]", generation)
        if k in taken
    ]
    return problems


def _generation_keys(model: ModelConfig) -> list[str]:
    """The `generationConfig` keys the model's own settings set."""
    mapping = {
        "temperature": "temperature",
        "top_p": "topP",
        "top_k": "topK",
        "thinking": "thinkingConfig",
    }
    keys = [wire for option, wire in mapping.items() if option in model.options]
    if model.max_tokens is not None:
        keys.append("maxOutputTokens")
    return keys


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
    seen: set[str] = set()
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
        elif category in seen:
            problems.append(f"option 'safety_settings' sets {category!r} twice")
        else:
            seen.add(cast("str", category))
    return problems


def _retry_delay(body: bytes) -> float | None:
    try:
        parsed: object = json.loads(body)
    except ValueError:
        return None
    error = cast("Json", parsed).get("error") if isinstance(parsed, dict) else None
    return _delay(cast("Json", error)) if isinstance(error, dict) else None


class GeminiProvider:
    """Samples Gemini's `streamGenerateContent` (`?alt=sse`) for the `ModelConfig` the driver
    resolved.

    One `httpx.AsyncClient` serves every sample, made on the first and closed by `aclose()`,
    after which a sample fails with `ProviderClosed`; a client belongs to the event loop that made
    it (as in `OpenAIProvider`). `transport` replaces the network (a test's
    `httpx.MockTransport`); `environ` is where `api_key_env` is looked up, at sample time.

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
        """Problems with the model's settings: no key named, an unknown or ill-typed option. Whether
        the named variable is set is found when sampling."""
        problems: list[str] = []
        if model.provider != NAME:
            problems.append(f"provider is {model.provider!r}, not {NAME!r}")
        if model.api_key_env is None and model.literal_api_key is None:
            problems.append("needs an API key: set api_key_env (or api_key)")
        options = model.options
        problems += [
            f"unknown option {key!r}; known options are {list(_OPTIONS)}"
            for key in options
            if key not in _OPTIONS
        ]
        problems += [
            f"option {key!r} must be a number"
            for key in ("temperature", "top_p", "top_k")
            if key in options and not _is_number(options[key])
        ]
        if "thinking" in options:
            problems += _thinking_problems(options["thinking"])
        if "safety_settings" in options:
            problems += _safety_problems(options["safety_settings"])
        if "extra_body" in options:
            problems += _extra_problems(model, options["extra_body"])
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
        """The URL, headers, and JSON body of the request for this sample.

        Raises `SampleError` for settings `check` refuses, a walk with nothing to send, or one
        the API would reject.
        """
        problems = self.check(model)
        if problems:
            raise SampleError("; ".join(problems))
        try:
            key = model.api_key(self._environ)
        except ConfigError as e:  # the variable `api_key_env` names is unset: known only now
            raise SampleError(str(e)) from e
        system, history = contents(walk)
        if not history:
            raise SampleError("the walk has no user content to send")
        body: Json = {}
        if system is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        body["contents"] = history
        if tools:
            body["tools"] = _tools(tools)
        options = model.options
        extra = cast("Json", _plain(options["extra_body"])) if "extra_body" in options else {}
        generation: Json = {}
        if model.max_tokens is not None:
            generation["maxOutputTokens"] = model.max_tokens
        for option, wire in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK")):
            if option in options:
                generation[wire] = options[option]
        if "thinking" in options:
            generation["thinkingConfig"] = _thinking_config(
                cast("Mapping[str, object]", options["thinking"])
            )
        generation.update(cast("Json", extra.pop("generationConfig", {})))
        if generation:
            body["generationConfig"] = generation
        if "safety_settings" in options:
            body["safetySettings"] = _plain(options["safety_settings"])
        body.update(extra)
        name = model.model if "/" in model.model else f"models/{model.model}"
        base = (model.base_url or DEFAULT_BASE_URL).rstrip("/")
        url = f"{base}/{name}:streamGenerateContent?alt=sse"
        return url, {"x-goog-api-key": cast("str", key)}, body

    @staticmethod
    async def _refuse(response: httpx.Response) -> None:
        """Raise the `SampleError` for an error response, with its status and wait taken
        (the `Retry-After` header, else the body's `RetryInfo`)."""
        status = response.status_code
        wait = _retry_after(response.headers.get("retry-after"))
        try:
            body = await response.aread()
        except httpx.HTTPError as e:
            raise SampleError(
                f"HTTP {status}: the error body could not be read ({type(e).__name__}: {e})",
                status=status,
                retry_after=wait,
            ) from e
        if wait is None:
            wait = _retry_delay(body)
        raise SampleError(_error_text(status, body), status=status, retry_after=wait)

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        """Yield deltas as they arrive and each part once complete (before the first delta of
        whatever follows it), then the `Finish`. A failure raises `SampleError` (with `status`
        and `retry_after` for an HTTP error or an error event) after yielding every part the
        stream had finished or begun; it carries the meta that had arrived. Once `finishReason`
        has arrived the reply is done: the stream is still read for `usageMetadata`, and a failure
        after it costs only the meta not yet received. A prompt the API blocked (`promptFeedback`
        with a `blockReason`) ends as `Refused`, the reason in `finish_reason` and the block in
        `meta`."""
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
        if error is None or reply.finish is not None or reply.blocked is not None:
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


def _thinking_config(thinking: Mapping[str, object]) -> Json:
    wire = {
        "include_thoughts": "includeThoughts",
        "budget": "thinkingBudget",
        "level": "thinkingLevel",
    }
    return {wire[k]: v for k, v in thinking.items()}


async def _chunks(response: httpx.Response) -> AsyncIterator[Json]:
    """The decoded chunks, to the end of the body (so the connection goes back to the pool)."""
    async for data in _events(response):
        try:
            yield _json(json.loads(data), "a stream chunk")
        except ValueError as e:
            raise SampleError(f"malformed SSE data {data[:200]!r}: {e}") from e
