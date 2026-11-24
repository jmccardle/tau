"""Images through the public API (D30): `Image`, `Outcome.Ok(value, images)`, the session's image
directory, `Tree.walk(node, images=True)`, `MissingImage`, forks and read copies, and a log the
v1 store wrote opening on this build."""

import shutil
from pathlib import Path

import pytest
from pngs import png

from tau import (
    Author,
    BadImage,
    Body,
    Config,
    Image,
    MissingImage,
    Node,
    Outcome,
    Part,
    Session,
    Stop,
    Version,
)

PICTURE = png(5, 4)
V1 = Path(__file__).parents[1] / "fixtures" / "sessions" / "v1"


def batch(session: Session, images: list[Image]) -> int:
    """A batch and its `Results`, holding `images`, under cursor 0's node; the results' id."""
    at = session.cursor(0).at
    call = session.append(
        at, Body.Assistant([Part.Call("c1", "read", "{}")], Stop.Tools), Author.Provider(), 1
    )
    outcome = Outcome.Ok("[image]", images)
    return session.append(call, Body.Results([("c1", outcome, Author.Tools())]), Author.Core(), 2)


def images_of(node: Node) -> list[Image]:
    assert isinstance(node.body, Body.Results)
    outcome = node.body.outcomes[0][1]
    assert isinstance(outcome, Outcome.Ok)
    return outcome.images


def test_an_image_is_its_header_and_its_hash() -> None:
    image = Image(PICTURE)
    assert (image.mime, image.width, image.height) == ("image/png", 5, 4)
    assert len(image.sha256) == 64
    assert image.data == PICTURE
    assert Outcome.Ok("x") == Outcome.Ok("x", [])
    with pytest.raises(BadImage, match="not a PNG, JPEG, GIF or WebP"):
        Image(b"plain text")


def test_from_path_reads_the_file_now(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(PICTURE)
    image = Image.from_path(tmp_path / "a.png")
    (tmp_path / "a.png").unlink()
    assert image == Image(PICTURE)
    assert image.data == PICTURE
    with pytest.raises(FileNotFoundError):
        Image.from_path(tmp_path / "a.png")


def test_a_session_keeps_the_bytes_beside_its_log_and_reads_them_back(tmp_path: Path) -> None:
    session = Session.create(Config("{}"), "sys", 0, tmp_path / "s")
    node = batch(session, [Image(PICTURE)])
    assert (tmp_path / "s" / "images" / Image(PICTURE).sha256).read_bytes() == PICTURE
    session.close()

    loaded = Session.load(tmp_path / "s")
    plain = images_of(loaded.tree.node(node))
    assert plain == [Image(PICTURE)]
    assert plain[0].data is None
    walked = loaded.tree.walk(node, images=True)[-1]
    assert images_of(walked)[0].data == PICTURE
    read_copy = Session.read(tmp_path / "s")
    assert images_of(read_copy.tree.walk(node, images=True)[-1])[0].data == PICTURE
    loaded.close()


def test_a_reference_the_session_does_not_keep_is_refused_before_it_is_written(
    tmp_path: Path,
) -> None:
    source = Session.create(Config("{}"), "sys", 0, tmp_path / "a")
    node = batch(source, [Image(PICTURE)])
    source.close()
    reference = images_of(Session.load(tmp_path / "a").tree.node(node))[0]
    assert reference.data is None

    other = Session.create(Config("{}"), "sys", 0)
    before = len(other.tree)
    with pytest.raises(MissingImage, match=reference.sha256):
        batch(other, [reference])
    assert len(other.tree) == before + 1, "only the batch: its results were refused"


def test_a_copy_within_the_session_needs_no_bytes(tmp_path: Path) -> None:
    session = Session.create(Config("{}"), "sys", 0, tmp_path / "s")
    node = batch(session, [Image(PICTURE)])
    session.close()
    session = Session.load(tmp_path / "s")
    copy = session.append(node, session.tree.node(node).body, Author.Core(), 3, copied_from=node)
    assert images_of(session.tree.walk(copy, images=True)[-1])[0].data == PICTURE
    session.close()


def test_in_memory_the_nodes_hold_the_bytes() -> None:
    session = Session.create(Config("{}"), "sys", 0)
    node = batch(session, [Image(PICTURE)])
    assert images_of(session.tree.walk(node, images=True)[-1])[0].data == PICTURE


def test_a_fork_takes_its_images_with_it(tmp_path: Path) -> None:
    session = Session.create(Config("{}"), "sys", 0, tmp_path / "s")
    node = batch(session, [Image(PICTURE)])
    session.close()
    loaded = Session.load(tmp_path / "s")
    fork = loaded.fork(node, 9, tmp_path / "f")
    loaded.close()
    shutil.rmtree(tmp_path / "s")
    tip = fork.cursor(0).at
    assert images_of(fork.tree.walk(tip, images=True)[-1])[0].data == PICTURE


def test_a_missing_image_file_is_named_when_the_walk_is_read(tmp_path: Path) -> None:
    session = Session.create(Config("{}"), "sys", 0, tmp_path / "s")
    node = batch(session, [Image(PICTURE)])
    session.close()
    shutil.rmtree(tmp_path / "s" / "images")
    loaded = Session.load(tmp_path / "s")
    assert loaded.tree.walk(node)[-1].id == node, "without images the walk reads"
    with pytest.raises(MissingImage, match="has no image"):
        loaded.tree.walk(node, images=True)
    loaded.close()


def _version(directory: Path) -> int:
    return int.from_bytes((directory / "log").read_bytes()[8:12], "little")


def test_a_v1_session_opens_on_this_build_and_is_marked_v2_at_its_first_write(
    tmp_path: Path,
) -> None:
    """`tests/fixtures/sessions/v1` was written by the v1 store (tau 0.16, ea0a3dcd)."""
    directory = tmp_path / "v1"
    shutil.copytree(V1, directory)
    copy = Session.read(directory)
    assert _version(directory) == 1, "a read copy writes nothing"
    Session.load(directory).close()
    assert _version(directory) == 1, "opened and closed: nothing written, still 0.16's"
    session = Session.load(directory)
    walk = session.tree.walk(session.cursor(session.label("main")).at)
    assert [type(n.body).__name__ for n in walk] == [
        "Config",
        "System",
        "User",
        "Assistant",
        "Results",
        "Assistant",
    ]
    assert walk == copy.tree.walk(copy.cursor(0).at)
    assert walk[4].body == Body.Results([("call_1", Outcome.Ok("hello\n"), Author.Tools())])
    batch(session, [Image(PICTURE)])  # and it goes on, now with an image
    assert _version(directory) == 2
    session.close()


def test_a_newer_version_is_refused_by_name(tmp_path: Path) -> None:
    directory = tmp_path / "v9"
    shutil.copytree(V1, directory)
    log = bytearray((directory / "log").read_bytes())
    log[8:12] = (9).to_bytes(4, "little")
    (directory / "log").write_bytes(bytes(log))
    with pytest.raises(Version, match="version 9; this build reads 1 to 2"):
        Session.load(directory)


def test_tree_image_gives_a_loaded_image_its_bytes(tmp_path: Path) -> None:
    session = Session.create(Config("{}"), "sys", 0, tmp_path / "s")
    node = batch(session, [Image(PICTURE)])
    session.close()
    loaded = Session.load(tmp_path / "s")
    reference = images_of(loaded.tree.node(node))[0]
    assert reference.data is None
    assert loaded.tree.image(reference).data == PICTURE
    other = Session.create(Config("{}"), "sys", 0)
    with pytest.raises(MissingImage):
        other.tree.image(reference)
    loaded.close()
