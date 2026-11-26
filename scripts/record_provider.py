#!/usr/bin/env python3
"""Record provider fixtures from real servers (D26 §10), redacted of keys.

    .venv/bin/python scripts/record_provider.py all [--keys FILE] [--only FAMILY,...]
        [--scenarios NAME,...] [--rounds N] [--out DIR] [--dry-run]
    .venv/bin/python scripts/record_provider.py FAMILY --model MODEL_ID [--base-url URL]
        [--key-env VAR] [--case SCENARIO] [--out DIR] [--gap SECONDS] [--max-tokens N]
        [--option KEY=JSON ...] [--dry-run]
    .venv/bin/python scripts/record_provider.py --regolden CASE_DIR
    .venv/bin/python scripts/record_provider.py --scenarios-list

(Run it with the Python that has `tau` installed; `./scripts/record_provider.py` works in an
activated venv.)

**`all`** records every scenario (`SCENARIOS`) for every family the keys file sets up, and
skips, saying so, each family whose key or URL is empty and each case already recorded. The keys
file (`--keys`, default `$TAU_FIXTURE_KEYS`, else `~/.config/tau-fixtures/keys.env`) holds
`NAME=VALUE` lines: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `LLAMACPP_BASE_URL`, `VLLM_BASE_URL`, and any number of
`OAICOMPAT_<NAME>_BASE_URL` with an optional `OAICOMPAT_<NAME>_API_KEY` (the family
`oaicompat-<name>`). `<PREFIX>_MODEL` overrides a family's model and `<PREFIX>_WINDOW` its
context window (for `error-overflow`), where PREFIX is `OPENAI`, `ANTHROPIC`, `GEMINI`,
`OPENROUTER`, `MISTRAL`, `LLAMACPP`, `VLLM` or `OAICOMPAT_<NAME>`. The values never reach
`os.environ`, a file written or anything printed. `--dry-run` sends nothing that costs: it
prints each family's plan (scenarios, requests, estimated tokens and cost) and asks each
provider's free model list whether the model exists.

**FAMILY** records one scenario (`--case`, default `turns`) for one family: `anthropic`,
`openai`, `gemini`, `openrouter`, `llamacpp` or `vllm` (the last two need `--base-url`; a key is
optional for them). The key is read from the environment variable `--key-env` names, never
from the command line, and is never written.

A `turns` scenario drives a real `Driver` and session over the built-in provider; a `raw` one
sends one request built here, for what the provider never sends (a non-streamed request, a
malformed body, a request past the window). Both go through a
transport that forwards to the network and keeps every exchange. Nothing is written unless the
whole run succeeded (a `raw` error scenario succeeds when the server refused it) and no file
holds a key or anything shaped like one (`SECRET_SHAPES`). An existing case is never
overwritten.

`--regolden CASE_DIR` replays a recorded case (no network, no key) and rewrites its
`expected.json`: for when a normalizer legitimately changes.

Exit status: 0 written (for `all`: every family that was not skipped recorded every
scenario), 1 a run failed or a fixture is not publishable, 2 usage.

# The fixture format, version 1

    <family>/<case>/
      case.json          how to run the case again
      NN.request.json    {"method", "url", "headers", "body"}
      NN.response.json   {"status", "headers"}
      NN.response.body   the response's bytes exactly as received (SSE or JSON)
      expected.json      {"replies": [{"usage", "stop", "finish_reason"}, ...], "error": ...}

`NN` is the exchange's index, two digits from `00`, in the order the requests were sent.

`case.json`:

    {"format": 1, "family": ..., "provider": ..., "kind": "turns" | "raw",
     "scenario": "<name>", "model": {...}, "system": "...", "turns": ["...", ...],
     "gaps": [0, 0, 0, 360], "tools": [{"name", "description", "parameters", "answers",
     "key", "images"?}], "extensions"?: {...}, "turn_images"?: [[{"width", "height", "rgb"}]],
     "raw"?: {"stream", "adapter", "error"}, "tau": "<tau.__version__>", "recorded": "YYYY-MM-DD"}

- `model` is the settings object the model catalog was given (`provider`, `model`,
  `base_url`, `max_tokens`, `options`, `api_key_env`; the unset ones absent). `api_key_env` is
  the variable's name. A replay puts any text under that name. A local server's origin
  (`llamacpp`, `vllm`, not loopback) is written as `http://<family>.invalid`, in every file.
- `kind` `turns`: a session with `system`, the `tools`, and each of `turns` sent in order with
  auto-step on, waiting for the cursor to rest before the next. `gaps[i]` is the seconds
  really slept before turn `i` at record time; a replay does not sleep. `extensions`, when
  present, is `{"token_calls": {OPTIONS}}`: the built-in `token_calls` was loaded with those
  options (and `tokens`, the counter it needs), so its count calls are among the exchanges.
- `kind` `raw`: the one request was built by hand, not by the provider (its body is in
  `00.request.json`; a body too large to keep, `error-overflow`'s, has its long text replaced
  by a note of what it was). `raw.adapter` true: a replay samples the provider once over the
  walk `system`, `turns[0]` with no tools and reads the recorded response (a `GET` the provider
  makes first, such as llama.cpp's `/props`, is answered 404); false: the response is not one
  the provider reads (it streams, and this was not streamed), and only the wire is a fixture.
  `raw.error` true: the server was expected to refuse it.
- A **table tool** answers a call whose JSON arguments are exactly `{"<key>": K}` with
  `answers[K]` (and, when `images` has K, that PNG, base64, after the text), and with the
  error `no such <key>: K` otherwise (K is the arguments' text when they are not that shape).
  The rule is the same at record and at replay, so a replay reproduces the walk.
- `turn_images` (optional; a case without it sends no user images) is one list per turn, in
  `turns`' order, of the images the user attaches to that turn (D31): each is a flat-colour
  PNG of `width` by `height` pixels and colour `rgb`, which `png()` writes the same way every
  time, so a replay regenerates the exact bytes. They are sent with `Client.send(images=)`, so
  the text goes first and the images after it, as tau sends them.

Requests keep only the headers in `REQUEST_HEADERS` and responses those in `RESPONSE_HEADERS`;
every other header is dropped. URLs lose the query parameters in `SECRET_PARAMS`. `body` is the
request's JSON, or null when it had none. Requests are sent with `accept-encoding: identity`,
so a response body is never compressed.

`expected.json` is what τ computed at record time: one object per `Assistant` node on the
cursor's final walk, in order, with `usage` (its `meta`'s `usage`, or null), `stop` (`End`,
`Tools`, `Length`, ...) and `finish_reason` (the provider's own string, or null); `error` is
null, or for a `raw` case whose sample failed, `{"status", "message"}` of the `SampleError`.
For a `raw` case `replies` is the one reply of the replay (empty when it failed), or null when
`raw.adapter` is false.
"""

import argparse
import asyncio
import base64
import datetime
import ipaddress
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import zlib
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

