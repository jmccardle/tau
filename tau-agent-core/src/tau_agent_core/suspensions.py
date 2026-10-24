"""A session's suspensions: a turn that waits, in-process, for a responder's answer.

docs/STEP-DEBUGGER.md §3. ``api.suspend`` validates its spec here, checks its
site against the dispatch mark (:mod:`tau_agent_core.suspension`), and hands
the wait to the session's :class:`SuspensionBroker`, which writes the durable
side entry, queues per cursor, emits ``suspension_open`` and
``suspension_closed``, and resolves on the first valid answer or on the turn's
abort. Milestone 1 is in-process only: nothing here resumes across a process
boundary, though the side entry records the continuation milestone 2 reads.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from tau_llm.abort import AbortSignal
from tau_llm.docs import agent_facing

from tau_agent_core.events import SuspensionCloseReason
from tau_agent_core.extension_types import (
    PANEL_BODY_KINDS,
    _validate_panel_body,
    validate_form_spec,
    validate_form_values,
)
from tau_agent_core.suspension import _DispatchMark, current_dispatch_mark, dispatch_refusal

if TYPE_CHECKING:
    from tau_agent_core.cursor import Cursor

SUSPENSION_KIND = "suspension"
"""The side entry kind a suspension writes (``session_log.SIDE_KINDS``)."""

SUSPENSION_SITES: tuple[str, ...] = ("tool_call", "entry_written", "turn_end", "user_turn_end")
"""The hooks ``api.suspend`` may be called from, directly or through an ``ext:`` handler (§3.2)."""

ABORT_ACTION = "abort"
"""The action label that also trips the turn's abort signal (§3.1)."""

RESUMED_SUSPENSION: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "tau_resumed_suspension", default=None
)
"""The suspension a resume is re-dispatching, ``{id, site, extension, resolution}`` (§3.7.3).

Read by ``api.resumed``. Milestone 1 has no resume from the tree, so nothing sets it.
"""

PendingScope = Literal["cursor", "tree", "session"]
"""Which suspensions :meth:`SuspensionBroker.pending` lists: one cursor's, a cursor and
every cursor it owns, or the whole session's."""


@agent_facing(topic="extensions", since="0.15.0")
class SuspensionRefused(RuntimeError):
    """``api.suspend`` was called where a suspension may not be raised (§3.2, §3.9).

    Not a site (a notify event, a tool, ``input``, ``tool_result``,
    ``annotate``, a nested tool call, a task a hook spawned), or a turn that
    cannot be stepped (no turn, ``store_history`` off, a read-only session).
    An ``Exception``: at ``tool_call`` it blocks the call, elsewhere it is
    surfaced like any handler error.
    """


@agent_facing(topic="extensions", since="0.15.0")
class NoResponderError(SuspensionRefused):
    """Nothing could answer the suspension, so it was not raised (§3.8).

    No live responder is registered with the session, or the submission
    driving the turn did not set ``allow_user_input`` (for a child turn, its
    root turn's submission).
    """


