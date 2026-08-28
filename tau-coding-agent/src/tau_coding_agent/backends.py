"""
Backend abstraction layer for tau-coding-agent.

Wraps tau-agent-core's AgentSession to provide Parley-compatible
Backend interfaces (chat, stream_chat).

Reference: SESSION-TREE-IMPLEMENTATION.md §2.6 (throwaway SessionManager retired;
AgentSession runs against a scratch InMemorySessionLog, caller owns persistence).
"""

import re
import sys
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Literal, cast
from uuid import uuid4
from tau_llm.compat import Compat
from tau_llm.models import EXTENDED_THINKING_LEVELS, is_valid_thinking_level
from tau_llm.providers import get_provider_spec, registered_apis
from tau_llm.types import Model
from tau_agent_core.agent_session import (
    AgentSession,
    ExtensionActionResult,
    ExtensionCommandResult,
)
from tau_agent_core.compaction import CompactionSettings, estimate_span_tokens
from tau_agent_core.event_projection import MessageDeltaProjector
from tau_agent_core.events import AgentEvent
from tau_agent_core.session_log import InMemorySessionLog, SessionLog, agent_spec_in_force
from tau_agent_core.sdk import (
    BASE_SYSTEM_PROMPT,
    LoadExtensionsResult,
    _build_system_prompt,
    _resolve_tools,
    append_system_prompt,
)
from tau_agent_core.submission import Submission, SubmissionResult

#: The render lane a caller that names none renders into. Every pre-B3-a consumer
#: had exactly one implicit lane; this is its name, so a single-stream caller
#: (``stream_chat``, the reload path, a test replaying widget events) reads the
#: same as it always did while a multi-lane renderer keys on real lane ids.
DEFAULT_LANE = "main"

#: The built-in tools a model entry that names none gets. Named rather than
#: inlined at its one use because the empty chat pane prints this list back to
#: the user before the first turn — two copies of it would drift, and the copy
#: on screen would be the one that lied.
DEFAULT_TOOL_NAMES: tuple[str, ...] = ("read", "write", "edit", "bash", "ls", "grep", "find")

#: A render event handler. Sync or async — :class:`RenderRouter` awaits whatever
#: it gets back, so a Textual app can mount widgets from it.
RenderHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


def resolve_tool_names(config: dict[str, Any]) -> list[str]:
    """The built-in tool names one model config resolves to, in order.

    The single reader of ``config["tools"]`` / ``config["exclude_tools"]``:
    :class:`TauBackend` calls it to decide what to construct, and
    ``Parley._session_facts`` calls it to decide what to *display*. Both take the
    config AFTER ``Parley._apply_run_config``, so ``--exclude-tools`` and both
    tool-suppression flags — ``--no-tools`` and ``--no-builtin-tools``, each of
    which sets ``tools=[]`` — are already folded in.

    This answers only "which BUILT-INS?". Whether extension-registered tools are
    also withheld is ``config["no_tools"] == "all"``, decided far downstream in
    ``AgentSession._build_turn_tools``; it is not this function's question and is
    deliberately not second-guessed here.
    """
    names = config.get("tools", list(DEFAULT_TOOL_NAMES))
    exclude = set(config.get("exclude_tools") or [])
    return [t for t in names if t not in exclude]


def tau_event_to_pi_event(event: AgentEvent) -> dict[str, Any] | None:
    """Serialize one τ :class:`AgentEvent` into a pi-faithful ``AgentSessionEvent``.

    pi's ``--mode json`` writes every session-subscribe event straight to stdout
    as a ``type``-discriminated JSON line (``print-mode.ts:104-108``). τ's
    ``AgentEvent`` already carries a ``type`` discriminator and τ-snake field
    names, so the wire shape is the event's own ``model_dump(exclude_none=True)``
    — there is no legacy ``kind`` remap here (that schema is the TUI widget
    channel; this is the pi-faithful channel the delegate reads, step S8 /
    D-delegate).

    One faithfulness adjustment — dedup ``message_end``. The agent loop emits
    ``message_end`` **twice** for a tool-bearing turn: once per-completion
    (carrying ``usage``/``model``/``stop_reason``, ``agent_loop.py:485``) and once
    from ``run()``/``run_continue`` (content only). pi emits exactly **one**
    ``message_end`` per assistant message, so keep the usage-bearing one and drop
    the content-only duplicate (``None`` → the caller skips it). Every emitted
    ``message_end`` therefore carries usage/model/stop_reason, which is what the
    delegate's per-child limit / stop_reason taxonomy reads.
    """
    if event.type == "message_end":
        message = event.message or {}
        if "usage" not in message:
            return None
    # The kept ``message_end`` already carries ``message.usage.extra`` (llama.cpp
    # ``timings`` + τ's JSON-repair count) verbatim through this plain-dict passthrough
    # — the ``--mode json`` telemetry surface (G4/B). We deliberately do NOT stamp a
    # ``constraints:{kind:none}`` onto every ``message_end``: the main agent loop applies
    # no DecodeConstraints, so it would be a fabricated placeholder on every turn. The
    # constraint echo surfaces only where a real constraint exists — the ``ctx.complete()``
    # path, as a ``{"kind":"constraints",...}`` extension record (G4/C), not a lifecycle field.
    return event.model_dump(exclude_none=True)


def prompt_tokens(usage: dict[str, Any]) -> int:
    """One completion's prompt size — the conversation's context when it was sent.

    Read as ``total_tokens - output_tokens``, because ``total_tokens`` is the
    server's own figure for the whole call and every provider's ``output_tokens``
    is the part of it the model generated. The remainder is the prompt, whatever
    fields the provider split it across.

    The alternative — summing ``input + cache_read + cache_write`` — is equal on
    a transcript written by today's code but WRONG on one written before the
    providers stopped double-counting the cached span inside ``input_tokens``. A
    reloaded chat from last week would read ~2× its real size on a cache-heavy
    provider. The subtraction gets both right with no version check and no guess.

    Falls to the field sum only when the server reported no ``total_tokens`` at
    all; ``Usage`` defaults it to 0, so 0 here means "nothing reported", not a
    zero-token prompt. A ``total`` below ``output`` is a contradiction rather than
    a small number, so it takes the same path instead of yielding a negative size.

    This is a per-completion reading. Callers REPLACE it as completions arrive
    rather than summing: prompt N contains prompt N-1 in full, so a sum reports
    the same conversation once per turn. Shared by the live path
    (:class:`TurnStream`) and the TUI's reload/header rollups, so both read the
    context the same way.
    """
    total = int(usage.get("total_tokens", 0) or 0)
    output = int(usage.get("output_tokens", 0) or 0)
    if total >= output and total > 0:
        return total - output
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_tokens", 0) or 0)
        + int(usage.get("cache_write_tokens", 0) or 0)
    )


