"""Images in tool results through the Harness (D30): `read` and a function tool return them, the
store keeps them, a sample is handed them with their bytes, and a model that cannot take them is
told so (at the call) or fails early (at the send). Never a silent drop."""

import json
from pathlib import Path

from fakes import FAKE_MODEL, FAKE_PROVIDER, FakeProvider, Reply, fake_catalog
from harness import Harness, scenario
from pngs import png

from tau import (
    Author,
    Body,
    Compaction,
    Cursor,
    Ending,
    Image,
    ImageLimits,
    ModelCatalog,
    Node,
    Outcome,
    Part,
    Prune,
    ReadTool,
    RequestCount,
    Stop,
    read_models,
)

LIMITS = ImageLimits(1_000_000, 4000)
PICTURE = png(40, 30)


def _read(path: str) -> Reply:
    return Reply([Part.Call("c1", "read", json.dumps({"path": path}))], Stop.Tools)


def _results(h: Harness) -> Body.Results:
    found = [b for b in h.bodies() if isinstance(b, Body.Results)]
    assert found, h.bodies()
    return found[-1]


def _outcome(h: Harness) -> Outcome:
    return _results(h).outcomes[0][1]


def _first(node: Node) -> Outcome.Ok:
    """The first outcome of a `Results` node, an `Ok`."""
    assert isinstance(node.body, Body.Results)
    outcome = node.body.outcomes[0][1]
    assert isinstance(outcome, Outcome.Ok)
    return outcome


def sighted_and_blind() -> ModelCatalog:
    """`fake` takes images, `blind` does not; both on the fake provider."""
    models = {
        FAKE_MODEL: {"model": FAKE_MODEL, "provider": FAKE_PROVIDER, "images": True},
        "blind": {"model": "blind", "provider": FAKE_PROVIDER},
    }
    return read_models(cli={"models": models})


@scenario
async def test_read_returns_an_image_the_next_sample_is_handed_with_its_bytes(
    tmp_path: Path,
) -> None:
    (tmp_path / "shot.png").write_bytes(PICTURE)
    provider = FakeProvider(
        [_read("shot.png"), Reply([Part.Text("a white box")])], image_limits=LIMITS
    )
    h = await Harness.create(
        tmp_path, provider=provider, catalog=fake_catalog(images=True), tools=[ReadTool(tmp_path)]
    )
    await h.send("look at shot.png")
    await h.settle()
    want = Image(PICTURE)
    assert _outcome(h) == Outcome.Ok(
        f"[image shot.png: image/png 40x30, {len(PICTURE)} bytes]", [want]
    )
    sent = provider.requests[-1].images
    assert sent == (want,)
    assert sent[0].data == PICTURE, "a provider is handed the bytes"
    await h.close()


@scenario
async def test_after_a_reload_the_image_is_read_from_the_session_and_sent_again(
    tmp_path: Path,
) -> None:
    """The log holds the reference; the bytes come from the session's image directory."""
    (tmp_path / "shot.png").write_bytes(PICTURE)
    first = FakeProvider([_read("shot.png"), Reply([Part.Text("seen")])], image_limits=LIMITS)
    h = await Harness.create(
        tmp_path, provider=first, catalog=fake_catalog(images=True), tools=[ReadTool(tmp_path)]
    )
    await h.send("look")
    await h.settle()
    await h.close()
    (tmp_path / "shot.png").unlink()  # the session keeps its own copy

    again = FakeProvider([Reply([Part.Text("still seen")])], image_limits=LIMITS)
    h = await Harness.load(tmp_path, provider=again, catalog=fake_catalog(images=True))
    loaded = _outcome(h)
    assert isinstance(loaded, Outcome.Ok)
    assert loaded.images[0].data is None, "a node loaded from disk holds the reference"
    await h.send("and now?")
    await h.settle()
    assert again.requests[-1].images[0].data == PICTURE
    await h.close()


