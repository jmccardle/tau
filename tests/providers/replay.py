"""Replaying recorded provider fixtures (D26 §10): the cases under `tests/fixtures/providers`,
which `scripts/record_provider.py` wrote from real servers. That script's docstring is the
format's spec; this reads format 1. Nothing here touches the network or needs a key.

    for case_dir in cases("anthropic"):
        async with replayed(case_dir, tmp_path) as run:
            run.walk, run.metas, run.sent, run.session, run.driver, run.cursor, run.case

A family with no recorded case is skipped loudly by its tests, with `RECORD[family]`: the
command the repository's owner runs to record it."""

import base64
import json
import struct
import tempfile
import zlib
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from tau import (
    AnthropicProvider,
    Author,
    Body,
    Cursor,
    Driver,
    Finish,
    GeminiProvider,
    Image,
    ImageInput,
    Node,
    OpenAIProvider,
    Outcome,
    Provider,
    SampleError,
    Session,
    Span,
    Stop,
    SystemClock,
    TokenCalls,
    Tokens,
    model_config,
    read_models,
)

FAMILIES = ("anthropic", "openai", "gemini", "openrouter", "mistral", "llamacpp", "vllm")
"""The families the recorder knows by name; `oaicompat-<name>` families are found on disk
(`recorded_families`)."""
ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
FORMAT = 1
ALIAS = "m"  # the catalog name of a case's one model
DUMMY_KEY = "replayed-no-key"
SECRET_PARAMS = ("key", "api_key", "access_token", "token")

_SCRIPT = ".venv/bin/python scripts/record_provider.py"
_NEEDS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "llamacpp": "LLAMACPP_BASE_URL",
    "vllm": "VLLM_BASE_URL",
}
RECORD: dict[str, str] = {
    family: f"{_SCRIPT} all --only {family}  # with {variable} set in the keys file"
    for family, variable in _NEEDS.items()
}
"""The command that records each family's fixtures: the recorder's `all` over the keys file
(`~/.config/tau-fixtures/keys.env`; its docstring says what the file holds)."""

_PROVIDERS = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "gemini": GeminiProvider}


def cases(family: str, root: Path = ROOT) -> list[Path]:
    """The recorded case directories of `family` under `root`, sorted; empty if none."""
    return sorted(p.parent for p in (root / family).glob("*/case.json"))


def recorded_families(root: Path = ROOT) -> list[str]:
    """Every family with a recorded case under `root`, the named ones and `oaicompat-*`."""
    return sorted({p.parent.parent.name for p in root.glob("*/*/case.json")})


def all_cases(root: Path = ROOT) -> list[Path]:
    """Every recorded case directory under `root`, sorted."""
    return sorted(p.parent for p in root.glob("*/*/case.json"))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def strip_query(url: str) -> str:
    """`url` without the query parameters a key travels in, as the recorder wrote it."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in SECRET_PARAMS]
    if len(kept) == len(pairs):
        return url
    return urlunsplit(parts._replace(query=urlencode(kept)))


class ReplayMismatch(AssertionError):
    """The provider asked for something other than what was recorded."""


class ReplayTransport(httpx.AsyncBaseTransport):
    """Answers the k-th request with the k-th recorded response of a case. A request whose
    method or URL (less its secret query parameters) is not the recorded one, or one more
    than were recorded, raises `ReplayMismatch` and is kept in `mismatch`, since a provider
    may turn what its transport raises into a failed sample. `sent` is each request's JSON
    body (None when it had none); `recorded` is each recorded request's."""

    def __init__(self, case_dir: Path, *, raw: bool = False) -> None:
        self._case_dir = case_dir
        self._requests: list[dict[str, Any]] = [
            read_json(p) for p in sorted(case_dir.glob("*.request.json"))
        ]
        self.recorded: list[object] = [r["body"] for r in self._requests]
        self.sent: list[object] = []
        self.mismatch: str | None = None
        self._raw = raw

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        k = len(self.sent)
        got = f"{request.method} {strip_query(str(request.url))}"
        if self._raw and request.method == "GET":  # a probe a raw case never recorded
            at = self._requests[k] if k < len(self._requests) else None
            if at is None or f"{at['method']} {at['url']}" != got:
                return httpx.Response(404, json={"error": "not recorded"})
        content = await request.aread()
        self.sent.append(json.loads(content) if content else None)
        if k >= len(self._requests):
            self.mismatch = f"request {k:02} ({got}): only {len(self._requests)} were recorded"
        else:
            recorded = f"{self._requests[k]['method']} {self._requests[k]['url']}"
            if got != recorded:
                self.mismatch = f"request {k:02} is {got}; the recorded one is {recorded}"
        if self.mismatch is not None:
            raise ReplayMismatch(f"{self._case_dir}: {self.mismatch}")
        meta = read_json(self._case_dir / f"{k:02}.response.json")
        body = (self._case_dir / f"{k:02}.response.body").read_bytes()
        return httpx.Response(meta["status"], headers=meta["headers"], content=body)


