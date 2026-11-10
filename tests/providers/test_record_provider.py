"""`scripts/record_provider.py`, run as a program against a scripted HTTP server on 127.0.0.1:
what it writes, what it keeps out of what it writes, when it refuses, and that `replay` reads
a written case back to the same replies and requests. The streams here are written by hand in
llama.cpp's shape; they are test data, not fixtures."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from harness import scenario
from replay import FAMILIES, RECORD, ReplayMismatch, cases, replayed
from test_provider_fixtures import spans_fit_their_ceilings
from wire import DONE_LINE, chunk, gemini_chunk

from tau import Body, Outcome

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "record_provider.py"
KEY_ENV = "TAU_RECORDER_TEST_KEY"
KEY = "sk-test-RECORDER-0123456789"
MODEL = "qwen-test"


def usage(prompt: int, completion: int) -> str:
    counts = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    return f"data: {json.dumps({'choices': [], 'usage': counts})}\n\n"


def calls(*parts: str, prompt: int) -> str:
    """A reply that calls `lookup_part` once per part."""
    deltas = [
        {
            "index": i,
            "id": f"call_{part}",
            "type": "function",
            "function": {"name": "lookup_part", "arguments": json.dumps({"part": part})},
        }
        for i, part in enumerate(parts)
    ]
    return chunk({"tool_calls": deltas}) + chunk({}, "tool_calls") + usage(prompt, 9) + DONE_LINE


def says(text: str, prompt: int) -> str:
    return chunk({"content": text}) + chunk({}, "stop") + usage(prompt, 5) + DONE_LINE


# Three turns: one lookup, two lookups, one lookup of a part that is not in the table.
STREAMS = (
    calls("A-113", prompt=100),
    says("Bin 14.", prompt=130),
    calls("B-207", "C-042", prompt=150),
    says("B-207 has more.", prompt=200),
    calls("Z-999", prompt=220),
    says("There is no such part.", prompt=240),
)


class Scripted:
    """A real HTTP server on 127.0.0.1: every GET is a 404 (it is not llama.cpp: no `/props`,
    no `/v1/models`), and the k-th POST is answered with the k-th of `streams` as SSE, or
    with `status` and a JSON error when that is not 200. `requests` is each request's method,
    path and headers. With `echo`, each stream begins with a comment holding the request's
    `authorization`."""

    def __init__(self, streams: Sequence[str], *, status: int = 200, echo: bool = False) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self._streams = list(streams)
        self._status = status
        self._echo = echo

    def _answer(self, handler: BaseHTTPRequestHandler, status: int, kind: str, body: bytes) -> None:
        handler.send_response(status)
        handler.send_header("content-type", kind)
        handler.send_header("content-length", str(len(body)))
        handler.send_header("set-cookie", "session=not-for-the-fixture")
        handler.end_headers()
        handler.wfile.write(body)

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        headers = {k.lower(): v for k, v in handler.headers.items()}
        handler.rfile.read(int(headers.get("content-length", "0")))
        self.requests.append((handler.command, handler.path, headers))
        if handler.command != "POST":
            self._answer(handler, 404, "application/json", b'{"error": "not found"}')
        elif self._status != 200:
            self._answer(handler, self._status, "application/json", b'{"error": "overloaded"}')
        else:
            body = self._streams.pop(0)
            if self._echo:
                body = f": you sent {headers['authorization']}\n\n" + body
            self._answer(handler, 200, "text/event-stream", body.encode())

    @contextmanager
    def running(self) -> Iterator[str]:
        """The server's root URL, while it runs."""
        handle = self._handle

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                handle(self)

            def do_POST(self) -> None:
                handle(self)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def script(*args: object, key: str | None = KEY) -> subprocess.CompletedProcess[str]:
    """Run the recorder with `args`; `key` is the value of `KEY_ENV` (None: unset)."""
    env = {k: v for k, v in os.environ.items() if k != KEY_ENV}
    if key is not None:
        env[KEY_ENV] = key
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def record(server: Scripted, out: Path, *more: object) -> subprocess.CompletedProcess[str]:
    """Record the `turns` case of family `llamacpp` from `server` under `out`."""
    with server.running() as root:
        return script(
            "llamacpp", "--model", MODEL, "--base-url", f"{root}/v1", "--key-env", KEY_ENV,
            "--out", out, *more,
        )  # fmt: skip


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def everything_under(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file()]


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Scripted]:
    """One recorded `turns` case, and the server it was recorded from. Tests read it; one
    that changes a case copies it first."""
    out = tmp_path_factory.mktemp("recorded")
    server = Scripted(STREAMS)
    done = record(server, out, "--option", "temperature=0.25")
    assert done.returncode == 0, done.stderr
    return out / "llamacpp" / "turns", server


