"""The REPL head: ``tau --mode repl`` — a prompt line, no screen takeover.

Reference: docs/REPL-HEAD.md. A fourth head beside the TUI, print mode and RPC,
rendering with ``rich`` and reading with :mod:`tau_coding_agent.repl_input`. It is
a head in the full sense docs/HEADS-AND-MULTIPLEXER.md §2 means: it owns capture,
display and :class:`~tau_agent_core.submission.Submission` construction, and it
owns nothing else — every line goes through ``AgentSession.submit`` by way of
``backend.submit_turn``/``submit_command``, and every rendered token comes off ONE
persistent ``subscribe_render`` subscription rather than a per-turn stream.

**Bind, do not append** (§3): this head binds its session as the AgentSession's
log and appends nothing itself, so the core is the log's only writer. That is why
``submit_turn`` is called with ``context=None`` — "the bound log's ``context_for``"
— and why a rename or a model change on resume goes through the backend's own
recording doors instead of a hand-written entry.

This module imports no Textual. What it needs from a module that does is copied
rather than imported: :func:`render_panel_body` is ``extension_ui.py``'s, ported
here because that module imports Textual at its top and is frozen for this work
(docs/REPL-HEAD.md §2). The telemetry line is still out of reach; see §9.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import signal
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Sequence, TypeVar
from uuid import uuid4

from rich import box
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table

from tau_agent_core.agent_session_runtime import AgentSessionRuntime
from tau_agent_core.attachments import (
    DEFAULT_INLINE_LIMIT,
    SENDABLE_KINDS,
    complete_attachment,
    render_attachments,
    scan_attachments,
)
from tau_agent_core.capabilities import BUILTIN, Vocabulary
from tau_agent_core.commands import (
    UnsupportedCommandError,
    complete_command,
    complete_command_argument,
    parse_command,
    resolve_command,
    unsupported_command_message,
)
from tau_agent_core.compaction import estimate_tokens
from tau_agent_core.extension_locks import ExtensionRequest, refusal_reason
from tau_agent_core.flows import (
    Dispatched,
    FlowStep,
    Performed,
    Ready,
    enumerate_domain,
    flow_arguments,
    flow_form_spec,
    next_step,
)
from tau_agent_core.sdk import summarize_extensions
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog
from tau_agent_core.submission import MultitaskStrategy, Submission
from tau_agent_core.tools.image_resize import DEFAULT_MAX_IMAGE_DIMENSION
from tau_agent_core.truncation import truncation_from_messages, truncation_notice
from tau_coding_agent.config import ConfigError
from tau_coding_agent.headless import (
    TRUNCATION_ADVICE,
    CLIError,
    cache_dialect_advice,
    extension_command_names,
    parse_ext_config_overrides,
    resolve_extensions_config,
    resolve_model_config,
    select_session,
)
from tau_coding_agent.repl_input import Candidate, LineReader, PromptToolkitReader
from tau_coding_agent.repl_theme import (
    ABORTED,
    EXTENSION_LABEL,
    GLYPH,
    MORE,
    PENDING,
    ROLE_STYLE,
    STEER,
    TOOL_OK,
    TURN,
    lane_label,
)
from tau_coding_agent.steering import (
    SteeringBuffer,
    configured_steering_strategy,
    steering_note,
)
from tau_coding_agent.session_store import subscribe_session_events
from tau_coding_agent.store_factory import build_session_catalog, resolve_backend_name

if TYPE_CHECKING:  # avoid importing the dataclass module at runtime cost
    from tau_coding_agent.cli import CLIArgs

#: What this head calls itself when it has to say it cannot perform something.
FRONTEND = "the REPL (tau --mode repl)"

#: The label the spinner wears while a completion is outstanding.
WAITING = "waiting for the model…"

#: The three states of docs/REPL-HEAD.md §5's Ctrl+C table.
IDLE, STREAMING, ABORTING = "idle", "streaming", "aborting"

REPL_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "quit": "leave the REPL",
    "reasoning": "toggle printing a reasoning block in full",
    "tools": "how much of a tool call and its result to print (verbose | compact)",
    "panel": "press an extension panel's action: /panel <key> <number>",
}
"""This head's own commands — display and process, never the session.

None is in :data:`~tau_agent_core.commands.FRONTEND_COMMANDS`, so
``resolve_command`` answers ``None`` for them and by its rule they would be sent
to the model as prose. They are therefore checked BEFORE it, by name, and merged
into the table the completer reads (docs/REPL-HEAD.md §6). A head may own them
because none touches the session (docs/HEADS-AND-MULTIPLEXER.md §2).
"""

#: The names of :data:`REPL_COMMAND_DESCRIPTIONS`, for the disjointness check and the peek.
REPL_COMMANDS: frozenset[str] = frozenset(REPL_COMMAND_DESCRIPTIONS)

#: What ``/tools`` accepts. ``verbose`` prints the arguments and the whole result.
TOOL_DETAIL: tuple[str, ...] = ("compact", "verbose")

PICKER_DOMAINS: frozenset[str] = frozenset({"session_id"})
"""Domains this head asks for as a numbered pick although they render as ``text``.