@agent_facing(topic="extensions", since="0.15.0")
def validate_suspension_spec(spec: Any) -> dict[str, Any]:
    """Validate and normalize an ``api.suspend`` spec: the ask spec's shape, plus ``evidence`` and ``targetId``.

    ``title``, the optional body (``text``, ``list`` or ``table``) and
    ``fields`` are the ask spec's (:func:`~tau_agent_core.extension_types.validate_ask_spec`).
    An action is ``{label}`` alone: a suspension's answer is returned to the
    code that raised it, so no action names a command, unlike an ask's.

    Args:
        spec: ``{title?, text|list|table?, fields?, actions, evidence?,
            targetId?}``. ``evidence`` is a list of ``{label, text}`` strings;
            ``targetId`` the id of the entry the suspension is about.

    Returns:
        ``{title, body, fields, actions, evidence, targetId}``; ``body`` and
        ``targetId`` are ``None`` and ``evidence`` ``[]`` when absent.

    Raises:
        ValueError: a malformed body or field (as for an ask), no actions, an
            action that is not exactly ``{label}``, a repeated label, or a
            malformed ``evidence`` or ``targetId``.
    """
    if not isinstance(spec, dict):
        raise ValueError("suspend: spec must be a dict")
    unknown = sorted(
        set(spec) - {"title", *PANEL_BODY_KINDS, "fields", "actions", "evidence", "targetId"}
    )
    if unknown:
        raise ValueError(f"suspend: unknown spec key(s) {unknown}")
    title = spec.get("title", "Suspended")
    if not isinstance(title, str):
        raise ValueError("suspend: spec['title'] must be a string")
    present = [k for k in PANEL_BODY_KINDS if k in spec]
    if len(present) > 1:
        raise ValueError(f"suspend: spec carries {len(present)} bodies {present}; at most one")
    body = _validate_panel_body(present[0], spec[present[0]]) if present else None
    fields: list[dict[str, Any]] = []
    if spec.get("fields") is not None:
        _, fields = validate_form_spec({"title": title, "fields": spec["fields"]})
    raw_actions = spec.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError("suspend: spec['actions'] must be a non-empty list of {label}")
    actions: list[dict[str, str]] = []
    for action in raw_actions:
        if (
            not isinstance(action, dict)
            or set(action) != {"label"}
            or not isinstance(action["label"], str)
            or not action["label"]
        ):
            raise ValueError(
                f"suspend: action {action!r} must be exactly {{label: <non-empty str>}}"
            )
        if any(a["label"] == action["label"] for a in actions):
            raise ValueError(f"suspend: action label {action['label']!r} is repeated")
        actions.append({"label": action["label"]})
    evidence = spec.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("suspend: spec['evidence'] must be a list of {label, text}")
    for item in evidence:
        if (
            not isinstance(item, dict)
            or set(item) != {"label", "text"}
            or not all(isinstance(item[k], str) for k in ("label", "text"))
        ):
            raise ValueError(
                f"suspend: evidence item {item!r} must be exactly {{label, text}} strings"
            )
    target_id = spec.get("targetId")
    if target_id is not None and (not isinstance(target_id, str) or not target_id):
        raise ValueError("suspend: spec['targetId'] must be a non-empty string")
    return {
        "title": title,
        "body": body,
        "fields": fields,
        "actions": actions,
        "evidence": [dict(item) for item in evidence],
        "targetId": target_id,
    }


def site_refusal(mark: _DispatchMark | None) -> str | None:
    """Why the code running now is not a suspension site, or ``None`` when it is (§3.2).

    Args:
        mark: The dispatch mark this context holds.

    Returns:
        The reason, naming the hook, or ``None``.
    """
    refusal = dispatch_refusal(mark)
    if refusal is not None or mark is None:
        return refusal
    if mark.hook not in SUSPENSION_SITES:
        return (
            f"{mark.hook!r} is not a suspension site ({', '.join(SUSPENSION_SITES)}): its "
            "entry is not yet written; carry the hit to the next entry_written"
        )
    if mark.hook == "tool_call" and (mark.event or {}).get("parent_tool_call_id") is not None:
        return (
            "a nested tool call is not a suspension site: its outer call's result is not "
            "in the tree; carry the hit to that result's entry_written"
        )
    return None


def suspension_site() -> _DispatchMark:
    """The dispatch mark of the site the code running now may suspend at.

    Returns:
        The mark.

    Raises:
        SuspensionRefused: this is not a suspension site, naming why.
    """
    mark = current_dispatch_mark()
    refusal = site_refusal(mark)
    if refusal is not None or mark is None:
        raise SuspensionRefused(f"api.suspend refused: {refusal}")
    return mark


