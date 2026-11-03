"""`attachments` (D24 section 5): `@path` in an input names a file, and the `User` node holds what
the model saw. The files are the session's, not the client's: in `remote` mode the client is
another process's view of the same driver, and the path is read where the driver is."""

import os
from pathlib import Path

import pytest
from fakes import FAKE_MODEL, FakeProvider, Reply
from harness import Harness, scenario

from tau import Attachments, Body, Ending, Part, Stop, model_config

DONE = Reply([Part.Text("ok")], Stop.End)


async def sent(tmp_path: Path, line: str, *, limit: int | None = None) -> str:
    """The text of the `User` node that `line` became in a session working in `tmp_path/work`."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    config = None if limit is None else model_config(FAKE_MODEL, {"attachments": {"limit": limit}})
    here = tmp_path / f"run{len(list(tmp_path.glob('run*')))}"  # one session per call
    here.mkdir()
    h = await Harness.create(
        here, provider=FakeProvider([DONE]), extensions=[Attachments(work)], config=config
    )
    await h.send(line)
    await h.settle()
    users = [b.text for b in h.bodies() if isinstance(b, Body.User)]
    await h.close()
    assert len(users) == 1
    return users[0]


def write(tmp_path: Path, name: str, text: str | bytes) -> Path:
    path = tmp_path / "work" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


@scenario
async def test_a_file_is_attached_after_the_line(tmp_path: Path) -> None:
    write(tmp_path, "src/main.rs", "fn main() {}\n")
    assert await sent(tmp_path, "explain @src/main.rs") == (
        'explain @src/main.rs\n\n<attachment path="src/main.rs">\nfn main() {}\n\n</attachment>'
    )


@scenario
async def test_the_session_reads_it_not_the_client(tmp_path: Path) -> None:
    """The path means the session's working directory; this test's own is somewhere else."""
    write(tmp_path, "only_here.txt", "found")
    assert Path.cwd() != tmp_path / "work"
    assert "found" in await sent(tmp_path, "look at @only_here.txt")


@scenario
async def test_an_absolute_path_is_read_as_it_is(tmp_path: Path) -> None:
    path = write(tmp_path, "abs.txt", "absolute")
    text = await sent(tmp_path, f"see @{path}")
    assert f'<attachment path="{path}">' in text


@scenario
async def test_a_file_over_the_limit_is_a_reference_the_model_can_read(tmp_path: Path) -> None:
    path = write(tmp_path, "big.log", "x" * 3000)
    text = await sent(tmp_path, "check @big.log", limit=1024)
    assert text == f'check @big.log\n\n<reference path="{path}" size="2.9 kB" reason="over 1 kB"/>'


@scenario
async def test_the_default_limit_is_ten_kilobytes(tmp_path: Path) -> None:
    write(tmp_path, "fits.txt", "x" * 10240)
    write(tmp_path, "over.txt", "x" * 10241)
    assert "<attachment" in await sent(tmp_path, "@fits.txt")
    assert 'reason="over 10 kB"' in await sent(tmp_path, "@over.txt")


@scenario
async def test_a_file_that_is_not_utf8_is_a_reference(tmp_path: Path) -> None:
    path = write(tmp_path, "blob.bin", b"\xff\xfe\x00bad")
    assert await sent(tmp_path, "@blob.bin") == (
        f'@blob.bin\n\n<reference path="{path}" size="6 B" reason="not UTF-8"/>'
    )


@scenario
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
async def test_a_file_that_cannot_be_read_is_a_reference_saying_why(tmp_path: Path) -> None:
    path = write(tmp_path, "secret.txt", "no")
    path.chmod(0)
    try:
        text = await sent(tmp_path, "@secret.txt")
    finally:
        path.chmod(0o600)
    assert f'<reference path="{path}"' in text
    assert 'reason="unreadable: Permission denied"' in text


@scenario
async def test_a_word_that_names_no_regular_file_stays_text(tmp_path: Path) -> None:
    (tmp_path / "work" / "dir").mkdir(parents=True)
    line = "ask @alice about @missing.txt and @dir and mail bob@example.com"
    assert await sent(tmp_path, line) == line


@scenario
async def test_trailing_punctuation_is_stripped_until_a_file_is_named(tmp_path: Path) -> None:
    write(tmp_path, "notes.txt", "n")
    text = await sent(tmp_path, "(see @notes.txt.)")
    assert text.startswith('(see @notes.txt.)\n\n<attachment path="notes.txt">')


@scenario
async def test_a_file_named_twice_is_attached_once(tmp_path: Path) -> None:
    write(tmp_path, "a.txt", "A")
    write(tmp_path, "b.txt", "B")
    text = await sent(tmp_path, "@a.txt @b.txt @./a.txt")
    assert text.count("<attachment") == 2
    assert text.index('"a.txt"') < text.index('"b.txt"')


@scenario
async def test_an_at_sign_inside_a_word_is_not_a_reference(tmp_path: Path) -> None:
    write(tmp_path, "a.txt", "A")
    line = "x@a.txt"
    assert await sent(tmp_path, line) == line


@scenario
async def test_the_model_is_sent_what_the_node_holds(tmp_path: Path) -> None:
    """I1: the request is the walk, and the walk holds the expanded text."""
    write(tmp_path, "a.txt", "A")
    work = tmp_path / "work"
    h = await Harness.create(
        tmp_path, provider=FakeProvider([DONE]), extensions=[Attachments(work)]
    )
    await h.send("go @a.txt")
    await h.settle()
    (request,) = h.provider.requests
    sent_texts = [
        n.body.text for n in h.nodes() if n.id in request.walk and isinstance(n.body, Body.User)
    ]
    assert sent_texts == ['go @a.txt\n\n<attachment path="a.txt">\nA\n</attachment>']
    await h.close()


@scenario
async def test_a_limit_below_one_fails_the_step_and_names_the_option(tmp_path: Path) -> None:
    write(tmp_path, "a.txt", "A")
    work = tmp_path / "work"
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([DONE]),
        extensions=[Attachments(work)],
        config=model_config(FAKE_MODEL, {"attachments": {"limit": 0}}),
    )
    await h.send("@a.txt")
    await h.settle()
    view = await h.view()
    assert view.hook_error is not None
    assert "at least 1" in view.hook_error.error
    assert isinstance(view.ending, Ending.Failed)
    assert h.provider.requests == []
    await h.close()
