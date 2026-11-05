"""The TUI shows the `usage` extension's line in the status bar and its cache notice as a toast
(D24 section 3; D23 section 2, "Status")."""

import json
from pathlib import Path

from fakes import FAKE_PROVIDER, FakeProvider, Reply
from harness import Harness, scenario
from tuikit import running, shows, type_line, until

from tau import Part, Stop, Usage, model_config, read_models

PRICE = {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}


def reply(input: int, output: int, cached: int) -> Reply:
    usage = {"input": input, "output": output, "cached": cached}
    return Reply([Part.Text("ok")], Stop.End, meta=json.dumps({"usage": usage}))


@scenario
async def test_the_status_bar_shows_the_replys_tokens_cost_and_context(tmp_path: Path) -> None:
    catalog = read_models(
        cli={
            "models": {
                "a": {
                    "model": "a",
                    "provider": FAKE_PROVIDER,
                    "price": PRICE,
                    "context_window": 200_000,
                }
            }
        }
    )
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(12_300, 812, 11_800)]),
        extensions=[Usage()],
        catalog=catalog,
        config=model_config("a"),
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(
            pilot,
            app,
            "usage: 12.3k in · 96% cached · 812 out · $0.017 · ctx 7% · $0.017 session",
        )
    await h.close()


@scenario
async def test_a_cache_miss_is_a_toast(tmp_path: Path) -> None:
    catalog = read_models(
        cli={"models": {"a": {"model": "a", "provider": FAKE_PROVIDER, "price": PRICE}}}
    )
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(5000, 1, 0), reply(5000, 1, 0)]),
        extensions=[Usage()],
        catalog=catalog,
        config=model_config("a"),
    )
    async with running(h, tmp_path) as (app, pilot):
        for asked, line in enumerate(("one", "two"), 1):
            await type_line(pilot, line)
            await pilot.press("enter")
            await until(pilot, lambda n=asked: len(h.provider.requests) == n)
            await shows(pilot, app, "model: a  ·  idle")
            h.clock.advance(1000)
        await until(
            pilot, lambda: any("read nothing from the prompt cache" in t for t in app.toasts)
        )
    await h.close()


@scenario
async def test_the_reply_badge_and_the_status_line_write_a_count_alike(tmp_path: Path) -> None:
    catalog = read_models(cli={"models": {"a": {"model": "a", "provider": FAKE_PROVIDER}}})
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([reply(999_950, 3_400_000_000, 0)]),
        extensions=[Usage()],
        catalog=catalog,
        config=model_config("a"),
    )
    async with running(h, tmp_path) as (app, pilot):
        await type_line(pilot, "go")
        await pilot.press("enter")
        await shows(pilot, app, "usage: 1.0M in · 0% cached · 3.4G out")
        await until(pilot, lambda: bool(app.query(".box-assistant")))
        (box,) = app.query(".box-assistant")
        assert box.border_subtitle == "↑ 1.0M  ↓ 3.4G"
    await h.close()
