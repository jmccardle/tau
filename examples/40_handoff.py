"""Example 40: Handoff — τ-NATIVE FLAGSHIP: a focused continuation session (E9).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S63 row 2. No pi original — pi
would need ``newSession`` (which it does not expose to extensions) plus a
bespoke ``ctx.ui.custom`` popup to pick the summary and hand it to a new chat.
τ's tree ops make the whole thing two calls, because the session tree IS the
canon store (roadmap §1.1's "tree-as-backplane" thesis) and forking a session
is a first-class op (``ctx.fork``), not a frontend feature.

## What this shows

``/handoff [focus instructions]`` compresses the ENTIRE current conversation
into one summary node — ``ctx.summarize_branch(root_id, ...)`` (E3-ctx / S19)
re-parents the summary at the tree's ROOT, so every prior message drops out of
the active path (it stays on disk, just off the live branch — same mechanism
``23_context_surgeon``'s ``summarize_now`` tool uses, applied to the whole
history instead of one sub-branch) — and then ``ctx.fork(mode="export")``
(E3-ctx / S19) copies the now-condensed session into a brand-new session file
at that same cursor. The result: a second, on-disk session that starts from
nothing but the summary — a "focused continuation" ready to pick up the work
without the token weight of the original transcript. The command reports the
new file's path and the summary text via the S46 command-output channel (the
same display-only report box ``38_todo``'s ``/todos`` uses) — no custom UI, no
new session-lifecycle API, just the two ops the roadmap named.

## Why this is exactly two calls (and not a manual copy)

- ``summarize_branch`` is the SAME op the E3-ctx surface already exposes to
  tools (``23_context_surgeon``'s ``summarize_now``); this demo is the first to
  point it at the tree's root instead of a sub-branch, condensing everything.
- ``fork(mode="export")`` is documented (``extension_types.py``) to copy the
  session into a new file "optionally positioning the new file's cursor at
  ``entry_id``" — called with no ``entry_id`` here, so the export lands at
  whatever the SOURCE session's cursor already is, i.e. exactly the summary
  node just appended. The source log is untouched by the export itself (only
  the prior ``summarize_branch`` call mutated it, which is the intended
  "the working session also gets to breathe" side effect — the same tradeoff
  ``ctx.compact`` makes on the live session).

## Usage

    tau -e examples/40_handoff.py
    > ... (a long working session) ...
    > /handoff focus on the auth refactor decisions
    Handoff session created: /home/user/.tau/chats/....jsonl

    Summary:
    <the condensed history>

    Resume with:
      tau -p --session <path> "..."
"""

from __future__ import annotations

from typing import Any


async def _handoff_command(args: str, ctx: Any) -> str:
    """``/handoff [instructions]``: summarize-to-root, then export a fresh session."""
    custom_instructions = args.strip() or None

    entries = ctx.entries()
    # A brand-new session already carries bookkeeping entries (model change,
    # etc.) before a single word is exchanged — "nothing to hand off" means no
    # actual conversation, not a literally-empty log.
    if not any(e.get("type") in ("message", "customMessage") for e in entries):
        return "Nothing to hand off yet — start a conversation first."
    root_id = entries[0]["id"]

    ctx.ui.notify("Summarizing conversation for handoff", "info")
    await ctx.summarize_branch(root_id, custom_instructions=custom_instructions)

    # The branch_summary just appended is now the last raw entry (fork below
    # copies the log verbatim — it does not append to THIS session).
    summary_entry = ctx.entries()[-1]
    summary_text = str(summary_entry.get("summary", ""))

    new_path = await ctx.fork(mode="export")
    ctx.ui.notify(f"Handoff session created: {new_path}", "info")

    return (
        f"Handoff session created: {new_path}\n\n"
        f"Summary:\n{summary_text}\n\n"
        "Resume with:\n"
        f'  tau -p --session {new_path} "..."'
    )


def handoff_extension(api: Any) -> None:
    """Extension entry point: register the ``/handoff`` command."""
    api.register_command(
        "handoff",
        {
            "description": (
                "Summarize this conversation and export it as a fresh, focused continuation session"
            ),
            "handler": _handoff_command,
        },
    )


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/40_handoff.py`` → ``getattr(module, "register")``).
register = handoff_extension
