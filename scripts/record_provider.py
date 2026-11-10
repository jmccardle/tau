#!/usr/bin/env python3
"""Record a provider fixture from a real server (D26 §10), redacted of keys.

    .venv/bin/python scripts/record_provider.py FAMILY --model MODEL_ID [--base-url URL]
        [--key-env VAR] [--case NAME] [--out DIR] [--gap SECONDS] [--max-tokens N]
        [--option KEY=JSON ...]
    .venv/bin/python scripts/record_provider.py --regolden CASE_DIR

(Run it with the Python that has `tau` installed; `./scripts/record_provider.py` works in an
activated venv.)

FAMILY is `anthropic`, `openai`, `gemini`, `llamacpp` or `vllm` (the last two use the `openai`
provider and need `--base-url`; a key is optional for them). The key is read from the
environment variable `--key-env` names (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY` by default), never from the command line, and is never written.

It drives a real `Driver` and session over the built-in provider, through a transport that
forwards to the network and keeps every exchange (any method, in the order sent), and writes
the case to `<out>/<family>/<case>/` (`--out` defaults to `tests/fixtures/providers`). Nothing
is written unless the whole run succeeded and no file holds the key. An existing case
directory is never overwritten.

Cases:

- `turns` (default): `SYSTEM`, the table tool `TOOL`, and the user turns `TURNS`, each run with
  auto-step until the cursor rests. `--gap SECONDS` sleeps that long and sends `GAP_TURN` as
  one more (D26: `--gap 360` on Anthropic is a cache miss).
- `builtin-tool` (gemini only): one hand-built `streamGenerateContent` request offering
  `google_search`, which `GeminiProvider` cannot offer. It is there for
  `toolUsePromptTokenCount` (D26 §2).

`--regolden CASE_DIR` replays a recorded case (no network, no key) and rewrites its
`expected.json`: for when a normalizer legitimately changes.

Exit status: 0 written, 1 the run failed or the fixture is not publishable, 2 usage.

# The fixture format, version 1

    <family>/<case>/
      case.json          how to run the case again
      NN.request.json    {"method", "url", "headers", "body"}
      NN.response.json   {"status", "headers"}
      NN.response.body   the response's bytes exactly as received (SSE or JSON)
      expected.json      {"replies": [{"usage", "stop", "finish_reason"}, ...]}

`NN` is the exchange's index, two digits from `00`, in the order the requests were sent.

`case.json`:

    {"format": 1, "family": ..., "provider": ..., "kind": "turns" | "raw",
     "model": {...}, "system": "...", "turns": ["...", ...], "gaps": [0, 0, 0, 360],
     "tools": [{"name", "description", "parameters", "answers": {...}, "key": "..."}],
     "tau": "<tau.__version__>", "recorded": "YYYY-MM-DD"}

- `model` is the settings object the model catalog was given (`provider`, `model`,
  `base_url`, `max_tokens`, `options`, `api_key_env`; the unset ones absent). `api_key_env` is
  the variable's name. A replay puts any text under that name.
- `kind` `turns`: a session with `system`, the `tools`, and each of `turns` sent in order with
  auto-step on, waiting for the cursor to rest before the next. `gaps[i]` is the seconds
  really slept before turn `i` at record time; a replay does not sleep.
- `kind` `raw`: the one request was built by hand, not by the provider (its body is in
  `00.request.json`). A replay samples the provider once over the walk `system`, `turns[0]`
  with no tools and reads the recorded response; the body the provider sends is not the
  recorded one.
- A **table tool** answers a call whose JSON arguments are exactly `{"<key>": K}` with
  `answers[K]`, and with the error `no such <key>: K` otherwise (K is the arguments' text
  when they are not that shape). The rule is the same at record and at replay, so a replay
  reproduces the walk.

Requests keep only the headers in `REQUEST_HEADERS` and responses those in `RESPONSE_HEADERS`;
every other header is dropped. URLs lose the query parameters in `SECRET_PARAMS`. `body` is the
request's JSON, or null when it had none. Requests are sent with `accept-encoding: identity`,
so a response body is never compressed.

`expected.json` is what τ computed at record time: one object per `Assistant` node on the
cursor's final walk, in order, with `usage` (its `meta`'s `usage`, or null), `stop` (`End`,
`Tools`, `Length`, ...) and `finish_reason` (the provider's own string, or null). For a `raw`
case it is the one reply of the replay.
"""