class TurnStream:
    """One lane's worth of agent events, normalized into widget-lifecycle dicts.

    Extracted verbatim from ``TauBackend.stream_submission``'s ``capture_event``
    closure, which was the only thing that knew how to turn an
    :class:`~tau_agent_core.events.AgentEvent` into something a renderer can draw
    — and could only ever do it for the ONE turn its enclosing call was awaiting.
    As a class it can be instantiated per lane, which is what lets two concurrent
    turns (a ``fork``) and a turn the frontend never initiated (a bus/timer
    submission) be rendered at all.

    :meth:`feed` returns the normalized events for one agent event, in order, each
    tagged with this stream's ``lane``. It also accumulates what a caller needs
    when the lane closes: the assistant text, the tool-call records for chat
    persistence, the real token totals, and the last completion's telemetry.

    Event shapes (all dicts with ``"kind"`` and ``"lane"``)::

        {"kind": "turn_start", "turn_index": int}
        {"kind": "text_delta", "delta": str}
        {"kind": "reasoning_delta", "delta": str}
        {"kind": "tool_call", "id": str, "name": str, "arguments": dict}
        {"kind": "tool_result", "id": str, "name": str, "result": str,
         "is_error": bool, "blocked": bool, "blocked_by": str | None}
        {"kind": "completion_end", "output": int, "context": int}

    Tool widgets are driven off ``tool_execution_start`` / ``tool_execution_end``
    (which carry name/args/result directly), NOT off ``message_end`` toolCall
    blocks — the agent loop emits ``message_end`` twice per tool-bearing turn, so
    consuming it for rendering would duplicate. ``message_end`` is used only to
    harvest ``tool_calls`` for chat persistence (deduplicated by id), the
    per-completion usage, and the ``completion_end`` boundary below.

    ``completion_end`` carries this lane's REAL token totals so far — the same
    running sums ``lane_end`` reports, published at every completion boundary
    instead of only at the end. A tool-bearing turn has one per tool call, so a
    live counter can show a measured figure that steps mid-turn rather than an
    approximation. It is emitted on both of the turn's ``message_end`` events;
    the second adds no usage, so it restates the same totals, and both mark the
    same real boundary — the completion is over and nothing is in flight.
    """

    def __init__(self, lane: str = DEFAULT_LANE) -> None:
        self.lane = lane
        #: Every text delta this lane produced, in order (``"".join`` = the answer).
        self.text_chunks: list[str] = []
        #: Tool calls harvested for chat persistence, deduped by id.
        self.tool_calls: list[dict[str, Any]] = []
        # Real token usage, summed across every completion in this lane. The agent
        # loop attaches per-completion usage to the message_end it emits once per
        # turn (agent_loop._stream_response), so summing is double-count-safe. We
        # surface the REAL numbers (Fail-Early: never a len//4 approximation).
        self.usage_totals: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        #: The LAST completion's prompt size (input + cache_read + cache_write) —
        #: the conversation's context at the moment this lane finished. Kept as a
        #: replace, never a sum: each completion's prompt already CONTAINS every
        #: earlier one, so summing prompts across a tool-bearing turn reports the
        #: same conversation once per completion. 0 until a completion reports.
        self.context_tokens: int = 0
        # The LAST completion's ``usage.extra`` — server-reported per-completion
        # telemetry (llama.cpp timings + τ's JSON-repair count). t/s and
        # forced-share are per-COMPLETION, not summable like tokens, so only the
        # final completion's dict is kept — never a merge or an average.
        self.last_extra: dict[str, Any] = {}
        # The cumulative-message -> delta projection (suffix-diffing "text" and
        # "thinking" blocks, including the defensive replace-not-extend case)
        # lives in tau_agent_core.event_projection — extracted so a non-TUI
        # consumer (the RPC wire) reuses the SAME rules without depending on
        # Textual (REMOTE-CONTROL.md E1, R-T6). It is stateful because the diff
        # needs to remember what was already emitted; reset per turn below.
        self._delta_projector = MessageDeltaProjector()

    @property
    def text(self) -> str:
        """The assistant text this lane streamed, concatenated."""
        return "".join(self.text_chunks)

    def feed(self, event: Any) -> list[dict[str, Any]]:
        """Normalize one agent event into zero or more render events."""
        if not hasattr(event, "type"):
            return []
        if event.type == "turn_start":
            # Clean per-turn boundary. Reset the text accumulators so the next
            # turn's assistant text is a fresh delta stream (not concatenated onto
            # the previous turn's), and tell the caller to open a new pending
            # widget for this turn — which is what preserves true arrival order
            # (assistant text after a tool call ends up after it, not pinned above).
            self._delta_projector.reset()
            return [self._tag({"kind": "turn_start", "turn_index": event.turn_index})]
        if event.type == "message_update":
            return self._feed_message_update(event)
        if event.type == "message_end":
            return self._harvest_message_end(event)
        if event.type == "tool_execution_start":
            # Render the tool call as soon as it begins — this is the
            # authoritative, ordered signal (carries name + args directly).
            return [
                self._tag(
                    {
                        "kind": "tool_call",
                        "id": getattr(event, "tool_call_id", "") or "",
                        "name": getattr(event, "tool_name", "") or "",
                        "arguments": getattr(event, "args", None) or {},
                    }
                )
            ]
        if event.type == "tool_execution_end":
            return [self._feed_tool_execution_end(event)]
        return []

    def _tag(self, structured: dict[str, Any]) -> dict[str, Any]:
        structured["lane"] = self.lane
        return structured

    def _feed_message_update(self, event: Any) -> list[dict[str, Any]]:
        message = getattr(event, "message", None)
        if not message:
            return []
        out: list[dict[str, Any]] = []
        for block_delta in self._delta_projector.project(message):
            if block_delta.delta is None:
                # A non-diffable block (today only toolCall). Tool activity is
                # rendered off tool_execution_start/_end, which carry name/args
                # directly and in true arrival order, so this lane drops it.
                continue
            # NOTE: block_delta.replace is deliberately IGNORED here, preserving
            # this code's pre-extraction behaviour exactly. In the ordinary case
            # `delta` is the incremental suffix and appending is correct. In the
            # replace-not-extend case `delta` is the block's ENTIRE new value and
            # the projector's contract asks the caller to RESET its accumulator;
            # this call site appends instead, so ``text`` does not reproduce the
            # final assistant text in that case. That defect predates the
            # extraction and is preserved, not introduced — fixing it changes
            # rendered output and belongs in its own change, not a merge.
            if block_delta.type == "text":
                self.text_chunks.append(block_delta.delta)
                out.append(self._tag({"kind": "text_delta", "delta": block_delta.delta}))
            elif block_delta.type == "thinking":
                # Reasoning streams on its own channel using the same suffix-diff
                # as text. Deliberately NOT part of the answer text (that contract
                # is the visible answer only) — it surfaces as a structured
                # ``reasoning_delta`` for the reasoning-region widget.
                out.append(self._tag({"kind": "reasoning_delta", "delta": block_delta.delta}))
        return out

    def _harvest_message_end(self, event: Any) -> list[dict[str, Any]]:
        message = getattr(event, "message", None)
        if not message:
            return []
        content = message.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    tc_id = block.get("id", "")
                    if any(tc["id"] == tc_id for tc in self.tool_calls):
                        continue
                    self.tool_calls.append(
                        {
                            "id": tc_id,
                            "name": block.get("name", ""),
                            "arguments": block.get("arguments", {}),
                        }
                    )
        # Sum the real usage carried on this completion's message_end. Only the
        # per-completion message_end (_stream_response) carries it, so the
        # duplicate run() emit adds nothing — no double count.
        usage = message.get("usage")
        if isinstance(usage, dict):
            for key in self.usage_totals:
                self.usage_totals[key] += int(usage.get(key, 0) or 0)
            self.context_tokens = prompt_tokens(usage)
            # Overwrite, don't merge: a real completion with no telemetry SHOULD
            # clear the prior reading, so take whatever this completion carried.
            extra = usage.get("extra")
            self.last_extra = extra if isinstance(extra, dict) else {}
        # The boundary itself, with whatever is measured at it. A completion that
        # reported no usage still ends here, and the totals it publishes are the
        # ones that ARE measured — a provider that never reports usage publishes
        # 0, which is the honest reading (nothing was measured), not a guess.
        return [
            self._tag(
                {
                    "kind": "completion_end",
                    "output": self.usage_totals["output_tokens"],
                    "context": self.context_tokens,
                }
            )
        ]

    def _feed_tool_execution_end(self, event: Any) -> dict[str, Any]:
        tool_call_id = getattr(event, "tool_call_id", "") or ""
        is_error = getattr(event, "is_error", False)
        # A `tool_call` extension VETO (S50, anchor G11) renders distinctly from a
        # generic error — carry the marker + attribution through.
        blocked = bool(getattr(event, "blocked", False))
        blocked_by = getattr(event, "blocked_by", None)
        result = getattr(event, "result", "")
        if isinstance(result, list):
            result = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in result)
        result_str = str(result)
        # Record result against the persisted tool call (if tracked).
        for tc in self.tool_calls:
            if tc["id"] == tool_call_id:
                tc["result"] = result_str[:200]
                tc["error"] = is_error
                break
        return self._tag(
            {
                "kind": "tool_result",
                "id": tool_call_id,
                "name": getattr(event, "tool_name", "") or "",
                "result": result_str,
                "is_error": is_error,
                "blocked": blocked,
                "blocked_by": blocked_by,
            }
        )


class RenderRouter:
    """Demultiplex ONE session's whole bus into per-lane render events (B3-a).

    Reference: docs/SUBMISSION-LIFECYCLE.md, end of "Phasing" — *"backends.py:200
    stream_chat is single-stream by construction, and nothing yet subscribes to
    the branch_event channel, so a fork today is unobservable"*.

    A frontend attaches this ONCE, for the life of the session, instead of
    subscribing per awaited turn. Every submission that runs a turn becomes a
    lane; every ``fork``/``spawn_branch`` sub-agent becomes a second lane; the
    events of each are tagged with it so a renderer can draw them side by side
    rather than interleaving them into one transcript.

    Follows Jupyter's rule, which the spec states explicitly and which is easy to
    get backwards: *"a frontend filters on 'is this mine?' to decide HOW to
    render, and still renders the rest. Dropping other sources' events is how a
    multi-client session becomes incoherent."* So this router does not filter on
    ``source`` at all — it CARRIES ``source``/``submitter``/``correlation`` onto
    ``lane_start``/``lane_end`` and lets the renderer decide how a bus or forked
    turn should look.

    The emitted vocabulary is :class:`TurnStream`'s, plus the two lane brackets::

        {"kind": "lane_start", "lane": str, "source": str | None,
         "submitter": str | None, "correlation": dict, "text": str}
        {"kind": "lane_end", "lane": str, "source": str | None,
         "submitter": str | None, "context": int, "output": int, "extra": dict}

    ``output`` is every token the lane GENERATED, summed across its completions
    and including the side-usage delta ``submission_end`` reports for work done
    off the agent loop (auto-compaction, an extension's ``ctx.complete()``).
    ``context`` is the prompt the lane last SENT — a replace, not a sum, because
    each completion's prompt contains every earlier one. Side usage is a different
    conversation's prompt, so it is not added to ``context``. ``extra`` is the last
    completion's telemetry, or ``{}`` when the provider reported none.

    An agent event whose ``submission_id`` names no open lane is NOT dropped in
    silence: it goes to ``on_orphan`` with a reason. Those exist — a
    ``continue_conversation()`` resume, or a ``compact()`` outside any submission,
    emits ``agent_start``/``agent_end`` with no submission to stamp them — and a
    renderer that swallowed them would be indistinguishable from one that had
    quietly stopped working.
    """

    def __init__(
        self,
        emit: RenderHandler,
        *,
        on_orphan: Callable[[str], None] | None = None,
    ) -> None:
        self._emit = emit
        self._on_orphan = on_orphan
        self._lanes: dict[str, TurnStream] = {}
        # source/submitter per open lane, so lane_end can report them without the
        # caller having to remember what lane_start said.
        self._identity: dict[str, tuple[str | None, str | None]] = {}
        # Set by whatever wired this router onto a bus (TauBackend.subscribe_render).
        # A router built by hand — a test replaying events — has nothing to detach.
        self._detach: Callable[[], None] | None = None

    def bind_detach(self, detach: Callable[[], None]) -> None:
        """Record how to unsubscribe this router from the bus it was wired onto."""
        self._detach = detach

    def detach(self) -> None:
        """Unsubscribe from the bus. Idempotent; still-open lanes are NOT closed.

        Closing them needs an ``await`` (the render handler may mount widgets), so
        it is :meth:`close_all` — a separate call, deliberately, because "stop
        listening" and "finish what is on screen" are different decisions and a
        caller tearing down a whole screen wants only the first.
        """
        if self._detach is not None:
            self._detach()
            self._detach = None

    @property
    def open_lanes(self) -> list[str]:
        """The lanes currently streaming, in the order they opened."""
        return list(self._lanes)

    async def on_submission_start(
        self, *, submission: Submission, text: str, images: Any = None
    ) -> None:
        """Open the lane for an admitted submission (``submission_start`` channel)."""
        lane = submission.submission_id
        self._lanes[lane] = TurnStream(lane)
        self._identity[lane] = (submission.source, submission.submitter)
        await self._deliver(
            {
                "kind": "lane_start",
                "lane": lane,
                "source": submission.source,
                "submitter": submission.submitter,
                "correlation": dict(submission.correlation),
                "text": text,
            }
        )

    async def on_submission_end(
        self, *, submission: Submission, side_usage: dict[str, int] | None = None
    ) -> None:
        """Close the lane for a finished submission (``submission_end`` channel)."""
        await self._close(submission.submission_id, side_usage=side_usage)

    async def on_agent_event(self, event: AgentEvent) -> None:
        """Route one ``AgentEvent`` from the primary bus into its submission's lane."""
        lane = event.submission_id
        if lane is None:
            self._orphan(
                f"{event.type} carries no submission_id — it was emitted outside "
                "submit() (continue_conversation, or a compact/navigate), so there "
                "is no lane to render it into"
            )
            return
        await self._route(lane, event)

    async def on_branch_event(self, *, lane: str, label: str, event: AgentEvent) -> None:
        """Route one sub-agent event (``branch_event`` channel) into its branch lane.

        A branch opens its lane on its FIRST event, because the
        ``submission_start``/``submission_end`` pair the primary path uses is
        emitted on the SUB-session's bus and only its ``AgentEvent``s are forwarded
        here (``ExtensionContext.spawn_branch``).

        It closes on :meth:`on_branch_end`, NOT on the sub-agent's own
        ``agent_end``. That was the original bracket and it leaked: ``agent_end``
        used to be reachable only by falling out of ``AgentLoop.run``'s while loop,
        so a branch whose turn raised (a dropped connection; a provider
        ``ErrorEvent``, which the loop turns into a ``RuntimeError``) or was
        cancelled (``abort()`` cancels every forked task) emitted no ``agent_end``
        at all — and ``spawn_branch`` contains the failure, so no other signal
        arrived either. The lane, its exchange and its LaneStrip entry were then
        held open for the rest of the session: a permanently "Working…" exchange,
        which is the silent-hang shape this lifecycle exists to remove.

        ``AgentLoop`` now closes that bracket from an ``except`` that re-raises, so
        the specific leak above is fixed at the source — but ``branch_end`` stays
        the bracket regardless, for the same reason ``submission_end`` does on the
        primary path: a branch can fail BEFORE ``agent_start`` (an admission
        refusal on the sub-session), and one span can contain more than one loop.
        A bracket that only exists once the loop has started cannot close a span
        that never got that far. ``branch_end`` is emitted from a ``finally``, so
        it arrives however the branch ended.

        The branch's own events carry the SUB-session's provenance (its
        ``prompt()`` wrapper says ``interactive``/``human``), which would be a lie
        on the primary transcript — a person did not type this. The lane is
        re-identified as ``source="agent"``, ``submitter="fork:<label>"``: τ
        driving itself, which is what :data:`SubmissionSource` reserves ``"agent"``
        for.
        """
        key = f"branch:{lane}"
        if key not in self._lanes:
            self._lanes[key] = TurnStream(key)
            self._identity[key] = ("agent", f"fork:{label}")
            await self._deliver(
                {
                    "kind": "lane_start",
                    "lane": key,
                    "source": "agent",
                    "submitter": f"fork:{label}",
                    "correlation": {"branch_lane": lane, "branch_label": label},
                    "text": label,
                }
            )
        await self._route(key, event)

    async def on_branch_end(self, *, lane: str, label: str, error: str | None = None) -> None:
        """Close a branch lane on the sub-agent's terminal event (``branch_end``).

        The counterpart of :meth:`on_submission_end`, and emitted from the same
        kind of ``finally`` — see :meth:`on_branch_event` for why the sub-agent's
        ``agent_end`` cannot serve as the bracket.

        A ``branch_end`` for a lane that was never opened is real and is REPORTED,
        not silently ignored: a branch that failed before emitting even
        ``agent_start`` (an admission refusal on the sub-session) rendered nothing,
        so there is no exchange to finalize — but a renderer that swallowed that
        would be indistinguishable from one that had stopped working. ``error``
        rides the reason so the report names what ended the branch.
        """
        key = f"branch:{lane}"
        if key not in self._lanes:
            self._orphan(
                f"branch lane {key!r} ({label!r}) ended without ever opening — the "
                "sub-agent emitted no event, so nothing was rendered for it "
                f"(error: {error!r})"
            )
            return
        await self._close(key)

    async def close_all(self) -> None:
        """Close every still-open lane — the renderer teardown (session swap, quit).

        Without it a backend swapped mid-turn leaves a lane that will never be
        closed by an event, i.e. an exchange stuck on "Working…" forever.
        """
        for lane in list(self._lanes):
            await self._close(lane)

    async def _route(self, lane: str, event: AgentEvent) -> None:
        stream = self._lanes.get(lane)
        if stream is None:
            self._orphan(
                f"{getattr(event, 'type', '?')} names lane {lane!r}, which is not "
                "open — the event arrived before its lane_start or after its lane_end"
            )
            return
        for structured in stream.feed(event):
            await self._deliver(structured)

    async def _close(self, lane: str, *, side_usage: dict[str, int] | None = None) -> None:
        stream = self._lanes.pop(lane, None)
        if stream is None:
            self._orphan(f"lane {lane!r} closed twice, or was never opened")
            return
        source, submitter = self._identity.pop(lane, (None, None))
        # Two numbers, not one. ``output`` is summable — every completion generated
        # its own tokens, and a side call (a tool's own summarizing model) generated
        # more. ``context`` is NOT: it is the prompt this lane last sent, and a side
        # call's prompt is a different conversation, so it never lands here.
        output = stream.usage_totals["output_tokens"] + int(
            (side_usage or {}).get("output_tokens", 0)
        )
        await self._deliver(
            {
                "kind": "lane_end",
                "lane": lane,
                "source": source,
                "submitter": submitter,
                "context": stream.context_tokens,
                "output": output,
                "extra": dict(stream.last_extra),
            }
        )

    async def _deliver(self, structured: dict[str, Any]) -> None:
        result = self._emit(structured)
        if result is not None:
            await result

    def _orphan(self, reason: str) -> None:
        if self._on_orphan is not None:
            self._on_orphan(reason)


