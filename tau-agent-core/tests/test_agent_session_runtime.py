"""AgentSessionRuntime (phase 3, H1-H4) — docs/REMOTE-CONTROL.md §4[6].

Covers:

- H3: the reset set, item by item, and that survivors are actually left alone.
- The "cleared, not re-derived" resolution for the last-compaction anchor
  (§10 "resolved").
- H2: the ``session_before_switch`` veto, and that a veto touches nothing.
- H4: atomicity — a turn's events all reach the subscriber strictly before a
  concurrent ``new_session`` call's result is observed, and no event from the
  old session arrives after.
- ``fork``/``switch_session``'s catalog wiring, and ``switch_session``'s
  Fail-Early "bad ref touches nothing" behaviour.
- ``dispose`` / ``set_rebind_session``.

Uses a minimal, test-only ``SessionCatalog``/``ConversationSession`` pair
(mirrors ``test_session_catalog.py``'s ``InMemorySessionCatalog`` — not
imported from there, since cross-test-file imports have no precedent in this
suite and this file needs only a handful of the ABC's members exercised).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.agent_session_runtime import AgentSessionRuntime
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.cursor import Cursor
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog, SessionInfo
from tau_agent_core.session_log import InMemorySessionLog, default_leaf
from tau_agent_core.submission import Submission

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000

_PROV = {
    "summarizer_model_id": "test-summarizer",
    "summary_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    "covered_entries": 1,
    "covered_tokens": 50,
    "config_id": None,
}


# ── a minimal, test-only SessionCatalog/ConversationSession pair ───────────


class _FakeConversationSession:
    """A RAM-only ``ConversationSession``: an ``InMemorySessionLog`` with the
    config reads layered on top as plain state."""

    def __init__(self, cwd: str, model: str, backend: str, name: str | None = None) -> None:
        self._log = InMemorySessionLog()
        self._cwd = cwd
        self._model = model
        self._backend = backend
        self._name = name
        now = datetime.now(timezone.utc)
        self._created = now
        self._modified = now

    @property
    def id(self) -> str:
        return self._log.id

    def entries(self) -> list[dict[str, Any]]:
        return self._log.entries()

    async def append_at(self, parent_id, entry_type, payload) -> str:
        return self._append_at_now(parent_id, entry_type, payload)

    async def finalize(self, entry_id, payload) -> None:
        await self._log.finalize(entry_id, payload)

    def _append_at_now(self, parent_id, entry_type, payload) -> str:
        """Synchronous write for ``create``/``fork``, which are not coroutines."""
        return self._log.append_at_now(parent_id, entry_type, payload)

    @property
    def header(self) -> dict[str, Any]:
        return {"type": "session", "id": self.id, "cwd": self._cwd}

    def _tree(self) -> ConversationTree:
        entries = self.entries()
        return ConversationTree(entries, default_leaf(entries))

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [e["message"] for e in self._tree().path() if e.get("type") == "message"]

    @property
    def context(self) -> list[dict[str, Any]]:
        return self._tree().context_for()

    @property
    def config(self) -> dict[str, Any]:
        return {"model": self._model, "backend": self._backend}

    @property
    def model(self) -> str:
        return self._model

    @property
    def backend(self) -> str:
        return self._backend

    def display_title(self) -> str:
        return self._name or f"Session ({self._model})"


class _FakeCatalog(SessionCatalog):
    """The five abstract primitives, RAM-only — enough for
    ``AgentSessionRuntime``'s three verbs, nothing more."""

    def __init__(self) -> None:
        self._sessions: dict[str, _FakeConversationSession] = {}

    def create(
        self, cwd, model, backend, *, system_prompt: str | None = None, name: str | None = None
    ) -> ConversationSession:
        session = _FakeConversationSession(cwd, model, backend, name)
        if system_prompt:
            # Sync core, like every real catalog: `create` is not a coroutine.
            session._append_at_now(
                None, "message", {"message": {"role": "system", "content": system_prompt}}
            )
        self._sessions[session.id] = session
        return session

    def create_ephemeral(
        self, cwd, model, backend, *, system_prompt: str | None = None, name: str | None = None
    ) -> ConversationSession:
        # Mirrors FileSessionCatalog: same construction, never registered.
        session = _FakeConversationSession(cwd, model, backend, name)
        if system_prompt:
            session._append_at_now(
                None, "message", {"message": {"role": "system", "content": system_prompt}}
            )
        return session

    def load(self, ref: str) -> ConversationSession:
        try:
            return self._sessions[ref]
        except KeyError:
            raise FileNotFoundError(f"no in-memory session {ref!r}") from None

    def fork(
        self, source: ConversationSession, cwd: str, *, at: str | None = None
    ) -> ConversationSession:
        assert isinstance(source, _FakeConversationSession)
        entries = source.entries()
        if at is not None:
            tree = ConversationTree(entries, at)
            if not tree.contains(at):
                raise ValueError(f"fork point {at!r} not found")
            entries = tree.path()
        forked = _FakeConversationSession(cwd, source.model, source.backend)
        new_ids: dict[str | None, str | None] = {None: None}
        for entry in entries:
            payload = {
                k: v for k, v in entry.items() if k not in ("type", "id", "parentId", "timestamp")
            }
            new_ids[entry["id"]] = forked._append_at_now(
                new_ids[entry.get("parentId")], entry["type"], payload
            )
        self._sessions[forked.id] = forked
        return forked

    def list(self, cwd: str | None = None) -> list[SessionInfo]:
        return [
            SessionInfo(
                ref=s.id,
                id=s.id,
                cwd=s._cwd,
                name=s._name,
                created=s._created,
                modified=s._modified,
                message_count=len(s.messages),
                first_message="",
                last_message="",
                parent=None,
            )
            for s in self._sessions.values()
            if cwd is None or s._cwd == cwd
        ]


