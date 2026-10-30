"""A session names its model (D13); the driver resolves the name at each sample."""

import json
from pathlib import Path

import pytest
from harness import scenario
from transcripts import BASE_URL, CONFIG, NAME, catalog, running, sse

from tau_next import (
    Body,
    Config,
    ConfigError,
    Driver,
    ModelConfig,
    Session,
    SystemClock,
    model_config,
    model_name,
    read_models,
)
from tau_next.providers.openai import OpenAIProvider

SECRET = "sk-test-0123456789"
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
    assert model_name(config) == "free_tokens:gpt"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("not json", "not JSON"),
        ("[]", "must be a JSON object, not list"),
        ("{}", "missing 'model'"),
        ('{"model": "m", "base_url": "http://x/v1"}', r"unexpected keys \['base_url'\]"),
        ('{"model": 3}', "'model' must be a string, not int"),
        ('{"model": ""}', "must not be empty"),
    ],
)
def test_any_other_shape_is_refused_with_a_clear_error(data: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        model_name(Config(data))


def test_an_empty_name_cannot_be_written() -> None:
    with pytest.raises(ConfigError, match="must not be empty"):
        model_config("")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("", URL), "'model' must not be empty"),
        (("m", "localhost"), "must be an http"),
        (("m", URL, ""), "'api_key_env' must not be empty"),
    ],
)
def test_a_resolved_model_is_validated(args: tuple[str, ...], message: str) -> None:
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
async def test_the_log_holds_the_name_and_no_url_or_key(tmp_path: Path) -> None:
    server = sse("plain")
    directory = tmp_path / "s"
    provider = OpenAIProvider(server.transport, {"K": SECRET})
    async with running(directory, provider, catalog("K")) as (s, driver, c):
        driver.send(c, "test", "hello")
        await driver.rest(c)
        assert s.tree.node(0).body == Body.Config(CONFIG)
    assert json.loads(CONFIG.data) == {"model": NAME}
    assert server.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    for file in directory.rglob("*"):
        if file.is_file():
            text = file.read_bytes()
            for leak in (SECRET, BASE_URL, "llama.test", "api_key", "base_url"):
                assert leak.encode() not in text, f"{leak!r} is in {file}"


@scenario
async def test_a_renamed_model_fails_the_next_sample_naming_it(tmp_path: Path) -> None:
    server = sse("plain")
    renamed = read_models(cli={"models": {"other": {"model": "m", "base_url": BASE_URL}}})
    async with running(tmp_path / "s", OpenAIProvider(server.transport, {}), renamed) as (
        s,
        driver,
        c,
    ):
        driver.send(c, "test", "hello")
        await driver.rest(c)
        assert not s.cursor(c).auto
        assert sample_errors(s) == [f"model '{NAME}' is not in your config"]
    assert server.requests == []


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
        driver = Driver(session, OpenAIProvider(server.transport, environ), models)
        driver.start()
        c = session.cursors()[0]
        try:
            driver.send(c, "test", "hello")
            await driver.rest(c)
        finally:
            await driver.stop()
            session.close()
        (request,) = server.requests
        assert request.url.host == host
        assert request.headers["authorization"] == f"Bearer {key}"


@scenario
async def test_a_config_node_that_is_not_a_model_name_fails_the_sample(tmp_path: Path) -> None:
    for i, data in enumerate(["not json", '{"model": "llama", "base_url": "http://x/v1"}']):
        server = sse("plain")
        session = Session.create(Config(data), "sys", SystemClock().now(), tmp_path / str(i))
        driver = Driver(session, OpenAIProvider(server.transport, {}), catalog())
        driver.start()
        c = session.cursors()[0]
        try:
            driver.send(c, "test", "hello")
            await driver.rest(c)
            (error,) = sample_errors(session)
            assert "config data" in error
            assert not session.cursor(c).auto
        finally:
            await driver.stop()
            session.close()
        assert server.requests == []
