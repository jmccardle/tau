"""`Client.models()` (D32 section 8): the driver's effective catalog as a client may know it, an
allowlist of facts with no secret in it, announced by `Commands` when it changes."""

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FAKE_MODEL, FAKE_PROVIDER, FakeProvider
from harness import Harness, scenario
from shipped import shipped

from tau import (
    Commands,
    ConfigError,
    Event,
    ImageLimits,
    LayerText,
    ModelInfo,
    ModelPrice,
    read_models,
)

SECRETS = ("sk-literal-secret", "SECRET_KEY_VAR", "secret.example", "X-Secret-Header", "hdr-value")
PRICE = {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}
LIMITS = ImageLimits(1_000_000, 4000)


def catalog_of(**more: object) -> dict[str, Any]:
    """A layer of three models on the fake provider: `fake` (the default; priced, a window, a
    cap, images), `plain` (nothing known), and `keyed` (every secret-bearing key set)."""
    return {
        "default_model": FAKE_MODEL,
        "models": {
            FAKE_MODEL: {
                "model": FAKE_MODEL,
                "provider": FAKE_PROVIDER,
                "context_window": 200_000,
                "max_tokens": 8192,
                "images": True,
                "usage": {"price": PRICE},
            },
            "plain": {"model": "plain", "provider": FAKE_PROVIDER},
            "keyed": {
                "model": "keyed",
                "provider": FAKE_PROVIDER,
                "base_url": "https://secret.example/v1",
                "api_key": "sk-literal-secret",
                "options": {"headers": {"X-Secret-Header": "hdr-value"}},
            },
            "by_var": {
                "model": "by_var",
                "provider": FAKE_PROVIDER,
                "api_key_env": "SECRET_KEY_VAR",
            },
        },
        **more,
    }


async def started(
    tmp_path: Path, layer: dict[str, Any] | None = None, *, usage: bool = True
) -> Harness:
    """A harness on `layer` (default: `catalog_of()`), its provider declaring `LIMITS`, with the
    built-in `usage` loaded (it reads the prices) unless `usage` is false."""
    catalog = read_models(cli=layer if layer is not None else catalog_of())
    return await Harness.create(
        tmp_path,
        catalog=catalog,
        provider=FakeProvider([], image_limits=LIMITS),
        extensions=[shipped("usage")] if usage else [],
    )


@scenario
async def test_models_lists_the_catalog_in_config_order_with_its_facts(tmp_path: Path) -> None:
    h = await started(tmp_path)
    models = await h.client.models()
    assert [m.name for m in models] == [FAKE_MODEL, "plain", "keyed", "by_var"]
    assert models[0] == ModelInfo(
        FAKE_MODEL,
        FAKE_PROVIDER,
        200_000,
        8192,
        LIMITS,
        ModelPrice("3", "15", "0.3", "3.75", "3.75"),
        default=True,
    )
    assert models[1] == ModelInfo("plain", FAKE_PROVIDER, None, None, None, None, default=False)
    await h.close()


@scenario
async def test_no_secret_bearing_field_reaches_a_client(tmp_path: Path) -> None:
    h = await started(tmp_path)
    models = await h.client.models()
    fields = {f.name for f in dataclasses.fields(ModelInfo)}
    assert fields == {
        "name",
        "provider",
        "context_window",
        "max_tokens",
        "images",
        "price",
        "default",
    }, "an allowlist: a new field is a decision, made here"
    text = json.dumps([dataclasses.asdict(m) for m in models])
    for secret in SECRETS:
        assert secret not in text, secret
    for gone in ("base_url", "api_key", "api_key_env", "options", "headers", "sections"):
        assert gone not in text, gone
    await h.close()


@scenario
async def test_a_model_that_takes_images_on_a_provider_that_sends_none_lists_no_limits(
    tmp_path: Path,
) -> None:
    catalog = read_models(cli=catalog_of())
    h = await Harness.create(tmp_path, catalog=catalog)  # the fake provider declares no limits
    (fake, *_) = await h.client.models()
    assert fake.images is None, "an image for it is refused, so none is offered"
    await h.close()


