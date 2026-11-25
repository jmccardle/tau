"""Images in user messages through the Harness (D31): `send(images=)` makes a `User` node that
holds them, the store keeps them, a sample is handed them with their bytes, and a model that
cannot take them refuses the input (at the call, nothing queued) or halts it (after the
`pre_input` hooks, which `@path` adds images in). Never a silent drop."""

import base64
from pathlib import Path

import pytest
from fakes import FAKE_MODEL, FAKE_PROVIDER, FakeProvider, Reply, fake_catalog
from harness import Harness, scenario
from pngs import png

from tau import (
    Attachments,
    Body,
    Compaction,
    Ending,
    Image,
    ImageInput,
    ImageLimits,
    ItemData,
    Part,
    Prune,
    read_models,
)

LIMITS = ImageLimits(1_000_000, 4000)
PICTURE = png(40, 30)


def say(text: str) -> Reply:
    return Reply([Part.Text(text)])


def _users(h: Harness) -> list[Body.User]:
    return [b for b in h.bodies() if isinstance(b, Body.User)]


@scenario
async def test_send_with_an_image_makes_a_user_node_that_holds_it(tmp_path: Path) -> None:
    provider = FakeProvider([say("a white box")], image_limits=LIMITS)
    h = await Harness.create(tmp_path, provider=provider, catalog=fake_catalog(images=True))
    await h.send("what is this?", images=[ImageInput.of(PICTURE)])
    await h.settle()
    want = Image(PICTURE)
    assert _users(h) == [Body.User("what is this?", [want])]
    assert provider.requests[-1].images == (want,)
    assert provider.requests[-1].images[0].data == PICTURE, "a provider is handed the bytes"
    assert (h.directory / "images" / want.sha256).read_bytes() == PICTURE
    await h.close()


@scenario
async def test_an_image_alone_is_a_message(tmp_path: Path) -> None:
    provider = FakeProvider([say("seen")], image_limits=LIMITS)
    h = await Harness.create(tmp_path, provider=provider, catalog=fake_catalog(images=True))
    await h.send("", images=[ImageInput.of(PICTURE), ImageInput.of(png(2, 2))])
    await h.settle()
    assert _users(h) == [Body.User("", [Image(PICTURE), Image(png(2, 2))])]
    assert len(provider.requests) == 1
    await h.close()


@scenario
async def test_after_a_reload_a_user_image_is_read_from_the_session_and_sent_again(
    tmp_path: Path,
) -> None:
    first = FakeProvider([say("seen")], image_limits=LIMITS)
    h = await Harness.create(tmp_path, provider=first, catalog=fake_catalog(images=True))
    await h.send("look", images=[ImageInput.of(PICTURE)])
    await h.settle()
    await h.close()

    again = FakeProvider([say("still seen")], image_limits=LIMITS)
    h = await Harness.load(tmp_path, provider=again, catalog=fake_catalog(images=True))
    assert _users(h)[0].images[0].data is None, "a node loaded from disk holds the reference"
    await h.send("and now?")
    await h.settle()
    assert [i.data for i in again.requests[-1].images] == [PICTURE]
    await h.close()


