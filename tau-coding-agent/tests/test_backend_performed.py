"""The four generic mutations report one record, not four unrelated Python types.

`set_model` returned `dict[str, Any]`, `set_session_name` a `str`,
`set_auto_compaction` a `bool` and the three extension actions an
`ExtensionActionResult`. The TUI stringified whichever it got, which is how a
reader came to see `set_auto_compaction: True`.

Reference: docs/REMOTE-CONTROL.md §6, "the result half is generated too".
"""

from __future__ import annotations

import pytest
from tau_agent_core.agent_session import ExtensionActionResult
from tau_agent_core.flows import Performed
from tau_agent_core.rpc import commands
from tau_agent_core.rpc.schema import result_schema_for
from tau_coding_agent.backends import TauBackend


@pytest.fixture
def backend() -> TauBackend:
    return TauBackend(
        {
            "backend": "openai",
            "model": "m",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "not-needed",
            "tools": [],
        }
    )


def _check(performed: object, mutation: str) -> Performed:
    assert isinstance(performed, Performed), f"{mutation} answered {type(performed).__name__}"
    assert performed.mutation == mutation
    violation = commands.validate_params(result_schema_for(mutation), performed.data)
    assert violation is None, f"{mutation}: {violation}\ndata={performed.data}"
    return performed


def test_set_session_name_reports_a_performed(backend: TauBackend, monkeypatch) -> None:
    monkeypatch.setattr(backend.agent_session, "set_session_name", lambda name: None)
    performed = _check(backend.set_session_name("the refactor"), "set_session_name")
    assert performed.data["name"] == "the refactor"


def test_set_auto_compaction_reports_the_effective_state(backend: TauBackend) -> None:
    performed = _check(backend.set_auto_compaction(False), "set_auto_compaction")
    assert performed.data["enabled"] is False
    assert performed.summary() == "set_auto_compaction: enabled=False"


def test_set_model_reports_the_model_it_switched_to(backend: TauBackend, monkeypatch) -> None:
    switched = {"id": "gpt-4o", "provider": "openai", "context_window": 128000}
    monkeypatch.setattr(backend.agent_session, "set_model", lambda name: switched)
    performed = _check(backend.set_model("fast"), "set_model")
    assert performed.data["model"] == switched


async def test_an_extension_action_reports_a_performed(backend: TauBackend, monkeypatch) -> None:
    async def _enable(path: str) -> ExtensionActionResult:
        return ExtensionActionResult(action="enable", path=path, ok=True, message=f"enabled {path}")

    monkeypatch.setattr(backend.agent_session, "enable_extension", _enable)
    performed = _check(await backend.enable_extension("/x/a.py"), "enable_extension")
    assert performed.summary() == "enabled /x/a.py"


async def test_the_three_extension_actions_name_their_own_capability(
    backend: TauBackend, monkeypatch
) -> None:
    """The record names the capability that ran, so `reload` is not reported as `enable`."""
    for verb in ("enable", "disable", "reload"):

        async def _action(path: str, verb: str = verb) -> ExtensionActionResult:
            return ExtensionActionResult(action=verb, path=path, ok=True, message=f"{verb} done")

        monkeypatch.setattr(backend.agent_session, f"{verb}_extension", _action)
        performed = await getattr(backend, f"{verb}_extension")("/x/a.py")
        _check(performed, f"{verb}_extension")
