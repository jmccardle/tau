"""`usage` (D24 section 3): the status line after each reply, `/usage`, and the cache notice.

The prices are the ones a user would write: USD per million tokens. A reply is priced by the
model its branch was on at that node, and money is exact."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fakes import FAKE_PROVIDER, FakeProvider, FakeTool, Gate, Reply
from harness import Harness, scenario
from shipped import shipped

from tau import (
    Author,
    Body,
    ConfigError,
    Cursor,
    Draft,
    Ending,
    Model,
    ModelCatalog,
    Part,
    Stop,
    Subject,
    TreeHandle,
    check_config,
    model_config,
    read_models,
)

PRICE = {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}

COUNTS = "counts: reported only, with byte ceilings for what is not sent yet"


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
        extensions=[shipped("usage")],
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
    cat = catalog(a={"usage": {"price": PRICE}, "context_window": 200_000})
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
async def test_a_reply_with_no_usage_says_so_and_is_not_in_the_totals(tmp_path: Path) -> None:
    h = await session(tmp_path, [reply()], catalog(a={"usage": {"price": PRICE}}))
    await say(h)
    assert await line(h) == "a reported no usage"
    assert await h.run("usage") == (
        "no replies yet\n1 reply reported no usage and not counted\n"
        "~<$0.001 estimated for 1 reply that reported no usage "
        "(counted toward max_usd, not in the totals above)\n" + COUNTS
    )
    await h.close()


@scenario
async def test_cache_writes_are_priced_at_the_write_price(tmp_path: Path) -> None:
    """1,000 input = 200 read + 300 written + 500 fresh: 500*3 + 200*0.3 + 300*3.75 + 100*15."""
    h = await session(
        tmp_path,
        [reply(1000, 100, cached=200, written=300)],
        catalog(a={"usage": {"price": PRICE}}),
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
    h = await session(
        tmp_path, [reply(1000, 500) for _ in range(20)], catalog(a={"usage": {"price": price}})
    )
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
    cat = catalog(a={"usage": {"price": cheap}}, b={"usage": {"price": dear}})
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(1_000_000, 0), reply(1_000_000, 0)]),
        extensions=[shipped("usage"), Switch("b")],
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
    cat = catalog(a={"usage": {"price": PRICE}}, b={})
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(1_000_000, 0), reply(1_000, 0)]),
        extensions=[shipped("usage"), Switch("b")],
        catalog=cat,
        config=model_config("a"),
    )
    await say(h, 2)
    assert (await line(h)).endswith("$3.00+ session")
    table = await h.run("usage")
    assert table is not None
    assert table.splitlines()[-2:] == ["no price, so no cost: b", COUNTS]
    await h.close()


@scenario
async def test_usage_counts_every_cursor_not_just_the_one_asking(tmp_path: Path) -> None:
    h = await session(
        tmp_path, [reply(1_000_000, 0), reply(1_000_000, 0)], catalog(a={"usage": {"price": PRICE}})
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
    h = await session(tmp_path, [reply(1_000_000, 0)], catalog(a={"usage": {"price": PRICE}}))
    await say(h)
    await h.close()
    dearer = dict(PRICE, input=30)
    again = await Harness.load(
        tmp_path, extensions=[shipped("usage")], catalog=catalog(a={"usage": {"price": dearer}})
    )
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
        catalog(a={"usage": {"price": PRICE}}),
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
            extensions=[shipped("usage")],
            catalog=catalog(a={"usage": {"price": PRICE}}),
            config=config,
        )
        await say(h)
        h.clock.advance(wait)
        await say(h)
        assert h.notices() == [], name
        await h.close()


@scenario
async def test_a_model_the_catalog_lost_is_marked_not_dropped(tmp_path: Path) -> None:
    h = await session(tmp_path, [reply(1000, 10)], catalog(a={"usage": {"price": PRICE}}))
    await say(h)
    await h.close()
    again = await Harness.load(tmp_path, extensions=[shipped("usage")], catalog=catalog(z={}))
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
async def test_the_cache_notice_is_for_a_priced_model_and_never_suggests_another_provider(
    tmp_path: Path,
) -> None:
    for name, settings in {"unpriced": {}, "priced": {"usage": {"price": PRICE}}}.items():
        cat = read_models(cli={"models": {"a": {"model": "a", "provider": "openai", **settings}}})
        h = await Harness.create(
            tmp_path / name,
            provider=OpenAICompatible([reply(5000, 1, cached=0), reply(5000, 1, cached=0)]),
            extensions=[shipped("usage")],
            catalog=cat,
            config=model_config("a"),
        )
        await say(h, 2)
        expected = (
            []
            if name == "unpriced"
            else ["a read nothing from the prompt cache (5.0k tokens sent at full price)."]
        )
        assert h.notices() == expected, name
        await h.close()


@scenario
async def test_an_aborted_reply_is_said_to_be_aborted_and_counted_apart(tmp_path: Path) -> None:
    gate = Gate()
    h = await session(
        tmp_path,
        [Reply([Part.Text("par"), Part.Text("tial")], Stop.End, gate=gate, gate_after=1)],
        catalog(a={"usage": {"price": PRICE}}),
    )
    await h.send("go")
    await h.until(lambda: gate.held == 1)
    await h.abort()
    await h.settle()
    assert await line(h) == "a reply was aborted and reported no usage"
    assert await h.run("usage") == (
        "no replies yet\n1 reply was aborted and reported no usage\n"
        "~<$0.001 estimated for 1 reply that reported no usage "
        "(counted toward max_usd, not in the totals above)\n" + COUNTS
    )
    await h.close()


@scenario
async def test_a_usage_the_provider_could_not_read_says_why_on_the_status_line(
    tmp_path: Path,
) -> None:
    sent = {"prompt_tokens": "many", "completion_tokens": 1}
    meta = json.dumps({"usage_raw": sent})
    h = await session(
        tmp_path,
        [Reply([Part.Text("ok")], Stop.End, meta=meta)],
        catalog(a={"usage": {"price": PRICE}}),
    )
    await say(h)
    shown = await line(h)
    assert shown.startswith("a reported no usage (node 3: the provider could not read the usage")
    assert "'many'" in shown
    await h.close()


@scenario
async def test_a_bad_count_in_a_past_reply_never_fails_a_later_step(tmp_path: Path) -> None:
    """Reply 1 says more was read from the cache than was sent. Steps 2 and 3 still succeed,
    `/usage` works and says why reply 1 is not counted, and so does a session reopened later."""
    h = await session(
        tmp_path,
        [reply(10, 1, cached=20), reply(10, 1), reply(10, 1)],
        catalog(a={"usage": {"price": PRICE}}),
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
    again = await Harness.load(
        tmp_path, extensions=[shipped("usage")], catalog=catalog(a={"usage": {"price": PRICE}})
    )
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
        extensions=[shipped("usage"), Switch("b")],
        catalog=catalog(a={"usage": {"price": PRICE}}, b={}),
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
        catalog(a={"usage": {"price": price}}),
    )
    await say(h)
    assert await line(h) == "1.0M in · 999.6k out · $1.00 · $1.00 session"
    await h.close()
    z = await session(tmp_path / "z", [reply(5, 0)], catalog(a={"usage": {"price": price}}))
    await say(z)
    assert await line(z) == "5 in · 0 out · $0 · $0 session"
    await z.close()


@scenario
async def test_hour_long_cache_writes_are_priced_at_their_own_price(tmp_path: Path) -> None:
    """D26 section 2: 1,000 input = 200 read + 300 written (100 of them for an hour) + 500 fresh,
    and the hour's write costs 6 per million, not the five minutes' 3.75."""
    usage = {"input": 1000, "output": 100, "cached": 200, "written": 300, "written_1h": 100}
    script = [Reply([Part.Text("ok")], Stop.End, meta=json.dumps({"usage": usage}))]
    price = {**PRICE, "cache_write_1h": 6}
    h = await session(tmp_path, script, catalog(a={"usage": {"price": price}}))
    await say(h)
    spent = Decimal(500 * 3) + Decimal("0.3") * 200 + Decimal("3.75") * 200 + 6 * 100 + 100 * 15
    assert spent.scaleb(-6) == Decimal("0.00441")
    assert (await line(h)).split(" · ")[3] == "$0.004"
    await h.close()


@scenario
async def test_an_hour_long_write_costs_a_write_when_the_price_names_no_other(
    tmp_path: Path,
) -> None:
    usage = {"input": 2_000_000, "output": 0, "written": 2_000_000, "written_1h": 2_000_000}
    script = [Reply([Part.Text("ok")], Stop.End, meta=json.dumps({"usage": usage}))]
    h = await session(tmp_path, script, catalog(a={"usage": {"price": PRICE}}))
    await say(h)
    assert "$7.50" in await line(h), "two million at 3.75, the write price"
    await h.close()


@scenario
async def test_an_hour_long_write_at_its_own_price_shows_in_the_total(tmp_path: Path) -> None:
    usage = {"input": 2_000_000, "output": 0, "written": 2_000_000, "written_1h": 2_000_000}
    script = [Reply([Part.Text("ok")], Stop.End, meta=json.dumps({"usage": usage}))]
    h = await session(
        tmp_path, script, catalog(a={"usage": {"price": {**PRICE, "cache_write_1h": 6}}})
    )
    await say(h)
    assert "$12.00" in await line(h)
    await h.close()


def test_a_price_may_name_the_hour_long_write_and_nothing_else_new() -> None:
    def problems(price: dict[str, object]) -> list[str]:
        found = check_config(
            cli={"models": {"a": {"model": "a", "provider": "fake", "usage": {"price": price}}}},
            providers={"fake": FakeProvider([])},  # pyright: ignore[reportArgumentType]
            extensions=[shipped("usage")],
        )
        return [p.message for p in found if p.severity == "error"]

    assert problems({**PRICE, "cache_write_1h": 6}) == []
    assert any("unknown keys" in m for m in problems({**PRICE, "cache_write_2h": 1}))
    assert any("cache_write_1h" in m for m in problems({**PRICE, "cache_write_1h": -1}))


@scenario
async def test_more_written_for_an_hour_than_written_is_a_usage_that_cannot_be_read(
    tmp_path: Path,
) -> None:
    usage = {"input": 1000, "output": 1, "written": 10, "written_1h": 11}
    script = [Reply([Part.Text("ok")], Stop.End, meta=json.dumps({"usage": usage}))]
    h = await session(tmp_path, script, catalog(a={}))
    await say(h)
    assert "more tokens written for an hour than written" in await line(h)
    await h.close()


def served(window: int) -> Reply:
    """A reply whose server said its window (`meta.server.n_ctx`, from llama.cpp's `/props`)."""
    meta = {"usage": {"input": 10, "output": 1}, "server": {"n_ctx": window}}
    return Reply([Part.Text("ok")], Stop.End, meta=json.dumps(meta))


@scenario
async def test_usage_says_when_the_server_reports_another_window_than_the_config(
    tmp_path: Path,
) -> None:
    """D26 section 2: `context_window` is the config's alone; llama.cpp's `n_ctx` is only said."""
    h = await session(tmp_path, [served(32_768)], catalog(a={"context_window": 40_960}))
    await say(h)
    text = await h.run("usage")
    assert text is not None
    note = "a: the server reports a window of 32768 tokens; the config says 40960 (context_window)"
    assert text.splitlines()[-2:] == [note, COUNTS]
    await h.close()


@scenario
async def test_usage_says_when_the_server_reports_a_window_and_the_config_sets_none(
    tmp_path: Path,
) -> None:
    h = await session(tmp_path, [served(32_768)], catalog(a={}))
    await say(h)
    text = await h.run("usage")
    assert text is not None
    assert "a: the server reports a window of 32768 tokens; the config sets none" in text
    await h.close()


@scenario
async def test_usage_says_nothing_of_a_window_that_agrees(tmp_path: Path) -> None:
    h = await session(tmp_path, [served(40_960)], catalog(a={"context_window": 40_960}))
    await say(h)
    text = await h.run("usage")
    assert text is not None
    assert "window" not in text
    await h.close()


class Answers:
    """Answers the first sample itself, as an extension, so the provider is never asked."""

    name = "answers"

    def __init__(self) -> None:
        self.first = True

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> None:
        if self.first:
            self.first = False
            body = Body.Assistant([Part.Text("canned")], Stop.End)
            subject.draft = Draft(body, [], Author.Extension(self.name))


@scenario
async def test_a_reply_an_extension_answered_is_not_a_sample_that_reported_no_usage(
    tmp_path: Path,
) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(1000, 100)]),
        extensions=[shipped("usage"), Answers()],
        catalog=catalog(a={"usage": {"price": PRICE}}),
        config=model_config("a"),
    )
    await say(h)
    assert "usage" not in dict((await h.view()).shown), "no status line for a reply never billed"
    assert await h.run("usage") == "no replies yet\n" + COUNTS
    await say(h)
    table = await h.run("usage")
    assert table is not None
    assert table.splitlines()[1].split()[:2] == ["a", "1"], "only the provider's reply counts"
    assert "reported no usage" not in table
    await h.close()


