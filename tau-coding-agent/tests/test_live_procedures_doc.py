"""E5 §6 (E5.5 / S37) — the live-procedures doc is real, not aspirational.

``docs/EXTENSIONS-LIVE-PROCEDURES.md`` is the manual companion to the automated floor
(S36). This test proves the doc's central claim — *the doc lists a concrete, runnable
manual procedure per demo plus the reload check, each naming the expected durable
observation* — WITHOUT being a tautology (it does not merely grep the prose):

* :func:`test_doc_lists_a_procedure_per_demo_plus_reload` parses the doc into its five
  procedures (gatekeeper / reminders / budget / delegate / reload) and, for each of
  the four demos, LOADS the exact example file named in the doc's command through the
  REAL public ``-e`` loader (``sdk._load_extensions`` → ``getattr(module, "register")``
  — the same code path ``tau -e <file>`` runs). It then asserts the loaded extension
  registered exactly the surface the doc's observation depends on (gatekeeper → a
  ``tool_call`` hook, reminders → ``tool_result`` + ``before_agent_start``, budget →
  ``tool_result``, delegate → the ``delegate`` tool), and that the doc's stated
  observation names the right durable-node shape. A wrong path, an unloadable example,
  or an observation describing the wrong mechanism all fail this — it binds doc→reality.

* :func:`test_gatekeeper_blocked_node_is_durable_in_transcript_and_tree` drives the
  doc's §1 procedure end-to-end: the real gatekeeper example, loaded through the live
  session's ``-e`` seam, vetoes an out-of-scope ``write`` inside the FULL agent loop
  (only ``stream_simple`` is faked). It asserts the blocked ``is_error`` ``toolResult``
  lands in BOTH the returned transcript AND the persisted session log (the tree) —
  proving "durable node in tree AND transcript", not just described.

* :func:`test_reload_check_node_survives_byte_identical` drives the doc's §5 procedure:
  a durable ``customMessage`` node (the reminders preamble shape) persisted to a real
  on-disk ``Session``, reloaded, is byte-identical, survives as EXACTLY ONE node, and
  remaps custom→user on the wire — proving "survives reload where applicable".

Reference: docs/EXTENSIONS-LIVE-PROCEDURES.md; EXTENSIONS-E5-WIRING.md §6 (E5.5 / S37);
§1 (the durable-hook invariant).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.messages import convert_to_llm
from tau_agent_core.sdk import _load_extensions, summarize_extensions
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage
from tau_coding_agent.session_store import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOC = _REPO_ROOT / "docs" / "EXTENSIONS-LIVE-PROCEDURES.md"


# ── doc parsing ───────────────────────────────────────────────────────────────


def _sections() -> dict[str, str]:
    """Split the doc into ``## `` sections, keyed by a demo slug found in the heading.

    Returns ``{slug: section_body}`` for the five procedures; a missing procedure
    simply never appears in the mapping (the tests assert the full set).
    """
    text = _DOC.read_text(encoding="utf-8")
    slugs = {
        "gatekeeper": "gatekeeper",
        "reminders": "reminders",
        "budget": "budget",
        "delegate": "delegate",
        "reload": "reload",
    }
    out: dict[str, str] = {}
    for block in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        heading = block.splitlines()[0].lower()
        for slug, needle in slugs.items():
            if needle in heading:
                out[slug] = block
    return out


def _commands(section: str) -> list[str]:
    """Every ``tau …`` command line inside the section's ```bash fenced blocks."""
    cmds: list[str] = []
    for fence in re.findall(r"```bash\n(.*?)```", section, flags=re.DOTALL):
        cmds += [ln.strip() for ln in fence.splitlines() if ln.strip().startswith("tau ")]
    return cmds


def _observation(section: str) -> str:
    """The ``**Expected durable …:**`` paragraph — the stated durable observation."""
    m = re.search(r"\*\*Expected durable[^\n]*", section)
    assert m, f"section has no '**Expected durable…**' observation:\n{section[:200]}"
    # Capture from the bold marker to the next blank line (the whole paragraph).
    tail = section[m.start() :]
    return tail.split("\n\n", 1)[0]


def _example_path(command: str) -> str | None:
    """The ``examples/NN_*.py`` path referenced by a ``tau -e …`` command, if any."""
    m = re.search(r"(examples/\S+\.py)", command)
    return m.group(1) if m else None


