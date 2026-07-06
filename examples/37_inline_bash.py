"""Example 37: Inline Bash — ``!{cmd}`` expansion in prompts (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S62. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/inline-bash.ts``.

## What this shows

The ``input`` mutating hook (E6 §2 / S42) rewriting a user's prompt BEFORE the
turn's user node is created. ``!{command}`` patterns anywhere in the prompt
text are executed via a subshell and replaced with their (trimmed) stdout —
so ``What's in !{pwd}?`` becomes ``What's in /home/john/project?`` and *that*
rewritten text is the one and only copy that reaches the model, gets rendered,
and is persisted on the active path (the S42 pre-node invariant: an ``input``
transform is legal precisely because it runs before any node exists to
duplicate).

## Field contract (faithful, not lazy)

pi's ``pi.on("input", async (event, ctx) => ...)`` returns
``{action: "continue"}`` / ``{action: "transform", text, images}``. τ's ``input``
hook contract (``runner.py:356`` ``emit_input``) is flatter: a handler returns
``None``/``{}`` to pass through, ``{"prompt": <text>}`` to replace the running
prompt (chaining — a later handler sees an earlier one's rewrite), or
``{"handled": True}`` to consume the input with no turn at all. This port maps
pi's ``continue`` to "return nothing" and pi's ``transform`` to
``{"prompt": result}`` — same behavior, τ's actual shape.

pi shells out via ``pi.exec("bash", ["-c", command], {timeout})``; τ's
extension API has no process-exec primitive (see ``35_auto_commit_on_exit.py``'s
note — a repo-wide, deliberate non-goal), so this port uses ``subprocess.run``
directly, exactly as ``02_git_checkpoint.py`` and ``35_auto_commit_on_exit.py``
already do.

pi preserves whole-line ``!command`` (a different, PTY-level feature it does
not implement standalone here — its comment says "existing !command behavior")
by skipping expansion when the trimmed text starts with ``!`` but not ``!{``.
τ ships no ``!``-prefixed shell interception at all (roadmap §9 non-goals list:
"`user_bash`/PTY interception (no `!` shell feature in τ yet)"), but the same
guard is kept here verbatim — it costs nothing and it is what pi's source
actually does, so a user who types a literal ``!something`` (not ``!{...}``)
still gets it left alone rather than silently mangled.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.inline_bash import inline_bash_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["read"],
    extensions=[inline_bash_extension],
)
await session.prompt("The current branch is !{git branch --show-current}")
```

Or load directly through the public ``-e`` surface::

    tau -e examples/37_inline_bash.py
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any

#: Matches ``!{command}`` — pi parity: ``/!\{([^}]+)\}/g``.
PATTERN = re.compile(r"!\{([^}]+)\}")

#: Per-command execution timeout, in seconds (pi parity: ``TIMEOUT_MS = 30000``).
TIMEOUT_SECONDS = 30

#: Preview length for the expansion summary notify (pi parity: ``slice(0, 50)``).
_PREVIEW_LEN = 50


@dataclass(frozen=True)
class _Expansion:
    """One ``!{command}`` -> output (or error) mapping, for the summary notify."""

    command: str
    output: str
    error: str | None = None


def _run_command(command: str) -> _Expansion:
    """Execute ``command`` in a subshell and trim its output (pi parity: prefers
    stdout, falls back to stderr; a non-zero exit WITH stderr is an error entry;
    a raised exception — e.g. a timeout — is also an error entry, never a crash)."""
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as err:  # noqa: BLE001 — surfaced as an expansion error, not a crash
        return _Expansion(command=command, output=f"[error: {err}]", error=str(err))

    output = result.stdout or result.stderr or ""
    trimmed = output.strip()
    if result.returncode != 0 and result.stderr:
        return _Expansion(command=command, output=trimmed, error=f"exit code {result.returncode}")
    return _Expansion(command=command, output=trimmed)


def expand_inline_bash(text: str) -> tuple[str, list[_Expansion]]:
    """Replace every ``!{command}`` in ``text`` with its (trimmed) output.

    Returns ``(expanded_text, expansions)`` — an empty ``expansions`` list means
    no pattern matched (the caller should pass the input through unchanged, not
    transform to an identical copy). Whole-line ``!command`` (not ``!{...}``) is
    left untouched, preserving that syntax for whatever else may claim it.
    """
    stripped = text.lstrip()
    if stripped.startswith("!") and not stripped.startswith("!{"):
        return text, []

    matches = list(PATTERN.finditer(text))
    if not matches:
        return text, []

    result = text
    expansions: list[_Expansion] = []
    for match in matches:
        command = match.group(1)
        expansion = _run_command(command)
        expansions.append(expansion)
        replacement = expansion.output if not expansion.error else f"[error: {expansion.error}]"
        result = result.replace(match.group(0), replacement, 1)
    return result, expansions


def _summary(expansions: list[_Expansion]) -> str:
    """The human-readable "Expanded N inline command(s): ..." notify body (pi
    parity: one line per expansion, output truncated to ``_PREVIEW_LEN``)."""
    lines = []
    for e in expansions:
        status = f" ({e.error})" if e.error else ""
        preview = e.output if len(e.output) <= _PREVIEW_LEN else f"{e.output[:_PREVIEW_LEN]}..."
        lines.append(f'!{{{e.command}}}{status} -> "{preview}"')
    return "\n".join(lines)


def on_input(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
    """The ``input`` hook handler: expand ``!{...}`` in ``event["prompt"]``."""
    text = event.get("prompt") or ""
    expanded, expansions = expand_inline_bash(text)
    if not expansions:
        return None
    ctx.ui.notify(f"Expanded {len(expansions)} inline command(s):\n{_summary(expansions)}", "info")
    return {"prompt": expanded}


def inline_bash_extension(api: Any) -> None:
    """Extension entry point: expand ``!{cmd}`` in every prompt before it lands."""
    api.on("input", on_input)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/37_inline_bash.py`` → ``getattr(module, "register")``).
register = inline_bash_extension
