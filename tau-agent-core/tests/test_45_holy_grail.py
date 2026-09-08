"""Tests for ``examples/45_holy_grail.py`` — the four request states, one per gesture.

Reference: docs/EXTENSION-LOCKS.md §3. ``test_extension_locks.py`` proves the
mechanism; this proves the example that demonstrates it, on the axis the example
adds: three of the four are raised by a TOOL the model calls, which is the case
§4.1 constrains — a request appended from inside ``execute`` is behind the leaf
before the turn ends, so all three defer to ``user_turn_end``.

Everything below runs against a real ``AgentSession`` with the file loaded
through the real ``-e`` path. The tools are invoked with the extension execute
signature and the turn edge is fired the way :meth:`AgentSession.prompt` fires
it, so the tool → pending → turn-edge → request path is the real one. Only the
grading turn's network boundary is faked.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_locks import RESPONSE_ENTRY_TYPE
from tau_agent_core.session_log import InMemorySessionLog, resolve_cursor
from tau_agent_core.submission import Submission

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MOD_PATH = _REPO_ROOT / "examples" / "45_holy_grail.py"
_spec = importlib.util.spec_from_file_location("holy_grail_example", _MOD_PATH)
assert _spec is not None and _spec.loader is not None
demo = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = demo
_spec.loader.exec_module(demo)


def _model() -> Model:
    return Model(
        id="grail",
        name="grail",
        api="openai-completions",
        provider="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        context_window=8192,
        max_tokens=1024,
    )


async def _session() -> AgentSession:
    """A real session with the example loaded through the real ``-e`` path."""
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), extensions=[])
    result = await session.load_extensions([str(_MOD_PATH)], discover=False)
    assert result.extensions and not result.errors, result.errors
    await session.emit_session_start()
    return session


def _tools(session: AgentSession) -> dict[str, Any]:
    return {tool.name: tool for tool in session._build_turn_tools()}


async def _call(session: AgentSession, name: str) -> dict[str, Any]:
    result = await _tools(session)[name].execute(tool_call_id="c1", args={}, signal=None)
    return dict(result)


async def _turn_edge(session: AgentSession) -> None:
    """What the tail of ``prompt()`` does: fire ``user_turn_end`` exactly once."""
    await session._run_user_turn_end([])


def _submission(text: str, **kwargs: Any) -> Submission:
    return Submission(
        text=text, source="interactive", submitter="human", submission_id="s", **kwargs
    )


# ── the four rows ────────────────────────────────────────────────────────────


async def test_black_knight_locks_with_no_ask() -> None:
    """Row 3 of §3's table: a lock nobody can answer, only navigate off."""
    session = await _session()
    result = await _call(session, "none_shall_pass")

    assert result["terminate"] is True
    assert session.pending_request is None, "a tool must not raise the request itself"

    await _turn_edge(session)
    request = session.pending_request
    assert request is not None
    assert request.lock is True
    assert request.ask is None
    assert request.sentence == "I move for no man."
    assert request.label == "Extension 45_holy_grail requires intervention"


async def test_bridgekeeper_locks_and_asks_three_questions() -> None:
    """Row 4: lock and ask. The third question is drawn once and carried into the ask."""
    session = await _session()
    result = await _call(session, "questions_three")
    assert result["terminate"] is True

    await _turn_edge(session)
    request = session.pending_request
    assert request is not None
    assert request.lock is True
    assert request.ask is not None
    labels = [field["label"] for field in request.ask["fields"]]
    assert labels[:2] == ["What is your name?", "What is your quest?"]
    assert labels[2] in demo.THIRD_QUESTIONS
    assert [a["label"] for a in request.ask["actions"]] == ["Answer"]


async def test_idiom_asks_without_locking() -> None:
    """Row 2: an ask a prompt walks straight past."""
    session = await _session()
    await session.run_extension_command("idiom", "")

    request = session.pending_request
    assert request is not None
    assert request.lock is False
    assert request.ask["fields"][0]["options"] == ["Lancelot", "Herbert", "Concorde"]
    assert request.label == "Extension 45_holy_grail requests a response"


async def test_the_ni_note_is_not_a_request_at_all() -> None:
    """Row 1: neither key set, so it is an ordinary display message, not a request."""
    session = await _session()
    session._session_log.append_message({"role": "user", "content": "I like it. Do it, mind it!"})

    await _call(session, "knights_of_ni")
    await _turn_edge(session)

    assert session.pending_request is None
    notes = [e for e in session._session_log.entries() if e.get("customType") == "knights_of_ni"]
    assert len(notes) == 1
    text = notes[0]["message"]["content"][0]["text"]
    assert text.startswith(("Ni!", "Ecky-ecky-ecky-ecky-pikang ZOOM-ping"))
    assert "3 times" in text, text


# ── what the locks do to a submission ────────────────────────────────────────


async def test_a_lock_refuses_the_next_prompt_and_names_its_release() -> None:
    session = await _session()
    await _call(session, "none_shall_pass")
    await _turn_edge(session)

    refused = await session.submit(_submission("carry on"))
    assert refused.accepted is False
    assert refused.rejection_reason is not None
    assert "Run /tis-but-a-scratch to clear it" in refused.rejection_reason
    assert refused.lock is not None
    assert refused.lock.sentence == "I move for no man."


async def test_the_release_command_moves_the_cursor() -> None:
    """Being exempt from the lock lets it run; only the cursor moving releases (§6)."""
    session = await _session()
    await _call(session, "none_shall_pass")
    await _turn_edge(session)

    admitted = await session.submit(_submission("/tis-but-a-scratch", expand_commands=True))
    assert admitted.accepted is True
    assert session.pending_request is None