# ── (1) doc conformance bound to the REAL loader surface ──────────────────────

# Per demo: the example the command must name, the hooks/tools its observation
# depends on, and keywords the observation must actually contain (the durable shape).
_DEMO_EXPECTATIONS = {
    "gatekeeper": {
        "path": "examples/22_gatekeeper.py",
        "hooks": {"tool_call"},
        "tools": set(),
        "observation_keywords": ["is_error", "toolResult"],
    },
    "reminders": {
        "path": "examples/21_reminders.py",
        "hooks": {"tool_result", "before_agent_start"},
        "tools": set(),
        "observation_keywords": ["system-reminder", "tool_result"],
    },
    "budget": {
        "path": "examples/24_budget.py",
        "hooks": {"tool_result"},
        "tools": set(),
        "observation_keywords": ["abort", "warning"],
    },
    "delegate": {
        "path": "examples/20_delegate.py",
        "hooks": set(),
        "tools": {"delegate"},
        "observation_keywords": ["toolResult", "child"],
    },
}


async def test_doc_lists_a_procedure_per_demo_plus_reload() -> None:
    sections = _sections()
    assert set(sections) == {"gatekeeper", "reminders", "budget", "delegate", "reload"}, (
        "the doc must document all four demos plus the reload check"
    )

    for slug, expect in _DEMO_EXPECTATIONS.items():
        section = sections[slug]

        # (a) the command names the right example, verbatim.
        paths = [_example_path(c) for c in _commands(section)]
        assert expect["path"] in paths, f"{slug}: doc command must run {expect['path']}"
        example = _REPO_ROOT / expect["path"]
        assert example.is_file(), f"{slug}: {example} does not exist"

        # (b) RUNNABLE: the exact file loads through the public -e loader (the same
        # getattr(module, "register") path `tau -e <file>` uses) — not a wrapper.
        result = await _load_extensions(explicit_paths=[str(example)], discover=False)
        assert not result.errors, f"{slug}: {example} failed to load: {result.errors}"
        info = summarize_extensions(result)[0]

        # (c) the loaded extension registers exactly the surface the doc's observation
        # relies on — so the promised durable node has a real mechanism behind it.
        assert expect["hooks"] <= set(info.hooks), (
            f"{slug}: expected hooks {expect['hooks']}, got {info.hooks}"
        )
        assert expect["tools"] <= set(info.tools), (
            f"{slug}: expected tools {expect['tools']}, got {info.tools}"
        )

        # (d) the observation names the durable-node shape (not a fabricated claim).
        observation = _observation(section).lower()
        for kw in expect["observation_keywords"]:
            assert kw.lower() in observation, (
                f"{slug}: observation must mention {kw!r}; got:\n{observation}"
            )

    # The reload check: two commands (inject via a demo, then --resume) and an
    # observation naming the invariant's proof (byte-identical, exactly one node).
    reload_section = sections["reload"]
    reload_cmds = _commands(reload_section)
    assert any("examples/21_reminders.py" in c for c in reload_cmds), (
        "reload check must first run a demo that injects a durable node"
    )
    assert any("--resume" in c or "--session" in c for c in reload_cmds), (
        "reload check must reopen the persisted session"
    )
    reload_obs = _observation(reload_section).lower()
    assert "byte-identical" in reload_obs
    assert "once" in reload_obs  # exactly ONE node — no forked second history


# ── (2) the gatekeeper durable node, proven in transcript AND tree ────────────


def _tool_call_assistant(call_id: str, name: str, args: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=args)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=Usage(),
    )


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
    """Minimal async stream matching the stream_simple contract (cf. test_gatekeeper)."""

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


def _role_name(m: Any) -> tuple[Any, Any]:
    role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
    name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
    return role, name


def _fake_stream_write(path: str):
    """Emit one out-of-scope ``write`` call, then stop once its toolResult appears.

    A vetoed call still yields an (error) toolResult, so the loop terminates whether
    the write ran or was blocked.
    """

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if any(_role_name(m) == ("toolResult", "write") for m in messages):
            final = _text_assistant("done")
            return _Stream(
                [TextDeltaEvent(delta="done", partial=final), DoneEvent(final=final, usage=Usage())]
            )
        final = _tool_call_assistant("call_1", "write", {"path": path, "content": "x"})
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _toolresult_text(message: Any) -> str:
    content = message["content"] if isinstance(message, dict) else message.content
    block = content[0]
    return block["text"] if isinstance(block, dict) else block.text


