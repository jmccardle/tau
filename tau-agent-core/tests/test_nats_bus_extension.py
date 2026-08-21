"""tau-007 — the tectum bus extension (SIM_SPEC_v2 §12.4, §16.6 (H7), §16.10 (H8)).

**Rewritten 2026-07-29 against tectum's real implementation.** The previous
version of this file asserted the wire format τ had *guessed* while it was the
only end that existed. With ``~/Development/tectum`` readable, the guess is
known wrong, so these tests now assert the real contract — transcribed from
``tectum/event.py``, ``tectum/subjects.py``, and the three effector nodes, and
recorded in ``docs/WIRE-CONTRACT.md``. Every difference is deliberate:

- the wire format is a ``TectumEvent`` **envelope**, not a bare payload dict
- ack subjects are **per-effector** and the kind token is not always the verb
- a *tectum* ack has **no ``status`` field**; failure is ``ok: False`` / non-null ``error``
- ``binding_id`` is **preserved across hops**, not minted per publish

**Extended 2026-08-01 for the second producer.** τ now also drives McRogueFace's
body node (``robot_sim_stack/world/body_node.py``), which is not a tectum
effector and does not speak tectum's failure dialect — it signals with
``status: ok|refused|error``. Reading only ``ok``/``error`` made this side blind
to all three, so a blocked robot reported a completed move. The world-verb facts
(``move_to``, ``wait``, ``note``) were confirmed against a *running headless
engine* on the wire before being asserted here, not read off a spec:
``events.journal.move_to.<bid>`` answered
``{"status":"ok","trigger":"DONE","verb":"move_to","world_tick":383}`` and the
courier moved to the target cell.

Two tiers of test:

- Declaration / capability-preflight tests need no network: H7/H8's checks run
  inside ``_load_one_extension`` BEFORE ``register()`` calls ``nats.connect``.
- The wire-level tests start a genuine ``nats-server`` container — no mock
  transport in this package models a broker, and tau-007's DoD requires "a real
  nats-server, not a mock ... with two OS processes." Skipped (not xfailed, not
  faked) when Docker is unavailable, so a missing dependency reads as "not run".

**Every stand-in effector flushes after subscribing.** Core NATS drops a message
with no matching subscription, silently, and ``nats-py``'s ``subscribe()``
returns once SUB is queued on the client — not once the server has registered
it. These tests subscribe on connection B and publish from τ's connection A, and
NATS only guarantees command ordering *within* one connection, so without the
flush the ack subscription can lose the race and the tool waits out its whole
timeout. Observed once as a 19s run with ``assert 0 == 1``. The production ack
path in ``_execute`` needs no flush for the same reason these do: it subscribes
and publishes on the SAME connection, where ordering is guaranteed.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extensions_builtin import nats_bus
from tau_agent_core.sdk import ExtensionCapabilityError
from tau_agent_core.session_log import InMemorySessionLog

_EXT_PATH = str(Path(nats_bus.__file__))

#: What ``praxis/harness_text.yaml`` drives the responder with.
_INBOUND = "events.sensation.audio.resolved.clean"

#: tectum's ``audio.stt`` in-flight-hypothesis rail (stt.py:56) — the second,
#: optional inbound subject that paints instead of submitting.
_DRAFT = "events.sensation.audio.partial"


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


def _config(**overrides: Any) -> dict[str, dict[str, Any]]:
    base: dict[str, Any] = {"workspace": "demo", "inbound_subject": _INBOUND}
    base.update(overrides)
    return {"nats_bus": base}


def _make_session(
    *,
    bus_available: bool = False,
    extensions_config: dict[str, dict[str, Any]] | None = None,
    extensions: list[Any] | None = None,
) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        bus_available=bus_available,
        extensions_config=extensions_config if extensions_config is not None else _config(),
        extensions=extensions or [],
    )


def _envelope(subject: str, payload: dict[str, Any], binding_id: str | None = None) -> bytes:
    """A TectumEvent as a tectum node puts it on the wire (``event.py:76``)."""
    return json.dumps(
        {
            "event_id": str(uuid.uuid4()),
            "event_type": subject,
            "source": "test.publisher",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "sequence_number": 0,
            "ttl_ms": 60000,
            "payload": payload,
            "produced_by_schema": None,
            "routed_by_schema": None,
            "binding_id": binding_id,
            "expectation": None,
            "residual": None,
            "origin_node": "test",
            "hops": [],
            "seen_by": [],
            "audit": None,
        }
    ).encode("utf-8")


# ── declaration / capability preflight (no network) ─────────────────────────


class TestDeclarationAndPreflightNoNetworkNeeded:
    async def test_touches_bus_and_subjects_are_declared(self):
        assert nats_bus.TOUCHES_BUS is True
        assert nats_bus.SUBJECTS == (
            "events.sensation.>",
            "events.workspace.*.in",
            "events.workspace.*.out.>",
            "events.journal.>",
            "events.action.>",
        )

    async def test_declared_inbound_covers_the_real_dispatch_subject(self):
        """``events.workspace.<agent>.in`` has FOUR tokens (``subjects.agent_in``).

        The old declaration was ``events.workspace.*.in.>``, which requires five
        or more and so could never match the subject tectum dispatches on. This
        asserts the fix, not merely the current value.
        """
        assert "events.workspace.*.in" in nats_bus.SUBJECTS
        assert "events.workspace.*.in.>" not in nats_bus.SUBJECTS

    async def test_ack_subjects_match_tectums_effectors(self):
        """Transcribed from the effector nodes; the kind token is not the verb."""
        assert nats_bus.VERBS["speak"].ack_subject == "events.action.speech.completed.{binding_id}"
        # journal_append acks as "append" — subjects.journal_ack("append", bid)
        assert nats_bus.VERBS["journal_append"].ack_subject == "events.journal.append.{binding_id}"
        assert (
            nats_bus.VERBS["jmfts_write"].ack_subject == "events.journal.jmfts_write.{binding_id}"
        )
        # delegate's consumer acks nothing; None is the contract, not a gap.
        assert nats_bus.VERBS["delegate"].ack_subject is None

    async def test_only_speak_is_terminal(self):
        """Terminality is per-verb, exactly as in tectum's ToolSpec.

        `speak` carries tectum's SPEAK.result wording and ends the turn;
        journalling and then speaking about it is one coherent turn, so
        journal_append does not. Getting this wrong in either direction is a
        live failure: too loose gives the observed speak loop, too strict makes
        journal_append the last thing an agent can ever do in a turn.
        """
        assert nats_bus.VERBS["speak"].terminal is True
        assert "Your turn is over" in nats_bus.VERBS["speak"].result
        for verb in ("journal_append", "jmfts_write", "delegate"):
            assert nats_bus.VERBS[verb].terminal is False, verb
            assert nats_bus.VERBS[verb].result == "", verb

    async def test_refused_when_session_has_no_bus(self):
        session = _make_session(bus_available=False)
        with pytest.raises(ExtensionCapabilityError, match="bus_available=False"):
            await session.load_extensions([_EXT_PATH], discover=False)

    async def test_requires_workspace_config(self):
        """Fail-Early: no default workspace — a silent one binds every subject
        to the wrong world."""
        session = _make_session(bus_available=True, extensions_config={})
        with pytest.raises(ValueError, match="workspace"):
            await session.load_extensions([_EXT_PATH], discover=False)

    async def test_requires_inbound_subject_config(self):
        """Fail-Early: which subject drives an agent is a property of the active
        tectum schema. A default binds silently to a subject nothing publishes
        on — a failure with no symptom at all."""
        session = _make_session(
            bus_available=True, extensions_config={"nats_bus": {"workspace": "demo"}}
        )
        with pytest.raises(ValueError, match="inbound_subject"):
            await session.load_extensions([_EXT_PATH], discover=False)

    async def test_unknown_verb_raises_rather_than_guessing_an_ack_subject(self):
        """A verb with no known ack contract cannot be served: a guessed subject
        makes every call wait out the full timeout."""
        session = _make_session(bus_available=True, extensions_config=_config(verbs=("teleport",)))
        with pytest.raises(ValueError, match="no ack contract known"):
            await session.load_extensions([_EXT_PATH], discover=False)

    async def test_registers_one_tool_per_verb(self):
        """Not one generic verb-parameterized tool: that made the model invent
        both the verb string and the payload shape."""
        session = _make_session(
            bus_available=True,
            extensions_config=_config(verbs=("speak", "journal_append")),
        )
        result = await session.load_extensions([_EXT_PATH], discover=False)
        assert len(result.errors) == 0

        from tau_agent_core.sdk import summarize_extensions

        info = summarize_extensions(result)[0]
        assert "speak" in info.tools
        assert "journal_append" in info.tools
        assert "workspace_effector" not in info.tools

        schema = session._registry.get_active_tools()["speak"]
        assert schema.parameters["required"] == ["text"]


# ── two producers, one wire (2026-08-01) ────────────────────────────────────
#
# VERBS spans tectum's effectors AND McRogueFace's body node. Every fact below
# was read off running code, and the wire behaviour was confirmed against a
# headless engine before these were written: a `move_to` acked
# {"status":"ok","trigger":"DONE","verb":"move_to","world_tick":383} on
# events.journal.move_to.<bid> and the courier moved to the target cell.


class TestWorldVerbs:
    async def test_the_world_verbs_ack_where_the_body_node_publishes(self):
        """Transcribed from ``world/body_node.py`` and seen on the wire.

        ``note`` is propose-class (``verbs.py`` ``VERB_CLASSES``): the body node
        records it in ``pending_proposals`` and returns WITHOUT acking. ``None``
        is that fact — waiting on an ack would time out on every call.
        """
        assert nats_bus.VERBS["move_to"].ack_subject == "events.journal.move_to.{binding_id}"
        assert nats_bus.VERBS["wait"].ack_subject == "events.journal.wait.{binding_id}"
        assert nats_bus.VERBS["note"].ack_subject is None

    async def test_move_to_takes_coordinates_not_text(self):
        """The regression this whole change exists to prevent.

        Every tool used to register one hard-coded ``{"text": string}`` schema,
        so a verb taking ``{x, y}`` was not unimplemented — it was inexpressible.
        Asserting the *absence* of ``text`` matters as much as the presence of
        ``x``: a schema carrying both would let the model keep speaking
        coordinates.
        """
        session = _make_session(
            bus_available=True,
            extensions_config=_config(verbs=("move_to", "wait", "note")),
        )
        result = await session.load_extensions([_EXT_PATH], discover=False)
        assert len(result.errors) == 0

        tools = session._registry.get_active_tools()
        move = tools["move_to"].parameters
        assert sorted(move["required"]) == ["x", "y"]
        assert move["properties"]["x"]["type"] == "integer"
        assert move["properties"]["y"]["type"] == "integer"
        assert "text" not in move["properties"]

        assert tools["wait"].parameters["properties"]["turns"]["type"] == "integer"
        assert tools["note"].parameters["required"] == ["text"]

    @pytest.mark.parametrize("verb", sorted(nats_bus.VERBS))
    async def test_every_verbs_schema_is_internally_consistent(self, verb):
        """A required name with no property is a parameter the model is asked
        for and the validator then ignores — the schema would look enforced and
        not be. Cheap to assert once per verb, and it bites the moment someone
        adds an eighth."""
        spec = nats_bus.VERBS[verb]
        schema = spec.parameters
        assert schema["type"] == "object"
        properties = schema["properties"]
        assert properties, f"{verb}: a verb with no parameters publishes an empty payload"
        for name in schema["required"]:
            assert name in properties, f"{verb}: required {name!r} has no property"
        # non_empty is only meaningful for a required string: τ's validator has
        # no minLength, which is exactly why this constraint lives in code.
        for name in spec.non_empty:
            assert name in schema["required"], f"{verb}: non_empty {name!r} is not required"
            assert properties[name]["type"] == "string", f"{verb}: non_empty {name!r} is not a str"

    @pytest.mark.parametrize(
        "payload,expected",
        [
            # tectum's dialect — no `status` field exists on these at all.
            ({"doc_id": 1234}, None),
            ({"text": "hi", "backend": "test", "ok": True, "error": None}, None),
            ({"ok": False}, "ok=False"),
            ({"error": "device busy"}, "error="),
            # McRogueFace's dialect, verbatim from a live ack.
            (
                {"status": "ok", "verb": "move_to", "trigger": "DONE", "world_tick": 383},
                None,
            ),
            (
                {"status": "refused", "verb": "move_to", "trigger": "BLOCKED", "world_tick": 400},
                "status='refused'",
            ),
            (
                {"status": "error", "verb": "move_to", "trigger": None, "world_tick": 586},
                "status='error'",
            ),
            # A dialect nobody speaks yet reads as failure, not success.
            ({"status": "weather"}, "status='weather'"),
        ],
    )
    async def test_ack_failure_reads_both_dialects(self, payload, expected):
        """The silent failure this change closes.

        Before ``status`` was read, all three McRogueFace values returned
        ``None`` — measured, not supposed. A blocked robot and a malformed
        payload both came back to the model as a completed move: silent, and in
        the optimistic direction (``SIM_SPEC_v2.md`` §13.2).
        """
        failure = nats_bus._ack_failure(payload)
        if expected is None:
            assert failure is None
        else:
            assert failure is not None
            assert expected in failure

    async def test_a_refusal_names_its_trigger(self):
        """BLOCKED and an immediate busy refusal are both ``status: "refused"``
        and only ``trigger`` tells them apart — the model needs it to know
        whether retrying could ever work."""
        failure = nats_bus._ack_failure(
            {"status": "refused", "verb": "move_to", "trigger": "BLOCKED"}
        )
        assert failure is not None and "trigger=BLOCKED" in failure

        busy = nats_bus._ack_failure({"status": "refused", "verb": "move_to", "trigger": None})
        assert busy is not None and "trigger=" not in busy


# ── inbound admission: the core's submission lifecycle, not a local flag ─────
#
# These drive the subscription callback directly rather than over a broker: what
# is under test is what this extension does with an inbound event once it has one
# (docs/SUBMISSION-LIFECYCLE.md phase 5 — it used to hand-roll a
# ``state["turn_in_flight"]`` flag and drop silently; it now submits through the
# one door and reports the core's refusal). The wire format itself is covered
# against a real nats-server below. The broker is replaced, not faked-in-place:
# nothing here asserts anything about NATS.


class _FakeMsg:
    def __init__(self, subject: str, data: bytes) -> None:
        self.subject = subject
        self.data = data


class _FakeSubscription:
    def __init__(self, subject: str, cb: Any) -> None:
        self.subject = subject
        self.cb = cb
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _FakeConnection:
    """Just enough of a NATS connection for ``register``'s session_start."""

    def __init__(self) -> None:
        self.subscriptions: list[_FakeSubscription] = []
        self.closed = False

    async def subscribe(self, subject: str, cb: Any = None) -> _FakeSubscription:
        sub = _FakeSubscription(subject, cb)
        self.subscriptions.append(sub)
        return sub

    async def close(self) -> None:
        self.closed = True


