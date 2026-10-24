"""Each head passes the source of the model and thinking level it applies (docs/TURN-SETTINGS.md §8)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from tau_agent_core.cursor import Cursor
from tau_agent_core.session_log import InMemorySessionLog, config_at
from tau_agent_core.setting_sources import SettingSource

from tau_coding_agent.backends import TauBackend, make_model_resolver
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import resolve_model_config
from tau_coding_agent.serve import protocol as p

from test_serve import _fake_stream, _serve, extensions_off  # noqa: F401

_CONFIG: dict[str, Any] = {
    "default_model": "one",
    "models": {
        name: {"backend": "openai", "model": f"{name}-model", "base_url": "http://127.0.0.1:1/v1"}
        for name in ("one", "two")
    },
}


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)


def _read(config: dict[str, Any]) -> tuple[SettingSource, SettingSource]:
    """Both settings' sources, after checking the model config is plain JSON."""
    assert json.loads(json.dumps(config)) == config
    return (
        SettingSource(config["model_source"], config["model_restored"]),
        SettingSource(config["thinking_source"], config["thinking_restored"]),
    )


def _sources(**args: Any) -> tuple[SettingSource, SettingSource]:
    _name, config = resolve_model_config(_CONFIG, CLIArgs(**args))
    return _read(config)


def _resumed(prior: dict[str, Any], **args: Any) -> tuple[SettingSource, SettingSource]:
    _name, config = resolve_model_config(
        _CONFIG, CLIArgs(**args), fallback_model=prior["model"], prior_config=prior
    )
    return _read(config)


class TestFlags:
    def test_dash_m_naming_the_default_model_is_cli(self) -> None:
        assert _sources(model="one")[0] == SettingSource("cli")

    def test_no_flag_is_default(self) -> None:
        assert _sources() == (SettingSource("default"), SettingSource("default"))

    def test_thinking_flag_is_cli_and_a_suffix_follows_its_spec(self) -> None:
        assert _sources(thinking="high") == (SettingSource("default"), SettingSource("cli"))
        assert _sources(model="one:high") == (SettingSource("cli"), SettingSource("cli"))


class TestResume:
    def test_a_recorded_command_pick_is_restored_as_command(self) -> None:
        prior = {"model": "two", "model_source": "command", "thinking": "high"}
        model, thinking = _resumed(prior)
        assert model == SettingSource("command", restored=True)
        assert thinking == SettingSource("resume", restored=True)

    def test_an_entry_from_before_sources_resumes_as_resume(self) -> None:
        model, thinking = _resumed({"model": "two"})
        assert model == SettingSource("resume", restored=True)
        assert thinking == SettingSource("default")

    def test_a_flag_on_resume_wins_over_the_record(self) -> None:
        prior = {"model": "two", "model_source": "command", "thinking": "low"}
        model, thinking = _resumed(prior, model="one")
        assert model == SettingSource("cli")
        assert thinking == SettingSource("resume", restored=True)


class TestBackend:
    def _backend(self, **args: Any) -> TauBackend:
        _name, config = resolve_model_config(_CONFIG, CLIArgs(**args))
        backend = TauBackend(config)
        backend.bind_cursor(Cursor.newest(InMemorySessionLog()))
        backend.agent_session.set_model_resolver(make_model_resolver(_CONFIG))
        return backend

    def test_the_session_takes_the_resolved_sources(self) -> None:
        session = self._backend(model="one").agent_session
        assert session.get_setting_source("model") == SettingSource("cli")
        assert session.get_setting_source("thinking") == SettingSource("default")

    async def test_slash_model_records_command_at_once(self) -> None:
        backend = self._backend()
        await backend.agent_session.start()
        await backend.set_model("two")
        cursor = backend.agent_session.cursor
        recorded = config_at(cursor.entries(), cursor.leaf)
        assert (recorded["model"], recorded["model_source"]) == ("two", "command")

    def test_slash_thinking_is_command(self) -> None:
        backend = self._backend()
        backend.set_reasoning("high")
        assert backend.agent_session.get_setting_source("thinking") == SettingSource("command")


class TestServe:
    @pytest.mark.parametrize(("model", "expected"), [(None, "default"), ("one", "rpc")])
    async def test_new_session_and_set_model_are_rpc(
        self, tmp_path: Any, extensions_off: Any, model: str | None, expected: str
    ) -> None:
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_stream):
            server, served = await _serve(tmp_path, _CONFIG)
            try:
                client = await served.client()
                opened = await client.request(p.NewSession(cwd=str(tmp_path), model=model))
                session_id = opened["session"]["session_id"]
                replica = await client.attach(session_id)
                session = served.daemon.hosts[session_id].backend.agent_session
                created = session.get_setting_source("model")
                head = replica.head_cursor_id
                await client.request(p.RpcCall("set_model", session_id, head, {"name": "two"}))
                switched = session.get_setting_source("model")
                recorded = config_at(session.cursor.entries(), session.cursor.leaf)
                await client.close()
            finally:
                server.close()
                await server.wait_closed()
                await served.daemon.shutdown()
        assert created == SettingSource(expected)
        assert switched == SettingSource("rpc")
        assert recorded["model_source"] == "rpc"
