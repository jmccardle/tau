"""model.md section 4 I5 and Q11: an append checks types only, so a walk no API could take is
accepted into the tree and fails at the next sample, before any request is made."""

from pathlib import Path

from harness import scenario
from wire import EACH, Dialect, Server, bodies, collect, running, sse_text, walk_of

from tau_next import Author, Body, Outcome, Part, SampleError, Stop

BATCH = Body.Assistant([Part.Call("a", "ta", "{}")], Stop.Tools, None)
WRONG_IDS = Body.Results([("zz", Outcome.Ok("v"), Author.Tools())])


@EACH
@scenario
async def test_a_results_that_names_other_calls_than_its_batch_fails_before_any_request(
    dialect: Dialect,
) -> None:
    server = sse_text(dialect.reply("never"))
    walk = walk_of(Body.User("go"), BATCH, WRONG_IDS)  # the appends are accepted
    items, error = await collect(dialect.over(server), walk)
    assert items == []
    assert isinstance(error, SampleError)
    assert "['zz']" in str(error)
    assert server.requests == []


@EACH
@scenario
async def test_the_driver_records_that_failure_as_a_sample_error_at_the_next_sample(
    dialect: Dialect, tmp_path: Path
) -> None:
    server: Server = sse_text(dialect.reply("never"))
    async with running(tmp_path / "s", dialect.over(server)) as (session, driver, cursor):
        at = session.cursor(cursor).at
        user = session.append(at, Body.User("go"), Author.Core(), 0)
        batch = session.append(user, BATCH, Author.Core(), 0)
        results = session.append(batch, WRONG_IDS, Author.Core(), 0)
        driver.go_to(cursor, results, "test")
        driver.send(cursor, "test", "again")
        await driver.rest(cursor)
        assert server.requests == []
        errors = [
            n.body
            for n in map(session.tree.node, range(len(session.tree)))
            if isinstance(n.body, Body.Custom) and n.body.kind == "sample_error"
        ]
        assert len(errors) == 1
        assert bodies(session, cursor)[-1] == Body.User("again")