The substitution docs/TUI-STYLE-GUIDE.md §2.4 licenses — a RICHER control, never
a poorer one — in CLI form: the TUI answers ``session_id`` with its picker, and a
prompt line answers it with the enumerated list rather than a bare text box no
reader could guess a session id into.
"""

#: Prefixes that continue the block above them rather than starting a new one.
_CONTINUATIONS = ("- ", "* ", "+ ", "| ", "> ", "    ", "\t")

#: How many lines of a tool result the compact fold shows before it counts the rest.
RESULT_FOLD_LINES = 4

#: How many lines of the unflushed block ride beside the spinner (docs/REPL-HEAD.md §4).
TOOLBAR_TAIL_LINES = 3

#: What :meth:`ReplLoop._ask_alone` hands back, whatever the question was.
_Answer = TypeVar("_Answer")


def render_panel_body(body: dict[str, Any]) -> RenderableType:
    """Render a normalized ``ui.panel`` body dict to a ``rich`` renderable.

    A PORT of ``extension_ui.render_panel_body``, not an import: that module
    imports Textual at its top (docs/REPL-HEAD.md §2 planned a shared
    ``render_text.py``, which needs an edit to a file frozen for this work). The
    body vocabulary is the core's — :func:`validate_panel_spec` — so the two
    copies answer to one validator; keeping them equal is
    ``test_repl_extension_ui.py``'s job until the move is made.

    Args:
        body: A ``{"kind": "text"|"list"|"table", …}`` body dict.

    Returns:
        The string or :class:`~rich.table.Table` to print.

    Raises:
        KeyError: The body names a kind this renderer has no case for, which is a
            new core body kind rather than something to print as its repr.
    """
    kind = body["kind"]
    if kind == "text":
        return str(body["text"])
    if kind == "list":
        return "\n".join(f"• {item}" for item in body["items"])
    if kind != "table":
        raise KeyError(f"render_panel_body: unknown body kind {kind!r}")
    table = Table(
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        padding=(0, 1, 0, 0),
        header_style=None,
    )
    for column in body["columns"]:
        table.add_column(column)
    for row in body["rows"]:
        table.add_row(*row)
    return table


def _continues_block(line: str) -> bool:
    """Whether *line* extends the Markdown block before the blank line above it.

    A list item, a table row, a block quote or an indented code line after a blank
    line belongs to the construct above; a flush there would render two half
    constructs. An ordered-list marker (``3. ``) is recognised by shape.
    """
    if line.startswith(_CONTINUATIONS):
        return True
    head, dot, rest = line.partition(". ")
    return bool(dot) and head.isdigit() and bool(rest)


class BlockSplitter:
    """One lane's streamed Markdown, cut into blocks as each one completes.

    Reference: docs/REPL-HEAD.md §4, ``text_delta``. A boundary is a blank line
    OUTSIDE a fence followed by a line that does not continue the block above it,
    and the decision waits for that next line to be COMPLETE — so a flush trails
    the boundary by one line and a fence that is still open never splits.

    This is a splitter, not a parser: a construct whose one-line lookahead is not
    enough (a table whose rows are separated by blank lines) renders as two
    blocks, which §10 records as the known cost.

    It is INCREMENTAL because it is fed once per delta and a block that never
    reaches a boundary — an open fence, a table, one long paragraph — grows to the
    size of the whole answer: re-scanning the buffer per delta is quadratic in it
    (measured: 3.2s of splitting alone for an 82 KB fenced block at 4 chars a
    delta). The scan resumes at the first line it has not yet decided about, and
    the fence parity carried beside it is what makes resuming legal.
    """

    def __init__(self) -> None:
        self._buffer = ""
        #: Offset of the first line this splitter has not decided about.
        self._scan = 0
        #: Fence parity AT ``_scan``, which is why the scan may start there.
        self._fenced = False

    @property
    def remainder(self) -> str:
        """Everything buffered that is not yet a block."""
        return self._buffer

    @property
    def scanned(self) -> int:
        """How much of :attr:`remainder` is already decided about, in characters.

        It only ever moves forward within one block, which is the property that
        makes streaming linear rather than quadratic.
        """
        return self._scan

    def feed(self, delta: str) -> list[str]:
        """Append *delta* and return every block it completed, in order."""
        self._buffer += delta
        blocks: list[str] = []
        while True:
            line, after = self._line_at(self._scan)
            if line is None:
                return blocks
            if line.lstrip().startswith("```"):
                self._fenced = not self._fenced
                self._scan = after
                continue
            if self._fenced or line.strip():
                self._scan = after
                continue
            following = self._next_content_line(after)
            if following is None:
                return blocks
            offset, text = following
            if _continues_block(text):
                self._scan = offset
                continue
            block = self._buffer[: self._scan].strip()
            if block:
                blocks.append(block)
            self._buffer = self._buffer[offset:]
            self._scan = 0

    def take(self) -> str:
        """Hand back what is buffered and start again empty."""
        rest, self._buffer, self._scan, self._fenced = self._buffer, "", 0, False
        return rest

    def _line_at(self, offset: int) -> tuple[str | None, int]:
        """The COMPLETE line at *offset* and the offset after it; ``None`` when partial."""
        end = self._buffer.find("\n", offset)
        if end < 0:
            return None, offset
        return self._buffer[offset:end], end + 1

    def _next_content_line(self, offset: int) -> tuple[int, str] | None:
        """The first complete non-blank line at or after *offset*, with its offset."""
        while True:
            line, after = self._line_at(offset)
            if line is None:
                return None
            if line.strip():
                return offset, line
            offset = after


def split_markdown_blocks(text: str) -> tuple[list[str], str]:
    """Split *text* into complete Markdown blocks and the unflushed rest.

    One :class:`BlockSplitter` fed once — the whole-string form of the same rule,
    kept because a boundary is easier to state about a string than about a stream.

    Args:
        text: Everything streamed on this lane since the last flush.

    Returns:
        ``(blocks, remainder)`` — the blocks are ready to render, the remainder is
        what must stay buffered.
    """
    splitter = BlockSplitter()
    return splitter.feed(text), splitter.remainder


def toolbar_tail(text: str) -> str:
    """The last :data:`TOOLBAR_TAIL_LINES` lines of an unflushed block, raw.

    Reference: docs/REPL-HEAD.md §4, ``text_delta``: text that has not reached a
    block boundary is shown ONLY here, beside the spinner, so a paragraph with no
    blank line in it is visibly arriving rather than silently buffered.
    """
    return "\n".join(text.splitlines()[-TOOLBAR_TAIL_LINES:])


class ReplRenderer:
    """Prints one session's whole render stream — every lane, nothing dropped.

    Reference: docs/REPL-HEAD.md §4. Holds the per-lane streaming state (the
    unflushed Markdown, the reasoning size) and drives the reader's spinner, since
    "is the model still thinking" is answered by the same events.

    Two events are also decisions the head takes rather than prints, and it hands
    in a callback for each: a ``tool_call`` is the ``"steer"`` strategy's delivery
    point, and a ``steer_message`` is the core's confirmation that a delivered
    message was woven in (docs/REPL-HEAD.md §5).
    """

    def __init__(
        self,
        console: Console,
        reader: LineReader,
        *,
        model_name: str,
        on_tool_call: Callable[[], None] | None = None,
        on_steer_delivered: Callable[[], None] | None = None,
    ) -> None:
        """
        Args:
            console: Where every line is printed.
            reader: Whose spinner tracks the stream.
            model_name: Named in the prompt-cache advice, since the fix is a key
                under it.
            on_tool_call: Called at every ``tool_call``, from any lane.
            on_steer_delivered: Called when a steering message is woven in.
        """
        self._console = console
        self._reader = reader
        self._model_name = model_name
        self._tool_call_hook = on_tool_call
        self._steer_hook = on_steer_delivered
        self._text: dict[str, BlockSplitter] = {}
        self._reasoning: dict[str, str] = {}
        self._labels: dict[str, str | None] = {}
        self._cache_warned: set[str] = set()
        #: Lanes this head aborted, so ``lane_end`` can say the turn was cut short.
        self.aborting: set[str] = set()
        #: Every orphan reason reported, in order — reported, never dropped.
        self.orphans: list[str] = []
        #: ``/reasoning``: print a reasoning block in full rather than its size.
        self.verbose_reasoning = False
        #: ``/tools verbose``: print a call's arguments and a result whole.
        self.verbose_tools = False

    def line(self, role: str, text: str) -> None:
        """Print one role-styled line, with the palette's glyph and no markup."""
        glyph = GLYPH[role]
        body = f"{glyph} {text}" if glyph else text
        self._console.print(body, style=ROLE_STYLE[role], markup=False, highlight=False)

    def clipped(self, role: str, text: str) -> None:
        """Print one role-styled line, cut to the terminal's width rather than wrapped.

        A tool call and a folded result are one line each BECAUSE they are one
        line each: a wrapped argument would cost three rows of scrollback for a
        record whose point is that it costs one.
        """
        glyph = GLYPH[role]
        body = f"{glyph} {text}" if glyph else text
        self._console.print(
            _clip(body, self._console.width), style=ROLE_STYLE[role], markup=False, highlight=False
        )

    def held(self, text: str, note: str) -> None:
        """Echo a line the head is holding, and say where it will be delivered.

        The REPL's answer to the TUI's ``PendingInput`` widget: a mid-turn line
        cannot go in the transcript, because the model has not been given it yet,
        and a prompt that accepted text and showed nothing appears to swallow it.

        Args:
            text: The line as typed.
            note: The strategy's delivery promise (``steering.steering_note``).
        """
        self._console.print(
            f"{PENDING} {text}", style=ROLE_STYLE["user"], markup=False, highlight=False
        )
        self.line("system", note)

    def mark_aborting(self, lane: str | None = None) -> None:
        """Say that every lane now streaming was cut short by this head.

        Nothing on the render stream says a turn was aborted (docs/REPL-HEAD.md
        §4), so ``lane_end`` reads this. *lane* names a submission whose
        ``lane_start`` may not have arrived yet — the abort can beat it.

        Args:
            lane: The lane of the turn being aborted, when the head knows it.
        """
        self.aborting.update(self._labels)
        if lane is not None:
            self.aborting.add(lane)

    def spin(self, label: str | None) -> None:
        """Set or clear the activity indicator."""
        self._reader.set_spinner(label)

    def __call__(self, event: dict[str, Any]) -> None:
        """Render one router event. Every kind is printed; nothing is dropped."""
        kind = event.get("kind")
        handler = getattr(self, f"_on_{kind}", None)
        if handler is None:
            raise ValueError(
                f"the REPL renderer has no case for render event {kind!r}. "
                "docs/REPL-HEAD.md §4 lists the vocabulary; a new kind is a "
                "rendering decision, not something to pass over in silence"
            )
        handler(event)

    def on_orphan(self, reason: str) -> None:
        """Report a render event that named no open lane."""
        self.orphans.append(reason)
        self.line("error", f"orphan render event: {reason}")

    def numbered(self, items: Sequence[str]) -> None:
        """Print a numbered list — the CLI form of every choice this head offers."""
        for number, item in enumerate(items, start=1):
            self._console.print(f"{number:>3}. {item}", markup=False, highlight=False)

    def panel(self, title: str, body: RenderableType, footer: Sequence[str] = ()) -> None:
        """Print one framed block: an extension's panel, or a request's body.

        The CLI form of the TUI's mounted panel — a frame in the scrollback rather
        than a widget in a docked host, because a REPL has nowhere to keep one
        live and a panel that scrolls away is one an extension can re-send.

        Args:
            title: The frame's title.
            body: What :func:`render_panel_body` returned.
            footer: Lines under the body — the numbered actions, and how to press one.
        """
        parts: list[RenderableType] = [body]
        if footer:
            parts.append("")
            parts.extend(footer)
        self._console.print(
            Panel(
                Group(*parts),
                title=title,
                title_align="left",
                border_style=ROLE_STYLE["extension"],
            )
        )

    def _on_lane_start(self, event: dict[str, Any]) -> None:
        lane = event["lane"]
        label = lane_label(event.get("source"), event.get("submitter"))
        self._labels[lane] = label
        self._text[lane] = BlockSplitter()
        self._reasoning[lane] = ""
        if label is None:
            # Live, the line is already in the scrollback; replayed, nothing put it there.
            if event.get("replay"):
                self.line("user", event.get("text") or "")
            return
        self._console.print(Rule(label, style=ROLE_STYLE["foreign"]))
        text = event.get("text") or ""
        if text:
            self.line("user", text)

    def _on_system_prompt(self, event: dict[str, Any]) -> None:
        """A replayed system message: its size, since it is not the conversation."""
        self.line("reasoning", f"system prompt ({int(event['chars']):,} chars)")

    def _on_turn_start(self, event: dict[str, Any]) -> None:
        if event.get("turn_index"):
            self.line("system", f"{TURN} turn {event['turn_index']}")

    def _on_steer_message(self, event: dict[str, Any]) -> None:
        self._flush(event["lane"])
        self.line("user", f"{STEER} {event.get('text') or ''}")
        if self._steer_hook is not None:
            self._steer_hook()

    def _on_reasoning_delta(self, event: dict[str, Any]) -> None:
        lane = event["lane"]
        self._reasoning[lane] = self._reasoning.get(lane, "") + (event.get("delta") or "")
        self.spin(f"thinking… (~{reasoning_tokens(self._reasoning[lane])} tokens)")

    def _on_text_delta(self, event: dict[str, Any]) -> None:
        lane = event["lane"]
        self._close_reasoning(lane)
        splitter = self._splitter(lane)
        for block in splitter.feed(event.get("delta") or ""):
            self._console.print(Markdown(block))
        # The unflushed text has ONE surface (§4): the toolbar, never the scrollback.
        self.spin(toolbar_tail(splitter.remainder) or WAITING)

    def _on_tool_call(self, event: dict[str, Any]) -> None:
        lane = event["lane"]
        self._flush(lane)
        name = event.get("name") or "?"
        arguments = event.get("arguments") or {}
        self.clipped("tool", f"{name}({_format_arguments(arguments)})")
        if self.verbose_tools and arguments:
            self._console.print(Syntax(json.dumps(arguments, indent=2, default=str), "json"))
        self.spin(f"{name}…")
        if self._tool_call_hook is not None:
            self._tool_call_hook()

    def _on_tool_result(self, event: dict[str, Any]) -> None:
        name = event.get("name") or "?"
        result = str(event.get("result") or "")
        if event.get("blocked"):
            self.clipped("blocked", f"{name} blocked by {event.get('blocked_by')}")
            self._fold(result)
        elif event.get("is_error"):
            self.clipped("tool_error", f"{name} — {_first_line(result)}")
            self._fold(result)
        else:
            self.clipped("tool", f"{TOOL_OK} {name} — {_summarize(result)}")
            self._fold(result)
        self.spin(WAITING)

    def _fold(self, result: str) -> None:
        """Show the body of a tool result: folded to :data:`RESULT_FOLD_LINES`, or whole.

        The first line is already on the header row, so this prints what follows
        it and then says how many lines it did not print — a fold that does not
        count what it hid is indistinguishable from a result that was that short.
        """
        if not result.strip():
            return
        if self.verbose_tools:
            self._console.print(
                result.strip(), markup=False, highlight=False, style=ROLE_STYLE["system"]
            )
            return
        shown, hidden = fold_lines(result, RESULT_FOLD_LINES)
        for line in shown[1:]:
            self._indented(line)
        if hidden:
            self._indented(f"{MORE} {hidden} more lines")

    def _indented(self, text: str) -> None:
        """Print one dim continuation line under a header row, cut to the width."""
        self._console.print(
            _clip(f"  {text}", self._console.width),
            style=ROLE_STYLE["system"],
            markup=False,
            highlight=False,
        )

    def _on_completion_end(self, event: dict[str, Any]) -> None:
        self._flush(event["lane"])

    def _on_custom_message(self, event: dict[str, Any]) -> None:
        message = event.get("message") or {}
        self.line("extension", EXTENSION_LABEL)
        self.line("extension", _message_text(message))

    def _on_lane_end(self, event: dict[str, Any]) -> None:
        lane = event["lane"]
        self._flush(lane)
        self.spin(None)
        if lane in self.aborting:
            self.aborting.discard(lane)
            self.line("system", f"{ABORTED} aborted")
        notice = event.get("cache_notice")
        if notice and notice not in self._cache_warned:
            self._cache_warned.add(notice)
            self.line("error", str(notice))
            self.line("system", cache_dialect_advice(self._model_name))
        self._console.print(
            _footer(event),
            style=ROLE_STYLE["system"],
            markup=False,
            highlight=False,
            justify="right",
        )
        label = self._labels.pop(lane, None)
        if label is not None:
            self._console.print(Rule(f"end {label}", style=ROLE_STYLE["foreign"]))
        self._text.pop(lane, None)
        self._reasoning.pop(lane, None)

    def _splitter(self, lane: str) -> BlockSplitter:
        """This lane's streaming splitter, opened on demand.

        On demand because a lane can be routed to before its ``lane_start`` — a
        branch opens on its first event — and a missing splitter would drop text.
        """
        splitter = self._text.get(lane)
        if splitter is None:
            splitter = self._text[lane] = BlockSplitter()
        return splitter

    def _flush(self, lane: str) -> None:
        """Write this lane's buffered Markdown out, block boundary or not."""
        self._close_reasoning(lane)
        rest = self._splitter(lane).take()
        if rest.strip():
            self._console.print(Markdown(rest.strip()))

    def _close_reasoning(self, lane: str) -> None:
        """Record a reasoning block that ended: its size, or all of it under ``/reasoning``."""
        thought = self._reasoning.get(lane, "")
        if not thought:
            return
        self._reasoning[lane] = ""
        self.line("reasoning", f"reasoning (~{reasoning_tokens(thought)} tokens)")
        if self.verbose_reasoning:
            self._console.print(
                thought.strip(), markup=False, highlight=False, style=ROLE_STYLE["reasoning"]
            )


