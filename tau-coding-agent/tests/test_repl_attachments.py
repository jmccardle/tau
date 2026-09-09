"""``@file`` at the REPL prompt — docs/FILE-ATTACHMENTS.md §2, §5.

The head expands at the moment the submission is built, so what is persisted and
what the model saw are the same string; the buffer a steering line waits in keeps
the RAW text, because reclaimed text must be expandable exactly once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tau_coding_agent.config import ConfigError

from repl_fakes import FakeBackend, ReplEnv, env  # noqa: F401

#: A turn with a tool call in it — the ``"steer"`` strategy's delivery point.
TURN_WITH_A_TOOL: list[dict[str, Any]] = [
    {"kind": "lane_start", "source": "interactive", "submitter": "human", "text": "do it"},
    {"kind": "text_delta", "delta": "Looking.\n"},
    {"kind": "tool_call", "id": "t1", "name": "read", "arguments": {"path": "main.py"}},
    {"kind": "lane_end", "context": 30, "output": 12, "seconds": 0.4},
]


async def test_a_reference_is_expanded_into_the_prompt_that_is_sent(
    env: ReplEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block goes in FRONT and the ``@word`` stays where it was typed: the
    model reads the material first and the instruction last."""
    (tmp_path / "notes.txt").write_text("remember the milk\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    await env.run(["summarize @notes.txt"])
    sent = env.submissions[0].text
    assert sent.startswith('<attachment filename="notes.txt">\nremember the milk\n</attachment>')
    assert sent.endswith("summarize @notes.txt")


async def test_a_word_that_names_no_file_is_left_as_prose(
    env: ReplEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolved ``@…`` is the human's own text; nothing is invented for it."""
    monkeypatch.chdir(tmp_path)
    await env.run(["ask @someone about it"])
    assert env.submissions[0].text == "ask @someone about it"


async def test_a_steering_line_is_expanded_at_its_delivery_point(
    env: ReplEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expanded when it is DELIVERED, not when it was typed, so a file edited
    during the turn is sent as it stands at the tool call."""
    (tmp_path / "spec.md").write_text("the rule\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def prepare(backend: FakeBackend) -> None:
        backend.script = list(TURN_WITH_A_TOOL)

    env.install(prepare)
    await env.run(["do it", "follow @spec.md"])
    steer = env.submissions[1]
    assert steer.multitask_strategy == "steer"
    assert '<attachment filename="spec.md">\nthe rule\n</attachment>' in steer.text
    assert steer.text.endswith("follow @spec.md")


async def test_a_bad_size_limit_is_refused_before_anything_is_built(env: ReplEnv) -> None:
    """Fail-Early: the key decides whether a file's CONTENT reaches the model, so a
    typo that silently restored 10 KB would be invisible until a large file went."""
    env.config["attachment_inline_limit"] = "10kb"
    with pytest.raises(ConfigError):
        await env.run(["hello"])
    assert env.backend is None
