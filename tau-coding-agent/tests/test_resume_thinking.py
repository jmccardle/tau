"""A resumed session runs at the thinking level its config recorded (docs/CURSORS.md §5).

``resolve_model_config`` is the one place ``tau -p``, the REPL and RPC turn CLI
flags into a backend config; ``--thinking`` and a ``:level`` suffix still win.
"""

from __future__ import annotations

from tau_coding_agent.cli import parse_cli_args
from tau_coding_agent.headless import resolve_model_config

CONFIG = {"models": {"fast": {"backend": "openai", "model": "gpt-4o"}}}


def test_a_recorded_level_is_resumed_when_no_flag_names_one():
    _, model_config = resolve_model_config(
        CONFIG,
        parse_cli_args(["-p", "x"]),
        fallback_model="fast",
        prior_config={"thinking": "high"},
    )
    assert model_config["thinking"] == "high"


def test_a_recorded_none_resumes_with_thinking_off():
    _, model_config = resolve_model_config(
        CONFIG, parse_cli_args(["-p", "x"]), fallback_model="fast", prior_config={"thinking": None}
    )
    assert model_config["thinking"] == "off"


def test_the_flag_wins_over_the_recorded_level():
    _, model_config = resolve_model_config(
        CONFIG,
        parse_cli_args(["-p", "x", "--thinking", "low"]),
        fallback_model="fast",
        prior_config={"thinking": "high"},
    )
    assert model_config["thinking"] == "low"


def test_a_session_that_recorded_no_level_leaves_the_config_alone():
    _, model_config = resolve_model_config(
        CONFIG, parse_cli_args(["-p", "x"]), fallback_model="fast", prior_config={"model": "fast"}
    )
    assert "thinking" not in model_config