# The budget: `max_usd`

DOLLAR = {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}  # per million tokens


def dollar(text: str = "ok") -> Reply:
    """A reply that cost exactly $1 at the `DOLLAR` price."""
    return reply(1_000_000, 0, text=text)


@scenario
async def test_with_no_max_usd_nothing_is_limited(tmp_path: Path) -> None:
    h = await session(
        tmp_path, [dollar() for _ in range(3)], catalog(a={"usage": {"price": DOLLAR}})
    )
    await say(h, 3)
    assert h.notices() == []
    assert (await h.view()).ending == Ending.Done()
    await h.close()


@scenario
async def test_new_input_is_refused_once_the_budget_is_spent(tmp_path: Path) -> None:
    provider = FakeProvider([dollar() for _ in range(3)])
    h = await Harness.create(
        tmp_path,
        provider=provider,
        extensions=[shipped("usage")],
        catalog=catalog(a={"usage": {"price": DOLLAR}}),
        config=model_config("a"),
    )
    await h.configure("usage", {"max_usd": 1.5})
    await say(h, 2)  # $1.00 is under $1.50, so the second goes through: $2.00 spent
    assert len(provider.requests) == 2
    assert h.notices() == []
    await say(h)
    assert len(provider.requests) == 2, "the third prompt never reached the provider"
    assert any("budget: $2.00 of $1.50" in n for n in h.notices())
    await h.close()