import argparse
import asyncio
import datetime
import json
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
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
    ModelCatalog,
    OpenAIProvider,
    Outcome,
    Provider,
    SampleError,
    Session,
    SystemClock,
    model_config,
    read_models,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "providers"
FORMAT = 1
ALIAS = "m"  # the catalog name of the one model
MIN_KEY = 8  # a shorter key is not searched for: it would match by chance
REPLAY_KEY = "replayed-no-key"

REQUEST_HEADERS = ("content-type", "anthropic-version", "accept")
RESPONSE_HEADERS = ("content-type",)
SECRET_PARAMS = ("key", "api_key", "access_token", "token")

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

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"  # the provider's own default
RAW_SYSTEM = "Answer in one short sentence."
RAW_TURN = "Search the web: in what year was the first transatlantic telegraph cable completed?"


@dataclass(frozen=True)
class Family:
    """One server family: its provider, and the defaults of `--key-env` and `--base-url`."""

    provider: str
    key_env: str | None = None  # None: a key is optional, and none is sent unless --key-env
    base_url: str | None = None
    needs_base_url: bool = False


FAMILIES = {
    "anthropic": Family("anthropic", "ANTHROPIC_API_KEY"),
    "openai": Family("openai", "OPENAI_API_KEY", "https://api.openai.com/v1"),
    "gemini": Family("gemini", "GEMINI_API_KEY"),
    "llamacpp": Family("openai", needs_base_url=True),
    "vllm": Family("openai", needs_base_url=True),
}
PROVIDERS = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "gemini": GeminiProvider}


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


# The table tool