class TableTool:
    """A fixture's table tool: a call whose arguments are `{"<key>": K}` is answered with
    `answers[K]`, any other with the error `no such <key>: K`. The recorder's rule."""

    def __init__(self, spec: Mapping[str, Any]) -> None:
        self.name: str = spec["name"]
        self.description: str = spec["description"]
        self.parameters: Mapping[str, object] = spec["parameters"]
        self._answers: Mapping[str, str] = spec["answers"]
        self._images: Mapping[str, str] = spec.get("images", {})
        self._key: str = spec["key"]

    async def call(self, args_raw: str) -> Outcome:
        try:
            args = json.loads(args_raw)
        except ValueError:
            args = None
        if isinstance(args, dict) and list(cast("dict[str, object]", args)) == [self._key]:
            wanted = cast("dict[str, object]", args)[self._key]
            if isinstance(wanted, str) and wanted in self._answers:
                image = self._images.get(wanted)
                images = [] if image is None else [Image(base64.b64decode(image))]
                return Outcome.Ok(self._answers[wanted], images)
            return Outcome.Err(f"no such {self._key}: {wanted}")
        return Outcome.Err(f"no such {self._key}: {args_raw}")


def flat_png(width: int, height: int, rgb: Sequence[int]) -> bytes:
    """The recorder's `png()`: a PNG of one colour, the same bytes every time, so a replay's
    request is the recorded one."""

    def block(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    row = b"\x00" + bytes(rgb) * width
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + block(b"IHDR", header)
        + block(b"IDAT", zlib.compress(row * height, 9))
        + block(b"IEND", b"")
    )


def turn_images(case: Mapping[str, Any]) -> list[list[ImageInput]]:
    """The images each turn of `case` sends (`turn_images`; none for a case without it)."""
    wanted: list[list[Mapping[str, Any]]] = case.get("turn_images") or [[] for _ in case["turns"]]
    return [
        [ImageInput.of(flat_png(d["width"], d["height"], d["rgb"])) for d in turn]
        for turn in wanted
    ]


def reply_of(stop: Stop, finish_reason: str | None, meta: str | None) -> dict[str, object]:
    """One reply as `expected.json` holds it: `usage`, `stop`, `finish_reason`."""
    usage = None if meta is None else json.loads(meta).get("usage")
    return {
        "usage": usage,
        "stop": repr(stop).removeprefix("Stop."),
        "finish_reason": finish_reason,
    }


class _Spans:
    """An extension whose `/spans` keeps the proved spans of the cursor's walk."""

    name = "replay_spans"

    def __init__(self) -> None:
        self.found: list[Span] = []
        self.commands = (self.spans,)

    def spans(self, cursor: Cursor) -> None:
        """Keep the proved spans of this walk."""
        self.found = cursor.spans()


@dataclass
class Run:
    """A replayed case, at rest.

    `case` is its `case.json` and `case_dir` where it is; `walk` the nodes on the cursor's
    final walk; `replies` each reply as `expected.json` holds it and `metas` each reply's
    parsed `meta` (None if it had none), in walk order; `sent` and `recorded` the JSON body of
    each request the provider sent and of each recorded one (None for a request without one).

    A `raw` case was not recorded through the provider: it is one `sample` over a
    one-user-message walk in `session`, its replies are the `Finish`es, and it has no driver.
    """

    case_dir: Path
    case: dict[str, Any]
    session: Session
    cursor: int
    walk: list[Node]
    replies: list[dict[str, object]]
    metas: list[dict[str, Any] | None]
    sent: list[object]
    recorded: list[object]
    started: Driver | None = None
    spans: tuple[Span, ...] = ()
    """The proved spans of the final walk (`TreeHandle.spans`); a raw case has none."""
    error: dict[str, object] | None = None
    """A raw case whose sample failed: the `SampleError`'s `status` and `message`."""

    @property
    def driver(self) -> Driver:
        if self.started is None:
            raise ValueError("a raw case has no driver")
        return self.started

    @property
    def expected(self) -> list[dict[str, object]]:
        """The `replies` of the case's `expected.json`."""
        return read_json(self.case_dir / "expected.json")["replies"]

    @property
    def expected_error(self) -> dict[str, object] | None:
        """The `error` of the case's `expected.json` (absent in older cases: none)."""
        return read_json(self.case_dir / "expected.json").get("error")


def _metas(metas: list[str | None]) -> list[dict[str, Any] | None]:
    return [None if m is None else json.loads(m) for m in metas]


def _failure(session: Session) -> str | None:
    """What the first failed sample in the session said, if one failed."""
    for i in range(len(session.tree)):
        match session.tree.node(i).body:
            case Body.Custom(kind="sample_error", data=data):
                return str(data)
            case _:
                pass
    return None