def compute_cost_usd(
    cost: dict[str, Any] | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
) -> float | None:
    """Dollar cost of one completed exchange, or ``None`` when the price is unknown.

    Port of pi ``calculateCost`` (``models.ts:39-48``), collapsed to a single
    total: τ stores no per-key ``cost`` breakdown on the frozen ``Usage`` (the
    E4.cost decision D2 leaves ``Usage`` untouched and prices at the emit
    boundary), so this is just ``sum(price[k] / 1e6 * tokens[k])`` over the
    priced buckets.

    ``cost`` is the optional per-model ``{input, output, cache_read,
    cache_write}`` block (USD per 1M tokens) declared on a ``~/.tau/config.json``
    model entry. Fail-Early — an **absent** block returns ``None`` (the caller
    emits tokens only, never a fabricated ``$0``); a **present** block whose
    prices are all ``0`` (a genuinely free/local model) returns ``0.0``. The two
    read differently on the wire (``cost_usd`` absent vs ``cost_usd: 0.0``),
    which is the whole point of the option.
    """
    if cost is None:
        return None
    return float(
        float(cost.get("input", 0.0)) / 1_000_000 * input_tokens
        + float(cost.get("output", 0.0)) / 1_000_000 * output_tokens
        + float(cost.get("cache_read", 0.0)) / 1_000_000 * cache_read_tokens
        # cache_write is inert against today's provider: cache_write_tokens is
        # never populated (a real 0), so its price term is always 0. Left
        # commented until a provider reports cache-write tokens.
        # + float(cost.get("cache_write", 0.0)) / 1_000_000 * cache_write_tokens
    )


#: Models already warned about an undeclared reasoning capability, so a resolver
#: rebuilding the same Model on every ``set_model`` says it once and not once per
#: call. Keyed by model id: two DIFFERENT misconfigured models are two findings.
_WARNED_UNDECLARED_REASONING: set[str] = set()


def _warn_undeclared_reasoning(model_id: str) -> None:
    """Warn (once per model) that a thinking level was requested without declaring
    reasoning support, and that the level is therefore being dropped.

    A warning rather than a raise, because this is a behavior change to a flag
    people already use: before, the request asserted the capability. A raise would
    break every working config on upgrade. The message names the exact key to add,
    so the warning is actionable rather than merely ignorable.
    """
    if model_id in _WARNED_UNDECLARED_REASONING:
        return
    _WARNED_UNDECLARED_REASONING.add(model_id)
    print(
        f"[τ] warning: a thinking level was requested for model {model_id!r}, which "
        "does not declare reasoning support, so no thinking level is being sent. Add "
        f'"reasoning": true to the {model_id!r} entry in ~/.tau/config.json if the '
        "endpoint supports it.",
        file=sys.stderr,
    )


#: A string of digits where a JSON number belongs. Rejected because it is the one
#: malformed value measured to be accepted AND discarded in silence: against
#: llama.cpp b1061-2da6686, `thinking_budget_tokens: "0"` returned HTTP 200 and a
#: generation byte-identical to not sending the parameter at all — as did `null`,
#: `-1`, and a misspelled key (docs/probe-results/). Only a JSON number is read.
#: A config that means 256 and writes "256" would otherwise reach an endpoint that
#: quietly does nothing, which is precisely the failure τ refuses to pass along.
_NUMERIC_STRING = re.compile(r"^[+-]?\d+$")


def _validate_thinking_level_map(value: Any, model_id: str) -> None:
    """Fail-Early on a ``thinking_level_map`` that cannot mean what it says.

    Pydantic already rejects a wrong TYPE at ``Model`` construction. This catches
    the two mistakes that are well-typed and still wrong: a key that is not a
    thinking level (a typo silently maps nothing), and a numeric string inside a
    fragment (a value the server accepts and discards).
    """
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(
            f"models.{model_id}.thinking_level_map must be a JSON object; got {value!r}"
        )
    for level, mapped in value.items():
        if not is_valid_thinking_level(level):
            known = ", ".join(EXTENDED_THINKING_LEVELS)
            raise ValueError(
                f"models.{model_id}.thinking_level_map has key {level!r}, which is not "
                f"a thinking level ({known}). A key that names no level maps nothing."
            )
        if mapped is None or isinstance(mapped, str):
            continue
        if not isinstance(mapped, dict):
            raise ValueError(
                f"models.{model_id}.thinking_level_map[{level!r}] must be a string "
                f"(sent as reasoning_effort), a request-body object, or null; "
                f"got {mapped!r}"
            )
        for key, fragment_value in mapped.items():
            if isinstance(fragment_value, str) and _NUMERIC_STRING.match(fragment_value):
                raise ValueError(
                    f"models.{model_id}.thinking_level_map[{level!r}][{key!r}] is the "
                    f"string {fragment_value!r}, not the number {fragment_value}. "
                    "A numeric field sent as a JSON string is accepted and silently "
                    "ignored by llama.cpp — the request succeeds and the setting "
                    "does nothing. Drop the quotes."
                )