@scenario
async def test_models_reflects_every_layer_command_line_last(tmp_path: Path) -> None:
    user = LayerText("user.json", json.dumps(catalog_of()))
    over = {"models": {FAKE_MODEL: {"context_window": 1_000_000, "usage": {"price": {"input": 1}}}}}
    h = await Harness.create(
        tmp_path, catalog=read_models(user, cli=over), extensions=[shipped("usage")]
    )
    (fake, *_) = await h.client.models()
    assert fake.context_window == 1_000_000
    assert fake.price == ModelPrice("1", "15", "0.3", "3.75", "3.75"), "merged by key"
    await h.close()


@scenario
async def test_a_price_is_exact_decimal_text(tmp_path: Path) -> None:
    price = {"input": 0.0000001, "output": 10.50, "cache_read": 0, "cache_write": 1e2}
    layer = {"models": {"m": {"model": "m", "provider": FAKE_PROVIDER, "usage": {"price": price}}}}
    h = await started(tmp_path, layer)
    (model,) = await h.client.models()
    assert model.price == ModelPrice("0.0000001", "10.5", "0", "100", "100")
    assert model.default is False, "no default_model"
    await h.close()


@scenario
async def test_a_price_usage_refuses_is_a_config_error_naming_the_model(tmp_path: Path) -> None:
    layer = {"models": {"m": {"model": "m", "provider": FAKE_PROVIDER, "usage": {"price": 3}}}}
    h = await started(tmp_path, layer)  # a start refuses it; a bare Driver is not checked
    with pytest.raises(ConfigError, match="'m'"):
        await h.client.models()
    await h.close()


@scenario
async def test_without_usage_no_model_is_priced(tmp_path: Path) -> None:
    h = await started(tmp_path, usage=False)
    models = await h.client.models()
    assert [m.price for m in models] == [None] * len(models), "nothing in the session prices"
    assert models[0].context_window == 200_000
    await h.close()


@scenario
async def test_a_reload_that_loads_usage_sends_commands_and_prices(tmp_path: Path) -> None:
    h = await started(tmp_path, usage=False)
    await h.settle()
    before = len(commands_heard(h.heard))
    await h.driver.reload([shipped("usage")])
    await h.settle()
    assert len(commands_heard(h.heard)) == before + 1
    assert (await h.client.models())[0].price is not None
    await h.close()


@scenario
async def test_a_model_completion_is_detailed_from_the_same_facts(tmp_path: Path) -> None:
    h = await started(tmp_path)
    found = {c.value: c.detail for c in await h.client.complete(h.cursor, "model", "")}
    assert found[FAKE_MODEL] == "fake · 200k ctx · images · $3/$15"
    assert found["plain"] == "fake"
    await h.close()


def commands_heard(heard: list[Event]) -> list[Commands]:
    return [e for e in heard if isinstance(e, Commands)]


@scenario
async def test_a_new_catalog_that_changes_the_models_sends_commands(tmp_path: Path) -> None:
    h = await started(tmp_path)
    await h.settle()
    before = len(commands_heard(h.heard))
    changed = catalog_of()
    changed["models"]["plain"]["context_window"] = 32_000
    h.driver.use_catalog(read_models(cli=changed))
    await h.settle()
    assert len(commands_heard(h.heard)) == before + 1, "clients call models() again on it"
    assert (await h.client.models())[1].context_window == 32_000
    await h.close()


@scenario
async def test_a_new_catalog_that_changes_only_a_secret_sends_none(tmp_path: Path) -> None:
    h = await started(tmp_path)
    await h.settle()
    before = len(commands_heard(h.heard))
    changed = catalog_of()
    changed["models"]["keyed"]["api_key"] = "sk-another"
    changed["models"]["keyed"]["base_url"] = "https://elsewhere.example/v1"
    h.driver.use_catalog(read_models(cli=changed))
    await h.settle()
    assert len(commands_heard(h.heard)) == before
    await h.close()