import tau
from tau import (
    AnthropicProvider,
    Body,
    ConfigError,
    Driver,
    Finish,
    GeminiProvider,
    Image,
    ModelCatalog,
    OpenAIProvider,
    Outcome,
    Provider,
    SampleError,
    Session,
    SystemClock,
    TokenCalls,
    Tokens,
    model_config,
    read_models,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "providers"
FORMAT = 1
ALIAS = "m"  # the catalog name of the one model
MIN_KEY = 8  # a shorter key is not searched for: it would match by chance
REPLAY_KEY = "replayed-no-key"
KEYS_FILE = Path("~/.config/tau-fixtures/keys.env")

REQUEST_HEADERS = ("content-type", "anthropic-version", "accept")
RESPONSE_HEADERS = ("content-type",)
SECRET_PARAMS = ("key", "api_key", "access_token", "token")
SECRET_SHAPES = (
    re.compile(rb"sk-[A-Za-z0-9_\-]{20,}"),  # OpenAI, Anthropic (sk-ant-), OpenRouter (sk-or-)
    re.compile(rb"AIza[0-9A-Za-z_\-]{30,}"),  # Google
    re.compile(rb"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(rb"(?i)\"(authorization|x-api-key|x-goog-api-key|api[-_]key)\"\s*:"),
    re.compile(rb"(?i)openai-(organization|project)"),
    re.compile(rb"\borg-[A-Za-z0-9]{20,}"),
    re.compile(rb"\b(10\.\d{1,3}|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
)
"""What looks like a key, or a header that carries one: a file matching any is not written.
`tests/providers/test_fixture_secrets.py` holds every committed fixture to its own list."""

# -- the `turns` scenario's conversation (D26 §10)

SYSTEM = (
    "You are a parts clerk. Answer questions about parts only from what the lookup_part "
    "tool returns; never guess a part's details."
)
TURNS = (
    "Look up part A-113 and tell me which bin it is in.",
    "Now look up B-207 and C-042 and tell me which of the two has more in stock.",
    "Look up part Z-999. If there is no such part, say so in one sentence.",
)
GAP_TURN = "Which of the parts we looked up is in bin 14? Answer from what you already know."
TOOL: dict[str, object] = {
    "name": "lookup_part",
    "description": "Look up one part by its part number: what it is, its bin and its stock.",
    "parameters": {
        "type": "object",
        "properties": {"part": {"type": "string", "description": "a part number, like A-113"}},
        "required": ["part"],
        "additionalProperties": False,
    },
    "answers": {
        "A-113": "A-113: hex bolt M8 x 40, zinc plated. Bin 14. 220 in stock.",
        "B-207": "B-207: flat washer M8, stainless. Bin 3. 1450 in stock.",
        "C-042": "C-042: lock nut M8, nylon insert. Bin 9. 85 in stock.",
    },
    "key": "part",
}

CATALOGUE = "\n".join(
    f"Part K-{n:03}: {kind}, grade {n % 7 + 2}. Bin {n % 40 + 1}. {(n * 37) % 900 + 10} in stock."
    for n, kind in (
        (n, ("hex bolt", "washer", "lock nut", "spring pin", "rivet")[n % 5]) for n in range(1, 321)
    )
)
CACHED_SYSTEM = (
    f"{SYSTEM}\n\nThe K-series catalogue below is for reference only; the parts asked about "
    f"are never in it.\n\n{CATALOGUE}"
)
"""A system text past every Anthropic model's minimum cacheable prompt (4096 tokens for the
smallest models), so its requests are cached and their spans proved by `cache` (D26 3.1)."""

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"  # the provider's own default
ANTHROPIC_URL = "https://api.anthropic.com/v1"  # the provider's own default
RAW_SYSTEM = "Answer in one short sentence."
RAW_TURN = "Search the web: in what year was the first transatlantic telegraph cable completed?"


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A PNG of one colour, written here so no image file is needed."""

    def block(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    row = b"\x00" + bytes(rgb) * width
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress(row * height, 9)
    return (
        b"\x89PNG\r\n\x1a\n" + block(b"IHDR", header) + block(b"IDAT", pixels) + block(b"IEND", b"")
    )


@dataclass(frozen=True)
class Swatch:
    """A user image described deterministically: a flat-colour PNG (`case.json`'s
    `turn_images`), so a replay regenerates the exact bytes."""

    width: int
    height: int
    rgb: tuple[int, int, int]

    def data(self) -> bytes:
        return png(self.width, self.height, self.rgb)

    def described(self) -> dict[str, object]:
        return {"width": self.width, "height": self.height, "rgb": list(self.rgb)}


RED_SWATCH = Swatch(64, 64, (220, 30, 30))
UHD_SWATCH = Swatch(3840, 2160, (30, 160, 60))  # a 4K screenshot's size; flat, so a few kB
RED = base64.b64encode(RED_SWATCH.data()).decode()
BLUE = base64.b64encode(png(64, 64, (30, 60, 220))).decode()
SWATCH_TOOL: dict[str, object] = {
    "name": "show_swatch",
    "description": "Show one colour swatch by its id, as an image.",
    "parameters": {
        "type": "object",
        "properties": {"swatch": {"type": "string", "description": "a swatch id, like S-1"}},
        "required": ["swatch"],
        "additionalProperties": False,
    },
    "answers": {"S-1": "Swatch S-1, as an image:"},
    "images": {"S-1": BLUE},
    "key": "swatch",
}

# -- scenarios


Wire = Literal["openai", "anthropic", "gemini"]
ALL_WIRES: frozenset[Wire] = frozenset(("openai", "anthropic", "gemini"))


@dataclass(frozen=True)
class Target:
    """One family as `all` or the command line resolved it: where to send and what to call
    the model, and the key (never written)."""

    family: str
    provider: str
    wire: Wire
    model: str
    base_url: str | None
    key_env: str | None
    key: str | None
    options: Mapping[str, object]
    max_tokens: int
    window: int | None
    local: bool  # its origin is not written into a fixture
    gap: float = 0

    @property
    def root(self) -> str:
        default = {"anthropic": ANTHROPIC_URL, "gemini": GEMINI_URL}.get(self.wire, "")
        return (self.base_url or default).rstrip("/")


@dataclass(frozen=True)
class RawRequest:
    """A request built by hand: `body` is what is sent; `kept` what the fixture keeps of it."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: Mapping[str, object]
    kept: Mapping[str, object] | None = None


@dataclass(frozen=True)
class Scenario:
    """One recordable case. `kind` `turns` runs `turns` through the driver; `raw` sends
    `build(target)`. `wires` are the wire formats it applies to; `local_only` scenarios need a
    server that counts for free. `requests`, `tokens_in`, `tokens_out` estimate one run (what
    `--dry-run` adds up); `billed` false when the server refuses it before any token is
    counted."""

    name: str
    what: str
    kind: Literal["turns", "raw"]
    requests: int
    tokens_in: int
    tokens_out: int
    wires: frozenset[Wire] = ALL_WIRES
    system: str = SYSTEM
    turns: tuple[str, ...] = TURNS
    tools: tuple[Mapping[str, object], ...] = (TOOL,)
    max_tokens: int | None = None  # None: the family's
    plain: bool = False  # True: none of the family's options (thinking needs room)
    images: bool = False  # the model's `images: true` (D30): it takes a tool's or user's images
    user_images: tuple[tuple[Swatch, ...], ...] = ()  # per turn, what the user attaches (D31)
    stream: bool = True
    adapter: bool = True
    error: bool = False
    refusals: frozenset[int] = frozenset((400, 413, 422))  # the statuses that are this refusal
    billed: bool = True
    local_only: bool = False
    extensions: Mapping[str, Mapping[str, object]] | None = None
    build: Callable[[Target], RawRequest] | None = None
    needs_window: bool = False
    gapped: bool = False  # ends with GAP_TURN after the family's gap (Anthropic: a cache miss)


def _openai_headers(target: Target) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if target.key is not None:
        headers["authorization"] = f"Bearer {target.key}"
    return headers


def _openai_cap(target: Target) -> str:
    """OpenAI's own API takes `max_completion_tokens` (its reasoning models refuse
    `max_tokens`); every compatible server takes `max_tokens`."""
    return "max_completion_tokens" if target.family == "openai" else "max_tokens"


def _anthropic_headers(target: Target) -> dict[str, str]:
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if target.key is not None:
        headers["x-api-key"] = target.key
    return headers


def _gemini_headers(target: Target) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if target.key is not None:
        headers["x-goog-api-key"] = target.key
    return headers


MALFORMED = "not a list of messages"


def _chat(  # noqa: PLR0913 - one request, every part of it said
    target: Target,
    system: str,
    items: Sequence[tuple[str, str]],
    *,
    stream: bool,
    tools: Sequence[Mapping[str, object]] = (),
    max_tokens: int = 512,
    malformed: bool = False,
) -> RawRequest:
    """One request in `target`'s wire format: `system`, one user message of `items` (each
    `("text", TEXT)` or `("png", BASE64)`), and `tools`. `malformed`: the messages are the text
    `MALFORMED` instead of a list, which every API refuses."""
    match target.wire:
        case "openai":
            content = [
                {"type": "text", "text": v}
                if k == "text"
                else {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{v}"}}
                for k, v in items
            ]
            messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
            body: dict[str, object] = {
                "model": target.model,
                "messages": MALFORMED if malformed else messages,
                "stream": stream,
                _openai_cap(target): max_tokens,
            }
            if stream:
                body["stream_options"] = {"include_usage": True}
            if tools:
                body["tools"] = [
                    {
                        "type": "function",
                        "function": {k: t[k] for k in ("name", "description", "parameters")},
                    }
                    for t in tools
                ]
            url = f"{target.root}/chat/completions"
            return RawRequest("POST", url, _openai_headers(target), body)
        case "anthropic":
            blocks = [
                {"type": "text", "text": v}
                if k == "text"
                else {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": v},
                }
                for k, v in items
            ]
            body = {
                "model": target.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": MALFORMED if malformed else [{"role": "user", "content": blocks}],
                "stream": stream,
            }
            if tools:
                body["tools"] = [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["parameters"],
                    }
                    for t in tools
                ]
            return RawRequest("POST", f"{target.root}/messages", _anthropic_headers(target), body)
        case "gemini":
            parts = [
                {"text": v}
                if k == "text"
                else {"inline_data": {"mime_type": "image/png", "data": v}}
                for k, v in items
            ]
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": MALFORMED if malformed else [{"role": "user", "parts": parts}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            }
            if tools:
                declared = [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "parametersJsonSchema": t["parameters"],  # as GeminiProvider sends it
                    }
                    for t in tools
                ]
                body["tools"] = [{"functionDeclarations": declared}]
            method = "streamGenerateContent?alt=sse" if stream else "generateContent"
            url = f"{target.root}/models/{target.model}:{method}"
            return RawRequest("POST", url, _gemini_headers(target), body)


def _nonstream_text(target: Target) -> RawRequest:
    items = [("text", "Name the three primary colours of light.")]
    return _chat(target, "Be terse.", items, stream=False)


def _nonstream_tools(target: Target) -> RawRequest:
    items = [("text", "Look up part A-113.")]
    return _chat(target, SYSTEM, items, stream=False, tools=(TOOL,), max_tokens=1024)


def _bad_request(target: Target) -> RawRequest:
    return _chat(target, "Be terse.", [("text", "Hello.")], stream=True, malformed=True)


OVERFLOW_WORD = "lorem "


def _overflow(target: Target) -> RawRequest:
    """A user message of one more word than the window has tokens, plus 10% (a word is at least
    one token in every tokenizer this was tried with)."""
    assert target.window is not None
    words = target.window + target.window // 10
    items = [("text", OVERFLOW_WORD * words)]
    built = _chat(target, "Be terse.", items, stream=True, max_tokens=16)
    note = f"<{OVERFLOW_WORD!r} * {words}: elided by the recorder>"
    kept = json.loads(json.dumps(built.body).replace(json.dumps(OVERFLOW_WORD * words)[1:-1], note))
    return replace(built, kept=kept)


def _builtin_tool(target: Target) -> RawRequest:
    """Gemini's built-in `google_search`, which `GeminiProvider` cannot offer (D26 §2)."""
    body = {
        "systemInstruction": {"parts": [{"text": RAW_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": RAW_TURN}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": target.max_tokens},
    }
    url = f"{target.root}/models/{target.model}:streamGenerateContent?alt=sse"
    return RawRequest("POST", url, _gemini_headers(target), body)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "turns",
        "three turns with a tool: one call, two parallel calls, a call that errors; reasoning "
        "where the model thinks; usage with cached tokens from the second request",
        "turns",
        requests=6,
        tokens_in=4500,
        tokens_out=2500,
    ),
    Scenario(
        "cached",
        "the turns scenario over a system text past the minimum cacheable prompt, so each "
        "request reads the one before from the cache (D26 3.1), then a turn after the gap: a miss",
        "turns",
        requests=7,
        tokens_in=48000,
        tokens_out=2500,
        wires=frozenset(("anthropic",)),
        system=CACHED_SYSTEM,
        gapped=True,
    ),
    Scenario(
        "tool-image",
        "a tool result that holds an image (D30), and the reply that reads it",
        "turns",
        requests=2,
        tokens_in=2000,
        tokens_out=600,
        system="You describe colour swatches. Use show_swatch to see one before you answer.",
        turns=("What colour is swatch S-1? Answer in one word.",),
        tools=(SWATCH_TOOL,),
        images=True,
    ),
    Scenario(
        "length",
        "a reply cut at max_tokens: stop Length",
        "turns",
        requests=1,
        tokens_in=80,
        tokens_out=24,
        system="Be terse.",
        turns=("Count from 1 to 300, separated by commas.",),
        tools=(),
        max_tokens=24,
        plain=True,
    ),
    Scenario(
        "user-image",
        "an image in a user message (D31), and the reply that reads it",
        "turns",
        requests=1,
        tokens_in=900,
        tokens_out=300,
        system="Answer in one word.",
        turns=("What colour is this image?",),
        tools=(),
        images=True,
        user_images=((RED_SWATCH,),),
    ),
    Scenario(
        "user-image-large",
        "a 3840x2160 image in a user message (flat, so small in bytes)",
        "turns",
        requests=1,
        tokens_in=6000,
        tokens_out=300,
        system="Answer in one word.",
        turns=("What colour is this image?",),
        tools=(),
        images=True,
        user_images=((UHD_SWATCH,),),
    ),
    Scenario(
        "user-and-tool-image",
        "an image in a user message and a tool result's image, in one request (D30, D31)",
        "turns",
        requests=2,
        tokens_in=2500,
        tokens_out=600,
        system="You describe colour swatches. Use show_swatch to see one before you answer.",
        turns=(
            "The attached image is a swatch of mine. What colour is it, and what colour is "
            "swatch S-1? Answer as: mine is <colour>, S-1 is <colour>.",
        ),
        tools=(SWATCH_TOOL,),
        images=True,
        user_images=((RED_SWATCH,),),
    ),
    Scenario(
        "nonstream-text",
        "a non-streamed text reply (τ always streams: wire only)",
        "raw",
        requests=1,
        tokens_in=40,
        tokens_out=400,
        adapter=False,
        stream=False,
        build=_nonstream_text,
    ),
    Scenario(
        "nonstream-tools",
        "a non-streamed tool call (wire only)",
        "raw",
        requests=1,
        tokens_in=300,
        tokens_out=400,
        adapter=False,
        stream=False,
        build=_nonstream_tools,
    ),
    Scenario(
        "error-bad-request",
        "a malformed body: the server's 4xx, through the provider's error path",
        "raw",
        requests=1,
        tokens_in=0,
        tokens_out=0,
        system="Be terse.",
        turns=("Hello.",),
        error=True,
        billed=False,
        build=_bad_request,
    ),
    Scenario(
        "error-overflow",
        "a request past the context window: the server's refusal, through the provider",
        "raw",
        requests=1,
        tokens_in=0,
        tokens_out=0,
        system="Be terse.",
        turns=("<the elided text>",),
        error=True,
        billed=False,
        build=_overflow,
        needs_window=True,
    ),
    Scenario(
        "counted",
        "the turns scenario with the built-in token_calls counting every request first "
        "(D26 §8): each count call, then the bill it predicts",
        "turns",
        requests=12,
        tokens_in=4500,
        tokens_out=2500,
        wires=frozenset(("openai",)),
        local_only=True,
        extensions={"token_calls": {"when": "always"}},
    ),
    Scenario(
        "builtin-tool",
        "Gemini's built-in google_search, for toolUsePromptTokenCount (D26 §2)",
        "raw",
        requests=1,
        tokens_in=300,
        tokens_out=200,
        wires=frozenset(("gemini",)),
        system=RAW_SYSTEM,
        turns=(RAW_TURN,),
        tools=(),
        build=_builtin_tool,
    ),
)
BY_NAME = {s.name: s for s in SCENARIOS}


# -- families


@dataclass(frozen=True)
class Family:
    """One server family: its provider, wire, and the defaults of `--key-env`, `--base-url`,
    the model and the options `all` uses. `local`: a server on the owner's network, whose
    origin is not written; `counts`: it has a free count endpoint (`counted`)."""

    provider: str
    key_env: str | None = None  # None: a key is optional, and none is sent unless --key-env
    base_url: str | None = None
    needs_base_url: bool = False
    model: str | None = None  # None: the first of the server's /models
    options: Mapping[str, object] = field(default_factory=lambda: {})
    max_tokens: int = 2048
    window: int | None = None
    local: bool = False
    counts: bool = False
    price: tuple[float, float] | None = None  # assumed USD per million tokens, in and out


FAMILIES: dict[str, Family] = {
    "anthropic": Family(
        "anthropic",
        "ANTHROPIC_API_KEY",
        model="claude-haiku-4-5",
        options={"thinking": {"type": "enabled", "budget_tokens": 1024}},
        max_tokens=4096,
        window=200_000,
        price=(1.0, 5.0),
    ),
    "openai": Family(
        "openai",
        "OPENAI_API_KEY",
        "https://api.openai.com/v1",
        model="gpt-5-nano",
        options={"reasoning_effort": "low"},
        window=400_000,
        price=(0.05, 0.40),
    ),
    "gemini": Family(
        "gemini",
        "GEMINI_API_KEY",
        model="gemini-3.5-flash-lite",  # 2.5-flash-lite is refused to new users (2026-11)
        options={"thinking": {"include_thoughts": True}},
        window=1_048_576,
        price=(0.10, 0.40),
    ),
    "openrouter": Family(
        "openai",
        "OPENROUTER_API_KEY",
        "https://openrouter.ai/api/v1",
        model="openai/gpt-5-nano",
        window=400_000,
        price=(0.05, 0.40),
    ),
    "mistral": Family(
        "openai",
        "MISTRAL_API_KEY",
        "https://api.mistral.ai/v1",
        model="mistral-small-2603",  # vision, tools and reasoning (its /v1/models capabilities)
        window=262_144,
        price=(0.10, 0.30),
    ),
    "llamacpp": Family("openai", needs_base_url=True, local=True, counts=True),
    "vllm": Family("openai", needs_base_url=True, local=True, counts=True),
}
OAICOMPAT = Family("openai", needs_base_url=True)
PROVIDERS = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "gemini": GeminiProvider}
WIRES: dict[str, Wire] = {"anthropic": "anthropic", "openai": "openai", "gemini": "gemini"}
PREFIXES = {
    "anthropic": "ANTHROPIC",
    "openai": "OPENAI",
    "gemini": "GEMINI",
    "openrouter": "OPENROUTER",
    "mistral": "MISTRAL",
    "llamacpp": "LLAMACPP",
    "vllm": "VLLM",
}