def build_model_from_config(config: dict[str, Any]) -> Model:
    """Build a tau-agent-core ``Model`` from a Parley/``~/.tau/config.json`` entry.

    The single seam that turns a config ``models`` entry (or a ``--model`` ad-hoc
    dict) into a ``Model`` — extracted from ``TauBackend.__init__`` so
    :func:`make_model_resolver` (S45) reproduces exactly the same construction a
    fresh backend would. Maps the ``backend`` provider field, derives the reasoning
    flag from a non-``off`` ``thinking`` level (or an explicit ``reasoning: true``),
    and carries the optional ``thinking_level_map``.
    """
    model_id = config.get("model", "gpt-4")
    backend_type = config.get("backend", "openai").lower()

    # Map provider name (Parley's "backend" field) to tau-agent-core provider.
    provider_map = {"openai": "openai", "anthropic": "anthropic", "gemini": "gemini"}
    provider = provider_map.get(backend_type, backend_type)

    # Which WIRE PROTOCOL this endpoint speaks. This used to be hardcoded to
    # "openai-completions", which meant no config could name any other — the
    # api registry existed and no `~/.tau/config.json` user could reach it.
    #
    # Resolution order, and the polarity rule from PLAN-0.9.3 §4.5: a stated
    # value wins, then the registered vendor's own protocol, then the historical
    # default. An UNRECOGNISED stated value raises against the registry rather
    # than falling through to the OpenAI wire — a model silently served over the
    # wrong protocol is the exact failure that got "openai-responses"
    # unregistered, and it is worse here than a startup error.
    spec = get_provider_spec(provider)
    api = config.get("api") or (spec.api if spec else "openai-completions")
    if api not in registered_apis():
        raise ValueError(
            f"models.{model_id}.api is {api!r}, which τ does not implement. "
            f"Registered wire protocols: {', '.join(sorted(registered_apis()))}."
        )

    # A stated base_url wins; otherwise take the vendor's own default, and only
    # then the historical OpenAI URL. Defaulting every model to OpenAI's endpoint
    # would point an Anthropic client at the wrong server.
    base_url = config.get("base_url") or (spec.base_url if spec else None)
    if not base_url:
        base_url = "https://api.openai.com/v1"

    # Reasoning capability is DECLARED, never inferred. This used to read
    # ``bool(config.get("reasoning")) or <a level was requested>``, so asking for a
    # level asserted the capability on the model's behalf — the opposite of what
    # ``Model.reasoning``'s own docstring promises ("Default False (Fail-Early: opt
    # in, don't guess capability)") and of the identical rule ``grammar_dialect``
    # states two fields below ("We do NOT infer support from the base_url or
    # provider"). pi does infer (model-resolver.ts:496); this is a deliberate
    # divergence, in the same family as ``reasoning_replay``.
    #
    # It is not a tidiness fix. The inference is what made a dead parameter look
    # alive: ``--thinking high`` against a config declaring no ``reasoning`` key
    # sent ``reasoning_effort`` to an endpoint that ignores the field, and nothing
    # anywhere said so. Declaring the capability is now the ONLY way to enable it,
    # and asking for a level without declaring it warns rather than assuming.
    thinking_level = config.get("thinking")
    reasoning_arg = thinking_level if thinking_level and thinking_level != "off" else None
    model_reasoning = bool(config.get("reasoning"))
    if reasoning_arg is not None and not model_reasoning:
        _warn_undeclared_reasoning(model_id)

    # Reasoning-replay scope (Model.reasoning_replay). The per-model entry wins;
    # the frontends fold a top-level ``reasoning_replay`` default into the entry
    # before this seam, so a missing key means "no default configured" → "turn"
    # (the τ code default). Fail-Early on an unknown value rather than silently
    # falling back to a scope the user didn't ask for.
    reasoning_replay = config.get("reasoning_replay") or "turn"
    if reasoning_replay not in ("all", "turn", "off"):
        raise ValueError(
            f"reasoning_replay must be one of 'all', 'turn', 'off'; got {reasoning_replay!r}"
        )
    reasoning_replay = cast(Literal["all", "turn", "off"], reasoning_replay)

    # Whether a reasoning-format quirk warns and degrades (default) or raises
    # (Model.strict_reasoning_formats). Per-model only — there is no top-level
    # default to fold in, because the flag is a statement about one endpoint's
    # pipeline, not about the installation.
    # Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md S3.
    strict_reasoning_formats = config.get("strict_reasoning_formats", False)
    if not isinstance(strict_reasoning_formats, bool):
        raise ValueError(
            "models.<name>.strict_reasoning_formats must be a boolean; got "
            f"{strict_reasoning_formats!r}"
        )

    # Constrained-decoding capability (Model.grammar_dialect). Absent = the endpoint
    # declares no grammar support, and a constraint-carrying call will raise. We do
    # NOT infer support from the base_url or provider: guessing wrong means either a
    # hard 400 or — worse — a server that silently ignores the grammar and returns an
    # unconstrained generation as if it were constrained (Fail-Early).
    grammar_dialect = config.get("grammar")
    if grammar_dialect is not None and grammar_dialect not in ("llguidance", "gbnf"):
        raise ValueError(
            f"models.<name>.grammar must be 'llguidance' or 'gbnf'; got {grammar_dialect!r}"
        )
    grammar_dialect = cast("Literal['llguidance', 'gbnf'] | None", grammar_dialect)

    _validate_thinking_level_map(config.get("thinking_level_map"), model_id)

    extra_body = config.get("extra_body") or {}
    if not isinstance(extra_body, dict):
        raise ValueError(f"models.<name>.extra_body must be a JSON object; got {extra_body!r}")

    server_features = config.get("server_features") or []
    if not isinstance(server_features, list):
        raise ValueError(
            f"models.<name>.server_features must be a list of strings; got {server_features!r}"
        )

    # Endpoint wire quirks (Model.compat): which spelling of the output cap this
    # server accepts, whether it tolerates `stream_options`, and which schema it
    # returns tool calls in. Absent means τ infers the first two from
    # provider/base_url and reads tool calls as OpenAI-shaped, which is what every
    # existing config gets and what it got before this key existed.
    # `tool_call_schema` is never inferred — see tau_llm/compat.py for why.
    compat_config = config.get("compat")
    if compat_config is not None and not isinstance(compat_config, dict):
        raise ValueError(f"models.<name>.compat must be a JSON object; got {compat_config!r}")
    compat = Compat(**compat_config) if compat_config else None

    # The model's own limits. These were hardcoded here — 128000 and 4096 for
    # every model in existence — and no config key reached them, so a 32k local
    # model advertised a 128k window to the compactor and a model that can emit
    # 128k tokens was capped at 4096. Both defaults are kept for a config that
    # states neither, because changing what an existing config resolves to is a
    # separate decision from making the key reachable.
    #
    # `python -m tau_llm.catalog config <provider>/<model>` emits both from
    # models.dev, which is where the real numbers live.
    # ``isinstance(True, int)`` is True in Python, so bools are excluded
    # explicitly — ``"context_window": true`` would otherwise resolve to a
    # one-token window and only show up as a model that can never say anything.
    context_window = config.get("context_window", 128000)
    if (
        isinstance(context_window, bool)
        or not isinstance(context_window, int)
        or context_window <= 0
    ):
        raise ValueError(
            f"models.<name>.context_window must be a positive integer; got {context_window!r}"
        )
    max_tokens = config.get("max_tokens", 4096)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError(f"models.<name>.max_tokens must be a positive integer; got {max_tokens!r}")

    return Model(
        id=model_id,
        name=model_id,
        api=api,
        provider=provider,
        base_url=base_url,
        context_window=context_window,
        max_tokens=max_tokens,
        reasoning=model_reasoning,
        thinking_level_map=config.get("thinking_level_map"),
        reasoning_replay=reasoning_replay,
        strict_reasoning_formats=strict_reasoning_formats,
        grammar_dialect=grammar_dialect,
        extra_body=dict(extra_body),
        server_features=list(server_features),
        # Both of these were reachable on Model and dead through config: the
        # field existed, the provider consumed it, and no user could set it
        # because this seam never carried it. A knob that only a library caller
        # can turn is not a knob a `~/.tau/config.json` user has.
        #
        # `stream: false` is for a gateway that does not implement SSE;
        # `request_timeout` is seconds, and matters because a gateway that drops
        # connections is exactly a timeout question. Both are typed on Model, so
        # a bad value raises here at config load rather than mid-turn.
        stream=config.get("stream", True),
        request_timeout=config.get("request_timeout"),
        # Same story as the two above, one step worse: `temperature` was read by
        # `agent_session` through a `getattr` against a field `Model` did not
        # have, so the config key was accepted, dropped, and replaced by a
        # hardcoded 0.7 on every request. Absent means absent — τ sends no
        # temperature and the endpoint applies its own.
        temperature=config.get("temperature"),
        compat=compat,
    )


class ConfigModelResolver:
    """A ``name -> Model`` resolver over a config ``models`` map (S45), which can
    also say WHICH names it knows (:meth:`model_names`).

    Callable, so it *is* the ``Callable[[str], Model]`` that
    ``AgentSession.set_model_resolver`` takes — the closure this class replaced
    resolved names identically and nothing about that path changes.

    The addition is :meth:`model_names`, and it exists for finding 7 of the Tier B
    review: ``set_model`` takes a config NAME, and until the RPC ``get_models`` verb
    there was nothing on the wire that enumerated them — a host's only route to a
    valid name was reading the child's ``~/.tau/config.json`` out of band, which
    defeats G1 ("a second implementation should be possible from this document plus
    the generated reference", docs/REMOTE-CONTROL.md). A closure cannot answer
    "which names?" without a caller reaching into ``__closure__``, so the map moves
    onto an object that can be asked. This class is the frontend half of that seam;
    ``tau_agent_core.rpc.commands``' ``get_models`` region is the wire half, and it
    REFUSES (``RuntimeError`` → ``INTERNAL_ERROR``) against a resolver that cannot
    enumerate rather than reporting an empty catalogue — an unenumerable resolver
    and a config with no models are different facts and stay different on the wire.
    """

    def __init__(self, models: dict[str, Any]) -> None:
        # A copy: a caller that later mutates its own config map must not silently
        # change what a bound resolver resolves (or advertises) mid-run.
        self._models = dict(models)

    def model_names(self) -> list[str]:
        """Every config model NAME this resolver resolves, sorted.

        Sorted here rather than at the wire so the two callers of the name list —
        this class's own unknown-name message and ``get_models`` — cannot disagree
        about ordering. An empty config map gives an empty list: a real answer
        ("this child has no configured models"), never conflated with "this
        resolver cannot be asked".
        """
        return sorted(self._models)

    def __call__(self, name: str) -> Model:
        """Resolve ``name`` through :func:`build_model_from_config`.

        Fail-Early: an unknown name raises ``KeyError`` (naming the known models)
        rather than fabricating a model — the raise propagates out of
        ``AgentSession.set_model`` unchanged, and the RPC ``set_model`` verb renders
        it as ``INVALID_PARAMS``.
        """
        entry = self._models.get(name)
        if entry is None:
            known = ", ".join(self.model_names()) or "(none configured)"
            raise KeyError(f"unknown model {name!r}; configured models: {known}")
        return build_model_from_config(entry)


def make_model_resolver(models: dict[str, Any]) -> ConfigModelResolver:
    """The ``name -> Model`` resolver a frontend binds onto a live ``AgentSession``
    (``set_model_resolver``) so an extension's ``ctx.set_model(name)`` — and the RPC
    ``set_model`` verb — resolve the NAME through the SAME ``config["models"]`` map
    ``--model`` resolution uses.

    Kept as a factory function (three call sites: ``app.py``, ``headless.py``,
    ``rpc_mode.py``) even though it now just constructs a
    :class:`ConfigModelResolver`; see that class for the resolution contract and for
    why the map lives on an object rather than in a closure.
    """
    return ConfigModelResolver(models)