@scenario
async def test_a_model_without_images_turns_the_call_into_an_error_keeping_the_text(
    tmp_path: Path,
) -> None:
    """(a) At the call: the model reads why, and the step goes on."""
    (tmp_path / "shot.png").write_bytes(PICTURE)
    provider = FakeProvider(
        [_read("shot.png"), Reply([Part.Text("ok, no image")])], image_limits=LIMITS
    )
    h = await Harness.create(tmp_path, provider=provider, tools=[ReadTool(tmp_path)])
    await h.send("look")
    await h.settle()
    text = f"[image shot.png: image/png 40x30, {len(PICTURE)} bytes]"
    assert _outcome(h) == Outcome.Err(
        "read returned 1 image, not sent: the model fake does not take images (its "
        "config has no `images: true`; set it if the model can see). The tool's text: " + text
    )
    assert provider.requests[-1].images == ()
    assert h.bodies()[-1] == Body.Assistant([Part.Text("ok, no image")], Stop.End)
    await h.close()


@scenario
async def test_an_image_over_the_providers_limits_is_an_error_that_says_so(tmp_path: Path) -> None:
    (tmp_path / "wide.png").write_bytes(png(60, 2))
    provider = FakeProvider(
        [_read("wide.png"), Reply([Part.Text("too big")])], image_limits=ImageLimits(10_000, 50)
    )
    h = await Harness.create(
        tmp_path, provider=provider, catalog=fake_catalog(images=True), tools=[ReadTool(tmp_path)]
    )
    await h.send("look")
    await h.settle()
    outcome = _outcome(h)
    assert isinstance(outcome, Outcome.Err)
    assert "image 1 (image/png 60x2, " in outcome.error
    assert (
        "is over what the model fake takes (at most 10000 bytes and 50 px a side)" in outcome.error
    )
    assert "τ does not resize images" in outcome.error
    await h.close()


@scenario
async def test_a_provider_without_image_limits_sends_none(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(PICTURE)
    provider = FakeProvider([_read("shot.png"), Reply([Part.Text("ok")])])
    h = await Harness.create(
        tmp_path, provider=provider, catalog=fake_catalog(images=True), tools=[ReadTool(tmp_path)]
    )
    await h.send("look")
    await h.settle()
    outcome = _outcome(h)
    assert isinstance(outcome, Outcome.Err)
    assert "its provider 'fake' sends none (it has no `image_limits`)" in outcome.error
    await h.close()


@scenario
async def test_a_walk_holding_an_image_fails_early_on_a_model_without_images(
    tmp_path: Path,
) -> None:
    """(b) At the send: a branch moved to a model that cannot see fails before any request."""
    (tmp_path / "shot.png").write_bytes(PICTURE)
    provider = FakeProvider([_read("shot.png"), Reply([Part.Text("seen")])], image_limits=LIMITS)
    h = await Harness.create(
        tmp_path, provider=provider, catalog=sighted_and_blind(), tools=[ReadTool(tmp_path)]
    )
    await h.send("look")
    await h.settle()
    await h.run("model", {"model": "blind"})
    await h.settle()
    results = next(n for n in h.nodes() if isinstance(n.body, Body.Results))
    await h.send("and again")
    await h.settle()
    assert len(provider.requests) == 2, "no request was made"
    ending = (await h.view()).ending
    assert ending == Ending.Failed(
        f"node {results.id} holds 1 image the request cannot carry: the model blind does not "
        "take images (its config has no `images: true`; set it if the model can see). Go back "
        "before it, or switch to a model that takes them"
    )
    await h.close()


def _shot() -> list[str | Image]:
    """Saves the screen."""
    return ["the screen, 40x30", Image(PICTURE)]


async def _async_shot() -> list[str | Image]:
    """Saves the screen, asynchronously."""
    return ["the screen", Image(PICTURE)]


def _not_an_image() -> list[object]:
    """Returns what a tool may not."""
    return ["text", b"bytes"]


@scenario
async def test_a_function_tool_returns_text_and_an_image_in_one_result(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            Reply([Part.Call("c1", "_shot", "{}")], Stop.Tools),
            Reply([Part.Call("c2", "_async_shot", "{}")], Stop.Tools),
            Reply([Part.Text("two screens")]),
        ],
        image_limits=LIMITS,
    )
    h = await Harness.create(
        tmp_path,
        provider=provider,
        catalog=fake_catalog(images=True),
        tools=[_shot, _async_shot],
    )
    await h.send("shoot")
    await h.settle()
    results = [b for b in h.bodies() if isinstance(b, Body.Results)]
    assert results == [
        Body.Results([("c1", Outcome.Ok("the screen, 40x30", [Image(PICTURE)]), Author.Tools())]),
        Body.Results([("c2", Outcome.Ok("the screen", [Image(PICTURE)]), Author.Tools())]),
    ]
    assert len(provider.requests[-1].images) == 2
    await h.close()