class TableTool:
    """A tool that answers from a table (see the format). Built from a `case.json` tool."""

    def __init__(self, spec: Mapping[str, object]) -> None:
        self.name = str(spec["name"])
        self.description = str(spec["description"])
        self.parameters: Mapping[str, object] = spec["parameters"]  # type: ignore[assignment]
        self._answers: Mapping[str, str] = spec["answers"]  # type: ignore[assignment]
        self._key = str(spec["key"])

    async def call(self, args_raw: str) -> Outcome:
        try:
            args = json.loads(args_raw)
        except ValueError:
            args = None
        if isinstance(args, dict) and list(args) == [self._key]:
            wanted = args[self._key]
            if isinstance(wanted, str) and wanted in self._answers:
                return Outcome.Ok(self._answers[wanted])
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
    which, and the request fails."""

    def __init__(self, case_dir: Path) -> None:
        self._requests = [
            json.loads(p.read_text()) for p in sorted(case_dir.glob("*.request.json"))
        ]
        self._case_dir = case_dir
        self._seen = 0
        self.mismatch: str | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        k, self._seen = self._seen, self._seen + 1
        got = f"{request.method} {strip_query(str(request.url))}"
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


async def run_turns(
    provider: Provider, case: Mapping[str, object], directory: Path, *, sleep: bool
) -> list[dict[str, object]]:
    """Run a `turns` case over `provider` with its session in `directory`; the replies on the
    final walk. `Failed` if a sample fails or a turn does not end with a reply."""
    turns: list[str] = case["turns"]  # type: ignore[assignment]
    gaps: list[float] = case["gaps"]  # type: ignore[assignment]
    specs: list[Mapping[str, object]] = case["tools"]  # type: ignore[assignment]
    clock = SystemClock()
    session = Session.create(model_config(ALIAS), str(case["system"]), clock.now(), directory)
    driver = Driver(
        session,
        {provider.name: provider},
        _catalog(case["model"]),  # type: ignore[arg-type]
        tools=[TableTool(spec) for spec in specs],
        clock=clock,
    )
    driver.start()
    cursor = session.cursors()[0]
    try:
        for i, (text, gap) in enumerate(zip(turns, gaps, strict=True), start=1):
            if sleep and gap > 0:
                print(f"turn {i}: sleeping {gap:g} s first", flush=True)
                await asyncio.sleep(gap)
            await driver.client("recorder").send(cursor, text)
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


async def run_raw(provider: Provider, case: Mapping[str, object]) -> list[dict[str, object]]:
    """Sample `provider` once over the walk of a `raw` case; its one reply."""
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
        raise Failed(f"the sample failed: {e}") from e
    finally:
        session.close()
    return [reply_of(f.stop, f.reason, f.meta) for f in finishes]


async def replay(case_dir: Path) -> list[dict[str, object]]:
    """Run a recorded case offline; the replies τ computes from the recorded responses."""
    case = json.loads((case_dir / "case.json").read_text())
    if case.get("format") != FORMAT:
        raise Failed(f"{case_dir}: format {case.get('format')!r}, this reads {FORMAT}")
    transport = Replay(case_dir)
    name = case["model"].get("api_key_env")
    provider = PROVIDERS[case["provider"]](
        transport=transport, environ={} if name is None else {name: REPLAY_KEY}
    )
    try:
        if case["kind"] == "raw":
            return await run_raw(provider, case)
        with tempfile.TemporaryDirectory() as tmp:
            return await run_turns(provider, case, Path(tmp) / "s", sleep=False)
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


async def record_raw(transport: Recording, case: Mapping[str, object], key: str) -> None:
    """Send the one hand-built request of `builtin-tool` and read its whole response."""
    model: Mapping[str, object] = case["model"]  # type: ignore[assignment]
    turns: list[str] = case["turns"]  # type: ignore[assignment]
    base = str(model.get("base_url") or GEMINI_URL).rstrip("/")
    body = {
        "systemInstruction": {"parts": [{"text": case["system"]}]},
        "contents": [{"role": "user", "parts": [{"text": turns[0]}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": model["max_tokens"]},
    }
    async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(300)) as client:
        try:
            response = await client.post(
                f"{base}/models/{model['model']}:streamGenerateContent?alt=sse",
                headers={"x-goog-api-key": key},
                json=body,
            )
        except httpx.HTTPError as e:
            raise Failed(f"the request failed: {e}") from e
    if response.status_code != httpx.codes.OK:
        raise Failed(f"the server answered {response.status_code}: {response.text[:2000]}")


# The files of a case


def _json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def exchange_files(exchanges: Sequence[Exchange]) -> dict[str, bytes]:
    """The `NN.*` files of `exchanges`, redacted."""
    files: dict[str, bytes] = {}
    for k, exchange in enumerate(exchanges):
        if exchange.status is None:
            raise Failed(f"request {k:02} ({exchange.method} {exchange.url}) got no response")
        encoding = exchange.response_headers.get("content-encoding", "identity")
        if encoding != "identity":
            raise Failed(f"response {k:02} is {encoding}-encoded though identity was asked for")
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


def _summary(files: Mapping[str, bytes], replies: Sequence[Mapping[str, object]]) -> str:
    requests = sum(name.endswith(".request.json") for name in files)
    with_usage = sum(r["usage"] is not None for r in replies)
    return f"{requests} exchanges, {len(replies)} replies ({with_usage} with usage)"


async def record(args: argparse.Namespace, case: dict[str, object], key: str | None) -> Path:
    """Record `case` and write it; the directory written."""
    dest = Path(args.out) / args.family / args.case
    if dest.exists():
        raise Usage(f"{dest} exists: a recorded case is not overwritten; remove it first")
    environ = {} if key is None else {str(args.key_env): key}
    transport = Recording(httpx.AsyncHTTPTransport())
    with tempfile.TemporaryDirectory() as scratch:
        try:
            if case["kind"] == "raw":
                assert key is not None
                await record_raw(transport, case, key)
                replies = None
            else:
                replies = await record_turns(transport, case, environ)
        finally:
            await transport.aclose()
        files = {"case.json": _json(case), **exchange_files(transport.exchanges)}
        if replies is None:  # what τ reads in the recorded response
            staged = Path(scratch) / "case"
            staged.mkdir()
            for name, data in files.items():
                (staged / name).write_bytes(data)
            replies = await replay(staged)
    files["expected.json"] = _json({"replies": replies})
    leaked = find_key(files, key)
    if leaked is not None:
        raise Failed(
            f"{leaked} would hold the value of {args.key_env}: nothing was written. "
            "The server sent the key back, so this fixture cannot be published as it is"
        )
    write_case(dest, files)
    print(f"{dest}: {_summary(files, replies)}")
    return dest


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
    p.add_argument("family", nargs="?", choices=sorted(FAMILIES), metavar="FAMILY")
    p.add_argument("--model", metavar="MODEL_ID", help="the model's id as the server knows it")
    p.add_argument("--base-url", metavar="URL", help="required for llamacpp and vllm")
    p.add_argument("--key-env", metavar="VAR", help="the environment variable holding the key")
    p.add_argument("--case", default="turns", choices=("turns", "builtin-tool"))
    p.add_argument("--out", default=str(OUT), metavar="DIR", help=f"default: {OUT}")
    p.add_argument("--gap", type=float, default=0, metavar="SECONDS", help="one more turn after")
    p.add_argument("--max-tokens", type=int, default=1024, metavar="N")
    p.add_argument("--option", action="append", type=_option, default=[], metavar="KEY=JSON")
    p.add_argument("--regolden", metavar="CASE_DIR", help="replay a case, rewrite expected.json")
    return p


def build_case(args: argparse.Namespace) -> tuple[dict[str, object], str | None]:
    """The `case.json` of the run `args` asks for, and the key's value (None: no key).
    `Usage` when the arguments do not say enough."""
    if args.family is None or args.model is None:
        raise Usage("FAMILY and --model are required (or --regolden CASE_DIR)")
    family = FAMILIES[args.family]
    raw = args.case == "builtin-tool"
    if raw and args.family != "gemini":
        raise Usage("--case builtin-tool is Gemini's built-in google_search: gemini only")
    if args.gap < 0 or (raw and args.gap):
        raise Usage("--gap is a positive number of seconds, and only for --case turns")
    base_url = args.base_url or family.base_url
    if family.needs_base_url and base_url is None:
        raise Usage(f"{args.family} needs --base-url URL (such as http://HOST:8100/v1)")
    args.key_env = args.key_env or family.key_env
    key = None
    if args.key_env is not None:
        key = os.environ.get(args.key_env)
        if not key:
            raise Usage(f"the environment variable {args.key_env} is not set: it holds the key")
    settings: dict[str, object] = {"provider": family.provider, "model": args.model}
    if base_url is not None:
        settings["base_url"] = strip_query(base_url)
    if args.key_env is not None:
        settings["api_key_env"] = args.key_env
    settings["max_tokens"] = args.max_tokens
    if args.option:
        settings["options"] = dict(args.option)
    gap_turns = [GAP_TURN] if args.gap > 0 else []
    turns = [RAW_TURN] if raw else [*TURNS, *gap_turns]
    case: dict[str, object] = {
        "format": FORMAT,
        "family": args.family,
        "provider": family.provider,
        "kind": "raw" if raw else "turns",
        "model": settings,
        "system": RAW_SYSTEM if raw else SYSTEM,
        "turns": turns,
        "gaps": [0] if raw else [*(0 for _ in TURNS), *([args.gap] if gap_turns else [])],
        "tools": [] if raw else [TOOL],
        "tau": tau.__version__,
        "recorded": datetime.datetime.now(datetime.UTC).date().isoformat(),
    }
    return case, key


async def regolden(case_dir: Path) -> None:
    if not (case_dir / "case.json").is_file():
        raise Usage(f"{case_dir} holds no case.json: not a recorded case")
    replies = await replay(case_dir)
    (case_dir / "expected.json").write_bytes(_json({"replies": replies}))
    print(f"{case_dir / 'expected.json'}: {len(replies)} replies")


def main() -> int:
    p = parser()
    args = p.parse_args()
    try:
        if args.regolden is not None:
            if args.family is not None:
                raise Usage("--regolden takes a case directory and nothing else")
            asyncio.run(regolden(Path(args.regolden)))
        else:
            case, key = build_case(args)
            _catalog(case["model"])  # type: ignore[arg-type]  # a bad setting fails before any request
            asyncio.run(record(args, case, key))
    except Usage as e:
        p.print_usage(sys.stderr)
        print(f"{p.prog}: {e}", file=sys.stderr)
        return 2
    except ConfigError as e:
        print(f"{p.prog}: {e}", file=sys.stderr)
        return 2
    except Failed as e:
        print(f"{p.prog}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