def _footer(event: dict[str, Any]) -> str:
    """The ``lane_end`` telemetry line: what the lane read, wrote and took."""
    parts = [f"ctx {int(event.get('context') or 0):,}", f"out {int(event.get('output') or 0):,}"]
    seconds = event.get("seconds")
    if seconds is not None:
        parts.append(f"{float(seconds):.1f}s")
    return " · ".join(parts)


def _format_arguments(arguments: dict[str, Any]) -> str:
    """Render a tool call's arguments for its one line: the first, then how many more.

    A call is one row of scrollback, so it shows the argument that identifies it
    — which for every built-in tool is the first one the model wrote — and says
    that the others exist. ``/tools verbose`` prints them all as JSON.
    """
    shown = []
    for key, value in arguments.items():
        text = str(value).replace("\n", " ")
        shown.append(f"{key}={text[:80] + '…' if len(text) > 80 else text}")
    if len(shown) > 1:
        return f"{shown[0]}, +{len(shown) - 1}"
    return ", ".join(shown)


def _clip(text: str, width: int) -> str:
    """Cut *text* to *width* columns, marking the cut. Never wraps and never pads."""
    if width <= 1 or len(text) <= width:
        return text
    return text[: width - 1] + MORE


def fold_lines(text: str, limit: int) -> tuple[list[str], int]:
    """Fold a tool result to at most *limit* lines.

    Args:
        text: The result as the tool returned it.
        limit: How many lines may be shown.

    Returns:
        The lines to show, and how many were left out — 0 when the whole result fits.
    """
    lines = text.strip().split("\n")
    if len(lines) <= limit:
        return lines, 0
    return lines[:limit], len(lines) - limit


def reasoning_tokens(thought: str) -> int:
    """Estimate a reasoning block's size in tokens.

    No provider τ speaks reports reasoning tokens separately, so this is the
    core's own ~4-chars-per-token heuristic (``compaction.estimate_tokens``) over
    the block, printed with a ``~`` so a reader knows which of the two it is.

    Args:
        thought: The accumulated reasoning text.

    Returns:
        The estimated token count.
    """
    return estimate_tokens(
        {"role": "assistant", "content": [{"type": "thinking", "thinking": thought}]}
    )


def _first_line(text: str) -> str:
    """The first line of a tool result, for the one-line record."""
    return text.strip().split("\n", 1)[0] if text.strip() else "(no output)"


def _summarize(text: str) -> str:
    """A tool result's first line, with its remaining line count when it has one."""
    stripped = text.strip()
    if not stripped:
        return "(no output)"
    lines = stripped.split("\n")
    return lines[0] if len(lines) == 1 else f"{lines[0]} ({len(lines)} lines)"


def _message_text(message: dict[str, Any]) -> str:
    """The text of a custom entry's message, however its content is shaped."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def session_vocabulary(session: Any) -> Vocabulary:
    """τ's flow registry, plus whatever ``session``'s extensions declared.

    Read off the live session on every use rather than held, because a
    ``/reload_extension`` changes it and a held copy would go on offering a gesture
    whose handler is gone.

    Args:
        session: The :class:`AgentSession`, or ``None``/a double that has none.

    Returns:
        The session's vocabulary, or :data:`BUILTIN` when it has none.
    """
    vocabulary = getattr(session, "vocabulary", None)
    return vocabulary if isinstance(vocabulary, Vocabulary) else BUILTIN


async def ask_form(
    reader: LineReader, renderer: ReplRenderer, spec: dict[str, Any]
) -> dict[str, Any] | None:
    """Ask a ``ui.form`` spec one field at a time. ``None`` means cancelled.

    The one control table (docs/REPL-HEAD.md §6): ``rich`` prints the choices and
    the reader takes the answer, so a flow's form and an extension's own
    ``ui.form`` are asked the same way. A ``select`` hands back the string it
    DISPLAYED, which is what the caller's label→value map is for.

    Args:
        reader: Where each answer is typed.
        renderer: Where the title and the choices are printed.
        spec: A spec :func:`~tau_agent_core.extension_types.validate_form_spec` accepts.

    Returns:
        ``{field name: value}`` typed per field kind, or ``None`` when the form was
        cancelled — Ctrl+C or EOF, since no field can be declared required.

    Raises:
        ValueError: The spec is not one that function accepts.
    """
    from tau_agent_core.extension_types import validate_form_spec

    title, fields = validate_form_spec(spec)
    renderer.line("extension", title)
    answers = await ask_fields(reader, renderer, fields)
    if answers is None:
        renderer.line("system", f"{title}: cancelled")
    return answers


async def ask_fields(
    reader: LineReader, renderer: ReplRenderer, fields: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """Ask a validated field list one at a time. ``None`` means cancelled.

    Split out of :func:`ask_form` because an extension REQUEST carries fields
    without a form spec around them and may carry none at all, where
    ``validate_form_spec`` requires a non-empty list (docs/REPL-HEAD.md §7). No
    fields is an empty answer set, not a cancellation.

    Args:
        reader: Where each answer is typed.
        renderer: Where the choices are printed.
        fields: Fields already through ``validate_form_spec``.

    Returns:
        ``{field name: value}``, or ``None`` when a field was cancelled.
    """
    answers: dict[str, Any] = {}
    for field in fields:
        value = await _ask_field(reader, renderer, field)
        if value is None:
            return None
        answers[field["name"]] = value
    return answers


async def _ask_field(
    reader: LineReader, renderer: ReplRenderer, field: dict[str, Any]
) -> Any | None:
    """Ask one validated field with the control its kind names. ``None`` = cancelled.

    Cancelling is Ctrl+C or EOF and nothing else: ``validate_form_spec`` has no
    ``required`` key, so no field can be declared one and an empty answer is a
    VALUE rather than a refusal — otherwise a form with one optional box could not
    be submitted at all, and the answers already typed were thrown away with it.

    Raises:
        ValueError: The kind has no control here — a new field kind is a rendering
            decision, not something to ask for as a text box.
    """
    kind = field["kind"]
    label = field.get("label") or field["name"]
    options: list[str] = list(field.get("options") or ())

    if kind in ("select", "multiselect"):
        renderer.numbered(options)
    prompt = f"{label} [1-{len(options)}]" if options else label
    if kind == "multiselect":
        prompt = f"{label} [1-{len(options)}, comma-separated]"
    if kind == "confirm":
        prompt = f"{label} [y/n]"

    validate = _FIELD_VALIDATORS[kind](options) if kind in _FIELD_VALIDATORS else None
    answer = await reader.ask(prompt, default=_default_text(field), validate=validate)
    if answer is None:
        return None
    return _field_value(field, answer.strip())


def _default_text(field: dict[str, Any]) -> str:
    """The declared default as the text the reader pre-fills with.

    A ``select`` is answered by the NUMBER printed beside the option, so its
    default pre-fills as that number: the label its own validator rejects, and
    ``LineReader.ask`` re-prefills the same default after every complaint, so
    pressing Enter on it would loop until the reader cleared the line by hand.
    A default naming no option pre-fills nothing rather than a number that would
    choose a different one.
    """
    kind = field["kind"]
    default = field.get("default")
    if default is None:
        return ""
    options: list[str] = list(field.get("options") or ())
    if kind == "confirm":
        return "y" if default else "n"
    if kind == "select":
        return str(options.index(default) + 1) if default in options else ""
    if kind == "multiselect":
        chosen = default if isinstance(default, list) else [default]
        return ", ".join(str(options.index(item) + 1) for item in chosen if item in options)
    return str(default)


def _field_value(field: dict[str, Any], answer: str) -> Any:
    """The typed value an accepted answer names; an empty one is the field's default.

    The fallbacks are the TUI's, so one spec answers the same in both heads
    (``_FieldForm._collect``, modals.py:206–238): a cleared text box is ``""``, a
    cleared number is its default, an untouched radio is its default or the first
    option, and an emptied multiselect is ``[]``.

    Raises:
        ValueError: The kind has no value rule here.
    """
    kind = field["kind"]
    default = field.get("default")
    options: list[str] = list(field.get("options") or ())
    if kind == "text":
        return answer
    if kind == "number":
        if not answer:
            return default if default is not None else 0
        return _as_number(answer)
    if kind == "confirm":
        return answer.lower() in ("y", "yes") if answer else bool(default)
    if kind == "select":
        index = _as_index(answer, options)
        if index is not None:
            return options[index]
        return default if default in options else options[0]
    if kind == "multiselect":
        picked = (_as_index(part.strip(), options) for part in answer.split(",") if part.strip())
        return list(dict.fromkeys(options[index] for index in picked if index is not None))
    raise ValueError(f"_field_value: no value rule for field kind {kind!r}")


def _validate_number(_options: list[str]) -> Callable[[str], str | None]:
    """A number field takes what ``int`` parses, else what ``float`` does."""

    def validate(answer: str) -> str | None:
        if not answer.strip() or _as_number(answer.strip()) is not None:
            return None
        return f"{answer!r} is not a number"

    return validate


def _validate_confirm(_options: list[str]) -> Callable[[str], str | None]:
    """A confirm field takes y/yes/n/no, in any case."""

    def validate(answer: str) -> str | None:
        if not answer.strip() or answer.strip().lower() in ("y", "yes", "n", "no"):
            return None
        return "answer y or n"

    return validate


def _validate_select(options: list[str]) -> Callable[[str], str | None]:
    """A select field takes ONE of the numbers printed beside the options."""

    def validate(answer: str) -> str | None:
        if not answer.strip() or _as_index(answer.strip(), options) is not None:
            return None
        return f"pick a number from 1 to {len(options)}"

    return validate


def _validate_multiselect(options: list[str]) -> Callable[[str], str | None]:
    """A multiselect field takes comma-separated numbers, each in range."""

    def validate(answer: str) -> str | None:
        if not answer.strip():
            return None
        parts = [part for part in answer.split(",") if part.strip()]
        if parts and all(_as_index(part.strip(), options) is not None for part in parts):
            return None
        return f"pick numbers from 1 to {len(options)}, separated by commas"

    return validate


def _as_number(text: str) -> int | float | None:
    """``text`` as an ``int``, else as a ``float``, else ``None``."""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def _as_index(text: str, options: list[str]) -> int | None:
    """The 0-based option ``text`` names, or ``None`` when it names none."""
    if not text.isdigit() or not 1 <= int(text) <= len(options):
        return None
    return int(text) - 1


#: Per kind, the validator built over that field's options. ``text`` accepts anything.
_FIELD_VALIDATORS: dict[str, Callable[[list[str]], Callable[[str], str | None]]] = {
    "number": _validate_number,
    "confirm": _validate_confirm,
    "select": _validate_select,
    "multiselect": _validate_multiselect,
}


class ReplDelegate:
    """What a loaded extension's ``api.ui`` reaches in this head (§7).

    The whole delegate protocol — ``notify``, ``form``, ``set_status``, ``panel``
    — in the paradigm of a CLI: a notification is one styled line, a form is asked
    at the prompt, a status slot rides in the prompt prefix, and a panel is a
    framed block in the scrollback whose actions are pressed by typing
    ``/panel <key> <n>``.

    Bound BEFORE extensions load (docs/REPL-HEAD.md §3 step 11), which is what
    makes ``ui.interactive`` true for the first notify an extension's module body
    makes.

    Attributes:
        panels: The live panel per key, which is what ``/panel`` presses against.
        status: The live status slot per key, in first-seen order.
    """

    LEVELS: dict[str, str] = {"info": "extension", "warning": "warning", "error": "error"}
    """Notify level → the palette role that prints it (docs/REPL-HEAD.md §7)."""

    def __init__(
        self,
        renderer: ReplRenderer,
        reader: LineReader,
        ask_alone: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]],
    ) -> None:
        """
        Args:
            renderer: Where every surface is printed.
            reader: Whose prefix carries the status slots.
            ask_alone: Runs a question with the prompt to itself —
                :meth:`ReplLoop._ask_alone`, because a hook may raise a form while
                a steering read is outstanding and two prompts must never coexist.
        """
        self._renderer = renderer
        self._reader = reader
        self._ask_alone = ask_alone
        self.panels: dict[str, dict[str, Any]] = {}
        self.status: dict[str, str] = {}

    def notify(self, message: str, level: str = "info") -> None:
        """Print one notification in the level's own style.

        A level this palette does not name is printed as a warning WITH its name,
        rather than silently as an info line: the level is information about the
        message, and losing it is losing part of what the extension said.
        """
        role = self.LEVELS.get(level)
        if role is None:
            self._renderer.line("warning", f"{level}: {message}")
            return
        self._renderer.line(role, message)

    async def form(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Ask ``api.ui.form``'s spec at the prompt. ``None`` when cancelled.

        The same control table a flow's form is asked with (:func:`ask_form`), so
        an extension's own form and a command's collected argument look alike and
        are validated alike. Reached only under ``allow_user_input=True``; a bus
        or timer submission raises in the core instead, which is what keeps a
        cancelled form from ever being a fabricated answer set.
        """
        answers: dict[str, Any] | None = await self._ask_alone(
            lambda: ask_form(self._reader, self._renderer, spec)
        )
        return answers

    def set_status(self, key: str, text: str | None) -> None:
        """Set, update or (with ``None``) clear one keyed slot in the prompt prefix.

        The prefix is a callable the reader repaints in place, so a slot changing
        mid-line leaves the half-typed draft exactly where it was (§5).
        """
        if text is None:
            self.status.pop(key, None)
        else:
            self.status[key] = text
        slots = " ".join(f"[{name}: {value}]" for name, value in self.status.items())
        self._reader.set_prefix(f"{slots} " if slots else "")

    def panel(self, key: str, spec: dict[str, Any] | None) -> None:
        """Print, re-print or retire one keyed panel.

        A REPL cannot keep a panel live the way a docked host does, so a re-call
        prints the panel again — the scrollback is the record of what it said
        when. The actions are numbered rather than bracketed because a press is
        typed (``/panel key 2``), and the number is what is typed.
        """
        if spec is None:
            self.panels.pop(key, None)
            self._renderer.line("system", f"panel {key} removed")
            return
        self.panels[key] = spec
        footer = [
            f"{number}) {action['label']} → /{action['command']} {action['args']}".rstrip()
            for number, action in enumerate(spec["actions"], start=1)
        ]
        if footer:
            footer.append(f"press one with /panel {key} <number>")
        self._renderer.panel(f"{spec['title']} ({key})", render_panel_body(spec["body"]), footer)


