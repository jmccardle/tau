"""``ExtensionAPI.submit`` — an extension originates a turn AS ITSELF (phase 5).

docs/SUBMISSION-LIFECYCLE.md "The extension API". Two things are pinned here:

1. **Provenance stops lying.** Before this, ``ctx.prompt()`` delegated to
   ``AgentSession.prompt()`` — the *interactive* compatibility wrapper — so every
   turn an extension originated emitted lifecycle events stamped
   ``source="interactive"``, ``submitter="human"``. The one non-human input source
   that exists (``nats_bus``) was therefore indistinguishable from a person typing,
   which is exactly the distinction phase 2 added those fields to make.
2. **The stamp is unforgeable.** ``source``/``submitter`` are supplied BY THE
   BINDING from the caller's own runner bucket and are not parameters — the same
   discipline ``ExtensionAPI.emit`` has for ``ext:<name>:<topic>`` channels
   (test_inter_extension_channels.py). An extension cannot claim to be a human,
   or to be another extension.

The event model's own round-trip contract lives in test_events.py; ``submit()``'s
admission/strategy semantics in test_submit_admission.py; this file is the
EXTENSION-side binding only.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import UNATTRIBUTED_EXTENSION, ExtensionAPI
from tau_agent_core.session_log import InMemorySessionLog


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


def _session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model())


def _bound(session: AgentSession, path: str) -> ExtensionAPI:
    """The api a loaded extension at ``path`` is handed — the real binding path."""
    return session._bind_extension_api(path)


@pytest.mark.usefixtures("fake_llm")
class TestSubmitStampsTheExtensionsOwnIdentity:
    async def test_events_carry_source_extension_and_the_extensions_name(self):
        session = _session()
        api = _bound(session, "/x/bus_driver.py")
        seen = []
        session.subscribe(seen.append)

        result = await api.submit("wake up")

        assert result.accepted is True
        assert seen, "the fake_llm turn must have emitted at least one event"
        for event in seen:
            assert event.source == "extension"
            assert event.submitter == "bus_driver"
            assert event.submission_id == result.submission_id
        # The defect this fixes, stated as an assertion.
        assert not any(e.source == "interactive" for e in seen)
        assert not any(e.submitter == "human" for e in seen)

    async def test_each_extension_submits_under_its_own_name(self):
        """Two extensions on one session: neither wears the other's name."""
        session = _session()
        first = _bound(session, "/x/alpha.py")
        second = _bound(session, "/x/beta.py")
        seen = []
        session.subscribe(seen.append)

        alpha_id = (await first.submit("one")).submission_id
        beta_id = (await second.submit("two")).submission_id

        alpha_events = [e for e in seen if e.submission_id == alpha_id]
        beta_events = [e for e in seen if e.submission_id == beta_id]
        assert alpha_events and beta_events
        assert all(e.submitter == "alpha" for e in alpha_events)
        assert all(e.submitter == "beta" for e in beta_events)

    async def test_correlation_rides_onto_every_event(self):
        """The spec names exactly this use: bus subject + binding id, so a
        renderer can fan a turn out to the consumer that caused it."""
        session = _session()
        api = _bound(session, "/x/nats_bus.py")
        seen = []
        session.subscribe(seen.append)

        correlation = {"subject": "events.sensation.audio.resolved.clean", "binding_id": "flow-42"}
        await api.submit("hi", correlation=correlation)

        assert seen
        assert all(e.correlation == correlation for e in seen)

    async def test_correlation_with_a_live_object_raises_here_not_downstream(self):
        """Decision 4: the traceback names the culprit at construction, rather
        than detonating in a JSON renderer three hops away."""
        session = _session()
        api = _bound(session, "/x/nats_bus.py")

        with pytest.raises(ValueError, match="correlation"):
            await api.submit("hi", correlation={"msg": object()})

    async def test_reject_is_the_default_and_refuses_with_a_reason(self):
        """A refusal is a RESULT, not an exception and not a silent drop."""
        session = _session()
        api = _bound(session, "/x/nats_bus.py")

        await session._turn_lock.acquire()
        try:
            result = await api.submit("while busy")
        finally:
            session._turn_lock.release()

        assert result.accepted is False
        assert result.rejection_reason and "in flight" in result.rejection_reason
        assert result.messages == []

    async def test_enqueue_waits_for_the_in_flight_turn(self):
        session = _session()
        api = _bound(session, "/x/nats_bus.py")

        await session._turn_lock.acquire()
        task = asyncio.create_task(api.submit("after you", multitask_strategy="enqueue"))
        await asyncio.sleep(0.05)
        assert not task.done(), "enqueue must wait, not run alongside the in-flight turn"
        session._turn_lock.release()

        result = await task
        assert result.accepted is True
        assert result.messages