# ── a real AgentSession + a gated fake provider (mirrors test_submit_admission.py) ──


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="stop",
        timestamp=_TS,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    def __init__(self, text: str) -> None:
        self._text = text

    def __aiter__(self):
        async def _gen():
            yield TextDeltaEvent(delta=self._text, partial=_assistant(self._text))
            final = _assistant(self._text)
            yield DoneEvent(final=final, usage=final.usage)

        return _gen()

    async def result(self):
        return _assistant(self._text)

    def abort(self) -> None:
        pass


def _sub(text: str, submission_id: str, **overrides: Any) -> Submission:
    fields: dict[str, Any] = {
        "text": text,
        "source": "interactive",
        "submitter": "human",
        "submission_id": submission_id,
    }
    fields.update(overrides)
    return Submission(**fields)


def _runtime(session: AgentSession, catalog: SessionCatalog) -> AgentSessionRuntime:
    return AgentSessionRuntime(session, catalog, "/work", "m", "openai", "file")


@pytest.fixture
def catalog() -> _FakeCatalog:
    return _FakeCatalog()


@pytest.fixture
def session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])


@pytest.fixture
def runtime(session: AgentSession, catalog: _FakeCatalog) -> AgentSessionRuntime:
    return _runtime(session, catalog)


# ── H3: the reset set ────────────────────────────────────────────────────


