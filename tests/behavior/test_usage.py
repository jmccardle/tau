"""`usage` (D24 section 3): the status line after each reply, `/usage`, and the cache notice.

The prices are the ones a user would write: USD per million tokens. A reply is priced by the
model its branch was on at that node, and money is exact."""

import json
from decimal import Decimal
from pathlib import Path

from fakes import FAKE_PROVIDER, FakeProvider, Reply
from harness import Harness, scenario

from tau import (
    ConfigError,
    Cursor,
    Model,
    ModelCatalog,
    Part,
    Stop,
    Subject,
    TreeHandle,
    Usage,
    model_config,
    read_models,
)

PRICE = {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}


def catalog(**models: dict[str, object]) -> ModelCatalog:
    """Models on the fake provider: `name=settings` (a `price`, a `context_window`)."""
    return read_models(
        cli={
            "models": {
                name: {"model": name, "provider": FAKE_PROVIDER, **settings}
                for name, settings in models.items()
            }
        }
    )


def reply(
    input: int | None = None,
    output: int = 0,
    cached: int | None = None,
    written: int | None = None,
    text: str = "ok",
) -> Reply:
    """A reply whose provider reported these counts (`input=None`: it reported no usage)."""
    meta = None
    if input is not None:
        usage = {"input": input, "output": output}
        if cached is not None:
            usage["cached"] = cached
        if written is not None:
            usage["written"] = written
        meta = json.dumps({"usage": usage})
    return Reply([Part.Text(text)], Stop.End, meta=meta)


async def session(
    tmp_path: Path, replies: list[Reply], cat: ModelCatalog, model: str = "a"
) -> Harness:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(replies),
        extensions=[Usage()],
        catalog=cat,
        config=model_config(model),
    )
    return h


async def say(h: Harness, n: int = 1) -> None:
    for i in range(n):
        await h.send(f"go {i}")
        await h.settle()


async def line(h: Harness) -> str:
    """The text of the `usage` line the cursor shows."""
    shown = dict((await h.view()).shown)
    return shown["usage"]


@scenario
async def test_the_status_line_after_a_reply(tmp_path: Path) -> None:
    cat = catalog(a={"price": PRICE, "context_window": 200_000})
    h = await session(tmp_path, [reply(12_300, 812, cached=11_800)], cat)
    await say(h)
    assert await line(h) == "12.3k in · 96% cached · 812 out · $0.017 · ctx 7% · $0.017 session"
    await h.close()


@scenario
async def test_a_model_with_no_price_shows_no_cost_and_none_with_no_window_shows_no_ctx(
    tmp_path: Path,
) -> None:
    h = await session(tmp_path, [reply(900, 40, cached=0)], catalog(a={}))
    await say(h)
    text = await line(h)
    assert text == "900 in · 0% cached · 40 out"
    assert "$" not in text
    assert "ctx" not in text
    await h.close()


@scenario
async def test_a_server_that_reports_no_cache_has_no_cached_share(tmp_path: Path) -> None:
    h = await session(tmp_path, [reply(900, 40)], catalog(a={"context_window": 1000}))
    await say(h)
    assert await line(h) == "900 in · 40 out · ctx 94%"
    await h.close()


@scenario
async def test_a_reply_with_no_usage_says_so_and_costs_nothing(tmp_path: Path) -> None:
    h = await session(tmp_path, [reply()], catalog(a={"price": PRICE}))
    await say(h)
    assert await line(h) == "a reported no usage"
    assert await h.run("usage") == "no replies yet\n1 reply reported no usage and not counted"
    await h.close()


@scenario
async def test_cache_writes_are_priced_at_the_write_price(tmp_path: Path) -> None:
    """1,000 input = 200 read + 300 written + 500 fresh: 500*3 + 200*0.3 + 300*3.75 + 100*15."""
    h = await session(
        tmp_path, [reply(1000, 100, cached=200, written=300)], catalog(a={"price": PRICE})
    )
    await say(h)
    cents = Decimal(500 * 3) + Decimal("0.3") * 200 + Decimal("3.75") * 300 + 100 * 15
    assert cents.scaleb(-6) == Decimal("0.004185")
    assert (await line(h)).split(" · ")[3] == "$0.004"
    table = await h.run("usage")
    assert table is not None
    assert table.splitlines()[1].split() == ["a", "1", "1.0k", "20%", "300", "100", "$0.004"]
    await h.close()


@scenario
async def test_a_total_of_many_replies_is_exact(tmp_path: Path) -> None:
    """Twenty replies of 500 output tokens at 0.15 USD per million: exactly $0.0015, which
    rounds to $0.002. In binary floats 0.15 is a hair under, and the sum shows $0.001."""
    price = {"input": 0, "output": 0.15, "cache_read": 0, "cache_write": 0}
    h = await session(tmp_path, [reply(1000, 500) for _ in range(20)], catalog(a={"price": price}))
    await say(h, 20)
    table = await h.run("usage")
    assert table is not None
    assert table.splitlines()[1].split()[-1] == "$0.002"
    assert (await line(h)).endswith("$0.002 session")
    await h.close()