class ReplCompleter:
    """What Tab offers: the three vocabularies of docs/SLASH-COMMANDS.md §3, in order.

    Asked about ``line[:cursor]`` — the word to the left of the point, the way a
    shell completes — so every candidate's span ends at the cursor and nothing
    after it is ever rewritten.

    The order is the TUI's (``TauApp._refresh_command_popup``): the ``@…`` the
    cursor is in, else the VALUE of a command argument, else the command WORD. A
    ``/…`` that names nothing offers the one warning line instead of nothing,
    which is what turns a silent fallthrough into a visible statement.
    """

    def __init__(self, backend: Any, runtime: Any) -> None:
        """
        Args:
            backend: Read for the session, its vocabulary and the extension commands.
            runtime: What ``session_id`` and ``path`` are enumerated against.
        """
        self._backend = backend
        self._runtime = runtime

    def _vocabulary(self) -> Vocabulary:
        """The live registry, re-read per press: a reloaded flow is offered, a gone one is not."""
        return session_vocabulary(getattr(self._backend, "agent_session", None))

    def __call__(self, line: str, cursor: int) -> list[Candidate]:
        """Every candidate for the word ending at ``cursor``."""
        text = line[:cursor]
        files = self._attachments(text, cursor)
        if files is not None:
            return files
        values = self._values(text, cursor)
        if values is not None:
            return values
        return self._commands(text, cursor)

    def _attachments(self, text: str, cursor: int) -> list[Candidate] | None:
        """Paths for the ``@…`` the cursor is inside, or ``None`` for "not one"."""
        found = complete_attachment(text, cursor, cwd=Path.cwd())
        if found is None:
            return None
        return [
            Candidate(text=match.name, start=found.start + 1, meta=match.detail)
            for match in found.matches
        ]

    def _values(self, text: str, cursor: int) -> list[Candidate] | None:
        """Legal values for the argument being typed, or ``None`` when none is.

        A domain that cannot be enumerated comes back as the candidate's meta text
        rather than raising: this runs on a keypress, and the reason there are no
        models is the Fail-Early answer where a traceback and an empty list are
        both wrong (the TUI's ``_argument_completions`` decided the same).
        """
        vocabulary = self._vocabulary()
        slot = complete_command_argument(text, vocabulary)
        if slot is None:
            return None
        try:
            found = enumerate_domain(
                slot.domain.name,
                session=getattr(self._backend, "agent_session", None),
                runtime=self._runtime,
                scope=slot.argument.scope,
                query=slot.query,
                vocabulary=vocabulary,
            )
        except ValueError as exc:
            return [Candidate(text=slot.query, start=slot.start, meta=str(exc))]
        return [
            Candidate(text=value.value, start=slot.start, display=value.label)
            for value in found.values
        ]

    def _commands(self, text: str, cursor: int) -> list[Candidate]:
        """The command WORD, while the cursor is still inside it."""
        lead = len(text) - len(text.lstrip())
        word = text[lead:]
        if not word.startswith("/") or " " in word:
            return []
        found = complete_command(text, self._extension_commands())
        if found is None:
            return []
        if not found.matches:
            return [
                Candidate(
                    text=found.token,
                    start=lead + 1,
                    meta="not a command τ knows; it will be sent as text",
                )
            ]
        return [
            Candidate(text=match.name, start=lead + 1, meta=match.description)
            for match in found.matches
        ]

    def _extension_commands(self) -> dict[str, str]:
        """The whole slash vocabulary Tab may offer: extensions plus this head's own."""
        lister = getattr(self._backend, "get_extension_commands", None)
        registered = dict(lister()) if lister is not None else {}
        return {**registered, **REPL_COMMAND_DESCRIPTIONS}


class ReplCancelled(Exception):
    """A startup choice the user declined: the process exits 0 having done nothing."""


def _require(backend: Any, name: str) -> Any:
    """The backend member this head cannot run without, or a :class:`CLIError`.

    Args:
        backend: The backend ``create_backend`` returned.
        name: The attribute this head is about to use.

    Returns:
        The attribute.
    """
    member = getattr(backend, name, None)
    if member is None:
        raise CLIError(
            f"--mode repl needs {type(backend).__name__}.{name}, which this backend "
            "does not have. The REPL binds its session log and renders from one "
            "subscription (docs/REPL-HEAD.md §3); a backend missing that cannot be "
            "driven from a prompt line."
        )
    return member


async def _pick_session(
    catalog: SessionCatalog, reader: LineReader, console: Console
) -> ConversationSession:
    """``--resume``: list this directory's sessions and load the one chosen.

    Read over ``catalog.list(cwd)`` rather than the ``session_id`` domain, which
    needs a runtime, which needs the session whose model resolution has not
    happened yet (docs/REPL-HEAD.md §2, ``--resume``).

    Args:
        catalog: The store this run resolved.
        reader: Where the number is typed.
        console: Where the list is printed.

    Returns:
        The chosen session, loaded.

    Raises:
        CLIError: When this directory has no saved sessions.
        ReplCancelled: When the answer was empty or the ask was cancelled.
    """
    rows = catalog.list(os.getcwd())
    if not rows:
        raise CLIError(
            "no saved sessions to resume in this directory (the session store this "
            "run resolved — ~/.tau/sessions unless --session-dir/--store says "
            "otherwise). Drop --resume to start a fresh one."
        )
    for number, row in enumerate(rows, start=1):
        console.print(
            f"{number:>3}. {row.display_title()}  [{row.id[:8]}]  {row.modified:%Y-%m-%d %H:%M}",
            markup=False,
            highlight=False,
        )

    def validate(answer: str) -> str | None:
        if not answer.strip():
            return None
        if not answer.strip().isdigit() or not 1 <= int(answer.strip()) <= len(rows):
            return f"pick a number from 1 to {len(rows)}, or press Enter to quit"
        return None

    answer = await reader.ask("resume which session?", validate=validate)
    if answer is None or not answer.strip():
        raise ReplCancelled()
    return catalog.load(rows[int(answer.strip()) - 1].ref)


def build_repl_submission(
    text: str,
    *,
    images: list[dict[str, Any]] | None = None,
    strategy: MultitaskStrategy = "enqueue",
    expand_commands: bool = True,
) -> Submission:
    """The record that says what a line typed at the REPL prompt means.

    ``source="interactive"`` / ``submitter="human"`` because a person typed it
    here; ``allow_user_input=True`` because there IS a human at this prompt, which
    is what lets an extension raise a form mid-turn (docs/REPL-HEAD.md §7).

    Args:
        text: The line as typed, expansions included.
        images: The image blocks ``@picture.png`` expanded to, or ``None``.
        strategy: ``"enqueue"`` for a line submitted at the prompt — a REPL is
            long-lived and a second line belongs after the first rather than
            instead of it — or ``"steer"`` for the mid-turn delivery point.
        expand_commands: ``True`` for a typed line, which is what declares this
            call site a human frontend; ``False`` for a steering flush, whose
            text was already checked against the command table before it was held.

    Returns:
        The submission to hand to ``submit_turn`` or ``submit_command``.
    """
    return Submission(
        text=text,
        images=images,
        source="interactive",
        submitter="human",
        submission_id=uuid4().hex,
        multitask_strategy=strategy,
        expand_commands=expand_commands,
        allow_user_input=True,
    )


def attachment_inline_limit(config: dict[str, Any]) -> int:
    """How large a ``@file`` may be before only its path is sent, in bytes.

    Fail-Early: a value that is not a positive integer RAISES rather than falling
    back, because the setting decides whether a file's CONTENT reaches the model.

    Args:
        config: The loaded ``config.json``.

    Returns:
        The configured limit, or :data:`DEFAULT_INLINE_LIMIT`.

    Raises:
        ConfigError: The key is present and is not a positive integer.
    """
    return _positive_int(config, "attachment_inline_limit", DEFAULT_INLINE_LIMIT, "bytes")