def test_a_turns_run_writes_the_case_in_the_fixture_format(
    recorded: tuple[Path, Scripted],
) -> None:
    case_dir, server = recorded
    posts = [k for k, (method, _, _) in enumerate(server.requests) if method == "POST"]
    assert len(posts) == len(STREAMS)
    names = {"case.json", "expected.json"}
    for k in range(len(server.requests)):
        names |= {f"{k:02}.request.json", f"{k:02}.response.json", f"{k:02}.response.body"}
    assert {p.name for p in case_dir.iterdir()} == names

    case = read(case_dir / "case.json")
    assert case["format"] == 1
    assert (case["family"], case["provider"], case["kind"]) == ("llamacpp", "openai", "turns")
    assert case["model"] == {
        "provider": "openai",
        "model": MODEL,
        "base_url": case["model"]["base_url"],
        "api_key_env": KEY_ENV,
        "max_tokens": 1024,
        "options": {"temperature": 0.25},
    }
    assert case["model"]["base_url"].startswith("http://127.0.0.1:")
    assert len(case["turns"]) == 3
    assert case["gaps"] == [0, 0, 0]
    assert [t["name"] for t in case["tools"]] == ["lookup_part"]
    assert case["tools"][0]["key"] == "part"
    assert set(case["tools"][0]) == {"name", "description", "parameters", "answers", "key"}

    for k, (method, path, _) in enumerate(server.requests):
        request = read(case_dir / f"{k:02}.request.json")
        assert set(request) == {"method", "url", "headers", "body"}
        assert request["method"] == method
        assert request["url"] == case["model"]["base_url"].removesuffix("/v1") + path
        response = read(case_dir / f"{k:02}.response.json")
        if method == "POST":
            stream = STREAMS[posts.index(k)]
            assert request["body"]["model"] == MODEL
            assert request["body"]["temperature"] == 0.25
            assert response == {"status": 200, "headers": {"content-type": "text/event-stream"}}
            assert (case_dir / f"{k:02}.response.body").read_bytes() == stream.encode()
        else:
            assert request["body"] is None
            assert response["status"] == 404

    replies = read(case_dir / "expected.json")["replies"]
    assert [(r["stop"], r["finish_reason"]) for r in replies] == [
        ("Tools", "tool_calls"),
        ("End", "stop"),
    ] * 3
    assert [r["usage"]["input"] for r in replies] == [100, 130, 150, 200, 220, 240]


@scenario
async def test_the_replayed_case_proves_every_span_by_its_requests_prefix(
    recorded: tuple[Path, Scripted], tmp_path: Path
) -> None:
    """The whole chain on a scripted server: the provider stamps what it sent, the recorder
    keeps it, the replay sends it again, and the meter proves each span between two bills."""
    case_dir, _ = recorded
    async with replayed(case_dir, tmp_path) as run:
        assert [s.proof for s in run.spans] == ["prefix"] * (len(STREAMS) - 1)
        assert [s.cost for s in run.spans] == [30, 20, 50, 20, 20]
        assert [n.id for s in run.spans for n in s.nodes] == [
            n.id for n in run.walk[:-1] if n.id >= run.spans[0].nodes[0].id
        ]