class Switch:
    """Moves the branch to model `to` after its first reply."""

    name = "switch"

    def __init__(self, to: str) -> None:
        self.to = to
        self.done = False

    async def post_reply(self, cursor: Cursor, subject: Subject) -> str | None:
        if not self.done:
            self.done = True
            cursor.set_model(Model(self.to))
        return None


@scenario
async def test_a_model_change_mid_walk_prices_each_reply_by_its_own_model(tmp_path: Path) -> None:
    cheap = {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}
    dear = {"input": 10, "output": 10, "cache_read": 10, "cache_write": 10}
    cat = catalog(a={"price": cheap}, b={"price": dear})
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(1_000_000, 0), reply(1_000_000, 0)]),
        extensions=[Usage(), Switch("b")],
        catalog=cat,
        config=model_config("a"),
    )
    await say(h, 2)
    table = await h.run("usage")
    assert table is not None
    rows = [r.split() for r in table.splitlines()]
    assert (rows[1][0], rows[1][-1]) == ("a", "$1.00")
    assert (rows[2][0], rows[2][-1]) == ("b", "$10.00")
    assert (rows[3][0], rows[3][-1]) == ("total", "$11.00")
    assert (await line(h)).endswith("$11.00 session")
    await h.close()


@scenario
async def test_the_session_total_marks_a_model_it_could_not_price(tmp_path: Path) -> None:
    cat = catalog(a={"price": PRICE}, b={})
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(1_000_000, 0), reply(1_000, 0)]),
        extensions=[Usage(), Switch("b")],
        catalog=cat,
        config=model_config("a"),
    )
    await say(h, 2)
    assert (await line(h)).endswith("$3.00+ session")
    table = await h.run("usage")
    assert table is not None
    assert table.splitlines()[-1] == "no price, so no cost: b"
    await h.close()


@scenario
async def test_usage_counts_every_cursor_not_just_the_one_asking(tmp_path: Path) -> None:
    h = await session(
        tmp_path, [reply(1_000_000, 0), reply(1_000_000, 0)], catalog(a={"price": PRICE})
    )
    await say(h)
    other = await h.client.open(h.session.cursor(h.cursor).at)
    await h.send("again", other)
    await h.settle()
    table = await h.run("usage", cursor=other)
    assert table is not None
    assert table.splitlines()[1].split()[1] == "2"
    await h.close()


@scenario
async def test_the_price_in_the_config_now_prices_the_whole_session(tmp_path: Path) -> None:
    """Cost is not stored: a driver started on the same session with another price re-prices it."""
    h = await session(tmp_path, [reply(1_000_000, 0)], catalog(a={"price": PRICE}))
    await say(h)
    await h.close()
    dearer = dict(PRICE, input=30)
    again = await Harness.load(tmp_path, extensions=[Usage()], catalog=catalog(a={"price": dearer}))
    table = await again.run("usage")
    assert table is not None
    assert table.splitlines()[1].split()[-1] == "$30.00"
    await again.close()


class OpenAICompatible(FakeProvider):
    """A fake that is called `openai`, the provider a proxy's OpenAI endpoint is driven by."""

    @property
    def name(self) -> str:
        return "openai"


@scenario
async def test_a_cache_miss_soon_after_a_reply_of_the_same_model_tells_the_clients_once(
    tmp_path: Path,
) -> None:
    h = await session(
        tmp_path,
        [reply(5000, 10, cached=0), reply(5000, 10, cached=0), reply(5000, 10, cached=0)],
        catalog(a={}),
    )
    await say(h)
    assert h.notices() == [], "the first reply has no earlier reply to have cached"
    h.clock.advance(60_000)
    await say(h)
    assert len(h.notices()) == 1
    assert (
        h.notices()[0] == "a read nothing from the prompt cache (5.0k tokens sent at full price)."
    )
    h.clock.advance(60_000)
    await say(h)
    assert len(h.notices()) == 1, "once per model"
    await h.close()


@scenario
async def test_no_cache_notice_when_the_miss_is_not_a_miss(tmp_path: Path) -> None:
    """Silence (no `cached`), a small prompt, a cold cache, a hit, and the option off."""
    cases = {
        "unreported": (reply(5000, 1), reply(5000, 1), 1_000, True),
        "small": (reply(4000, 1, cached=0), reply(4095, 1, cached=0), 1_000, True),
        "cold": (reply(5000, 1, cached=0), reply(5000, 1, cached=0), 300_000, True),
        "hit": (reply(5000, 1, cached=0), reply(5000, 1, cached=4000), 1_000, True),
        "off": (reply(5000, 1, cached=0), reply(5000, 1, cached=0), 1_000, False),
    }
    for name, (first, second, wait, on) in cases.items():
        config = model_config("a", None if on else {"usage": {"cache_notice": False}})
        h = await Harness.create(
            tmp_path / name,
            provider=FakeProvider([first, second]),
            extensions=[Usage()],
            catalog=catalog(a={}),
            config=config,
        )
        await say(h)
        h.clock.advance(wait)
        await say(h)
        assert h.notices() == [], name
        await h.close()


