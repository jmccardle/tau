"""Example 39: Trigger-compact — self-managing context (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S63. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/trigger-compact.ts``.

## What this shows

A watcher that measures context usage after every turn via
``ctx.get_context_usage()`` (S45) and, the first time the estimated token count
CROSSES the configured threshold (armed → tripped edge, not "every turn while
over"), schedules a compaction with ``ctx.compact(defer=True)`` (S43's
``turn_end`` mutating hook fires *inside* the live agent loop, so — same
reasoning as ``23_context_surgeon``'s ``compact_now`` tool — compaction cannot
run immediately there; it is RECORDED and drained exactly once at the tail of
``prompt()``, the same site as auto-compaction). A ``/trigger-compact`` command
offers the same behaviour on demand, immediately (no live loop to defer
around), and reports the outcome via the S46 command-output channel.

## Faithful port, one necessary divergence

pi's ``turn_end`` is notify-only and its ``ctx.compact()`` is fire-and-forget
with ``onComplete``/``onError`` callbacks (no ``await``, agent-session.ts:2986).
τ's mutating ``turn_end`` (S43, D-E6-6) additionally lets a handler RETURN a
durable message — this demo does not use that: the watcher is a pure observer
(it returns nothing), because "self-managing context" here means a *side
effect* (schedule a compaction), not a message to inject. τ's ``ctx.compact``
is ``async``/awaitable and returns a ``CompactionResult`` (or ``None`` for the
deferred path, which only records intent — S20 decision 3), so this port
awaits it and reports through ``ctx.ui.notify`` instead of pi's callback pair;
behaviour (schedule once per threshold crossing, then compact) is unchanged.

## Usage

    tau -e examples/39_trigger_compact.py
    > ... (long conversation crosses the threshold) ...
    (a notify fires; the next prompt() call drains the deferred compaction)
    > /trigger-compact
    (compacts immediately, on demand)
"""

from __future__ import annotations

from typing import Any

#: Matches pi's ``COMPACT_THRESHOLD_TOKENS`` (trigger-compact.ts).
COMPACT_THRESHOLD_TOKENS = 100_000


def trigger_compact_extension(api: Any) -> None:
    """Extension entry point: a ``turn_end`` watcher + a ``/trigger-compact`` command."""
    # Armed/tripped edge state (in-memory; resets on reload — acceptable, mirrors
    # pi's closure-scoped ``previousTokens``: at worst a reload re-arms the watch,
    # it never double-fires within one live process).
    state: dict[str, int | None] = {"previous_tokens": None}

    async def on_turn_end(event: dict[str, Any], ctx: Any) -> None:
        """Pure observer (S43): schedule a deferred compaction on the crossing edge."""
        usage = ctx.get_context_usage()
        current_tokens = usage["tokens"] if usage is not None else None
        if current_tokens is None:
            return

        previous = state["previous_tokens"]
        crossed_threshold = previous is not None and previous <= COMPACT_THRESHOLD_TOKENS
        state["previous_tokens"] = current_tokens
        if not crossed_threshold or current_tokens <= COMPACT_THRESHOLD_TOKENS:
            return

        ctx.ui.notify("Compaction started", "info")
        try:
            await ctx.compact(defer=True)
        except Exception as err:  # noqa: BLE001 — surfaced via notify, not swallowed
            ctx.ui.notify(f"Compaction failed: {err}", "error")
            raise

    async def trigger_compact_command(args: str, ctx: Any) -> str:
        """``/trigger-compact``: run compaction immediately (no live loop to defer
        around — a command runs outside the agent loop, S46's output channel
        reports the result the way pi's ``onComplete``/``onError`` would have)."""
        instructions = args.strip() or None
        ctx.ui.notify("Compaction started", "info")
        try:
            result = await ctx.compact(custom_instructions=instructions)
        except Exception as err:  # noqa: BLE001 — surfaced to the caller, not lost
            ctx.ui.notify(f"Compaction failed: {err}", "error")
            return f"Compaction failed: {err}"
        ctx.ui.notify("Compaction completed", "info")
        if result is None:
            return "Nothing to compact yet."
        return (
            f"Compacted {len(result.compacted_entry_ids)} entries, "
            f"saved ~{result.tokens_saved} tokens.\n\nSummary:\n{result.summary}"
        )

    api.on("turn_end", on_turn_end)
    api.register_command(
        "trigger-compact",
        {
            "description": "Trigger compaction immediately",
            "handler": trigger_compact_command,
        },
    )


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/39_trigger_compact.py`` → ``getattr(module, "register")``).
register = trigger_compact_extension
