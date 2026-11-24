"""An embedding program's own extension returns text and an image in one tool result, and the
OpenAI-compatible provider moves the image into a `user` message after the tool message (D30): the
path mcrf-term takes, `start_session(own=[extension])` against a llama.cpp server. Here the server
is a fake that records what it is sent.

The live tests at the end talk to a real vision model, and run only when `TAU_VISION_URL` names
its OpenAI-compatible base URL (`http://host:8100/v1`; `TAU_VISION_MODEL` its model name, default
`qwen3.8-27b`). Each makes two small requests."""

import argparse
import asyncio
import base64
import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
from pngs import picture
from world import World

from tau import Body, Image, Outcome, Part, Started, start_session
from tau.cli import extension_flags, session_flags

PICTURE = picture()
B64 = base64.b64encode(PICTURE).decode("ascii")


def _args(*argv: str) -> argparse.Namespace:
    return argparse.ArgumentParser(parents=[extension_flags(), session_flags()]).parse_args(
        list(argv)
    )


class Screens:
    """An embedding program's own extension: `screenshot` saves the screen as a PNG under
    `shots` and returns a line of text and the image, in one result."""

    name = "screens"

    def __init__(self, shots: Path) -> None:
        async def screenshot() -> list[str | Image]:
            """Take a screenshot of the game and show it."""
            shots.mkdir(exist_ok=True)
            path = shots / f"shot-{len(list(shots.iterdir()))}.png"
            path.write_bytes(PICTURE)  # the engine writes it; here, a picture of our own
            return [f"screenshot saved to {path.name}", Image.from_path(path)]

        self.tools = (screenshot,)


class _Server(BaseHTTPRequestHandler):
    """A chat completions server: the first request is answered with a call to `screenshot`, the
    next with text. It keeps every body it is sent."""

    bodies: ClassVar[list[dict[str, object]]] = []

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.bodies.append(body)
        if len(self.bodies) == 1:
            call = {
                "index": 0,
                "id": "c0",
                "type": "function",
                "function": {"name": "screenshot", "arguments": "{}"},
            }
            delta, finish = {"tool_calls": [call]}, "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": "a red disc"}, "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {"id": "x", "model": "m", "choices": [{"index": 0, "delta": delta}]}
        chunk["choices"][0]["finish_reason"] = finish  # type: ignore[index]
        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def model_server(world: World) -> Iterator[str]:
    _Server.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


async def _ask(started: Started, text: str) -> list[Body]:
    client = started.driver.client("test:images")
    cursor = started.session.cursors()[0]
    await client.send(cursor, text)
    await started.driver.rest(cursor)
    return [n.body for n in started.session.tree.walk(started.session.cursor(cursor).at)]


def _config(world: World, url: str, model: str, *, images: bool = True) -> None:
    world.user_config(
        {"default_model": "v", "models": {"v": {"model": model, "base_url": url, "images": images}}}
    )


def _results(bodies: list[Body]) -> Outcome:
    found = [b for b in bodies if isinstance(b, Body.Results)]
    assert found, bodies
    return found[-1].outcomes[0][1]


def test_an_own_extensions_image_reaches_the_model_after_the_tool_message(
    world: World, model_server: str
) -> None:
    _config(world, model_server, "m")
    work = world.home / "work"
    work.mkdir()

    async def run() -> list[Body]:
        started = await start_session(
            _args(), environ=world.environ, cwd=work, own=[Screens(work / "shots")]
        )
        try:
            return await _ask(started, "show me the game")
        finally:
            await started.stop()

    bodies = asyncio.run(run())
    assert _results(bodies) == Outcome.Ok("screenshot saved to shot-0.png", [Image(PICTURE)])
    last = bodies[-1]
    assert isinstance(last, Body.Assistant)
    assert last.parts == [Part.Text("a red disc")]
    sent = _Server.bodies[1]["messages"]
    assert isinstance(sent, list)
    assert sent[-2] == {
        "role": "tool",
        "tool_call_id": "c0",
        "content": "screenshot saved to shot-0.png",
    }
    assert sent[-1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "[images from tool results: c0]"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
        ],
    }


