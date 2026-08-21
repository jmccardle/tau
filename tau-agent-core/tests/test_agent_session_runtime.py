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

from tau_llm.streaming import TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.agent_session_runtime import AgentSessionRuntime
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog, SessionInfo
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission

# ── a minimal, test-only SessionCatalog/ConversationSession pair ───────────


class _FakeConversationSession:
    """A RAM-only ``ConversationSession`` — wraps ``InMemorySessionLog`` for
    the entry/cursor algebra and layers the config reads on top."""

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

    @property
    def cursor(self) -> str | None:
        return self._log.cursor

    def entries(self) -> list[dict[str, Any]]:
        return self._log.entries()

    def append_message(self, message: dict[str, Any]) -> str:
        return self._log.append_message(message)

    def append_custom_message(self, message: dict[str, Any], custom_type: str) -> str:
        return self._log.append_custom_message(message, custom_type)

    def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        return self._log.append_custom_entry(custom_type, data)

    def append_compaction(self, summary: str, first_kept_id: str, tokens_before: int) -> str:
        return self._log.append_compaction(summary, first_kept_id, tokens_before)

    def append_elide(self, first_kept_id: str) -> str:
        return self._log.append_elide(first_kept_id)

    def append_navigate(self, target_id: str | None) -> str:
        return self._log.append_navigate(target_id)

    def append_branch_summary(self, summary: str, from_id: str | None) -> str:
        return self._log.append_branch_summary(summary, from_id)

    def append_at(self, parent_id, entry_type, payload, *, lane: str | None = None) -> str:
        return self._log.append_at(parent_id, entry_type, payload, lane=lane)

    @property
    def header(self) -> dict[str, Any]:
        return {"type": "session", "id": self.id, "cwd": self._cwd}

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [e["message"] for e in self._log.entries() if e.get("type") == "message"]

    @property
    def context(self) -> list[dict[str, Any]]:
        return ConversationTree(self.entries(), self.cursor).context_for()

    @property
    def model(self) -> str:
        return self._model

    @property
    def backend(self) -> str:
        return self._backend

    def display_title(self) -> str:
        return self._name or f"Session ({self._model})"

    def append_model_change(self, model: str, backend: str) -> str:
        self._model, self._backend = model, backend
        return "model-change"

    def append_session_info(self, name: str) -> str:
        self._name = name
        return "session-info"


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
            session.append_message({"role": "system", "content": system_prompt})
        self._sessions[session.id] = session
        return session

    def create_ephemeral(
        self, cwd, model, backend, *, system_prompt: str | None = None, name: str | None = None
    ) -> ConversationSession:
        # Mirrors FileSessionCatalog: same construction, never registered.
        session = _FakeConversationSession(cwd, model, backend, name)
        if system_prompt:
            session.append_message({"role": "system", "content": system_prompt})
        return session

    def load(self, ref: str) -> ConversationSession:
        try:
            return self._sessions[ref]
        except KeyError:
            raise FileNotFoundError(f"no in-memory session {ref!r}") from None

    def fork(self, source: ConversationSession, cwd: str) -> ConversationSession:
        assert isinstance(source, _FakeConversationSession)
        forked = _FakeConversationSession(cwd, source.model, source.backend)
        for entry in source.entries():
            if entry.get("type") == "message":
                forked.append_message(entry["message"])
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
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    def __init__(self, text: str) -> None:
        self._text = text

    def __aiter__(self):
        async def _gen():
            yield TextDeltaEvent(delta=self._text, partial=_assistant(self._text))

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
    old_log.append_message({"role": "user", "content": "hi"})

    session._last_usage = {"input_tokens": 5}
    session.record_side_usage({"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
    session._pending_follow_up_messages.append("queued-follow-up")
    session._pending_next_turn_messages.append("queued-next-turn")
    from tau_llm.types import UserMessage

    session._pending_steer_messages.append(
        UserMessage.model_validate(
            {"role": "user", "content": [{"type": "text", "text": "x"}], "timestamp": 0}
        )
    )
    session._deferred_ops.append({"kind": "compact", "custom_instructions": None})
    session._is_streaming = True
    session._pre_turn_leaf = old_log.cursor

    result = await runtime.new_session(persist=False)

    from tau_agent_core.usage import zero_usage

    assert result["cancelled"] is False
    assert session.get_usage() is None
    assert session.side_usage == zero_usage()
    assert session._pending_follow_up_messages == []
    assert session._pending_next_turn_messages == []
    assert session._pending_steer_messages == []
    assert session._deferred_ops == []
    assert session.is_streaming is False
    assert session._pre_turn_leaf is None
    # log + cursor: a brand new, empty log — not the dirty one.
    assert session.session_log is not old_log
    assert session.session_log.cursor is None
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
    # _turn_token_counter is documented as NEVER reset on AgentSession itself;
    # this runtime must not touch it either.
    assert session._turn_token_counter == 0


async def test_last_compaction_anchor_is_cleared_not_rederived(session: AgentSession, runtime):
    """§10 'resolved': a fresh log has no compaction to anchor to — this
    proves it is genuinely GONE, not carried over or recomputed."""
    log = session.session_log
    first = log.append_message({"role": "user", "content": "turn one"})
    log.append_compaction(summary="a summary", first_kept_id=first, tokens_before=100)
    # Sanity: the OLD log really does have a splice anchor before the reset.
    old_active = ConversationTree(log.entries(), log.cursor).context_for()
    assert any(m.get("role") == "user" and "summary" in str(m.get("content")) for m in old_active)

    await runtime.new_session(persist=False)

    new_log = session.session_log
    assert new_log.entries() == []
    active = ConversationTree(new_log.entries(), new_log.cursor).context_for()
    assert active == []


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
        # new_session() must be BLOCKED on the turn lock, not racing ahead —
        # abort() alone doesn't finish a turn synchronously.
        assert not swap_task.done()

        gate.set()
        await asyncio.wait_for(task_a, timeout=5.0)
        result = await asyncio.wait_for(swap_task, timeout=5.0)

    assert result["cancelled"] is False
    # Every event the turn emitted reached the subscriber strictly before the
    # swap completed — in particular agent_end, the turn's last event.
    assert "agent_end" in recorded
    agent_end_index = recorded.index("agent_end")
    # Nothing recorded after the last agent_end belongs to a NEW turn (there
    # isn't one), and the new, empty session confirms the swap really
    # happened after that point, not interleaved with it.
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

        # The whole point: this must return, not hang. The outer timeout is a
        # test safety net (generous relative to the 0.05s the runtime itself
        # is bounded by), not the behaviour under test.
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
    # fork() requires a catalog-produced ConversationSession (see
    # test_fork_before_any_catalog_session_raises for the bare-log case) —
    # establish the invariant the same way rpc_mode.py does at startup.
    session.session_log = catalog.create_ephemeral("/work", "m", "openai")
    source_log = session.session_log
    source_log.append_message({"role": "user", "content": "hello"})
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
    other.append_message({"role": "user", "content": "over there"})

    result = await runtime.switch_session(other.id)

    assert result["cancelled"] is False
    assert session.session_log is other
    assert result["session_id"] == other.id
    assert result["cursor"] == other.cursor


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
        # Proves the lock is NOT held here: acquiring it would hang forever
        # if new_session() were still holding it at rebind time.
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
