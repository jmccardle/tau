"""Tests for the single ``~/.tau/config.json`` reader/writer (config.py).

Covers the two defects W0 fixed:

1. Two divergent readers — ``cli.load_config`` validated, ``Parley.load_config``
   did not and wrote a hardcoded default that disagreed with the packaged
   ``tau_default_config.json``.
2. ``action_edit_system_prompt`` persisted the *runtime* config (CLI overrides
   merged in), silently promoting a one-run ``--model`` flag into the on-disk
   default. ``update_config`` is the read-modify-write that prevents it.

Reference: docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §2, docs/JMFTS-INTEGRATION-PLAN.md §3.1
(both add config keys, so the divergence is fixed before they land).
"""

from __future__ import annotations

import json

import pytest

from tau_coding_agent.config import (
    DEFAULT_CONFIG_TEMPLATE,
    ConfigError,
    bootstrap_config,
    load_config,
    save_config,
    update_config,
)


class TestLoadConfig:
    def test_absent_config_is_empty_not_an_error(self, tmp_path):
        assert load_config(tmp_path / "nope.json") == {}

    def test_reads_a_json_object(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"default_model": "local-llm"}))
        assert load_config(path) == {"default_model": "local-llm"}

    def test_non_object_raises(self, tmp_path):
        """Fail-Early: a JSON array must not be silently coerced to {}."""
        path = tmp_path / "config.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(ConfigError, match="must contain a JSON object"):
            load_config(path)

    def test_malformed_json_raises(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            load_config(path)


class TestThePackagedTemplateActuallyShips:
    """W0 made ``tau_default_config.json`` load-bearing without making it SHIP.

    ``bootstrap_config`` reads the template out of the installed package on first
    run. ``tau-coding-agent/pyproject.toml`` declared no ``package-data``, so the
    wheel contained no JSON at all: ``pip install tau-coding-agent && tau`` died
    with ``FileNotFoundError`` before printing a prompt. It passed every test only
    because the test venv is an editable install, where the "installed package" IS
    the source tree.

    No test of ``config.py`` can catch that — the bug lives in packaging metadata.
    So assert the packaging invariant itself.
    """

    def test_the_template_is_declared_as_package_data(self):
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text())
        declared = data["tool"]["setuptools"]["package-data"]["tau_coding_agent"]

        assert DEFAULT_CONFIG_TEMPLATE.name in declared, (
            f"{DEFAULT_CONFIG_TEMPLATE.name} is read at runtime by bootstrap_config() "
            "but is not declared in [tool.setuptools.package-data] — it will not be "
            "in the wheel, and first run of an installed τ will crash."
        )

    def test_the_template_exists_and_is_a_valid_config(self):
        assert DEFAULT_CONFIG_TEMPLATE.is_file()
        template = json.loads(DEFAULT_CONFIG_TEMPLATE.read_text())
        assert isinstance(template, dict)
        assert template["models"], "a template with no models bootstraps an unusable τ"
        assert template["default_model"] in template["models"], (
            "default_model must name a model that exists in the template"
        )


class TestBootstrap:
    def test_creates_from_the_packaged_template(self, tmp_path):
        path = tmp_path / "config.json"
        created = bootstrap_config(path)

        assert path.exists()
        # The file a first-run user gets is the file we maintain — not a second,
        # hardcoded default living in the TUI.
        assert created == json.loads(DEFAULT_CONFIG_TEMPLATE.read_text())

    def test_existing_config_is_not_clobbered(self, tmp_path):
        path = tmp_path / "config.json"
        save_config({"default_model": "mine"}, path)
        assert bootstrap_config(path) == {"default_model": "mine"}


class TestUpdateConfig:
    def test_round_trips_one_key(self, tmp_path):
        path = tmp_path / "config.json"
        save_config({"models": {"a": {}}, "system_prompt": "old"}, path)

        update_config("system_prompt", "new", path)

        assert load_config(path) == {"models": {"a": {}}, "system_prompt": "new"}

    def test_does_not_persist_runtime_only_cli_overrides(self, tmp_path):
        """The W0 regression: editing the system prompt must not persist --model.

        The TUI merges CLI overrides into ``self.config``; the old code wrote that
        dict straight back to disk, so a one-run ``tau --model x`` silently became
        the permanent default. ``update_config`` re-reads from disk instead, so an
        override held only in memory never reaches the file.
        """
        path = tmp_path / "config.json"
        on_disk = {"models": {"real": {}}, "default_model": "real"}
        save_config(on_disk, path)

        # Simulate the TUI's runtime config: on-disk + a CLI override merged in.
        runtime = dict(on_disk)
        runtime["default_model"] = "cli-override-model"
        runtime["models"] = {**on_disk["models"], "adhoc": {"from": "cli"}}

        update_config("system_prompt", "edited", path)

        persisted = load_config(path)
        assert persisted["default_model"] == "real"
        assert "adhoc" not in persisted["models"]
        assert persisted["system_prompt"] == "edited"
