"""E3-ctx / step S19 — the ``ExtensionContext`` session-control op surface.

Verifies each ``ctx`` op (``compact`` / ``entries`` / ``summarize_branch`` /
``navigate`` / ``fork``) mutates the ONE authoritative session log the bound
``AgentSession`` persists through, and re-renders context (``context_for``).

The zero-LLM ops (``entries``/``navigate``/``fork`` in-place) run over a session
built through the REAL agent loop via the ``fake_llm`` fixture; the LLM-backed ops
(``compact``/``summarize_branch``) patch the summarizer ``complete_simple`` so the
append + re-render is exercised without a network call.
"""

from __future__ import annotations

import pytest

from tau_ai.types import AssistantMessage, Model, TextContent

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.session_log import InMemorySessionLog


def _model() -> Model:
    return Model(
        id="test-model",
        name="test-model",
        api="openai-completions",
        provider="openai",
        base_url="http://localhost",
        context_window=128000,
        max_tokens=4096,
    )


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _summary_response(text: str = "SUMMARY"):
    async def _impl(model, context, options=None):
        return AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model="test-model",
            stop_reason="stop",  # type: ignore[arg-type]
            timestamp=0,
        )

    return _impl


def _ctx(session: AgentSession):
    return session._extension_api.context


# ── zero-LLM ops through the real loop ───────────────────────────────────────


@pytest.mark.usefixtures("fake_llm")
class TestZeroLlmOps:
    async def _seeded(self) -> AgentSession:
        """A session with two real turns appended via the agent loop."""
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model(),
            compaction_settings=CompactionSettings(enabled=False),
        )
        await session.prompt("first")
        await session.prompt("second")
        return session

    async def test_entries_passes_through_the_bound_log(self):
        session = await self._seeded()
        ctx = _ctx(session)
        entries = ctx.entries()
        # Same content as the bound log's own entries (thin pass-through) …
        assert entries == session.session_log.entries()
        # … and a COPY: mutating it must not touch the log.
        entries.append({"type": "message"})
        assert len(ctx.entries()) == len(session.session_log.entries())
        # Four message entries: user+assistant per prompt.
        assert [e["type"] for e in ctx.entries()] == ["message"] * 4

    async def test_navigate_moves_cursor_and_reshrinks_context(self):
        session = await self._seeded()
        ctx = _ctx(session)
        entries = session.session_log.entries()
        assert len(session.messages) == 4
        target = entries[1]["id"]  # the first assistant reply

        rendered = await ctx.navigate(target)

        # A navigate entry was APPENDED to the one log …
        log_entries = session.session_log.entries()
        assert len(log_entries) == 5
        assert log_entries[-1]["type"] == "navigate"
        assert log_entries[-1]["targetId"] == target
        # … the cursor moved to the target …
        assert session.session_log.cursor == target
        # … and context re-rendered to the truncated path (root → target).
        assert len(rendered) == 2
        assert rendered == session.messages

    async def test_navigate_to_current_cursor_is_a_noop(self):
        session = await self._seeded()
        ctx = _ctx(session)
        before = session.session_log.entries()
        rendered = await ctx.navigate(session.session_log.cursor)
        # No entry appended; context unchanged.
        assert session.session_log.entries() == before
        assert rendered == session.messages

    async def test_fork_in_place_navigates_and_appends(self):
        session = await self._seeded()
        ctx = _ctx(session)
        target = session.session_log.entries()[1]["id"]

        rendered = await ctx.fork(target, mode="in_place")

        log_entries = session.session_log.entries()
        assert log_entries[-1]["type"] == "navigate"
        assert log_entries[-1]["targetId"] == target
        assert session.session_log.cursor == target
        assert len(rendered) == 2
        assert rendered == session.messages

    async def test_fork_export_on_in_memory_log_raises(self):
        # Fail-Early: an in-memory SDK log cannot be exported to a file.
        session = await self._seeded()
        ctx = _ctx(session)
        with pytest.raises(RuntimeError, match="not file-backed"):
            await ctx.fork(mode="export")

    async def test_fork_unknown_mode_raises(self):
        session = await self._seeded()
        ctx = _ctx(session)
        with pytest.raises(ValueError, match="unknown mode"):
            await ctx.fork(mode="sideways")  # type: ignore[arg-type]


