"""Tests for ``examples/37_inline_bash.py`` — the ``input`` hook ``!{cmd}`` port (S62).

Proves:

* ``expand_inline_bash`` replaces every ``!{command}`` with trimmed stdout, leaves
  whole-line ``!command`` (not ``!{...}``) untouched (pi parity), and reports "no
  expansions" when nothing matched (so the hook can pass the prompt through
  unchanged rather than "transforming" to an identical copy);
* a failing command becomes an ``[error: ...]`` marker inline, not a crash;
* through the FULL ``AgentSession.prompt`` path (only the network boundary
  faked), the EXPANDED text — and only it — is what the model sees and what
  lands as the persisted user node (the S42 pre-node/durable invariant);
* the expansion survives a reload (a fresh fold over the log's raw entries),
  à la S29/S42's own reload-invariance proof.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S62.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import InMemorySessionLog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "37_inline_bash.py"
_spec = importlib.util.spec_from_file_location("inline_bash_37_example", _PATH)
assert _spec is not None and _spec.loader is not None
inline_bash = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = inline_bash
_spec.loader.exec_module(inline_bash)


# ── pure expansion logic ─────────────────────────────────────────────────────


def test_expand_replaces_single_command():
    expanded, expansions = inline_bash.expand_inline_bash("What's in !{echo hi}?")
    assert expanded == "What's in hi?"
    assert len(expansions) == 1
    assert expansions[0].command == "echo hi"
    assert expansions[0].output == "hi"
    assert expansions[0].error is None


def test_expand_replaces_multiple_commands():
    expanded, expansions = inline_bash.expand_inline_bash("!{echo a} and !{echo b}")
    assert expanded == "a and b"
    assert [e.command for e in expansions] == ["echo a", "echo b"]


def test_expand_no_pattern_is_noop():
    expanded, expansions = inline_bash.expand_inline_bash("plain text, no bash here")
    assert expanded == "plain text, no bash here"
    assert expansions == []


def test_expand_preserves_whole_line_bang_command():
    """A whole-line ``!command`` (not ``!{...}``) is left untouched (pi parity)."""
    text = "!ls -la"
    expanded, expansions = inline_bash.expand_inline_bash(text)
    assert expanded == text
    assert expansions == []


def test_expand_does_not_skip_bang_brace_prefixed_text():
    """``!{...}`` at the start of the text IS expanded — only bare ``!cmd`` is skipped."""
    expanded, expansions = inline_bash.expand_inline_bash("!{echo start}, rest")
    assert expanded == "start, rest"
    assert len(expansions) == 1


def test_expand_failing_command_becomes_error_marker():
    """A non-zero exit WITH stderr output (pi parity: only that case is flagged
    an error — see ``_run_command``) becomes an ``[error: ...]`` marker inline."""
    expanded, expansions = inline_bash.expand_inline_bash("!{bad-nonexistent-cmd-xyz}")
    assert "[error:" in expanded
    assert expansions[0].error is not None


def test_expand_nonzero_exit_with_no_stderr_is_not_flagged_an_error():
    """pi parity: a non-zero exit with EMPTY stderr is not an error entry — just
    empty output (mirrors pi's ``if (code !== 0 && stderr) { ...error... }``)."""
    expanded, expansions = inline_bash.expand_inline_bash("!{exit 7}")
    assert expanded == ""
    assert expansions[0].error is None


# ── full AgentSession.prompt() path: expansion is pre-node and durable ──────


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(),
    )


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event

    async def result(self) -> Any:
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self) -> None:
        pass


def _fake_text_reply(captured: list[dict[str, Any]]):
    async def fake(model, context, options=None):
        if isinstance(context, dict):
            captured.append(context)
        final = _text_assistant("ok")
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _user_texts(messages: list[Any]) -> list[str]:
    out: list[str] = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role != "user":
            continue
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, str):
            out.append(content)
            continue
        for block in content or []:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if btype == "text" and text is not None:
                out.append(text)
    return out


class _NullUI:
    def notify(self, message: str, level: str = "info") -> None:
        pass


def _session_with_inline_bash() -> AgentSession:
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt="You are a helpful assistant.",
        extensions=[],
    )
    inline_bash.inline_bash_extension(session._bind_extension_api("examples/37_inline_bash.py"))
    session.set_ui_delegate(_NullUI())
    return session


async def test_prompt_reaches_model_expanded_and_only_expanded() -> None:
    session = _session_with_inline_bash()
    captured: list[dict[str, Any]] = []
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply(captured)):
        await session.prompt("The greeting is: !{echo hello}")

    wire_texts = _user_texts(captured[0]["messages"])
    assert wire_texts == ["The greeting is: hello"]
    # The single persisted copy carries the expansion, not the original pattern.
    assert _user_texts(session.messages) == ["The greeting is: hello"]


async def test_prompt_with_no_pattern_passes_through_unchanged() -> None:
    session = _session_with_inline_bash()
    captured: list[dict[str, Any]] = []
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply(captured)):
        await session.prompt("just a normal question")

    assert _user_texts(captured[0]["messages"]) == ["just a normal question"]


async def test_expansion_survives_reload() -> None:
    """Reload-invariance (S29/S42 style): a fresh fold over the raw log entries
    still carries the EXPANDED text — no separate original copy anywhere."""
    session = _session_with_inline_bash()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_text_reply([])):
        await session.prompt("value=!{echo 42}")

    reloaded = ConversationTree(
        session.session_log.entries(), session.session_log.cursor
    ).context_for()
    assert _user_texts(reloaded) == ["value=42"]