class TestInboundAdmission:
    async def _started(self, session: AgentSession) -> _FakeConnection:
        connection = _FakeConnection()

        async def _connect(url: str, **kwargs: Any) -> _FakeConnection:
            return connection

        await session.load_extensions([_EXT_PATH], discover=False)
        with patch.object(nats_bus.nats, "connect", _connect):
            await session.emit_session_start()
        assert connection.subscriptions, "register() never subscribed the inbound subject"
        return connection

    async def test_an_inbound_event_submits_as_this_extension(self):
        """Provenance stops lying: the turn a bus event drives is stamped
        ``source="extension"``/``submitter="nats_bus"``, carrying the flow's
        identity in ``correlation`` — not ``interactive``/``human``."""
        session = _make_session(bus_available=True)
        connection = await self._started(session)
        seen: list[Any] = []
        session.subscribe(seen.append)

        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_silent(),
            ):
                await connection.subscriptions[0].cb(
                    _FakeMsg(_INBOUND, _envelope(_INBOUND, {"text": "hello bus"}, "flow-7"))
                )

            assert seen, "the inbound event drove no turn"
            for event in seen:
                assert event.source == "extension"
                assert event.submitter == "nats_bus"
                assert event.correlation["subject"] == _INBOUND
                assert event.correlation["binding_id"] == "flow-7"
            assert "hello bus" in json.dumps(session.messages)
        finally:
            await session.emit_session_shutdown()

    async def test_a_second_message_while_a_turn_is_in_flight_is_refused(self):
        """The core refuses it (``multitask_strategy="reject"``), and this
        extension reports the refusal's own reason rather than inventing one —
        the ``turn_in_flight`` flag it used to keep is gone."""
        dropped: list[Any] = []

        def listener_ext(api: Any) -> None:
            async def _on_dropped(payload: Any) -> None:
                dropped.append(payload)

            api.on("ext:nats_bus:inbound_dropped", _on_dropped)

        session = _make_session(bus_available=True, extensions=[listener_ext])
        connection = await self._started(session)

        try:
            # A turn genuinely holds the session's in-flight slot.
            await session._turn_lock.acquire()
            try:
                await connection.subscriptions[0].cb(
                    _FakeMsg(_INBOUND, _envelope(_INBOUND, {"text": "second"}, "flow-8"))
                )
            finally:
                session._turn_lock.release()

            assert len(dropped) == 1
            assert dropped[0]["subject"] == _INBOUND
            assert "in flight" in dropped[0]["reason"]
            assert dropped[0]["submission_id"]
            # Refused, not enqueued: the stale utterance never reaches the log.
            assert "second" not in json.dumps(session.messages)
        finally:
            await session.emit_session_shutdown()