# ── unbound context ──────────────────────────────────────────────────────────


class TestUnbound:
    async def test_ops_raise_without_a_bound_session(self):
        from tau_agent_core.extension_types import ExtensionContext

        ctx = ExtensionContext()
        assert ctx._session is None
        with pytest.raises(RuntimeError, match="no session bound"):
            ctx.entries()
        with pytest.raises(RuntimeError, match="no session bound"):
            await ctx.navigate("x")
        with pytest.raises(RuntimeError, match="no session bound"):
            await ctx.compact()


# ── LLM-backed ops (patched summarizer) ──────────────────────────────────────


class TestCompact:
    async def test_compact_appends_a_compaction_and_rerenders(self, monkeypatch):
        monkeypatch.setattr(
            "tau_agent_core.compaction.complete_simple", _summary_response("COMPACTED")
        )
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model(),
            compaction_settings=CompactionSettings(enabled=True, keep_recent_tokens=1),
        )
        log = session.session_log
        # Three turns so the cut keeps the most recent and summarizes the prefix.
        for i in range(3):
            log.append_message(_msg("user", f"u{i}"))
            log.append_message(_msg("assistant", f"a{i}"))
        before = len(log.entries())

        result = await _ctx(session).compact()

        assert result is not None
        # The summarizer stub returns "COMPACTED"; a split-turn compaction may
        # concatenate the history + turn-prefix summaries, so assert containment.
        assert "COMPACTED" in result.summary
        entries = log.entries()
        assert len(entries) == before + 1
        assert entries[-1]["type"] == "compaction"
        # Re-rendered context carries the compaction summary and is shorter than
        # the six raw messages.
        rendered = session.messages
        assert len(rendered) < 6
        assert any(
            "[[Compaction summary:" in _text(m) and "COMPACTED" in _text(m) for m in rendered
        )


class TestSummarizeBranch:
    async def test_summarize_branch_appends_branch_summary_and_rerenders(self, monkeypatch):
        monkeypatch.setattr("tau_ai.client.complete_simple", _summary_response("BRANCH"))
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model(),
            compaction_settings=CompactionSettings(enabled=False),
        )
        log = session.session_log
        log.append_message(_msg("user", "u0"))
        first_asst = log.append_message(_msg("assistant", "a0"))
        log.append_message(_msg("user", "u1"))
        log.append_message(_msg("assistant", "a1"))
        assert len(session.messages) == 4

        rendered = await _ctx(session).summarize_branch(first_asst)

        entries = log.entries()
        assert entries[-1]["type"] == "branch_summary"
        assert entries[-1]["fromId"] == first_asst
        # Cursor now sits on the branch_summary; the u1/a1 siblings dropped out.
        assert log.cursor == entries[-1]["id"]
        assert len(rendered) == 3
        assert any("[[Branch summary: BRANCH]]" in _text(m) for m in rendered)
        assert rendered == session.messages

    async def test_navigate_summarize_delegates_to_summarize_branch(self, monkeypatch):
        monkeypatch.setattr("tau_ai.client.complete_simple", _summary_response("VIANAV"))
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model(),
            compaction_settings=CompactionSettings(enabled=False),
        )
        log = session.session_log
        log.append_message(_msg("user", "u0"))
        first_asst = log.append_message(_msg("assistant", "a0"))
        log.append_message(_msg("user", "u1"))
        log.append_message(_msg("assistant", "a1"))

        rendered = await _ctx(session).navigate(first_asst, summarize=True)

        assert log.entries()[-1]["type"] == "branch_summary"
        assert any("[[Branch summary: VIANAV]]" in _text(m) for m in rendered)


def _text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
