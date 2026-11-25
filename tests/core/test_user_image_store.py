"""User images through the public API (D31): `Body.User(text, images)`, `Body.images`, an input's
images on disk before the scratchpad names them, the store's version 3 only once a user image is
written, forks, `Tree.image_named`, and `MissingImage` for a reference the session lacks."""

import shutil
from pathlib import Path

import pytest
from pngs import png

from tau import (
    Author,
    Body,
    Config,
    Image,
    ItemData,
    MissingImage,
    Outcome,
    Session,
)

PICTURE = png(5, 4)
ME = Author.Frontend("term")


def _version(directory: Path, name: str = "log") -> int:
    return int.from_bytes((directory / name).read_bytes()[8:12], "little")


def test_a_user_body_holds_its_images_after_its_text() -> None:
    image = Image(PICTURE)
    assert Body.User("hi").images == []
    assert Body.User("hi", [image]).images == [image]
    assert Body.User("hi", [image]) != Body.User("hi")
    ok = Body.Results([("c", Outcome.Ok("x", [image]), Author.Tools())])
    assert ok.images == [image], "Body.images is every body's"
    assert Body.System("s").images == []
    assert ItemData.Input(ME, "hi").images == []


def test_an_input_keeps_its_images_before_the_pad_and_3_only_once_delivered(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "s"
    session = Session.create(Config("{}"), "sys", 0, directory)
    assert _version(directory) == 2, "a new session opens on a 0.17 build"
    session.input(0, ME, "look", [Image(PICTURE)])
    assert (directory / "images" / Image(PICTURE).sha256).read_bytes() == PICTURE
    assert _version(directory, "scratchpad") == 3
    assert _version(directory) == 2, "no record needs 3 yet"
    [item] = session.cursor(0).items
    at = session.cursor(0).at
    session.append(at, Body.User("look", [Image(PICTURE)]), ME, 1)
    assert _version(directory) == 3
    session.close()
    loaded = Session.load(directory)
    [user] = [n for n in loaded.tree.walk(len(loaded.tree) - 1) if n.body.images]
    assert user.body.images[0].data is None, "a reference"
    assert loaded.tree.walk(user.id, images=True)[-1].body.images[0].data == PICTURE
    assert loaded.cursor(0).items[0].id == item.id
    loaded.close()


def test_image_named_finds_a_nodes_or_an_inputs_image_with_its_bytes(tmp_path: Path) -> None:
    session = Session.create(Config("{}"), "sys", 0, tmp_path / "s")
    sha = Image(PICTURE).sha256
    with pytest.raises(MissingImage):
        session.tree.image_named(sha)
    session.input(0, ME, "look", [Image(PICTURE)])
    assert session.tree.image_named(sha).data == PICTURE
    session.close()
    loaded = Session.load(tmp_path / "s")
    assert loaded.tree.image_named(sha) == Image(PICTURE)
    assert loaded.tree.image_named(sha).data == PICTURE
    with pytest.raises(MissingImage):
        loaded.tree.image_named("../../etc/passwd")
    loaded.close()


def test_an_input_naming_an_image_the_session_lacks_is_refused(tmp_path: Path) -> None:
    first = Session.create(Config("{}"), "sys", 0, tmp_path / "a")
    first.append(first.cursor(0).at, Body.User("x", [Image(PICTURE)]), ME, 1)
    first.close()
    loaded = Session.load(tmp_path / "a")
    reference = loaded.tree.node(len(loaded.tree) - 1).body.images[0]
    other = Session.create(Config("{}"), "sys", 0, tmp_path / "b")
    with pytest.raises(MissingImage):
        other.input(0, ME, "x", [reference])
    assert other.cursor(0).items == []
    other.input(0, ME, "x", [loaded.tree.image(reference)])  # with its bytes it goes
    loaded.close()
    other.close()


def test_a_fork_takes_a_users_images_with_it(tmp_path: Path) -> None:
    session = Session.create(Config("{}"), "sys", 0, tmp_path / "s")
    node = session.append(session.cursor(0).at, Body.User("x", [Image(PICTURE)]), ME, 1)
    session.close()
    loaded = Session.load(tmp_path / "s")
    fork = loaded.fork(node, 9, tmp_path / "f")
    loaded.close()
    shutil.rmtree(tmp_path / "s")
    tip = fork.cursor(0).at
    assert fork.tree.walk(tip, images=True)[-1].body.images[0].data == PICTURE
    assert _version(tmp_path / "f") == 3
    fork.close()
