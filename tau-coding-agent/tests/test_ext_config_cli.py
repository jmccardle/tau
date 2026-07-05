"""S40 (E6) — per-extension config: CLI parsing, value decoding, source merge.

Covers the coding-agent half of ``api.config`` (E6 §2 / S40): the ``--ext-config
NAME.KEY=VALUE`` flag, its value-decoding rule, and the merge of
``~/.tau/config.json`` ``"extensions"`` slices with the per-run overrides
(CLI > config.json). The tau-agent-core half (the sliced dict actually reaching
each extension's ``api.config``) lives in
``tau-agent-core/tests/test_extension_config.py``.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S40; docs/EXTENSIONS-E5-WIRING.md.
"""

from __future__ import annotations

import pytest

from tau_coding_agent.cli import parse_cli_args
from tau_coding_agent.headless import (
    CLIError,
    _decode_ext_config_value,
    parse_ext_config_overrides,
    resolve_extensions_config,
)


# ── the flag parses (repeatable, normalized to a list) ──────────────────────


def test_ext_config_flag_is_repeatable():
    assert parse_cli_args(["-p", "x"]).ext_config == []
    args = parse_cli_args(
        ["--ext-config", "budget.ceiling=5.0", "--ext-config", "gate.paths=[]", "-p", "x"]
    )
    assert args.ext_config == ["budget.ceiling=5.0", "gate.paths=[]"]


# ── value decoding: JSON when it parses, else a plain string ────────────────


def test_decode_value_json_typed():
    assert _decode_ext_config_value("5.0") == 5.0
    assert isinstance(_decode_ext_config_value("5.0"), float)
    assert _decode_ext_config_value("10") == 10
    assert _decode_ext_config_value("true") is True
    assert _decode_ext_config_value("false") is False
    assert _decode_ext_config_value("null") is None
    assert _decode_ext_config_value('["a", "b"]') == ["a", "b"]
    assert _decode_ext_config_value('{"k": 1}') == {"k": 1}
    assert _decode_ext_config_value('"quoted"') == "quoted"


def test_decode_value_bare_string_kept():
    # A bare unquoted word is not valid JSON → stays a string (no fabrication).
    assert _decode_ext_config_value("strict") == "strict"
    assert _decode_ext_config_value("src/") == "src/"
    assert _decode_ext_config_value("") == ""


# ── override parsing: NAME.KEY=VALUE → {name: {key: value}} ─────────────────


def test_parse_overrides_basic():
    got = parse_ext_config_overrides(["budget.ceiling=5.0", "gate.mode=strict"])
    assert got == {"budget": {"ceiling": 5.0}, "gate": {"mode": "strict"}}


def test_parse_overrides_accumulates_keys_per_extension():
    got = parse_ext_config_overrides(["budget.ceiling=5.0", "budget.warn=0.8"])
    assert got == {"budget": {"ceiling": 5.0, "warn": 0.8}}


def test_parse_overrides_value_may_contain_equals():
    # split on the FIRST '=' only.
    got = parse_ext_config_overrides(["ext.token=a=b=c"])
    assert got == {"ext": {"token": "a=b=c"}}


def test_parse_overrides_key_may_contain_dot():
    # NAME is up to the FIRST '.'; the remainder is the (dotted) key.
    got = parse_ext_config_overrides(["ext.a.b=1"])
    assert got == {"ext": {"a.b": 1}}


def test_parse_overrides_missing_equals_raises():
    with pytest.raises(CLIError, match="missing '='"):
        parse_ext_config_overrides(["budget.ceiling"])


def test_parse_overrides_missing_dot_raises():
    with pytest.raises(CLIError, match="no '\\.'"):
        parse_ext_config_overrides(["ceiling=5.0"])


def test_parse_overrides_empty_name_or_key_raises():
    with pytest.raises(CLIError, match="non-empty"):
        parse_ext_config_overrides([".ceiling=5.0"])
    with pytest.raises(CLIError, match="non-empty"):
        parse_ext_config_overrides(["budget.=5.0"])


# ── source merge: config.json slices + overrides, CLI > config.json ─────────


def _config() -> dict:
    return {
        "extensions": {
            "budget": {"ceiling": 1.0, "warn": 0.5},
            "gate": {"paths": ["src/"]},
        }
    }


def test_resolve_config_only_no_overrides():
    merged = resolve_extensions_config(_config(), {})
    assert merged == {"budget": {"ceiling": 1.0, "warn": 0.5}, "gate": {"paths": ["src/"]}}


def test_resolve_override_wins_per_key():
    merged = resolve_extensions_config(_config(), {"budget": {"ceiling": 9.0}})
    # ceiling overridden, warn survives from config.json.
    assert merged["budget"] == {"ceiling": 9.0, "warn": 0.5}
    assert merged["gate"] == {"paths": ["src/"]}


def test_resolve_override_adds_new_extension():
    merged = resolve_extensions_config(_config(), {"newext": {"k": "v"}})
    assert merged["newext"] == {"k": "v"}


def test_resolve_missing_extensions_block_is_empty():
    assert resolve_extensions_config({}, {}) == {}


def test_resolve_does_not_mutate_source_config():
    config = _config()
    resolve_extensions_config(config, {"budget": {"ceiling": 9.0}})
    # The source config.json dict is untouched (a fresh copy is returned).
    assert config["extensions"]["budget"]["ceiling"] == 1.0


def test_resolve_non_object_extensions_raises():
    with pytest.raises(CLIError, match='"extensions" must be a JSON object'):
        resolve_extensions_config({"extensions": ["budget"]}, {})


def test_resolve_non_object_entry_raises():
    with pytest.raises(CLIError, match='"extensions.budget" must be a JSON object'):
        resolve_extensions_config({"extensions": {"budget": 5}}, {})
