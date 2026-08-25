"""Where does a turn's reasoning stop reaching the screen?

    tau -p --mode json "your prompt here" | python scripts/reasoning_trace.py

Reads τ's JSONL bus stream on stdin and reports which content-block types each
event carried. It answers one question and nothing else: **does a ``thinking``
block reach the bus at all?**

A reasoning region is drawn from ``message_update`` events carrying a
``thinking`` block — ``TurnStream._feed_message_update`` diffs them into
``reasoning_delta``, and ``ChatDisplay`` streams that into the region. So:

* ``message_update`` carrying ``thinking`` > 0 — the reasoning reached the bus,
  and anything missing on screen is a rendering problem.
* only ``message_end`` carries ``thinking`` — the provider produced reasoning but
  never STREAMED it, so nothing drew it. That is a provider bug, not a TUI one.
* neither carries ``thinking`` — the model or gateway did not report reasoning
  for this request at all.

Written for the 0.9.4 report "if the model calls no tools, then there is no
[reasoning] block at all", which was not reproducible against the renderer: the
same event sequence drives a surviving region in
``tau-coding-agent/tests/test_chat_rendering.py``. Run this against the model
that shows the symptom, with a prompt that gets a plain answer and no tool call.

Nothing here is τ-specific beyond the event shape, and it makes no requests of
its own — it only reads what a run already printed.
"""

from __future__ import annotations

import json
import sys
from collections import Counter


def summarize(lines: list[str]) -> tuple[Counter, int, int]:
    """Count ``(event type, content block types)`` pairs over a JSONL stream.

    Returns the counter plus the two numbers the conclusion turns on: how many
    ``message_update`` and ``message_end`` events carried a ``thinking`` block.
    A line that is not JSON is skipped — the stream begins with a session header
    and a caller may have mixed in log output.
    """
    seen: Counter = Counter()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type", "?"))
        content = (event.get("message") or {}).get("content")
        if isinstance(content, list):
            types = sorted({str(b.get("type")) for b in content if isinstance(b, dict)})
        elif isinstance(content, str):
            # A plain-string body is legal and carries no blocks, so it can never
            # carry reasoning. Named rather than counted as "no content".
            types = ["<plain string>"]
        else:
            types = []
        seen[(kind, ",".join(types))] += 1

    updates = sum(c for (k, t), c in seen.items() if k == "message_update" and "thinking" in t)
    ends = sum(c for (k, t), c in seen.items() if k == "message_end" and "thinking" in t)
    return seen, updates, ends


def main() -> int:
    seen, updates, ends = summarize(sys.stdin.readlines())
    if not seen:
        print("no events read — did the run print anything?", file=sys.stderr)
        return 1

    for (kind, types), count in sorted(seen.items()):
        print(f"{count:5d}  {kind:22s}  {types}")

    print()
    print(f"message_update carrying a thinking block: {updates}")
    print(f"message_end    carrying a thinking block: {ends}")
    print()
    if updates:
        print("Reasoning reached the bus. If no region is drawn, it is a TUI problem.")
    elif ends:
        print(
            "Reasoning was produced but never streamed: the provider yielded no\n"
            "ThinkingDeltaEvent, so nothing ever reached the reasoning region."
        )
    else:
        print("No reasoning at all in this run — the model or gateway reported none.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