class TestTheStampIsUnforgeable:
    def test_source_and_submitter_are_not_parameters(self):
        params = inspect.signature(ExtensionAPI.submit).parameters
        assert "source" not in params
        assert "submitter" not in params
        # Nor is expand_commands: injected text must not be able to smuggle a
        # "/compact" through a bus payload (pi's expandPromptTemplates: false).
        assert "expand_commands" not in params

    @pytest.mark.usefixtures("fake_llm")
    async def test_an_extension_cannot_claim_to_be_a_human(self):
        session = _session()
        api = _bound(session, "/x/impostor.py")

        with pytest.raises(TypeError):
            api.submit("hi", source="interactive")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            api.submit("hi", submitter="human")  # type: ignore[call-arg]

        seen = []
        session.subscribe(seen.append)
        await api.submit("hi")
        assert all(e.source == "extension" and e.submitter == "impostor" for e in seen)

    async def test_an_api_with_no_bucket_has_no_identity_to_submit_under(self):
        """Fail-Early, same as ``emit``: no anonymous submissions."""
        api = ExtensionAPI(session=_session())
        with pytest.raises(RuntimeError, match="api.submit"):
            await api.submit("hi")

    async def test_an_api_with_no_session_raises_rather_than_no_opping(self):
        api = ExtensionAPI()
        api._hook_handlers = _session()._extension_runner.register_extension("/x/lonely.py")
        with pytest.raises(RuntimeError, match="not bound to an AgentSession"):
            await api.submit("hi")


@pytest.mark.usefixtures("fake_llm")
class TestSubmitThreadsafeIsTheSameDoorFromAnotherThread:
    """The extension counterpart to :meth:`AgentSession.submit_threadsafe`.

    docs/SUBMISSION-LIFECYCLE.md "Task marshalling". The driver an extension
    writes is not always a coroutine on this loop — ``paho-mqtt``, ``watchdog``
    and a WSGI handler all deliver on threads of their own — and those have no
    loop to ``await`` on, so the extension surface needs the synchronous door
    too. (``nats_bus`` does NOT: its callback is awaited by a task ``nats-py``
    creates on the loop that called ``subscribe()``, which is the session's. See
    the note in ``extensions_builtin/nats_bus.py``.)

    The core's own detection/refusal behaviour is pinned in
    test_submit_threadsafe.py; this is the BINDING — that the marshalled
    submission is still stamped with, and can still only be stamped with, this
    extension's own identity.
    """

    async def test_a_thread_marshals_a_turn_stamped_with_the_extensions_name(self):
        session = _session()
        api = _bound(session, "/x/mqtt_driver.py")
        seen = []
        session.subscribe(seen.append)

        future = await asyncio.to_thread(lambda: api.submit_threadsafe("from a device"))
        result = await asyncio.wrap_future(future)

        assert result.accepted is True
        assert seen, "the marshalled turn must have run and emitted events"
        for event in seen:
            assert event.source == "extension"
            assert event.submitter == "mqtt_driver"
            assert event.submission_id == result.submission_id

    def test_the_stamp_is_as_unforgeable_here_as_on_submit(self):
        params = inspect.signature(ExtensionAPI.submit_threadsafe).parameters
        assert "source" not in params
        assert "submitter" not in params
        assert "expand_commands" not in params

    async def test_an_api_with_no_bucket_or_no_session_raises(self):
        session = _session()
        no_bucket = ExtensionAPI(session=session)
        with pytest.raises(RuntimeError, match="api.submit_threadsafe"):
            no_bucket.submit_threadsafe("hi")

        no_session = ExtensionAPI()
        no_session._hook_handlers = session._extension_runner.register_extension("/x/lonely.py")
        with pytest.raises(RuntimeError, match="not bound to an AgentSession"):
            no_session.submit_threadsafe("hi")


@pytest.mark.usefixtures("fake_llm")
class TestCtxPromptIsADeprecatedAlias:
    def _ctx(self, session: AgentSession):
        # The ONE shared ExtensionContext every loaded extension's api receives.
        return session._extension_api.context

    async def test_still_returns_this_turns_messages(self):
        session = _session()

        messages = await self._ctx(session).prompt("hello")

        assert isinstance(messages, list)
        assert messages
        assert any(m.get("role") == "user" for m in messages)
        assert any(m.get("role") == "assistant" for m in messages)

    async def test_still_enqueues_rather_than_rejecting(self):
        """Behaviour preserved: a second event source's coroutine waits for the
        in-flight turn and then runs, exactly as before."""
        session = _session()
        ctx = self._ctx(session)

        await session._turn_lock.acquire()
        task = asyncio.create_task(ctx.prompt("after you"))
        await asyncio.sleep(0.05)
        assert not task.done(), "ctx.prompt must still enqueue, not reject"
        session._turn_lock.release()

        messages = await task
        assert messages

    async def test_reports_extension_not_a_human_at_a_frontend(self):
        session = _session()
        seen = []
        session.subscribe(seen.append)

        await self._ctx(session).prompt("from an extension")

        assert seen
        assert all(e.source == "extension" for e in seen)
        # The shared context carries no per-extension identity, so it says so
        # instead of guessing — see UNATTRIBUTED_EXTENSION.
        assert all(e.submitter == UNATTRIBUTED_EXTENSION for e in seen)
        assert "<" in UNATTRIBUTED_EXTENSION, "must be unmistakably not an extension stem"
