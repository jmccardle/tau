"""Print mode's delivery of the truncation notice.

The verdict is ``tau_agent_core.truncation``'s and is tested there; what is
head-specific is where the sentence goes and which cap it quotes. It goes to
**stderr**, because ``--mode text`` is a transcript and ``--mode json`` is JSONL
and both are routinely redirected — a diagnostic on stdout would corrupt either.

Reference: docs/TRUNCATED-TOOL-CALLS.md §3.
"""

from __future__ import annotations

import pytest

import tau_coding_agent.session_store as store
from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import report_truncation, run_print


def _config(max_tokens: int | None = None) -> dict:
    entry = {
        "backend": "openai",
        "model": "qwen3-32b-kv4b",
        "base_url": "http://localhost:8080/v1",
        "api_key": "not-needed",
    }
    if max_tokens is not None:
        entry["max_tokens"] = max_tokens
    return {
        "models": {"local-llm": entry},
        "default_model": "local-llm",
        "system_prompt": "You are helpful.",
    }


def _assistant(stop_reason: str, dropped: int | None = None) -> dict:
    usage: dict = {"total_tokens": 4100, "output_tokens": 4096}
    if dropped is not None:
        usage["extra"] = {"dropped_partial_tool_calls": dropped}
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": "ANSWER"}],
        "usage": usage,
        "stop_reason": stop_reason,
    }


class _Backend:
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

    async def _run(messages: list[dict], config: dict | None = None, mode: str = "text") -> None:
        monkeypatch.setattr(
            "tau_coding_agent.backends.create_backend",
            lambda cfg: _Backend(cfg, messages),
        )
        rc = await run_print(
            CLIArgs(messages=["hi"], print_mode=True, mode=mode), config or _config()
        )
        assert rc == 0

    return _run


class TestTheFunction:
    def test_a_truncated_completion_is_written_to_stderr(self, capsys):
        written = report_truncation([_assistant("length")], 4096)
        assert written is not None
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "output cap" in captured.err
        assert "max_tokens = 4096" in captured.err

    def test_a_dropped_tool_call_is_counted_in_the_line(self, capsys):
        report_truncation([_assistant("length", 2)], 4096)
        assert "2 tool calls were dropped" in capsys.readouterr().err

    def test_a_clean_turn_writes_nothing(self, capsys):
        assert report_truncation([_assistant("stop")], 4096) is None
        assert capsys.readouterr().err == ""

    def test_an_unknown_cap_is_reported_as_unknown(self, capsys):
        report_truncation([_assistant("length")], None)
        assert "max_tokens = unknown" in capsys.readouterr().err


class TestTheRun:
    async def test_text_mode_keeps_the_transcript_clean(self, run, capsys):
        await run([_assistant("length")])
        captured = capsys.readouterr()
        assert "output cap" in captured.err
        assert "output cap" not in captured.out

    async def test_json_mode_keeps_the_lines_parseable(self, run, capsys):
        import json

        await run([_assistant("length")], mode="json")
        captured = capsys.readouterr()
        assert "output cap" in captured.err
        for line in captured.out.splitlines():
            json.loads(line)

    async def test_it_quotes_the_cap_this_run_actually_sent(self, run, capsys):
        """The number on screen is the number on the wire, read from the same
        model entry ``create_backend`` resolved its ``Model`` from."""
        await run([_assistant("length")], config=_config(max_tokens=900))
        assert "max_tokens = 900" in capsys.readouterr().err

    async def test_an_unstated_cap_reports_the_default(self, run, capsys):
        await run([_assistant("length")])
        assert "max_tokens = 4096" in capsys.readouterr().err

    async def test_a_clean_run_says_nothing(self, run, capsys):
        await run([_assistant("stop")])
        assert capsys.readouterr().err == ""