class Backend(ABC):
    """Abstract base class for LLM backends."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.model = config.get("model", "")
        #: The composed system prompt this backend will send. :class:`TauBackend`
        #: BUILDS it — base text, project context files, tool list — and every
        #: frontend stores THIS on a new session, because the session's first
        #: message is what actually goes on the wire and takes precedence over
        #: anything the AgentSession holds. A backend that composes no prompt
        #: leaves it empty and the session gets no system message.
        self.system_prompt: str = ""

    @abstractmethod
    async def chat(self, messages: list[dict]) -> tuple[str, dict, list[dict]]:
        """Return (assistant_text, usage, new_messages)."""

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[dict],
        callback: Callable[[str], None],
        on_event: Callable[[dict], None] | None = None,
        on_pi_event: Callable[[dict], None] | None = None,
    ) -> tuple[str, dict, list[dict], list[dict]]:
        """Return (assistant_text, usage, new_messages, tool_calls).

        ``on_pi_event`` (optional) is the pi-faithful ``--mode json`` sink:
        every bus event serialized via :func:`tau_event_to_pi_event` (``type``
        discriminator, deduped ``message_end`` carrying usage/model/stop_reason).
        Distinct from ``on_event`` (the legacy ``kind`` widget-lifecycle channel).

        This is the *derive-the-submission-for-me* convenience: the last user
        message in ``messages`` becomes an ordinary interactive
        :class:`~tau_agent_core.submission.Submission`. A frontend that owns its
        own submission record — the TUI, which must stamp ``source``/
        ``submitter``/``multitask_strategy`` itself (docs/SUBMISSION-LIFECYCLE.md
        phase 3) — calls :meth:`stream_submission` instead.
        """

    @abstractmethod
    async def stream_submission(
        self,
        submission: Submission,
        context: list[dict],
        callback: Callable[[str], None],
        on_event: Callable[[dict], None] | None = None,
        on_pi_event: Callable[[dict], None] | None = None,
    ) -> tuple[str, dict, list[dict], list[dict], SubmissionResult]:
        """Admit ``submission`` through the one door and stream the turn it starts.

        Reference: docs/SUBMISSION-LIFECYCLE.md phase 3 — "TUI becomes renderer +
        one source". :meth:`stream_chat` *derives* a submission from the message
        list; this takes the caller's own, so the frontend decides ``source``,
        ``submitter``, ``multitask_strategy`` and the per-submission capabilities
        rather than inheriting whatever the adapter happened to hardcode. The turn
        is admitted EXACTLY ONCE — by :meth:`AgentSession.submit` inside this
        method — and the same normalized ``callback`` / ``on_event`` /
        ``on_pi_event`` channels :meth:`stream_chat` documents drive the render.

        Returns :meth:`stream_chat`'s 4-tuple plus the
        :class:`~tau_agent_core.submission.SubmissionResult` VERBATIM, refusals
        included. A refusal is a typed in-band result (LSP
        ``ApplyWorkspaceEditResult``), and an adapter that folded ``accepted=False``
        into "an empty turn" would be exactly the silent drop this lifecycle exists
        to prevent — the caller shows the reason.
        """

    @abstractmethod
    async def submit_turn(self, submission: Submission, context: list[dict]) -> SubmissionResult:
        """Admit ``submission`` through the one door and AWAIT the turn — no stream.

        Reference: docs/SUBMISSION-LIFECYCLE.md phase 3 (B3-a). The counterpart of
        :meth:`subscribe_render`, and the reason the two exist as a pair: a
        frontend that renders from a persistent bus subscription still has to know
        when ITS OWN submission finished (to re-enable input) and what it was
        answered with (a refusal, or a dispatched command). It does not need a
        second copy of the deltas it has already drawn.

        :meth:`stream_submission` returns those deltas because its callers — the
        SDK-shaped ones, and headless ``run_print``, which prints the transcript
        from the return value — genuinely have no persistent renderer. A frontend
        that does would otherwise be subscribed twice and render everything twice.

        Returns the :class:`~tau_agent_core.submission.SubmissionResult` VERBATIM,
        refusals and ``command`` outcomes included, for the same reason
        :meth:`stream_submission` does: a typed in-band refusal the adapter folded
        into "an empty turn" is exactly the silent drop this lifecycle removes.
        """

    @abstractmethod
    def subscribe_render(
        self,
        handler: RenderHandler,
        *,
        on_orphan: Callable[[str], None] | None = None,
    ) -> RenderRouter:
        """Attach a PERSISTENT renderer to this backend's whole event bus (B3-a).

        Reference: docs/SUBMISSION-LIFECYCLE.md, end of "Phasing". Returns the
        live :class:`RenderRouter`: ``detach()`` stops listening, and
        ``await close_all()`` closes whatever lanes are still streaming. Two calls
        rather than one unsubscribe callable because they are different decisions
        — a screen being torn down wants the first without the second.

        ``handler`` receives :class:`RenderRouter`'s lane-tagged render events —
        ``lane_start``, :class:`TurnStream`'s ``turn_start`` / ``text_delta`` /
        ``reasoning_delta`` / ``tool_call`` / ``tool_result``, and ``lane_end`` —
        for **every** turn this session runs, not only the one the caller happens
        to be awaiting. That is the whole difference: a ``fork`` submission's
        second agent and a turn originated by a bus, timer or extension have no
        awaiting caller at all, so under :meth:`stream_chat`'s signature they were
        not merely unrendered, they were unrepresentable.

        The handler must render other sources' lanes, distinguishably, rather than
        filtering them out — Jupyter's rule, quoted in :class:`RenderRouter`.

        ``on_orphan`` receives a reason string for an event that named no open
        lane. Fail-Early: those are real (an unstamped ``continue_conversation``
        turn) and a renderer that dropped them in silence would look exactly like
        one that had stopped working.
        """

    @abstractmethod
    async def submit_command(self, submission: Submission) -> SubmissionResult:
        """Admit a submission whose text the frontend has already resolved to a command.

        Reference: docs/SUBMISSION-LIFECYCLE.md ``submit()`` step 3 (phase 3, B2-b).
        The SAME door as :meth:`stream_submission` — ``AgentSession.submit`` — with
        none of the streaming plumbing, because a dispatched command runs no model
        call and emits no deltas to render. Splitting it out rather than reusing
        ``stream_submission`` keeps the renderer from opening an exchange, a
        display lock, and a bus subscription for a turn that will not happen.

        The submission must carry ``expand_commands=True``; ``submit()`` is the
        authority on dispatch and will simply run a turn if it does not.

        Returns the :class:`~tau_agent_core.submission.SubmissionResult` VERBATIM.
        ``result.command`` is the typed outcome the caller must act on — perform it
        if ``performer == "frontend"``, render ``output`` if ``performer ==
        "core"`` — and ``result.command is None`` means no command was dispatched
        after all (an ``input`` hook transformed the text), which the caller must
        NOT treat as "nothing happened".
        """

    @abstractmethod
    def abort(self) -> None:
        """Cooperatively abort the in-flight turn (LLM stream + tool loop).

        Safe to call when nothing is running. The TUI binds this to Esc so a
        long response can be cancelled mid-stream; the active ``stream_chat``
        returns with whatever streamed so far."""

    @abstractmethod
    async def load_extensions(
        self,
        explicit_paths: list[str] | None = None,
        *,
        discover: bool = True,
        user_dir: str | None = None,
        extensions_config: dict[str, dict[str, Any]] | None = None,
    ) -> LoadExtensionsResult:
        """Load file-path extensions into this backend's live session (E5 §2.2).

        Both run paths (headless ``run_print`` and the TUI ``Parley``) load
        extensions through this seam after building the backend, so a file
        extension's hooks fire in the same ``AgentSession`` the loop runs on.
        ``extensions_config`` (S40) is the per-extension config map handed to each
        extension's ``api.config``, keyed by file stem. Returns the
        :class:`LoadExtensionsResult`; the caller surfaces its ``errors`` (an
        explicit ``-e`` failure raises out of here instead — Fail-Early)."""


class TauBackend(Backend):
    """tau-agent-core backend adapter.

    Wraps tau-agent-core's AgentSession to provide Parley-compatible
    chat/stream_chat interfaces.
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)

        # Build a tau-agent-core model config from the Parley config. The Model
        # construction is shared with make_model_resolver (S45) via
        # build_model_from_config so ctx.set_model rebuilds a Model identically.
        model_id = config.get("model", "gpt-4")
        # NO default. The provider raises "No API key for provider: …" on a
        # falsy key (openai.py), and that raise is the documented behaviour —
        # but it only ever fired if a missing key actually reached it. This line
        # used to substitute the "not-needed" sentinel, which is truthy, so a
        # config entry with no api_key sailed past the gate and sent that string
        # to whatever base_url resolved to (default: api.openai.com), producing a
        # 401 from a third party instead of a startup error naming the real
        # problem. A local server still opts in by writing "not-needed"
        # explicitly, exactly as the shipped template does.
        api_key = config.get("api_key")

        self.model_name = model_id
        # Thinking/reasoning level. The CLI's --thinking flag (or a model:level
        # suffix) lands here as config["thinking"]; a model config entry may also
        # declare a default "thinking" level and a "thinking_level_map". A
        # non-"off" level asserts the model is reasoning-capable (mirrors pi
        # model-resolver.ts:496), so reasoning_effort is actually sent; an
        # explicit config "reasoning": true also enables it. None/"off" → no
        # reasoning requested.
        thinking_level = config.get("thinking")
        reasoning_arg = thinking_level if thinking_level and thinking_level != "off" else None

        model = build_model_from_config(config)
        # Kept for the tree-browser's summarizer (navigate_tree, §3.3): the
        # branch-summary ``complete_simple`` call needs the model + api key directly,
        # not via the AgentSession loop.
        self._model = model
        self._api_key = api_key

        # Discover tools from config. Defaults to all built-in tools, minus the
        # --exclude-tools denylist (pi excludeTools; E5 §2.3 / S28) — see
        # ``resolve_tool_names``, which the empty chat pane shares so the list the
        # user reads is the list this constructs. Extension-registered tools merge
        # in later (``AgentSession._build_turn_tools``) and are NOT subject to the
        # denylist — pi's excludeTools targets the built-in registry.
        tool_names = resolve_tool_names(config)
        if tool_names:
            tools = _resolve_tools(tool_names)
        else:
            tools = []

        # The system prompt is BUILT, not copied out of the config (0.9.3 §1).
        #
        # This line used to be `config.get("system_prompt", "")`, and that one
        # `.get` is why nothing a user wrote in an AGENTS.md ever reached a
        # model: `_build_system_prompt` — τ's base prompt AND its context-file
        # discovery — lives behind `create_agent_session`, which NOTHING in this
        # package calls. TauBackend constructs `AgentSession` directly, so the
        # TUI and every headless run sent whatever string the config held, and
        # on a default install (no `system_prompt` key) that string was `""`.
        # Dropping the key from tau_default_config.json was necessary but not
        # sufficient; this is the other half.
        #
        # A configured `system_prompt` still wins over the base text — it is
        # passed as `custom_prompt`, which replaces that text and nothing else,
        # so project context files compose with it instead of being switched off
        # by it (pi system-prompt.ts:46-62).
        # ``cwd`` is left to default to ``os.getcwd()`` — the same directory the
        # TUI stamps on a session and the same one the tools run in.
        #
        # ``--append-system-prompt`` sections augment the BASE TEXT, not the
        # composed whole — they land ahead of the project context and the tool
        # list, which is pi's placement (system-prompt.ts:48). Applied HERE, the
        # one point every frontend's model config passes through, so the TUI,
        # print mode and RPC mode compose identically instead of each folding
        # the sections in on its own before the builder ever sees them.
        custom_prompt = config.get("system_prompt") or None
        append_sections = config.get("append_system_prompt")
        if append_sections:
            custom_prompt = append_system_prompt(
                custom_prompt or BASE_SYSTEM_PROMPT, list(append_sections)
            )
        self.system_prompt = _build_system_prompt(
            tools=tools,
            custom_prompt=custom_prompt,
            no_context_files=bool(config.get("no_context_files", False)),
            # ``{{model}}``: the id that goes on the wire, not the config key it
            # was reached by — a prompt saying which model it is should say the
            # thing the server sees.
            model=config.get("model") or None,
        )

        # The resolved tool-suppression policy (``--no-tools`` → "all",
        # ``--no-builtin-tools`` → "builtin", absent → None), set by
        # ``headless.resolve_no_tools`` at the argv boundary and carried on the
        # model config next to ``exclude_tools``. Both flags already emptied
        # ``tool_names`` above; forwarding this is what makes "all" ALSO withhold
        # extension-registered tools, and it is the only difference between the
        # two flags. An unrecognised value raises in AgentSession rather than
        # degrading to "no suppression".
        no_tools = config.get("no_tools")

        # The turn ceiling, resolved upstream (``--max-turns`` > the model entry >
        # config.json's top-level ``max_turns``) by ``resolve_model_config`` for
        # ``tau -p`` and by ``Parley._apply_run_config`` for the TUI. ``None`` here
        # means nobody stated one, and ``AgentSession`` passes that through to
        # ``AgentLoopConfig``, whose default is no ceiling. Nothing in this class
        # invents a number: the 50 that used to bound every run lived in
        # ``AgentLoopConfig`` and could not be reached from either frontend.
        max_turns = config.get("max_turns")

        # Forward the configured API key to the session -> agent loop ->
        # provider. The provider requires a truthy key (Fail-Early); local
        # servers use the "not-needed" sentinel, which is passed through as-is.
        # (Previously this was stashed in an unused self._api_key and dropped,
        # so a real-OpenAI key from config never reached the provider.)
        #
        # SessionLog. The AgentSession is constructed against a scratch
        # ``InMemorySessionLog``, but the TUI immediately rebinds it onto its LIVE
        # ``session_store.Session`` via :meth:`bind_session_log` (E3-ctx / D3), so on
        # the interactive path this session is the SOLE persister — the turn's user +
        # assistant/tool messages append through the one on-disk log the TUI reads
        # back, and the TUI no longer double-writes them itself. Callers that own a
        # separate persistence-of-record and never rebind (headless ``run_print``,
        # the cost/json backend tests, SDK-style use) keep the scratch log: for them
        # ``prompt()`` persists to a log that is never flushed or read (context
        # arrives via ``stream_chat``'s ``messages`` argument), and they append to
        # their own ``Session`` themselves.
        #
        # Auto-compaction is disabled here: the caller's own message list — not this
        # log — is the context sent to the model, so a post-turn auto-compaction
        # would do useless work (and fire a slow summary LLM call every turn once it
        # crossed the threshold). The TUI compacts explicitly via ``/compact`` →
        # ``compact_messages``, which works with auto-compaction off.
        self.agent_session = AgentSession(
            session_log=InMemorySessionLog(),
            model=model,
            system_prompt=self.system_prompt,
            tools=tools,
            api_key=api_key,
            reasoning=reasoning_arg,
            compaction_settings=CompactionSettings(enabled=False),
            # H8 capability declaration (``--bus``, or ``"bus_available": true``
            # on the model entry). Until this was threaded, NOTHING in this
            # package ever set it, so ``AgentSession``'s default of False stood
            # for every TUI and print-mode run and the loader refused every
            # extension declaring TOUCHES_BUS — ``nats_bus`` was loadable only
            # from a hand-written script. Default stays False: reaching a bus is
            # a capability the operator grants, never one a run assumes.
            bus_available=bool(config.get("bus_available", False)),
            no_tools=no_tools,
            max_turns=max_turns,
        )

    def bind_session_log(self, session_log: SessionLog) -> None:
        """Point the AgentSession at the caller's authoritative ``SessionLog``.

        The TUI owns a live ``session_store.Session`` that is swapped on new-chat /
        clear / resume; each time it becomes current, the TUI rebinds this backend's
        AgentSession onto it so ``prompt()`` / ``compact`` / ``navigate`` persist
        through that one on-disk log (E3-ctx / D3 — AgentSession becomes the sole
        persister, retiring the app-side ``append_message`` double-write). The
        scratch ``InMemorySessionLog`` created in ``__init__`` is discarded on the
        first bind; a backend that is never bound (headless, tests) keeps it.
        """
        self.agent_session.session_log = session_log

    def abort(self) -> None:
        """Abort the current turn by tripping the AgentSession's abort signal.

        The signal is threaded down to the provider (agent_loop forwards it to
        ``stream_simple``), which polls it per SSE line and stops the stream — so
        an in-flight completion ends promptly instead of draining in full."""
        self.agent_session.abort()

    def set_ui_delegate(self, delegate: Any) -> None:
        """Forward a front-end UI delegate to the wrapped ``AgentSession`` (E5 §4 / S33).

        The app hands in a delegate whose ``notify`` paints on the Textual screen;
        this routes every loaded extension's ``api.ui.notify(...)`` there instead of
        the headless stderr sink. Delegates to :meth:`AgentSession.set_ui_delegate`,
        which sets it on the one shared :class:`ExtensionContext`.
        """
        self.agent_session.set_ui_delegate(delegate)

    def set_extension_record_sink(self, sink: Any) -> None:
        """Forward a headless JSON record sink to the wrapped session (E7 §3 / S49 — G10).

        The ``--mode json`` headless path hands in a writer that serializes each
        extension record to one stdout line; this routes every loaded extension's
        ``api.ui.notify(...)`` there instead of the headless stderr sink, so a parent
        reading the child stream can see the child's extension activity. Delegates to
        :meth:`AgentSession.set_extension_record_sink`.
        """
        self.agent_session.set_extension_record_sink(sink)

    def set_headless_ui_defaults(self, policy: dict[str, str]) -> None:
        """Forward the headless dialog-answer policy to the session (E7 §3 / S48).

        The headless run path resolves ``--ui-defaults`` / config ``"ui_defaults"``
        and calls this so a dialog opened by a loaded extension auto-answers only
        for the opted-in methods; every other headless dialog raises
        (Fail-Early, D-E6-2). Delegates to
        :meth:`AgentSession.set_headless_ui_defaults`.
        """
        self.agent_session.set_headless_ui_defaults(policy)

    async def emit_session_start(self, reason: str = "startup") -> None:
        """Fire the ``session_start`` lifecycle hook on the wrapped session (S41).

        Delegates to :meth:`AgentSession.emit_session_start`; the frontends call
        this after :meth:`load_extensions` so a loaded extension's ``session_start``
        handler runs with its registration in place (state reconstruction, watchers).
        """
        await self.agent_session.emit_session_start(reason)

    async def emit_session_shutdown(self, reason: str = "quit") -> None:
        """Fire the ``session_shutdown`` lifecycle hook on the wrapped session (S41).

        Delegates to :meth:`AgentSession.emit_session_shutdown`; the frontends call
        this on end-of-runtime (TUI quit, headless completion, SIGINT/SIGTERM) so an
        extension can run teardown side effects (exit commits, stopping watchers).
        """
        await self.agent_session.emit_session_shutdown(reason)

    async def load_extensions(
        self,
        explicit_paths: list[str] | None = None,
        *,
        discover: bool = True,
        user_dir: str | None = None,
        extensions_config: dict[str, dict[str, Any]] | None = None,
        collect_explicit_errors: bool = False,
    ) -> LoadExtensionsResult:
        """Load file-path extensions into the wrapped ``AgentSession`` (E5 §2.2).

        Delegates to :meth:`AgentSession.load_extensions`, which binds each
        extension to this session's live :class:`ExtensionRunner` so its mutating
        hooks fire in the loop this backend drives. ``extensions_config`` (S40) is
        forwarded so each extension's ``api.config`` receives its config slice.
        ``collect_explicit_errors=True`` (passed by the TUI) demotes an explicit
        ``-e`` failure to a collected ``result.errors`` entry instead of raising, so
        a partial load still returns the extensions that DID load (headless leaves
        it False to keep the Fail-Early abort).
        """
        return await self.agent_session.load_extensions(
            explicit_paths,
            discover=discover,
            user_dir=user_dir,
            extensions_config=extensions_config,
            collect_explicit_errors=collect_explicit_errors,
        )

    def list_managed_extensions(self) -> list[tuple[str, bool]]:
        """Every managed file extension as ``(path, enabled)`` (E10 §6 / S70).

        Delegates to :meth:`AgentSession.list_managed_extensions`; the ``/extensions``
        listing reads this so a runtime-disabled extension is shown as disabled.
        """
        return self.agent_session.list_managed_extensions()

    async def disable_extension(self, path: str) -> ExtensionActionResult:
        """Runtime-disable a loaded extension (E10 §6 / S70).

        Delegates to :meth:`AgentSession.disable_extension`, which fires the
        extension's ``session_shutdown`` teardown, then detaches its hooks + registry
        entries. Returns the reportable :class:`ExtensionActionResult`.
        """
        return await self.agent_session.disable_extension(path)

    async def enable_extension(self, path: str) -> ExtensionActionResult:
        """Runtime-enable a disabled extension (E10 §6 / S70).

        Delegates to :meth:`AgentSession.enable_extension` (re-invoke ``register`` +
        ``session_start``).
        """
        return await self.agent_session.enable_extension(path)

    async def reload_extension(self, path: str) -> ExtensionActionResult:
        """Runtime-reload an extension from disk (E10 §6 / S70).

        Delegates to :meth:`AgentSession.reload_extension` (teardown → re-import →
        re-register → ``session_start``). A broken file raises, per Fail-Early.
        """
        return await self.agent_session.reload_extension(path)

    def get_extension_commands(self) -> list[tuple[str, str]]:
        """List extension-registered slash commands as ``(name, description)`` (S35).

        Delegates to :meth:`AgentSession.get_extension_commands` — the app's
        command palette reads this to surface extension commands alongside its
        built-ins.
        """
        return self.agent_session.get_extension_commands()

    def get_extension_command_args(self, name: str) -> str | None:
        """The declared argument placeholder for command ``name`` (E7 §3 / S51).

        Delegates to :meth:`AgentSession.get_extension_command_args`. The palette
        reads this to decide whether a command's entry must open the S47 input modal
        to collect an arg string before dispatch (parity with typed ``/name args``).
        """
        return self.agent_session.get_extension_command_args(name)

    def get_extension_shortcuts(self) -> list[tuple[str, str, str, str]]:
        """List extension-registered key shortcuts as ``(key, command, args, desc)`` (S69).

        Delegates to :meth:`AgentSession.get_extension_shortcuts`. The app's ``ctrl+e``
        chord menu and command palette read this to surface extension shortcuts and
        dispatch each one's command through :meth:`run_extension_command`.
        """
        return self.agent_session.get_extension_shortcuts()

    async def run_extension_command(self, name: str, args: str = "") -> ExtensionCommandResult:
        """Run an extension-registered slash command (S35; output channel S46).

        Delegates to :meth:`AgentSession.run_extension_command`, forwarding the
        :class:`ExtensionCommandResult` (``handled`` + the handler's ``output``) so
        the caller can both fall through on an unknown command and render a handled
        command's returned value as display-only chrome.
        """
        return await self.agent_session.run_extension_command(name, args)

    async def compact_messages(self, messages: list[dict]) -> list[dict] | None:
        """Compact the conversation the TUI sends, returning the shortened list.

        Delegates to the AgentSession's compaction engine. Operates on the
        caller's ``messages`` (the TUI's authoritative ``current_chat.messages``,
        which ``stream_chat`` passes as the LLM context) — not the parallel
        session-manager path. Returns None when there is nothing to compact.
        """
        return await self.agent_session.compact_messages(messages)

    async def navigate_tree(
        self,
        session: SessionLog,
        target_id: str,
        *,
        summarize: bool = False,
        custom_instructions: str | None = None,
    ) -> list[dict]:
        """Move the live session's cursor to ``target_id`` and return the new context.

        Typed to the ``SessionLog`` Protocol, not the concrete file ``Session``: this
        method only ever touches ``cursor`` / ``entries()`` / ``append_navigate`` /
        ``append_branch_summary``, all four of which are on the Protocol. Any
        conforming store — in-memory, file, or database-backed — works here unchanged.

        Port of pi's ``AgentSession.navigateTree`` (agent-session.ts:2708). The live
        coding-agent ``Session`` is passed in (the TUI owns it, §2.6): this method
        operates on IT, not on the scratch ``InMemorySessionLog`` the AgentSession runs
        against. Two modes (§3.1):

        - ``summarize=False`` → append a ``navigate`` entry (zero LLM calls). The
          abandoned branch drops out of context via the ``parentId`` walk but stays on
          disk (append-only, still browsable).
        - ``summarize=True`` → summarize the abandoned subtree (``ConversationTree
          .subtree_text(target_id)`` → ``summarize_branch``, Fail-Early: raises on a
          failed/empty summary) and append a ``branch_summary`` parented at the branch
          point (Decision 5, fix 1). Mode-3 ``custom_instructions`` reach the summarizer
          SYSTEM prompt.

        Returns ``ConversationTree.context_for(cursor)`` — the flat message list the TUI
        swaps into ``self.messages`` and re-renders (reusing the compaction path, §3.4).
        """
        from tau_agent_core.conversation_tree import ConversationTree
        from tau_agent_core.session_manager import summarize_branch

        old_leaf = session.cursor
        if target_id == old_leaf:
            # No-op (pi navigateTree:2716) — already at the target.
            return ConversationTree(session.entries(), session.cursor).context_for()

        if summarize:
            branch_text = ConversationTree(session.entries(), old_leaf).subtree_text(target_id)
            summary, summary_usage = await summarize_branch(
                branch_text,
                self._model,
                api_key=self._api_key,
                custom_instructions=custom_instructions,
            )
            # A real LLM call, outside the agent loop — bill it to the session rather
            # than letting it vanish (tau_agent_core.usage).
            self.agent_session.record_side_usage(summary_usage)
            session.append_branch_summary(summary, target_id)
        else:
            session.append_navigate(target_id)

        return ConversationTree(session.entries(), session.cursor).context_for()

    def elide_span(self, session: SessionLog, anchor_id: str, first_kept_id: str) -> list[dict]:
        """Fold a span out of the live session's context and return the new context.

        The TUI half of W3 (NODE-ADDRESSABLE-AGENTS.md): ``elide`` is the
        summary-less generalization of the compaction anchor, and until now nothing
        outside the core could create one. Sibling of :meth:`navigate_tree` — same
        seam (the app owns the modals and the re-render, the backend performs the
        session-tree mutation and hands back ``context_for``), same ``SessionLog``
        typing, so any conforming store works. **Synchronous**, unlike
        ``navigate_tree``: there is no summary, therefore no model call and nothing
        to await. An ``async def`` with no ``await`` would advertise an I/O boundary
        this operation does not have.

        Two ids, because an elide is not a branch point. ``anchor_id`` is where the
        fold jumps FROM — the elide entry is appended as its child, so the anchor
        becomes the end of the kept region and the new tip. ``first_kept_id`` is
        where it jumps TO: ``ConversationTree._active_path_entries`` emits the
        anchor, then the anchor's ancestors from ``firstKeptId`` onward. Everything
        on that path BEFORE ``firstKeptId`` is the elided span.

        **``first_kept_id`` must therefore be the anchor itself or one of its
        ancestors, never a descendant.** That direction is not a style choice, it is
        what the fold's forward scan over ``path[:anchor_idx]`` can reach: a
        boundary the scan never finds leaves ``found`` False forever, so the fold
        emits the anchor and NOTHING else — an empty context, silently, with no
        error. Hence the check here, before either append: ``append_elide``'s own
        Fail-Early only proves the id names *an* entry, not that it names a
        reachable one, and the unreachable case is the more damaging of the two.

        Refusing a no-op elide is the other check. An elide whose span is empty
        (``first_kept_id`` already the first entry the fold keeps) persists a node
        that changes nothing about the context it was created to change — the
        silent-no-op anti-pattern, indistinguishable to the user from a successful
        fold. The core deliberately permits it (an anchor on a root-level entry is
        a pinned contract case); this policy layer, where a human just asked for a
        span to disappear, does not.

        Nothing is erased: the navigate/elide pair are appends like any other, and
        every entry the fold now skips is still in ``entries()`` (Decision 7 / T5).

        Returns ``ConversationTree.context_for(cursor)`` — the flat message list the
        TUI swaps into ``self.messages`` and re-renders, exactly as
        :meth:`navigate_tree` does.

        Raises:
            ValueError: an unknown anchor or resume point, a resume point that is
                not on the anchor's path, or a span that would hide nothing.
        """
        from tau_agent_core.conversation_tree import ConversationTree

        entries = session.entries()
        known = {e["id"] for e in entries}
        if anchor_id not in known:
            raise ValueError(f"elide anchor {anchor_id!r} not found")
        if first_kept_id not in known:
            raise ValueError(f"elide resume point {first_kept_id!r} not found")

        tree = ConversationTree(entries, session.cursor)
        path_ids = [e["id"] for e in tree.path(anchor_id)]
        if first_kept_id not in path_ids:
            raise ValueError(
                f"elide resume point {first_kept_id!r} is not on the path to anchor "
                f"{anchor_id!r} — it must be the anchor itself or one of its ancestors. "
                "The fold scans only the anchor's ancestors for the boundary, so a "
                "resume point it cannot reach would drop the ENTIRE context, silently."
            )

        # What the fold would lose: the entries it keeps at the anchor today, minus
        # the kept region (firstKeptId…anchor) it would keep afterwards. Computed
        # against context_entries, not the raw path, so a span already hidden by an
        # earlier anchor is not counted twice.
        kept = set(path_ids[path_ids.index(first_kept_id) :])
        hidden = [e for e in tree.context_entries(anchor_id) if e["id"] not in kept]
        if not hidden:
            raise ValueError(
                f"elide from anchor {anchor_id!r} resuming at {first_kept_id!r} would hide "
                "nothing — the resume point is already the first entry the fold keeps"
            )

        if session.cursor != anchor_id:
            # Put the leaf on the anchor so the elide parents there (append_elide
            # appends at the current leaf). Same append the "no summary" tree mode
            # makes; skipped entirely when the anchor already IS the tip, which is
            # the common "fold my history and keep going" case.
            session.append_navigate(anchor_id)
        # TREE-BROWSER-AS-EDITOR.md §8.2/§8.3, required keywords per §11.3. ``hidden``
        # is the span this elide removes — already computed above for the no-op
        # refusal and, until now, thrown away immediately afterwards, which is §8.1's
        # pattern exactly. ``coveredTokens`` is the figure §8.2 names as the one an
        # elide records nowhere and no reader can recompute later; the count is
        # passed alongside it so both halves describe the same list rather than one
        # being re-derived from the tree on a different basis.
        #
        # The frame is looked up from ``anchor_id``, the entry this elide parents at,
        # not from the backend's live AgentSession: the browser aims an elide at an
        # arbitrary historical anchor, which may sit two ``set_model`` swaps behind
        # the session's current spec, and the frame that governed the covered span is
        # the one on that anchor's ancestor chain.
        session.append_elide(
            first_kept_id,
            covered_entries=len(hidden),
            covered_tokens=estimate_span_tokens(hidden),
            agent_spec_id=agent_spec_in_force(entries, anchor_id),
        )

        return ConversationTree(session.entries(), session.cursor).context_for()

    async def rollback_turn(self, text: str) -> SubmissionResult:
        """Abort the in-flight turn, un-path what it produced, and run ``text`` instead.

        The TUI half of ``multitask_strategy="rollback"``
        (docs/SUBMISSION-LIFECYCLE.md decision 2). Everything this method does
        happens inside :meth:`AgentSession.submit`: signal the running turn's abort,
        wait for its slot, ``append_navigate`` back to the leaf THAT turn recorded at
        its own admission, and run this submission from there. Deliberately a
        four-line delegation rather than a TUI-side navigate — one implementation of
        "un-path a turn" is the entire point of the seam, and the stale-target guard
        that makes it safe (``_current_turn_token``) lives on the other side of it.

        **No ``context=``.** :meth:`stream_chat` passes the TUI's working message
        list because that list IS the conversation on the ordinary path. Here it is
        the wrong list by construction: it still holds the messages of the turn being
        rolled back, so sending it would re-submit to the model exactly what the
        rollback just removed from the path. With ``context`` omitted the session
        folds its own log — which, after the navigate, is the pre-turn state. That
        also makes ``bind_session_log`` a precondition rather than a nicety: on the
        live TUI path the AgentSession is bound to the same ``Session`` the app
        renders (E3-ctx / D3), so "the log" and "what the user sees" are one thing.

        ``allow_user_input=True`` and ``submitter="human"`` for the same reason
        :meth:`AgentSession.prompt` asserts them: a person at the terminal pressed
        the key, so a hook running under this turn may ask that person a question.
        ``expand_commands`` stays ``False`` now that B2-b has given the flag a
        consumer, and that is a choice rather than an omission: this submission's
        job is to run a MODEL turn in place of the one it just aborted, so
        dispatching a command instead would leave the conversation un-pathed with
        nothing running where the discarded turn was. The app refuses a leading "/"
        before it gets here, with that reason.

        Returns the :class:`SubmissionResult` VERBATIM, refusals included. A rollback
        can be legitimately refused — the stale-target guard returns
        ``accepted=False`` with a ``rejection_reason`` when another submission was
        admitted and completed while this one waited for the slot — and a typed
        refusal the adapter swallowed would be exactly the silent drop the submission
        lifecycle exists to prevent. The caller shows the reason.
        """
        return await self.agent_session.submit(
            Submission(
                text=text,
                source="interactive",
                submitter="human",
                submission_id=uuid4().hex,
                multitask_strategy="rollback",
                allow_user_input=True,
            )
        )

    async def submit_turn(self, submission: Submission, context: list[dict]) -> SubmissionResult:
        """Admit ``submission`` through :meth:`AgentSession.submit` and await the turn.

        A four-line delegation, like :meth:`rollback_turn` and
        :meth:`submit_command`, and for the same reason: the door is
        ``AgentSession.submit`` and nothing here is allowed to have a second
        opinion about admission. What makes this method distinct from
        :meth:`stream_submission` is what it does NOT do — it opens no bus
        subscription and collects no deltas, because a caller that uses this has
        already attached a persistent renderer via :meth:`subscribe_render` and
        would otherwise draw every token twice.

        See :meth:`Backend.submit_turn` for the contract.
        """
        return await self.agent_session.submit(submission, context=context)

    def subscribe_render(
        self,
        handler: RenderHandler,
        *,
        on_orphan: Callable[[str], None] | None = None,
    ) -> RenderRouter:
        """Wire a :class:`RenderRouter` across all five of this session's channels.

        The ``AgentEvent`` stream carries the turns; ``submission_start`` /
        ``submission_end`` carry the submission spans that bracket them (which
        ``agent_start``/``agent_end`` cannot — a followUp re-entry runs a second
        loop inside one submission); ``branch_event`` carries a forked sub-agent's
        events, which until now had no consumer anywhere, which is the concrete
        sense in which "a fork today is unobservable"; and ``branch_end`` is that
        sub-agent's own span close, emitted from a ``finally`` so a branch that
        raised or was cancelled cannot leave its lane open forever.

        See :meth:`Backend.subscribe_render` for the contract.
        """
        router = RenderRouter(handler, on_orphan=on_orphan)
        unsubs = [
            self.agent_session.subscribe(router.on_agent_event),
            self.agent_session.subscribe_channel("submission_start", router.on_submission_start),
            self.agent_session.subscribe_channel("submission_end", router.on_submission_end),
            self.agent_session.subscribe_channel("branch_event", router.on_branch_event),
            self.agent_session.subscribe_channel("branch_end", router.on_branch_end),
        ]

        def detach() -> None:
            for unsub in unsubs:
                unsub()

        router.bind_detach(detach)
        return router

    async def submit_command(self, submission: Submission) -> SubmissionResult:
        """Admit a command submission through :meth:`AgentSession.submit` (B2-b).

        A four-line delegation for the same reason :meth:`rollback_turn` is one: the
        dispatch decision, the ``expand_commands`` gate, and the ``input`` hook chain
        all live on the other side of this seam, and duplicating any of them here
        would give the TUI a second, drifting answer to "what is a command".

        See :meth:`Backend.submit_command` for the contract the caller must honour —
        in particular that ``result.command is None`` is not "nothing happened".
        """
        return await self.agent_session.submit(submission)

    async def _extract_last_user_message(self, messages: list[dict]) -> str:
        """Extract the last user message text from a Parley messages list."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    return "\n".join(text_parts)
        return ""

    async def chat(self, messages: list[dict]) -> tuple[str, dict, list[dict]]:
        """Send a chat completion via tau-agent-core's AgentSession.

        Passes all messages as context so the agent loop has full
        conversation history (system prompt, prior assistant/tool results).
        Returns (assistant_text, usage, new_messages).
        """
        # Extract the last user message text
        last_user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_message = content
                elif isinstance(content, list):
                    # Multi-modal: extract text blocks
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    last_user_message = "\n".join(text_parts)
                break

        if not last_user_message:
            return "", {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}, []

        # Send through the agent loop with full conversation context
        # so the model sees prior tool calls and results
        result_messages = await self.agent_session.prompt(last_user_message, context=messages)

        # Extract the last assistant message text
        assistant_content = ""
        for msg in reversed(result_messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    assistant_content = "\n".join(text_parts)
                elif isinstance(content, str):
                    assistant_content = content
                break

        # Approximate token count
        prompt_tokens = sum(len(m.get("content", "")) // 4 for m in messages)
        completion_tokens = len(assistant_content) // 4 if assistant_content else 0

        return (
            assistant_content,
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            result_messages,
        )

    async def stream_chat(
        self,
        messages: list[dict],
        callback: Callable[[str], None],
        on_event: Callable[[dict], None] | None = None,
        on_pi_event: Callable[[dict], None] | None = None,
    ) -> tuple[str, dict, list[dict], list[dict]]:
        """Derive an ordinary interactive submission from ``messages`` and stream it.

        The convenience half of the seam, for callers that have a message list and
        no submission record of their own — the SDK-shaped backend tests, and any
        embedder holding a conversation rather than a submission. The last user
        message becomes the submission text and everything else is
        :meth:`stream_submission`'s job, constructed here so there is ONE admission
        per call rather than one derived here and another derived a layer down.

        **No frontend in this repo routes through here any more.** The TUI owns its
        record (B2-a) and headless ``run_print`` owns its own since B2-c, which is
        the point of phase 3: a frontend states what its input MEANS instead of
        inheriting whatever an adapter hardcoded. This method is kept because the
        message-list contract is a genuinely different (and older) shape — it is the
        SDK's, not a frontend's.

        ``expand_commands=False``, which is where this DIVERGES from
        :meth:`AgentSession.prompt` since B2-b, deliberately: this method returns a
        4-tuple with no slot for a
        :class:`~tau_agent_core.commands.CommandOutcome`, so a dispatched command
        would be dropped on the floor — the silent no-op the lifecycle exists to
        remove. ``prompt()`` faces the same problem and answers it by raising; this
        one cannot raise instead, because a caller holding a message list has no way
        to know a command is in it before calling and no channel to receive the
        outcome after. So it declines to dispatch at all, which leaves a leading
        ``/`` as literal prompt text — the same thing an unregistered ``/…`` has
        always been. A caller that wants dispatch owns its submission and calls
        :meth:`stream_submission` or :meth:`submit_command`.

        Returns (assistant_text, usage, new_messages, tool_calls) — unchanged. The
        :class:`SubmissionResult` is dropped on this path *because* the derived
        submission's strategy is ``enqueue``, which waits rather than refusing; a
        caller that needs to see a refusal owns its submission and calls
        :meth:`stream_submission`.
        """
        # Extract the last user message text (same as chat()). Reuses the helper
        # that already existed for exactly this and had lost its only caller,
        # rather than keeping a third hand-inlined copy of the walk.
        last_user_message = await self._extract_last_user_message(messages)
        if not last_user_message:
            return "", {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}, [], []

        text, usage, new_messages, tool_calls, _result = await self.stream_submission(
            Submission(
                text=last_user_message,
                source="interactive",
                submitter="human",
                submission_id=uuid4().hex,
                multitask_strategy="enqueue",
                expand_commands=False,
                allow_user_input=True,
            ),
            messages,
            callback,
            on_event=on_event,
            on_pi_event=on_pi_event,
        )
        return text, usage, new_messages, tool_calls

    async def stream_submission(
        self,
        submission: Submission,
        context: list[dict],
        callback: Callable[[str], None],
        on_event: Callable[[dict], None] | None = None,
        on_pi_event: Callable[[dict], None] | None = None,
    ) -> tuple[str, dict, list[dict], list[dict], SubmissionResult]:
        """Admit ``submission`` via :meth:`AgentSession.submit` and stream the turn.

        Passes ALL of ``context`` as the LLM context so the agent loop has full
        conversation history (system prompt, prior assistant/tool results).
        Returns (assistant_text, usage, new_messages, tool_calls, result).

        Two consumer channels are driven from the agent-core event bus:

        - ``callback(delta)`` receives raw assistant text fragments, for the
          streaming-text widget (unchanged contract).
        - ``on_event(event)`` (optional) receives *normalized, ordered*
          lifecycle events so the caller can mount/resolve widgets in true
          arrival order. Event shapes (all dicts with a ``"kind"`` key)::

              {"kind": "turn_start", "turn_index": int}
              {"kind": "text_delta", "delta": str}
              {"kind": "tool_call", "id": str, "name": str, "arguments": dict}
              {"kind": "tool_result", "id": str, "name": str,
               "result": str, "is_error": bool}

        Tool widgets are driven off ``tool_execution_start`` /
        ``tool_execution_end`` (which carry name/args/result directly),
        NOT off ``message_end`` toolCall blocks — the agent loop emits
        ``message_end`` twice per tool-bearing turn, so consuming it for
        rendering would duplicate. ``message_end`` is used only to harvest
        ``tool_calls_info`` for chat persistence (deduplicated by id).
        """
        # ONE normalizer for this lane, shared with the persistent multi-lane
        # renderer (:class:`RenderRouter`) so both paths draw a turn from exactly
        # the same rules — the suffix diff, the message_end dedup, the usage sum.
        # Before B3-a this logic lived only in a closure here, which is why a turn
        # this method was not awaiting could not be rendered at all.
        stream = TurnStream()

        # message_end only fires for the LOOP's completions. An exchange can also
        # trigger an AUTO-COMPACTION, whose summarizer reads the entire
        # conversation, and any extension ctx.complete() calls. Those go through
        # `complete_simple`, which emits no events — so summing message_end alone
        # produced a cost that was confidently understated, most of all for the one
        # call the user never asked for. The session's side ledger is cumulative, so
        # this exchange's share of it is the before/after delta.
        side_usage_before = self.agent_session.side_usage

        def capture_event(event: AgentEvent) -> None:
            """Normalize agent-core events into ordered widget-lifecycle events.

            Text deltas drive ``callback`` (and a ``text_delta`` structured event);
            tool execution drives ``tool_call`` / ``tool_result`` structured events.
            ``turn_start`` resets the per-turn text accumulator and signals the
            caller to open a fresh pending slot, which is what preserves true
            arrival order (assistant text after a tool call ends up *after* it, not
            pinned above it).
            """
            for structured in stream.feed(event):
                if structured["kind"] == "text_delta":
                    callback(structured["delta"])
                if on_event is not None:
                    on_event(structured)

        def pi_capture(event: AgentEvent) -> None:
            """Forward each bus event to the pi-faithful ``--mode json`` sink.

            Sourced directly from the AgentEvent bus (not the ``kind`` widget
            channel above): :func:`tau_event_to_pi_event` maps each event to its
            ``type``-discriminated pi shape, deduping the double ``message_end`` so
            each assistant message yields one message_end with usage/model/
            stop_reason (step S8). ``None`` = the content-only duplicate; skip it.
            """
            if on_pi_event is None:
                return
            pi_event = tau_event_to_pi_event(event)
            if pi_event is not None:
                on_pi_event(pi_event)

        # Subscribe to events before running the prompt
        # This captures ALL events during the full agent loop (LLM calls + tool execution)
        unsubscribe = self.agent_session.subscribe(capture_event)
        unsubscribe_pi = (
            self.agent_session.subscribe(pi_capture) if on_pi_event is not None else None
        )

        # THE one door (docs/SUBMISSION-LIFECYCLE.md "The one door"). The turn is
        # admitted exactly once, here, with the caller's own submission record —
        # not re-derived from ``context`` and not re-admitted by ``prompt()`` a
        # layer down. The loop handles LLM calls, tool execution, and re-calls the
        # LLM for tool results; all streaming flows through the event bus above.
        result = await self.agent_session.submit(submission, context=context)
        new_messages = result.messages

        # Unsubscribe
        unsubscribe()
        if unsubscribe_pi is not None:
            unsubscribe_pi()

        # Combine all streaming chunks
        full_content = stream.text
        tool_calls_info = stream.tool_calls
        usage_totals = stream.usage_totals
        last_extra = stream.last_extra

        # Fold in whatever this exchange spent OFF the loop (auto-compaction, an
        # extension's ctx.complete()). The delta is taken after the unsubscribe so a
        # compaction in the end-of-prompt drain is inside the window.
        side_usage_after = self.agent_session.side_usage
        for _field, _before in side_usage_before.items():
            usage_totals[_field] += side_usage_after.get(_field, 0) - _before

        # Real token usage, summed across the exchange's completions. The dict
        # keeps the prompt/completion/total key names the TUI + headless paths
        # already read, mapped from τ's input/output/total fields. No fabricated
        # fallback — if a provider reports nothing, the count is a true 0.
        usage_out: dict[str, Any] = {
            "prompt_tokens": usage_totals["input_tokens"],
            "completion_tokens": usage_totals["output_tokens"],
            "total_tokens": usage_totals["total_tokens"],
            "cache_read_tokens": usage_totals["cache_read_tokens"],
            "cache_write_tokens": usage_totals["cache_write_tokens"],
        }
        # The last completion's server-reported telemetry, for the G4 exchange-summary
        # readout (t/s · repairs · forced-share). Attach the key ONLY when a provider
        # actually reported something — Fail-Early: a non-llama provider or a stock
        # server that sent no timings leaves ``extra`` off entirely, never ``extra: {}``,
        # so the summary reads exactly as it does today.
        if last_extra:
            usage_out["extra"] = last_extra
        # Cost at the emit boundary (E4.cost / step S7). ``self.config`` IS the
        # resolved model_config, so its optional per-model ``cost`` block prices
        # this exchange here — the one final total, on the same usage dict the TUI
        # finalizer and headless ``done`` both read. Emit ``cost_usd`` ONLY when a
        # cost block is configured: an absent block yields tokens-only (unknown
        # price), never a fabricated ``$0`` — a real free model ``cost:{…:0}``
        # yields ``0.0`` and reads differently. The frozen ``Usage`` is untouched.
        cost_usd = compute_cost_usd(
            self.config.get("cost"),
            input_tokens=usage_totals["input_tokens"],
            output_tokens=usage_totals["output_tokens"],
            cache_read_tokens=usage_totals["cache_read_tokens"],
        )
        if cost_usd is not None:
            usage_out["cost_usd"] = cost_usd
        return (
            full_content,
            usage_out,
            new_messages,
            tool_calls_info,
            result,
        )


def create_backend(config: dict[str, Any]) -> Backend:
    """Factory function to create a tau-agent-core backend."""
    return TauBackend(config)
