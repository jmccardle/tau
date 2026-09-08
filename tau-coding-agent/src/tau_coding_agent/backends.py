"""
Backend abstraction layer for tau-coding-agent.

Wraps tau-agent-core's AgentSession to provide TauApp-compatible
Backend interfaces (chat, stream_chat).

Reference: SESSION-TREE-IMPLEMENTATION.md §2.6 (throwaway SessionManager retired;
AgentSession runs against a scratch InMemorySessionLog, caller owns persistence).
"""

import re
import sys
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Literal, Sequence, cast
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
from tau_agent_core import tree_ops
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.extension_locks import ExtensionRequest
from tau_agent_core.event_projection import MessageDeltaProjector
from tau_agent_core.flows import Performed
from tau_agent_core.events import AgentEvent
from tau_agent_core.prompt_cache import (
    CONVERSATION_PREFIX,
    CompletionCache,
    PromptCacheObserver,
    completion_cache,
    prompt_tokens,
)
from tau_agent_core.session_log import InMemorySessionLog, SessionLog
from tau_agent_core.sdk import (
    BASE_SYSTEM_PROMPT,
    LoadExtensionsResult,
    _build_system_prompt,
    _resolve_tools,
    append_system_prompt,
)
from tau_agent_core.submission import Submission, SubmissionResult
from tau_agent_core.truncation import dropped_tool_calls

DEFAULT_LANE = "main"

#: What a sub-agent's lane key starts with, as opposed to a submission's.
BRANCH_LANE_PREFIX = "branch:"

DEFAULT_TOOL_NAMES: tuple[str, ...] = ("read", "write", "edit", "bash", "ls", "grep", "find")

DEFAULT_MAX_TOKENS = 4096

RenderHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


