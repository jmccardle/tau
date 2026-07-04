"""Example 21: Reminders — a four-rule discipline bank driven by the hooks (E2).

A stateful extension that watches what the agent *does* (via the ``tool_call`` and
``tool_result`` hooks) and, when a rule trips, injects a ``<system-reminder>`` by
editing the **triggering ``tool_result`` node in place** — a DURABLE edit (E5 §3.3
/ S31). This is the pi "planning / implementing / evaluating" reminder bank
(``pi_planning_implementing_evaluating.md §2``): four coding-discipline rules, each
with its own cooldown so a tripped rule nags **once** and then falls silent for a
while instead of screaming on every single turn.

## Why a durable ``tool_result`` edit (E5 §3.2), not the retired ``context`` hook

E5 eliminated the per-call ``context`` transform: under the durable-hook invariant
the model's input for any LLM call is exactly the system prompt + the linear active
path, so an ephemeral per-send message-list mutation is a hidden divergence. Every
LLM round-trip past the first is preceded by a **real node** — the tool results of
the previous round — so the reminder rides *that* node instead. Draining the
pending rules in the ``tool_result`` hook and appending the ``<system-reminder>`` to
the result's ``content`` makes the nag a durable, reloadable part of the transcript:
the tree node, the on-disk node, and the wire bytes are the same object. The
following LLM call sees the edited node exactly as the interface shows it.

## The four rules

1. **tests-readonly** (cooldown 3) — a ``write`` / ``edit`` whose ``path`` lands on
   a test file. Tests encode the spec; the agent must change the implementation to
   satisfy them, not rewrite the tests to pass.
2. **root-cause-after-2-failures** (cooldown 4) — the *same* tool has returned an
   error twice in a row (tracked across ``tool_result`` events). Stop retrying the
   identical action; diagnose the cause first.
3. **scope-guard** (cooldown 2) — a ``write`` / ``edit`` whose ``path`` resolves
   **outside** the run's working directory (``ctx.cwd``). Keep edits inside the
   project unless explicitly told otherwise.
4. **no-new-deps** (cooldown 1) — an attempt to add a dependency: a ``bash``
   ``command`` running an installer (``pip install`` / ``npm install`` /
   ``poetry add`` / ``go get`` / …), or a ``write`` / ``edit`` to a dependency
   manifest (``requirements.txt`` / ``pyproject.toml`` / ``package.json`` / …).

## Cooldown semantics

State advances on the hooks that carry it:

* ``tool_call`` **triggers** rules — a triggered rule becomes *pending* — and never
  patches (observe, don't block; blocking is the gatekeeper's job, example 22).
* ``tool_result`` fires once per landed result. It first updates failure state
  (a two-in-a-row error trips ``root-cause-after-2-failures``), then **drains** the
  pending set into a ``<system-reminder>`` appended to *this* result's ``content``
  (the durable edit). Each drained rule then enters a cooldown of ``COOLDOWNS[rule]``
  tool-result events during which it cannot fire again (even if it keeps being
  triggered). ``N`` = the number of results a rule stays silent after firing; on the
  ``N``-th following result the cooldown reaches zero and the rule may fire once more.

Because every LLM round-trip after the first is preceded by the previous round's
tool results, "drain on the tool_result that precedes the next call" is the durable
equivalent of the retired "drain before the next call".

## Field contract

τ owns the tool-argument field names, so this reads ``event["input"]["path"]`` /
``event["input"]["command"]`` directly — no pi ``args ?? input`` dual-read. The
``tool_result`` error signal is ``event["is_error"]``; the working scope is
``ctx.cwd`` (pi parity: the runner always hands handlers a live context).

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.reminders import reminders_extension  # loaded via importlib in tests

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "edit", "bash"],
    extensions=[reminders_extension],
)
```

The ``tool_call`` handler **never vetoes** a call (returns ``None``); it only
accumulates state. The ``tool_result`` handler patches the result's ``content`` only
when a rule fires — appending a durable ``<system-reminder>`` — and otherwise returns
``None`` (result passes through untouched). The nagging is delivered exclusively by
that durable edit.

Reference: EXTENSIONS-IMPLEMENTATION.md §E-demo-2, §8 S16; EXTENSIONS-E5-WIRING.md
§3.2–§3.3 / S31 (durable-hook rework — the ``context`` hook is retired).
"""

