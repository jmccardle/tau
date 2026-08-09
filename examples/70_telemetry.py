"""70 — Telemetry: llama.cpp `timings` + JSON-repair count on the status strip (W8/G4).

llama.cpp emits a per-completion ``timings`` block as a TOP-LEVEL sibling of
``usage`` on the final SSE chunk (``tau_ai.providers.openai._usage_from_openai``
folds it onto ``Usage.extra["timings"]`` verbatim — server-reported keys, never
filtered or renamed). ``tau_ai.providers.openai._build_final_message`` separately
counts, per completion, how many complete tool-call argument buffers needed a
JSON repair and folds that onto ``Usage.extra["repairs"]`` (only when the message
had at least one tool call with a non-empty argument buffer — a message with no
tool calls has no repair count to report).

This is the readout, wired as plain extension plumbing — **zero TUI code**.
``ExtensionContext.get_usage()`` already returns a copy of the last completion's
``usage`` dict (S45, anchor G14) and ``ctx.ui.set_status(key, text)`` already
drives the TUI's keyed status strip / the headless JSON status record (S67).
Every ``message_end`` notify event, this extension reads the landed usage and
refreshes one status slot with:

* effective decode speed — ``extra.timings.predicted_per_second`` tokens/sec;
* the repair count, when the completion had one (``extra.repairs``);
* the forced-token share ``n_ff_total / predicted_n`` — but ONLY when
  ``n_ff_total`` is present. Stock llama.cpp builds never send it (it is a
  jump-forward-fork-only field); the honest move on a stock server is to omit
  the figure entirely, never print a fabricated ``0%``.

This is the only grammar-agnostic way to verify a decode constraint was actually
applied: a forced-token share near 100% on a short, tightly-constrained
completion (e.g. the ``60_retrieval_review`` verdict fan-out) is direct evidence
the grammar drove the decode, independent of what the grammar itself was.

Run:  tau -e examples/70_telemetry.py
Requires a server that reports ``timings`` (llama.cpp) to see anything beyond
the repair count; against a server that reports neither, the status slot clears.
"""

from __future__ import annotations

from typing import Any

# ONE formatter, shared with the TUI exchange summary (G4). The rendering of
# t/s · repairs=N · forced=NN% lives next to format_tokens/format_duration in the
# coding-agent widgets, so the live readout and this demo can never drift.
from tau_coding_agent.chat_widgets import format_telemetry


def register(api: Any) -> None:
    def on_message_end(event: Any) -> None:
        usage = api.context.get_usage()
        if usage is None:
            return
        api.ui.set_status("telemetry", format_telemetry(usage.get("extra") or {}))

    api.on("message_end", on_message_end)