@scenario
async def test_a_tool_loop_halts_at_the_next_sample_once_spend_reached_the_limit(
    tmp_path: Path,
) -> None:
    call = Reply([Part.Call("c", "read", "{}")], Stop.Tools, meta=dollar().meta)
    tool = FakeTool("read", "r")
    provider = FakeProvider([call, call, dollar()])
    h = await Harness.create(
        tmp_path,
        provider=provider,
        tools=[tool],
        extensions=[shipped("usage")],
        catalog=catalog(a={"usage": {"price": DOLLAR}}),
        config=model_config("a"),
    )
    await h.configure("usage", {"max_usd": 0.5})
    await say(h)
    assert len(provider.requests) == 1, "the first reply cost $1: no second sample"
    assert len(tool.calls) == 1, "the tools of that reply still ran"
    assert (await h.view()).ending == Ending.Halted("budget: $1.00 of $0.50")
    await h.close()


@scenario
async def test_raising_max_usd_lets_the_user_go_on_and_resume_a_halted_loop(
    tmp_path: Path,
) -> None:
    call = Reply([Part.Call("c", "read", "{}")], Stop.Tools, meta=dollar().meta)
    tool = FakeTool("read", "r")
    provider = FakeProvider([call, dollar("done"), dollar("again")])
    h = await Harness.create(
        tmp_path,
        provider=provider,
        tools=[tool],
        extensions=[shipped("usage")],
        catalog=catalog(a={"usage": {"price": DOLLAR}}),
        config=model_config("a"),
    )
    await h.configure("usage", {"max_usd": 1})
    await say(h)
    assert (await h.view()).ending == Ending.Halted("budget: $1.00 of $1.00")
    assert len(provider.requests) == 1
    await h.configure("usage", {"max_usd": 3})
    await h.set_auto(True)  # resume the halted loop: the tool result goes to the model
    await h.settle()
    assert len(provider.requests) == 2, "the loop went on"
    assert (await h.view()).ending == Ending.Done()
    await say(h)  # $2.00 of $3.00: new input is taken again
    assert len(provider.requests) == 3
    await h.close()