def max_image_dimension(config: dict[str, Any]) -> int:
    """The pixel cap an attached image is scaled down to.

    Args:
        config: The loaded ``config.json``.

    Returns:
        The configured cap, or :data:`DEFAULT_MAX_IMAGE_DIMENSION`.

    Raises:
        ConfigError: The key is present and is not a positive integer.
    """
    return _positive_int(config, "max_image_dimension", DEFAULT_MAX_IMAGE_DIMENSION, "pixels")


def _positive_int(config: dict[str, Any], key: str, default: int, unit: str) -> int:
    """One config reader for the two attachment sizes; both refuse a non-positive value."""
    configured = config.get(key, default)
    if not isinstance(configured, int) or isinstance(configured, bool) or configured <= 0:
        raise ConfigError(
            f"config key {key!r} = {configured!r} is not a positive number of {unit}."
        )
    return int(configured)


def expand_attachments(
    text: str, config: dict[str, Any], renderer: ReplRenderer
) -> tuple[str, list[dict[str, Any]] | None]:
    """Resolve ``@file`` in a typed line into the prompt actually sent.

    Reference: docs/FILE-ATTACHMENTS.md §2. Called at the moment the submission is
    built — the delivery point for a steering line — so what is persisted and what
    the model saw are the same string. A file that could not be read is reported
    here AND rides in the prompt as a ``<reference … error=>``, so neither side is
    left believing an attachment landed.

    Args:
        text: The line as typed.
        config: The loaded config, for the two size limits.
        renderer: Where a failure is reported.

    Returns:
        ``(text_to_send, images)``; ``images`` is ``None`` when there are none.
    """
    attachments = scan_attachments(
        text, cwd=Path.cwd(), inline_limit=attachment_inline_limit(config)
    )
    if not any(a.kind in SENDABLE_KINDS for a in attachments):
        return text, None
    rendered = render_attachments(attachments, max_image_dimension=max_image_dimension(config))
    for failure in rendered.failures:
        renderer.line("error", f"attachment failed: {failure}")
    return rendered.prefix + text, list(rendered.images) or None


def _install_signal_handlers(
    on_signal: Callable[[], None], renderer: ReplRenderer
) -> list[signal.Signals]:
    """Route SIGINT/SIGTERM to the head's own interrupt, and say when it cannot.

    A typed Ctrl+C never becomes a signal — the reader holds the terminal in raw
    mode and binds the key — so this covers the press that arrives from OUTSIDE
    the terminal. Where the loop has no signal support (Windows), that is stated
    rather than swallowed, since the difference is which presses abort a turn.

    Args:
        on_signal: What each signal calls.
        renderer: Where the one line goes when signals are unavailable.

    Returns:
        The signals actually installed, for the teardown to remove.
    """
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except NotImplementedError:
            renderer.line(
                "system",
                f"this event loop installs no {sig.name} handler, so a signal sent "
                "from outside this terminal will not abort a turn; Ctrl+C typed at "
                "the prompt still does.",
            )
            return installed
        installed.append(sig)
    return installed


def _remove_signal_handlers(installed: list[signal.Signals]) -> None:
    """Give every installed signal back to whoever had it."""
    loop = asyncio.get_running_loop()
    for sig in installed:
        loop.remove_signal_handler(sig)


async def run_repl(
    args: "CLIArgs",
    config: dict[str, Any],
    *,
    reader: LineReader | None = None,
    console: Console | None = None,
    catalog: SessionCatalog | None = None,
) -> int:
    """Run the interactive prompt loop until EOF. Returns a process exit code.

    The startup and shutdown order is docs/REPL-HEAD.md §3, and it is load-bearing
    in three places: the UI delegate is installed BEFORE extensions load, the
    render subscription is taken ONCE, and ``session_start`` fires after both.

    Args:
        args: The parsed :class:`~tau_coding_agent.cli.CLIArgs`.
        config: The loaded ``~/.tau/config.json``.
        reader: The line source; a real terminal reader is built when omitted.
        console: Where to render; a default ``rich`` console when omitted.
        catalog: The session store; resolved from ``args``/``config`` when omitted.

    Returns:
        0 — the loop ends on EOF, an idle interrupt, or a shutdown request.
    """
    from tau_coding_agent.backends import DEFAULT_MAX_TOKENS, create_backend, make_model_resolver
    from tau_coding_agent.config import TAU_DIR

    configured_steering_strategy(config)
    attachment_inline_limit(config)
    max_image_dimension(config)
    console = console if console is not None else Console()
    reader = (
        reader
        if reader is not None
        else PromptToolkitReader(console, history_path=TAU_DIR / "repl_history")
    )
    catalog = (
        catalog
        if catalog is not None
        else build_session_catalog(
            config, args.store, args.session_dir, persist=not args.no_session
        )
    )

    if args.no_session and (args.continue_session or args.session or args.fork or args.resume):
        raise CLIError(
            "--no-session can't be combined with --continue/--session/--fork/--resume "
            "(those resume or fork a persisted session)"
        )

    try:
        prior = (
            await _pick_session(catalog, reader, console)
            if args.resume
            else select_session(args, catalog)
        )
    except ReplCancelled:
        return 0

    if prior is not None and args.system_prompt is not None:
        raise CLIError(
            "--system-prompt can't be combined with --continue/--session/--fork; "
            "the resumed session already has a system prompt"
        )

    model_name, model_config = resolve_model_config(
        config, args, fallback_model=prior.model if prior is not None else None
    )
    backend_name = model_config.get("backend", "")
    cwd = os.getcwd()
    base_prompt = (
        args.system_prompt if args.system_prompt is not None else config.get("system_prompt")
    )
    if base_prompt:
        model_config["system_prompt"] = base_prompt

    backend = create_backend(model_config)
    agent_session = _require(backend, "agent_session")

    if prior is None:
        create = catalog.create_ephemeral if args.no_session else catalog.create
        session = create(
            cwd,
            model_name,
            backend_name,
            system_prompt=getattr(backend, "system_prompt", "") or None,
            name=args.name,
        )
    elif args.fork is not None:
        session = catalog.fork(prior, cwd)
    else:
        session = prior

    _require(backend, "bind_session_log")(session)
    runtime = AgentSessionRuntime(
        agent_session,
        catalog,
        cwd,
        model_name,
        backend_name,
        resolve_backend_name(config, args.store),
    )
    agent_session.set_model_resolver(make_model_resolver(config.get("models", {})))

    if prior is not None:
        if args.name is not None:
            _require(backend, "set_session_name")(args.name)
        if model_name != prior.model or backend_name != prior.backend:
            _require(backend, "record_model_change")(model_name)

    unsubscribe = subscribe_session_events(agent_session.route_session_event)
    loop: ReplLoop | None = None
    renderer = ReplRenderer(
        console,
        reader,
        model_name=model_name,
        on_tool_call=lambda: loop.at_tool_call() if loop is not None else None,
        on_steer_delivered=lambda: loop.confirm_steer() if loop is not None else None,
    )
    loop = ReplLoop(
        backend,
        agent_session,
        runtime,
        renderer,
        reader,
        console,
        config,
        max_tokens=model_config.get("max_tokens", DEFAULT_MAX_TOKENS),
    )
    runtime.set_rebind_session(loop.on_rebind)
    reader.set_interrupt(loop.on_interrupt)
    reader.set_reclaim(loop.reclaim)
    reader.set_completer(ReplCompleter(backend, runtime))
    installed = _install_signal_handlers(loop.on_signal, renderer)
    router = None

    try:
        # Before load_extensions, so a module body's own notify reaches a live surface.
        _require(backend, "set_ui_delegate")(loop.delegate)
        ext_result = await backend.load_extensions(
            model_config.get("extensions") or None,
            discover=not model_config.get("no_extensions", False),
            extensions_config=resolve_extensions_config(
                config, parse_ext_config_overrides(list(args.ext_config or []))
            ),
        )
        for ext_error in ext_result.errors:
            renderer.line("error", f"failed to load extension {ext_error.path}: {ext_error.error}")

        router = backend.subscribe_render(renderer, on_orphan=renderer.on_orphan)
        await _require(backend, "emit_session_start")("startup")
        if prior is not None:
            loop.replay(session.context)
        await loop.run()
        return 0
    finally:
        _remove_signal_handlers(installed)
        reader.set_interrupt(None)
        reader.set_reclaim(None)
        reader.set_completer(None)
        runtime.set_rebind_session(None)
        unsubscribe()
        if router is not None:
            router.detach()
            await router.close_all()
        await runtime.dispose()

        from tau_llm.client import aclose_providers

        await aclose_providers()


