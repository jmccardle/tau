"""Tests for :mod:`tau_llm.catalog` — building a Model from models.dev data.

No test here reaches the network. Each one runs against a small hand-written
catalog whose records are shaped like real models.dev entries, so the suite
pins τ's *conversion* rather than models.dev's current contents — which change
daily and are not τ's to assert.

The cases that matter are the refusals. This module exists because
``build_model_from_config`` hardcoded a 128000-token window and a 4096-token cap
for every model that has ever existed, and a catalog importer that quietly does
the same thing in a new place would be no improvement.
"""

from __future__ import annotations

import json

import pytest

from tau_llm.catalog import (
    CatalogError,
    _split_ref,
    config_entry_from_record,
    find_models,
    get_record,
    iter_models,
    load_catalog,
    main,
    model_from_record,
    thinking_level_map_from_record,
)

# A models.dev-shaped catalog. Field names and nesting follow the real api.json:
# providers carry id/name/env/doc, models carry limit/cost/reasoning_options.
CATALOG = {
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "env": ["OPENAI_API_KEY"],
        "doc": "https://platform.openai.com/docs/models",
        "models": {
            "gpt-5.1": {
                "id": "gpt-5.1",
                "name": "GPT-5.1",
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["none", "low", "medium", "high"]}
                ],
                "tool_call": True,
                "limit": {"context": 400000, "input": 272000, "output": 128000},
                "cost": {"input": 1.25, "output": 10},
            },
            "no-tools": {
                "id": "no-tools",
                "name": "Embedding Only",
                "tool_call": False,
                "limit": {"context": 8192, "output": 4096},
            },
            "no-limits": {"id": "no-limits", "name": "Mystery", "tool_call": True},
            "zero-output": {
                "id": "zero-output",
                "name": "Zero",
                "tool_call": True,
                "limit": {"context": 8192, "output": 0},
            },
        },
    },
    "hpc-ai": {
        "id": "hpc-ai",
        "name": "HPC-AI",
        "env": ["HPC_AI_API_KEY"],
        # Model ids contain slashes here. That is real, and it is why _split_ref
        # partitions on the FIRST slash.
        "models": {
            "moonshotai/kimi-k2.5": {
                "id": "moonshotai/kimi-k2.5",
                "name": "Kimi K2.5",
                "reasoning": True,
                "reasoning_options": [{"type": "toggle"}],
                "tool_call": True,
                "limit": {"context": 256000, "output": 256000},
            },
            "maxed": {
                "id": "maxed",
                "name": "Maxed",
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["low", "high", "max"]}],
                "tool_call": True,
                "limit": {"context": 1048576, "output": 128000},
            },
            "budgeted": {
                "id": "budgeted",
                "name": "Budgeted",
                "reasoning": True,
                "reasoning_options": [{"type": "budget_tokens", "min": 1024, "max": 32768}],
                "tool_call": True,
                "limit": {"context": 200000, "output": 64000},
            },
            "alien-efforts": {
                "id": "alien-efforts",
                "name": "Alien",
                "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["default", "turbo"]}],
                "tool_call": True,
                "limit": {"context": 8192, "output": 4096},
            },
        },
    },
}


def _record(provider_id: str, model_id: str) -> dict:
    return get_record(CATALOG, provider_id, model_id)


class TestLookup:
    def test_iter_models_yields_every_pair(self):
        pairs = {(p, m) for p, m, _ in iter_models(CATALOG)}
        assert ("openai", "gpt-5.1") in pairs
        assert ("hpc-ai", "moonshotai/kimi-k2.5") in pairs

    def test_search_matches_id_name_and_provider(self):
        assert ("openai", "gpt-5.1") in find_models(CATALOG, "GPT-5")
        assert ("hpc-ai", "maxed") in find_models(CATALOG, "hpc")

    def test_search_is_substring_not_fuzzy(self):
        """A search that invents near-matches hides "this model is not listed"."""
        assert find_models(CATALOG, "gpt5") == []

    def test_an_unknown_provider_names_what_to_do(self):
        with pytest.raises(CatalogError, match="providers"):
            get_record(CATALOG, "nope", "gpt-5.1")

    def test_an_unknown_model_names_the_provider_it_looked_under(self):
        with pytest.raises(CatalogError, match="openai"):
            get_record(CATALOG, "openai", "gpt-9")

    @pytest.mark.parametrize("ref", ["openai", "/gpt-5.1", "openai/", ""])
    def test_a_malformed_reference_is_refused(self, ref):
        with pytest.raises(CatalogError):
            _split_ref(ref)

    def test_a_reference_splits_on_the_first_slash(self):
        """``hpc-ai/moonshotai/kimi-k2.5`` is one provider and one slashed id."""
        assert _split_ref("hpc-ai/moonshotai/kimi-k2.5") == ("hpc-ai", "moonshotai/kimi-k2.5")