@scenario
async def test_a_model_with_no_price_cannot_be_held_to_a_budget_and_says_what_to_do(
    tmp_path: Path,
) -> None:
    h = await session(tmp_path, [reply(10, 10)], catalog(a={}))
    await h.configure("usage", {"max_usd": 5})
    await say(h)
    assert h.provider.requests == [], "nothing was sampled: its cost cannot be known"
    assert h.notices() == ["budget: no price for model a, set its price or unset usage.max_usd"]
    await h.close()


@scenario
async def test_a_reply_of_a_model_with_no_price_is_never_counted_as_free(tmp_path: Path) -> None:
    cat = catalog(a={}, b={"usage": {"price": DOLLAR}})
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(10, 10), dollar()]),
        extensions=[shipped("usage"), Switch("b")],
        catalog=cat,
        config=model_config("a"),
    )
    await say(h)  # the first reply is a's, unpriced; no budget yet
    await h.configure("usage", {"max_usd": 5})
    await say(h)
    assert len(h.provider.requests) == 1
    assert any("no price for model a" in n for n in h.notices())
    await h.close()


BIG = "x" * 40_000  # about 40,000 tokens at one per byte: $0.04 at DOLLAR


@scenario
async def test_a_finished_reply_with_no_usage_is_estimated_for_the_budget(tmp_path: Path) -> None:
    provider = FakeProvider([reply(text=BIG), reply(text="ok")])
    h = await Harness.create(
        tmp_path,
        provider=provider,
        extensions=[shipped("usage")],
        catalog=catalog(a={"usage": {"price": DOLLAR}}),
        config=model_config("a"),
    )
    await say(h)  # no budget yet: only /usage says it is estimated
    assert h.notices() == []
    table = await h.run("usage")
    assert table is not None
    assert "~$0.040 estimated for 1 reply that reported no usage" in table
    await h.configure("usage", {"max_usd": 0.03})
    await say(h)
    assert len(provider.requests) == 1, "an estimated $0.040 is over $0.03"
    (notice,) = h.notices()
    assert notice.startswith("budget: $0.04 of $0.03")
    await h.configure("usage", {"max_usd": 1})
    await say(h)
    assert len(provider.requests) == 2, "under the raised limit it goes on"
    await h.close()