@scenario
async def test_a_function_tool_returning_something_else_is_an_error(tmp_path: Path) -> None:
    provider = FakeProvider(
        [Reply([Part.Call("c1", "_not_an_image", "{}")], Stop.Tools), Reply([Part.Text("no")])]
    )
    h = await Harness.create(tmp_path, provider=provider, tools=[_not_an_image])
    await h.send("go")
    await h.settle()
    assert _outcome(h) == Outcome.Err(
        "_not_an_image returned a list holding bytes; a list holds str and Image"
    )
    await h.close()


# What else meets an image (D30 section 9).


class _Probe:
    """Counts the walk when told."""

    name = "probe"

    def __init__(self) -> None:
        self.counted: list[RequestCount] = []
        self.commands = (self.probe,)

    async def probe(self, cursor: Cursor) -> str:
        """Count this walk."""
        self.counted.append(await cursor.count())
        return "counted"


@scenario
async def test_an_image_counts_by_its_pixels_never_by_its_base64(tmp_path: Path) -> None:
    noisy = png(400, 300, lambda x, y: ((x * 7919 + y * 104729) % 251, (x * y) % 253, 9))
    (tmp_path / "noise.png").write_bytes(noisy)
    probe = _Probe()
    provider = FakeProvider([_read("noise.png"), Reply([Part.Text("noise")])], image_limits=LIMITS)
    h = await Harness.create(
        tmp_path,
        provider=provider,
        catalog=fake_catalog(images=True),
        tools=[ReadTool(tmp_path)],
        extensions=[probe],
    )
    await h.send("look")
    await h.settle()
    await h.run("probe")
    results = next(n for n in h.nodes() if isinstance(n.body, Body.Results))
    count = probe.counted[-1].nodes[results.id]
    # 400x300 is one 512-pixel tile: 85 + 170 = 255, more than its 15 x 11 patches.
    assert 255 <= count.upper < 255 + 200, count
    assert len(noisy) > 100_000, "the file is large; its base64 is larger still"
    await h.close()


def _two_image_turns() -> list[Reply]:
    return [
        _read("one.png"),
        Reply([Part.Text("one seen")]),
        _read("two.png"),
        Reply([Part.Text("two seen")]),
        Reply([Part.Text("three")]),
    ]


@scenario
async def test_prune_keeps_only_the_newest_turns_images(tmp_path: Path) -> None:
    """Turn N-1's results become `ok` (its images go); turn N is copied with its image."""
    (tmp_path / "one.png").write_bytes(png(10, 10))
    (tmp_path / "two.png").write_bytes(png(20, 20))
    provider = FakeProvider(_two_image_turns(), image_limits=LIMITS)
    h = await Harness.create(
        tmp_path,
        provider=provider,
        catalog=fake_catalog(images=True),
        tools=[ReadTool(tmp_path)],
        extensions=[Prune()],
    )
    await h.send("first")
    await h.settle()
    await h.send("second")
    await h.settle()
    await h.run("prune")
    await h.settle()
    new = h.session.tree.walk(h.session.cursor(h.cursor).at)
    results = [n for n in new if isinstance(n.body, Body.Results)]
    assert [_first(r) for r in results] == [
        Outcome.Ok("ok"),
        Outcome.Ok("[image two.png: image/png 20x20, 81 bytes]", [Image(png(20, 20))]),
    ]
    assert results[1].copied_from is not None
    await h.send("third")
    await h.settle()
    assert provider.requests[-1].images == (Image(png(20, 20)),)
    assert provider.requests[-1].images[0].data == png(20, 20)
    await h.close()