@asynccontextmanager
async def _turns(  # noqa: PLR0913, PLR0917 - a case, and what runs it
    case_dir: Path,
    case: dict[str, Any],
    provider: Provider,
    transport: ReplayTransport,
    at: Path,
    more: Sequence[object] = (),
) -> AsyncIterator[Run]:
    wanted: dict[str, Any] = case.get("extensions") or {}
    catalog = read_models(cli={"models": {ALIAS: case["model"]}, "extensions": wanted})
    clock = SystemClock()
    session = Session.create(model_config(ALIAS), case["system"], clock.now(), at)
    tools = [TableTool(spec) for spec in case["tools"]]
    probe = _Spans()
    counting: list[object] = []
    if "token_calls" in wanted:  # the counter it needs, as the recorder loaded it
        counting = [Tokens(at.parent / "home"), TokenCalls()]
    driver = Driver(
        session,
        {provider.name: provider},
        catalog,
        tools=tools,
        extensions=[*counting, probe, *more],  # pyright: ignore[reportArgumentType]
        clock=clock,
    )
    await driver.start()
    cursor = session.cursors()[0]
    try:
        for text, images in zip(case["turns"], turn_images(case), strict=True):
            await driver.client("replay").send(cursor, text, images)
            await driver.rest(cursor)
            failure = _failure(session)
            if failure is not None:
                raise AssertionError(f"{case_dir}: a replayed sample failed: {failure}")
        walk = session.tree.walk(session.cursor(cursor).at)
        replies = [n.body for n in walk if isinstance(n.body, Body.Assistant)]
        await driver.client("replay").run(cursor, "spans", {})
        yield Run(
            case_dir,
            case,
            session,
            cursor,
            walk,
            [reply_of(r.stop, r.finish_reason, r.meta) for r in replies],
            _metas([r.meta for r in replies]),
            transport.sent,
            transport.recorded,
            driver,
            tuple(probe.found),
        )
    finally:
        await driver.stop()
        session.close()


@asynccontextmanager
async def _raw(
    case_dir: Path, case: dict[str, Any], provider: Provider, transport: ReplayTransport
) -> AsyncIterator[Run]:
    model = read_models(cli={"models": {ALIAS: case["model"]}}).models[ALIAS].config
    session = Session.create(model_config(ALIAS), case["system"], 0)
    try:
        cursor = session.cursors()[0]
        user = Body.User(case["turns"][0])
        walk = session.tree.walk(session.append(session.cursor(cursor).at, user, Author.Core(), 0))
        error = None
        try:
            finishes = [i async for i in provider.sample(walk, model, ()) if isinstance(i, Finish)]
        except SampleError as e:
            finishes, error = [], {"status": e.status, "message": str(e)}
        yield Run(
            case_dir,
            case,
            session,
            cursor,
            walk,
            [reply_of(f.stop, f.reason, f.meta) for f in finishes],
            _metas([f.meta for f in finishes]),
            transport.sent,
            transport.recorded,
            error=error,
        )
    finally:
        session.close()


@asynccontextmanager
async def replayed(
    case_dir: Path, tmp_path: Path, extensions: Sequence[object] = ()
) -> AsyncIterator[Run]:
    """Run the recorded case in `case_dir` again, offline, and yield it at rest: the provider
    over a `ReplayTransport`, any text as the key, the case's table tools, a session under
    `tmp_path` and a started driver, each scripted turn sent with auto-step on and waited for.
    `ReplayMismatch` if the provider's requests are not the recorded ones (method and URL;
    compare `run.sent` with `run.recorded` for the bodies). `extensions` are loaded beside the
    case's own (a turns case only)."""
    case: dict[str, Any] = read_json(case_dir / "case.json")
    assert case["format"] == FORMAT, f"{case_dir}: format {case['format']}, this reads {FORMAT}"
    transport = ReplayTransport(case_dir, raw=case["kind"] == "raw")
    key_env = case["model"].get("api_key_env")
    provider = _PROVIDERS[case["provider"]](
        transport=transport, environ={} if key_env is None else {key_env: DUMMY_KEY}
    )
    if case["kind"] == "raw":
        running = _raw(case_dir, case, provider, transport)
    else:
        directory = Path(tempfile.mkdtemp(prefix="replay-", dir=tmp_path)) / "s"
        running = _turns(case_dir, case, provider, transport, directory, extensions)
    at_rest = False
    try:
        async with running as run:
            at_rest = True
            yield run
    except ReplayMismatch:
        raise
    except Exception as e:
        if not at_rest and transport.mismatch is not None:  # however the provider reported it
            raise ReplayMismatch(f"{case_dir}: {transport.mismatch}") from e
        raise
    finally:
        await provider.aclose()