@scenario
async def test_an_aborted_reply_is_estimated_and_lets_work_continue_under_the_limit(
    tmp_path: Path,
) -> None:
    gate = Gate()
    cut = Reply([Part.Text(BIG), Part.Text("more")], Stop.End, gate=gate, gate_after=1)
    provider = FakeProvider([cut, reply(text="ok")])
    h = await Harness.create(
        tmp_path,
        provider=provider,
        extensions=[shipped("usage")],
        catalog=catalog(a={"usage": {"price": DOLLAR}}),
        config=model_config("a"),
    )
    await h.configure("usage", {"max_usd": 1})
    await h.send("go")
    await h.until(lambda: gate.held == 1)
    await h.abort()
    await h.settle()
    await say(h)
    assert len(provider.requests) == 2, "an aborted reply is estimated, not a lock"
    assert h.notices() == []
    await h.configure("usage", {"max_usd": 0.03})
    await say(h)
    assert len(provider.requests) == 2
    assert h.notices()[-1].startswith("budget: $0.08 of $0.03"), "the second reply too"
    await h.close()


@scenario
async def test_a_later_reply_that_reports_usage_corrects_the_estimate(tmp_path: Path) -> None:
    """A reports 100 in and 10 out. B reports nothing; its text is estimated at 40 tokens. C
    reports 270 in, so the 160 it holds beyond A's 110 are the three nodes between: the two
    messages (estimated 20 each) and B (40), split in proportion, B's share 80. At $1 per output
    token the estimate in dollars is B's output."""
    price = {"input": 0, "output": 1_000_000, "cache_read": 0, "cache_write": 0}
    h = await session(
        tmp_path,
        [reply(100, 10), reply(text="t" * 24), reply(270, 20)],
        catalog(a={"usage": {"price": price}}),
    )

    async def estimated() -> str:
        table = await h.run("usage")
        assert table is not None
        (note,) = [t for t in table.splitlines() if "estimated for" in t]
        return note.split()[0]

    await say(h, 2)
    assert await estimated() == "~$40.00"
    await say(h)
    assert await estimated() == "~$80.00", "C's input says B's output was more"
    await h.close()


