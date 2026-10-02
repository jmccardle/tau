"""AgentSessionRuntime — the session-lifecycle layer ABOVE AgentSession (H1).

Reference: docs/REMOTE-CONTROL.md §4[6] (block [6] "Runtime host", H1-H4),
§8 decision 4 ("extracted from the TUI, not added as a method on
AgentSession"), §10 resolved decision 9 ("AgentSessionRuntime lives in
tau-agent-core"), §9 R-T2.

pi separates ``AgentSessionRuntime`` — ``newSession`` / ``fork`` /
``switchSession`` / ``dispose`` / ``setRebindSession`` — from ``AgentSession``,
one conversation (``packages/coding-agent/src/core/agent-session-runtime.ts``).
τ's version is deliberately LIGHTER than pi's, because the two harnesses solve
a different problem at this layer:

pi's ``newSession``/``fork``/``switchSession`` each build a WHOLE NEW
``AgentSession`` (``createRuntime`` reconstructs cwd-bound services, a new
``SessionManager``, a new extension context — see ``agent-session-runtime.ts``
``apply()``) and discard the old one via ``session.dispose()``. τ's design goes
the other way: ``AgentSession.session_log`` is ALREADY a settable property
(``agent_session.py`` — added for exactly this purpose) and ``messages`` is
ALREADY derived from the log at read time, never stored
(``session.cursor.context()``). So τ's
runtime does not rebuild an ``AgentSession`` at all — it gives the EXISTING one
a new head cursor on a new ``SessionLog`` (H3), leaving everything else — model, tools, extensions, the
provider's pooled HTTP client — untouched. "That warmth is the entire reason a
host pools a process instead of respawning" (§4[6]).

**What this module ports from pi, and what it does not:**

- ``new_session`` / ``fork`` / ``switch_session`` / ``dispose`` — ported,
  same names (snake_case), same ``{"cancelled": bool}`` veto contract (H2).
- ``set_rebind_session`` — ported. A caller-supplied callback invoked after
  every successful (non-cancelled) swap, so consumer-specific rebinding (the
  TUI's seam-3 event bridge, its model-name resolver, its renderer; RPC mode
  currently needs no such step) stays IN the consumer rather than becoming a
  case this module has to know about. Mirrors pi's ``rebindSession`` exactly.
- pi's ``services`` / ``AgentSessionServices`` (cwd-bound tool/service
  rebuilding) — NOT ported. τ's tools and extensions are session-scoped and
  stay warm across a swap (H3); there is no per-cwd service bundle to
  reconstruct because nothing about "the services" changes when the log does.
- pi's ``diagnostics`` / ``modelFallbackMessage`` — NOT ported. Both report on
  MODEL resolution during ``createRuntime``; this runtime never resolves a
  model (the model is fixed for its whole lifetime — swapping it is a
  different, TUI/backend-level operation, unrelated to session lifecycle).
- pi's ``setBeforeSessionInvalidate`` — NOT ported. It exists so host UI can
  detach components before the OLD session's ``ExtensionContext`` goes stale —
  a real hazard in pi, where a swap replaces the extension context along with
  everything else. τ's swap is in-place: the SAME ``AgentSession``, the SAME
  ``ExtensionRunner``, the SAME ``ExtensionContext`` survive every swap, so
  there is no stale-context moment for this callback to guard.
- pi's ``importFromJsonl`` — NOT ported. A ``--import-session`` CLI feature
  already implemented at the CLI layer (``cli.py``'s ``_run_import_session``),
  unrelated to switching the LIVE session mid-run; not one of the three verbs
  phase 3 asks for.
- pi's per-operation ``SessionManager``/cwd/trust-context plumbing — NOT
  ported. τ's ``fork`` copies the path to the session's cursor
  (``SessionCatalog.fork(source, cwd, at=leaf)``).

**H2 — the veto hook.** Every operation dispatches
``AgentSession.extension_runner``'s ``session_before_switch`` hook (an
``ExtensionRunner`` mutating hook, exactly the mechanism ``tool_call``/
``input``/etc. already use — see ``extensions/runner.py``) BEFORE touching
anything. A handler returning ``{"cancel": True}`` short-circuits: the method
returns ``{"cancelled": True}`` and the session is left completely alone —
no abort signalled, no lock touched, no log swapped. This is pi's own
ordering (``emitBeforeSwitch``/``emitBeforeFork`` run before
``teardownCurrent()``).

**H3 — what a swap replaces.** The head's cursor: assigning the new
``session_log`` opens a fresh cursor at its default leaf, and every per-turn
field lives on a cursor (docs/CURSORS.md §2), so the new one starts with no
usage, no queues, no deferred ops and no rollback target. ``side_usage`` is reset
to zero as well, because it is session-wide.

The old cursor is not cleared (§8). If it still holds an accepted message, or a
submission is waiting on its lock, :meth:`AgentSession.deliver_queued` runs it
there as background turns with no head attached, then closes it; otherwise it
is closed at once. Nothing accepted is discarded by a swap.

**Anything else survives on purpose** — system prompt, model, tools,
extensions and their registrations, the provider's pooled HTTP client, the
``EventBus`` and every subscription on it (including ``RPCHandler``'s own — see
H4 below), ``_turn_token_counter`` (resetting it would break the staleness
detection ``rollback`` depends on), and the ``CompactionPolicy``/
``_policy_turns_used`` pair. That warmth is the entire reason a host pools a
process instead of respawning.

**H4 — atomicity with respect to the event stream.** ``AgentSession``'s
``EventBus`` is fire-and-forget in the sense that an emitter does not care
whether a subscriber SUCCEEDS (``events.py``'s own module docstring), but
``EventBus.emit()`` itself is NOT "schedule a task and move on" — it calls
every subscriber SYNCHRONOUSLY and awaits any coroutine result before moving
to the next one (``events.py:262-291``). Combined with the fact that
``RPCHandler`` subscribes exactly ONCE, for its whole lifetime
(``RPCHandler.__init__``), the guarantee this module needs falls out of one
observation: the turn machinery releases :attr:`AgentSession.turn_lock` only
in a ``finally`` that runs strictly AFTER ``AgentLoop.run()``'s own await has
returned — which is strictly after every ``AgentEvent`` that turn emitted has
already been synchronously dispatched to ``RPCHandler._forward_event`` and
``put_nowait``'d onto the outbound queue.

So: :meth:`_apply_swap` aborts the head's cursor if it is busy (a request,
not a guarantee — see ``AgentSession.abort``'s own docstring) and then awaits
that cursor's ``turn_lock``, BOUNDED by :data:`DEFAULT_SWAP_TIMEOUT_S`
(phase-3 review Finding 1 — see that constant's own docstring for the
timeout value and why an unbounded wait here is a defect, not a design: the
RPC reader is strictly serial, so a handler that blocks forever wedges every
later request behind it, including ``abort`` itself). Two outcomes:

- **Timed out**: the turn did not free the lock in time. Nothing has been
  touched — no new cursor, no log swap — so this returns
  ``{"cancelled": False, "blocked": True, "reason": <str>}`` rather than
  raising, the same "a refusal is a result, not an exception" shape
  ``SubmissionResult.rejection_reason`` already uses one layer down
  (``submission.py``). A caller that talks JSON-RPC never sees this dict
  directly — ``commands._lifecycle_result`` turns a ``blocked`` outcome into
  ``RPCError(TURN_STILL_RUNNING, ...)`` — but this module itself has no
  JSON-RPC in it (a caller like the TUI reads the dict as data, matching
  its treatment of ``cancelled``).
- **Acquired**: either no turn was running (the acquire was uncontended) or
  a turn WAS running and has now fully unwound, persisted, and finished
  emitting — its last event is already sitting in the output queue, strictly
  BEFORE this call's own response can be enqueued (Python's single-threaded
  cooperative scheduling: nothing else runs between ``turn_lock.acquire()``
  returning and this method's own synchronous reset-and-swap code, because
  there is no ``await`` in between). The output queue is FIFO (T6) and the
  writer drains it in insertion order, so "enqueued first" means "written to
  the wire first" — which is the whole guarantee: no event from the OLD
  session can arrive AFTER this call's response, because any such event was
  necessarily enqueued BEFORE it. Holding the SAME lock through the
  reset-and-swap additionally prevents a brand new turn from being ADMITTED
  mid-swap (every ``multitask_strategy`` in ``AgentSession.submit`` either
  checks ``turn_lock.locked()`` or awaits the same lock), so nothing can
  start writing to the log this call is about to orphan.

``test_agent_session_runtime.py`` pins this with a real gated provider
(mirrors ``test_submit_admission.py``'s pattern): a turn is left mid-flight,
``new_session`` is invoked concurrently, and the test asserts every event the
in-flight turn emits reaches the subscriber's record BEFORE the swap's own
result is observed to complete — plus a second gated-provider test for the
timed-out path, and ``test_rpc_conformance.py`` reproduces Finding 1's wire
trace end to end against a real subprocess (a fake provider that never
responds, driving ``new_session``/``get_state``/``abort`` in a pipeline and
asserting all three still answer).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from tau_llm.docs import agent_facing

from tau_agent_core.session_catalog import ConversationSession, SessionCatalog
from tau_agent_core.usage import zero_usage

if TYPE_CHECKING:
    from tau_agent_core.agent_session import AgentSession

DEFAULT_SWAP_TIMEOUT_S = 5.0

RebindCallback = Callable[["AgentSession"], "Awaitable[None] | None"]


class AgentSessionRuntime:
    """The session-lifecycle layer above ONE ``AgentSession`` (H1).

    Public surface (audited by ``test_rpc_capability_audit.py``, R-T2):
    :meth:`new_session`, :meth:`fork`, :meth:`switch_session`,
    :meth:`dispose`, :meth:`set_rebind_session`. See the module docstring for
    the full H1-H4 design and what was deliberately left out of pi's shape.

    Bound to exactly one ``AgentSession`` for its whole life — "warm" state
    (model, tools, extensions, provider client) belongs to THAT object and
    outlives every swap this runtime performs. A caller whose model/tools
    change (the TUI swapping backends on a model change) constructs a NEW
    ``AgentSessionRuntime`` alongside the new ``AgentSession``, exactly as it
    already constructs a new backend — this runtime is not the seam for that
    (Decision 4: session lifecycle, not backend/model reconfiguration).
    """

    def __init__(
        self,
        agent_session: "AgentSession",
        session_catalog: SessionCatalog,
        cwd: str,
        model: str,
        backend: str,
        store: str,
        *,
        swap_timeout_s: float = DEFAULT_SWAP_TIMEOUT_S,
    ) -> None:
        """
        Args:
            agent_session: The session this runtime resets/rebinds in place.
            session_catalog: The storage-agnostic construction seam (W10,
                ``session_catalog.py``) every operation below uses to build/
                load/fork a ``ConversationSession`` — the SAME seam
                ``tau_coding_agent.app``/``headless.py`` already use, injected
                here instead of hardcoded so a second store (JMFTS) needs no
                change to this class.
            cwd: Passed to every catalog call unchanged (matches
                ``os.getcwd()`` on the TUI's own call sites; a fixed value for
                the process's whole life on the RPC path).
            model: Opaque metadata string stored on a NEW session's header by
                ``session_catalog.create``/``create_ephemeral`` — this class
                never interprets it (it is not the ``tau_llm.types.Model``
                the loop actually uses, which lives on ``agent_session``
                already and is untouched by this class).
            backend: Same opaque-metadata treatment as ``model`` (the
                provider-backend config key, e.g. ``"openai"``).
            store: The catalog's storage-backend label (``"file"``/
                ``"jmfts"``) — carried only so a caller building the F2 wire
                tuple (``{store, session_id, lane, cursor}``,
                docs/REMOTE-CONTROL.md §7.2 F2) has it without reaching into
                ``session_catalog``'s concrete type. Purely descriptive; this
                class does no behaviour on it.
            swap_timeout_s: Overrides :data:`DEFAULT_SWAP_TIMEOUT_S` — see
                that constant's docstring for what it bounds and why. A
                keyword-only override exists so tests exercising the
                timed-out path do not have to sleep out the real default.
        """
        self._session = agent_session
        self._catalog = session_catalog
        self._cwd = cwd
        self._model = model
        self._backend = backend
        self._store = store
        self._swap_timeout_s = swap_timeout_s
        self._rebind: RebindCallback | None = None

    @property
    @agent_facing(topic="sessions")
    def catalog(self) -> "SessionCatalog":
        """The catalog this runtime creates, forks and resolves sessions through.

        The read behind ``list_sessions`` and the ``session_id`` domain. It is the
        runtime's and not the session's because a session knows only itself: which
        OTHER sessions exist is a question about the store.
        """
        return self._catalog

    @property
    @agent_facing(topic="sessions")
    def cwd(self) -> str:
        """The working directory sessions are listed and resolved against.

        Every catalog call in this class passes it, and so must every caller that
        lists sessions or completes a path — a listing taken against a different
        directory is a different listing, not a slightly stale one.
        """
        return self._cwd

    @property
    @agent_facing(topic="sessions")
    def store(self) -> str:
        """The catalog's storage-backend label, the first field of a session ref.

        Purely descriptive — this class never branches on it. Published because a
        caller building a ref (``{store, session_id, lane, cursor}``) needs the same
        label this runtime stamps, not one it guessed.
        """
        return self._store

    def set_rebind_session(self, callback: RebindCallback | None) -> None:
        """Install (or clear, with ``None``) the post-swap rebind callback.

        See :data:`RebindCallback` for the exact contract (when it runs,
        relative to the lock and the swap).
        """
        self._rebind = callback

    async def new_session(
        self, *, persist: bool, system_prompt: str | None = None
    ) -> dict[str, Any]:
        """Start a fresh conversation on the SAME ``AgentSession`` (H1-H4).

        Args:
            persist: Required, not defaulted (Fail-Early: the caller states
                what it wants rather than inheriting a guess). ``True`` calls
                ``session_catalog.create`` (a real, on-disk-addressable
                session — the TUI's ``action_new_chat``/``action_clear_chat``
                convention); ``False`` calls ``create_ephemeral`` (RPC mode's
                "every run starts a fresh, unpersisted session" convention,
                unchanged by this phase).
            system_prompt: Seeded into the new log as a ``system`` entry
                (the catalog's own convention) — ``None`` writes nothing,
                which is correct for a caller whose ``AgentSession`` already
                carries the prompt on ``_system_prompt`` and injects it at
                completion time regardless (``agent_loop.py:684-693``); a
                caller that wants the prompt VISIBLE in the persisted
                transcript (the TUI's display convention) passes it.

        Returns:
            ``{"cancelled": True}`` if a ``session_before_switch`` handler
            vetoed (H2); ``{"cancelled": False, "blocked": True, "reason":
            str}`` if an in-flight turn did not stop within
            :data:`DEFAULT_SWAP_TIMEOUT_S` of ``abort()`` (Finding 1 — see
            the module docstring's H4 section); otherwise
            ``{"cancelled": False, "session": <the new ConversationSession>,
            "session_id": str, "cursor": str | None, "store": str}``.
        """

        def _build() -> ConversationSession:
            if persist:
                return self._catalog.create(
                    self._cwd, self._model, self._backend, system_prompt=system_prompt
                )
            return self._catalog.create_ephemeral(
                self._cwd, self._model, self._backend, system_prompt=system_prompt
            )

        return await self._apply_swap(_build, reason="new", target=None)

    async def fork(self) -> dict[str, Any]:
        """Copy the path to this session's cursor into a new session, and move onto it.

        The source session is untouched (``SessionCatalog.fork``'s own
        contract) — only this runtime's ``AgentSession`` moves forward onto
        the fork. The snapshot is taken AFTER the turn-lock wait (see the
        module docstring's H4 section), so a fork never captures a
        half-written turn.

        Raises:
            RuntimeError: the current ``session_log`` does not satisfy
                ``ConversationSession`` (e.g. a bare ``InMemorySessionLog``
                that was never bound through a catalog) — forking needs a
                session the catalog can address; Fail-Early rather than a
                confusing ``AttributeError`` three calls deep.

        Returns:
            Same shape as :meth:`new_session`.
        """

        def _build() -> ConversationSession:
            current = self._session.session_log
            if not isinstance(current, ConversationSession):
                raise RuntimeError(
                    "AgentSessionRuntime.fork(): the current session_log does not "
                    f"implement ConversationSession (got {type(current).__name__}). "
                    "fork() needs a session the catalog can address; a bare SessionLog "
                    "was never bound through this runtime's catalog."
                )
            return self._catalog.fork(current, self._cwd, at=self._session.cursor.leaf)

        return await self._apply_swap(_build, reason="fork", target=None)

    async def switch_session(self, session_id: str) -> dict[str, Any]:
        """Load a different, already-addressable session and move this
        runtime's ``AgentSession`` onto it (H1-H4).

        ``session_id`` resolves via ``session_catalog.resolve_ref`` — exact
        id match, else a unique id prefix, scoped to this runtime's ``cwd``
        (the same resolution ``--session REF`` uses headlessly). Resolved
        BEFORE the veto hook fires and before anything is touched: a bad id
        raises ``LookupError`` with the CURRENT session completely
        undisturbed — no abort signalled, no turn interrupted for an
        operation that was never going to succeed.

        Returns:
            Same shape as :meth:`new_session`.
        """
        resolved = self._catalog.resolve_ref(session_id, cwd=self._cwd)
        return await self._apply_swap(lambda: resolved, reason="resume", target=session_id)

    async def dispose(self) -> None:
        """Tear down this runtime's session (pi's ``dispose()``).

        Fires ``session_shutdown`` (reason ``"quit"``) — the SAME lifecycle
        hook a normal process exit already fires
        (``rpc_mode.py``'s ``finally``, the TUI's on-quit path) — so a caller
        that owns this runtime for a whole process life can route its
        shutdown through here uniformly instead of reaching past it to
        ``session.emit_session_shutdown`` directly. Does not swap the log or
        touch the reset set: the process is ending, there is no "next
        conversation" to reset state FOR.
        """
        await self._session.emit_session_shutdown("quit")

    async def _apply_swap(
        self,
        build_log: Callable[[], ConversationSession],
        *,
        reason: str,
        target: str | None,
    ) -> dict[str, Any]:
        """The shared critical section behind all three verbs — H2 veto,
        H3 cursor replacement, H4 atomicity, and the Finding-1 bounded wait. See the
        module docstring for the reasoning; this is deliberately the ONE
        place that reasoning is implemented.
        """
        session = self._session
        runner = session.extension_runner
        if runner.has_handlers("session_before_switch"):
            if await runner.emit_session_before_switch(reason, target):
                return {"cancelled": True}

        old = session.cursor
        if old.busy:
            session.abort(old)
        try:
            await asyncio.wait_for(old.turn_lock.acquire(), timeout=self._swap_timeout_s)
        except asyncio.TimeoutError:
            return {
                "cancelled": False,
                "blocked": True,
                "reason": (
                    f"a turn is in flight and did not stop within "
                    f"{self._swap_timeout_s:g}s of abort() — retry, or wait "
                    "for agent_end before retrying"
                ),
            }
        try:
            new_log = build_log()
            session._side_usage = zero_usage()
            session.session_log = new_log
        finally:
            old.turn_lock.release()

        if old.has_queued or old.busy:
            session.deliver_queued(old)
        else:
            await session.close_cursor(old)

        if self._rebind is not None:
            maybe_awaitable = self._rebind(session)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable

        return {
            "cancelled": False,
            "session": new_log,
            "session_id": new_log.id,
            "cursor_id": session.cursor.id,
            "cursor": session.cursor.leaf,
            "store": self._store,
        }