class TestThinkingLevelMap:
    def test_effort_values_become_a_level_map(self):
        level_map, notes = thinking_level_map_from_record(_record("openai", "gpt-5.1"))
        assert level_map == {
            "off": "none",
            "minimal": None,
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": None,
        }
        assert notes == []

    def test_an_unoffered_level_is_null_not_absent(self):
        """``None`` marks a level unsupported; an ABSENT key means pass-through.

        ``get_supported_thinking_levels`` reads the difference, so a map that
        omitted the unsupported levels would advertise them instead of hiding
        them.
        """
        level_map, _ = thinking_level_map_from_record(_record("openai", "gpt-5.1"))
        assert "minimal" in level_map and level_map["minimal"] is None

    def test_the_map_drives_supported_levels(self):
        from tau_llm.models import get_supported_thinking_levels

        model, _ = model_from_record(
            _record("openai", "gpt-5.1"), base_url="https://api.openai.com/v1", provider="openai"
        )
        assert get_supported_thinking_levels(model) == ["off", "low", "medium", "high"]

    def test_a_max_effort_is_dropped_and_reported(self):
        """τ has no ``max`` level, and quietly aiming ``xhigh`` at it would make
        the config say something the operator never asked for."""
        level_map, notes = thinking_level_map_from_record(_record("hpc-ai", "maxed"))
        assert level_map["xhigh"] is None
        assert any("max" in note for note in notes)

    def test_a_toggle_yields_no_map_but_says_so(self):
        level_map, notes = thinking_level_map_from_record(
            _record("hpc-ai", "moonshotai/kimi-k2.5")
        )
        assert level_map is None
        assert any("toggle" in note for note in notes)

    def test_a_token_budget_yields_no_map_but_says_so(self):
        """τ can express a budget as a fragment, but the FIELD NAME is
        vendor-specific and the catalog does not carry it."""
        level_map, notes = thinking_level_map_from_record(_record("hpc-ai", "budgeted"))
        assert level_map is None
        assert any("budget_tokens" in note for note in notes)

    def test_efforts_with_no_tau_equivalent_yield_no_map(self):
        level_map, notes = thinking_level_map_from_record(_record("hpc-ai", "alien-efforts"))
        assert level_map is None
        assert any("turbo" in note for note in notes)

    def test_a_model_with_no_reasoning_options_is_silent(self):
        assert thinking_level_map_from_record({"id": "x"}) == (None, [])


class TestModelFromRecord:
    def test_limits_come_from_the_catalog(self):
        model, _ = model_from_record(
            _record("openai", "gpt-5.1"), base_url="https://api.openai.com/v1", provider="openai"
        )
        assert model.context_window == 400000
        assert model.max_tokens == 128000
        assert model.reasoning is True
        assert model.base_url == "https://api.openai.com/v1"

    def test_a_missing_limit_block_is_refused(self):
        """The defect this module replaces: a fabricated context window is not
        discovered until a turn is silently truncated."""
        with pytest.raises(CatalogError, match="limit"):
            model_from_record(_record("openai", "no-limits"), base_url="u", provider="openai")

    def test_a_zero_output_limit_is_refused(self):
        with pytest.raises(CatalogError, match="limit.output"):
            model_from_record(_record("openai", "zero-output"), base_url="u", provider="openai")

    def test_a_model_that_cannot_call_tools_is_refused_by_default(self):
        with pytest.raises(CatalogError, match="tools"):
            model_from_record(_record("openai", "no-tools"), base_url="u", provider="openai")

    def test_the_tool_call_refusal_is_overridable(self):
        model, _ = model_from_record(
            _record("openai", "no-tools"),
            base_url="u",
            provider="openai",
            require_tool_call=False,
        )
        assert model.id == "no-tools"

    def test_compat_is_passed_through_not_derived(self):
        """models.dev carries nothing about wire quirks, so nothing here infers
        any — :func:`tau_llm.compat.detect_compat` owns that, from the URL."""
        from tau_llm.compat import Compat

        stated = Compat(max_tokens_field="max_tokens")
        model, _ = model_from_record(
            _record("openai", "gpt-5.1"),
            base_url="https://api.openai.com/v1",
            provider="openai",
            compat=stated,
        )
        assert model.compat == stated