class _StatusRecordingDelegate:
    """Just enough of ``_ExtensionUIDelegate`` (app.py) to observe ``set_status``."""

    def __init__(self) -> None:
        self.status_calls: list[tuple[str, str | None]] = []

    def set_status(self, key: str, text: str | None) -> None:
        self.status_calls.append((key, text))


class TestDraftStatus:
    """The second inbound rail (``draft_subject``): paints, never submits."""

    async def _started(
        self, session: AgentSession
    ) -> _FakeConnection:  # mirrors TestInboundAdmission._started
        connection = _FakeConnection()

        async def _connect(url: str, **kwargs: Any) -> _FakeConnection:
            return connection

        await session.load_extensions([_EXT_PATH], discover=False)
        with patch.object(nats_bus.nats, "connect", _connect):
            await session.emit_session_start()
        return connection

    async def test_omitting_draft_subject_subscribes_only_the_inbound_one(self):
        session = _make_session(bus_available=True)  # default config: no draft_subject
        connection = await self._started(session)
        try:
            assert [s.subject for s in connection.subscriptions] == [_INBOUND]
        finally:
            await session.emit_session_shutdown()

    async def test_a_draft_event_paints_the_status_strip_when_interactive(self):
        session = _make_session(bus_available=True, extensions_config=_config(draft_subject=_DRAFT))
        connection = await self._started(session)
        delegate = _StatusRecordingDelegate()
        session.set_ui_delegate(delegate)
        try:
            assert [s.subject for s in connection.subscriptions] == [_INBOUND, _DRAFT]
            await connection.subscriptions[1].cb(
                _FakeMsg(_DRAFT, _envelope(_DRAFT, {"text": "walk to the"}, "flow-9"))
            )
            assert delegate.status_calls == [("hearing", "walk to the")]
        finally:
            await session.emit_session_shutdown()

    async def test_a_draft_event_is_skipped_before_parsing_when_headless(self):
        """Not just "no delegate call" — the callback returns before ``json.loads``,
        so a headless run pays nothing per partial (the module docstring's claim)."""
        session = _make_session(bus_available=True, extensions_config=_config(draft_subject=_DRAFT))
        connection = await self._started(session)
        # No set_ui_delegate call: session stays in headless mode.
        try:
            with patch.object(nats_bus.json, "loads") as loads:
                await connection.subscriptions[1].cb(_FakeMsg(_DRAFT, b"irrelevant"))
                loads.assert_not_called()
        finally:
            await session.emit_session_shutdown()

    async def test_an_empty_partial_clears_rather_than_blanks_the_slot(self):
        session = _make_session(bus_available=True, extensions_config=_config(draft_subject=_DRAFT))
        connection = await self._started(session)
        delegate = _StatusRecordingDelegate()
        session.set_ui_delegate(delegate)
        try:
            await connection.subscriptions[1].cb(
                _FakeMsg(_DRAFT, _envelope(_DRAFT, {"text": ""}, "flow-10"))
            )
            assert delegate.status_calls == [("hearing", None)]
        finally:
            await session.emit_session_shutdown()

    async def test_a_committed_turn_clears_the_draft_slot(self):
        session = _make_session(bus_available=True, extensions_config=_config(draft_subject=_DRAFT))
        connection = await self._started(session)
        delegate = _StatusRecordingDelegate()
        session.set_ui_delegate(delegate)
        try:
            await connection.subscriptions[1].cb(
                _FakeMsg(_DRAFT, _envelope(_DRAFT, {"text": "walk to the door"}, "flow-11"))
            )
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_silent(),
            ):
                await connection.subscriptions[0].cb(
                    _FakeMsg(
                        _INBOUND,
                        _envelope(_INBOUND, {"text": "walk to the door"}, "flow-11"),
                    )
                )
            assert delegate.status_calls == [
                ("hearing", "walk to the door"),
                ("hearing", None),
            ]
        finally:
            await session.emit_session_shutdown()

    async def test_draft_subject_must_be_a_string(self):
        session = _make_session(bus_available=True, extensions_config=_config(draft_subject=123))
        with pytest.raises(ValueError, match="draft_subject"):
            await session.load_extensions([_EXT_PATH], discover=False)


