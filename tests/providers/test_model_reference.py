"""A session names its model (D13); the driver resolves the name at each sample."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import scenario
from wire import ALIAS, OPENAI, OPENAI_MODEL, OPENAI_URL, catalog, running, sse, sse_sequence

from tau_next import (
    Body,
    Config,
    ConfigError,
    Driver,
    ModelCatalog,
    ModelConfig,
    Session,
    SystemClock,
    model_config,
    read_models,
)

URL = "http://localhost:8100/v1"


def sample_errors(session: Session) -> list[str]:
    out: list[str] = []
    i = 0
    while True:
        try:
            body = session.tree.node(i).body
        except LookupError:
            return out
        if isinstance(body, Body.Custom) and body.kind == "sample_error":
            out.append(body.data)
        i += 1


def test_a_config_names_a_model_and_nothing_else() -> None:
    config = model_config("free_tokens:gpt")
    assert json.loads(config.data) == {"model": "free_tokens:gpt"}


def test_an_empty_name_cannot_be_written() -> None:
    with pytest.raises(ConfigError, match="must not be empty"):
        model_config("")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("", URL), "'model' must not be empty"),
        (("m", "localhost"), "must be an http"),
        (("m", URL, ""), "'api_key_env' must not be empty"),
        (("m", URL, None, "openai", None, 0), "'max_tokens' must be a positive integer"),
        (("m", URL, None, "openai", None, None, ["x"]), "'options' must be an object"),
    ],
)
def test_a_resolved_model_is_validated(args: tuple[Any, ...], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        ModelConfig(*args)


def test_the_key_is_read_from_the_environment_and_an_unset_variable_is_an_error() -> None:
    model = ModelConfig("qwen", URL, "MY_KEY")
    assert model.api_key({"MY_KEY": "sk-1"}) == "sk-1"
    assert ModelConfig("qwen", URL).api_key({}) is None
    with pytest.raises(ConfigError, match="MY_KEY is not set"):
        model.api_key({})
    with pytest.raises(ConfigError, match="MY_KEY is not set"):
        model.api_key({"MY_KEY": ""})


@scenario
async def test_a_name_missing_from_the_catalog_fails_the_sample_naming_it(
    tmp_path: Path,
) -> None:
    server = sse("plain")
    renamed = catalog("other", model="m", base_url=OPENAI_URL)
    async with running(tmp_path / "s", OPENAI.over(server), renamed) as (s, driver, c):
        await driver.client("test").send(c, "hello")
        await driver.rest(c)
        assert not s.cursor(c).auto
        (error,) = sample_errors(s)
        assert ALIAS in error
    assert server.requests == []


def at(url: str) -> ModelCatalog:
    return catalog(ALIAS, model=OPENAI_MODEL, base_url=url)


@scenario
async def test_a_catalog_change_takes_effect_at_the_next_sample(tmp_path: Path) -> None:
    server = sse_sequence("plain", "plain")
    before = at("http://before.test/v1")
    after = at("http://after.test/v1")
    async with running(tmp_path / "s", OPENAI.over(server), before) as (s, driver, c):
        await driver.client("test").send(c, "one")
        await driver.rest(c)
        driver.use_catalog(after)
        await driver.client("test").send(c, "two")
        await driver.rest(c)
        assert sample_errors(s) == []
    assert [r.url.host for r in server.requests] == ["before.test", "after.test"]


@scenario
async def test_the_same_bare_name_in_two_groups_is_told_apart_by_the_node(tmp_path: Path) -> None:
    models = read_models(
        cli={
            "models": {
                "free_tokens": {
                    "api_key_env": "SUBSCRIPTION_KEY",
                    "base_url": "http://free.test/v1",
                    "models": {"gpt": {"model": "gpt-6"}},
                },
                "pay_by_usage": {
                    "api_key_env": "BILLING_KEY",
                    "base_url": "http://pay.test/v1",
                    "models": {"gpt": {"model": "gpt-6"}},
                },
            }
        }
    )
    environ = {"SUBSCRIPTION_KEY": "sub-key", "BILLING_KEY": "bill-key"}
    for group, host, key in (
        ("free_tokens", "free.test", "sub-key"),
        ("pay_by_usage", "pay.test", "bill-key"),
    ):
        server = sse("plain")
        session = Session.create(
            model_config(f"{group}:gpt"), "sys", SystemClock().now(), tmp_path / group
        )
        driver = Driver(session, {"openai": OPENAI.make(server.transport, environ)}, models)
        driver.start()
        c = session.cursors()[0]
        try:
            await driver.client("test").send(c, "hello")
            await driver.rest(c)
        finally:
            await driver.stop()
            session.close()
        (request,) = server.requests
        assert request.url.host == host
        assert request.headers["authorization"] == f"Bearer {key}"


@scenario
async def test_a_config_node_that_is_not_a_model_name_fails_the_sample(tmp_path: Path) -> None:
    shapes = [
        "not json",
        "[]",
        "{}",
        '{"model": 3}',
        '{"model": "llama", "base_url": "http://x/v1"}',
    ]
    for i, data in enumerate(shapes):
        server = sse("plain")
        session = Session.create(Config(data), "sys", SystemClock().now(), tmp_path / str(i))
        driver = Driver(session, {"openai": OPENAI.over(server)}, OPENAI.catalog())
        driver.start()
        c = session.cursors()[0]
        try:
            await driver.client("test").send(c, "hello")
            await driver.rest(c)
            (error,) = sample_errors(session)
            assert error
            assert not session.cursor(c).auto
        finally:
            await driver.stop()
            session.close()
        assert server.requests == []