async def test_new_session_resets_the_documented_set(session: AgentSession, runtime):
    """Every H3 item, driven dirty, then proven clean after new_session()."""
    old_log = session.session_log
    await session.cursor.append_message({"role": "user", "content": "hi"})

    session.cursor.last_usage = {"input_tokens": 5}
    session.record_side_usage({"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
    session.cursor.follow_up_queue.append("queued-follow-up")
    session.cursor.next_turn_queue.append("queued-next-turn")
    from tau_llm.types import UserMessage

    session.cursor.steer_queue.append(
        UserMessage.model_validate(
            {"role": "user", "content": [{"type": "text", "text": "x"}], "timestamp": _TS}
        )
    )
    session.cursor.deferred_ops.append({"kind": "compact", "custom_instructions": None})
    session.cursor.is_streaming = True
    session.cursor.pre_turn_leaf = session.cursor.leaf

    result = await runtime.new_session(persist=False)

    from tau_agent_core.usage import zero_usage

    assert result["cancelled"] is False
    assert session.get_usage() is None
    assert session.side_usage == zero_usage()
    assert session.cursor.follow_up_queue == []
    assert session.cursor.next_turn_queue == []
    assert session.cursor.steer_queue == []
    assert session.cursor.deferred_ops == []
    assert session.is_streaming is False
    assert session.cursor.pre_turn_leaf is None
    # log + cursor: a cursor on a brand new, empty log — not the dirty one.
    assert session.session_log is not old_log
    assert session.cursor.leaf is None
    assert session.session_log.entries() == []


async def test_survivors_are_left_alone(session: AgentSession, catalog, runtime):
    """H3's other half: what is NOT on the list must not move."""
    model_before = session.get_model()
    tools_before = session._tools
    extensions_before = session._extensions
    events_before = session._events
    runner_before = session.extension_runner

    result = await runtime.new_session(persist=False)

    assert result["cancelled"] is False
    assert session.get_model() == model_before
    assert session._tools is tools_before
    assert session._extensions is extensions_before
    assert session._events is events_before
    assert session.extension_runner is runner_before
    assert session._turn_token_counter == 0


async def test_last_compaction_anchor_is_cleared_not_rederived(session: AgentSession, runtime):
    """§10 'resolved': a fresh log has no compaction to anchor to — this
    proves it is genuinely GONE, not carried over or recomputed."""
    first = await session.cursor.append_message({"role": "user", "content": "turn one"})
    await session.cursor.append_compaction(
        summary="a summary", first_kept_id=first, tokens_before=100, **_PROV
    )
    # Sanity: the OLD cursor really does sit below a splice anchor before the reset.
    old_active = session.cursor.context()
    assert any(m.get("role") == "user" and "summary" in str(m.get("content")) for m in old_active)

    await runtime.new_session(persist=False)

    assert session.session_log.entries() == []
    assert session.cursor.context() == []
    assert session.get_last_compaction() is None


# ── H2: the veto hook ────────────────────────────────────────────────────


async def test_session_before_switch_veto_leaves_everything_untouched(
    session: AgentSession, runtime
):
    seen: list[dict[str, Any]] = []

    def _veto(event: dict[str, Any], ctx) -> dict[str, Any]:
        seen.append(event)
        return {"cancel": True}

    bucket = session._extension_runner.register_extension("test-ext")
    bucket.on("session_before_switch", _veto)

    old_log = session.session_log
    result = await runtime.new_session(persist=False)

    assert result == {"cancelled": True}
    assert session.session_log is old_log
    assert seen == [{"type": "session_before_switch", "reason": "new", "target": None}]


async def test_switch_session_veto_carries_the_target(session: AgentSession, catalog, runtime):
    other = catalog.create("/work", "m", "openai")
    seen: list[dict[str, Any]] = []

    def _veto(event, ctx):
        seen.append(event)
        return {"cancel": True}

    bucket = session._extension_runner.register_extension("test-ext")
    bucket.on("session_before_switch", _veto)

    result = await runtime.switch_session(other.id)

    assert result == {"cancelled": True}
    assert seen == [{"type": "session_before_switch", "reason": "resume", "target": other.id}]


async def test_non_cancelling_handler_lets_the_swap_proceed(session: AgentSession, runtime):
    def _observe(event, ctx):
        return {"cancel": False}

    bucket = session._extension_runner.register_extension("test-ext")
    bucket.on("session_before_switch", _observe)

    old_log = session.session_log
    result = await runtime.new_session(persist=False)

    assert result["cancelled"] is False
    assert session.session_log is not old_log


# ── H4: atomicity ────────────────────────────────────────────────────────


async def test_new_session_is_atomic_with_respect_to_the_event_stream(
    session: AgentSession, runtime
):
    """A turn is left mid-flight; new_session() runs concurrently. Every
    event that turn was going to emit must be recorded BEFORE new_session()'s
    own result is observed, and nothing may arrive after.
    """
    recorded: list[str] = []
    session.subscribe(lambda event: recorded.append(event.type))

    gate = asyncio.Event()

    async def _gated_stream_simple(model, context, options=None):
        await gate.wait()
        return _Stream("the reply")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated_stream_simple):
        task_a = asyncio.create_task(
            session.submit(_sub("turn A", "a-1", multitask_strategy="enqueue"))
        )
        await asyncio.sleep(0)
        assert session.is_streaming is True

        swap_task = asyncio.create_task(runtime.new_session(persist=False))
        await asyncio.sleep(0)
        assert not swap_task.done()

        gate.set()
        await asyncio.wait_for(task_a, timeout=5.0)
        result = await asyncio.wait_for(swap_task, timeout=5.0)

    assert result["cancelled"] is False
    assert "agent_end" in recorded
    agent_end_index = recorded.index("agent_end")
    assert recorded[agent_end_index:].count("agent_end") == 1
    assert session.session_log.entries() == []


async def test_new_session_waits_even_when_nothing_is_streaming(session: AgentSession, runtime):
    """The uncontended path: no turn in flight, the lock acquire is immediate."""
    assert session.is_streaming is False
    result = await runtime.new_session(persist=False)
    assert result["cancelled"] is False


async def test_new_session_refuses_rather_than_hanging_when_the_turn_never_stops(
    session: AgentSession, catalog
):
    """Finding 1 (phase-3 review): `abort()` only ever REQUESTS a stop — a
    provider that never notices it (the reviewer's repro: "accepts the
    connection and never sends an SSE line", which never even reaches
    `tau_llm` openai.py's `abort_signal.is_aborted()` check, since that check
    runs INSIDE the per-received-line loop) leaves the turn lock held
    indefinitely. `_apply_swap` must return a structured refusal within its
    bounded wait rather than hang forever — the RPC reader is strictly
    serial, so an unbounded wait here wedges every later request behind it,
    including `abort` itself (see `test_rpc_conformance.py`'s wire-level
    reproduction of the same finding).

    Uses a tiny `swap_timeout_s` override — a real 5s `DEFAULT_SWAP_TIMEOUT_S`
    would be a legitimate but slow way to pin the same behaviour; the
    override exists (per its own docstring) exactly so this test does not
    have to sleep it out.
    """
    runtime = AgentSessionRuntime(
        session, catalog, "/work", "m", "openai", "file", swap_timeout_s=0.05
    )
    old_log = session.session_log

    gate = asyncio.Event()

    async def _wedged_stream_simple(model, context, options=None):
        await gate.wait()  # never set within this test -- the silent provider
        return _Stream("never gets here")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_wedged_stream_simple):
        task_a = asyncio.create_task(
            session.submit(_sub("turn A", "a-1", multitask_strategy="enqueue"))
        )
        await asyncio.sleep(0)
        assert session.is_streaming is True

        result = await asyncio.wait_for(runtime.new_session(persist=False), timeout=5.0)

        assert result["cancelled"] is False
        assert result["blocked"] is True
        assert result["reason"]  # a non-empty, actionable message
        assert "session" not in result  # nothing was touched
        assert session.session_log is old_log  # no swap happened

        # Clean up the still-running turn so it doesn't leak past this test.
        gate.set()
        await asyncio.wait_for(task_a, timeout=5.0)


