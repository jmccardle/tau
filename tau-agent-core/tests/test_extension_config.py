"""S40 (E6) — per-extension ``api.config`` reaches the extension, keyed by stem.

``api.config`` (E6 §2 / S40) is the per-extension config slice sourced from
``~/.tau/config.json`` ``"extensions": {"<name>": {…}}`` + per-run
``--ext-config`` overrides. The coding-agent layer resolves the merged map
(``tau-coding-agent/tests/test_ext_config_cli.py`` covers that); this suite proves
the tau-agent-core wiring — the session slices the map by each extension's FILE
STEM in ``_bind_extension_api`` and hands the right slice to that extension's
``api.config``, an unconfigured extension reads ``{}``, and the config is
run-scoped runtime state (NOT persisted onto the tree-as-truth session path).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S40; docs/EXTENSIONS-E5-WIRING.md.
"""

from __future__ import annotations

import json

from tau_ai.types import Model

from tau_agent_core.agent_session import AgentSession
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


# An extension that writes its own ``api.config`` (as JSON) to a path baked into
# the source, so the test can read back exactly what slice it received.
_CONFIG_CAPTURE_EXT = """
import json

def register(api):
    with open({out!r}, "w") as f:
        json.dump(api.config, f)
"""


def _write_capture_ext(path, out_path):
    path.write_text(_CONFIG_CAPTURE_EXT.format(out=str(out_path)))
    return path


# ── the slice keyed by file stem reaches api.config ─────────────────────────


class TestConfigSlicing:
    async def test_slice_reaches_extension_by_stem(self, tmp_path):
        out = tmp_path / "captured.json"
        ext = _write_capture_ext(tmp_path / "budget.py", out)
        session = _make_session()

        cfg = {"budget": {"ceiling": 5.0, "warn": 0.8}, "other": {"k": "v"}}
        result = await session.load_extensions(
            [str(ext)], discover=False, extensions_config=cfg
        )

        assert len(result.extensions) == 1
        # The "budget" slice (keyed by the file stem) — NOT "other", NOT the whole map.
        assert json.loads(out.read_text()) == {"ceiling": 5.0, "warn": 0.8}

    async def test_unconfigured_extension_reads_empty_dict(self, tmp_path):
        out = tmp_path / "captured.json"
        # stem "budget", but the config only names a different extension.
        ext = _write_capture_ext(tmp_path / "budget.py", out)
        session = _make_session()

        await session.load_extensions(
            [str(ext)], discover=False, extensions_config={"unrelated": {"x": 1}}
        )

        # No entry for this stem → {} (never a fabricated value; Fail-Early).
        assert json.loads(out.read_text()) == {}

    async def test_each_extension_gets_its_own_slice(self, tmp_path):
        out_a = tmp_path / "a.json"
        out_b = tmp_path / "b.json"
        ext_a = _write_capture_ext(tmp_path / "alpha.py", out_a)
        ext_b = _write_capture_ext(tmp_path / "beta.py", out_b)
        session = _make_session()

        cfg = {"alpha": {"id": "A"}, "beta": {"id": "B"}}
        await session.load_extensions(
            [str(ext_a), str(ext_b)], discover=False, extensions_config=cfg
        )

        assert json.loads(out_a.read_text()) == {"id": "A"}
        assert json.loads(out_b.read_text()) == {"id": "B"}

    async def test_config_from_constructor_is_used(self, tmp_path):
        """A config passed at construction is used when load_extensions omits one."""
        out = tmp_path / "captured.json"
        ext = _write_capture_ext(tmp_path / "budget.py", out)
        session = _make_session(extensions_config={"budget": {"ceiling": 3.0}})

        # No extensions_config kwarg here → the constructor-supplied map stands.
        await session.load_extensions([str(ext)], discover=False)

        assert json.loads(out.read_text()) == {"ceiling": 3.0}


# ── api.config is isolated per extension (no cross-mutation) ─────────────────


class TestConfigIsolation:
    def test_api_config_defaults_to_empty_dict(self):
        from tau_agent_core.extension_types import ExtensionAPI

        assert ExtensionAPI().config == {}

    def test_api_config_is_a_copy_of_the_slice(self):
        """ExtensionAPI copies its slice, so an extension-side mutation can't
        bleed back into the source map (or a sibling's slice)."""
        from tau_agent_core.extension_types import ExtensionAPI

        source = {"ceiling": 5.0}
        api = ExtensionAPI(config=source)
        api.config["ceiling"] = 999.0

        assert source["ceiling"] == 5.0


# ── config is run-scoped runtime state, NOT on the tree-as-truth path ───────


class TestConfigNotPersisted:
    async def test_config_absent_from_the_session_log(self, tmp_path):
        """Config lives in runtime state only — never appended as a tree entry.

        The invariant analog of a reload test: because config is deliberately
        re-sourced each run (not persisted), it must not leak into the durable
        session log, or a reload would fork "what the model saw" from the path.
        """
        out = tmp_path / "captured.json"
        ext = _write_capture_ext(tmp_path / "budget.py", out)
        session = _make_session()

        await session.load_extensions(
            [str(ext)], discover=False, extensions_config={"budget": {"ceiling": 5.0}}
        )

        # It IS held as runtime state on the session...
        assert session._extensions_config == {"budget": {"ceiling": 5.0}}
        # ...but nothing about it was appended to the durable log.
        blob = json.dumps(session.session_log.entries())
        assert "ceiling" not in blob
        assert session.session_log.entries() == []