@scenario
async def test_compaction_shows_the_summarizer_the_images_and_copies_the_tails(
    tmp_path: Path,
) -> None:
    (tmp_path / "one.png").write_bytes(png(10, 10))
    (tmp_path / "two.png").write_bytes(png(20, 20))
    summary = Reply([Part.Text("Goal: look at two pictures.")])
    provider = FakeProvider(
        [
            _read("one.png"),
            Reply([Part.Text("one seen " + "a" * 4000)]),
            _read("two.png"),
            Reply([Part.Text("two seen")]),
            summary,
            Reply([Part.Text("after")]),
        ],
        image_limits=LIMITS,
    )
    models = {"fake": {"model": FAKE_MODEL, "provider": FAKE_PROVIDER, "images": True}}
    catalog = read_models(
        cli={"models": models, "extensions": {"compaction": {"reserve": 2000, "keep": 2000}}}
    )
    h = await Harness.create(
        tmp_path,
        provider=provider,
        catalog=catalog,
        tools=[ReadTool(tmp_path)],
        extensions=[Compaction()],
    )
    await h.send("first " + "y" * 4000)
    await h.settle()
    await h.send("second")
    await h.settle()
    result = await h.run("compact")
    await h.settle()
    assert result is not None
    assert result.startswith("compacted"), result
    summarizer = provider.requests[4]
    assert [i.width for i in summarizer.images] == [10, 20], "it sees every image to summarize"
    assert all(i.data is not None for i in summarizer.images)
    new = h.session.tree.walk(h.session.cursor(h.cursor).at)
    kept = [n for n in new if isinstance(n.body, Body.Results)]
    assert [(n.copied_from is not None, [i.width for i in _first(n).images]) for n in kept] == [
        (True, [20])
    ], "the head's image went with the head; the tail's is copied"
    await h.send("and now")
    await h.settle()
    assert [i.width for i in provider.requests[-1].images] == [20]
    await h.close()


class _Again:
    """A tool object that answers with an image it was handed: one taken from a node loaded from
    disk, so it carries no bytes."""

    def __init__(self) -> None:
        self.name = "again"
        self.description = "Show the earlier picture again."
        self.parameters: dict[str, object] = {"type": "object", "properties": {}}
        self.image: Image | None = None

    async def call(self, args_raw: str) -> Outcome:
        assert self.image is not None
        return Outcome.Ok("the earlier picture", [self.image])


@scenario
async def test_a_tool_may_return_an_image_from_an_earlier_node_without_its_bytes(
    tmp_path: Path,
) -> None:
    """The session holds the bytes, so the image is checked and sent with them: no false
    "has no bytes" refusal."""
    (tmp_path / "shot.png").write_bytes(PICTURE)
    first = FakeProvider([_read("shot.png"), Reply([Part.Text("seen")])], image_limits=LIMITS)
    again = _Again()
    h = await Harness.create(
        tmp_path,
        provider=first,
        catalog=fake_catalog(images=True),
        tools=[ReadTool(tmp_path), again],
    )
    await h.send("look")
    await h.settle()
    await h.close()

    second = FakeProvider(
        [Reply([Part.Call("c2", "again", "{}")], Stop.Tools), Reply([Part.Text("again")])],
        image_limits=LIMITS,
    )
    h = await Harness.load(
        tmp_path, provider=second, catalog=fake_catalog(images=True), tools=[again]
    )
    loaded = _first(next(n for n in h.nodes() if isinstance(n.body, Body.Results)))
    assert loaded.images[0].data is None
    again.image = loaded.images[0]
    await h.send("show it again")
    await h.settle()
    assert _outcome(h) == Outcome.Ok("the earlier picture", [Image(PICTURE)])
    assert [i.data for i in second.requests[-1].images] == [PICTURE, PICTURE]
    await h.close()