# ── fork / switch_session ────────────────────────────────────────────────


async def test_fork_carries_history_and_leaves_the_source_untouched(
    session: AgentSession, catalog, runtime
):
    """The fork is taken at the session cursor's leaf: a branch the cursor moved
    off stays behind in the source."""
    session.session_log = catalog.create_ephemeral("/work", "m", "openai")
    source_log = session.session_log
    hello = await session.cursor.append_message({"role": "user", "content": "hello"})
    await session.cursor.append_message({"role": "assistant", "content": "moved off"})
    session.cursor.move(hello)
    source_before = list(source_log.entries())

    result = await runtime.fork()

    assert result["cancelled"] is False
    assert session.session_log is not source_log
    forked_messages = [
        e["message"]["content"] for e in session.session_log.entries() if e.get("type") == "message"
    ]
    assert forked_messages == ["hello"]
    # The source object itself, still reachable via the catalog, is unchanged.
    assert list(source_log.entries()) == source_before


async def test_fork_before_any_catalog_session_raises(catalog: _FakeCatalog):
    """A bare InMemorySessionLog (never bound through the catalog) cannot be
    forked — Fail-Early, not an AttributeError three calls deep."""
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
    runtime = _runtime(session, catalog)
    with pytest.raises(RuntimeError, match="ConversationSession"):
        await runtime.fork()


async def test_switch_session_loads_a_different_session(
    session: AgentSession, catalog: _FakeCatalog, runtime
):
    other = catalog.create("/work", "m", "openai")
    over_there = await Cursor.newest(other).append_message(
        {"role": "user", "content": "over there"}
    )

    result = await runtime.switch_session(other.id)

    assert result["cancelled"] is False
    assert session.session_log is other
    assert result["session_id"] == other.id
    assert result["cursor"] == over_there == session.cursor.leaf


