"""``{{field}}`` placeholders in a system prompt.

The point of the feature: ``custom_prompt`` replaces τ's base text wholesale, so
a user who wanted their own voice had to give up the ``Available tools:`` list
and the project context that compose around it — or rather, had to accept
wherever the builder chose to put them. Placeholders let the base text say where
those sections go, and let a custom prompt reach the facts τ already knows.

The rule that keeps it honest: an unknown field RAISES. A misspelled
``{{tols}}`` rendered literally would look exactly like a prompt that worked.
"""

from __future__ import annotations

import pytest

from tau_agent_core.sdk import (
    BASE_SYSTEM_PROMPT,
    SystemPromptFieldError,
    _build_system_prompt,
    _resolve_tools,
)

TOOLS = _resolve_tools(["read", "bash"])


def _build(custom: str | None, tmp_path, **kwargs) -> str:
    """Build against an empty directory so only the named fields vary.

    ``agent_dir`` and ``cwd`` both point at a scratch tree: the ancestor walk
    reaches ``/`` by design, and a developer's own ``~/AGENTS.md`` would
    otherwise decide what these tests see.
    """
    return _build_system_prompt(
        cwd=str(tmp_path),
        tools=TOOLS,
        custom_prompt=custom,
        agent_dir=str(tmp_path / "agent"),
        **kwargs,
    )


def test_unknown_field_raises_instead_of_rendering_literally(tmp_path):
    with pytest.raises(SystemPromptFieldError) as excinfo:
        _build("You have {{tols}}.", tmp_path)
    message = str(excinfo.value)
    assert "{{tols}}" in message
    # The message must name what IS available, or the user is left guessing.
    assert "{{tool_names}}" in message


def test_base_prompt_inside_the_base_text_says_why_it_cannot_work(tmp_path):
    """``{{base_prompt}}`` in the base text is self-reference, not a typo.

    Reported as its own case rather than as an ordinary unknown field, because
    "unknown" would be a lie — the field exists, just not there — and would send
    the reader looking for a spelling mistake.
    """
    from tau_agent_core.sdk import _render_prompt_fields

    with pytest.raises(SystemPromptFieldError) as excinfo:
        _render_prompt_fields("{{base_prompt}}", {"cwd": "/x"})
    assert "cannot embed itself" in str(excinfo.value)


def test_base_prompt_field_wraps_tau_voice_instead_of_replacing_it(tmp_path):
    prompt = _build("PREAMBLE\n\n{{base_prompt}}\n\nPOSTAMBLE", tmp_path)
    assert prompt.startswith("PREAMBLE")
    assert "You are Tau, a coding agent." in prompt
    assert "POSTAMBLE" in prompt


def test_tools_placeholder_moves_the_block_and_does_not_duplicate_it(tmp_path):
    prompt = _build("TOP\n\n{{tools}}\n\nBOTTOM", tmp_path)
    assert prompt.count("Available tools:") == 1
    assert prompt.index("Available tools:") < prompt.index("BOTTOM")


def test_a_prompt_naming_no_section_composes_exactly_as_before(tmp_path):
    """The feature is invisible to a template that does not ask for it."""
    prompt = _build("JUST MY TEXT", tmp_path)
    assert prompt.startswith("JUST MY TEXT")
    # The tool list is still appended, after the text, exactly as it always was.
    assert prompt.count("Available tools:") == 1
    assert prompt.index("Available tools:") > prompt.index("JUST MY TEXT")


def test_tool_names_is_a_mention_not_the_section(tmp_path):
    """Using ``{{tool_names}}`` must not suppress the block — it is not the block."""
    prompt = _build("I can call: {{tool_names}}.", tmp_path)
    assert "I can call: read, bash." in prompt
    assert "Available tools:" in prompt


def test_cwd_and_model_reach_the_prompt(tmp_path):
    prompt = _build("at {{cwd}} as {{model}}", tmp_path, model="qwen-x")
    assert f"at {tmp_path} as qwen-x" in prompt


def test_an_empty_section_slot_leaves_no_hole(tmp_path):
    """No context file means the slot's line goes, not a blank gap in its place."""
    prompt = _build("TOP\n\n{{project_context}}\n\nBOTTOM", tmp_path)
    assert prompt.startswith("TOP\n\nBOTTOM")
    assert "\n\n\n" not in prompt
    assert "{{project_context}}" not in prompt


def test_substitution_never_rewrites_a_context_file(tmp_path):
    """``{{tools}}`` inside a project's AGENTS.md is user content, not a slot.

    Substitution runs on the template only. A project whose instructions happen
    to discuss placeholders must reach the model with them intact.
    """
    (tmp_path / "AGENTS.md").write_text("Our docs use {{tools}} as an example.\n")
    prompt = _build("MY TEXT", tmp_path)
    assert "Our docs use {{tools}} as an example." in prompt


def test_the_shipped_base_prompt_renders(tmp_path):
    """τ's own default is a template; a bad field in it would break every run."""
    prompt = _build(None, tmp_path, model="m")
    assert "{{" not in prompt
    assert prompt.startswith("You are Tau, a coding agent.")
    assert "Available tools:" in prompt
    # The default carries the real newlines its markdown headings need.
    assert "\n## Behavior\n" in BASE_SYSTEM_PROMPT
    assert "\\n" not in BASE_SYSTEM_PROMPT