class ReplLoop:
    """Read, submit, steer and interrupt: the prompt for the session's life.

    Reference: docs/REPL-HEAD.md §5. At most ONE read is outstanding at any
    moment, and the loop is a ``FIRST_COMPLETED`` wait over that read, whatever
    turn is running and whatever steering delivery is in flight — which is what
    keeps the prompt open for the whole of a turn, so a line typed there is
    steering rather than a second turn.

    The three states are the Ctrl+C table's: ``idle`` (a press exits), ``streaming``
    (a press aborts the turn and reclaims the buffer) and ``aborting`` (a press
    does nothing, because the abort is already sent and the turn is unwinding).
    """

    def __init__(
        self,
        backend: Any,
        agent_session: Any,
        runtime: AgentSessionRuntime,
        renderer: ReplRenderer,
        reader: LineReader,
        console: Console,
        config: dict[str, Any],
        *,
        max_tokens: int | None = None,
    ) -> None:
        """
        Args:
            backend: The bound backend; the one door and the abort signal.
            agent_session: Read for ``shutdown_requested`` after every submission.
            runtime: Performs the two session-lifecycle mutations (``fork``,
                ``switch_session``) and answers the ``session_id``/``path`` domains.
            renderer: Where a held line, a refusal and a fault are printed.
            reader: The one line source.
            console: Where a traceback is printed.
            config: The loaded config, re-read per flush for ``steering_strategy``.
            max_tokens: The cap this run sends, quoted by the truncation notice so
                the reader knows which number to raise; ``None`` reports it unknown.
        """
        self._backend = backend
        self._agent_session = agent_session
        self._runtime = runtime
        self._renderer = renderer
        self._reader = reader
        self._console = console
        self._config = config
        self._max_tokens = max_tokens
        #: What ``api.ui`` reaches; owned here because a form needs the prompt to itself.
        self.delegate = ReplDelegate(renderer, reader, self._ask_alone)
        self._steering = SteeringBuffer()
        self._state = IDLE
        self._lane: str | None = None
        self._eof = False
        self._read_task: asyncio.Task[str | None] | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._steer_tasks: set[asyncio.Task[None]] = set()
        #: Set whenever something OUTSIDE the loop body changes what it must wait on.
        self._wake = asyncio.Event()

    async def run(self) -> None:
        """Read and submit until EOF, an idle interrupt, or a shutdown request.

        The wait set is rebuilt per iteration and :attr:`_wake` is in it, because
        the read is not only re-opened HERE: :meth:`_ask_alone` cancels it and
        re-issues it from inside a turn task, and a loop already parked on that
        turn alone would see nothing typed after a mid-turn form until the turn
        ended — missing the very delivery point the line was aimed at.
        """
        await self._show_request()
        self._read_task = asyncio.ensure_future(self._reader.read())
        while True:
            waiting: set[asyncio.Future[Any]] = {
                task for task in (self._read_task, self._turn_task) if task is not None
            }
            waiting |= set(self._steer_tasks)
            if not waiting:
                return
            wake = asyncio.ensure_future(self._wake.wait())
            done, _ = await asyncio.wait(waiting | {wake}, return_when=asyncio.FIRST_COMPLETED)
            wake.cancel()
            self._wake.clear()
            if self._read_task is not None and self._read_task in done:
                await self._take_line(self._read_task)
            for task in [task for task in self._steer_tasks if task in done]:
                self._steer_tasks.discard(task)
                _reraise(task)
            if self._turn_task is not None and self._turn_task in done:
                finished, self._turn_task = self._turn_task, None
                _reraise(finished)
                await self._settle()
            if self._agent_session.shutdown_requested:
                await self._reap()
                return
            if self._eof and self._turn_task is None and not self._steer_tasks:
                return

    async def _reap(self) -> None:
        """Finish what this head started before ``run_repl`` tears the session down.

        A shutdown asked for from inside a turn (``ctx.shutdown()`` in a hook)
        leaves that turn awaiting ``submit_turn``, so returning straight out of
        :meth:`run` detaches the render router and fires ``session_shutdown``
        underneath it — the turn's own ``lane_end`` is then never rendered and a
        queued delivery submits into a session that has already closed its
        providers. The running turn is AWAITED because it is the session's own
        work and it is already in flight; a delivery is CANCELLED and SAID, since
        the conversation it was typed for is the one that is ending.
        """
        if self._turn_task is not None:
            finished, self._turn_task = self._turn_task, None
            with contextlib.suppress(asyncio.CancelledError):
                await finished
        # After the turn, not before: its own tool call can start one more delivery.
        undelivered = [task for task in self._steer_tasks if not task.done()]
        for task in undelivered:
            task.cancel()
        for task in list(self._steer_tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._steer_tasks.clear()
        if undelivered:
            self._renderer.line(
                "error",
                f"the session is shutting down; {len(undelivered)} steering message(s) "
                "were not delivered.",
            )
        if self._read_task is not None:
            read, self._read_task = self._read_task, None
            read.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await read

    async def _take_line(self, task: asyncio.Task[str | None]) -> None:
        """Act on a completed read, and re-open the prompt unless that was EOF."""
        self._read_task = None
        line = None if task.cancelled() else task.result()
        if line is None:
            self._eof = True
            return
        if line.strip():
            await self._on_line(line)
        if not self._eof:
            self._read_task = asyncio.ensure_future(self._reader.read())

    async def _on_line(self, line: str) -> None:
        """Submit a line typed at an idle prompt; hold one typed during a turn.

        This head's own commands are peeked BEFORE ``resolve_command``, which
        answers ``None`` for them (they are not in ``FRONTEND_COMMANDS``) and would
        therefore send ``/quit`` to the model as prose — docs/REPL-HEAD.md §6.
        """
        head_command = _head_command(line)
        invocation = resolve_command(line, extension_command_names(self._backend))
        is_command = head_command is not None or invocation is not None
        if self._state != IDLE:
            self._hold(line, is_command=is_command)
            return
        if head_command is not None:
            await self._run_head_command(*head_command)
            return
        if invocation is not None:
            await self._run_command(line)
            return
        # A lock is read one step early, so a refused line stays typed (app.py:882).
        if await self._bounce_if_locked(line):
            return
        self._start_turn(line, expand_commands=True)

    def _hold(self, line: str, *, is_command: bool) -> None:
        """Buffer a line typed mid-turn, or refuse it when it is a command.

        Commands are refused and not queued (docs/TUI-STEERING.md §2): ``/compact``
        and ``/fork`` rewrite the very context the running turn is being answered
        from. The refused line goes back to the prompt, so "press Enter again" is
        literally what is left to do.
        """
        if is_command:
            self._renderer.line(
                "error",
                f"{line.split()[0]} runs between turns. Press Enter again when this one finishes.",
            )
            self._return_to_draft(line)
            return
        self._steering.hold(line)
        self._renderer.held(line, steering_note(self._strategy()))

    def _start_turn(self, text: str, *, expand_commands: bool) -> None:
        """Run one turn in a task, so the prompt stays open beside it."""
        self._state = STREAMING
        self._turn_task = asyncio.ensure_future(
            self._run_turn(text, expand_commands=expand_commands)
        )

    async def _settle(self) -> None:
        """One turn ended: go idle, show what the turn left, and deliver the buffer.

        The order is the TUI's (app.py:1110–1112): the cursor moved, so the
        extension request is re-read BEFORE the enqueue flush — a hook that locked
        the session during the turn must refuse the buffered line rather than
        have it submitted into the lock.

        Both strategies deliver here — for ``"enqueue"`` that is the whole
        contract, and for ``"steer"`` it is what happens when the turn ended
        without another tool call, so the message becomes its own turn rather than
        being stranded in a queue no further turn would drain.
        """
        self._state = IDLE
        self._lane = None
        await self._show_request()
        text = self._steering.take()
        if text is None:
            return
        if await self._bounce_if_locked(text):
            return
        self._start_turn(text, expand_commands=False)

    def at_tool_call(self) -> None:
        """The ``"steer"`` strategy's delivery point: the turn is about to loop.

        Waiting for a tool call rather than delivering when the line was typed is
        docs/TUI-STEERING.md §2's rule: it is the point at which there is provably
        another call to the model coming.
        """
        if self._strategy() != "steer":
            return
        text = self._steering.take()
        if text is None:
            return
        # Recorded HERE and not in the task: an abort before the task runs must find it.
        self._steering.await_weave(text)
        self._steer_tasks.add(asyncio.ensure_future(self._deliver_steer(text)))

    def confirm_steer(self) -> None:
        """A ``steer_message`` landed: the oldest delivery is no longer reclaimable."""
        self._steering.confirm()

    def reclaim(self) -> str | None:
        """Up on an empty prompt: hand back everything not yet woven in."""
        return self._steering.reclaim()

    def on_interrupt(self) -> bool:
        """Ctrl+C, from the key binding or from a signal. Returns "handled".

        Idle is the only state that answers ``False``, which is what ends the read
        and the loop with it. Streaming aborts the turn and takes the buffer back;
        aborting does nothing at all, so a second press cannot hang waiting for a
        turn that is already unwinding.
        """
        if self._state == IDLE:
            return False
        if self._state == STREAMING:
            self._state = ABORTING
            self._backend.abort()
            self._renderer.mark_aborting(self._lane)
            reclaimed = self._steering.reclaim()
            if reclaimed is not None:
                self._return_to_draft(reclaimed)
        return True

    def on_signal(self) -> None:
        """SIGINT/SIGTERM: the same press, arriving from outside the terminal."""
        if self.on_interrupt():
            return
        self._eof = True
        if self._read_task is not None:
            self._read_task.cancel()

    def _strategy(self) -> str:
        """The configured delivery point, re-read so a swapped config decides."""
        return configured_steering_strategy(self._config)

    def _return_to_draft(self, text: str) -> None:
        """Put text the head could not deliver back in front of the prompt's draft."""
        draft = self._reader.draft()
        self._reader.set_draft(f"{text}\n\n{draft}" if draft else text)

    async def _run_head_command(self, name: str, args: str) -> None:
        """Run one of THIS head's commands — display and process, never the session.

        Each refuses stray text rather than discarding it: the head owns these four
        words, so the docs/SLASH-COMMANDS.md §4 defect it records for τ's built-ins
        (``/tree extra words`` runs and drops the words) is not repeated here.

        ``/panel`` is the exception to "never the session": it presses an action a
        panel declared, and that press goes through :meth:`_run_command` — the one
        door — rather than to the extension's handler (docs/REPL-HEAD.md §7).
        """
        if name == "panel":
            await self._press_panel_action(args)
            return
        if name == "quit":
            if args:
                self._renderer.line("error", f"/quit takes no argument; got {args!r}.")
                return
            self._eof = True
            return
        if name == "reasoning":
            if args:
                self._renderer.line("error", f"/reasoning takes no argument; got {args!r}.")
                return
            self._renderer.verbose_reasoning = not self._renderer.verbose_reasoning
            state = "in full" if self._renderer.verbose_reasoning else "as its size only"
            self._renderer.line("system", f"reasoning is now printed {state}")
            return
        if args not in TOOL_DETAIL:
            self._renderer.line(
                "error",
                f"/tools takes {' or '.join(TOOL_DETAIL)}; got {args!r}. Nothing was changed.",
            )
            return
        self._renderer.verbose_tools = args == "verbose"
        self._renderer.line("system", f"tool calls and results are now printed {args}")

    async def _press_panel_action(self, args: str) -> None:
        """``/panel <key> <n>``: run the command that panel's action names.

        Through :meth:`_run_command` and therefore through ``submit_command``, not
        through ``run_extension_command``: the door is what runs the input-hook
        chain and the lock refusal, so under extension A's lock a ``/panel b 1``
        that reached B's handler directly would run the very thing a typed line is
        refused for (docs/REPL-HEAD.md §7).
        """
        parts = args.split()
        if len(parts) != 2:
            self._renderer.line(
                "error", f"/panel takes a panel key and an action number; got {args!r}."
            )
            return
        key, number = parts
        spec = self.delegate.panels.get(key)
        if spec is None:
            known = ", ".join(sorted(self.delegate.panels)) or "(none are live)"
            self._renderer.line("error", f"no panel named {key!r}; live panels: {known}.")
            return
        actions = spec["actions"]
        index = _as_index(number, [action["label"] for action in actions])
        if index is None:
            self._renderer.line(
                "error",
                f"panel {key!r} has {len(actions)} action(s); {number!r} names none of them.",
            )
            return
        action = actions[index]
        # The ask path checks this (`_open_ask`); an unchecked press is prose to the model.
        if action["command"] not in set(extension_command_names(self._backend)):
            self._renderer.line(
                "error",
                f"panel {key!r} action {action['label']!r} names /{action['command']}, which no "
                "loaded extension registered, so there is nothing to run.",
            )
            return
        await self._run_command(f"/{action['command']} {action['args']}".rstrip())

    def _vocabulary(self) -> Vocabulary:
        """This session's registry, re-read per use (see :func:`session_vocabulary`)."""
        return session_vocabulary(self._agent_session)

    async def _perform_command_outcome(self, dispatched: Dispatched) -> None:
        """Do the half of a dispatched command only a frontend can do.

        Routes on WHICH ARM came back, exactly as the TUI's counterpart does, and
        an arm with no branch RAISES: the core is allowed to resolve a command this
        head cannot perform, and the contract is that it says so out loud.

        Raises:
            UnsupportedCommandError: For a view this head does not have.
        """
        if isinstance(dispatched, Performed):
            self._report_performed(dispatched)
            return
        if isinstance(dispatched, FlowStep):
            await self._run_flow(dispatched)
            return
        if isinstance(dispatched, Ready):
            await self._perform_ready(dispatched)
            return
        if dispatched.name == "extensions":
            self._print_extensions()
            return
        raise UnsupportedCommandError(unsupported_command_message(dispatched.name, FRONTEND))

    def _report_performed(self, performed: Performed) -> None:
        """Print what a capability returned: its own output, else its one-line record."""
        output = performed.data.get("output")
        if isinstance(output, str) and output.strip():
            self._console.print(Markdown(output))
            return
        self._renderer.line("system", performed.summary())

    def _print_extensions(self) -> None:
        """The ``/extensions`` listing, read LIVE so a reload is reflected.

        The REPL's answer to the TUI's listing box: this head opens no view, and
        the state the view would be drawn from is a read anyone can make.
        """
        state = self._agent_session.get_extension_state()
        infos = summarize_extensions(state)
        if not infos and not state.errors:
            self._renderer.line("system", "no extensions loaded")
        for info in infos:
            self._renderer.line("extension", f"{info.name} — {info.path}")
            self._renderer.line("system", f"    tools: {', '.join(info.tools) or '(none)'}")
            self._renderer.line("system", f"    commands: {', '.join(info.commands) or '(none)'}")
            self._renderer.line("system", f"    hooks: {', '.join(info.hooks) or '(none)'}")
        for error in state.errors:
            self._renderer.line("error", f"failed to load {error.path}: {error.error}")
        self._print_shortcuts()

    def _print_shortcuts(self) -> None:
        """List the key shortcuts extensions registered — as commands to TYPE.

        A shortcut is an accelerator over a registered command, and a REPL has no
        key grabber (``ctrl+e`` is the line editor's end-of-line), so this head
        binds no chord and says how each one is reached instead: by typing the
        command it names, which goes through the one door like any other line
        (docs/REPL-HEAD.md §7).
        """
        getter = getattr(self._backend, "get_extension_shortcuts", None)
        if getter is None:
            return
        shortcuts = getter()
        if not shortcuts:
            return
        self._renderer.line("system", "shortcuts (this head binds no chord — type the command):")
        for key, command, args, description in shortcuts:
            invocation = f"/{command} {args}".rstrip()
            self._renderer.line("system", f"    ctrl+e {key} → {invocation} — {description}")

    def _pending_request(self) -> ExtensionRequest | None:
        """The extension request at the cursor, read from the live backend.

        docs/EXTENSION-LOCKS.md §2: one reader, so what this head draws, what it
        bounces a line against and what ``submit`` refuses are the same entry.
        """
        request: ExtensionRequest | None = getattr(self._backend, "pending_request", None)
        return request

    async def _show_request(self) -> None:
        """Draw the request at the cursor, if there is one, and open its ask.

        Called at every point the cursor can have moved — startup, a turn edge, a
        command, a swap, a bounced line. Where the TUI draws a row and opens the
        ask only sometimes (app.py:663, ``open_ask``), this head does both at once:
        a row is clickable and a scrollback line is not, so a request drawn without
        being asked would be one nothing could answer.
        """
        request = self._pending_request()
        if request is None:
            return
        self._print_request(request)
        await self._open_ask(request)

    def _print_request(self, request: ExtensionRequest) -> None:
        """Print τ's framing line, the extension's sentence, and any body.

        A lock with no ask is printed as :func:`refusal_reason` alone, which is
        the same three sentences plus the way out — printing the label and the
        sentence beside it would say each of them twice.
        """
        if request.ask is None:
            self._renderer.line("extension", refusal_reason(request))
            return
        self._renderer.line("extension", request.label)
        self._renderer.line("extension", request.sentence)
        if request.ask["body"] is not None:
            self._renderer.panel(request.ask["title"], render_panel_body(request.ask["body"]))

    async def _open_ask(self, request: ExtensionRequest) -> None:
        """Ask a request's fields and actions, unless its actions cannot run.

        Auto-opened only when every action names a registered command (app.py:682):
        an ask whose buttons would do nothing is drawn and explained rather than
        put in front of someone as a question with no answer.
        """
        if request.ask is None:
            return
        known = set(extension_command_names(self._backend))
        missing = sorted({action["command"] for action in request.ask["actions"]} - known)
        if missing:
            self._renderer.line(
                "error",
                f"this ask's actions name {', '.join('/' + name for name in missing)}, which no "
                "loaded extension registered, so there is nothing to answer it with.",
            )
            if request.lock:
                self._renderer.line("system", refusal_reason(request))
            return
        try:
            await self._ask_alone(lambda: self._collect_answer(request))
        except Exception as exc:
            self._report_fault(exc, f"answering {request.extension_name}")

    async def _collect_answer(self, request: ExtensionRequest) -> None:
        """Fill an ask's fields, pick an action, and answer through the core.

        The lock is released by the APPEND ``answer_request`` makes, so a dismissal
        (an empty answer, or Ctrl+C) leaves the session exactly as it was and the
        request is drawn again at the next cursor move — dismissing is not
        answering, and nothing is fabricated on the way past.
        """
        ask = request.ask
        assert ask is not None  # _open_ask returns for a request with none
        values = await ask_fields(self._reader, self._renderer, ask["fields"])
        if values is None:
            self._renderer.line("system", f"{request.label}: dismissed, and shown again later")
            return
        labels = [action["label"] for action in ask["actions"]]
        self._renderer.numbered(labels)
        answer = await self._reader.ask(
            f"which action? [1-{len(labels)}]", validate=_validate_select(labels)
        )
        if answer is None or not answer.strip():
            self._renderer.line("system", f"{request.label}: dismissed, and shown again later")
            return
        index = _as_index(answer.strip(), labels)
        if index is None:
            raise ValueError(f"{answer!r} passed the action validator and then named no action")
        try:
            result = await self._backend.answer_request(request.entry_id, labels[index], values)
        except ValueError as exc:
            self._renderer.line("error", f"the answer was refused: {exc}")
            return
        if not result.handled:
            self._renderer.line(
                "warning",
                f"{request.extension_name} is not loaded, so nothing ran — the request is "
                "answered and the session is unlocked.",
            )
        output = result.output_text()
        if output:
            self._console.print(Markdown(output))

    async def _bounce_if_locked(self, line: str) -> bool:
        """Refuse a prompt while an extension holds the session, and say why.

        Read HERE and not after the fact, the way app.py:882 reads it: the core
        refuses the same submission for the same reason, but by then the line has
        left the prompt. Reading one step early is what lets the refused text go
        back into the draft where it was typed.

        Args:
            line: What was about to be submitted.

        Returns:
            Whether the line was bounced.
        """
        request = self._pending_request()
        if request is None or not request.lock:
            return False
        self._renderer.line("error", refusal_reason(request))
        self._return_to_draft(line)
        await self._open_ask(request)
        return True

    async def _ask_alone(self, ask: Callable[[], Awaitable[_Answer]]) -> _Answer:
        """Run ``ask`` with the prompt to itself, then give the draft back.

        Two prompts must never coexist on one tty (docs/REPL-HEAD.md §5): a
        question raised while a steering read is outstanding cancels that read
        first, keeps its half-typed text, and re-opens it with that text restored
        when the question is done. With no read outstanding — a command at an idle
        prompt — this is just the call.

        Args:
            ask: The question to run.

        Returns:
            Whatever ``ask`` answered.
        """
        outstanding = self._read_task
        draft = ""
        if outstanding is not None and not outstanding.done():
            draft = self._reader.draft()
            self._read_task = None
            outstanding.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outstanding
        else:
            outstanding = None
        try:
            return await ask()
        finally:
            if outstanding is not None and not self._eof:
                if draft:
                    self._reader.set_draft(draft)
                self._read_task = asyncio.ensure_future(self._reader.read())
                # run() may already be waiting on the turn ALONE; the new read is news.
                self._wake.set()

    def _select_options(self, step: FlowStep) -> dict[str, dict[str, str]]:
        """Enumerate every argument of ``step``'s flow this head offers as a list.

        ``{argument: {label: value}}`` rather than a bare list, because a form hands
        back the string it DISPLAYED and two of these domains label a value with
        something other than the value.

        Args:
            step: The step whose flow is being asked about.

        Returns:
            One entry per select-rendered argument and per :data:`PICKER_DOMAINS`
            argument still unbound.

        Raises:
            ValueError: A domain could not be enumerated, or two of its values share
                a label — which would make the answer ambiguous rather than visibly
                wrong.
        """
        vocabulary = self._vocabulary()
        options: dict[str, dict[str, str]] = {}
        for argument in flow_arguments(step.flow, vocabulary):
            if argument.required and argument.name in step.bound:
                continue
            domain = vocabulary.domains[argument.domain]
            if domain.field_kind != "select" and domain.name not in PICKER_DOMAINS:
                continue
            found = enumerate_domain(
                domain.name,
                session=self._agent_session,
                runtime=self._runtime,
                scope=argument.scope,
                cursor=step.cursor,
                vocabulary=vocabulary,
            )
            by_label = {value.label: value.value for value in found.values}
            if len(by_label) != len(found.values):
                raise ValueError(
                    f"domain {domain.name!r} returned two values with the same label; "
                    "the form would not be able to say which was chosen"
                )
            options[argument.name] = by_label
        return options

    async def _run_flow(self, step: FlowStep) -> None:
        """Ask for what a flow still needs, then perform it (TUI-STYLE-GUIDE §2.2).

        Generic: it reads the STEP and the registry, never the flow's name, so a
        flow an extension declared is asked for like a built-in. No read is
        outstanding here — a command runs at an idle prompt and the loop re-issues
        the read only after this returns — so the ask owns the terminal alone.

        Raises:
            UnsupportedCommandError: The form and ``next_step`` disagree about what
                the flow still needs.
        """
        vocabulary = self._vocabulary()
        try:
            options = self._select_options(step)
            spec = flow_form_spec(
                step.flow,
                step.bound,
                options={name: list(values) for name, values in options.items()},
                vocabulary=vocabulary,
            )
            spec = _offer_pickers(spec, step, options, vocabulary)
        except (ValueError, KeyError) as exc:
            # A domain that cannot be listed is one refusal, not a traceback (app.py does this too).
            self._renderer.line("error", f"/{step.flow}: {exc}")
            return
        if not spec:
            raise UnsupportedCommandError(
                f"/{step.flow} needs {step.argument.name!r} and its form asks for nothing — "
                "next_step and flow_form_spec disagree about what is still unbound"
            )
        answers = await self._ask_alone(lambda: ask_form(self._reader, self._renderer, spec))
        if answers is None:
            return
        bound = dict(step.bound)
        for name, value in answers.items():
            bound[name] = _translate(options.get(name), value)
        outcome = next_step(step.flow, bound, cursor=step.cursor, vocabulary=vocabulary)
        if isinstance(outcome, Ready):
            await self._perform_ready(outcome)
            return
        raise UnsupportedCommandError(
            f"/{step.flow} still needs {outcome.argument.name!r} after its form was answered"
        )

    async def _perform_ready(self, ready: Ready) -> None:
        """Perform a bound flow and report what came back.

        Two mutations are the RUNTIME's rather than the backend's, because moving
        this head onto another session is not something a backend method does; the
        rest go through the backend method the capability names and report its
        :class:`Performed`. ``compact`` is the core's own
        :meth:`AgentSession.compact`, not the TUI's ``compact_messages``: this head
        BINDS its log, so the model's context is what the log yields and a
        shortened list returned to the head would be discarded (docs/REPL-HEAD.md §6).

        Raises:
            UnsupportedCommandError: The backend has no method for the mutation, or
                answered with something other than a ``Performed``.
        """
        if ready.mutation == "compact":
            await self._compact(ready.arguments.get("custom_instructions") or None)
            return
        if ready.mutation == "fork":
            await self._swap(self._runtime.fork(), "fork")
            return
        if ready.mutation == "switch_session":
            await self._swap(self._runtime.switch_session(ready.arguments["session_id"]), "resume")
            return
        if ready.flow in self._vocabulary().extension_flows:
            bound = " ".join(str(value) for value in ready.arguments.values())
            await self._run_extension_command(ready.flow, bound)
            return
        action = getattr(self._backend, ready.mutation, None)
        if action is None:
            raise UnsupportedCommandError(
                f"/{ready.flow} performs {ready.mutation!r}, which this backend does not offer"
            )
        result = action(**ready.arguments)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Performed):
            raise UnsupportedCommandError(
                f"/{ready.flow} performs {ready.mutation!r}, which this backend answered with "
                f"{type(result).__name__} rather than a Performed"
            )
        self._report_performed(result)

    async def _run_extension_command(self, name: str, args: str) -> None:
        """Run a flow an extension DECLARED, whose mutation is its own handler.

        The one call to ``run_extension_command`` this head makes, and it is
        legitimate here alone: the submission that produced this ``Ready`` already
        passed the input hooks and the lock check at the one door.
        """
        result = await self._backend.run_extension_command(name, args)
        if not result.handled:
            self._renderer.line("error", f"/{name} declared a flow but registered no handler")
            return
        if result.output:
            self._console.print(Markdown(str(result.output)))

    async def _compact(self, custom_instructions: str | None) -> None:
        """Compact the bound log — the core appends the entry, this head prints it."""
        result = await self._agent_session.compact(custom_instructions)
        if result is None:
            self._renderer.line("system", "nothing to compact yet")
            return
        self._renderer.line(
            "system",
            f"compacted {len(result.compacted_entry_ids)} entries "
            f"({result.tokens_before:,} tokens before, {result.tokens_saved:,} saved)",
        )

    async def _swap(self, swap: Any, verb: str) -> None:
        """Report a session swap the runtime performed, whichever way it ended.

        The new session's own request is NOT read here: every path that reaches a
        swap is a command, and :meth:`_run_command` re-reads once the outcome is
        performed — reading in both places would ask the same ask twice
        (docs/REPL-HEAD.md §3 step 7).
        """
        outcome = await swap
        if outcome.get("cancelled"):
            self._renderer.line("system", f"/{verb} was vetoed by an extension")
            return
        if outcome.get("blocked"):
            self._renderer.line("error", str(outcome.get("reason")))
            return
        self._renderer.line("system", f"{verb}: now on session {outcome['session_id']}")

    def on_rebind(self, session: Any) -> None:
        """The runtime swapped this head onto another session's log.

        Two of docs/REPL-HEAD.md §3 step 7's three: steering aimed at the session
        that just went away is handed back to the prompt rather than delivered
        into a conversation it was not typed for, and the transcript now in front
        of the head is drawn — a ``/resume`` that printed one id and no content
        left the reader unable to tell which conversation they were in. The third,
        the ``pending_request`` re-read, is :meth:`_run_command`'s, which every
        path to a swap goes through.

        Args:
            session: The ``AgentSession``, now reading the new log.
        """
        reclaimed = self._steering.reclaim()
        if reclaimed is not None:
            self._return_to_draft(reclaimed)
        self.replay(session.messages)

    def replay(self, messages: list[dict[str, Any]]) -> None:
        """Draw a transcript this head did not stream: a resume, a fork, a swap.

        Through the SAME handler a live turn goes through
        (``backends.replay_render_events``), so a reloaded conversation and a
        streamed one cannot drift apart in how they read (docs/REPL-HEAD.md §4).

        Args:
            messages: The folded active path — ``session.context``.
        """
        from tau_coding_agent.backends import replay_render_events

        for event in replay_render_events(messages):
            self._renderer(event)

    async def _run_turn(self, text: str, *, expand_commands: bool) -> None:
        """Admit one turn through the one door and report what came back.

        Every failure a turn can raise is caught HERE and not above: a provider
        error on turn 30 ends the turn, never the session (docs/REPL-HEAD.md §5).
        ``Exception`` and never ``BaseException``, so a cancellation or a real
        interrupt still unwinds.
        """
        sent, images = expand_attachments(text, self._config, self._renderer)
        submission = build_repl_submission(sent, images=images, expand_commands=expand_commands)
        self._lane = submission.submission_id
        self._renderer.spin(WAITING)
        try:
            result = await self._backend.submit_turn(submission, None)
            if not result.accepted:
                self._renderer.line(
                    "error", result.rejection_reason or "the submission was refused"
                )
                return
            self._report_truncation(result.messages)
            if result.command is not None:
                await self._perform_command_outcome(result.command)
        except UnsupportedCommandError as exc:
            self._renderer.line("error", str(exc))
        except Exception as exc:
            self._report_fault(exc)
        finally:
            self._renderer.spin(None)

    async def _run_command(self, line: str) -> None:
        """Admit one command through the command door and perform its outcome.

        A command is exempt from a lock by PLACEMENT (docs/EXTENSION-LOCKS.md §5),
        so nothing is bounced here; the request is re-read afterwards because a
        handler may have appended one, and because a release command's whole job
        is to move the cursor off the one that was there.
        """
        submission = build_repl_submission(line)
        try:
            try:
                result = await self._backend.submit_command(submission)
            except ValueError as exc:
                self._renderer.line("error", f"{line!r} was refused: {exc}")
                return
            if not result.accepted:
                self._renderer.line(
                    "error", result.rejection_reason or "the submission was refused"
                )
                return
            if result.command is None:
                raise UnsupportedCommandError(
                    f"{line!r} was dispatched as a command but AgentSession.submit ran a "
                    "TURN for it — an `input` hook transformed the text after this head "
                    "resolved it. The turn ran; it was rendered, but nothing performed."
                )
            await self._perform_command_outcome(result.command)
        except UnsupportedCommandError as exc:
            self._renderer.line("error", str(exc))
        except Exception as exc:
            self._report_fault(exc, line.split()[0])
        finally:
            await self._show_request()

    async def _deliver_steer(self, text: str) -> None:
        """Hand the buffer to the running turn, and keep it until it is woven in.

        The core parks an accepted ``"steer"`` submission in
        ``_pending_steer_messages`` and answers ``messages=[]``; ``abort()`` clears
        that list without telling anyone, so the raw text stays on the buffer's
        ``delivered`` list until the ``steer_message`` render event confirms the
        weave. Non-empty ``messages`` mean the turn had already ended and the core
        ran this as a turn of its own — no ``steer_message`` will come.

        Text no longer on ``delivered`` was reclaimed between the tool call and
        this task's first step, and delivering it now would put a line the user
        took back into the turn they took it back from.
        """
        if text not in self._steering.delivered:
            return
        # The buffer keeps the RAW line; only what is SENT carries the expansion.
        sent, images = expand_attachments(text, self._config, self._renderer)
        submission = build_repl_submission(
            sent, images=images, strategy="steer", expand_commands=False
        )
        try:
            result = await self._backend.submit_turn(submission, None)
        except Exception as exc:
            self._steering.discard(text)
            self._report_fault(exc)
            self._return_to_draft(text)
            return
        if not result.accepted:
            self._steering.discard(text)
            self._renderer.line(
                "error", result.rejection_reason or "the steering message was refused"
            )
            self._return_to_draft(text)
            return
        if result.messages:
            self._steering.discard(text)

    def _report_truncation(self, messages: list[dict[str, Any]]) -> None:
        """Say when this turn's answer was cut at the cap rather than finished.

        The pure reader print mode uses (``truncation.py``), on the head's own
        channel: ``tau -p`` writes stderr because its stdout is a transcript, and
        a REPL's scrollback IS the transcript, so the notice belongs in it.

        Args:
            messages: The turn's produced messages; the assistant ones carry
                ``stop_reason`` and the dropped-call count.
        """
        notice = truncation_notice(truncation_from_messages(messages), max_tokens=self._max_tokens)
        if notice is None:
            return
        self._renderer.line("error", notice)
        self._renderer.line("system", TRUNCATION_ADVICE)

    def _report_fault(self, exc: Exception, subject: str = "turn") -> None:
        """Print an unexpected fault with its traceback; the loop continues.

        Args:
            exc: What was raised.
            subject: What failed — a turn, or the command word that was typed.
        """
        self._renderer.line("error", f"{subject} failed: {type(exc).__name__}: {exc}")
        self._console.print(
            Syntax("".join(traceback.format_exception(exc)).rstrip(), "python", word_wrap=True)
        )


