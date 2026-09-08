"""An extension declares what its config keys ARE, so a head can render them.

``api.config`` returned an untyped dict slice and nothing said what keys were in
it or what type each was, so no generic settings screen could be written — not
because a head lacked a form renderer, but because there was nothing to render.

``CONFIG_SCHEMA`` is that declaration: a ``ui.form`` spec, read at import time
and validated at load beside ``TOUCHES_BUS``/``SUBJECTS``, for the same reason —
a declaration nobody checked is the failure, and an invalid one is a load error
rather than a settings screen that comes up empty.

The pair on top of it is ``get_extension_config`` (schema + live values) and
``set_extension_config`` (check, replace, reload).
"""

from __future__ import annotations

import pytest

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.sdk import ExtensionCapabilityError
from tau_agent_core.session_log import InMemorySessionLog


def _make_session(extensions_config=None) -> AgentSession:
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=model,
        extensions_config=extensions_config,
    )


_SCHEMA_EXT = """
CONFIG_SCHEMA = {
    "title": "Budget",
    "fields": [
        {"name": "ceiling", "kind": "number", "label": "Spend ceiling", "default": 5.0},
        {"name": "notify", "kind": "confirm", "default": False},
        {"name": "mode", "kind": "select", "options": ["strict", "loose"]},
    ],
}

CAPTURED = []


def register(api):
    CAPTURED.append(dict(api.config))
"""

_NO_SCHEMA_EXT = """
def register(api):
    pass
"""


def _write(path, source):
    path.write_text(source)
    return path


class TestTheDeclarationIsReadAndChecked:
    async def test_a_declared_schema_is_normalized_onto_the_record(self, tmp_path):
        ext = _write(tmp_path / "budget.py", _SCHEMA_EXT)
        session = _make_session()

        result = await session.load_extensions([str(ext)], discover=False)

        schema = result.extensions[0].config_schema
        assert schema is not None
        assert schema["title"] == "Budget"
        # validate_form_spec's normalization: label defaults to name.
        assert [f["name"] for f in schema["fields"]] == ["ceiling", "notify", "mode"]
        assert schema["fields"][1]["label"] == "notify"

    async def test_no_declaration_is_none_not_an_empty_schema(self, tmp_path):
        ext = _write(tmp_path / "plain.py", _NO_SCHEMA_EXT)
        session = _make_session()

        result = await session.load_extensions([str(ext)], discover=False)

        assert result.extensions[0].config_schema is None

    async def test_an_invalid_schema_is_a_load_error(self, tmp_path):
        ext = _write(
            tmp_path / "broken.py",
            'CONFIG_SCHEMA = {"fields": [{"name": "x", "kind": "colour"}]}\n\n'
            "def register(api):\n    pass\n",
        )
        session = _make_session()

        with pytest.raises(ExtensionCapabilityError, match="not a valid form spec"):
            await session.load_extensions([str(ext)], discover=False)


class TestTheReadASettingsScreenIsBuiltFrom:
    async def test_schema_and_live_values_come_back_together(self, tmp_path):
        ext = _write(tmp_path / "budget.py", _SCHEMA_EXT)
        session = _make_session()
        await session.load_extensions(
            [str(ext)], discover=False, extensions_config={"budget": {"ceiling": 9.0}}
        )

        got = session.get_extension_config("budget")

        assert got["path"] == str(ext)
        assert got["schema"]["title"] == "Budget"
        assert got["values"] == {"ceiling": 9.0}

    async def test_an_unknown_target_raises(self, tmp_path):
        session = _make_session()
        with pytest.raises(ValueError, match="no loaded extension"):
            session.get_extension_config("nope")


class TestTheWrite:
    async def test_values_reach_the_extension_after_the_reload(self, tmp_path):
        ext = _write(tmp_path / "budget.py", _SCHEMA_EXT)
        session = _make_session()
        await session.load_extensions([str(ext)], discover=False)

        outcome = await session.set_extension_config(
            "budget", {"ceiling": 12.5, "notify": True, "mode": "strict"}
        )

        assert outcome.action == "configure"
        assert outcome.ok is True
        assert session.get_extension_config("budget")["values"] == {
            "ceiling": 12.5,
            "notify": True,
            "mode": "strict",
        }

    async def test_an_undeclared_key_raises_rather_than_being_dropped(self, tmp_path):
        ext = _write(tmp_path / "budget.py", _SCHEMA_EXT)
        session = _make_session()
        await session.load_extensions([str(ext)], discover=False)

        with pytest.raises(ValueError, match="undeclared key"):
            await session.set_extension_config(
                "budget",
                {"ceiling": 1.0, "notify": False, "mode": "strict", "typo": 1},
            )

    async def test_a_wrong_type_raises(self, tmp_path):
        ext = _write(tmp_path / "budget.py", _SCHEMA_EXT)
        session = _make_session()
        await session.load_extensions([str(ext)], discover=False)

        with pytest.raises(ValueError, match="is 'number' but got str"):
            await session.set_extension_config(
                "budget", {"ceiling": "lots", "notify": False, "mode": "strict"}
            )

    async def test_a_value_outside_a_selects_options_raises(self, tmp_path):
        ext = _write(tmp_path / "budget.py", _SCHEMA_EXT)
        session = _make_session()
        await session.load_extensions([str(ext)], discover=False)

        with pytest.raises(ValueError, match="is not one of"):
            await session.set_extension_config(
                "budget", {"ceiling": 1.0, "notify": False, "mode": "sideways"}
            )

    async def test_an_extension_with_no_schema_refuses_the_write(self, tmp_path):
        ext = _write(tmp_path / "plain.py", _NO_SCHEMA_EXT)
        session = _make_session()
        await session.load_extensions([str(ext)], discover=False)

        with pytest.raises(ValueError, match="declares no CONFIG_SCHEMA"):
            await session.set_extension_config("plain", {"anything": 1})