def family_of(name: str) -> Family:
    if name.startswith("oaicompat-"):
        return OAICOMPAT
    return FAMILIES[name]


def applies(scenario: Scenario, family: str, target_wire: Wire) -> bool:
    """Whether `scenario` is recorded for `family`."""
    if target_wire not in scenario.wires:
        return False
    return not (scenario.local_only and not family_of(family).counts)


class Usage(Exception):
    """The command line asks for something this cannot do: exit 2."""


class Failed(Exception):
    """The run failed, or what it recorded cannot be published: exit 1."""


# Redaction


def strip_query(url: str) -> str:
    """`url` without the query parameters in `SECRET_PARAMS`; every other part as it was."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in SECRET_PARAMS]
    if len(kept) == len(pairs):
        return url
    return urlunsplit(parts._replace(query=urlencode(kept)))


def kept_headers(headers: Mapping[str, str], names: Sequence[str]) -> dict[str, str]:
    """The headers of `headers` named in `names` (lower case); every other is dropped."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return {name: lowered[name] for name in names if name in lowered}


def find_key(files: Mapping[str, bytes], key: str | None) -> str | None:
    """The name of the first of `files` holding `key` (as it is, or percent-encoded), if any.
    A key shorter than `MIN_KEY` is not searched for."""
    if key is None or len(key) < MIN_KEY:
        return None
    needles = {key.encode(), quote(key, safe="").encode()}
    for name in sorted(files):
        if any(needle in files[name] for needle in needles):
            return name
    return None


