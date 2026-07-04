"""Example 21: Reminders — a four-rule discipline bank driven by the hooks (E2).

A stateful extension that watches what the agent *does* (via the ``tool_call`` and
``tool_result`` hooks) and, when a rule trips, injects an ephemeral
``<system-reminder>`` into the next LLM payload (via the ``context`` hook). This is
the pi "planning / implementing / evaluating" reminder bank (``pi_planning_\
implementing_evaluating.md §2``): four coding-discipline rules, each with its own
cooldown so a tripped rule nags **once** and then falls silent for a while instead
of screaming on every single turn.

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

* ``tool_call`` / ``tool_result`` **trigger** rules — a triggered rule becomes
  *pending*.
* ``context`` fires before every LLM call: it **drains** the pending set into a
  ``<system-reminder>`` message, and each drained rule then enters a cooldown of
  ``COOLDOWNS[rule]`` context calls during which it cannot fire again (even if it
  keeps being triggered). ``N`` = the number of context calls a rule stays silent
  after firing; on the ``N``-th following call the cooldown reaches zero and the
  rule may fire once more.

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

The reminder handlers **never veto** a call (``tool_call`` returns ``None``) and
**never patch** a result (``tool_result`` returns ``None``); they only accumulate
state. The nagging is delivered exclusively through the ``context`` seam.

Reference: EXTENSIONS-IMPLEMENTATION.md §E-demo-2, §8 S16.
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

    One instance per loaded extension (state is per-session). The three bound
    methods (:meth:`on_tool_call`, :meth:`on_tool_result`, :meth:`on_context`) are
    the hook handlers registered by :func:`reminders_extension`.
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
        """Advance cooldowns and return the rules that fire on this context call.

        A rule fires when it is pending AND off cooldown; firing arms its cooldown.
        A rule on cooldown decrements and stays silent this call even if pending.
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

    def on_tool_result(self, event: dict[str, Any], ctx: Any) -> None:
        """``tool_result`` handler: count consecutive failures, never patch.

        Returns ``None`` — no field patch. A tool's error streak reaching
        :data:`FAILURE_THRESHOLD` trips ``root-cause-after-2-failures``; a success
        resets that tool's streak.
        """
        tool_name = event.get("tool_name")
        if not tool_name:
            return None
        if event.get("is_error"):
            streak = self._failures.get(tool_name, 0) + 1
            self._failures[tool_name] = streak
            if streak >= FAILURE_THRESHOLD:
                self.trigger("root-cause-after-2-failures")
        else:
            self._failures[tool_name] = 0
        return None

    def on_context(self, event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """``context`` handler: drain pending rules into a ``<system-reminder>``.

        Fires before every LLM call. Returns ``{"messages": ...}`` with an appended
        reminder message when at least one rule fires, else ``None`` (leaving the
        payload untouched — no wasted injection).
        """
        fired = self._drain()
        if not fired:
            return None
        messages = event["messages"]
        messages.append(reminder_message(fired))
        return {"messages": messages}


def reminder_message(rules: list[str]) -> dict[str, Any]:
    """Build the injected user message carrying one ``<system-reminder>`` per rule."""
    body = "\n".join(f"<system-reminder>{REMINDER_TEXT[rule]}</system-reminder>" for rule in rules)
    return {"role": "user", "content": [{"type": "text", "text": body}]}


def reminders_extension(api: Any) -> None:
    """Extension entry point: register the reminder bank's three hook handlers."""
    bank = ReminderBank()
    api.on("tool_call", bank.on_tool_call)
    api.on("tool_result", bank.on_tool_result)
    api.on("context", bank.on_context)