@agent_facing(topic="extensions", since="0.15.0")
@dataclass(eq=False)
class Suspension:
    """One raised suspension, open or queued, as the broker holds it.

    Attributes:
        id: The suspension's id, which is its side entry's id.
        cursor: The cursor whose turn waits on it.
        spec: The normalized spec (:func:`validate_suspension_spec`).
        site: The hook it was raised from.
        extension: The extension that raised it.
        call: At a ``tool_call`` site, ``{id, effective_args}``; else ``None``.
        entry_id: At an ``entry_written`` site, the entry written; else ``None``.
        queued: Whether another suspension on its cursor is open ahead of it.
        opened_at_ms: When it was raised.
    """

    id: str
    cursor: Cursor
    spec: dict[str, Any]
    site: str
    extension: str
    call: dict[str, Any] | None
    entry_id: str | None
    queued: bool
    opened_at_ms: int
    signal: AbortSignal = field(repr=False)
    payload: dict[str, Any] = field(repr=False)
    future: asyncio.Future[dict[str, Any]] = field(repr=False)
    ready: asyncio.Future[None] = field(repr=False)
    closed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def state(self) -> dict[str, Any]:
        """What a head renders and an attach snapshot carries: ``SuspensionState`` on the wire.

        Returns:
            ``{id, session_id, cursor_id, parent_cursor_id, depth, spawned_from,
            spec, site, extension, target_id, call, entry_id, queued,
            opened_at_ms}``. ``depth`` is the cursor's owner-chain length;
            ``spawned_from`` the entry a child cursor was opened at.
        """
        owner = self.cursor.owner
        depth = 0
        walk = owner
        while walk is not None:
            depth += 1
            walk = walk.owner
        return {
            "id": self.id,
            "session_id": self.cursor.session_id,
            "cursor_id": self.cursor.id,
            "parent_cursor_id": None if owner is None else owner.id,
            "depth": depth,
            "spawned_from": self.cursor.opened_at if owner is not None else None,
            "spec": copy.deepcopy(self.spec),
            "site": self.site,
            "extension": self.extension,
            "target_id": self.spec.get("targetId"),
            "call": copy.deepcopy(self.call),
            "entry_id": self.entry_id,
            "queued": self.queued,
            "opened_at_ms": self.opened_at_ms,
        }


SuspensionEmitter = Callable[[str, "Suspension", dict[str, Any]], Awaitable[None]]
"""Emits ``(event_type, suspension, extra_fields)`` as an ``AgentEvent`` on the session's bus."""


def _now_ms() -> int:
    """Milliseconds since the epoch."""
    return int(time.time() * 1000)


def aborted_resolution(suspension_id: str | None) -> dict[str, Any]:
    """The resolution of a suspension its turn's abort ended: action ``abort``, nobody answering.

    Args:
        suspension_id: The suspension's id, or ``None`` when the turn was
            already aborted before one was raised.

    Returns:
        ``{id, action: "abort", values: {}, answered_by: None, at_ms}``.
    """
    return {
        "id": suspension_id,
        "action": ABORT_ACTION,
        "values": {},
        "answered_by": None,
        "at_ms": _now_ms(),
    }


