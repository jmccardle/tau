"""S48 (E7) — headless dialog policy: CLI flag parsing + config source merge.

Covers the coding-agent half of the headless dialog policy (E7 §3 / S48, anchor
G9, decision D-E6-2): the ``--ui-defaults METHOD=ANSWER,...`` flag, its parse
into ``{method: token}``, and the merge of ``~/.tau/config.json`` ``"ui_defaults"``
with the per-run overrides (CLI > config.json). The tau-agent-core half (a headless
dialog raising by default and honoring a set policy) lives in
``tau-agent-core/tests/test_extension_types.py`` /
``test_phase6_subphase3_errors.py``.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §3 S48; docs/EXTENSIONS-E5-WIRING.md.
"""

from __future__ import annotations

import pytest

from tau_coding_agent.cli import parse_cli_args
from tau_coding_agent.headless import (
    CLIError,
    parse_ui_defaults,
    resolve_ui_defaults,
)


# ── the flag parses onto CLIArgs ────────────────────────────────────────────


def test_ui_defaults_flag_absent_is_none():
    assert parse_cli_args(["-p", "x"]).ui_defaults is None


def test_ui_defaults_flag_captured():
    args = parse_cli_args(["--ui-defaults", "confirm=yes,select=first", "-p", "x"])
    assert args.ui_defaults == "confirm=yes,select=first"


# ── parse: "METHOD=ANSWER,..." → {method: token} ────────────────────────────


def test_parse_none_and_empty_are_empty():
    assert parse_ui_defaults(None) == {}
    assert parse_ui_defaults("") == {}
    assert parse_ui_defaults("   ") == {}


def test_parse_basic():
    assert parse_ui_defaults("confirm=yes,select=first") == {
        "confirm": "yes",
        "select": "first",
    }


def test_parse_strips_whitespace_and_skips_empty_items():
    assert parse_ui_defaults(" confirm = yes , , select=first ") == {
        "confirm": "yes",
        "select": "first",
    }


def test_parse_missing_equals_raises():
    with pytest.raises(CLIError, match="missing '='"):
        parse_ui_defaults("confirm")


def test_parse_empty_method_or_answer_raises():
    with pytest.raises(CLIError, match="non-empty"):
        parse_ui_defaults("=yes")
    with pytest.raises(CLIError, match="non-empty"):
        parse_ui_defaults("confirm=")


# ── resolve: config.json "ui_defaults" + overrides, CLI > config.json ───────


def test_resolve_config_only():
    config = {"ui_defaults": {"confirm": "no", "select": "first"}}
    assert resolve_ui_defaults(config, {}) == {"confirm": "no", "select": "first"}


def test_resolve_override_wins_per_method():
    config = {"ui_defaults": {"confirm": "no", "select": "first"}}
    merged = resolve_ui_defaults(config, {"confirm": "yes"})
    # confirm overridden by CLI, select survives from config.json.
    assert merged == {"confirm": "yes", "select": "first"}


def test_resolve_missing_block_is_empty():
    assert resolve_ui_defaults({}, {}) == {}


def test_resolve_stringifies_json_values():
    # A JSON bool in config becomes a token the policy validator recognises.
    config = {"ui_defaults": {"confirm": True}}
    assert resolve_ui_defaults(config, {}) == {"confirm": "True"}


def test_resolve_non_object_block_raises():
    with pytest.raises(CLIError, match='"ui_defaults" must be a JSON object'):
        resolve_ui_defaults({"ui_defaults": ["confirm=yes"]}, {})
