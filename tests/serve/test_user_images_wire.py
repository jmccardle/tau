"""User images on the wire (D31), as a client in another language sends them: `input`/`send` take
`images`, each `{"data": base64}` or `{"sha256": hex}`, checked where the params are decoded
whatever the transport (strict base64, the server's own sniff, at most 20 images of 20 MiB), and
`image` reads one back. The behavior suite run remote is the test of the calls themselves."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Windows: tau serve does not run there (a Unix domain socket, D21; not ported)",
        allow_module_level=True,
    )

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from fakes import FakeProvider, Reply, fake_catalog
from harness import Harness, scenario
from pngs import png
from websockets.asyncio.client import ClientConnection, unix_connect

from tau import MAX_INPUT_IMAGES, PROTOCOL, Image, ImageLimits, Part

PICTURE = png(8, 6)
DATA = base64.b64encode(PICTURE).decode("ascii")
LIMITS = ImageLimits(1_000_000)
PADDED = base64.b64encode(PICTURE + b"\0" * ((1 - len(PICTURE) % 3) % 3)).decode("ascii")
assert PADDED.endswith("=")


pytestmark = pytest.mark.usefixtures("remote")


async def dial(h: Harness) -> ClientConnection:
    assert h.socket is not None
    ws = await unix_connect(h.socket, max_size=None)
    await say(ws, 0, "hello", protocol=PROTOCOL, client="test:raw")
    await answer(ws, 0)
    return ws


async def say(ws: ClientConnection, request_id: int, method: str, **params: object) -> None:
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    await ws.send(json.dumps(message))


async def answer(ws: ClientConnection, request_id: int) -> dict[str, Any]:
    """The response to `request_id`, past the events before it."""
    while True:
        heard = json.loads(await asyncio.wait_for(ws.recv(), 5))
        if heard.get("id") == request_id:
            return heard


def error_of(message: dict[str, Any]) -> dict[str, Any]:
    found = message["error"]
    assert isinstance(found, dict)
    return found


@scenario
async def test_images_that_are_not_strict_base64_of_an_image_are_invalid_params(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path, provider=FakeProvider([], image_limits=LIMITS), catalog=fake_catalog(images=True)
    )
    ws = await dial(h)
    bad = [
        [{"data": DATA[:-2] + "!!"}],  # not base64
        [{"data": PADDED.rstrip("=")}],  # padding left out
        [{"data": " " + DATA}],  # whitespace
        [{"data": base64.b64encode(b"GIF8 not really").decode()}],  # the server sniffs
        [{"data": base64.b64encode(b"hello").decode()}],  # not an image at all
        [{"sha256": "../../etc/passwd"}],  # a name, never a path
        [{"data": DATA, "sha256": Image(PICTURE).sha256}],  # one or the other
        [{}],
        [{"data": DATA}] * (MAX_INPUT_IMAGES + 1),
    ]
    for i, images in enumerate(bad, 1):
        await say(ws, i, "send", cursor=h.cursor, text="look", images=images)
        got = await answer(ws, i)
        assert error_of(got)["code"] == -32602, (images[:1], got)
        assert DATA not in json.dumps(got), "the bytes are never echoed"
    assert h.session.cursor(h.cursor).items == []
    await ws.close()
    await h.close()


@scenario
async def test_an_image_is_sent_by_its_bytes_then_named_and_read_back(tmp_path: Path) -> None:
    provider = FakeProvider([Reply([Part.Text("seen")])], image_limits=LIMITS)
    h = await Harness.create(tmp_path, provider=provider, catalog=fake_catalog(images=True))
    ws = await dial(h)
    claimed = [{"data": DATA, "mime": "image/gif", "width": 1}]  # a client's claims are ignored
    await say(ws, 1, "input", cursor=h.cursor, text="look", images=claimed)
    assert isinstance((await answer(ws, 1))["result"], int)
    [item] = h.session.cursor(h.cursor).items
    assert item.data.images == [Image(PICTURE)]  # type: ignore[union-attr]
    sha = Image(PICTURE).sha256
    await say(ws, 2, "image", sha256=sha)
    assert base64.b64decode(str((await answer(ws, 2))["result"])) == PICTURE
    await say(ws, 3, "input", cursor=h.cursor, text="again", images=[{"sha256": sha}])
    assert isinstance((await answer(ws, 3))["result"], int)
    other = Image(png(2, 2)).sha256
    await say(ws, 4, "input", cursor=h.cursor, text="nope", images=[{"sha256": other}])
    assert error_of(await answer(ws, 4))["code"] == -32007, "NotFound: the session has none"
    await say(ws, 5, "image", sha256=other)
    assert error_of(await answer(ws, 5))["code"] == -32007
    await ws.close()
    await h.close()


@scenario
async def test_a_model_without_images_is_refused_with_why(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([], image_limits=LIMITS))
    ws = await dial(h)
    await say(ws, 1, "send", cursor=h.cursor, text="look", images=[{"data": DATA}])
    got = error_of(await answer(ws, 1))
    assert got["code"] == -32009
    assert "does not take images" in str(got["message"])
    assert h.session.cursor(h.cursor).items == []
    await ws.close()
    await h.close()
