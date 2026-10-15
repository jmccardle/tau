"""The ``compaction`` config block: on by default in every CLI head.

``TauBackend`` hard-coded ``CompactionSettings(enabled=False)`` from the initial
commit, so a small model ran to its window and failed with the provider's 400.
These pin the replacement: the block's validation, the two head seams that apply
it (``resolve_model_config`` for ``-p``/rpc/repl/serve, ``_apply_run_config`` for
the TUI), and how ``TauBackend`` turns it into settings or a summarizer policy.

Reference: docs/PRICING-AND-COMPACTION-CONFIG.md §3–§5.
"""

from __future__ import annotations

from typing import Any

import pytest

from tau_agent_core.compaction import DEFAULT_COMPACTION_SETTINGS
from tau_agent_core.compaction_policy import CompactionPolicyError
from tau_coding_agent.backends import TauBackend
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.config import (
    ConfigError,
    apply_compaction_config,
    parse_compaction_config,
)
from tau_coding_agent.headless import resolve_model_config


def _entry(model: str, **over: Any) -> dict[str, Any]:
    entry = {
        "backend": "openai",
        "model": model,
        "base_url": "http://localhost/v1",
        "api_key": f"key-{model}",
        "tools": [],
    }
    entry.update(over)
    return entry


def _config(**over: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "models": {"main": _entry("main"), "small": _entry("small")},
        "default_model": "main",
    }
    config.update(over)
    return config


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


# --- parse_compaction_config -----------------------------------------------


def test_empty_block_is_enabled_with_the_dataclass_defaults():
    parsed = parse_compaction_config({})
    assert parsed.settings == DEFAULT_COMPACTION_SETTINGS
    assert parsed.settings.enabled is True
    assert parsed.summarizer is None


def test_every_key_is_read():
    parsed = parse_compaction_config(
        {"enabled": False, "reserve_tokens": 1000, "keep_recent_tokens": 500, "summarizer": "s"}
    )
    assert parsed.settings.enabled is False
    assert parsed.settings.reserve_tokens == 1000
    assert parsed.settings.keep_recent_tokens == 500
    assert parsed.summarizer == "s"


def test_unknown_key_raises_naming_it():
    with pytest.raises(ConfigError, match=r"compaction\.reserve\b"):
        parse_compaction_config({"reserve": 10})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("enabled", "yes"),
        ("enabled", 1),
        ("reserve_tokens", True),
        ("reserve_tokens", "1000"),
        ("keep_recent_tokens", 1.5),
        ("summarizer", 3),
    ],
)
def test_wrong_type_raises_naming_the_key(key, value):
    with pytest.raises(ConfigError, match=rf"compaction\.{key}"):
        parse_compaction_config({key: value})


@pytest.mark.parametrize("key", ["reserve_tokens", "keep_recent_tokens"])
def test_non_positive_token_count_raises(key):
    with pytest.raises(ConfigError, match=rf"compaction\.{key}.*positive"):
        parse_compaction_config({key: 0})


def test_empty_summarizer_raises():
    with pytest.raises(ConfigError, match="summarizer"):
        parse_compaction_config({"summarizer": ""})


@pytest.mark.parametrize("block", [None, True, [], "on"])
def test_non_object_block_raises(block):
    with pytest.raises(ConfigError, match="'compaction' must be an object"):
        parse_compaction_config(block)


# --- apply_compaction_config ------------------------------------------------


def test_no_block_returns_the_model_config_itself():
    mc = _entry("main")
    assert apply_compaction_config(mc, _config()) is mc


def test_global_block_is_copied_without_mutating_the_entry():
    config = _config(compaction={"reserve_tokens": 2048})
    entry = config["models"]["main"]
    resolved = apply_compaction_config(entry, config)
    assert resolved["compaction"] == {"reserve_tokens": 2048}
    assert "compaction" not in entry


def test_a_model_entry_block_replaces_the_global_one_whole():
    config = _config(compaction={"reserve_tokens": 2048, "keep_recent_tokens": 100})
    entry = {**config["models"]["main"], "compaction": {"enabled": False}}
    assert apply_compaction_config(entry, config)["compaction"] == {"enabled": False}


def test_summarizer_entry_is_resolved_from_the_models_map():
    config = _config(compaction={"summarizer": "small"})
    resolved = apply_compaction_config(dict(config["models"]["main"]), config)
    assert resolved["compaction_summarizer"] == {
        **config["models"]["small"],
        "config_name": "small",
    }