def resolve_tool_names(config: dict[str, Any]) -> list[str]:
    """The built-in tool names one model config resolves to, in order.

    The single reader of ``config["tools"]`` / ``config["exclude_tools"]``:
    :class:`TauBackend` calls it to decide what to construct, and
    ``TauApp._session_facts`` calls it to decide what to *display*. Both take the
    config AFTER ``TauApp._apply_run_config``, so ``--exclude-tools`` and both
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
    return event.model_dump(exclude_none=True)


class TurnStream:
    """One lane's worth of agent events, normalized into widget-lifecycle dicts.

    One instance per lane, which is what lets two concurrent turns (a ``fork``)
    and a turn the frontend never initiated (a bus or timer submission) render at
    all.

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
        {"kind": "completion_end", "output": int, "context": int,
         "stop_reason": str | None, "dropped_tool_calls": int}

    Tool widgets are driven off ``tool_execution_start`` / ``tool_execution_end``
    (which carry name/args/result directly), NOT off ``message_end`` toolCall
    blocks — the agent loop emits ``message_end`` twice per tool-bearing turn, so
    consuming it for rendering would duplicate. ``message_end`` is used only to
    harvest ``tool_calls`` for chat persistence (deduplicated by id), the
    per-completion usage, and the ``completion_end`` boundary.

    ``completion_end`` carries this lane's running token totals at every
    completion boundary rather than only at the end, so a live counter steps
    mid-turn on measured figures. Both of a turn's ``message_end`` events emit
    one; the second adds no usage and restates the same totals.
    """

    def __init__(self, lane: str = DEFAULT_LANE) -> None:
        self.lane = lane
        #: Every text delta this lane produced, in order (``"".join`` = the answer).
        self.text_chunks: list[str] = []
        #: Tool calls harvested for chat persistence, deduped by id.
        self.tool_calls: list[dict[str, Any]] = []
        self.usage_totals: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        self.context_tokens: int = 0
        self.last_extra: dict[str, Any] = {}
        self.last_stop_reason: str | None = None
        self.last_dropped_tool_calls: int = 0
        #: One :class:`CompletionCache` per completion, in call order.
        self.completions: list[CompletionCache] = []
        #: Epoch ms of this lane's first and last agent event (the loop's clock).
        self.first_event_ms: int | None = None
        self.last_event_ms: int | None = None
        self._delta_projector = MessageDeltaProjector()

    @property
    def text(self) -> str:
        """The assistant text this lane streamed, concatenated."""
        return "".join(self.text_chunks)

    @property
    def elapsed_seconds(self) -> float | None:
        """Wall-clock span of this lane, or None when no event carried a clock.

        None is "not measured", never 0.0 — an exchange that produced one event
        has no span to report and says so (docs/MESSAGE-TIMESTAMPS.md §3).
        """
        if self.first_event_ms is None or self.last_event_ms is None:
            return None
        if self.last_event_ms <= self.first_event_ms:
            return None
        return (self.last_event_ms - self.first_event_ms) / 1000

    def _note_clock(self, event: Any) -> None:
        """Widen this lane's span by one event's timestamp (epoch ms)."""
        stamp = getattr(event, "timestamp", None)
        if not isinstance(stamp, int) or isinstance(stamp, bool):
            return
        if self.first_event_ms is None:
            self.first_event_ms = stamp
        self.last_event_ms = stamp

    def feed(self, event: Any) -> list[dict[str, Any]]:
        """Normalize one agent event into zero or more render events."""
        if not hasattr(event, "type"):
            return []
        self._note_clock(event)
        if event.type == "turn_start":
            self._delta_projector.reset()
            return [self._tag({"kind": "turn_start", "turn_index": event.turn_index})]
        if event.type == "message_start":
            return self._feed_message_start(event)
        if event.type == "message_update":
            return self._feed_message_update(event)
        if event.type == "message_end":
            return self._harvest_message_end(event)
        if event.type == "tool_execution_start":
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

    def _feed_message_start(self, event: Any) -> list[dict[str, Any]]:
        """Normalize a ``message_start`` — only a USER one produces a render event.

        Reference: docs/TUI-STEERING.md §5. The agent loop emits ``message_start``
        for an assistant completion (``_stream_response``) and for a provider
        error, and both are rendered off the deltas and the ``message_end`` that
        follow them, so this drops those.

        A ``message_start`` whose message is a USER one has exactly one producer:
        ``AgentLoop._deliver_steer`` weaving a steering message into the running
        turn. It carries content that will never appear in any other event on
        this lane — the deltas that follow belong to the model's answer to it —
        so a renderer that ignored it would show the answer and not the question.
        """
        message = getattr(event, "message", None)
        if not message or message.get("role") != "user":
            return []
        content = message.get("content", "")
        if isinstance(content, str):
            text = content
        else:
            text = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        return [self._tag({"kind": "steer_message", "text": text})]

    def _feed_message_update(self, event: Any) -> list[dict[str, Any]]:
        message = getattr(event, "message", None)
        if not message:
            return []
        out: list[dict[str, Any]] = []
        for block_delta in self._delta_projector.project(message):
            if block_delta.delta is None:
                continue
            if block_delta.type == "text":
                self.text_chunks.append(block_delta.delta)
                out.append(self._tag({"kind": "text_delta", "delta": block_delta.delta}))
            elif block_delta.type == "thinking":
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
        usage = message.get("usage")
        if isinstance(usage, dict):
            for key in self.usage_totals:
                self.usage_totals[key] += int(usage.get(key, 0) or 0)
            self.context_tokens = prompt_tokens(usage)
            self.completions.append(completion_cache(usage))
            extra = usage.get("extra")
            self.last_extra = extra if isinstance(extra, dict) else {}
            reason = message.get("stop_reason")
            self.last_stop_reason = reason if isinstance(reason, str) else None
            self.last_dropped_tool_calls = dropped_tool_calls(usage)
        return [
            self._tag(
                {
                    "kind": "completion_end",
                    "output": self.usage_totals["output_tokens"],
                    "context": self.context_tokens,
                    "stop_reason": self.last_stop_reason,
                    "dropped_tool_calls": self.last_dropped_tool_calls,
                }
            )
        ]

    def _feed_tool_execution_end(self, event: Any) -> dict[str, Any]:
        tool_call_id = getattr(event, "tool_call_id", "") or ""
        is_error = getattr(event, "is_error", False)
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
         "submitter": str | None, "context": int, "output": int,
         "seconds": float | None, "cache_notice": str | None, "extra": dict}

    ``output`` is every token the lane GENERATED, summed across its completions
    and including the side-usage delta ``submission_end`` reports for work done
    off the agent loop (auto-compaction, an extension's ``ctx.complete()``).
    ``context`` is the prompt the lane last SENT — a replace, not a sum, because
    each completion's prompt contains every earlier one. Side usage is a different
    conversation's prompt, so it is not added to ``context``. ``extra`` is the last
    completion's telemetry, or ``{}`` when the provider reported none.

    ``cache_notice`` is this router's :class:`PromptCacheObserver` verdict on the
    closing lane. The observer is shared across lanes because its latch is a fact
    about the server, but a branch lane names no prefix — a sub-agent's prompt is
    not the conversation's, so it neither reads nor writes the cross-turn clock.

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
        self._identity: dict[str, tuple[str | None, str | None]] = {}
        self._prompt_cache = PromptCacheObserver()
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

    async def on_custom_message(self, *, entry_id: str, message: dict[str, Any]) -> None:
        """Deliver an extension's durable message (``custom_message`` channel).

        Named no lane, deliberately. ``api.send_message`` is reachable from a
        command handler with no turn in flight as well as from inside one, and a
        message that belongs to the conversation rather than to a completion is
        the transcript's, not a lane's — the renderer mounts it at the tail
        (docs/EXTENSION-LOCKS.md §9.1).
        """
        await self._deliver({"kind": "custom_message", "entry_id": entry_id, "message": message})

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
        key = f"{BRANCH_LANE_PREFIX}{lane}"
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
        key = f"{BRANCH_LANE_PREFIX}{lane}"
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
        cache_notice = self._prompt_cache.observe_turn(
            stream.completions,
            prefix=None if lane.startswith(BRANCH_LANE_PREFIX) else CONVERSATION_PREFIX,
            first_event_ms=stream.first_event_ms,
            last_event_ms=stream.last_event_ms,
        )
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
                "seconds": stream.elapsed_seconds,
                "cache_notice": cache_notice,
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
    )


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
    """Build a tau-agent-core ``Model`` from a TauApp/``~/.tau/config.json`` entry.

    The single seam that turns a config ``models`` entry (or a ``--model`` ad-hoc
    dict) into a ``Model`` — extracted from ``TauBackend.__init__`` so
    :func:`make_model_resolver` (S45) reproduces exactly the same construction a
    fresh backend would. Maps the ``backend`` provider field, derives the reasoning
    flag from a non-``off`` ``thinking`` level (or an explicit ``reasoning: true``),
    and carries the optional ``thinking_level_map``.
    """
    model_id = config.get("model", "gpt-4")
    backend_type = config.get("backend", "openai").lower()

    # Map provider name (TauApp's "backend" field) to tau-agent-core provider.
    provider_map = {"openai": "openai", "anthropic": "anthropic", "gemini": "gemini"}
    provider = provider_map.get(backend_type, backend_type)

    spec = get_provider_spec(provider)
    api = config.get("api") or (spec.api if spec else "openai-completions")
    if api not in registered_apis():
        raise ValueError(
            f"models.{model_id}.api is {api!r}, which τ does not implement. "
            f"Registered wire protocols: {', '.join(sorted(registered_apis()))}."
        )

    base_url = config.get("base_url") or (spec.base_url if spec else None)
    if not base_url:
        base_url = "https://api.openai.com/v1"

    thinking_level = config.get("thinking")
    reasoning_arg = thinking_level if thinking_level and thinking_level != "off" else None
    model_reasoning = bool(config.get("reasoning"))
    if reasoning_arg is not None and not model_reasoning:
        _warn_undeclared_reasoning(model_id)

    reasoning_replay = config.get("reasoning_replay") or "turn"
    if reasoning_replay not in ("all", "turn", "off"):
        raise ValueError(
            f"reasoning_replay must be one of 'all', 'turn', 'off'; got {reasoning_replay!r}"
        )
    reasoning_replay = cast(Literal["all", "turn", "off"], reasoning_replay)

    strict_reasoning_formats = config.get("strict_reasoning_formats", False)
    if not isinstance(strict_reasoning_formats, bool):
        raise ValueError(
            "models.<name>.strict_reasoning_formats must be a boolean; got "
            f"{strict_reasoning_formats!r}"
        )

    grammar_dialect = config.get("grammar")
    if grammar_dialect is not None and grammar_dialect not in ("llguidance", "gbnf"):
        raise ValueError(
            f"models.<name>.grammar must be 'llguidance' or 'gbnf'; got {grammar_dialect!r}"
        )
    grammar_dialect = cast("Literal['llguidance', 'gbnf'] | None", grammar_dialect)

    prompt_cache = config.get("prompt_cache", True)
    if isinstance(prompt_cache, str):
        raise ValueError(
            f"models.<name>.prompt_cache is a boolean; got {prompt_cache!r}. The dialect "
            "moved to prompt_cache_dialect: 'anthropic', and 'off' is prompt_cache: false"
        )
    if not isinstance(prompt_cache, bool):
        raise ValueError(f"models.<name>.prompt_cache must be a boolean; got {prompt_cache!r}")

    cache_dialect = config.get("prompt_cache_dialect")
    if cache_dialect is not None and cache_dialect != "anthropic":
        raise ValueError(
            f"models.<name>.prompt_cache_dialect must be 'anthropic'; got {cache_dialect!r}"
        )
    cache_dialect = cast("Literal['anthropic'] | None", cache_dialect)

    _validate_thinking_level_map(config.get("thinking_level_map"), model_id)

    extra_body = config.get("extra_body") or {}
    if not isinstance(extra_body, dict):
        raise ValueError(f"models.<name>.extra_body must be a JSON object; got {extra_body!r}")

    server_features = config.get("server_features") or []
    if not isinstance(server_features, list):
        raise ValueError(
            f"models.<name>.server_features must be a list of strings; got {server_features!r}"
        )

    compat_config = config.get("compat")
    if compat_config is not None and not isinstance(compat_config, dict):
        raise ValueError(f"models.<name>.compat must be a JSON object; got {compat_config!r}")
    compat = Compat(**compat_config) if compat_config else None

    context_window = config.get("context_window", 128000)
    if (
        isinstance(context_window, bool)
        or not isinstance(context_window, int)
        or context_window <= 0
    ):
        raise ValueError(
            f"models.<name>.context_window must be a positive integer; got {context_window!r}"
        )
    max_tokens = config.get("max_tokens", DEFAULT_MAX_TOKENS)
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
        prompt_cache=prompt_cache,
        prompt_cache_dialect=cache_dialect,
        extra_body=dict(extra_body),
        server_features=list(server_features),
        stream=config.get("stream", True),
        request_timeout=config.get("request_timeout"),
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
        ``result.command`` is the :data:`~tau_agent_core.flows.Dispatched` arm the
        caller must act on — render a ``Performed``, ask for a ``FlowStep``'s
        argument, perform a ``Ready``, open a ``View`` — and ``result.command is
        None`` means no command was dispatched
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

        Both run paths (headless ``run_print`` and the TUI ``TauApp``) load
        extensions through this seam after building the backend, so a file
        extension's hooks fire in the same ``AgentSession`` the loop runs on.
        ``extensions_config`` (S40) is the per-extension config map handed to each
        extension's ``api.config``, keyed by file stem. Returns the
        :class:`LoadExtensionsResult`; the caller surfaces its ``errors`` (an
        explicit ``-e`` failure raises out of here instead — Fail-Early)."""