from __future__ import annotations

import os
import re
from typing import Any

# ── rule identifiers, order, and per-rule cooldowns ──────────────────────────
# RULE_ORDER fixes a deterministic drain order (pi iterates rules in a stable
# order so the injected reminder text is reproducible).
RULE_ORDER: tuple[str, ...] = (
    "tests-readonly",
    "root-cause-after-2-failures",
    "scope-guard",
    "no-new-deps",
)

#: Context calls a rule stays silent after firing (the "3/4/2/1" bank).
COOLDOWNS: dict[str, int] = {
    "tests-readonly": 3,
    "root-cause-after-2-failures": 4,
    "scope-guard": 2,
    "no-new-deps": 1,
}

#: The reminder body injected for each rule (wrapped in ``<system-reminder>``).
REMINDER_TEXT: dict[str, str] = {
    "tests-readonly": (
        "Tests are read-only. Do not edit test files to make them pass — change "
        "the implementation under test so it satisfies the existing tests."
    ),
    "root-cause-after-2-failures": (
        "The same tool has failed twice in a row. Stop repeating the identical "
        "action; investigate the root cause before the next attempt."
    ),
    "scope-guard": (
        "That edit targets a path outside the working scope. Keep changes inside "
        "the project directory unless you were explicitly asked to go elsewhere."
    ),
    "no-new-deps": (
        "Do not add new dependencies. Solve the task with the packages already "
        "available in this project."
    ),
}

#: Number of consecutive same-tool errors that trips the root-cause rule.
FAILURE_THRESHOLD = 2

#: Path-based mutation tools (the ``path`` argument governs several rules).
WRITE_TOOLS: frozenset[str] = frozenset({"write", "edit"})

#: Dependency-manifest basenames whose edit trips ``no-new-deps``.
DEP_MANIFESTS: frozenset[str] = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
    }
)

#: A ``bash`` command that installs a package trips ``no-new-deps``. The
#: ``\binstall\b`` boundary means ``pip uninstall`` does NOT match (no word break
#: before "install" inside "uninstall").
_INSTALL_RE = re.compile(
    r"\b(pip3?|python\s+-m\s+pip|poetry|npm|yarn|pnpm|cargo|go|gem|bundle)\b"
    r".*\b(install|add|get)\b"
)


# ── pure predicates over the τ tool-argument fields ──────────────────────────


def _basename(path: str) -> str:
    """Final path segment, tolerating both ``/`` and ``\\`` separators."""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def is_test_path(path: str) -> bool:
    """True if ``path`` names a test file or lives under a ``tests``/``test`` dir."""
    normalized = path.replace("\\", "/")
    segments = normalized.split("/")
    if any(seg in ("tests", "test") for seg in segments):
        return True
    name = _basename(normalized)
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def is_dep_manifest(path: str) -> bool:
    """True if ``path``'s basename is a known dependency manifest."""
    return _basename(path) in DEP_MANIFESTS


def is_install_command(command: str) -> bool:
    """True if ``command`` looks like a package-installer invocation."""
    return bool(_INSTALL_RE.search(command))


def is_outside_scope(path: str, cwd: str) -> bool:
    """True if ``path`` (resolved against ``cwd``) escapes the ``cwd`` subtree."""
    root = os.path.abspath(cwd)
    target = os.path.abspath(os.path.join(cwd, path))
    return not (target == root or target.startswith(root + os.sep))


# ── the stateful reminder bank ───────────────────────────────────────────────