async def test_gatekeeper_blocked_node_is_durable_in_transcript_and_tree(tmp_path, monkeypatch):
    """The doc's §1 observation, proven: a real gatekeeper veto produces an
    ``is_error`` ``toolResult`` node in BOTH the turn transcript AND the tree."""
    # A project whose declared scope allows only ``src/``; the write targets outside.
    (tmp_path / ".tau").mkdir()
    (tmp_path / ".tau" / "scope.txt").write_text("src/\n")
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    outside = str(tmp_path / "outside.txt")

    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    log = InMemorySessionLog()
    session = AgentSession(session_log=log, model=model, extensions=[])

    # Load the REAL example through the live -e seam (binds to this session's runner).
    result = await session.load_extensions(
        explicit_paths=[str(_REPO_ROOT / "examples" / "22_gatekeeper.py")], discover=False
    )
    assert not result.errors
    assert session._extension_runner.has_handlers("tool_call")

    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_fake_stream_write(outside)
    ):
        transcript = await session.prompt("write outside the scope")

    # Transcript guise: the returned turn carries the blocked is_error toolResult.
    t_blocked = [
        m
        for m in transcript
        if _role_name(m)[0] == "toolResult"
        and _role_name(m)[1] == "write"
        and bool(m["is_error"] if isinstance(m, dict) else getattr(m, "is_error", False))
    ]
    assert t_blocked, "no blocked toolResult in the transcript"
    assert "outside the allowed scope" in _toolresult_text(t_blocked[0])

    # Tree guise: the SAME durable node is persisted in the session log (the tree).
    persisted = [
        e["message"]
        for e in log.entries()
        if e.get("type") == "message"
        and isinstance(e.get("message"), dict)
        and e["message"].get("role") == "toolResult"
        and e["message"].get("tool_name") == "write"
    ]
    assert persisted, "blocked toolResult not persisted to the tree"
    assert persisted[0].get("is_error") is True
    assert "outside the allowed scope" in _toolresult_text(persisted[0])
    # And no file was written outside scope (the veto fenced the mutation).
    assert not (tmp_path / "outside.txt").exists()


# ── (3) the reload check: the durable node survives byte-identically ──────────


async def test_reload_check_node_survives_byte_identical(tmp_path):
    """The doc's §5 observation, proven: a durable ``customMessage`` node reloads
    byte-identically, as exactly one node, remapping custom→user on the wire."""
    preamble = "<system-reminder>Coding discipline for this session.</system-reminder>"
    session = Session.create(
        cwd=str(tmp_path),
        model="gpt-4o",
        backend="openai",
        system_prompt="You are helpful.",
        base_dir=tmp_path / "sessions",
    )
    session.append_message({"role": "user", "content": "hello there"})
    session.append_custom_message(
        {"role": "custom", "content": [{"type": "text", "text": preamble}]},
        "reminder-preamble",
    )
    path = session.path
    assert path is not None

    # A load must NOT rewrite the file — the on-disk bytes are the single artifact.
    raw = path.read_bytes()
    first = Session.load(path)
    assert path.read_bytes() == raw

    # Two independent reloads yield identical entries AND identical model context.
    second = Session.load(path)
    assert first.entries() == second.entries()
    ctx_first = ConversationTree(first.entries(), first.cursor).context_for()
    ctx_second = ConversationTree(second.entries(), second.cursor).context_for()
    assert ctx_first == ctx_second

    # Exactly ONE injected node survives — no forked "second history".
    customs = [e for e in first.entries() if e.get("type") == "customMessage"]
    assert len(customs) == 1

    # On the wire the durable custom node remaps custom→user (the LLM never sees
    # "custom"), while the preamble text survives as a user message.
    wire = convert_to_llm(ctx_first)
    assert "custom" not in [m.get("role") for m in wire]
    user_texts = []
    for m in wire:
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                user_texts.append(content)
            else:
                user_texts += [b.get("text", "") for b in content or [] if isinstance(b, dict)]
    assert "hello there" in user_texts
    assert preamble in user_texts