class TauBackend(Backend):
    """tau-agent-core backend adapter.

    Wraps tau-agent-core's AgentSession to provide TauApp-compatible
    chat/stream_chat interfaces.
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)

        model_id = config.get("model", "gpt-4")
        api_key = config.get("api_key")

        self.model_name = model_id
        thinking_level = config.get("thinking")
        reasoning_arg = thinking_level if thinking_level and thinking_level != "off" else None

        model = build_model_from_config(config)
        self._model = model
        self._api_key = api_key

        tool_names = resolve_tool_names(config)
        if tool_names:
            tools = _resolve_tools(
                tool_names,
                {"read": {"max_image_dimension": config["max_image_dimension"]}}
                if "max_image_dimension" in config
                else None,
            )
        else:
            tools = []

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
            model=config.get("model") or None,
        )

        no_tools = config.get("no_tools")

        max_turns = config.get("max_turns")

        self.agent_session = AgentSession(
            session_log=InMemorySessionLog(),
            model=model,
            system_prompt=self.system_prompt,
            tools=tools,
            api_key=api_key,
            reasoning=reasoning_arg,
            compaction_settings=CompactionSettings(enabled=False),
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

    def get_extension_state(self) -> LoadExtensionsResult:
        """Every managed extension and every file that failed to load, read live.

        Delegates to :meth:`AgentSession.get_extension_state`. The ``/extensions``
        listing reads this rather than the value ``load_extensions`` returned, so a
        reload is reflected instead of showing the load-time snapshot.
        """
        return self.agent_session.get_extension_state()

    def _extension_action(self, outcome: ExtensionActionResult) -> Performed:
        """One :class:`ExtensionActionResult` as the :class:`Performed` all three report.

        The projection is written once because all three actions return the same
        record and E5 applies to all three identically — the same reason the RPC
        layer's ``_extension_action_result`` is one function and not three.
        """
        return self.agent_session.performed(
            f"{outcome.action}_extension",
            {
                "action": outcome.action,
                "path": outcome.path,
                "ok": outcome.ok,
                "message": outcome.message,
            },
        )

    @property
    def pending_request(self) -> ExtensionRequest | None:
        """The extension request at the cursor, or ``None`` (EXTENSION-LOCKS §2).

        A pass-through to :attr:`AgentSession.pending_request` so a head reads the
        lock from the same place :meth:`AgentSession.submit` reads it, rather than
        from a copy it kept.
        """
        return self.agent_session.pending_request

    async def answer_request(
        self, request_id: str, action: str, values: dict[str, Any]
    ) -> ExtensionCommandResult:
        """Answer an ask: append the response, then dispatch its action's command."""
        return await self.agent_session.answer_request(request_id, action, values)

    async def disable_extension(self, path: str) -> Performed:
        """Runtime-disable a loaded extension (E10 §6 / S70).

        Delegates to :meth:`AgentSession.disable_extension`, which fires the
        extension's ``session_shutdown`` teardown, then detaches its hooks + registry
        entries.

        Args:
            path: The managed extension's path, or a unique file stem.

        Returns:
            A :class:`~tau_agent_core.flows.Performed` whose ``data`` is
            ``{action, path, ok, message, cursor}``, as ``disable_extension``'s
            ``returns`` declares.
        """
        return self._extension_action(await self.agent_session.disable_extension(path))

    async def enable_extension(self, path: str) -> Performed:
        """Runtime-enable a disabled extension (E10 §6 / S70).

        Delegates to :meth:`AgentSession.enable_extension` (re-invoke ``register`` +
        ``session_start``).

        Args:
            path: The managed extension's path, or a unique file stem.

        Returns:
            A :class:`~tau_agent_core.flows.Performed`, shaped as
            :meth:`disable_extension`'s.
        """
        return self._extension_action(await self.agent_session.enable_extension(path))

    async def reload_extension(self, path: str) -> Performed:
        """Runtime-reload an extension from disk (E10 §6 / S70).

        Delegates to :meth:`AgentSession.reload_extension` (teardown → re-import →
        re-register → ``session_start``). A broken file raises, per Fail-Early.

        Args:
            path: The managed extension's path, or a unique file stem.

        Returns:
            A :class:`~tau_agent_core.flows.Performed`, shaped as
            :meth:`disable_extension`'s.
        """
        return self._extension_action(await self.agent_session.reload_extension(path))

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

    def set_model(self, name: str) -> Performed:
        """Switch the active model by config NAME, effective on the next turn.

        Delegates to :meth:`AgentSession.set_model`, which resolves the name through
        the resolver ``TauApp._build_session_runtime`` bound at startup and raises on
        a name that resolver does not know. A config key, not a model id — the same
        name ``--model NAME`` accepts headlessly and the same one ``get_models``
        publishes.

        Args:
            name: The config model name.

        Returns:
            A :class:`~tau_agent_core.flows.Performed` whose ``data`` is
            ``{model, cursor}``, as ``set_model``'s ``returns`` declares.
        """
        return self.agent_session.performed(
            "set_model", {"model": self.agent_session.set_model(name)}
        )

    def set_session_name(self, name: str) -> Performed:
        """Give the live session a display name, persisted to its log.

        Delegates to :meth:`AgentSession.set_session_name`. The name is what the
        session picker shows, so this is the head's side of a session becoming
        findable by something other than its id.

        Args:
            name: The name to persist. Must be non-empty.

        Returns:
            A :class:`~tau_agent_core.flows.Performed` whose ``data`` is
            ``{name, cursor}``, as ``set_session_name``'s ``returns`` declares.
        """
        self.agent_session.set_session_name(name)
        return self.agent_session.performed("set_session_name", {"name": name})

    def set_auto_compaction(self, enabled: bool) -> Performed:
        """Turn automatic compaction on or off for the live session.

        Delegates to :meth:`AgentSession.set_auto_compaction`, which writes the one
        in-memory field on ``CompactionSettings``. Nothing is appended, so this does
        not survive a restart; the config file is where a durable answer lives.

        Args:
            enabled: The state to put it in.

        Returns:
            A :class:`~tau_agent_core.flows.Performed` whose ``data`` is
            ``{enabled, cursor}``, as ``set_auto_compaction``'s ``returns`` declares.
            The cursor is the unchanged tip: nothing is appended, and absence is
            never this codebase's way of saying "nothing moved".
        """
        effective = self.agent_session.set_auto_compaction(enabled)
        return self.agent_session.performed("set_auto_compaction", {"enabled": effective})

    async def compact_messages(
        self, messages: list[dict], custom_instructions: str | None = None
    ) -> list[dict] | None:
        """Compact the conversation the TUI sends, returning the shortened list.

        Delegates to the AgentSession's compaction engine. Operates on the
        caller's ``messages`` (the TUI's authoritative ``current_chat.messages``,
        which ``stream_chat`` passes as the LLM context) — not the parallel
        session-manager path. Returns None when there is nothing to compact.

        Args:
            messages: The context to compact.
            custom_instructions: Extra focus for the generated summary, threaded
                unchanged into the summarizer's system prompt. This is the
                ``compact`` capability's one declared argument
                (:data:`tau_agent_core.capabilities.CAPABILITIES`), so a head
                that reads the registry and a head that reads this signature
                agree about what the command takes.
        """
        return await self.agent_session.compact_messages(messages, custom_instructions)

    async def navigate_tree(
        self,
        session: SessionLog,
        target_id: str,
        *,
        summarize: bool = False,
        custom_instructions: str | None = None,
    ) -> list[dict]:
        """Move the live session's cursor to ``target_id`` and return the new context.

        The head's side of two core capabilities: :func:`tau_agent_core.tree_ops.navigate`
        when there is no summary to make, :func:`~tau_agent_core.tree_ops
        .summarize_and_navigate` when there is. The core split them because they differ
        in cost — one is an append, the other spends tokens — and this method keeps the
        one ``summarize`` flag the tree browser's three modes already speak.

        The live coding-agent ``Session`` is passed in (the TUI owns it, §2.6), so the
        mutation lands on IT, not on the scratch ``InMemorySessionLog`` the AgentSession
        runs against. What this adds over calling the core directly is banking the
        summarizer's tokens: ``summarize_and_navigate`` returns its usage because it
        holds no session to bank it against, and this does hold one.

        Returns ``ConversationTree.context_for(cursor)`` — the flat message list the TUI
        swaps into ``self.messages`` and re-renders (reusing the compaction path, §3.4).
        """
        if target_id == session.cursor or not summarize:
            return tree_ops.navigate(session, target_id)
        messages, summary_usage = await tree_ops.summarize_and_navigate(
            session,
            target_id,
            self._model,
            api_key=self._api_key,
            custom_instructions=custom_instructions,
        )
        self.agent_session.record_side_usage(summary_usage)
        return messages

    def elide_span(self, session: SessionLog, anchor_id: str, first_kept_id: str) -> list[dict]:
        """Fold a span out of the live session's context and return the new context.

        Delegates to :func:`tau_agent_core.tree_ops.elide_span`, which holds the two
        refusals that make a fold safe — a resume point the fold's scan cannot reach
        would empty the context silently, and a fold that hides nothing is the
        silent-no-op anti-pattern. The app owns the modals and the re-render; the
        core owns the mutation and both refusals.

        **Synchronous**, unlike :meth:`navigate_tree`: there is no summary, therefore
        no model call and nothing to await.

        Returns ``ConversationTree.context_for(cursor)`` — the flat message list the
        TUI swaps into ``self.messages`` and re-renders.

        Raises:
            ValueError: an unknown anchor or resume point, a resume point that is
                not on the anchor's path, or a span that would hide nothing.
        """
        return tree_ops.elide_span(session, anchor_id, first_kept_id)

    def commit_branch(
        self, session: SessionLog, ids: Sequence[str], *, drop_context: bool
    ) -> list[dict]:
        """Build a branch out of the marked messages and continue on it.

        Delegates to :func:`tau_agent_core.tree_ops.commit_branch`, which plans the
        branch (``tree_surgery``) and performs it in the order TREE-BROWSER-AS-EDITOR.md
        §6.3 fixes. Nothing is re-parented and nothing is erased.

        Args:
            session: The live session log to write to.
            ids: The marked entry ids, in any order.
            drop_context: Whether the branch keeps only the selection.

        Returns:
            ``ConversationTree.context_for(cursor)`` — the new flat message list, the
            same re-render seam :meth:`elide_span` and :meth:`navigate_tree` use.

        Raises:
            ValueError: The selection is empty, names an unknown entry, contains an
                entry no branch can carry, or composes a path that is not
                turn-complete. Checked before the first append, so a refusal leaves
                the log byte-identical.
        """
        return tree_ops.commit_branch(session, ids, drop_context=drop_context)

    def paste_subtree(self, session: SessionLog, source_id: str, target_id: str) -> list[str]:
        """Re-create the subtree at ``source_id`` under ``target_id``.

        Delegates to :func:`tau_agent_core.tree_ops.paste_subtree`. The paste never
        moves the leaf, which is why this returns ids rather than a message list:
        nothing about the current context changed, so there is nothing to re-render
        until the reader navigates onto the copy.

        Args:
            session: The live session log to write to.
            source_id: The copied node — the root of the subtree.
            target_id: The entry the copy hangs from.

        Returns:
            The ids minted, in the order they were appended. The first is the copy of
            ``source_id`` itself.

        Raises:
            ValueError: An unknown id, a source whose kind cannot be copied, a target
                inside the source's own subtree, or a copied tool result whose call is
                on neither the target's path nor the copied run.
        """
        return tree_ops.paste_subtree(session, source_id, target_id)

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
        """Wire a :class:`RenderRouter` across all six of this session's channels.

        The ``AgentEvent`` stream carries the turns; ``submission_start`` /
        ``submission_end`` carry the submission spans that bracket them (which
        ``agent_start``/``agent_end`` cannot — a followUp re-entry runs a second
        loop inside one submission); ``branch_event`` carries a forked sub-agent's
        events, which until now had no consumer anywhere, which is the concrete
        sense in which "a fork today is unobservable"; ``branch_end`` is that
        sub-agent's own span close, emitted from a ``finally`` so a branch that
        raised or was cancelled cannot leave its lane open forever; and
        ``custom_message`` carries an extension's durable message, which belongs
        to none of the other five because it belongs to no completion.

        See :meth:`Backend.subscribe_render` for the contract.
        """
        router = RenderRouter(handler, on_orphan=on_orphan)
        unsubs = [
            self.agent_session.subscribe(router.on_agent_event),
            self.agent_session.subscribe_channel("submission_start", router.on_submission_start),
            self.agent_session.subscribe_channel("submission_end", router.on_submission_end),
            self.agent_session.subscribe_channel("branch_event", router.on_branch_event),
            self.agent_session.subscribe_channel("branch_end", router.on_branch_end),
            self.agent_session.subscribe_channel("custom_message", router.on_custom_message),
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
        """Extract the last user message text from a TauApp messages list."""
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
        :class:`~tau_agent_core.flows.Dispatched`, so a dispatched command
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
        stream = TurnStream()

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

        unsubscribe = self.agent_session.subscribe(capture_event)
        unsubscribe_pi = (
            self.agent_session.subscribe(pi_capture) if on_pi_event is not None else None
        )

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

        side_usage_after = self.agent_session.side_usage
        for _field, _before in side_usage_before.items():
            usage_totals[_field] += side_usage_after.get(_field, 0) - _before

        usage_out: dict[str, Any] = {
            "prompt_tokens": usage_totals["input_tokens"],
            "completion_tokens": usage_totals["output_tokens"],
            "total_tokens": usage_totals["total_tokens"],
            "cache_read_tokens": usage_totals["cache_read_tokens"],
            "cache_write_tokens": usage_totals["cache_write_tokens"],
        }
        if last_extra:
            usage_out["extra"] = last_extra
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
