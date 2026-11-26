"""`/clear` (D33) keeps the prompt cache warm: the request after it starts with the bytes the
first request of the session did, through each hosted provider's real request builder.

The new line hangs under the end of the session's system prefix, and what `/clear` writes there (an
undo marker) is a `Custom` node, which no provider sends, so a first message after `/clear` is
the same request, byte for byte, as the same first message in a new session."""

from pathlib import Path

from harness import scenario
from shipped import shipped
from wire import EACH, Dialect, running, sse_texts


@EACH
@scenario
async def test_the_first_message_after_clear_is_the_same_request_as_the_first_of_all(
    dialect: Dialect, tmp_path: Path
) -> None:
    server = sse_texts(dialect.reply("one"), dialect.reply("two"), dialect.reply("three"))
    extensions = [shipped("session"), shipped("undo")]
    async with running(tmp_path / "s", dialect.over(server), extensions=extensions) as (
        _,
        driver,
        c,
    ):
        client = driver.client("test")
        for text in ("hello", "and more"):
            await client.send(c, text)
            await driver.rest(c)
        cleared = await client.run(c, "clear", {})
        assert cleared is not None
        assert cleared.startswith("cleared")
        await driver.rest(c)
        await client.send(c, "hello")
        await driver.rest(c)
        undone = await client.run(c, "undo", {})
        assert undone is not None
        assert undone.startswith("back to node"), "edit-last on the line"
        await driver.rest(c)
    first, _, after = server.requests
    assert after.content == first.content