def test_unknown_summarizer_raises_listing_the_configured_models():
    config = _config(compaction={"summarizer": "nope"})
    with pytest.raises(ConfigError, match=r"'nope'.*main, small"):
        apply_compaction_config(dict(config["models"]["main"]), config)


def test_malformed_block_raises_at_resolution():
    config = _config(compaction={"enable": True})
    with pytest.raises(ConfigError, match=r"compaction\.enable\b"):
        apply_compaction_config(dict(config["models"]["main"]), config)


# --- TauBackend --------------------------------------------------------------


def test_backend_compacts_by_default():
    settings = TauBackend(_entry("main")).agent_session.compaction_settings
    assert settings.enabled is True
    assert settings.reserve_tokens == DEFAULT_COMPACTION_SETTINGS.reserve_tokens


def test_backend_reads_the_block():
    backend = TauBackend(
        _entry(
            "main", compaction={"enabled": False, "reserve_tokens": 4096, "keep_recent_tokens": 9}
        )
    )
    settings = backend.agent_session.compaction_settings
    assert (settings.enabled, settings.reserve_tokens, settings.keep_recent_tokens) == (
        False,
        4096,
        9,
    )


def test_backend_does_not_share_the_module_default_settings():
    backend = TauBackend(_entry("main"))
    backend.set_auto_compaction(False)
    assert DEFAULT_COMPACTION_SETTINGS.enabled is True


def test_backend_summarises_through_the_named_model():
    config = _config(compaction={"summarizer": "small", "reserve_tokens": 1024})
    backend = TauBackend(apply_compaction_config(dict(config["models"]["main"]), config))
    model, key = backend.agent_session.summarizer()
    assert (model.id, key) == ("small", "key-small")
    settings = backend.agent_session.compaction_settings
    assert (settings.enabled, settings.reserve_tokens) == (True, 1024)


def test_backend_keeps_the_summarizer_when_auto_compaction_is_off():
    config = _config(compaction={"summarizer": "small", "enabled": False})
    backend = TauBackend(apply_compaction_config(dict(config["models"]["main"]), config))
    assert backend.agent_session.compaction_settings.enabled is False
    assert backend.agent_session.summarizer()[0].id == "small"


def test_summarizer_without_api_key_raises():
    small = _entry("small")
    del small["api_key"]
    config = _config(compaction={"summarizer": "small"})
    config["models"]["small"] = small
    with pytest.raises(ConfigError, match="no api_key"):
        TauBackend(apply_compaction_config(dict(config["models"]["main"]), config))


def test_summarizer_with_a_smaller_window_raises_at_construction():
    config = _config(compaction={"summarizer": "small"})
    config["models"]["small"] = _entry("small", context_window=32000)
    with pytest.raises(CompactionPolicyError, match="smaller than session model"):
        TauBackend(apply_compaction_config(dict(config["models"]["main"]), config))


def test_unresolved_summarizer_raises():
    with pytest.raises(ConfigError, match="apply_compaction_config"):
        TauBackend(_entry("main", compaction={"summarizer": "small"}))


# --- the heads ---------------------------------------------------------------


def test_resolve_model_config_applies_the_block():
    """``tau -p``, ``--mode rpc``, ``--mode repl`` and ``tau serve`` all resolve here."""
    config = _config(compaction={"summarizer": "small", "keep_recent_tokens": 77})
    _name, mc = resolve_model_config(config, CLIArgs(model="main"))
    assert mc["compaction"] == {"summarizer": "small", "keep_recent_tokens": 77}
    assert mc["compaction_summarizer"]["config_name"] == "small"


def test_resolve_model_config_applies_the_block_to_an_ad_hoc_model():
    config = _config(compaction={"enabled": False})
    _name, mc = resolve_model_config(config, CLIArgs(model="openai/other"))
    assert mc["compaction"] == {"enabled": False}


def test_tui_applies_the_block(make_app):
    config = _config(compaction={"summarizer": "small", "enabled": False})
    app = make_app(create_backend=lambda _mc: None, config=config)
    mc = app._apply_run_config(app.config["models"]["main"])
    assert mc["compaction"] == {"summarizer": "small", "enabled": False}
    assert mc["compaction_summarizer"]["config_name"] == "small"
    assert "compaction" not in app.config["models"]["main"]