def find_shape(files: Mapping[str, bytes]) -> str | None:
    """`NAME: what` for the first of `files` holding something shaped like a key
    (`SECRET_SHAPES`), if any."""
    for name in sorted(files):
        for shape in SECRET_SHAPES:
            if shape.search(files[name]):
                return f"{name}: something matching {shape.pattern.decode()!r}"
    return None


def scrub(text: str, secrets: Mapping[str, str]) -> str:
    """`text` with every value of `secrets` (at least `MIN_KEY` long) replaced: for what is
    printed, which may quote a server's error."""
    for secret in secrets.values():
        if len(secret) >= MIN_KEY:
            text = text.replace(secret, "<redacted>").replace(quote(secret, safe=""), "<redacted>")
    return text


def private_origin(url: str, *, local: bool) -> str | None:
    """`scheme://host[:port]` of `url` when it is not to be written: a `local` family's server
    that is not on loopback, or any server at a private IP address."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        hidden = local and host != "localhost"
    else:
        hidden = not address.is_loopback and (local or address.is_private)
    return f"{parts.scheme}://{parts.netloc}" if hidden else None


def public_base(family: str) -> str:
    return f"http://{family}.invalid"


# The table tool


class TableTool:
    """A tool that answers from a table (see the format). Built from a `case.json` tool."""

    def __init__(self, spec: Mapping[str, object]) -> None:
        self.name = str(spec["name"])
        self.description = str(spec["description"])
        self.parameters: Mapping[str, object] = spec["parameters"]  # type: ignore[assignment]
        self._answers: Mapping[str, str] = spec["answers"]  # type: ignore[assignment]
        self._images: Mapping[str, str] = spec.get("images", {})  # type: ignore[assignment]
        self._key = str(spec["key"])

    async def call(self, args_raw: str) -> Outcome:
        try:
            args = json.loads(args_raw)
        except ValueError:
            args = None
        if isinstance(args, dict) and list(args) == [self._key]:
            wanted = args[self._key]
            if isinstance(wanted, str) and wanted in self._answers:
                images = (
                    [Image(base64.b64decode(self._images[wanted]))]
                    if wanted in self._images
                    else []
                )
                return Outcome.Ok(self._answers[wanted], images)
            return Outcome.Err(f"no such {self._key}: {wanted}")
        return Outcome.Err(f"no such {self._key}: {args_raw}")


# Transports


@dataclass
class Exchange:
    """One request and what answered it. `status` is None until a response began."""

    method: str
    url: str
    request_headers: dict[str, str]
    request_body: bytes
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict[str, str])
    chunks: list[bytes] = field(default_factory=list[bytes])


class _Kept(httpx.AsyncByteStream):
    """A response body that keeps each chunk it passes on."""

    def __init__(self, inner: httpx.AsyncByteStream, chunks: list[bytes]) -> None:
        self._inner = inner
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._chunks.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class Recording(httpx.AsyncBaseTransport):
    """Forwards each request to `inner` and keeps the exchange, in the order requests were
    sent. It asks for an uncompressed body, so the bytes kept are the bytes a replay reads."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.exchanges: list[Exchange] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        request.headers["accept-encoding"] = "identity"
        exchange = Exchange(request.method, str(request.url), dict(request.headers), body)
        self.exchanges.append(exchange)
        response = await self._inner.handle_async_request(request)
        exchange.status = response.status_code
        exchange.response_headers = dict(response.headers)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_Kept(response.stream, exchange.chunks),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