@scenario
async def test_with_no_max_usd_a_reply_that_reported_no_usage_limits_nothing(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([reply(), reply(text="ok")])
    h = await Harness.create(
        tmp_path,
        provider=provider,
        extensions=[shipped("usage")],
        catalog=catalog(a={"usage": {"price": DOLLAR}}),
        config=model_config("a"),
    )
    await say(h, 2)
    assert len(provider.requests) == 2
    assert h.notices() == []
    await h.close()


@scenario
async def test_the_budget_line_shows_two_decimals_and_more_only_to_be_nonzero(
    tmp_path: Path,
) -> None:
    h = await session(tmp_path, [reply(1000, 0)], catalog(a={"usage": {"price": DOLLAR}}))
    await h.configure("usage", {"max_usd": 0.0004})
    await say(h, 2)
    assert h.notices() == ["budget: $0.001 of $0.0004"]
    await h.close()


@scenario
async def test_max_usd_is_checked_when_set(tmp_path: Path) -> None:
    h = await session(tmp_path, [], catalog(a={"usage": {"price": DOLLAR}}))
    with pytest.raises(ConfigError):
        await h.configure("usage", {"max_usd": -1})
    with pytest.raises(ConfigError):
        await h.configure("usage", {"max_usd": "lots"})
    await h.close()


@scenario
async def test_usage_json_has_a_row_per_model_with_exact_cost_and_a_total(tmp_path: Path) -> None:
    """CV-02: `/usage json` answers the figures `/usage` tabulates, cost as exact decimal text."""
    cheap = {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}
    cat = catalog(a={"usage": {"price": cheap}}, b={})
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(1_000_000, 500_000, cached=250_000), reply(100, 20)]),
        extensions=[shipped("usage"), Switch("b")],
        catalog=cat,
        config=model_config("a"),
    )
    await say(h, 2)
    text = await h.run("usage", {"format": "json"})
    assert text is not None
    found = json.loads(text)
    assert [r["model"] for r in found["models"]] == ["a", "b"]
    a, b = found["models"]
    assert (a["samples"], a["input"], a["output"], a["cached"], a["cached_input"]) == (
        1,
        1_000_000,
        500_000,
        250_000,
        1_000_000,
    )
    assert a["cost"] == "1.500000"  # 750k fresh + 250k cached + 500k out, at $1 per million
    assert (b["priced"], b["cost"]) == (False, None), "no price is not free"
    assert (found["total"]["samples"], found["total"]["input"]) == (2, 1_000_100)
    assert found["total"]["cost"] == "1.500000"
    assert found["cost_complete"] is False, "b has replied and has no price"
    assert "model" not in found["total"]
    await h.close()


@scenario
async def test_usage_json_counts_replies_that_reported_nothing(tmp_path: Path) -> None:
    h = await session(tmp_path, [reply(), reply(10, 2)], catalog(a={"usage": {"price": PRICE}}))
    await say(h, 2)
    text = await h.run("usage", {"format": "json"})
    assert text is not None
    found = json.loads(text)
    (row,) = found["models"]
    assert (row["samples"], row["silent"], row["estimates"]) == (1, 1, 1)
    assert row["estimated_cost"] is not None, "an estimate is reported apart from the cost"
    assert found["cost_complete"] is True
    await h.close()


@scenario
async def test_usage_json_with_no_replies_is_an_empty_document(tmp_path: Path) -> None:
    h = await session(tmp_path, [], catalog(a={}))
    text = await h.run("usage", {"format": "json"})
    assert text is not None
    found = json.loads(text)
    assert found["models"] == []
    assert (found["total"]["samples"], found["total"]["cost"]) == (0, None)
    await h.close()


@scenario
async def test_usage_defaults_to_the_table_and_refuses_an_unknown_format(tmp_path: Path) -> None:
    h = await session(tmp_path, [reply(10, 2)], catalog(a={}))
    await say(h)
    table = await h.run("usage", {"format": "text"})
    assert table is not None
    assert table.startswith("model")
    with pytest.raises(ValueError, match="format"):
        await h.run("usage", {"format": "yaml"})
    await h.close()


@scenario
async def test_the_usage_json_agrees_with_the_meta_usage_the_tree_holds(tmp_path: Path) -> None:
    """Clients may sum `Assistant.meta.usage` per node (conventions, CV-02): the same figures."""
    h = await session(tmp_path, [reply(120, 30, cached=100), reply(200, 40)], catalog(a={}))
    await say(h, 2)
    metas = [
        json.loads(b.meta)["usage"] for b in h.bodies() if isinstance(b, Body.Assistant) and b.meta
    ]
    text = await h.run("usage", {"format": "json"})
    assert text is not None
    total = json.loads(text)["total"]
    assert total["input"] == sum(m["input"] for m in metas)
    assert total["output"] == sum(m["output"] for m in metas)
    assert total["cached"] == sum(m.get("cached", 0) for m in metas)
    await h.close()
