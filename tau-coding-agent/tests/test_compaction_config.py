"""The ``compaction`` config block: on by default in every CLI head.

``TauBackend`` hard-coded ``CompactionSettings(enabled=False)`` from the initial
commit, so a small model ran to its window and failed with the provider's 400.
These pin the replacement: the block's validation, the resolver that hands it out
per model name, the head seams that copy the starting block
(``resolve_model_config`` for ``-p``/rpc/repl/serve, ``_apply_run_config`` for the
TUI), and how a session's compaction follows each kind of model switch.

Reference: docs/PRICING-AND-COMPACTION-CONFIG.md §3–§7.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_agent_core import ModelUnavailableError
from tau_agent_core.compaction import DEFAULT_COMPACTION_SETTINGS
from tau_agent_core.compaction_policy import CompactionPolicyError
from tau_agent_core.rpc import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog
from tau_coding_agent.backends import TauBackend, make_model_resolver
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.config import (
    ConfigError,
    apply_compaction_config,
    compaction_for,
    parse_compaction_config,
)
from tau_coding_agent.headless import resolve_model_config
from tau_llm.types import AssistantMessage, TextContent, Usage

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000


def _entry(name: str, **over: Any) -> dict[str, Any]:
    entry = {
        "backend": "openai",
        "model": f"{name}-id",
        "base_url": "http://localhost/v1",
        "api_key": f"key-{name}",
        "tools": [],
    }
    entry.update(over)
    return entry


def _config(**over: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "models": {"main": _entry("main"), "small": _entry("small"), "big": _entry("big")},
        "default_model": "main",
    }
    config.update(over)
    return config


def _backend(config: dict[str, Any], name: str = "main", *, bind: bool = True) -> TauBackend:
    """Build a backend the way ``tau -p``/rpc/repl/serve do: resolve, construct, bind."""
    _spec, model_config = resolve_model_config(config, CLIArgs(model=name))
    backend = TauBackend(model_config)
    if bind:
        backend.agent_session.set_model_resolver(make_model_resolver(config))
    return backend


def _settings(backend: TauBackend) -> tuple[bool, int, int]:
    s = backend.agent_session.compaction_settings
    return s.auto, s.reserve_tokens, s.keep_recent_tokens


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


# --- parse_compaction_config -----------------------------------------------


def test_empty_block_is_auto_with_the_dataclass_defaults():
    parsed = parse_compaction_config({})
    assert parsed.settings == DEFAULT_COMPACTION_SETTINGS
    assert parsed.settings.auto is True
    assert parsed.summarizer is None


def test_every_key_is_read():
    parsed = parse_compaction_config(
        {"auto": False, "reserve_tokens": 1000, "keep_recent_tokens": 500, "summarizer": "s"}
    )
    assert parsed.settings.auto is False
    assert parsed.settings.reserve_tokens == 1000
    assert parsed.settings.keep_recent_tokens == 500
    assert parsed.summarizer == "s"


def test_the_old_enabled_key_is_refused_by_name():
    with pytest.raises(ConfigError, match=r"unknown key 'compaction\.enabled'.*auto"):
        parse_compaction_config({"enabled": False})


def test_unknown_key_raises_naming_it():
    with pytest.raises(ConfigError, match=r"compaction\.reserve\b"):
        parse_compaction_config({"reserve": 10})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("auto", "yes"),
        ("auto", 1),
        ("reserve_tokens", True),
        ("reserve_tokens", "1000"),
        ("keep_recent_tokens", 1.5),
        ("summarizer", 3),
    ],
)
def test_wrong_type_raises_naming_the_key(key, value):
    with pytest.raises(
        ConfigError, match=rf"compaction\.{key}' must be (a boolean|an integer|a string); got "
    ):
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


# --- compaction_for and the resolver ------------------------------------------


def test_a_model_block_replaces_the_top_level_one_whole():
    config = _config(compaction={"reserve_tokens": 2048, "summarizer": "big"})
    config["models"]["small"]["compaction"] = {"auto": False}
    parsed = compaction_for(config, "small")
    assert parsed.settings.auto is False
    assert parsed.settings.reserve_tokens == DEFAULT_COMPACTION_SETTINGS.reserve_tokens
    assert parsed.summarizer is None
    assert compaction_for(config, "main").settings.reserve_tokens == 2048


def test_no_block_anywhere_is_the_defaults():
    assert compaction_for(_config(), "main").settings == DEFAULT_COMPACTION_SETTINGS


def test_an_unconfigured_name_gets_the_top_level_block():
    config = _config(compaction={"keep_recent_tokens": 77})
    assert compaction_for(config, "openai/other").settings.keep_recent_tokens == 77


def test_the_resolver_hands_out_compaction_by_name():
    config = _config(compaction={"summarizer": "big"})
    config["models"]["small"]["compaction"] = {"reserve_tokens": 4000}
    resolver = make_model_resolver(config)
    assert resolver.compaction("main").summarizer == "big"
    assert resolver.compaction("small").settings.reserve_tokens == 4000


def test_a_bad_model_block_is_that_entrys_build_error_not_the_resolvers():
    """Only the selected entry is fatal, its block included (docs/SILENT-DEFAULTS.md §8)."""
    config = _config()
    config["models"]["small"]["compaction"] = {"enable": True}
    resolver = make_model_resolver(config)
    pattern = r"unknown key 'models\.small\.compaction\.enable'"
    with pytest.raises(ModelUnavailableError, match=pattern):
        resolver("small")
    assert sorted(resolver.model_errors()) == ["small"]
    assert resolver("main").id == "main-id"


def test_an_unknown_summarizer_raises_at_resolver_construction():
    with pytest.raises(ConfigError, match=r"'nope'.*big, main, small"):
        make_model_resolver(_config(compaction={"summarizer": "nope"}))


# --- apply_compaction_config --------------------------------------------------


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
    entry = {**config["models"]["main"], "compaction": {"auto": False}, "config_name": "main"}
    assert apply_compaction_config(entry, config)["compaction"] == {"auto": False}


def test_an_entry_block_is_blamed_on_its_entry_never_the_top_level():
    config = _config()
    config["models"]["small"]["compaction"] = {"reserve_tokens": "lots"}
    entry = {**config["models"]["small"], "config_name": "small"}
    with pytest.raises(ConfigError, match=r"^'models\.small\.compaction\.reserve_tokens' must"):
        apply_compaction_config(entry, config)
    with pytest.raises(ConfigError, match="names no configured entry"):
        apply_compaction_config(config["models"]["small"], config)


def test_backend_config_carries_no_models_map_and_no_other_key():
    """TauBackend's config holds only its own model's credential, summarizer or not."""
    config = _config(compaction={"summarizer": "big"})
    _spec, mc = resolve_model_config(config, CLIArgs(model="main"))
    assert "models" not in mc
    text = json.dumps(mc)
    assert "key-main" in text
    assert "key-big" not in text and "key-small" not in text


def test_unknown_summarizer_raises_at_resolution():
    config = _config(compaction={"summarizer": "nope"})
    with pytest.raises(ConfigError, match=r"'nope'.*big, main, small"):
        apply_compaction_config(dict(config["models"]["main"]), config)


# --- TauBackend and binding ---------------------------------------------------


def test_backend_compacts_automatically_by_default():
    assert _settings(_backend(_config())) == (
        True,
        DEFAULT_COMPACTION_SETTINGS.reserve_tokens,
        DEFAULT_COMPACTION_SETTINGS.keep_recent_tokens,
    )


def test_backend_reads_the_block_before_binding():
    config = _config(compaction={"auto": False, "reserve_tokens": 4096, "keep_recent_tokens": 9})
    assert _settings(_backend(config, bind=False)) == (False, 4096, 9)


def test_backend_does_not_share_the_module_default_settings():
    backend = _backend(_config())
    backend.set_auto_compaction(False)
    assert DEFAULT_COMPACTION_SETTINGS.auto is True


def test_binding_puts_the_named_summarizer_in_force():
    backend = _backend(_config(compaction={"summarizer": "big", "reserve_tokens": 1024}))
    model, key = backend.agent_session.summarizer()
    assert (model.id, key) == ("big-id", "key-big")
    assert _settings(backend)[:2] == (True, 1024)


def test_summarizer_without_api_key_gets_none_so_the_provider_reads_its_env_var():
    config = _config(compaction={"summarizer": "big"})
    del config["models"]["big"]["api_key"]
    model, key = _backend(config).agent_session.summarizer()
    assert model.id == "big-id"
    assert key is None


def test_the_session_key_never_reaches_the_summarizer():
    config = _config(compaction={"summarizer": "big"})
    del config["models"]["big"]["api_key"]
    assert _backend(config).agent_session.summarizer()[1] != "key-main"


def test_the_summarizer_key_is_the_one_set_model_would_use():
    session = _backend(_config(compaction={"summarizer": "big"})).agent_session
    assert session.summarizer() == session.resolve_model("big")


def test_a_summarizer_with_a_smaller_window_raises_when_the_resolver_is_bound():
    config = _config(compaction={"summarizer": "small"})
    config["models"]["small"]["context_window"] = 32000
    backend = _backend(config, bind=False)
    with pytest.raises(CompactionPolicyError, match="smaller than session model"):
        backend.agent_session.set_model_resolver(make_model_resolver(config))


def test_an_unbound_summarizer_refuses_summaries():
    backend = _backend(_config(compaction={"summarizer": "big"}), bind=False)
    with pytest.raises(CompactionPolicyError, match="no resolver supplying compaction"):
        backend.agent_session.summarizer()


async def test_an_unbound_summarizer_refuses_turns():
    backend = _backend(_config(compaction={"summarizer": "big"}), bind=False)
    with pytest.raises(CompactionPolicyError, match="no resolver supplying compaction"):
        await backend.agent_session.prompt("hi")


def test_a_plain_resolver_keeps_the_constructed_settings():
    backend = _backend(_config(compaction={"reserve_tokens": 3000}), bind=False)
    backend.agent_session.set_model_resolver(make_model_resolver(_config()).__call__)
    assert _settings(backend)[1] == 3000


# --- every kind of model switch -----------------------------------------------


def _per_model_config() -> dict[str, Any]:
    config = _config()
    config["models"]["big"]["compaction"] = {
        "auto": False,
        "reserve_tokens": 5000,
        "keep_recent_tokens": 600,
        "summarizer": "main",
    }
    return config


def test_set_model_puts_the_new_models_block_in_force():
    """``ctx.set_model`` delegates to ``AgentSession.set_model``."""
    backend = _backend(_per_model_config())
    assert backend.agent_session.summarizer()[0].id == "main-id"
    backend.agent_session.set_model("big", source="command")
    assert _settings(backend) == (False, 5000, 600)
    assert backend.agent_session.summarizer()[0].id == "main-id"
    backend.agent_session.set_model("small", source="command")
    assert _settings(backend) == (
        True,
        DEFAULT_COMPACTION_SETTINGS.reserve_tokens,
        DEFAULT_COMPACTION_SETTINGS.keep_recent_tokens,
    )
    assert backend.agent_session.summarizer()[0].id == "small-id"


def test_set_model_refused_by_compaction_leaves_everything_as_it_was():
    config = _per_model_config()
    config["models"]["main"]["context_window"] = 32000
    backend = _backend(config, "small")
    with pytest.raises(CompactionPolicyError):
        backend.agent_session.set_model("big", source="command")
    assert backend.agent_session.get_model()["id"] == "small-id"
    assert _settings(backend)[0] is True


class _DurableLog(InMemorySessionLog):
    """An in-memory log that names a location, so ``require_durable_session`` admits it."""

    path = Path("/nonexistent/compaction-config.jsonl")


async def test_rpc_set_model_puts_the_new_models_block_in_force():
    backend = _backend(_per_model_config())
    session = backend.agent_session
    session.session_log = _DurableLog()
    handler = RPCHandler(session)
    await handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "set_model", "params": {"name": "big"}}
    )
    response = await handler._output_queue.get()
    assert "error" not in response, response
    assert _settings(backend) == (False, 5000, 600)


def test_tui_model_switch_and_set_model_agree(make_app):
    """TUI ``/model`` rebuilds the backend; ``ctx.set_model`` switches in place."""
    config = _per_model_config()
    app = make_app(create_backend=lambda _mc: None, config=config)
    tui_mc = app._apply_run_config({**app.config["models"]["big"], "config_name": "big"})
    rebuilt = TauBackend(tui_mc)
    rebuilt.agent_session.set_model_resolver(make_model_resolver(app.config))

    switched = _backend(config, "main")
    switched.agent_session.set_model("big", source="command")

    assert _settings(rebuilt) == _settings(switched) == (False, 5000, 600)
    assert rebuilt.agent_session.summarizer() == switched.agent_session.summarizer()


# --- auto is automatic compaction only ---------------------------------------


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="main-id",
        stop_reason="stop",
        timestamp=_TS,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    def __init__(self, text: str) -> None:
        from tau_llm.streaming import DoneEvent, TextDeltaEvent

        message = _assistant(text)
        self._events = [
            TextDeltaEvent(delta=text, partial=message),
            DoneEvent(final=message, usage=message.usage),
        ]

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event in self._events:
            yield event

    async def result(self):
        return self._events[-1].final

    def abort(self) -> None:
        pass


async def _fake_stream(model, context, options=None, **_):
    return _Stream("ok " * 50)


async def test_auto_false_stops_automatic_compaction_but_not_compact():
    config = _config(
        compaction={"auto": False, "keep_recent_tokens": 1, "summarizer": "big"},
    )
    session = _backend(config).agent_session
    calls: list[Any] = []

    async def fake_summary(model, *args, **kwargs):
        calls.append(model)
        return _assistant("SUMMARY")

    with (
        patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_stream),
        patch("tau_agent_core.compaction.complete_simple", side_effect=fake_summary),
        patch("tau_agent_core.agent_session.should_compact", return_value=True),
        patch("tau_agent_core.agent_session.must_compact", return_value=True),
    ):
        await session.prompt("first " * 50)
        await session.prompt("second " * 50)
        assert calls == [], "auto: false must stop automatic compaction"
        result = await session.compact()

    assert result is not None
    assert calls, "/compact runs although auto is off"
    assert {m.id for m in calls} == {"big-id"}, "and summarises through the summarizer"


@pytest.mark.parametrize(
    ("argv", "own"),
    [
        (["--model", "openai/adhoc"], False),
        (["--model", "small"], True),
        (["--model", "big"], False),
        (["--model", "small:high"], True),
        (["--model", "big:high"], False),
    ],
    ids=["ad-hoc", "entry-with-block", "entry-without-block", "suffix-with", "suffix-without"],
)
def test_the_tui_model_override_carries_only_an_entrys_own_block(make_app, monkeypatch, argv, own):
    """The top-level block stays top-level; only an entry's own block rides on its override."""
    from tau_coding_agent import cli

    config = _config(compaction={"reserve_tokens": 2048})
    config["models"]["small"]["compaction"] = {"auto": False}
    seen: dict[str, Any] = {}

    class _FakeApp:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        def run(self) -> None:
            pass

    from tau_coding_agent import app as app_module

    real = app_module.TauApp
    monkeypatch.setattr(app_module, "TauApp", _FakeApp)
    assert cli._launch_tui(cli.parse_cli_args(argv), config) == 0
    monkeypatch.setattr(app_module, "TauApp", real)
    overrides = seen["cli_overrides"]
    name = overrides["default_model"]
    assert ("compaction" in overrides["models"][name]) is own

    app = make_app(config=config)
    app._apply_cli_overrides(overrides)
    entry = {**app.config["models"][name], "config_name": name}
    expected = {"auto": False} if own else {"reserve_tokens": 2048}
    assert app._apply_run_config(entry)["compaction"] == expected
    assert app._configured_max_tokens() is not None


def test_every_compaction_key_type_has_a_wording():
    """A new key type without a ``_JSON_TYPE_NAMES`` entry fails here, not as a KeyError."""
    from tau_coding_agent.config import _JSON_TYPE_NAMES, COMPACTION_KEYS

    assert set(COMPACTION_KEYS.values()) <= set(_JSON_TYPE_NAMES)