class Replay(httpx.AsyncBaseTransport):
    """Answers the k-th request with the k-th recorded response. A request whose method or
    URL is not the recorded one, or one more than were recorded, is refused: `mismatch` says
    which, and the request fails. With `raw`, a `GET` that was not recorded is answered 404
    and not counted (the provider's own probes, such as llama.cpp's `/props`)."""

    def __init__(self, case_dir: Path, *, raw: bool = False) -> None:
        self._requests = [
            json.loads(p.read_text()) for p in sorted(case_dir.glob("*.request.json"))
        ]
        self._case_dir = case_dir
        self._seen = 0
        self._raw = raw
        self.mismatch: str | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        got = f"{request.method} {strip_query(str(request.url))}"
        k = self._seen
        if self._raw and request.method == "GET":
            recorded = self._requests[k] if k < len(self._requests) else None
            if recorded is None or f"{recorded['method']} {recorded['url']}" != got:
                return httpx.Response(404, json={"error": "not recorded"})
        self._seen += 1
        if k >= len(self._requests):
            self.mismatch = f"request {k:02} ({got}): only {len(self._requests)} were recorded"
        else:
            recorded = f"{self._requests[k]['method']} {self._requests[k]['url']}"
            if got != recorded:
                self.mismatch = f"request {k:02} is {got}; the recorded one is {recorded}"
        if self.mismatch is not None:
            raise httpx.ConnectError(self.mismatch)
        meta = json.loads((self._case_dir / f"{k:02}.response.json").read_text())
        body = (self._case_dir / f"{k:02}.response.body").read_bytes()
        return httpx.Response(meta["status"], headers=meta["headers"], content=body)


# Running a case


def reply_of(stop: object, finish_reason: str | None, meta: str | None) -> dict[str, object]:
    """One reply as `expected.json` holds it."""
    usage = None if meta is None else json.loads(meta).get("usage")
    return {
        "usage": usage,
        "stop": repr(stop).removeprefix("Stop."),
        "finish_reason": finish_reason,
    }


def _catalog(settings: Mapping[str, object]) -> ModelCatalog:
    return read_models(cli={"models": {ALIAS: dict(settings)}})


def _sample_error(session: Session) -> str | None:
    """What the first failed sample in the session said, if one failed."""
    for i in range(len(session.tree)):
        match session.tree.node(i).body:
            case Body.Custom(kind="sample_error", data=data):
                return str(data)
            case _:
                pass
    return None


def _extensions(case: Mapping[str, object], home: Path) -> list[object]:
    """The extensions a `turns` case loads: `token_calls` (with `tokens`, its counter) when
    `extensions` names it."""
    wanted: Mapping[str, object] = case.get("extensions") or {}  # type: ignore[assignment]
    if "token_calls" not in wanted:
        return []
    return [Tokens(home), TokenCalls()]


def _extension_options(case: Mapping[str, object]) -> dict[str, object]:
    wanted: Mapping[str, object] = case.get("extensions") or {}  # type: ignore[assignment]
    return {name: dict(options) for name, options in wanted.items()}  # type: ignore[call-overload]


def _turn_images(case: Mapping[str, object]) -> list[list[tau.ImageInput]]:
    """The images each turn of `case` sends, from `turn_images` (none for a case without it)."""
    turns: list[str] = case["turns"]  # type: ignore[assignment]
    wanted: list[list[Mapping[str, object]]] = case.get("turn_images") or [[] for _ in turns]  # type: ignore[assignment]
    return [
        [
            tau.ImageInput.of(Swatch(d["width"], d["height"], tuple(d["rgb"])).data())  # type: ignore[arg-type]
            for d in described
        ]
        for described in wanted
    ]


async def run_turns(
    provider: Provider, case: Mapping[str, object], directory: Path, *, sleep: bool
) -> list[dict[str, object]]:
    """Run a `turns` case over `provider` with its session in `directory`; the replies on the
    final walk. `Failed` if a sample fails or a turn does not end with a reply."""
    turns: list[str] = case["turns"]  # type: ignore[assignment]
    gaps: list[float] = case["gaps"]  # type: ignore[assignment]
    specs: list[Mapping[str, object]] = case["tools"]  # type: ignore[assignment]
    attached = _turn_images(case)
    clock = SystemClock()
    session = Session.create(model_config(ALIAS), str(case["system"]), clock.now(), directory)
    catalog = read_models(
        cli={"models": {ALIAS: dict(case["model"])}, "extensions": _extension_options(case)}  # type: ignore[call-overload]
    )
    driver = Driver(
        session,
        {provider.name: provider},
        catalog,
        tools=[TableTool(spec) for spec in specs],
        extensions=_extensions(case, directory.parent / "home"),
        clock=clock,
    )
    await driver.start()
    cursor = session.cursors()[0]
    try:
        for i, (text, gap, images) in enumerate(zip(turns, gaps, attached, strict=True), start=1):
            if sleep and gap > 0:
                print(f"turn {i}: sleeping {gap:g} s first", flush=True)
                await asyncio.sleep(gap)
            await driver.client("recorder").send(cursor, text, images)
            await driver.rest(cursor)
            error = _sample_error(session)
            if error is not None:
                raise Failed(f"turn {i}: the sample failed: {error}")
            walk = session.tree.walk(session.cursor(cursor).at)
            if not isinstance(walk[-1].body, Body.Assistant):
                raise Failed(f"turn {i} did not end with a reply but with {walk[-1].body!r}")
        return [
            reply_of(n.body.stop, n.body.finish_reason, n.body.meta)
            for n in session.tree.walk(session.cursor(cursor).at)
            if isinstance(n.body, Body.Assistant)
        ]
    finally:
        await driver.stop()
        session.close()


async def run_raw(provider: Provider, case: Mapping[str, object]) -> dict[str, object]:
    """Sample `provider` once over the walk of a `raw` case: `expected.json`'s content, its one
    reply or its error."""
    turns: list[str] = case["turns"]  # type: ignore[assignment]
    model = _catalog(case["model"]).models[ALIAS].config  # type: ignore[arg-type]
    session = Session.create(model_config(ALIAS), str(case["system"]), 0)
    try:
        at = session.cursor(session.cursors()[0]).at
        at = session.append(at, Body.User(turns[0]), tau.Author.Core(), 0)
        finishes = [
            item
            async for item in provider.sample(session.tree.walk(at), model, ())
            if isinstance(item, Finish)
        ]
    except SampleError as e:
        return {"replies": [], "error": {"status": e.status, "message": str(e)}}
    finally:
        session.close()
    return {"replies": [reply_of(f.stop, f.reason, f.meta) for f in finishes], "error": None}


async def replay(case_dir: Path) -> dict[str, object]:
    """Run a recorded case offline: what τ computes from the recorded responses, as
    `expected.json` holds it."""
    case = json.loads((case_dir / "case.json").read_text())
    if case.get("format") != FORMAT:
        raise Failed(f"{case_dir}: format {case.get('format')!r}, this reads {FORMAT}")
    raw = case["kind"] == "raw"
    if raw and not case.get("raw", {}).get("adapter", True):
        return {"replies": None, "error": None}
    transport = Replay(case_dir, raw=raw)
    name = case["model"].get("api_key_env")
    provider = PROVIDERS[case["provider"]](
        transport=transport, environ={} if name is None else {name: REPLAY_KEY}
    )
    try:
        if raw:
            return await run_raw(provider, case)
        with tempfile.TemporaryDirectory() as tmp:
            replies = await run_turns(provider, case, Path(tmp) / "s", sleep=False)
            return {"replies": replies, "error": None}
    except Failed as e:
        raise Failed(transport.mismatch or str(e)) from e
    finally:
        await provider.aclose()


async def record_turns(
    transport: Recording, case: Mapping[str, object], environ: Mapping[str, str]
) -> list[dict[str, object]]:
    provider = PROVIDERS[str(case["provider"])](transport=transport, environ=environ)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            return await run_turns(provider, case, Path(tmp) / "s", sleep=True)
    finally:
        await provider.aclose()


