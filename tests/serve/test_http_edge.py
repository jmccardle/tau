"""The HTTP edge (D29) as a client sees it: the stream and its connection id, `hello` POSTed and
answered after the snapshot, the order of events and responses, the bearer token and the origin
rule, unknown and reclaimed streams, each way a stream ends, and what a disconnect does. The
behavior suite run in `http` mode is the test of the whole `Client`; these are the things only the
edge has. Requests are made by hand with httpx, as a client in another language would."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from fakes import FakeProvider
from harness import Harness, scenario

from tau import PROTOCOL, Cursor, HttpEdge, Server

SECONDS = 5


@pytest.fixture(autouse=True)
def http() -> Iterator[None]:
    """The `Harness` serves its driver on an HTTP edge and its clients dial that."""
    Harness.mode = "http"
    yield
    Harness.mode = "local"


def edge_of(h: Harness) -> tuple[str, str]:
    assert h.url is not None
    assert h.token is not None
    return h.url, h.token


class Raw:
    """A stream opened by hand: its connection id, and the lines after it."""

    def __init__(self, http: httpx.AsyncClient, response: httpx.Response) -> None:
        self.http = http
        self.response = response
        self.lines: AsyncIterator[str] = response.aiter_lines()
        self.id = ""

    @classmethod
    async def open(cls, url: str, token: str, **headers: str) -> "Raw":
        http = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}", **headers},
            timeout=httpx.Timeout(SECONDS, read=None),
            trust_env=False,
        )
        response = await http.send(http.build_request("GET", "stream"), stream=True)
        assert response.status_code == 200, response.status_code
        raw = cls(http, response)
        kind, data = await raw.event()
        assert kind == "connection"
        raw.id = data
        return raw

    async def line(self) -> str:
        return await asyncio.wait_for(anext(self.lines), SECONDS)

    async def event(self) -> tuple[str, str]:
        """The next event: its type and data (comments skipped)."""
        kind, data = "message", list[str]()
        while True:
            line = await self.line()
            if not line and data:
                return kind, "\n".join(data)
            field, _, value = line.partition(":")
            if field == "event":
                kind = value.strip()
            elif field == "data":
                data.append(value.removeprefix(" "))

    async def hear(self) -> dict[str, Any]:
        kind, data = await self.event()
        assert kind == "message"
        return json.loads(data)

    async def post(self, request_id: int, method: str, **params: object) -> httpx.Response:
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        return await self.http.post(f"stream/{self.id}", content=json.dumps(message))

    async def call(self, request_id: int, method: str, **params: object) -> list[dict[str, Any]]:
        """POST a request (accepted: 202) and return every message up to its response."""
        answer = await self.post(request_id, method, **params)
        assert answer.status_code == 202, answer.text
        heard = [await self.hear()]
        while heard[-1].get("id") != request_id:
            heard.append(await self.hear())
        return heard

    async def hello(self, client: str = "test:raw", **extra: object) -> list[dict[str, Any]]:
        return await self.call(0, "hello", protocol=PROTOCOL, client=client, **extra)

    async def ended(self) -> None:
        """The server closes the stream: the lines run out."""
        with pytest.raises(StopAsyncIteration):
            await self.drain()

    async def drain(self) -> None:
        while True:
            await self.line()

    async def close(self) -> None:
        await self.response.aclose()
        await self.http.aclose()


def plain(url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=url, timeout=SECONDS, trust_env=False)


# The stream and the handshake


@scenario
async def test_a_stream_opens_with_its_id_and_headers_that_keep_it_streaming(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    url, token = edge_of(h)
    first, second = await Raw.open(url, token), await Raw.open(url, token)
    assert first.response.headers["content-type"] == "text/event-stream"
    assert first.response.headers["cache-control"] == "no-cache"
    assert first.response.headers["x-accel-buffering"] == "no"
    assert first.id
    assert first.id != second.id
    await first.close()
    await second.close()
    await h.close()


@scenario
async def test_hello_is_posted_and_answered_on_the_stream_after_the_snapshot(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    raw = await Raw.open(*edge_of(h))
    heard = await raw.hello("john@laptop:vscode")
    assert [m["params"]["kind"] for m in heard[:-1]] == ["CursorView", "Labels"]
    assert all(m["method"] == "event" for m in heard[:-1])
    welcome = heard[-1]["result"]
    assert welcome["protocol"] == PROTOCOL
    assert welcome["session"] == h.directory.name
    await raw.close()
    await h.close()


@scenario
async def test_a_response_follows_the_events_its_call_caused(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    raw = await Raw.open(*edge_of(h))
    await raw.hello()
    heard = await raw.call(1, "open", at=1)
    opened = heard[-1]["result"]
    views = [m["params"]["cursor"] for m in heard[:-1] if m["params"]["kind"] == "CursorView"]
    assert opened in views, "the new cursor's view came before the answer naming it"
    await raw.close()
    await h.close()


@scenario
async def test_anything_but_hello_first_is_answered_on_the_stream_which_then_ends(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    raw = await Raw.open(*edge_of(h))
    (refused,) = await raw.call(4, "node", id=0)
    assert refused["error"]["code"] == -32600
    await raw.ended()
    await raw.close()
    await h.close()


# Access


@scenario
async def test_a_request_without_the_token_or_with_another_is_refused_401(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    url, token = edge_of(h)
    raw = await Raw.open(url, token)
    async with plain(url) as http:
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": token}):
            opened = await http.get("stream", headers=headers)
            assert opened.status_code == 401
            assert opened.headers["www-authenticate"] == "Bearer"
            posted = await http.post(f"stream/{raw.id}", content=b"{}", headers=headers)
            assert posted.status_code == 401
    await raw.close()
    await h.close()


@scenario
async def test_a_request_from_an_origin_not_allowed_is_refused_403(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    url, token = edge_of(h)
    async with plain(url) as http:
        refused = await http.get(
            "stream",
            headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
        )
        assert refused.status_code == 403, "the right token does not make a web page welcome"
        preflight = await http.options("stream", headers={"Origin": "https://evil.example"})
        assert preflight.status_code == 403
    await h.close()


@scenario
async def test_an_allowed_origin_is_answered_with_cors_headers(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    origin = "http://localhost:5173"
    server = await Server.start(
        h.driver, session="s", cwd=h.directory, http=HttpEdge(port=0, origins=(origin,))
    )
    assert server.url is not None
    assert server.token is not None
    async with plain(server.url) as http:
        preflight = await http.options("stream", headers={"Origin": origin})
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == origin
        assert "Authorization" in preflight.headers["access-control-allow-headers"]
    raw = await Raw.open(server.url, server.token, Origin=origin)
    assert raw.response.headers["access-control-allow-origin"] == origin
    await raw.hello()
    await raw.close()
    await server.close()
    await h.close()


# Streams that are not open


@scenario
async def test_a_post_to_a_stream_that_is_not_open_is_404_with_a_json_rpc_error(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    raw = await Raw.open(*edge_of(h))
    await raw.hello()
    gone = raw.id
    await raw.close()
    url, token = edge_of(h)
    request = {"jsonrpc": "2.0", "id": 7, "method": "node", "params": {"id": 0}}
    async with plain(url) as http:
        auth = {"Authorization": f"Bearer {token}"}
        for id in ("never-was", gone):
            answer = await http.post(f"stream/{id}", json=request, headers=auth)
            for _ in range(100):  # the server notices the closed stream on its own time
                if answer.status_code == 404:
                    break
                await asyncio.sleep(0.01)
                answer = await http.post(f"stream/{id}", json=request, headers=auth)
            assert answer.status_code == 404
            body = answer.json()
            assert body["id"] == 7
            assert body["error"]["code"] == -32600
    await h.close()


@scenario
async def test_a_second_stream_cannot_claim_an_id_there_is_no_resume(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    url, token = edge_of(h)
    raw = await Raw.open(url, token)
    async with plain(url) as http:
        claimed = await http.get(f"stream/{raw.id}", headers={"Authorization": f"Bearer {token}"})
        assert claimed.status_code == 409
        elsewhere = await http.get("ws", headers={"Authorization": f"Bearer {token}"})
        assert elsewhere.status_code == 404
        wrong = await http.post("stream", headers={"Authorization": f"Bearer {token}"})
        assert wrong.status_code == 405
    await raw.hello()  # the first stream is untouched
    await raw.close()
    await h.close()


# The end


@scenario
async def test_a_stopped_driver_ends_the_stream_with_a_null_error_and_closes_it(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    raw = await Raw.open(*edge_of(h))
    await raw.hello()
    await h.driver.stop()
    assert await raw.hear() == {"jsonrpc": "2.0", "method": "end", "params": {"error": None}}
    await raw.ended()
    await raw.close()
    await h.close()


class Shouts:
    """A step that tells every connected client something too big for a connection to hold."""

    name = "shouts"

    def __init__(self, size: int) -> None:
        self.text = "x" * size

    async def pre_step(self, cursor: Cursor, subject: object) -> None:
        for _ in range(12):
            cursor.tell(self.text)


@scenario
async def test_a_client_that_does_not_read_is_cut_off_and_told_so_in_end(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Shouts(300_000)])
    raw = await Raw.open(*edge_of(h))
    await raw.hello(buffer=2)
    await h.send("hi")  # nobody reads the stream: the connection fills, then the backlog passes 2
    await h.settle()
    last: dict[str, Any] = {}
    while last.get("method") != "end":
        last = await raw.hear()
    assert last["params"]["error"]["code"] == -32014
    await raw.ended()
    await raw.close()
    await h.close()


# Disconnects and the heartbeat


class Asks:
    """A `pre_step` hook asking `to` one question, once."""

    name = "asks"

    def __init__(self, to: str) -> None:
        self.to = to
        self.answers: list[object] = []

    async def pre_step(self, cursor: Cursor, subject: object) -> None:
        if not self.answers:
            self.answers.append(await cursor.ask("still yours?", Literal["yes"], to=self.to))


async def asked_to(h: Harness) -> str | None:
    (question,) = (await h.view()).questions
    return question.to


async def pooled(h: Harness) -> None:
    for _ in range(200):  # the server notices the drop on its own time
        if await asked_to(h) is None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the question stayed with the client that went")


@scenario
async def test_a_dropped_stream_loses_its_client_and_its_question_goes_to_everyone(
    tmp_path: Path,
) -> None:
    asks = Asks("alice")
    h = await Harness.create(tmp_path, provider=FakeProvider([]), extensions=[asks])
    raw = await Raw.open(*edge_of(h))
    await raw.hello("alice")
    await h.send("hi")
    await h.settle()
    assert await asked_to(h) == "alice"
    await raw.close()
    await pooled(h)
    await h.respond("yes")
    await h.settle()
    assert asks.answers == ["yes"]
    await h.close()


@scenario
async def test_an_idle_stream_pings_every_heartbeat(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    server = await Server.start(
        h.driver, session="s", cwd=h.directory, http=HttpEdge(port=0, heartbeat=0.05)
    )
    assert server.url is not None
    assert server.token is not None
    raw = await Raw.open(server.url, server.token)
    await raw.hello()
    pings = 0
    while pings < 3:
        line = await raw.line()
        pings += line == ": ping"
    await raw.close()
    await server.close()
    await h.close()


async def raw_stream(url: str, token: str) -> tuple[asyncio.StreamWriter, str]:
    """A stream opened over a bare TCP connection: its writer (to kill the connection with) and
    its connection id."""
    parsed = httpx.URL(url)
    assert parsed.port is not None
    reader, writer = await asyncio.open_connection(parsed.host, parsed.port)
    writer.write(
        f"GET /stream HTTP/1.1\r\nHost: {parsed.host}\r\n"
        f"Authorization: Bearer {token}\r\n\r\n".encode()
    )
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), SECONDS)
    assert head.startswith(b"HTTP/1.1 200")
    body = b""
    while b"\n\n" not in body:
        body += await asyncio.wait_for(reader.read(4096), SECONDS)
    (id,) = [line[6:] for line in body.decode().splitlines() if line.startswith("data: ")]
    return writer, id


@scenario
async def test_a_client_that_vanishes_from_an_idle_stream_is_disconnected(tmp_path: Path) -> None:
    """The connection is reset, with no clean end of the response, while nothing is being sent
    to the client: the server finds it gone (by the reset, or the heartbeat's write) and its
    question goes to the pool."""
    asks = Asks("alice")
    h = await Harness.create(tmp_path, provider=FakeProvider([]), extensions=[asks])
    server = await Server.start(
        h.driver, session="s", cwd=h.directory, http=HttpEdge(port=0, heartbeat=0.05)
    )
    assert server.url is not None
    assert server.token is not None
    writer, id = await raw_stream(server.url, server.token)
    hello = {"jsonrpc": "2.0", "id": 0, "method": "hello"}
    hello["params"] = {"protocol": PROTOCOL, "client": "alice"}  # type: ignore[assignment]
    async with plain(server.url) as http:
        auth = {"Authorization": f"Bearer {server.token}"}
        assert (await http.post(f"stream/{id}", json=hello, headers=auth)).status_code == 202
    await h.send("hi")
    await h.settle()
    assert await asked_to(h) == "alice"
    writer.transport.abort()  # the process died
    await pooled(h)
    await h.respond("yes")
    await h.settle()
    assert asks.answers == ["yes"]
    await server.close()
    await h.close()


# Hardening


async def exchange(url: str, request: bytes) -> bytes:
    """Send `request` over a bare connection and read until the server answers (or closes)."""
    parsed = httpx.URL(url)
    assert parsed.port is not None
    reader, writer = await asyncio.open_connection(parsed.host, parsed.port)
    writer.write(request)
    try:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), SECONDS)
    finally:
        writer.transport.abort()


@scenario
async def test_a_request_without_the_token_is_refused_before_its_body_is_read(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    url, _ = edge_of(h)
    head = b"POST /stream/x HTTP/1.1\r\nHost: h\r\nContent-Length: 1000000000\r\n\r\n"
    answer = await exchange(url, head)  # not one byte of the gigabyte is sent
    assert answer.startswith(b"HTTP/1.1 401")
    await h.close()


@scenario
async def test_a_request_that_is_not_http_is_answered_400(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    url, _ = edge_of(h)
    answer = await exchange(url, b"GET /stream HTTP/1.1\r\nno colon here\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 400")
    await h.close()


@scenario
async def test_a_client_that_stops_reading_does_not_hold_the_server_open(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, extensions=[Shouts(300_000)])
    server = await Server.start(
        h.driver, session="s", cwd=h.directory, http=HttpEdge(port=0, heartbeat=0.1)
    )
    assert server.url is not None
    assert server.token is not None
    raw = await Raw.open(server.url, server.token)
    await raw.hello(buffer=1000)  # a buffer large enough that it is never cut off
    await h.send("hi")
    await h.settle()  # megabytes wait for a client that reads nothing
    async with asyncio.timeout(SECONDS * 2):
        await server.close()
    await raw.close()
    await h.close()


# The web client and the help page


def edge_answer(answer: bytes) -> int:
    """The status of a response head read by `exchange`."""
    return int(answer.split(b" ", 2)[1])


@scenario
async def test_the_help_page_is_served_without_a_token_and_says_nothing_of_the_session(
    tmp_path: Path,
) -> None:
    h = await Harness.create(tmp_path)
    url, token = edge_of(h)
    async with plain(url) as http:
        for path in ("", "index.html"):
            page = await http.get(path)
            assert page.status_code == 200
            assert page.headers["content-type"] == "text/html; charset=utf-8"
            assert "connect-src 'self'" in page.headers["content-security-policy"]
            assert page.headers["referrer-policy"] == "no-referrer"
            assert "tau serve --web-root DIR" in page.text
            assert str(h.directory) not in page.text
            assert token not in page.text
        head = await http.head("")
        assert head.status_code == 200
        assert head.content == b""
        assert int(head.headers["content-length"]) == len(page.content)
        assert (await http.get("app.js")).status_code == 404, "no client: nothing else"
        assert (await http.post("")).status_code == 405
        assert (await http.get("stream")).status_code == 401, "the wire still needs the token"
    await h.close()


@scenario
async def test_the_edges_own_origin_may_call_the_wire_and_another_may_not(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    url, token = edge_of(h)
    port = httpx.URL(url).port
    for own in (url.rstrip("/"), f"http://localhost:{port}"):
        raw = await Raw.open(url, token, Origin=own)
        await raw.hello()  # its POST carries the Origin too
        await raw.close()
    async with plain(url) as http:
        auth = {"Authorization": f"Bearer {token}"}
        other_port = await http.get("stream", headers={**auth, "Origin": "http://127.0.0.1:1"})
        assert other_port.status_code == 403
    assert port != 1
    await h.close()


def client_dir(base: Path) -> Path:
    """A built web client, and a secret beside it that must stay out of reach."""
    root = base / "client"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>client</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (base / "secret.txt").write_text("not yours", encoding="utf-8")
    (root / "out").symlink_to(base / "secret.txt")
    return root


@scenario
async def test_a_web_root_is_served_with_its_routes_and_nothing_outside_it(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    root = client_dir(tmp_path)
    server = await Server.start(
        h.driver, session="s", cwd=h.directory, http=HttpEdge(port=0, web_root=root)
    )
    assert server.url is not None
    async with plain(server.url) as http:
        index = await http.get("")
        assert index.text.endswith("<title>client</title>")
        script = await http.get("assets/app.js")
        assert script.headers["content-type"].startswith("text/javascript") or script.headers[
            "content-type"
        ].startswith("application/javascript")
        assert script.text == "console.log(1)"
        route = await http.get("sessions/abc")  # the client's own routing
        assert route.text == index.text
        assert (await http.get("assets/missing.png")).status_code == 404
    for target in (
        b"/../secret.txt",
        b"/%2e%2e/secret.txt",
        b"/assets/..%2f..%2fsecret.txt",
        b"/out",
    ):
        answer = await exchange(server.url, b"GET " + target + b" HTTP/1.1\r\nHost: h\r\n\r\n")
        assert edge_answer(answer) == 404, target
    await server.close()
    await h.close()


@scenario
async def test_a_web_root_without_an_index_is_refused_at_start(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path)
    with pytest.raises(ValueError, match=r"holds no index\.html"):
        await Server.start(
            h.driver, session="s", cwd=h.directory, http=HttpEdge(port=0, web_root=tmp_path)
        )
    await h.close()