def _head_command(line: str) -> tuple[str, str] | None:
    """``(name, args)`` when ``line`` is one of THIS head's commands, else ``None``.

    Purely syntactic, like ``parse_command`` it is built on: the head's four words
    win over an extension that registered one, the same order τ's built-ins win in
    (``resolve_command``).
    """
    parsed = parse_command(line)
    if parsed is None or parsed[0] not in REPL_COMMANDS:
        return None
    return parsed


def _offer_pickers(
    spec: dict[str, Any],
    step: FlowStep,
    options: dict[str, dict[str, str]],
    vocabulary: Vocabulary,
) -> dict[str, Any]:
    """Turn a :data:`PICKER_DOMAINS` field from a text box into a numbered pick.

    The substitution a head is allowed to make (docs/TUI-STYLE-GUIDE.md §2.4):
    ``session_id`` renders as ``text`` because a person may have hundreds of
    sessions, and a prompt line can show the enumerated list where a modal shows a
    filtered picker.

    Args:
        spec: What ``flow_form_spec`` built.
        step: The step being asked about.
        options: The label→value maps :meth:`ReplLoop._select_options` enumerated.
        vocabulary: The registry the flow's arguments are declared in.

    Returns:
        The spec, with each picker field re-kinded ``select`` over its labels.

    Raises:
        ValueError: A picker domain enumerated nothing — a question with no
            answers, which is the refusal ``flow_form_spec`` already makes for an
            empty select.
    """
    if not spec:
        return spec
    domains = {argument.name: argument.domain for argument in flow_arguments(step.flow, vocabulary)}
    fields = []
    for field in spec["fields"]:
        if domains.get(field["name"]) in PICKER_DOMAINS:
            choices = list(options.get(field["name"]) or ())
            if not choices:
                raise ValueError(
                    f"/{step.flow} needs {field['name']!r}, whose domain "
                    f"{domains[field['name']]!r} enumerated nothing here — there is "
                    "no answer to offer"
                )
            field = {**field, "kind": "select", "options": choices}
        fields.append(field)
    return {**spec, "fields": fields}


def _translate(labels: dict[str, str] | None, answer: Any) -> Any:
    """Map what the form DISPLAYED back to the value the mutation takes.

    Args:
        labels: The label→value map for this argument, or ``None`` when the answer
            is already the value.
        answer: What :func:`ask_form` returned for it.

    Returns:
        The bound value; a list stays a list.
    """
    if labels is None:
        return answer
    if isinstance(answer, list):
        return [labels[item] for item in answer]
    return labels[answer]


def _reraise(task: asyncio.Task[Any]) -> None:
    """Let a background submission's own fault out, rather than swallowing it."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        raise error