class SuspensionBroker:
    """A session's open and queued suspensions, serialized per cursor (§3.2.1, §3.3).

    Args:
        emit: Sends ``suspension_open``/``suspension_closed``.
        abort_cursor: Aborts a cursor's turn and the cursors it owns; called
            when an answer picks :data:`ABORT_ACTION`.
    """

    def __init__(self, emit: SuspensionEmitter, abort_cursor: Callable[[Cursor], None]) -> None:
        self._emit = emit
        self._abort_cursor = abort_cursor
        self._open: dict[str, Suspension] = {}
        self._queues: dict[str, list[Suspension]] = {}
        self._closed: dict[str, dict[str, Any]] = {}
        self._forced: dict[str, SuspensionCloseReason] = {}

    def pending(
        self, cursor: Cursor | None = None, scope: PendingScope = "session"
    ) -> list[Suspension]:
        """The suspensions not yet closed, oldest first.

        Args:
            cursor: The cursor ``"cursor"`` and ``"tree"`` are relative to.
            scope: ``"cursor"``: ``cursor``'s own; ``"tree"``: ``cursor``'s and
                every cursor it owns, transitively; ``"session"``: all.

        Returns:
            The suspensions, open ones before the ones queued behind them.

        Raises:
            ValueError: ``scope`` is unknown, or needs a ``cursor`` and has none.
        """
        if scope == "session":
            return list(self._open.values())
        if scope not in ("cursor", "tree") or cursor is None:
            raise ValueError(f"pending: scope {scope!r} needs one of cursor/tree with a cursor")

        def under(candidate: Cursor) -> bool:
            walk: Cursor | None = candidate
            while walk is not None:
                if walk is cursor:
                    return True
                walk = walk.owner if scope == "tree" else None
            return False

        return [s for s in self._open.values() if under(s.cursor)]

    def get(self, suspension_id: str) -> Suspension | None:
        """The suspension ``suspension_id`` names while it is open or queued, else ``None``."""
        return self._open.get(suspension_id)

    async def raise_and_wait(
        self,
        *,
        cursor: Cursor,
        signal: AbortSignal,
        spec: dict[str, Any],
        site: str,
        extension: str,
        call: dict[str, Any] | None,
        entry_id: str | None,
        continuation: dict[str, Any],
    ) -> dict[str, Any]:
        """Write the side entry, queue on ``cursor``, and wait for the answer or the turn's abort.

        Args:
            cursor: The turn's cursor; the side entry hangs off its leaf.
            signal: The turn's abort signal, captured now.
            spec: The normalized spec.
            site: The hook it is raised from.
            extension: The extension raising it.
            call: ``{id, effective_args}`` at a ``tool_call`` site.
            entry_id: The written entry at an ``entry_written`` site.
            continuation: Where the loop stands, recorded for milestone 2 (§3.7.3).

        Returns:
            The resolution, ``{id, action, values, answered_by, at_ms}``. When the
            turn is already aborted nothing is written or emitted, and the
            resolution is :func:`aborted_resolution` with ``id`` ``None``.
        """
        if signal.is_aborted():
            return aborted_resolution(None)
        payload: dict[str, Any] = {
            "spec": copy.deepcopy(spec),
            "site": site,
            "extension": extension,
            "targetId": spec.get("targetId"),
            "call": copy.deepcopy(call),
            "entryId": entry_id,
            "continuation": continuation,
        }
        suspension_id = await cursor.open_side(SUSPENSION_KIND, **payload)
        loop = asyncio.get_running_loop()
        queue = self._queues.setdefault(cursor.id, [])
        suspension = Suspension(
            id=suspension_id,
            cursor=cursor,
            spec=spec,
            site=site,
            extension=extension,
            call=call,
            entry_id=entry_id,
            queued=bool(queue),
            opened_at_ms=_now_ms(),
            signal=signal,
            payload=payload,
            future=loop.create_future(),
            ready=loop.create_future(),
        )
        queue.append(suspension)
        self._open[suspension_id] = suspension
        if not suspension.queued:
            suspension.ready.set_result(None)
        resolution: dict[str, Any] | None = None
        reason: SuspensionCloseReason = "abandoned"
        try:
            await self._emit("suspension_open", suspension, {})
            resolution, reason = await self._wait(suspension)
        finally:
            if resolution is None:
                resolution = aborted_resolution(suspension_id)
            await self._close(suspension, resolution, reason)
        return resolution

    async def _wait(self, suspension: Suspension) -> tuple[dict[str, Any], SuspensionCloseReason]:
        """Wait out the queue, then the answer, each raced against the turn's abort."""
        aborted: asyncio.Future[Any] = asyncio.ensure_future(suspension.signal.wait())
        try:
            if not suspension.ready.done():
                turn: asyncio.Future[Any] = suspension.ready
                await asyncio.wait({turn, aborted}, return_when=asyncio.FIRST_COMPLETED)
                if not suspension.ready.done():
                    return aborted_resolution(suspension.id), "aborted"
                if suspension.future.done():
                    return suspension.future.result(), self._forced.pop(suspension.id, "answered")
                if suspension.signal.is_aborted():
                    return aborted_resolution(suspension.id), "aborted"
                suspension.queued = False
                await self._emit("suspension_open", suspension, {})
            answer: asyncio.Future[Any] = suspension.future
            await asyncio.wait({answer, aborted}, return_when=asyncio.FIRST_COMPLETED)
            if suspension.future.done():
                return suspension.future.result(), self._forced.pop(suspension.id, "answered")
            return aborted_resolution(suspension.id), "aborted"
        finally:
            aborted.cancel()

    async def close_all(self, reason: SuspensionCloseReason) -> None:
        """Resolve every open and queued suspension as ``abort``, closing each with ``reason``.

        Each one's turn is aborted too, as an ``abort`` answer would. Returns once
        every one has finalized its side entry and emitted ``suspension_closed``.

        Args:
            reason: The close reason, e.g. ``"cursor_closed"`` at session shutdown.
        """
        closing = list(self._open.values())
        for suspension in closing:
            if suspension.future.done():
                continue
            self._forced[suspension.id] = reason
            suspension.future.set_result(aborted_resolution(suspension.id))
            if not suspension.ready.done():
                suspension.ready.set_result(None)
            self._abort_cursor(suspension.cursor)
        for suspension in closing:
            await suspension.closed.wait()

    async def _close(
        self, suspension: Suspension, resolution: dict[str, Any], reason: SuspensionCloseReason
    ) -> None:
        """Finalize the side entry, leave the queue, wake the next, and emit ``suspension_closed``."""
        self._closed[suspension.id] = resolution
        self._open.pop(suspension.id, None)
        queue = self._queues.get(suspension.cursor.id, [])
        if suspension in queue:
            queue.remove(suspension)
        if queue and not queue[0].ready.done():
            queue[0].ready.set_result(None)
        if not queue:
            self._queues.pop(suspension.cursor.id, None)
        if not suspension.future.done():
            suspension.future.cancel()
        try:
            await suspension.cursor.finalize_side(
                suspension.id, **suspension.payload, resolution=resolution, reason=reason
            )
            await self._emit(
                "suspension_closed", suspension, {"resolution": resolution, "reason": reason}
            )
        finally:
            suspension.closed.set()

    def answer(
        self,
        suspension_id: str,
        action: str,
        values: dict[str, Any] | None = None,
        answered_by: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Answer an open suspension; the first valid answer wins (§3.3).

        Args:
            suspension_id: The suspension's id.
            action: One of its spec's action labels. :data:`ABORT_ACTION` also
                aborts its cursor's turn and the cursors that cursor owns.
            values: The filled fields; every declared field, checked as
                ``answer_request`` checks them. ``None`` is ``{}``.
            answered_by: Who answered, e.g. ``{"client_id", "client"}``; recorded.

        Returns:
            ``{"accepted": True, "resolution": …}``, or ``{"accepted": False,
            "reason": "already resolved", "resolution": …}`` for one already
            closed, which is not an error.

        Raises:
            ValueError: no such suspension, one still queued behind another, an
                action its spec does not declare, or values its fields reject.
        """
        closed = self._closed.get(suspension_id)
        if closed is not None:
            return {"accepted": False, "reason": "already resolved", "resolution": closed}
        suspension = self._open.get(suspension_id)
        if suspension is None:
            raise ValueError(f"answer_suspension: no suspension with id {suspension_id!r}")
        if suspension.future.done():
            return {
                "accepted": False,
                "reason": "already resolved",
                "resolution": suspension.future.result(),
            }
        if not suspension.ready.done():
            raise ValueError(
                f"answer_suspension: suspension {suspension_id!r} is queued behind another on "
                f"cursor {suspension.cursor.id}; answer the open one first"
            )
        labels = [a["label"] for a in suspension.spec["actions"]]
        if action not in labels:
            raise ValueError(f"answer_suspension: {action!r} is not one of its actions {labels}")
        answered = dict(values or {})
        try:
            validate_form_values(suspension.spec["fields"], answered)
        except ValueError as exc:
            raise ValueError(f"answer_suspension: {exc}") from exc
        resolution = {
            "id": suspension_id,
            "action": action,
            "values": answered,
            "answered_by": None if answered_by is None else dict(answered_by),
            "at_ms": _now_ms(),
        }
        suspension.future.set_result(resolution)
        if action == ABORT_ACTION:
            self._abort_cursor(suspension.cursor)
        return {"accepted": True, "resolution": resolution}