async def record_raw(transport: Recording, request: RawRequest, scenario: Scenario) -> None:
    """Send one hand-built request and read its whole response. `Failed` when the server
    refused a request that should pass, or passed one it should refuse, or refused it for
    another reason (a status outside `scenario.refusals`: no credit, no such model, a rate
    limit)."""
    expect_error = scenario.error
    async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(600)) as client:
        try:
            response = await client.request(
                request.method, request.url, headers=dict(request.headers), json=request.body
            )
        except httpx.HTTPError as e:
            raise Failed(f"the request failed: {e}") from e
    refused = response.status_code >= httpx.codes.BAD_REQUEST
    if refused and expect_error and response.status_code not in scenario.refusals:
        raise Failed(
            f"refused, but for another reason: {response.status_code}: {response.text[:2000]}"
        )
    if refused != expect_error:
        expected = "a refusal" if expect_error else "200"
        raise Failed(
            f"expected {expected}; the server answered {response.status_code}: "
            f"{response.text[:2000]}"
        )


# The files of a case


def _json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def exchange_files(
    exchanges: Sequence[Exchange], kept_bodies: Mapping[int, object] | None = None
) -> dict[str, bytes]:
    """The `NN.*` files of `exchanges`, redacted. `kept_bodies` replaces the body kept of a
    request by its index (a body too large to keep)."""
    files: dict[str, bytes] = {}
    for k, exchange in enumerate(exchanges):
        if exchange.status is None:
            raise Failed(f"request {k:02} ({exchange.method} {exchange.url}) got no response")
        encoding = exchange.response_headers.get("content-encoding", "identity")
        if encoding != "identity":
            raise Failed(f"response {k:02} is {encoding}-encoded though identity was asked for")
        if kept_bodies and k in kept_bodies:
            body = kept_bodies[k]
        else:
            try:
                body = json.loads(exchange.request_body) if exchange.request_body else None
            except ValueError as e:
                raise Failed(f"request {k:02} has a body that is not JSON: {e}") from e
        files[f"{k:02}.request.json"] = _json(
            {
                "method": exchange.method,
                "url": strip_query(exchange.url),
                "headers": kept_headers(exchange.request_headers, REQUEST_HEADERS),
                "body": body,
            }
        )
        files[f"{k:02}.response.json"] = _json(
            {
                "status": exchange.status,
                "headers": kept_headers(exchange.response_headers, RESPONSE_HEADERS),
            }
        )
        files[f"{k:02}.response.body"] = b"".join(exchange.chunks)
    return files