def test_the_checks_a_real_fixture_gets_run_on_a_recorded_case(
    recorded: tuple[Path, Scripted], tmp_path: Path
) -> None:
    """`test_provider_fixtures.py` skips until the owner records fixtures; its span check is
    run here on the scripted server's case, so it is known to work the day they arrive. (This
    case is synthetic: it says nothing about any real server's tokens per byte.)"""
    case_dir, _ = recorded
    spans_fit_their_ceilings(case_dir, tmp_path)


@scenario
async def test_replaying_the_written_case_gives_its_replies_and_its_requests(
    recorded: tuple[Path, Scripted], tmp_path: Path
) -> None:
    case_dir, _ = recorded
    assert cases("llamacpp", case_dir.parents[1]) == [case_dir]
    async with replayed(case_dir, tmp_path) as run:
        assert run.replies == run.expected
        assert len(run.replies) == len(STREAMS)
        assert run.sent == run.recorded
        assert [m is not None and "usage" in m for m in run.metas] == [True] * len(STREAMS)
        assert run.case == read(case_dir / "case.json")
        assert run.walk == run.session.tree.walk(run.session.cursor(run.cursor).at)
        assert run.driver.session is run.session
        outcomes = [
            outcome
            for node in run.walk
            if isinstance(node.body, Body.Results)
            for _, outcome, _ in node.body.outcomes
        ]
        answers = run.case["tools"][0]["answers"]
        assert outcomes == [
            Outcome.Ok(answers["A-113"]),
            Outcome.Ok(answers["B-207"]),
            Outcome.Ok(answers["C-042"]),
            Outcome.Err("no such part: Z-999"),
        ]


@scenario
async def test_a_gap_adds_a_fourth_turn_after_really_waiting(tmp_path: Path) -> None:
    server = Scripted([*STREAMS, says("A-113 is.", prompt=260)])
    done = record(server, tmp_path / "out", "--gap", "0.25", "--case", "turns")
    assert done.returncode == 0, done.stderr
    assert "sleeping 0.25 s" in done.stdout
    [case_dir] = cases("llamacpp", tmp_path / "out")
    case = read(case_dir / "case.json")
    assert case["gaps"] == [0, 0, 0, 0.25]
    assert len(case["turns"]) == 4
    async with replayed(case_dir, tmp_path) as run:
        assert run.replies == run.expected
        assert len(run.replies) == len(STREAMS) + 1
        assert run.sent == run.recorded


@scenario
async def test_the_builtin_tool_case_is_one_raw_gemini_request_that_replays(
    tmp_path: Path,
) -> None:
    counts = {"promptTokenCount": 12, "candidatesTokenCount": 3, "totalTokenCount": 45}
    stream = gemini_chunk([{"text": "In 1858."}], "STOP", {**counts, "toolUsePromptTokenCount": 30})
    server = Scripted([stream])
    with server.running() as root:
        done = script(
            "gemini", "--model", "gemini-test", "--case", "builtin-tool", "--key-env", KEY_ENV,
            "--base-url", f"{root}/v1beta", "--out", tmp_path / "out",
        )  # fmt: skip
    assert done.returncode == 0, done.stderr
    [(method, path, headers)] = server.requests
    assert (method, path) == ("POST", "/v1beta/models/gemini-test:streamGenerateContent?alt=sse")
    assert headers["x-goog-api-key"] == KEY

    [case_dir] = cases("gemini", tmp_path / "out")
    assert case_dir.name == "builtin-tool"
    case = read(case_dir / "case.json")
    assert (case["kind"], case["provider"], case["tools"]) == ("raw", "gemini", [])
    request = read(case_dir / "00.request.json")
    assert request["url"] == f"{case['model']['base_url']}{path.removeprefix('/v1beta')}"
    assert request["headers"] == {"content-type": "application/json", "accept": "*/*"}
    assert request["body"]["tools"] == [{"google_search": {}}]
    assert request["body"]["contents"] == [{"role": "user", "parts": [{"text": case["turns"][0]}]}]
    assert (case_dir / "00.response.body").read_bytes() == stream.encode()
    for written in everything_under(tmp_path):
        assert KEY.encode() not in written.read_bytes(), written

    [reply] = read(case_dir / "expected.json")["replies"]
    assert (reply["stop"], reply["finish_reason"]) == ("End", "STOP")
    assert reply["usage"]["output"] == 3
    async with replayed(case_dir, tmp_path) as run:
        assert run.replies == run.expected == [reply]
        assert [m is not None and m["usage"] for m in run.metas] == [reply["usage"]]
        assert [type(n.body) for n in run.walk] == [Body.Config, Body.System, Body.User]
        assert len(run.sent) == 1
        with pytest.raises(ValueError, match="no driver"):
            _ = run.driver


