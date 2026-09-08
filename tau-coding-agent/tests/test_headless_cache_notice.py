"""Print mode's delivery of the prompt-cache notice.

The verdict is ``tau_agent_core.prompt_cache``'s and is tested there; what is
head-specific is where the sentence goes. It goes to **stderr**, because
``--mode text`` is a transcript and ``--mode json`` is JSONL and both are
routinely redirected — a diagnostic on stdout would corrupt either.

Reference: docs/PROMPT-CACHING.md §7.
"""

from __future__ import annotations

import pytest

import tau_coding_agent.session_store as store
from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import report_cache_miss, run_print


def _config() -> dict:
    return {
        "models": {
            "local-llm": {
                "backend": "openai",
                "model": "qwen3-32b-kv4b",
                "base_url": "http://localhost:8080/v1",
                "api_key": "not-needed",
            },
        },
        "default_model": "local-llm",
        "system_prompt": "You are helpful.",
    }


def _assistant(read: int, total: int) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": "ANSWER"}],
        "usage": {
            "cache_read_tokens": read,
            "cache_reported": True,
            "output_tokens": 10,
            "total_tokens": total,
        },
    }


class _CacheBackend:
    """A backend whose turn produced ``messages``, and nothing else."""

    def __init__(self, config, messages):
        self.config = config
        self._messages = messages

    async def load_extensions(self, *a, **k):
        from tau_agent_core.sdk import LoadExtensionsResult

        return LoadExtensionsResult()

    async def stream_submission(self, submission, context, callback, **kwargs):
        callback("ANSWER")
        return (
            "ANSWER",
            {"total_tokens": 1},
            self._messages,
            [],
            SubmissionResult(
                accepted=True,
                submission_id=submission.submission_id,
                messages=self._messages,
            ),
        )


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)

    async def _run(messages: list[dict], mode: str = "text") -> None:
        monkeypatch.setattr(
            "tau_coding_agent.backends.create_backend",
            lambda config: _CacheBackend(config, messages),
        )
        rc = await run_print(CLIArgs(messages=["hi"], print_mode=True, mode=mode), _config())
        assert rc == 0

    return _run


class TestTheFunction:
    def test_a_cache_less_tool_loop_is_written_to_stderr(self, capsys):
        written = report_cache_miss(
            [_assistant(0, 30_010), _assistant(0, 34_010)], "gateway-sonnet"
        )
        assert written is not None
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "2 calls" in captured.err
        assert "gateway-sonnet" in captured.err
        assert "prompt_cache_dialect" in captured.err

    def test_a_healthy_turn_writes_nothing(self, capsys):
        assert report_cache_miss([_assistant(0, 30_010), _assistant(29_000, 34_010)], "m") is None
        assert capsys.readouterr().err == ""

    def test_a_turn_with_no_usage_writes_nothing(self, capsys):
        assert report_cache_miss([{"role": "assistant", "content": []}], "m") is None
        assert capsys.readouterr().err == ""


class TestTheRun:
    async def test_text_mode_keeps_the_transcript_clean(self, run, capsys):
        await run([_assistant(0, 30_010), _assistant(0, 34_010)])
        captured = capsys.readouterr()
        assert "2 calls" in captured.err
        assert "prompt-cache" not in captured.out

    async def test_json_mode_keeps_the_lines_parseable(self, run, capsys):
        import json

        await run([_assistant(0, 30_010), _assistant(0, 34_010)], mode="json")
        captured = capsys.readouterr()
        assert "2 calls" in captured.err
        for line in captured.out.splitlines():
            json.loads(line)

    async def test_a_healthy_run_says_nothing(self, run, capsys):
        await run([_assistant(0, 30_010), _assistant(29_000, 34_010)])
        assert capsys.readouterr().err == ""