# ── over a real nats-server ──────────────────────────────────────────────────


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@pytest.fixture
def real_nats_url():
    """Starts a REAL nats-server in its own container; yields its URL.

    Two OS processes for the duration of the test: this pytest process (the
    NATS client, twice over — the extension's connection and the test's own
    "external node" connection) and the containerized nats-server. No part of
    this fixture fakes the broker.
    """
    if not _docker_available():
        pytest.skip(
            "docker not available; tau-007's DoD requires verification against a "
            "real nats-server, not a mock, so this test is skipped rather than faked"
        )
    proc = subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", "127.0.0.1::4222", "nats:latest"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not start a real nats-server container: {proc.stderr.strip()}")
    container_id = proc.stdout.strip()
    try:
        port_out = subprocess.run(
            ["docker", "port", container_id, "4222"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        host_port = int(port_out.splitlines()[0].rsplit(":", 1)[1])
        deadline = time.time() + 15
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", host_port), timeout=0.5):
                    break
            except OSError as exc:
                last_err = exc
                time.sleep(0.2)
        else:
            raise RuntimeError(f"nats-server container did not open its port: {last_err}")
        yield f"nats://127.0.0.1:{host_port}"
    finally:
        subprocess.run(["docker", "stop", container_id], capture_output=True, timeout=30)


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


def _has_tool_result(messages: list[Any], tool_name: str) -> bool:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return True
    return False


def _fake_stream_calling(tool_name: str, tool_args: dict[str, Any]):
    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if _has_tool_result(messages, tool_name):
            final = _text_assistant("done")
            return _Stream(
                [TextDeltaEvent(delta="done", partial=final), DoneEvent(final=final, usage=Usage())]
            )
        final = _tool_call_assistant("call_1", tool_name, tool_args)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _fake_stream_silent():
    """A turn that calls nothing — the agent choosing silence."""

    async def fake(model, context, options=None):
        final = _text_assistant("")
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _tool_results(messages: list[Any]) -> list[Any]:
    return [
        m
        for m in messages
        if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) == "toolResult"
    ]


class TestOverARealNatsServer:
    async def test_tool_call_publishes_a_valid_tectum_envelope(self, real_nats_url):
        """A genuine agent-loop tool call publishes a real NATS message that a
        separate client receives — and it parses as a TectumEvent.

        The old code published a bare ``{"text":…, "binding_id":…}``, which
        raises ``KeyError: 'event_id'`` in ``TectumEvent.from_dict``. This
        asserts every field that consumer indexes without a default.
        """
        import nats

        session = _make_session(
            bus_available=True,
            extensions_config=_config(nats_url=real_nats_url, ack_timeout_s=5),
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()

        # An independent connection — stands in for effector.speech, a second
        # client against the same real broker. It acks what it sees, because
        # tau-002 means the tool call does not return without one.
        body_node = await nats.connect(real_nats_url)
        received: list[Any] = []

        async def _cb(msg):
            received.append(msg)
            event = json.loads(msg.data.decode("utf-8"))
            ack_subject = f"events.action.speech.completed.{event['binding_id']}"
            await body_node.publish(
                ack_subject,
                _envelope(
                    ack_subject,
                    {
                        "text": event["payload"]["text"],
                        "backend": "test",
                        "ok": True,
                        "error": None,
                        "world_tick": 42,
                    },
                    event["binding_id"],
                ),
            )

        sub = await body_node.subscribe("events.workspace.demo.out.speak", cb=_cb)
        await body_node.flush()  # SUB must reach the server before tau publishes

        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_calling("speak", {"text": "hello world"}),
            ):
                messages = await session.prompt("greet the room")

            deadline = time.time() + 5
            while not received and time.time() < deadline:
                await asyncio.sleep(0.05)

            assert len(received) == 1
            assert received[0].subject == "events.workspace.demo.out.speak"
            sent = json.loads(received[0].data.decode("utf-8"))

            # Every key TectumEvent.from_dict indexes without a default.
            for required in (
                "event_id",
                "event_type",
                "source",
                "timestamp",
                "sequence_number",
                "ttl_ms",
                "payload",
                "origin_node",
            ):
                assert required in sent, f"envelope is missing {required!r}"
            assert sent["event_type"] == "events.workspace.demo.out.speak"
            assert sent["source"] == "agent.demo"
            # The body is at .payload, not the top level.
            assert sent["payload"]["text"] == "hello world"
            assert sent["payload"]["agent"] == "demo"
            assert "text" not in sent
            uuid.UUID(sent["event_id"])  # parses, or raises
            datetime.datetime.fromisoformat(sent["timestamp"])

            results = _tool_results(messages)
            assert len(results) == 1
            assert results[0]["is_error"] is False
            # A terminal verb returns tectum's turn-ending sentence, NOT the
            # transport blob — see test_speak_terminates_the_turn.
            assert "Your turn is over" in results[0]["content"][0]["text"]
        finally:
            await sub.unsubscribe()
            await body_node.close()
            await session.emit_session_shutdown()

    async def test_speak_terminates_the_turn(self, real_nats_url):
        """Regression for a live infinite loop.

        Observed: ``speak`` returned only ``{"content": [...]}``, so
        ``batch.terminate`` was False and the agent loop ran another turn. The
        tool result it fed back was a transport blob
        (``…acked on events.action.speech.completed.<bid>: {json}``) that said
        nothing about the turn being over and quoted the model's own sentence
        back at it, so the model called ``speak`` again with identical text —
        turn after turn, to 28 before it was killed, bounded only by
        ``max_turns=50``.

        tectum reproduced and documented this exact failure for pi
        (``tectum/tools.py:29-33``: "a bare 'ok' invites another call (observed
        live: a speak loop)"). The fake model here is that pathological model: it
        calls ``speak`` on EVERY completion, unconditionally. Exactly one call
        must happen anyway, because ``terminate`` stops the loop before the
        model is consulted a second time.
        """
        import nats

        session = _make_session(
            bus_available=True,
            extensions_config=_config(nats_url=real_nats_url, ack_timeout_s=5),
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()

        node = await nats.connect(real_nats_url)
        published: list[Any] = []

        async def _cb(msg):
            published.append(msg)
            event = json.loads(msg.data.decode("utf-8"))
            ack = f"events.action.speech.completed.{event['binding_id']}"
            await node.publish(
                ack, _envelope(ack, {"ok": True, "error": None}, event["binding_id"])
            )

        sub = await node.subscribe("events.workspace.demo.out.speak", cb=_cb)
        await node.flush()  # SUB must reach the server before tau publishes

        async def _always_calls_speak(model, context, options=None):
            final = _tool_call_assistant("call_x", "speak", {"text": "the same thing"})
            return _Stream([DoneEvent(final=final, usage=Usage())])

        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_always_calls_speak,
            ):
                messages = await session.prompt("say something")

            deadline = time.time() + 5
            while not published and time.time() < deadline:
                await asyncio.sleep(0.05)

            # One publish, not 28. This is the whole assertion.
            assert len(published) == 1, (
                f"speak ran {len(published)} times against a model that always calls "
                "it — terminate did not stop the loop"
            )
            results = _tool_results(messages)
            assert len(results) == 1
            assert results[0]["is_error"] is False
        finally:
            await sub.unsubscribe()
            await node.close()
            await session.emit_session_shutdown()

    async def test_non_terminal_verb_lets_the_turn_continue(self, real_nats_url):
        """The other direction: journal_append must NOT end the turn.

        Noting something and then speaking about it is one coherent turn, and
        tectum's JOURNAL_APPEND leaves its ToolSpec.result at "" for that reason.
        A blanket terminate would make journalling the last act of any turn.
        """
        import nats

        session = _make_session(
            bus_available=True,
            extensions_config=_config(
                nats_url=real_nats_url, ack_timeout_s=5, verbs=("journal_append",)
            ),
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()
        node = await nats.connect(real_nats_url)

        async def _cb(msg):
            event = json.loads(msg.data.decode("utf-8"))
            ack = f"events.journal.append.{event['binding_id']}"
            await node.publish(ack, _envelope(ack, {"doc_id": 7}, event["binding_id"]))

        sub = await node.subscribe("events.workspace.demo.out.journal_append", cb=_cb)
        await node.flush()  # SUB must reach the server before tau publishes
        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_calling("journal_append", {"text": "a note"}),
            ):
                messages = await session.prompt("note it")
            # _fake_stream_calling returns plain text on the turn AFTER the tool
            # result lands; reaching that text at all proves the loop continued.
            assert any(
                (m.get("role") if isinstance(m, dict) else None) == "assistant"
                and "done" in json.dumps(m.get("content"))
                for m in messages
            ), "the loop stopped after journal_append — it must not be terminal"
        finally:
            await sub.unsubscribe()
            await node.close()
            await session.emit_session_shutdown()

    async def test_inbound_envelope_drives_a_turn_and_reaches_the_channel(self, real_nats_url):
        """τ on the bus directly: an inbound TectumEvent becomes an agent turn.

        The text is read from ``.payload.text``; the old code required it at the
        top level and so rejected every real tectum event.
        """
        import nats

        seen: list[Any] = []

        def listener_ext(api: Any) -> None:
            async def _on_inbound(payload: Any) -> None:
                seen.append(payload)

            api.on("ext:nats_bus:inbound", _on_inbound)

        session = _make_session(
            bus_available=True,
            extensions_config=_config(nats_url=real_nats_url),
            extensions=[listener_ext],
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()

        publisher = await nats.connect(real_nats_url)
        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_silent(),
            ):
                await publisher.publish(
                    _INBOUND, _envelope(_INBOUND, {"text": "hello from the bus"}, "bid-1")
                )
                deadline = time.time() + 5
                while not seen and time.time() < deadline:
                    await asyncio.sleep(0.05)
                # let the driven turn finish before tearing the session down
                await asyncio.sleep(0.5)

            assert len(seen) == 1
            assert seen[0]["subject"] == _INBOUND
            assert seen[0]["event"]["payload"]["text"] == "hello from the bus"

            # The turn actually ran: the utterance is in the session's messages.
            assert "hello from the bus" in json.dumps(session.messages)
        finally:
            await publisher.close()
            await session.emit_session_shutdown()

    async def test_binding_id_is_preserved_from_the_inbound_event(self, real_nats_url):
        """binding_id correlates one logical flow across hops (``event.py:23``).

        The old code minted a fresh one per publish, which broke correlation
        and — because effectors ack on ``<subject>.<binding_id>`` — sent the ack
        somewhere nobody was listening.
        """
        import nats

        session = _make_session(
            bus_available=True,
            extensions_config=_config(nats_url=real_nats_url, ack_timeout_s=5),
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()

        node = await nats.connect(real_nats_url)
        spoke: list[Any] = []

        async def _cb(msg):
            spoke.append(msg)
            event = json.loads(msg.data.decode("utf-8"))
            ack = f"events.action.speech.completed.{event['binding_id']}"
            await node.publish(
                ack, _envelope(ack, {"ok": True, "error": None}, event["binding_id"])
            )

        sub = await node.subscribe("events.workspace.demo.out.speak", cb=_cb)
        await node.flush()  # SUB must reach the server before tau publishes
        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_calling("speak", {"text": "answering"}),
            ):
                await node.publish(
                    _INBOUND, _envelope(_INBOUND, {"text": "say something"}, "flow-42")
                )
                deadline = time.time() + 10
                while not spoke and time.time() < deadline:
                    await asyncio.sleep(0.05)

            assert spoke, "tau published no effector event"
            sent = json.loads(spoke[0].data.decode("utf-8"))
            assert sent["binding_id"] == "flow-42"
        finally:
            await sub.unsubscribe()
            await node.close()
            await session.emit_session_shutdown()

    async def test_malformed_inbound_is_reported_not_raised(self, real_nats_url):
        """A bad payload is an event to report, never a reason to tear down the
        subscription — a raising callback would stall the connection's read loop."""
        import nats

        errors: list[Any] = []

        def listener_ext(api: Any) -> None:
            async def _on_err(payload: Any) -> None:
                errors.append(payload)

            api.on("ext:nats_bus:inbound_error", _on_err)

        session = _make_session(
            bus_available=True,
            extensions_config=_config(nats_url=real_nats_url),
            extensions=[listener_ext],
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()

        publisher = await nats.connect(real_nats_url)
        try:
            # A bare dict — the shape the OLD implementation published. It must
            # be rejected as an event: not accepted, and not raised.
            await publisher.publish(_INBOUND, json.dumps({"text": "top level"}).encode())
            await publisher.publish(_INBOUND, b"{not json")

            deadline = time.time() + 5
            while len(errors) < 2 and time.time() < deadline:
                await asyncio.sleep(0.05)

            assert len(errors) == 2
            # The subscription survived both.
            assert session._loaded_extensions
        finally:
            await publisher.close()
            await session.emit_session_shutdown()

    async def _drive_move_to(self, real_nats_url, ack_payload: dict[str, Any]):
        """Run one ``move_to`` against a stand-in body node acking ``ack_payload``.

        Returns ``(published_envelope, tool_results)``. The stand-in mirrors
        ``world/body_node.py``: it answers on ``events.journal.move_to.<bid>``
        with the *inbound* binding_id, which is the whole reason binding_id is
        preserved across hops rather than minted per publish.
        """
        import nats

        session = _make_session(
            bus_available=True,
            extensions_config=_config(nats_url=real_nats_url, ack_timeout_s=5, verbs=("move_to",)),
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()

        body_node = await nats.connect(real_nats_url)
        received: list[Any] = []

        async def _cb(msg):
            received.append(msg)
            event = json.loads(msg.data.decode("utf-8"))
            bid = event["binding_id"]
            subject = f"events.journal.move_to.{bid}"
            await body_node.publish(subject, _envelope(subject, dict(ack_payload), bid))

        sub = await body_node.subscribe("events.workspace.demo.out.move_to", cb=_cb)
        await body_node.flush()  # SUB must reach the server before tau publishes
        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_calling("move_to", {"x": 4, "y": 3}),
            ):
                messages = await session.prompt("go to the far corner")
            assert len(received) == 1
            return json.loads(received[0].data.decode("utf-8")), _tool_results(messages)
        finally:
            await sub.unsubscribe()
            await body_node.close()
            await session.emit_session_shutdown()

    async def test_move_to_publishes_coordinates_and_takes_the_body_nodes_ok(self, real_nats_url):
        """The payload IS the verb's arguments plus ``agent`` — one projection
        for every verb.

        Shape confirmed against a running headless engine before this was
        written: the body node reads the verb args straight off ``payload`` and
        acked ``{"status":"ok","trigger":"DONE","world_tick":383}`` while the
        courier actually moved.
        """
        sent, results = await self._drive_move_to(
            real_nats_url,
            {
                "status": "ok",
                "verb": "move_to",
                "trigger": "DONE",
                "world_tick": 383,
                "ok": True,
                "error": None,
            },
        )

        assert sent["event_type"] == "events.workspace.demo.out.move_to"
        assert sent["payload"] == {"x": 4, "y": 3, "agent": "demo"}
        # The speech-shaped payload is gone, not merely unused.
        assert "text" not in sent["payload"]

        assert len(results) == 1
        assert results[0]["is_error"] is False
        # move_to is not terminal — walking somewhere and then saying something
        # about it is one coherent turn.
        assert nats_bus.VERBS["move_to"].terminal is False
        assert "world_tick" in json.dumps(results[0])

    async def test_a_blocked_move_fails_the_tool_on_status_alone(self, real_nats_url):
        """The optimistic-direction silent failure, asserted end to end.

        The ack here carries ``status: "refused"`` and **no** ``ok``/``error``
        key — deliberately, so this proves τ reads the body node's own dialect
        rather than relying on McRogueFace also emitting tectum's markers. Before
        ``_ack_failure`` learned ``status`` this returned a SUCCESSFUL tool
        result and the model was told the robot had arrived.
        """
        _, results = await self._drive_move_to(
            real_nats_url,
            {"status": "refused", "verb": "move_to", "trigger": "BLOCKED", "world_tick": 400},
        )

        assert len(results) == 1
        assert results[0]["is_error"] is True
        rendered = json.dumps(results[0])
        assert "refused" in rendered
        # WHICH refusal: BLOCKED (it tried and the terrain stopped it) vs an
        # immediate busy refusal, which carries trigger: null.
        assert "BLOCKED" in rendered


class TestTau002ZeroOrphans:
    """H9/T8: subscribe-first → publish → await the ack, or say so loudly."""

    async def test_no_ack_within_timeout_raises(self, real_nats_url):
        """Nothing subscribes the outbound subject, so nothing acks."""
        session = _make_session(
            bus_available=True,
            extensions_config=_config(nats_url=real_nats_url, ack_timeout_s=1),
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()
        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_calling("speak", {"text": "into the void"}),
            ):
                messages = await session.prompt("speak")
            results = _tool_results(messages)
            assert len(results) == 1
            assert results[0]["is_error"] is True
            assert "no ack" in results[0]["content"][0]["text"]
        finally:
            await session.emit_session_shutdown()

    @pytest.mark.parametrize(
        "ack_payload, expected",
        [
            ({"ok": False, "error": None}, "ok=False"),
            ({"ok": True, "error": "tts backend died"}, "tts backend died"),
        ],
    )
    async def test_failing_ack_raises(self, real_nats_url, ack_payload, expected):
        """There is no ``status`` field on a tectum ack; failure is ``ok: False``
        or a non-null ``error``, which is what the effectors actually set."""
        import nats

        session = _make_session(
            bus_available=True,
            extensions_config=_config(nats_url=real_nats_url, ack_timeout_s=5),
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()
        node = await nats.connect(real_nats_url)

        async def _cb(msg):
            event = json.loads(msg.data.decode("utf-8"))
            ack = f"events.action.speech.completed.{event['binding_id']}"
            await node.publish(ack, _envelope(ack, ack_payload, event["binding_id"]))

        sub = await node.subscribe("events.workspace.demo.out.speak", cb=_cb)
        await node.flush()  # SUB must reach the server before tau publishes
        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_calling("speak", {"text": "try me"}),
            ):
                messages = await session.prompt("speak")
            results = _tool_results(messages)
            assert results[0]["is_error"] is True
            assert expected in results[0]["content"][0]["text"]
        finally:
            await sub.unsubscribe()
            await node.close()
            await session.emit_session_shutdown()

    async def test_ack_with_no_failure_markers_is_success(self, real_nats_url):
        """journal_append's real ack carries only ``doc_id`` — no ``ok``, no
        ``error``. Absence of failure markers is success, or every journal write
        would report as failed."""
        import nats

        session = _make_session(
            bus_available=True,
            extensions_config=_config(
                nats_url=real_nats_url, ack_timeout_s=5, verbs=("journal_append",)
            ),
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()
        node = await nats.connect(real_nats_url)

        async def _cb(msg):
            event = json.loads(msg.data.decode("utf-8"))
            # subjects.journal_ack("append", bid) — the kind is NOT the verb
            ack = f"events.journal.append.{event['binding_id']}"
            await node.publish(ack, _envelope(ack, {"doc_id": 1234}, event["binding_id"]))

        sub = await node.subscribe("events.workspace.demo.out.journal_append", cb=_cb)
        await node.flush()  # SUB must reach the server before tau publishes
        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_calling("journal_append", {"text": "a note"}),
            ):
                messages = await session.prompt("write it down")
            results = _tool_results(messages)
            assert results[0]["is_error"] is False
            assert '"doc_id": 1234' in results[0]["content"][0]["text"]
        finally:
            await sub.unsubscribe()
            await node.close()
            await session.emit_session_shutdown()

    async def test_fire_and_forget_verb_returns_without_an_ack(self, real_nats_url):
        """``delegate``'s consumer acks nothing (``VERB_ACK_SUBJECTS`` maps it to
        ``None``), so the tool returns once the publish lands. Not a fallback —
        the contract for verbs no effector acks."""
        import nats

        session = _make_session(
            bus_available=True,
            extensions_config=_config(nats_url=real_nats_url, ack_timeout_s=1, verbs=("delegate",)),
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()
        node = await nats.connect(real_nats_url)
        seen: list[Any] = []

        async def _cb(msg):
            seen.append(msg)

        sub = await node.subscribe("events.workspace.demo.out.delegate", cb=_cb)
        await node.flush()  # SUB must reach the server before tau publishes
        try:
            with patch(
                "tau_agent_core.agent_loop.stream_simple",
                side_effect=_fake_stream_calling("delegate", {"text": "think about this"}),
            ):
                messages = await session.prompt("delegate")
            results = _tool_results(messages)
            # ack_timeout_s is 1s and nothing acked; success here proves the
            # call did not wait for one.
            assert results[0]["is_error"] is False
            assert "acks nothing" in results[0]["content"][0]["text"]
            deadline = time.time() + 3
            while not seen and time.time() < deadline:
                await asyncio.sleep(0.05)
            assert seen, "the event was never published"
        finally:
            await sub.unsubscribe()
            await node.close()
            await session.emit_session_shutdown()

    async def test_aborted_signal_stops_the_wait(self, real_nats_url):
        """An aborted turn must not leave the ack wait parked until the timeout."""
        session = _make_session(
            bus_available=True,
            extensions_config=_config(nats_url=real_nats_url, ack_timeout_s=30),
        )
        await session.load_extensions([_EXT_PATH], discover=False)
        await session.emit_session_start()
        try:
            tool = session._registry.get_active_tools()["speak"]
            started = time.time()
            with pytest.raises(asyncio.CancelledError):
                await tool.execute("call_1", {"text": "hi"}, _AbortedSignal(), None, None)
            # Nothing acked and ack_timeout_s is 30s: returning fast is the proof.
            assert time.time() - started < 5
        finally:
            await session.emit_session_shutdown()


class _AbortedSignal:
    def is_aborted(self) -> bool:
        return True