def write_case(dest: Path, files: Mapping[str, bytes]) -> None:
    """Write `files` as the directory `dest`, all or nothing: a sibling temporary directory,
    renamed into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".recording-", dir=dest.parent))
    try:
        for name, data in files.items():
            (tmp / name).write_bytes(data)
        tmp.chmod(0o755)
        tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _summary(files: Mapping[str, bytes], expected: Mapping[str, object]) -> str:
    requests = sum(name.endswith(".request.json") for name in files)
    replies: list[Mapping[str, object]] | None = expected["replies"]  # type: ignore[assignment]
    if expected.get("error") is not None:
        error: Mapping[str, object] = expected["error"]  # type: ignore[assignment]
        return f"{requests} exchanges, refused ({error['status']}) as expected"
    if replies is None:
        return f"{requests} exchanges (wire only)"
    with_usage = sum(r["usage"] is not None for r in replies)
    return f"{requests} exchanges, {len(replies)} replies ({with_usage} with usage)"


def _publishable(
    files: dict[str, bytes], secrets: Mapping[str, str], origin: str | None, family: str
) -> dict[str, bytes]:
    """`files` with a local `origin` replaced, once nothing in them holds a secret or looks
    like one; `Failed` naming the file otherwise."""
    if origin is not None:
        files = {
            n: d.replace(origin.encode(), public_base(family).encode()) for n, d in files.items()
        }
    for name, secret in secrets.items():
        leaked = find_key(files, secret)
        if leaked is not None:
            raise Failed(
                f"{leaked} would hold the value of {name}: nothing was written. The server sent "
                "the key back, so this fixture cannot be published as it is"
            )
    shaped = find_shape(files)
    if shaped is not None:
        raise Failed(f"{shaped} looks like a key or an auth header: nothing was written")
    return files


async def record_case(
    target: Target,
    scenario: Scenario,
    dest: Path,
    secrets: Mapping[str, str],
) -> Path:
    """Record `scenario` for `target` and write it at `dest`; the directory written."""
    if dest.exists():
        raise Usage(f"{dest} exists: a recorded case is not overwritten; remove it first")
    case = build_case(target, scenario)
    _catalog(case["model"])  # type: ignore[arg-type]  # a bad setting fails before any request
    environ = {} if target.key is None or target.key_env is None else {target.key_env: target.key}
    transport = Recording(httpx.AsyncHTTPTransport())
    kept: dict[int, object] = {}
    with tempfile.TemporaryDirectory() as scratch:
        try:
            if scenario.kind == "raw":
                assert scenario.build is not None
                request = scenario.build(target)
                if request.kept is not None:
                    kept[0] = request.kept
                await record_raw(transport, request, scenario)
                expected: dict[str, object] | None = None
            else:
                replies = await record_turns(transport, case, environ)
                expected = {"replies": replies, "error": None}
        finally:
            await transport.aclose()
        origin = private_origin(target.root, local=target.local)
        files = {"case.json": _json(case), **exchange_files(transport.exchanges, kept)}
        files = _publishable(files, secrets, origin, target.family)
        if expected is None:  # what τ reads in the recorded response
            staged = Path(scratch) / "case"
            staged.mkdir()
            for name, data in files.items():
                (staged / name).write_bytes(data)
            expected = await replay(staged)
    files["expected.json"] = _json(expected)
    files = _publishable(files, secrets, None, target.family)
    write_case(dest, files)
    print(f"{dest}: {_summary(files, expected)}", flush=True)
    return dest


def build_case(target: Target, scenario: Scenario) -> dict[str, object]:
    """The `case.json` of `scenario` for `target`; a `gapped` scenario ends with the gap turn
    when the family has a gap."""
    base_url = target.base_url
    settings: dict[str, object] = {"provider": target.provider, "model": target.model}
    if base_url is not None:
        settings["base_url"] = strip_query(base_url)
    if target.key_env is not None:
        settings["api_key_env"] = target.key_env
    cap = scenario.max_tokens or target.max_tokens
    options = dict(target.options) if target.options and not scenario.plain else {}
    if target.family == "openai":
        # OpenAI's reasoning models refuse `max_tokens`, which `OpenAIProvider` sends for the
        # model's `max_tokens` setting: the cap goes as `max_completion_tokens` instead.
        options["extra_body"] = {"max_completion_tokens": cap}
    else:
        settings["max_tokens"] = cap
    if scenario.images:
        settings["images"] = True
    if options:
        settings["options"] = options
    gapped = scenario.gapped and target.gap > 0
    turns = [*scenario.turns, *([GAP_TURN] if gapped else [])]
    gaps = [*(0 for _ in scenario.turns), *([target.gap] if gapped else [])]
    case: dict[str, object] = {
        "format": FORMAT,
        "family": target.family,
        "provider": target.provider,
        "kind": scenario.kind,
        "scenario": scenario.name,
        "model": settings,
        "system": scenario.system,
        "turns": turns,
        "gaps": gaps,
        "tools": [dict(t) for t in scenario.tools] if scenario.kind == "turns" else [],
    }
    if any(scenario.user_images):
        images = [[sw.described() for sw in turn] for turn in scenario.user_images]
        case["turn_images"] = [*images, *([] for _ in turns[len(images) :])]
    if scenario.extensions:
        case["extensions"] = {k: dict(v) for k, v in scenario.extensions.items()}
    if scenario.kind == "raw":
        case["raw"] = {
            "stream": scenario.stream,
            "adapter": scenario.adapter,
            "error": scenario.error,
        }
    case["tau"] = tau.__version__
    case["recorded"] = datetime.datetime.now(datetime.UTC).date().isoformat()
    return case


# The keys file and `all`


def read_keys(path: Path) -> dict[str, str]:
    """The `NAME=VALUE` lines of the keys file (`#` comments, blank lines and an `export `
    prefix allowed; a value may be quoted). `Usage` when the file cannot be read or a line is
    not of that shape. Nothing here goes to `os.environ`."""
    try:
        text = path.read_text()
    except OSError as e:
        raise Usage(f"the keys file {path} cannot be read: {e.strerror}") from e
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, eq, value = stripped.removeprefix("export ").partition("=")
        name = name.strip()
        if not eq or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise Usage(f"{path}:{number}: not NAME=VALUE (the value is not shown)")
        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        values[name] = value
    return values


@dataclass(frozen=True)
class Planned:
    """One family of `all`: its target, or why it is skipped."""

    family: str
    target: Target | None
    skipped: str | None = None
    model_source: str = "default"


def _int(text: str | None, name: str) -> int | None:
    if not text:
        return None
    try:
        return int(text)
    except ValueError as e:
        raise Usage(f"{name} is not a whole number") from e


async def _served(base_url: str, key: str | None) -> tuple[str | None, int | None]:
    """The first model a compatible server lists at `/models`, and its window (llama.cpp's
    `meta.n_ctx`, vLLM's `max_model_len`), or `(None, None)` when it lists none."""
    headers = {} if key is None else {"authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
        try:
            response = await client.get(base_url.rstrip("/") + "/models", headers=headers)
            listed = response.json()
        except (httpx.HTTPError, ValueError):
            return None, None
    data = listed.get("data") if isinstance(listed, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None, None
    first = data[0]
    meta = first.get("meta") if isinstance(first.get("meta"), dict) else {}
    window = meta.get("n_ctx") or first.get("max_model_len")
    return (first.get("id") if isinstance(first.get("id"), str) else None), (
        window if isinstance(window, int) else None
    )


async def plan_family(name: str, keys: Mapping[str, str], prefix: str) -> Planned:
    """The target of family `name` from the keys file, or why it is skipped."""
    family = family_of(name)
    key_env = family.key_env or (f"{prefix}_API_KEY" if name.startswith("oaicompat-") else None)
    key = keys.get(key_env, "") if key_env else ""
    if family.key_env and not key:
        return Planned(name, None, f"{key_env} is empty")
    base_url = family.base_url
    if family.needs_base_url:
        base_url = keys.get(f"{prefix}_BASE_URL", "")
        if not base_url:
            return Planned(name, None, f"{prefix}_BASE_URL is empty")
    model = keys.get(f"{prefix}_MODEL") or family.model
    window = _int(keys.get(f"{prefix}_WINDOW"), f"{prefix}_WINDOW") or family.window
    source = "keys file" if keys.get(f"{prefix}_MODEL") else "default"
    if model is None or (window is None and family.provider == "openai" and base_url):
        assert base_url is not None
        served, served_window = await _served(base_url, key or None)
        if model is None:
            if served is None:
                return Planned(name, None, f"{base_url}/models lists no model; set {prefix}_MODEL")
            model, source = served, "the server's /models"
        window = window or served_window
    target = Target(
        family=name,
        provider=family.provider,
        wire=WIRES[family.provider],
        model=model,
        base_url=base_url,
        key_env=key_env if key else None,
        key=key or None,
        options=family.options,
        max_tokens=family.max_tokens,
        window=window,
        local=family.local,
        gap=360 if name == "anthropic" else 0,
    )
    return Planned(name, target, model_source=source)


def family_names(keys: Mapping[str, str]) -> list[tuple[str, str]]:
    """Every family the keys file can set up, with its variables' prefix."""
    names = list(PREFIXES.items())
    for variable in sorted(keys):
        found = re.fullmatch(r"OAICOMPAT_([A-Z0-9_]+?)_BASE_URL", variable)
        if found:
            names.append(
                (
                    f"oaicompat-{found.group(1).lower().replace('_', '-')}",
                    f"OAICOMPAT_{found.group(1)}",
                )
            )
    return names


def scenarios_for(
    target: Target, wanted: Sequence[str] | None, rounds: int
) -> list[tuple[str, Scenario]]:
    """`(case name, scenario)` for every scenario `target` records: `turns` `rounds` times
    (`turns`, `turns-2`, ...)."""
    chosen = [s for s in SCENARIOS if applies(s, target.family, target.wire)]
    if wanted is not None:
        chosen = [s for s in chosen if s.name in wanted]
    out: list[tuple[str, Scenario]] = []
    for s in chosen:
        if s.name == "turns":
            out += [("turns" if r == 1 else f"turns-{r}", s) for r in range(1, rounds + 1)]
        else:
            out.append((s.name, s))
    return out


async def model_exists(target: Target) -> str:
    """Whether the provider's free model list has `target.model`: `ok`, or what it said."""
    match target.wire:
        case "anthropic":
            url = f"{target.root}/models/{target.model}"
            headers = _anthropic_headers(target)
        case "gemini":
            url = f"{target.root}/models/{target.model}"
            headers = _gemini_headers(target)
        case "openai":
            url = f"{target.root}/models"
            headers = _openai_headers(target)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            return f"could not ask: {type(e).__name__}"
    if response.status_code != httpx.codes.OK:
        return f"the model list answered {response.status_code}"
    if target.wire == "openai":
        try:
            data = response.json().get("data") or []
        except ValueError:
            return "the model list is not JSON"
        ids = {m.get("id") for m in data if isinstance(m, dict)}
        if target.model not in ids:
            return f"{target.model} is not in the {len(ids)} models listed"
    return "ok"


def _cost(target: Target, plan: Sequence[tuple[str, Scenario]]) -> tuple[int, int, int, str]:
    requests = sum(s.requests for _, s in plan)
    tokens_in = sum(s.tokens_in for _, s in plan if s.billed)
    tokens_out = sum(s.tokens_out for _, s in plan if s.billed)
    family = family_of(target.family)
    if family.local:
        return requests, tokens_in, tokens_out, "free (local)"
    price = family.price if target.model == family.model else None
    if price is None:
        return requests, tokens_in, tokens_out, "unknown (not the default model)"
    usd = (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000
    return (
        requests,
        tokens_in,
        tokens_out,
        f"about ${usd:.3f} at ${price[0]}/${price[1]} per M (assumed)",
    )


async def _print_plan(planned: Planned, plan: Sequence[tuple[str, Scenario]], out: Path) -> None:
    """`--dry-run`: one family's model, whether its provider lists it, and each case planned."""
    target = planned.target
    assert target is not None
    requests, tokens_in, tokens_out, cost = _cost(target, plan)
    listed = await model_exists(target)
    print(f"{target.family}: {target.model} ({planned.model_source}); model check: {listed}")
    for case_name, s in plan:
        notes = "" if s.billed else ", refused unbilled"
        if s.needs_window and target.window is None:
            notes += " SKIPPED: no window; set _WINDOW"
        if (out / target.family / case_name).exists():
            notes += " (recorded already)"
        estimate = f"~{s.requests} req, ~{s.tokens_in}+{s.tokens_out} tok"
        print(f"  {case_name:18} {s.kind:5} {estimate}{notes}")
    print(f"  total: ~{requests} requests, ~{tokens_in} in + ~{tokens_out} out tokens; cost {cost}")


async def _record_plan(
    target: Target, plan: Sequence[tuple[str, Scenario]], out: Path, secrets: Mapping[str, str]
) -> int:
    """Record each case of `plan` not recorded yet; how many failed (each is printed)."""
    failures = 0
    for case_name, s in plan:
        dest = out / target.family / case_name
        if dest.exists():
            print(f"{dest}: recorded already; skipped")
            continue
        if s.needs_window and target.window is None:
            prefix = PREFIXES.get(target.family, target.family.upper())
            print(f"{target.family}/{case_name}: skipped: no window known; set {prefix}_WINDOW")
            continue
        try:
            await record_case(target, s, dest, secrets)
        except (Failed, ConfigError) as e:
            failures += 1
            said = scrub(str(e), secrets)
            print(f"{target.family}/{case_name}: FAILED: {said}", file=sys.stderr, flush=True)
    return failures


async def run_all(args: argparse.Namespace) -> int:
    """`all`: plan every family, then record (or, with `--dry-run`, print the plan)."""
    path = Path(args.keys or os.environ.get("TAU_FIXTURE_KEYS") or KEYS_FILE).expanduser()
    keys = read_keys(path)
    secrets = {k: v for k, v in keys.items() if v and ("KEY" in k or "TOKEN" in k)}
    only = set(args.only.split(",")) if args.only else None
    wanted = args.scenarios.split(",") if args.scenarios else None
    unknown = [w for w in wanted or () if w not in BY_NAME]
    if unknown:
        raise Usage(f"no such scenario: {', '.join(unknown)} (--scenarios-list lists them)")
    failures = 0
    print(f"keys: {path}")
    for name, prefix in family_names(keys):
        if only is not None and name not in only:
            continue
        planned = await plan_family(name, keys, prefix)
        if planned.target is None:
            print(f"{name}: skipped: {planned.skipped}")
            continue
        plan = scenarios_for(planned.target, wanted, args.rounds)
        if args.dry_run:
            await _print_plan(planned, plan, Path(args.out))
        else:
            failures += await _record_plan(planned.target, plan, Path(args.out), secrets)
    return 1 if failures else 0


# The command line


def _option(text: str) -> tuple[str, object]:
    name, eq, value = text.partition("=")
    if not eq or not name:
        raise argparse.ArgumentTypeError(f"{text!r} is not KEY=JSON")
    try:
        return name, json.loads(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r}: the value is not JSON ({e})") from e


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n")[0],
        epilog="The fixture format is this file's docstring.",
    )
    p.add_argument(
        "family", nargs="?", metavar="FAMILY", help=f"all, or one of {', '.join(FAMILIES)}"
    )
    p.add_argument("--model", metavar="MODEL_ID", help="the model's id as the server knows it")
    p.add_argument("--base-url", metavar="URL", help="required for llamacpp and vllm")
    p.add_argument("--key-env", metavar="VAR", help="the environment variable holding the key")
    p.add_argument("--case", default="turns", choices=sorted(BY_NAME), help="the scenario")
    p.add_argument("--out", default=str(OUT), metavar="DIR", help=f"default: {OUT}")
    p.add_argument("--gap", type=float, default=0, metavar="SECONDS", help="one more turn after")
    p.add_argument("--max-tokens", type=int, default=1024, metavar="N")
    p.add_argument("--window", type=int, metavar="TOKENS", help="the window, for error-overflow")
    p.add_argument("--option", action="append", type=_option, default=[], metavar="KEY=JSON")
    p.add_argument("--regolden", metavar="CASE_DIR", help="replay a case, rewrite expected.json")
    p.add_argument("--keys", metavar="FILE", help=f"all: the keys file (default {KEYS_FILE})")
    p.add_argument("--only", metavar="FAMILY,...", help="all: these families only")
    p.add_argument("--scenarios", metavar="NAME,...", help="all: these scenarios only")
    p.add_argument("--rounds", type=int, default=3, metavar="N", help="all: recordings of turns")
    p.add_argument("--dry-run", action="store_true", help="print the plan; send nothing billed")
    p.add_argument("--scenarios-list", action="store_true", help="list the scenarios")
    return p


def single_target(args: argparse.Namespace) -> Target:
    """The target of the one-family command line. `Usage` when it does not say enough."""
    if args.family not in FAMILIES or args.model is None:
        raise Usage(
            "FAMILY (one of " + ", ".join(FAMILIES) + ") and --model are required "
            "(or all, or --regolden CASE_DIR)"
        )
    family = FAMILIES[args.family]
    scenario = BY_NAME[args.case]
    wire = WIRES[family.provider]
    if wire not in scenario.wires:
        only = ", ".join(sorted(scenario.wires))
        raise Usage(
            f"--case {args.case} is for the {only} wire only"
            + (
                " (Gemini's built-in google_search): gemini only"
                if args.case == "builtin-tool"
                else ""
            )
        )
    if args.gap < 0 or (scenario.kind == "raw" and args.gap):
        raise Usage("--gap is a positive number of seconds, and only for a turns scenario")
    base_url = args.base_url or family.base_url
    if family.needs_base_url and base_url is None:
        raise Usage(f"{args.family} needs --base-url URL (such as http://HOST:8100/v1)")
    key_env = args.key_env or family.key_env
    key = None
    if key_env is not None:
        key = os.environ.get(key_env)
        if not key:
            raise Usage(f"the environment variable {key_env} is not set: it holds the key")
    if scenario.needs_window and args.window is None:
        raise Usage(f"--case {args.case} needs --window TOKENS")
    return Target(
        family=args.family,
        provider=family.provider,
        wire=wire,
        model=args.model,
        base_url=strip_query(base_url) if base_url else None,
        key_env=key_env,
        key=key,
        options=dict(args.option),
        max_tokens=args.max_tokens,
        window=args.window,
        local=family.local,
        gap=args.gap,
    )


async def regolden(case_dir: Path) -> None:
    if not (case_dir / "case.json").is_file():
        raise Usage(f"{case_dir} holds no case.json: not a recorded case")
    expected = await replay(case_dir)
    (case_dir / "expected.json").write_bytes(_json(expected))
    replies = expected["replies"]
    print(f"{case_dir / 'expected.json'}: {'no' if replies is None else len(replies)} replies")  # type: ignore[arg-type]


def _scenarios_list() -> int:
    for s in SCENARIOS:
        wires = ", ".join(sorted(s.wires))
        local = "; local servers only" if s.local_only else ""
        print(f"{s.name:18} {s.kind:5} [{wires}{local}] {s.what}")
    return 0


def _single(args: argparse.Namespace, secrets: dict[str, str]) -> int:
    """One family, one scenario (the command line's FAMILY form)."""
    target = single_target(args)
    if target.key and target.key_env:
        secrets[target.key_env] = target.key
    scenario = BY_NAME[args.case]
    if target.gap > 0:  # --gap: this run ends with the gap turn
        scenario = replace(scenario, gapped=True)
    if args.dry_run:
        print(f"{target.family}/{args.case}: {scenario.what}")
        estimate = f"~{scenario.tokens_in}+{scenario.tokens_out} tokens"
        print(f"  ~{scenario.requests} requests, {estimate}")
        return 0
    dest = Path(args.out) / args.family / args.case
    asyncio.run(record_case(target, scenario, dest, secrets))
    return 0


def _run(args: argparse.Namespace, secrets: dict[str, str]) -> int:
    if args.scenarios_list:
        return _scenarios_list()
    if args.regolden is not None:
        if args.family is not None:
            raise Usage("--regolden takes a case directory and nothing else")
        asyncio.run(regolden(Path(args.regolden)))
        return 0
    if args.family == "all":
        return asyncio.run(run_all(args))
    return _single(args, secrets)


def main() -> int:
    p = parser()
    args = p.parse_args()
    secrets: dict[str, str] = {}
    try:
        return _run(args, secrets)
    except Usage as e:
        p.print_usage(sys.stderr)
        print(f"{p.prog}: {scrub(str(e), secrets)}", file=sys.stderr)
        return 2
    except (ConfigError, Failed) as e:
        print(f"{p.prog}: {scrub(str(e), secrets)}", file=sys.stderr)
        return 2 if isinstance(e, ConfigError) else 1


if __name__ == "__main__":
    sys.exit(main())
