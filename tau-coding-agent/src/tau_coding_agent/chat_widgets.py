"""Collapsible chat widgets: reasoning regions, paired tool call+result boxes,
and exchange grouping.

Built on Textual's ``Collapsible`` (validated for deep nesting with no interior
scrollbars in ``tests/test_nested_collapsible_spike.py``). Collapse/expand is
the framework's reactive→CSS-display mechanism — widgets mount once as content
streams in; toggling never re-mounts the DOM.

Design (from the reasoning/TUI discussion):
- ``ReasoningRegion`` — a completion's reasoning, streamed live, collapsible and
  rendered distinctly from the answer (pi renders reasoning dim+italic).
- ``ToolBox`` — a tool call paired with its result in ONE collapsible: the
  collapsed title is the one-line call signature; the body holds args + result.
- ``ExchangeBox`` — groups one user→answer exchange's steps under a summary line
  ("N tools · X tok · M:SS"); the final answer streams inside it.

These have NO dependency on the Parley app module, so they import cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from textual.widget import Widget
from textual.widgets import Collapsible, Markdown
from textual.widgets.markdown import MarkdownStream


def _extension_display_name(blocked_by: str | None) -> str:
    """A short, readable name for the vetoing extension (S50).

    The runner attributes a veto by its bucket LABEL — a file path for a
    discovered/`-e` extension (``…/30_permission_gate.py``) or a ``module:qualname``
    for an inline factory. Show the file stem when it looks like a path, else the
    label verbatim; ``None`` (an unattributed block) reads as the honest
    ``"extension"`` rather than a fabricated name."""
    if not blocked_by:
        return "extension"
    if blocked_by.endswith(".py") or "/" in blocked_by:
        return Path(blocked_by).stem
    return blocked_by


# ──────────────────────────────────────────────────────────────────────────
# Formatting helpers (shared by the live and reload paths)
# ──────────────────────────────────────────────────────────────────────────


#: What a message body's text actually IS, chosen by the widget that renders it.
#:
#: ``"markdown"`` — the author wrote markdown and meant it: an assistant answer,
#: a τ-authored listing. Its blank lines are its paragraph breaks and its single
#: newlines are soft wraps, exactly as CommonMark says.
#:
#: ``"verbatim"`` — line-oriented output that is not markdown at all: a tool
#: result, shell/command output, a traceback, the text a user typed. Its line
#: breaks carry meaning, and markdown would collapse each one into a space.
ContentSource = Literal["markdown", "verbatim"]


class MarkdownLineFormatter:
    """Prepare a message body for a Textual ``Markdown`` widget, according to
    what the body's SOURCE is.

    A ``"verbatim"`` body — a tool result, an error listing, shell output — is not
    markdown, and markdown collapses each of its lone newlines into a space. Every
    newline is doubled so each line survives as its own paragraph. (Not inside a
    fenced code block, where a newline is already literal: doing it there is what
    used to put a blank line between every line of every code block.)

    A ``"markdown"`` body — an assistant's answer — is passed through untouched.
    Doubling there is actively wrong: a model that hard-wraps its prose at 80
    columns got a blank line between every wrapped line, so one sentence rendered
    as several paragraphs. The split is by SOURCE and never by inspecting the text,
    because the only text-level test available is "does this look hard-wrapped",
    and a heuristic that guesses wrong rejoins two lines of a tool result — the one
    thing the doubling exists to protect. The caller always knows which it has.

    Stateful by necessity: whether a newline is inside a fence depends on every
    line before it. The state is carried across :meth:`feed` calls, so a streaming
    caller can hand over one delta at a time and get exactly the document a single
    whole-text call would produce, no matter where the deltas split — including
    mid-newline. That is the property ``MessageBox.append_content_delta`` relies on.
    A ``"markdown"`` formatter tracks fences too, so :attr:`in_fence` reads the same
    either way even though nothing is rewritten.

    Fence detection is a line whose first non-space characters are ``` — an opener
    and its closer are not required to match, which is looser than CommonMark but
    is what a streamed body actually contains.
    """

    __slots__ = ("_in_fence", "_line", "_source")

    def __init__(self, source: ContentSource) -> None:
        # Required, with no default: "which kind of text is this" has no safe
        # guess, and a caller that has not decided is the bug (Fail-Early).
        self._source = source
        self._in_fence = False
        self._line = ""

    @property
    def source(self) -> ContentSource:
        """What this formatter was told the body is."""
        return self._source

    @property
    def in_fence(self) -> bool:
        """Whether the next character belongs to an open fenced code block."""
        return self._in_fence

    def feed(self, text: str) -> str:
        """Format the next fragment, continuing from the state so far."""
        out: list[str] = []
        double = self._source == "verbatim"
        for char in text:
            if char != "\n":
                out.append(char)
                self._line += char
                continue
            # The line just ended, so we can finally classify it. A fence
            # DELIMITER keeps its single newline too: doubling it would open the
            # code block with a blank line, or close it with one.
            is_delimiter = self._line.lstrip().startswith("```")
            out.append("\n" if (not double or self._in_fence or is_delimiter) else "\n\n")
            if is_delimiter:
                self._in_fence = not self._in_fence
            self._line = ""
        return "".join(out)