@scenario
async def test_a_model_without_images_refuses_the_input_and_queues_nothing(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([], image_limits=LIMITS)
    h = await Harness.create(tmp_path, provider=provider, catalog=fake_catalog())
    before = h.snapshot()
    with pytest.raises(ValueError, match=r"1 image not sent: the model .* does not take images"):
        await h.send("look", images=[ImageInput.of(PICTURE)])
    with pytest.raises(ValueError, match="does not take images"):
        await h.input("look", images=[ImageInput.of(PICTURE)])
    await h.settle()
    assert h.snapshot() == before
    assert provider.requests == []
    assert not (h.directory / "images").exists()
    await h.close()


@scenario
async def test_an_image_over_the_providers_limits_is_refused_saying_so(tmp_path: Path) -> None:
    provider = FakeProvider([], image_limits=ImageLimits(10))
    h = await Harness.create(tmp_path, provider=provider, catalog=fake_catalog(images=True))
    with pytest.raises(ValueError, match="τ does not resize images"):
        await h.send("look", images=[ImageInput.of(PICTURE)])
    await h.close()


@scenario
async def test_an_image_the_session_holds_is_named_by_its_sha256(tmp_path: Path) -> None:
    provider = FakeProvider([say("one"), say("two")], image_limits=LIMITS)
    h = await Harness.create(tmp_path, provider=provider, catalog=fake_catalog(images=True))
    await h.send("look", images=[ImageInput.of(PICTURE)])
    await h.settle()
    sha = Image(PICTURE).sha256
    assert base64.b64decode(await h.client.image(sha)) == PICTURE
    await h.send("again", images=[ImageInput(sha256=sha)])
    await h.settle()
    assert _users(h)[-1] == Body.User("again", [Image(PICTURE)])
    assert [i.data for i in provider.requests[-1].images] == [PICTURE, PICTURE]
    other = Image(png(3, 3)).sha256
    with pytest.raises(LookupError):
        await h.send("not mine", images=[ImageInput(sha256=other)])
    with pytest.raises(LookupError):
        await h.client.image(other)
    await h.close()


@scenario
async def test_more_than_twenty_images_are_refused(tmp_path: Path) -> None:
    provider = FakeProvider([], image_limits=LIMITS)
    h = await Harness.create(tmp_path, provider=provider, catalog=fake_catalog(images=True))
    many = [ImageInput.of(png(1, 1 + i)) for i in range(21)]
    with pytest.raises((ValueError, TypeError), match="20"):
        await h.send("many", images=many)
    await h.close()


@scenario
async def test_an_input_with_images_is_pending_and_can_be_removed(tmp_path: Path) -> None:
    provider = FakeProvider([], image_limits=LIMITS)
    h = await Harness.create(tmp_path, provider=provider, catalog=fake_catalog(images=True))
    item = await h.input("later", images=[ImageInput.of(PICTURE)])
    await h.settle()
    view = await h.view()
    pending = [i for i in view.items if i.id == item]
    assert [i.data.images for i in pending if isinstance(i.data, ItemData.Input)] == [
        [Image(PICTURE)]
    ]
    assert (h.directory / "images" / Image(PICTURE).sha256).exists(), "on disk before the pad"
    await h.client.remove_item(h.cursor, item)
    await h.settle()
    assert (await h.view()).items == ()
    await h.close()


def _attaching(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (work / "shot.png").write_bytes(PICTURE)
    return work


@scenario
async def test_at_path_attaches_an_image_file_as_an_image(tmp_path: Path) -> None:
    work = _attaching(tmp_path)
    provider = FakeProvider([say("seen")], image_limits=LIMITS)
    h = await Harness.create(
        tmp_path / "s",
        provider=provider,
        catalog=fake_catalog(images=True),
        extensions=[Attachments(work)],
    )
    await h.send("what is @shot.png")
    await h.settle()
    assert _users(h) == [
        Body.User("what is @shot.png\n\n[image @shot.png: image/png 40x30]", [Image(PICTURE)])
    ]
    assert h.nodes()[-2].typed == "what is @shot.png"
    assert [i.data for i in provider.requests[-1].images] == [PICTURE]
    await h.close()


@scenario
async def test_at_path_on_a_model_without_images_halts_with_a_notice(tmp_path: Path) -> None:
    work = _attaching(tmp_path)
    provider = FakeProvider([], image_limits=LIMITS)
    h = await Harness.create(
        tmp_path / "s", provider=provider, catalog=fake_catalog(), extensions=[Attachments(work)]
    )
    await h.send("what is @shot.png")
    await h.settle()
    assert _users(h) == []
    assert provider.requests == []
    view = await h.view()
    assert view.items == ()
    assert isinstance(view.ending, Ending.Halted)
    assert "1 image not sent: the model" in view.ending.reason
    assert any("does not take images" in n for n in h.notices())
    await h.close()


@scenario
async def test_prune_keeps_only_the_newest_turns_user_images(tmp_path: Path) -> None:
    """Turn N-1's user image becomes a label; turn N keeps its image."""
    one, two, three = png(10, 10), png(20, 20), png(30, 30)
    provider = FakeProvider(
        [say("one"), say("two"), say("three"), say("after")], image_limits=LIMITS
    )
    h = await Harness.create(
        tmp_path, provider=provider, catalog=fake_catalog(images=True), extensions=[Prune()]
    )
    for text, picture in (("first", one), ("second", two), ("third", three)):
        await h.send(text, images=[ImageInput.of(picture)])
        await h.settle()
    await h.run("prune")
    await h.settle()
    users = _users(h)
    assert users[0].images == [], "turn N-1"
    assert users[0].text.endswith("second\n[image 20x20 image/png]")
    assert "User: first\n[image 10x10 image/png]" in users[0].text, "turns 0 to N-2"
    assert users[1] == Body.User("third", [Image(three)])
    await h.send("and now")
    await h.settle()
    assert [i.width for i in provider.requests[-1].images] == [30]
    await h.close()


@scenario
async def test_compaction_copies_the_tails_user_images(tmp_path: Path) -> None:
    summary = say("Goal: look at two pictures.")
    provider = FakeProvider(
        [say("one seen " + "a" * 4000), say("two seen"), summary, say("after")],
        image_limits=LIMITS,
    )
    models = {"fake": {"model": FAKE_MODEL, "provider": FAKE_PROVIDER, "images": True}}
    catalog = read_models(
        cli={"models": models, "extensions": {"compaction": {"reserve": 2000, "keep": 2000}}}
    )
    h = await Harness.create(
        tmp_path, provider=provider, catalog=catalog, extensions=[Compaction()]
    )
    await h.send("first " + "y" * 4000, images=[ImageInput.of(png(10, 10))])
    await h.settle()
    await h.send("second", images=[ImageInput.of(png(20, 20))])
    await h.settle()
    result = await h.run("compact")
    await h.settle()
    assert result is not None
    assert result.startswith("compacted"), result
    assert [i.width for i in provider.requests[2].images] == [10, 20], "the summarizer sees both"
    new = h.session.tree.walk(h.session.cursor(h.cursor).at)
    kept = [n for n in new if isinstance(n.body, Body.User) and n.body.images]
    assert [(n.copied_from is not None, [i.width for i in n.body.images]) for n in kept] == [
        (True, [20])
    ], "the head's image went with the head; the tail's is copied"
    await h.send("and now")
    await h.settle()
    assert [i.width for i in provider.requests[-1].images] == [20]
    await h.close()