@scenario
async def test_a_model_the_catalog_lost_is_marked_not_dropped(tmp_path: Path) -> None:
    h = await session(tmp_path, [reply(1000, 10)], catalog(a={"price": PRICE}))
    await say(h)
    await h.close()
    again = await Harness.load(tmp_path, extensions=[Usage()], catalog=catalog(z={}))
    table = await again.run("usage")
    assert table is not None
    row = table.splitlines()[1]
    assert row.startswith("a (not in config)")
    assert row.endswith("-")
    await again.close()


@scenario
async def test_a_tree_handle_reads_models_by_node_and_name(tmp_path: Path) -> None:
    """`model(at=)` and `resolved_model(name)`, the reads usage and compaction are built on."""
    seen: list[object] = []

    def probe(tree: TreeHandle, node: int) -> str:
        """Reads the model at `node`."""
        seen.append(tree.model(at=node))
        seen.append(tree.resolved_model("b").context_window)
        try:
            tree.model()
        except ValueError as e:
            seen.append(str(e))
        try:
            tree.resolved_model("zz")
        except ConfigError as e:
            seen.append(str(e))
        return "ok"

    class Probe:
        name = "probe"
        commands = (probe,)

    cat = catalog(a={}, b={"context_window": 4096})
    h = await Harness.create(tmp_path, extensions=[Probe()], catalog=cat, config=model_config("a"))
    await h.run("probe", {"node": 0})
    assert seen[0] == "a"
    assert seen[1] == 4096
    assert "at=" in str(seen[2])
    assert "zz" in str(seen[3])
    await h.close()


@scenario
async def test_through_an_openai_endpoint_the_notice_suggests_the_anthropic_provider(
    tmp_path: Path,
) -> None:
    cat = read_models(cli={"models": {"a": {"model": "a", "provider": "openai"}}})
    h = await Harness.create(
        tmp_path,
        provider=OpenAICompatible([reply(5000, 1, cached=0), reply(5000, 1, cached=0)]),
        extensions=[Usage()],
        catalog=cat,
        config=model_config("a"),
    )
    await say(h, 2)
    assert h.notices() == [
        "a read nothing from the prompt cache (5.0k tokens sent at full price). "
        "Through a proxy's OpenAI endpoint nothing marks the cache; "
        'try provider "anthropic".'
    ]
    await h.close()


@scenario
async def test_a_bad_count_in_a_past_reply_never_fails_a_later_step(tmp_path: Path) -> None:
    """Reply 1 says more was read from the cache than was sent. Steps 2 and 3 still succeed,
    `/usage` works and says why reply 1 is not counted, and so does a session reopened later."""
    h = await session(
        tmp_path,
        [reply(10, 1, cached=20), reply(10, 1), reply(10, 1)],
        catalog(a={"price": PRICE}),
    )
    await say(h, 3)
    view = await h.view()
    assert view.hook_error is None
    assert dict(view.shown)["usage"].startswith("10 in · 1 out")
    table = await h.run("usage")
    assert table is not None
    assert table.splitlines()[1].split()[:2] == ["a", "2"]
    assert "1 reply reported no usage and not counted (node 3: meta.usage reports more" in table
    await h.close()
    again = await Harness.load(tmp_path, extensions=[Usage()], catalog=catalog(a={"price": PRICE}))
    reopened = await again.run("usage")
    assert reopened == table
    await again.close()


@scenario
async def test_a_reply_with_a_bad_count_shows_why_on_its_own_line(tmp_path: Path) -> None:
    h = await session(tmp_path, [reply(10, 1, cached=20)], catalog(a={}))
    await say(h)
    assert (await line(h)).startswith("a reported no usage (node 3: meta.usage reports more")
    await h.close()


@scenario
async def test_the_total_row_ends_in_a_plus_when_a_model_could_not_be_priced(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(1_000_000, 0), reply(1_000, 0)]),
        extensions=[Usage(), Switch("b")],
        catalog=catalog(a={"price": PRICE}, b={}),
        config=model_config("a"),
    )
    await say(h, 2)
    table = await h.run("usage")
    assert table is not None
    assert table.splitlines()[3].split()[-1] == "$3.00+"
    await h.close()


@scenario
async def test_money_and_token_edges_round_consistently(tmp_path: Path) -> None:
    """$0.9996 is $1.00 (never $1.000); exactly nothing is $0; 999,950 tokens are 1.0M."""
    price = {"input": 0, "output": 1, "cache_read": 0, "cache_write": 0}
    h = await session(
        tmp_path,
        [reply(999_950, 999_600), reply(5, 0)],
        catalog(a={"price": price}),
    )
    await say(h)
    assert await line(h) == "1.0M in · 999.6k out · $1.00 · $1.00 session"
    await h.close()
    z = await session(tmp_path / "z", [reply(5, 0)], catalog(a={"price": price}))
    await say(z)
    assert await line(z) == "5 in · 0 out · $0 · $0 session"
    await z.close()