def format_tool_summary(name: str, arguments: object) -> str:
    """A one-line call signature, ``name(key=val, …)``, truncated for the
    collapsed title row. Values are shortened individually and the whole
    argument list is capped so the line stays scannable."""
    inner = ""
    if isinstance(arguments, dict) and arguments:
        parts = []
        for key, value in arguments.items():
            text = value if isinstance(value, str) else json.dumps(value, default=str)
            text = text.replace("\n", " ")
            if len(text) > 40:
                text = text[:39] + "…"
            parts.append(f"{key}={text}")
        inner = ", ".join(parts)
        if len(inner) > 60:
            inner = inner[:59] + "…"
    return f"{name}({inner})"


def format_tokens(n: int) -> str:
    """102700 → '102.7k'; small counts stay exact."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def format_duration(seconds: float) -> str:
    """186.0 → '3:06'."""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def format_telemetry(extra: dict[str, Any]) -> str | None:
    """Render one telemetry line from a completion's ``usage["extra"]`` dict (G4).

    The single formatter shared by the TUI exchange summary and
    ``examples/70_telemetry.py`` (which imports this) — one implementation, so the
    live readout and the example demo can never drift. ``extra`` is the
    server-reported, non-portable per-completion telemetry τ folds onto
    ``Usage.extra`` (llama.cpp's ``timings`` block + τ's own JSON-repair count);
    pass ``usage.get("extra") or {}``.

    Renders (each only when its source figure is actually present):

    * effective decode speed — ``timings.predicted_per_second`` as ``N.N t/s``;
    * the tool-arg JSON-repair count, when the completion reported one
      (``repairs``);
    * the number of tool calls dropped because the stream ended mid-``arguments``
      (``dropped_partial_tool_calls``), when the completion dropped any;
    * the forced-token share ``n_ff_total / predicted_n`` as ``forced=NN%`` — but
      ONLY when ``n_ff_total`` is present. Stock llama.cpp builds never send it (a
      jump-forward-fork-only field); omitting it is the honest move, never a
      fabricated ``0%``.

    Returns ``None`` when the completion carried no telemetry at all (no
    ``timings``, no ``repairs``) — the caller shows the summary exactly as it would
    without telemetry, never a stale or fabricated reading (Fail-Early).
    """
    timings = extra.get("timings") or {}
    parts: list[str] = []

    predicted_per_second = timings.get("predicted_per_second")
    if isinstance(predicted_per_second, (int, float)):
        parts.append(f"{predicted_per_second:.1f} t/s")

    repairs = extra.get("repairs")
    if isinstance(repairs, int):
        parts.append(f"repairs={repairs}")

    # Tool calls the provider refused to finish building because the stream was
    # incomplete — cancelled, or cut off at the output cap. The key is present ONLY
    # when something was actually dropped (``_build_final_message``), so this row
    # never reads "dropped=0"; its absence is what says nothing was lost. Without
    # it the drop is silent, which is the half of the Fail-Early contract the
    # provider cannot fulfil on its own: it declines to fabricate the call, and
    # this is the only place a user finds out one went missing.
    dropped = extra.get("dropped_partial_tool_calls")
    if isinstance(dropped, int):
        parts.append(f"dropped={dropped}")

    # n_ff_total is the fork-only forced-token count. Absent on stock builds —
    # omit the figure entirely rather than default it to 0 (Fail-Early: a 0%
    # forced share would claim the grammar forced nothing, which is not known).
    n_ff_total = timings.get("n_ff_total")
    predicted_n = timings.get("predicted_n")
    if isinstance(n_ff_total, (int, float)) and isinstance(predicted_n, (int, float)):
        if predicted_n > 0:
            parts.append(f"forced={n_ff_total / predicted_n:.0%}")

    return " · ".join(parts) if parts else None


# ──────────────────────────────────────────────────────────────────────────
# Widgets
# ──────────────────────────────────────────────────────────────────────────


class QuietCollapsible(Collapsible):
    """A ``Collapsible`` that does not scroll its container when it folds.

    Textual's own (8.2.7) ends ``_watch_collapsed`` with::

        if self.is_mounted:
            self.call_after_refresh(self.scroll_visible)

    which is right for a page of collapsibles a reader is clicking through, and
    wrong for a transcript: **these boxes fold without anyone asking.** A finished
    exchange folds behind its summary when a turn ends, a reasoning region folds
    when the answer starts, and a reload folds one of each per rebuilt span. Every
    one of those scrolled the transcript to put that box on screen — dragging a
    reader who was deliberately reading history, on the model's schedule, and
    landing a freshly reloaded conversation three rows short of its newest
    message.

    So the scroll is dropped and the two state-keeping lines are kept verbatim.
    The messages still post, so anything that wants to react to a fold still can;
    what changes is that the transcript decides where the transcript scrolls
    (``ChatDisplay._size_updated``, ``_finish_build``), which is the same
    ownership rule the window already follows.

    Keyboard focus is unaffected: ``Screen.set_focus`` brings a widget into view
    with ``scroll_to_center``, not with ``scroll_visible`` (verified, textual
    8.2.7), so tabbing to an off-screen box still scrolls to it.
    """

    def _watch_collapsed(self, collapsed: bool) -> None:
        self._update_collapsed(collapsed)
        if self.collapsed:
            self.post_message(self.Collapsed(self))
        else:
            self.post_message(self.Expanded(self))


class ReasoningRegion(QuietCollapsible):
    """A collapsible reasoning/thinking block that streams live.

    Kept distinct from the answer so it can be reviewed and collapsed
    independently. Expanded by default (matches pi, which shows reasoning by
    default); the streaming state machine may collapse it once answer/tool
    content begins.
    """

    def __init__(self, *, collapsed: bool = False) -> None:
        self._md = Markdown("")
        super().__init__(self._md, title="Thinking…", collapsed=collapsed)
        self.add_class("reasoning-region")
        self._text = ""
        # Lazily created by append_delta on the first streamed delta once the
        # inner Markdown has mounted; see append_delta/finish_stream.
        self._stream: MarkdownStream | None = None
        # Whether self._text has actually been parsed into self._md yet (D1).
        # A region that mounts (or is set_text'd) while COLLAPSED — e.g.
        # Parley._promote_answer's copy of a finished completion's reasoning,
        # which sets collapsed=True in the same beat it's created — has no
        # audience: the Contents container is `display: none`, so a full
        # Markdown parse there produces a DOM nobody sees (measured 104ms at
        # 2.2k reasoning tokens). Rendering is deferred until the region is
        # actually expanded; see on_mount/_on_collapsible_expanded/_render_now.
        self._rendered = False

    def on_mount(self) -> None:
        # A region that mounts already-collapsed has no audience yet -- defer
        # the parse to first expand (see _rendered's docstring above). One that
        # mounts expanded (the normal live-streaming default) renders right
        # away, exactly as before D1.
        if self.collapsed:
            return
        self._render_now()

    def _on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        """Render the buffered text the first time this region is opened.

        ``Collapsible._watch_collapsed`` posts this message on EVERY
        collapsed->False transition, including the one that happens inside
        ``Collapsible.__init__`` itself when a region is constructed with
        ``collapsed=False`` (our own default) — that message is queued before
        mount and can still be pending when a caller flips ``collapsed`` back
        to ``True`` synchronously afterwards (exactly what ``_promote_answer``
        and ``_reload_exchange`` do: create, ``set_text``, then collapse, all
        before the next await). By the time that stale message is finally
        delivered, ``self.collapsed`` already reads ``True`` again, so this
        checks the CURRENT state rather than trusting the message -- a truly
        stale event is a no-op, a real user/toggle expand renders.
        """
        if self.collapsed:
            return
        self._render_now()

    def _render_now(self) -> None:
        """Parse the buffered text into ``self._md``, once."""
        if self._rendered:
            return
        self._rendered = True
        if self._text:
            self._md.append(self._text)

    def set_text(self, text: str) -> None:
        # finalize_exchange flushes and then collapses in the same beat, both
        # calling set_text with the identical final string (plus append_delta,
        # via finish_stream, having already streamed it in) -- Markdown.update()
        # re-parses and remounts every block, so an unchanged string must be a
        # no-op (measured: 791ms -> 455ms per end-of-message at 4k reasoning
        # tokens). A no-op here means self._text was already this value, so the
        # pre-mount buffer (on_mount's flush) still sees it.
        if text == self._text:
            return
        self._text = text
        # Only touch the widget once this region has actually committed to
        # rendering (D1): before that -- not yet mounted, or mounted but still
        # collapsed and never opened -- stay buffered in self._text alone.
        # _render_now reads self._text fresh on first expand, so the latest
        # value wins even if set_text is called more than once before then.
        if self._rendered:
            self._md.update(text)

    def append(self, delta: str) -> None:
        """Whole-text convenience append (non-streaming callers, tests)."""
        self.set_text(self._text + delta)

    async def append_delta(self, delta: str) -> None:
        """Stream one delta into the reasoning body without a full rebuild.

        Uses ``Markdown.get_stream``/``MarkdownStream.write`` (Textual 8.2.7),
        which appends the fragment to the tail of the document instead of
        reparsing+remounting everything ``set_text``/``Markdown.update()``
        would. ``self._text`` is kept in sync on every call (not just on a
        throttled tick) so ``text``/``set_text``'s equality guard stay correct
        whether or not this delta was actually streamed yet.

        Mirrors ``on_mount``'s pre-mount buffering: a delta that arrives before
        the inner Markdown is mounted (or before this region is ever expanded,
        D1) is accumulated into ``self._text`` only — the eventual mount/expand
        catches the full buffered text up via ``append()`` (not ``update()`` —
        see its comment), and streaming resumes from there.
        """
        if not delta:
            return
        self._text += delta
        if not self._rendered:
            return
        if self._stream is None:
            self._stream = Markdown.get_stream(self._md)
        await self._stream.write(delta)

    async def finish_stream(self) -> None:
        """Stop this region's open stream, if any.

        Called at every point the active step stops being the streaming target
        (``_flush``, ``_collapse_active_reasoning``, ``finalize_exchange``) so
        no ``MarkdownStream`` background task is left running once the box may
        be collapsed, promoted from, or removed. Safe to call when nothing was
        ever streamed (idempotent no-op).
        """
        if self._stream is not None:
            stream, self._stream = self._stream, None
            await stream.stop()

    @property
    def text(self) -> str:
        return self._text

    def mark_done(self, seconds: float | None = None) -> None:
        """Freeze the title once reasoning is complete."""
        self.title = "Thought" if seconds is None else f"Thought for {format_duration(seconds)}"


class ToolBox(QuietCollapsible):
    """A tool call paired with its result in ONE collapsible.

    Collapsed (the default — matching pi's default-collapsed tool output) shows
    just the call signature; expanded shows the arguments and, once it arrives,
    the result. The collapsed title gains a ✓/✗ status mark when the result
    lands, so the one-liner reads as call + outcome.
    """

    def __init__(self, name: str, arguments: object, tool_call_id: str = "") -> None:
        self.tool_name = name
        self.tool_call_id = tool_call_id
        self._summary = format_tool_summary(name, arguments)
        self._args_md = Markdown(self._args_block(arguments))
        self._result_md = Markdown("")
        self._result_md.display = False  # hidden until a result arrives
        super().__init__(self._args_md, self._result_md, title=self._summary, collapsed=True)
        self.add_class("tool-box")
        self.has_result = False
        # Result body written before this box composed; flushed by on_mount.
        # ``Markdown.update()`` on an unmounted widget removes the old blocks and
        # mounts the new ones into nothing -- the title and the display flag
        # survive, the text does NOT (verified: 0 blocks after the box mounts).
        # A call and its result can arrive in the same synchronous burst off the
        # agent loop's queue, so this is reachable, and losing the body silently
        # is the failure this buffer exists to prevent.
        self._pending_result: str | None = None

    def on_mount(self) -> None:
        if self._pending_result is not None:
            self._result_md.update(self._pending_result)
            self._pending_result = None

    def _write_result_body(self, markdown: str) -> None:
        """Show ``markdown`` in the result body, buffering until this box mounts."""
        self._result_md.display = True
        if self._result_md.is_mounted:
            self._result_md.update(markdown)
        else:
            self._pending_result = markdown

    @staticmethod
    def _args_block(arguments: object) -> str:
        return "```json\n" + json.dumps(arguments, indent=2, default=str) + "\n```"

    def set_result(
        self,
        result_text: str,
        is_error: bool = False,
        *,
        blocked: bool = False,
        blocked_by: str | None = None,
    ) -> None:
        # A `tool_call` extension VETO (S50, anchor G11) is a DISTINCT presentation
        # from a generic errored result: a ⛔ mark and a "blocked by <ext>: <reason>"
        # body, so the user reads it as a policy block, not a tool failure.
        if blocked:
            who = _extension_display_name(blocked_by)
            self.title = f"⛔ {self._summary}"
            body = f"blocked by {who}: {result_text}"
            body = body if len(body) <= 2000 else body[:2000] + "\n…(truncated)"
            self._write_result_body(f"```\n{body}\n```")
            self.has_result = True
            self.add_class("box-blocked")
            return
        mark = "✗" if is_error else "✓"
        self.title = f"{mark} {self._summary}"
        body = result_text if len(result_text) <= 2000 else result_text[:2000] + "\n…(truncated)"
        self._write_result_body(f"```\n{body}\n```")
        self.has_result = True
        if is_error:
            self.add_class("box-error")


class ExchangeBox(QuietCollapsible):
    """Groups one user→answer exchange's steps (reasoning, tool calls, the final
    answer) under a single summary line.

    Expanded by default so streaming is visible; the title shows a live
    ``Working… · 1.2k out · ~83 chunks · 0:12`` readout while the exchange runs
    (:meth:`set_live`) and a ``N tools · X tok · M:SS`` summary once it finishes
    (:meth:`set_summary`). Steps are mounted into the collapsible body as they
    arrive.

    ``label`` names the lane this exchange belongs to when it is NOT the ordinary
    "a human typed this" one — ``"bus · nats_bus"``, ``"agent · fork:explore"``
    (B3-a). It rides on both the running title and the finished summary, because
    the whole point of rendering another source's turn is that the reader can tell
    it apart at a glance; ``None`` leaves both reading exactly as they always have.
    """

    def __init__(self, *, collapsed: bool = False, label: str | None = None) -> None:
        super().__init__(
            title="Working…" if label is None else f"{label} · Working…", collapsed=collapsed
        )
        self.add_class("exchange-box")
        self._tool_count = 0
        self._label = label
        if label is not None:
            self.add_class("exchange-foreign")

    def add_step(self, widget: Widget) -> None:
        """Mount a step widget into the exchange body, in arrival order.

        The exchange is mounted (and its ``Collapsible.Contents`` composed)
        before any step is added — the caller awaits the exchange mount — so the
        body container is always present here.
        """
        self.query_one(Collapsible.Contents).mount(widget)
        if isinstance(widget, ToolBox):
            self._tool_count += 1

    async def add_step_async(self, widget: Widget) -> None:
        """Like :meth:`add_step` but awaits the mount.

        The reload path builds an exchange synchronously — it writes reasoning,
        text and tool *results* into a step right after mounting it — so it must
        wait for the step (and its slots) to compose before touching them. The
        live path is network-paced and doesn't need to wait, so it uses the
        fire-and-forget :meth:`add_step`.
        """
        await self.query_one(Collapsible.Contents).mount(widget)
        if isinstance(widget, ToolBox):
            self._tool_count += 1

    @property
    def tool_count(self) -> int:
        return self._tool_count

    def set_live(self, *, seconds: float | None, output: int, chunks: int) -> None:
        """Update the running title while the exchange is still streaming.

        The liveness readout. Before this the title said ``Working…`` and then
        nothing on screen changed until the answer text started arriving — so a
        turn spent thinking, or waiting on a slow tool, was indistinguishable
        from a turn that had died.

        Three parts, and the distinction between them is the whole point:

        * ``N out`` is MEASURED. It is the sum of the per-completion
          ``usage.output_tokens`` this lane has been told, so it steps at each
          completion boundary (each tool call) and is omitted entirely until the
          first completion reports one. It is never an approximation of the
          completion currently in flight — no such measurement exists.
        * ``~N chunks`` is the completion in flight, and is labelled twice over:
          ``~`` and the word *chunks*. It counts stream events, not tokens. Most
          OpenAI-compatible servers send one token per chunk, but nothing
          guarantees that, so this never claims to be a token count.
        * the duration is wall-clock since the exchange opened.

        A part with nothing to say is left out rather than shown as zero: no
        completion has reported, or nothing is in flight (a tool is running).
        ``seconds=None`` omits the duration for the same Fail-Early reason
        :meth:`set_summary` does — an unknown time is not a 0:00 one.
        """
        parts = ["Working…"]
        if output:
            parts.append(f"{format_tokens(output)} out")
        if chunks:
            parts.append(f"~{chunks} chunk" + ("" if chunks == 1 else "s"))
        if seconds is not None:
            parts.append(format_duration(seconds))
        if self._label is not None:
            parts.insert(0, self._label)
        self.title = " · ".join(parts)

    def set_summary(
        self,
        *,
        tools: int,
        context: int,
        output: int,
        seconds: float | None = None,
        telemetry: str | None = None,
    ) -> None:
        """Finalize the title with the exchange's stats. A count of 0 is shown as
        0 — we never hide a real value behind a branch.

        ``context`` is how large the prompt had grown by the end of this exchange
        (the last completion's prompt size); ``output`` is what the exchange
        generated. They are reported separately and never summed: a prompt already
        contains every earlier turn, so ``context + output`` restates the whole
        conversation as if it were this exchange's cost.

        ``seconds`` is omitted on the reload path: wall-clock duration is not
        persisted, so a reconstructed exchange shows ``N tools · X ctx · Y out``
        without a fabricated time (Fail-Early).

        ``telemetry`` is the last completion's G4 readout (t/s · repairs ·
        forced-share, from :func:`format_telemetry`); it is per-completion, not an
        exchange aggregate like ``output``, so it is appended verbatim as one
        more ``·`` part when present. ``None`` (a provider that reported nothing)
        appends nothing — the summary reads exactly as it did before G4."""
        tool_label = f"{tools} tool" + ("" if tools == 1 else "s")
        parts = [tool_label, f"{format_tokens(context)} ctx", f"{format_tokens(output)} out"]
        if seconds is not None:
            parts.append(format_duration(seconds))
        if telemetry is not None:
            parts.append(telemetry)
        if self._label is not None:
            parts.insert(0, self._label)
        self.title = "✓ " + " · ".join(parts)