def test_the_key_and_every_header_off_the_whitelist_stay_out_of_the_files(
    recorded: tuple[Path, Scripted],
) -> None:
    case_dir, server = recorded
    assert all(
        headers.get("authorization") == f"Bearer {KEY}"
        for method, _, headers in server.requests
        if method == "POST"
    ), "the run did send the key"
    for path in everything_under(case_dir.parents[1]):
        assert KEY.encode() not in path.read_bytes(), path
    for path in case_dir.glob("*.request.json"):
        assert set(read(path)["headers"]) <= {"content-type", "anthropic-version", "accept"}, path
    for path in case_dir.glob("*.response.json"):
        assert set(read(path)["headers"]) <= {"content-type"}, path
    assert read(case_dir / "case.json")["model"]["api_key_env"] == KEY_ENV


def test_a_url_loses_its_secret_query_parameters_and_keeps_the_rest() -> None:
    spec = importlib.util.spec_from_file_location("record_provider", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)
    strip = recorder.strip_query
    plain = "https://h.test/v1beta/models/m:streamGenerateContent?alt=sse"
    assert strip(plain) == plain
    assert strip(f"{plain}&key={KEY}") == plain
    assert strip(f"https://h.test/v1/models?key={KEY}") == "https://h.test/v1/models"
    assert (
        strip(f"https://h.test/p?api_key={KEY}&a=1&access_token=x&token=y&KEY=z&b=2")
        == "https://h.test/p?a=1&b=2"
    )
    files = {"a": b"clean", "b": f"x {KEY} y".encode(), "c": b"sk%2Dtest"}
    assert recorder.find_key(files, KEY) == "b"
    assert recorder.find_key({"a": b"p%2Fq%2Bkey-long"}, "p/q+key-long") == "a"
    assert recorder.find_key(files, None) is None
    kept = recorder.kept_headers(
        {"Authorization": "Bearer x", "Content-Type": "application/json", "User-Agent": "u"},
        recorder.REQUEST_HEADERS,
    )
    assert kept == {"content-type": "application/json"}


def test_a_server_that_echoes_the_key_fails_the_run_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    done = record(Scripted(STREAMS, echo=True), tmp_path / "out")
    assert done.returncode == 1, done.stderr
    assert ".response.body would hold the value of " + KEY_ENV in done.stderr
    assert KEY not in done.stderr
    assert KEY not in done.stdout
    assert everything_under(tmp_path) == []
    assert not (tmp_path / "out" / "llamacpp").exists()


def test_a_failed_sample_fails_the_run_with_its_error_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    done = record(Scripted(STREAMS, status=503), tmp_path / "out")
    assert done.returncode == 1, done.stderr
    assert "turn 1: the sample failed" in done.stderr
    assert "503" in done.stderr
    assert everything_under(tmp_path) == []