async def test_the_release_command_says_so_when_there_is_nothing_to_release() -> None:
    session = await _session()
    result = await session.run_extension_command("tis-but-a-scratch", "")
    assert result.handled is True
    assert "nothing at the cursor" in str(result.output)


async def test_an_unlocked_ask_leaves_the_session_open() -> None:
    """The /idiom request is at the cursor and the submission is still admitted."""
    session = await _session()
    await session.run_extension_command("idiom", "")
    assert session.pending_request is not None

    with patch.object(AgentSession, "_run_one_turn", return_value=[]) as run:
        accepted = await session.submit(_submission("carry on"))
    assert accepted.accepted is True
    assert run.called


# ── answering ────────────────────────────────────────────────────────────────


async def test_adopting_an_idiom_puts_a_model_visible_message_in_context() -> None:
    session = await _session()
    await session.run_extension_command("idiom", "")
    request = session.pending_request
    assert request is not None

    result = await session.answer_request(request.entry_id, "Adopt", {"character": "Herbert"})
    assert result.output == "Now doing Herbert."

    injected = [e for e in session._session_log.entries() if e.get("customType") == "idiom"]
    assert len(injected) == 1
    message = injected[0]["message"]
    assert message["visibleToModel"] is True
    assert "answer as Herbert" in message["content"][0]["text"]
    assert session.pending_request is None, "answering appends, and appending releases"


async def test_dismissing_the_idiom_ask_changes_nothing() -> None:
    """Navigating off an unanswered ask leaves no idiom message behind."""
    session = await _session()
    await session.run_extension_command("idiom", "")
    request = session.pending_request
    assert request is not None

    entries = session._session_log.entries()
    parent = next(e["parentId"] for e in entries if str(e["id"]) == request.entry_id)
    session._session_log.append_navigate(str(parent))

    assert session.pending_request is None
    assert not [e for e in session._session_log.entries() if e.get("customType") == "idiom"]


async def test_answering_the_bridgekeeper_submits_the_answers_for_grading() -> None:
    """The action's turn carries all three answers and the question that was drawn."""
    session = await _session()
    await _call(session, "questions_three")
    await _turn_edge(session)
    request = session.pending_request
    assert request is not None
    third = request.ask["fields"][2]["label"]

    submitted: list[str] = []

    async def _capture(text: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        submitted.append(text)
        return []

    with patch.object(AgentSession, "_run_one_turn", side_effect=_capture):
        result = await session.answer_request(
            request.entry_id,
            "Answer",
            {"name": "Arthur", "quest": "To seek the Grail", "third": "Blue"},
        )

    assert result.output == "Auuuuuuuugh."
    assert len(submitted) == 1
    assert "Arthur" in submitted[0]
    assert "To seek the Grail" in submitted[0]
    assert f"- {third} Blue" in submitted[0]
    assert "Gorge of Eternal Peril" in submitted[0]


async def test_the_answers_are_readable_off_the_tree_after_the_fact() -> None:
    """``answers()`` reads the response entry, which is what survives a reload."""
    session = await _session()
    await session.run_extension_command("idiom", "")
    request = session.pending_request
    assert request is not None

    await session.answer_request(request.entry_id, "Adopt", {"character": "Lancelot"})
    entries = session._session_log.entries()
    responses = [e for e in entries if e.get("customType") == RESPONSE_ENTRY_TYPE]
    assert responses[-1]["data"]["values"] == {"character": "Lancelot"}


# ── the reload ───────────────────────────────────────────────────────────────


async def test_a_lock_survives_a_reload_with_the_extension_gone() -> None:
    """The lock is a tree node, so it outlives the extension that raised it."""
    session = await _session()
    await _call(session, "none_shall_pass")
    await _turn_edge(session)

    reloaded_log = InMemorySessionLog()
    reloaded_log._entries = session._session_log.entries()
    reloaded_log._ids = {str(e["id"]) for e in reloaded_log._entries}
    reloaded_log._leaf_id = resolve_cursor(reloaded_log._entries)
    reloaded = AgentSession(session_log=reloaded_log, model=_model(), extensions=[])

    request = reloaded.pending_request
    assert request is not None
    assert request.sentence == "I move for no man."
    assert request.extension_name == "45_holy_grail"


# ── the pure parts ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I like it. Do it, and mind it!", 3),
        ("it starts a sentence and ends one with it", 0),
        ("is it? it's it, and that is it.", 4),
        ("commit it to the pit", 1),
        ("nothing here", 0),
    ],
)
def test_the_it_counter_matches_its_pattern(text: str, expected: int) -> None:
    """A leading space and a following delimiter — so ``commit`` and a bare tail miss."""
    entries = [{"type": "message", "message": {"role": "user", "content": text}}]
    assert demo.count_it(entries) == expected


def test_the_it_counter_ignores_everything_that_is_not_a_turn() -> None:
    """Only user and assistant messages count; a request entry is not a turn."""
    entries: list[dict[str, Any]] = [
        {"type": "message", "message": {"role": "system", "content": "it it it, it!"}},
        {"type": "customEntry", "customType": "extension_request", "data": {"sentence": "it it!"}},
        {"type": "message", "message": {"role": "assistant", "content": "it is, isn't it?"}},
    ]
    assert demo.count_it(entries) == 1


def test_message_text_reads_a_string_and_a_block_list_the_same() -> None:
    blocks = {"content": [{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text"}]}
    assert demo.message_text({"content": "a"}) == "a"
    assert demo.message_text(blocks).split() == ["a"]


def test_every_idiom_has_a_direction() -> None:
    """The select's options and the styles it applies are the same three names."""
    assert set(demo.IDIOMS) == {"Lancelot", "Herbert", "Concorde"}
    assert all(direction for direction in demo.IDIOMS.values())
