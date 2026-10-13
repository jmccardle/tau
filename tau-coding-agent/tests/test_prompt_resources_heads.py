"""Prompt templates and skills reach the heads: flags, config, ``tau -p``, the backend.

Reference: docs/PROMPT-TEMPLATES-AND-SKILLS.md §6–§7.

The core half is tau-agent-core/tests/test_prompt_resources.py. This file pins
what only the coding agent does: the four CLI flags and the two config.json
lists joining in one precedence order (``apply_resource_flags``, shared by every
head), ``TauBackend`` loading both catalogs and listing skills in its system
prompt, and ``tau -p`` persisting the EXPANDED prompt — which it did not do for
any input transform before §7, because it appended the typed text itself.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import tau_coding_agent.session_store as store
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, TextContent, Usage
from tau_coding_agent.backends import create_backend
from tau_coding_agent.cli import CLIArgs, parse_cli_args
from tau_coding_agent.headless import CLIError, apply_resource_flags, run_print

_TS = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _no_user_dir(tmp_path, monkeypatch):
    """Point ``~/.tau`` discovery at an empty directory, so the developer's own files stay out."""
    monkeypatch.setattr("tau_agent_core.resources.AGENT_DIR", str(tmp_path / "home"))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestFlags:
    def test_the_four_flags_parse(self):
        args = parse_cli_args(
            ["--prompt-template", "a", "--prompt-template", "b", "--skill", "s", "--no-skills"]
        )
        assert args.prompt_templates == ["a", "b"] and args.skills == ["s"]
        assert args.no_skills is True and args.no_prompt_templates is False

    def test_cli_then_model_entry_then_config_and_the_switches(self):
        model_config = {"prompt_templates": ["entry"]}
        apply_resource_flags(
            model_config,
            {"prompt_templates": ["global", "entry"], "skills": ["~/.claude/skills"]},
            prompt_templates=["cli"],
            no_prompt_templates=True,
            skills=[],
            no_skills=False,
        )
        assert model_config["prompt_templates"] == ["cli", "entry", "global"]
        assert model_config["skills"] == ["~/.claude/skills"]
        assert model_config["no_prompt_templates"] is True and "no_skills" not in model_config

    def test_a_config_value_that_is_not_a_list_of_paths_is_refused(self):
        with pytest.raises(CLIError, match="list of paths"):
            apply_resource_flags(
                {},
                {"skills": "~/.claude/skills"},
                prompt_templates=[],
                no_prompt_templates=False,
                skills=[],
                no_skills=False,
            )


def _model_config(tmp_path: Path, **extra) -> dict:
    return {
        "backend": "openai",
        "model": "qwen",
        "base_url": "http://localhost:8080/v1",
        "api_key": "not-needed",
        "cwd": str(tmp_path),
        "no_context_files": True,
        **extra,
    }


class TestBackend:
    def test_skills_are_listed_and_templates_offered(self, tmp_path):
        _write(tmp_path / "lib" / "pdf" / "SKILL.md", "---\ndescription: PDFs\n---\nbody")
        _write(tmp_path / "p" / "review.md", "---\ndescription: Review\n---\nReview $ARGUMENTS")
        backend = create_backend(
            _model_config(
                tmp_path,
                tools=["read"],
                skills=["lib"],
                prompt_templates=["p"],
                no_skills=True,
                no_prompt_templates=True,
            )
        )
        assert "<name>pdf</name>" in backend.system_prompt
        names = {c.name: c.origin for c in backend.get_prompt_commands()}
        assert names == {"review": "template", "skill:pdf": "skill"}

    def test_project_templates_load_and_project_skills_do_not(self, tmp_path):
        _write(tmp_path / ".tau" / "prompts" / "local.md", "local body")
        _write(tmp_path / ".tau" / "skills" / "s" / "SKILL.md", "---\ndescription: d\n---\n")
        backend = create_backend(_model_config(tmp_path, tools=["read"]))
        names = {c.name for c in backend.get_prompt_commands()}
        assert "local" in names and "skill:s" not in names
        assert "<available_skills>" not in backend.system_prompt


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="qwen",
        stop_reason="stop",
        timestamp=_TS,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    def __aiter__(self):
        async def _gen():
            yield TextDeltaEvent(delta="ok", partial=_assistant("ok"))
            yield DoneEvent(final=_assistant("ok"), usage=_assistant("ok").usage)

        return _gen()

    async def result(self):
        return _assistant("ok")

    def abort(self):
        pass


class TestPrintMode:
    async def test_print_mode_persists_the_expanded_prompt(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(store, "TAU_DIR", tmp_path)
        _write(tmp_path / "p" / "review.md", "Review $ARGUMENTS carefully")
        config = {
            "models": {"local-llm": _model_config(tmp_path, tools=[])},
            "default_model": "local-llm",
            "prompt_templates": [str(tmp_path / "p")],
        }
        seen: list = []

        async def _stream_simple(model, context, options=None):
            seen.append(str(context))
            return _Stream()

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_simple):
            rc = await run_print(CLIArgs(messages=["/review the parser"], print_mode=True), config)
        assert rc == 0
        assert "Review the parser carefully" in seen[0] and "/review" not in seen[0]

        (path,) = list((tmp_path / "sessions").rglob("*.jsonl"))
        users = [m for m in store.Session.load(path).messages if m["role"] == "user"]
        assert len(users) == 1
        content = users[0]["content"]
        text = content if isinstance(content, str) else "".join(b["text"] for b in content)
        assert text == "Review the parser carefully"
