"""`ModelConfig`: what a `Config` node's data may say, and what it may never hold."""

import json

import pytest

from tau_next import Config, ConfigError, ModelConfig

URL = "http://localhost:8100/v1"


def test_a_config_round_trips_through_its_node() -> None:
    model = ModelConfig("qwen", URL, "MY_KEY")
    config = model.to_config()
    assert json.loads(config.data) == {"model": "qwen", "base_url": URL, "api_key_env": "MY_KEY"}
    assert ModelConfig.parse(config) == model


def test_the_key_variable_is_optional() -> None:
    config = ModelConfig("qwen", URL).to_config()
    assert json.loads(config.data) == {"model": "qwen", "base_url": URL}
    assert ModelConfig.parse(config).api_key_env is None
    assert ModelConfig.parse(config).api_key({}) is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("not json", "not JSON"),
        ("[]", "must be a JSON object, not list"),
        ("{}", "missing 'model'"),
        (
            f'{{"model": "m", "base_url": "{URL}", "api_key": "sk-x"}}',
            r"unknown config keys \['api_key'\]",
        ),
        (f'{{"model": 3, "base_url": "{URL}"}}', "'model' must be a string, not int"),
        ('{"model": "m"}', "missing 'base_url'"),
        ('{"model": "m", "base_url": "localhost"}', "must be an http"),
        (
            f'{{"model": "m", "base_url": "{URL}", "api_key_env": null}}',
            "'api_key_env' must be a string",
        ),
        (f'{{"model": "", "base_url": "{URL}"}}', "must not be empty"),
    ],
)
def test_invalid_data_is_refused_with_a_clear_error(data: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        ModelConfig.parse(Config(data))


def test_the_key_is_read_from_the_environment_and_an_unset_variable_is_an_error() -> None:
    model = ModelConfig("qwen", URL, "MY_KEY")
    assert model.api_key({"MY_KEY": "sk-1"}) == "sk-1"
    with pytest.raises(ConfigError, match="MY_KEY is not set"):
        model.api_key({})
    with pytest.raises(ConfigError, match="MY_KEY is not set"):
        model.api_key({"MY_KEY": ""})