class TestConfigEntry:
    def test_the_entry_uses_the_keys_the_config_seam_reads(self):
        entry, _ = config_entry_from_record(
            _record("openai", "gpt-5.1"), base_url="https://api.openai.com/v1", provider="openai"
        )
        assert entry == {
            "backend": "openai",
            "model": "gpt-5.1",
            "base_url": "https://api.openai.com/v1",
            "context_window": 400000,
            "max_tokens": 128000,
            "reasoning": True,
            "thinking_level_map": {
                "off": "none",
                "minimal": None,
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": None,
            },
        }

    def test_a_non_reasoning_model_omits_the_reasoning_keys(self):
        """``reasoning`` is opt-in on Model, so a false value is noise that
        invites someone to flip it without knowing whether the endpoint agrees."""
        entry, _ = config_entry_from_record(
            _record("openai", "no-tools") | {"tool_call": True},
            base_url="u",
            provider="openai",
        )
        assert "reasoning" not in entry
        assert "thinking_level_map" not in entry

    def test_the_entry_round_trips_through_the_config_seam(self):
        """The point of the whole module: what it prints must be loadable.

        ``build_model_from_config`` lives in tau-coding-agent, which tau-llm does
        not depend on — imported inside the test so the suite still runs against
        a bare tau-llm install.
        """
        pytest.importorskip("tau_coding_agent")
        from tau_coding_agent.backends import build_model_from_config

        entry, _ = config_entry_from_record(
            _record("openai", "gpt-5.1"), base_url="https://api.openai.com/v1", provider="openai"
        )
        model = build_model_from_config(entry)
        assert model.context_window == 400000
        assert model.max_tokens == 128000
        assert model.reasoning is True
        assert model.thinking_level_map == entry["thinking_level_map"]


class TestLoadCatalog:
    def test_a_local_file_is_read(self, tmp_path):
        path = tmp_path / "api.json"
        path.write_text(json.dumps(CATALOG), encoding="utf-8")
        assert load_catalog(path)["openai"]["name"] == "OpenAI"

    def test_a_missing_file_names_the_path(self, tmp_path):
        with pytest.raises(CatalogError, match="api.json"):
            load_catalog(tmp_path / "api.json")

    def test_a_json_array_is_refused(self, tmp_path):
        path = tmp_path / "api.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(CatalogError, match="expected a JSON object"):
            load_catalog(path)


class TestCli:
    """The CLI is the surface an operator actually touches, so its contract —
    JSON on stdout, everything else on stderr — is pinned."""

    def _run(self, tmp_path, capsys, *args):
        path = tmp_path / "api.json"
        path.write_text(json.dumps(CATALOG), encoding="utf-8")
        code = main(["--catalog", str(path), *args])
        return code, capsys.readouterr()

    def test_config_prints_json_on_stdout_only(self, tmp_path, capsys):
        code, out = self._run(
            tmp_path, capsys, "config", "openai/gpt-5.1", "--base-url", "https://x/v1"
        )
        assert code == 0
        assert json.loads(out.out) == {
            "gpt-5.1": {
                "backend": "openai",
                "model": "gpt-5.1",
                "base_url": "https://x/v1",
                "context_window": 400000,
                "max_tokens": 128000,
                "reasoning": True,
                "thinking_level_map": {
                    "off": "none",
                    "minimal": None,
                    "low": "low",
                    "medium": "medium",
                    "high": "high",
                    "xhigh": None,
                },
            }
        }

    def test_the_credential_env_var_goes_to_stderr(self, tmp_path, capsys):
        """It lives on the PROVIDER record and is the one thing left to do after
        pasting the entry in, so it must be said — and must not corrupt stdout."""
        _, out = self._run(
            tmp_path, capsys, "config", "openai/gpt-5.1", "--base-url", "https://x/v1"
        )
        assert "OPENAI_API_KEY" in out.err

    def test_notes_go_to_stderr(self, tmp_path, capsys):
        _, out = self._run(tmp_path, capsys, "config", "hpc-ai/maxed", "--base-url", "https://x/v1")
        assert "max" in out.err
        assert json.loads(out.out)

    def test_search_lists_matches(self, tmp_path, capsys):
        code, out = self._run(tmp_path, capsys, "search", "kimi")
        assert code == 0
        assert out.out.strip() == "hpc-ai/moonshotai/kimi-k2.5"

    def test_search_with_no_match_exits_nonzero(self, tmp_path, capsys):
        code, out = self._run(tmp_path, capsys, "search", "nothing-like-this")
        assert code == 1
        assert "No model matches" in out.err

    def test_providers_lists_ids_and_env(self, tmp_path, capsys):
        code, out = self._run(tmp_path, capsys, "providers")
        assert code == 0
        assert "openai\t4 models\tOPENAI_API_KEY" in out.out

    def test_show_prints_the_raw_record(self, tmp_path, capsys):
        code, out = self._run(tmp_path, capsys, "show", "openai/gpt-5.1")
        assert code == 0
        assert json.loads(out.out)["limit"]["context"] == 400000

    def test_a_refusal_exits_nonzero_with_the_reason(self, tmp_path, capsys):
        code, out = self._run(
            tmp_path, capsys, "config", "openai/no-tools", "--base-url", "https://x/v1"
        )
        assert code == 1
        assert "tools" in out.err
        assert out.out == ""

    def test_base_url_is_required(self, tmp_path, capsys):
        """models.dev carries no URL, and one model id is served by many
        gateways — so τ asks rather than picking one."""
        with pytest.raises(SystemExit):
            self._run(tmp_path, capsys, "config", "openai/gpt-5.1")