async def test_switch_session_bad_ref_raises_and_touches_nothing(session: AgentSession, runtime):
    old_log = session.session_log
    with pytest.raises(LookupError):
        await runtime.switch_session("no-such-session")
    assert session.session_log is old_log
    assert session.is_streaming is False


# ── dispose / set_rebind_session ─────────────────────────────────────────


async def test_dispose_fires_session_shutdown(session: AgentSession, runtime):
    seen: list[str] = []

    async def _on_shutdown(event, ctx):
        seen.append(event["reason"])

    bucket = session._extension_runner.register_extension("test-ext")
    bucket.on("session_shutdown", _on_shutdown)

    await runtime.dispose()

    assert seen == ["quit"]


async def test_rebind_runs_after_the_swap_with_the_lock_released(session: AgentSession, runtime):
    calls: list[AgentSession] = []

    async def _rebind(rebound_session: AgentSession) -> None:
        calls.append(rebound_session)
        await asyncio.wait_for(rebound_session.turn_lock.acquire(), timeout=1.0)
        rebound_session.turn_lock.release()

    runtime.set_rebind_session(_rebind)
    result = await runtime.new_session(persist=False)

    assert result["cancelled"] is False
    assert calls == [session]


async def test_rebind_does_not_run_on_a_vetoed_swap(session: AgentSession, runtime):
    calls = []
    runtime.set_rebind_session(lambda s: calls.append(s))

    def _veto(event, ctx):
        return {"cancel": True}

    bucket = session._extension_runner.register_extension("test-ext")
    bucket.on("session_before_switch", _veto)

    result = await runtime.new_session(persist=False)

    assert result == {"cancelled": True}
    assert calls == []


@pytest.mark.parametrize("deliver_as", ["steer", "followUp", "nextTurn"])
async def test_a_swap_never_discards_a_queued_message_without_a_trace(
    session: AgentSession, runtime, deliver_as: str
):
    """An accepted message either reaches a tree or is named by an event.

    Design-neutral on purpose: delivering it to the tree it was aimed at, or
    reporting the discard, both pass. Only the silent clear fails. The built
    answer is delivery (docs/CURSORS.md §8): the old cursor runs it in the
    background on its own tree.
    """
    old_log = session.session_log
    seen: list[str] = []
    session.subscribe(lambda event: seen.append(event.model_dump_json()))
    session._queue_message("QUEUED-BEFORE-SWAP", deliver_as=deliver_as)

    with patch("tau_agent_core.agent_loop.stream_simple", return_value=_Stream("answered")):
        result = await runtime.new_session(persist=False)
        await session.wait_for_deliveries()

    assert result["cancelled"] is False
    in_old_tree = "QUEUED-BEFORE-SWAP" in repr(old_log.entries())
    in_an_event = any("QUEUED-BEFORE-SWAP" in dumped for dumped in seen)
    assert in_old_tree or in_an_event


async def test_a_swap_delivers_the_queue_to_the_old_tree_and_retires_its_cursor(
    session: AgentSession, runtime
):
    """§8: the old cursor runs its queue where it was aimed, then closes; the head
    moves on to a fresh cursor on the new tree, which holds nothing."""
    old_log = session.session_log
    old_cursor = session.cursor
    session._queue_message("follow this up", deliver_as="followUp")

    with patch("tau_agent_core.agent_loop.stream_simple", return_value=_Stream("done")):
        await runtime.new_session(persist=False)
        await session.wait_for_deliveries()

    texts = repr(old_log.entries())
    assert "follow this up" in texts and "done" in texts
    assert session.cursor is not old_cursor
    assert not session.cursor.has_queued
    assert old_cursor not in session.cursors, "the delivered cursor is closed"
    assert "follow this up" not in repr(session.session_log.entries())


async def test_a_swap_of_an_idle_head_with_nothing_queued_closes_it_at_once(
    session: AgentSession, runtime
):
    old_cursor = session.cursor
    await runtime.new_session(persist=False)
    assert old_cursor not in session.cursors
    assert session.cursors == (session.cursor,)