class ReminderBank:
    """Tracks rule state across hook calls and drains it into reminders.

    One instance per loaded extension (state is per-session). The two bound methods
    (:meth:`on_tool_call`, :meth:`on_tool_result`) are the hook handlers registered
    by :func:`reminders_extension`. :meth:`on_tool_result` both advances failure
    state and drains pending rules into a durable ``<system-reminder>`` appended to
    the result's ``content``.
    """

    def __init__(self) -> None:
        # Rules triggered but not yet injected.
        self._pending: set[str] = set()
        # rule -> context calls remaining before it may fire again (0 == ready).
        self._cooldown: dict[str, int] = {}
        # tool name -> consecutive error count (reset on a success).
        self._failures: dict[str, int] = {}

    # -- state transitions ----------------------------------------------------

    def trigger(self, rule: str) -> None:
        """Mark ``rule`` pending. Raises on an unknown rule (Fail-Early)."""
        if rule not in COOLDOWNS:
            raise ValueError(f"unknown reminder rule: {rule!r}")
        self._pending.add(rule)

    def _drain(self) -> list[str]:
        """Advance cooldowns and return the rules that fire on this drain.

        A rule fires when it is pending AND off cooldown; firing arms its cooldown.
        A rule on cooldown decrements and stays silent this drain even if pending.
        One drain happens per ``tool_result`` event (the durable injection point).
        """
        fired: list[str] = []
        for rule in RULE_ORDER:
            remaining = self._cooldown.get(rule, 0)
            if remaining > 0:
                self._cooldown[rule] = remaining - 1
                continue
            if rule in self._pending:
                fired.append(rule)
                self._pending.discard(rule)
                self._cooldown[rule] = COOLDOWNS[rule]
        return fired

    # -- hook handlers --------------------------------------------------------

    def on_tool_call(self, event: dict[str, Any], ctx: Any) -> None:
        """``tool_call`` handler: inspect the prepared call, never veto.

        Returns ``None`` unconditionally — the reminder bank observes, it does not
        block (blocking is the gatekeeper's job, example 22).
        """
        tool_name = event["tool_name"]
        tool_input = event.get("input") or {}
        cwd = getattr(ctx, "cwd", ".") or "."

        command = tool_input.get("command")
        if tool_name == "bash" and command and is_install_command(str(command)):
            self.trigger("no-new-deps")
            return None

        path = tool_input.get("path")
        if tool_name in WRITE_TOOLS and path is not None:
            path_str = str(path)
            if is_dep_manifest(path_str):
                self.trigger("no-new-deps")
            if is_test_path(path_str):
                self.trigger("tests-readonly")
            if is_outside_scope(path_str, cwd):
                self.trigger("scope-guard")
        return None

    def on_tool_result(self, event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """``tool_result`` handler: count failures, then drain into a durable edit.

        Two phases on each landed result:

        1. **Failure accounting** — a tool's error streak reaching
           :data:`FAILURE_THRESHOLD` trips ``root-cause-after-2-failures``; a
           success resets that tool's streak.
        2. **Durable injection** — drain the pending rules (:meth:`_drain`) and, when
           at least one fires, APPEND a ``<system-reminder>`` text block to *this*
           result's ``content`` and return the patched ``{"content": …}``. The
           patched content is what the loop persists as the tree node and sends on
           the next LLM call — one artifact, no ephemeral copy (E5 §3.3 / S31).

        Returns ``None`` when nothing fires (result passes through untouched).
        """
        tool_name = event.get("tool_name")
        if tool_name:
            if event.get("is_error"):
                streak = self._failures.get(tool_name, 0) + 1
                self._failures[tool_name] = streak
                if streak >= FAILURE_THRESHOLD:
                    self.trigger("root-cause-after-2-failures")
            else:
                self._failures[tool_name] = 0

        fired = self._drain()
        if not fired:
            return None
        # Append (never replace) so the tool's own output survives beneath the nag.
        content = list(event.get("content") or [])
        content.append({"type": "text", "text": reminder_body(fired)})
        return {"content": content}


def reminder_body(rules: list[str]) -> str:
    """Join one ``<system-reminder>`` line per fired rule (the durable edit's text)."""
    return "\n".join(f"<system-reminder>{REMINDER_TEXT[rule]}</system-reminder>" for rule in rules)


def reminders_extension(api: Any) -> None:
    """Extension entry point: register the reminder bank's two hook handlers.

    ``tool_call`` accumulates rule triggers; ``tool_result`` accounts failures and
    performs the durable ``<system-reminder>`` edit. The retired ``context`` hook is
    gone (E5 §3.2 / S31) — there is no per-call message-list transform.
    """
    bank = ReminderBank()
    api.on("tool_call", bank.on_tool_call)
    api.on("tool_result", bank.on_tool_result)