def test_a_model_without_images_is_told_and_is_sent_none(world: World, model_server: str) -> None:
    _config(world, model_server, "m", images=False)
    work = world.home / "work"
    work.mkdir()

    async def run() -> list[Body]:
        started = await start_session(
            _args(), environ=world.environ, cwd=work, own=[Screens(work / "shots")]
        )
        try:
            return await _ask(started, "show me the game")
        finally:
            await started.stop()

    outcome = _results(asyncio.run(run()))
    assert outcome == Outcome.Err(
        "screenshot returned 1 image, not sent: the model v does not take images (its config "
        "has no `images: true`; set it if the model can see). The tool's text: screenshot "
        "saved to shot-0.png"
    )
    assert "image_url" not in json.dumps(_Server.bodies[1])


# -- live, against a real vision model (opt-in)

LIVE_URL = os.environ.get("TAU_VISION_URL")
LIVE_MODEL = os.environ.get("TAU_VISION_MODEL", "qwen3.8-27b")
live = pytest.mark.skipif(
    LIVE_URL is None, reason="set TAU_VISION_URL to a vision model's OpenAI-compatible base URL"
)
ASK = (
    "Describe the picture in {what} in one sentence: its colours, its shapes, and any word written "
    "in it."
)


def _transcript(bodies: list[Body]) -> str:
    lines: list[str] = []
    for body in bodies:
        match body:
            case Body.User(text=text):
                lines.append(f"user: {text}")
            case Body.Assistant(parts=parts):
                for part in parts:
                    if isinstance(part, Part.Call):
                        lines.append(f"call: {part.tool} {part.args_raw}")
                    elif isinstance(part, Part.Text) and part.text.strip():
                        lines.append(f"model: {part.text.strip()}")
            case Body.Results(outcomes=outcomes):
                for _, outcome, _ in outcomes:
                    lines.append(f"result: {outcome!r}")
            case _:
                pass
    return "\n".join(lines)


def _live(world: World, ask: str, own: list[object]) -> list[Body]:
    assert LIVE_URL is not None
    _config(world, LIVE_URL, LIVE_MODEL)
    work = world.home / "work"
    work.mkdir(exist_ok=True)
    (work / "picture.png").write_bytes(PICTURE)

    async def run() -> list[Body]:
        started = await start_session(
            _args(),
            environ=world.environ,
            cwd=work,
            own=own,  # type: ignore[arg-type]
        )
        try:
            return await _ask(started, ask)
        finally:
            await started.stop()

    bodies = asyncio.run(run())
    print(_transcript(bodies))  # the transcript, for `pytest -s`
    return bodies


def _described(bodies: list[Body]) -> str:
    last = bodies[-1]
    assert isinstance(last, Body.Assistant), last
    return " ".join(p.text for p in last.parts if isinstance(p, Part.Text)).lower()


@live
def test_live_the_model_reads_a_png_with_read_and_describes_it(world: World) -> None:
    bodies = _live(world, ASK.format(what="the file picture.png (use the read tool)"), [])
    outcome = _results(bodies)
    assert isinstance(outcome, Outcome.Ok), outcome
    assert outcome.images, outcome
    said = _described(bodies)
    assert "red" in said, said
    assert "hello" in said, said


@live
def test_live_the_model_sees_an_own_extensions_screenshot(world: World) -> None:
    work = world.home / "work"
    bodies = _live(world, ASK.format(what="a screenshot (take one)"), [Screens(work / "shots")])
    outcome = _results(bodies)
    assert isinstance(outcome, Outcome.Ok), outcome
    assert outcome.images, outcome
    said = _described(bodies)
    assert "red" in said, said
    assert "hello" in said, said