def test_what_the_command_line_refuses(recorded: tuple[Path, Scripted], tmp_path: Path) -> None:
    case_dir, server = recorded
    seen = len(server.requests)
    again = record(server, case_dir.parents[1])
    assert again.returncode == 2
    assert "exists" in again.stderr
    assert len(server.requests) == seen, "refused before any request"

    url = "http://127.0.0.1:9/v1"  # nothing is sent: each is refused first
    not_gemini = script("llamacpp", "--model", MODEL, "--base-url", url, "--case", "builtin-tool")
    assert not_gemini.returncode == 2
    assert "gemini only" in not_gemini.stderr

    no_url = script("llamacpp", "--model", MODEL, "--out", tmp_path)
    assert no_url.returncode == 2
    assert "--base-url" in no_url.stderr

    no_key = script(
        "llamacpp", "--model", MODEL, "--base-url", url, "--key-env", KEY_ENV, "--out", tmp_path,
        key=None,
    )  # fmt: skip
    assert no_key.returncode == 2
    assert KEY_ENV in no_key.stderr

    for family, variable in (("anthropic", "ANTHROPIC_API_KEY"), ("gemini", "GEMINI_API_KEY")):
        env = {k: v for k, v in os.environ.items() if k != variable}
        unset = subprocess.run(
            [sys.executable, str(SCRIPT), family, "--model", MODEL, "--out", str(tmp_path)],
            env=env, capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip
        assert unset.returncode == 2
        assert variable in unset.stderr
    assert everything_under(tmp_path) == []


def test_regolden_rewrites_expected_json_from_the_recorded_responses(
    recorded: tuple[Path, Scripted], tmp_path: Path
) -> None:
    case_dir = tmp_path / "turns"
    shutil.copytree(recorded[0], case_dir)
    golden = (case_dir / "expected.json").read_bytes()
    before = {p.name: p.read_bytes() for p in case_dir.iterdir()}

    same = script("--regolden", case_dir, key=None)
    assert same.returncode == 0, same.stderr
    assert {p.name: p.read_bytes() for p in case_dir.iterdir()} == before

    (case_dir / "expected.json").write_text('{"replies": []}\n')
    repaired = script("--regolden", case_dir, key=None)
    assert repaired.returncode == 0, repaired.stderr
    assert (case_dir / "expected.json").read_bytes() == golden

    assert script("--regolden", tmp_path / "nothing", key=None).returncode == 2


def test_regolden_says_which_request_is_not_the_recorded_one(
    recorded: tuple[Path, Scripted], tmp_path: Path
) -> None:
    case_dir = tmp_path / "turns"
    shutil.copytree(recorded[0], case_dir)
    last = max(case_dir.glob("*.request.json"))
    for path in case_dir.glob(last.name.replace("request.json", "*")):
        path.unlink()
    golden = (case_dir / "expected.json").read_bytes()
    done = script("--regolden", case_dir, key=None)
    assert done.returncode == 1
    assert f"only {int(last.name[:2])} were recorded" in done.stderr
    assert (case_dir / "expected.json").read_bytes() == golden


@scenario
async def test_a_replay_that_asks_for_what_was_not_recorded_fails_saying_which_request(
    recorded: tuple[Path, Scripted], tmp_path: Path
) -> None:
    short = tmp_path / "short"
    shutil.copytree(recorded[0], short)
    last = max(short.glob("*.request.json"))
    for path in short.glob(last.name.replace("request.json", "*")):
        path.unlink()
    with pytest.raises(ReplayMismatch, match=f"only {int(last.name[:2])} were recorded"):
        async with replayed(short, tmp_path):
            pass

    moved = tmp_path / "moved"
    shutil.copytree(recorded[0], moved)
    first = min(moved.glob("*.request.json"))
    first.write_text(json.dumps({**read(first), "url": "http://elsewhere.test/v1/other"}))
    with pytest.raises(ReplayMismatch, match=r"request 00 is \S+ http://127\.0\.0\.1:.* recorded"):
        async with replayed(moved, tmp_path):
            pass


def test_a_family_without_fixtures_has_no_cases_and_a_command_that_records_it(
    tmp_path: Path,
) -> None:
    (tmp_path / "gemini").mkdir()
    (tmp_path / "gemini" / ".recording-left").mkdir()
    for family in FAMILIES:
        assert cases(family, tmp_path) == []
        assert f"scripts/record_provider.py {family} --model MODEL_ID" in RECORD[family]
    assert set(RECORD) == set(FAMILIES)
    assert "--gap 360" in RECORD["anthropic"]
    assert "--case builtin-tool" in RECORD["gemini"]
    assert "--base-url http://HOST:8100/v1" in RECORD["llamacpp"]
