"""``ext_kit.steer`` — the *in-loop steering* atom.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S58.

*Steering* is nudging a running agent from inside its own loop — not vetoing a
call (that is the gate, S55) and not spawning a helper (that is the spawn/stream
atoms, S53/S54), but shaping what the model reads and how a tool behaves as the
turn unfolds. This module distils three moves the research catalog kept
re-deriving by hand, each composed on τ's **public** surface and each obeying the
durable-hook invariant (E5 §1 — a steering edit is a durable node on the active
path, never a hidden per-call channel):

* :class:`ReminderBank` — the generalized ``21_reminders`` bank: a set of named
  rules, each with a *threshold* (how many bumps trip it) and a *cooldown* (how
  many drains it then stays silent), drained on the ``tool_result`` hook into a
  ``<system-reminder>`` **appended to the triggering result's content** — the
  durable edit E5 §3.3 sanctions (the tree node, the on-disk node, and the wire
  bytes are one object). ``21_reminders``'s four hard-coded rules become *data*.
* :class:`TurnDebouncer` — a turn-counting rate limiter: ``fire(key)`` returns
  ``True`` at most once per ``interval`` turns, so a periodic nudge (a budget
  check, a "still working?" ping) fires on a cadence instead of every turn.
* :func:`wrap_tool` — the pi *tool-override* pattern (``tool-override.ts``) as a
  helper: build a ``register_tool`` definition that **shadows a built-in of the
  same name** (pi/τ resolve extension tools last, so same-name wins), running a
  ``before`` hook (inspect / mutate args / short-circuit with a veto result) and
  an ``after`` hook (post-process the result) around the original built-in's
  execute.

**Fail-Early.** :class:`ReminderBank` raises on an unknown rule name (no silent
no-op), :meth:`ReminderBank.add` rejects a duplicate or a non-positive threshold,
:class:`TurnDebouncer` rejects a non-positive ``interval``, and :func:`wrap_tool`
raises on an unknown built-in name (it resolves the real built-in to delegate to;
a typo must not silently register a tool that does nothing).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias, Union

# ── reminder bank ────────────────────────────────────────────────────────────

#: The wrapper tag every reminder body rides in — the marker the model is trained
#: to treat as an out-of-band instruction (pi / τ ``<system-reminder>`` parity).
REMINDER_OPEN = "<system-reminder>"
REMINDER_CLOSE = "</system-reminder>"


@dataclass
class Rule:
    """One reminder rule: a named nag with a threshold and a cooldown.

    ``text`` is the reminder body (wrapped in ``<system-reminder>`` when drained).
    ``threshold`` is how many :meth:`ReminderBank.bump` calls trip the rule into
    the pending set (``1`` = trips on the first bump; :meth:`ReminderBank.trigger`
    always trips immediately regardless of threshold). ``cooldown`` is how many
    subsequent *drains* the rule then stays silent — the "nag once, then hush"
    discipline of the ``21_reminders`` bank (``0`` = eligible again on the very
    next drain).
    """

    name: str
    text: str
    threshold: int = 1
    cooldown: int = 0


class ReminderBank:
    """Threshold-and-cooldown rules drained into a durable ``<system-reminder>``.

    The generalized form of ``21_reminders``: where that demo hard-codes four
    coding-discipline rules, this bank takes rules as *data* (:meth:`add`) and
    owns the same state machine — a pending set, per-rule bump counters, and
    per-rule cooldowns — advanced by three inputs an extension feeds from its
    hooks:

    * :meth:`bump` (usually from ``tool_call`` / ``tool_result``) increments a
      rule's counter; on reaching its :attr:`Rule.threshold` the rule goes pending
      and the counter resets. :meth:`trigger` trips a rule immediately (threshold
      bypassed); :meth:`reset` clears a counter (e.g. on a success that breaks a
      failure streak).
    * :meth:`drain` (from ``tool_result``, once per landed result) advances every
      cooldown, fires the pending rules that are off cooldown, arms their
      cooldowns, and returns the fired names in registration order (a stable,
      reproducible drain order).
    * :meth:`patch_result` is the convenience that ties it to the hook: it drains
      and, when anything fired, returns the ``{"content": …}`` patch appending the
      rendered ``<system-reminder>`` block to *this* result's content — the
      durable edit (E5 §3.3). Returns ``None`` when nothing fired (result passes
      through untouched).

    State is in-memory (per loaded extension / per session), exactly like
    ``21_reminders``: a reload resets the counters, but the reminders *already
    drained* persist in the tree nodes they were appended to, so a reload replays
    them. This is backplane-adjacent steering state, not model context.
    """

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}
        self._order: list[str] = []
        #: Rules tripped but not yet drained.
        self._pending: set[str] = set()
        #: rule -> bumps accumulated toward its threshold (reset on trip).
        self._counts: dict[str, int] = {}
        #: rule -> drains remaining before it may fire again (0 == ready).
        self._cooldown: dict[str, int] = {}

    # -- rule registration ----------------------------------------------------

    def add(self, name: str, text: str, *, threshold: int = 1, cooldown: int = 0) -> Rule:
        """Register a rule and return it. Raises on a duplicate or bad threshold.

        ``threshold`` must be ``>= 1`` (a rule that trips on zero bumps is
        meaningless) and ``cooldown`` ``>= 0``. A duplicate ``name`` raises rather
        than silently overwriting an existing rule's config (Fail-Early).
        """
        if name in self._rules:
            raise ValueError(f"ReminderBank: rule {name!r} already registered")
        if threshold < 1:
            raise ValueError(f"ReminderBank: rule {name!r} threshold must be >= 1, got {threshold}")
        if cooldown < 0:
            raise ValueError(f"ReminderBank: rule {name!r} cooldown must be >= 0, got {cooldown}")
        rule = Rule(name=name, text=text, threshold=threshold, cooldown=cooldown)
        self._rules[name] = rule
        self._order.append(name)
        return rule

    def add_rule(self, rule: Rule) -> Rule:
        """Register a pre-built :class:`Rule` (validated like :meth:`add`)."""
        return self.add(rule.name, rule.text, threshold=rule.threshold, cooldown=rule.cooldown)

    def _require(self, name: str) -> Rule:
        rule = self._rules.get(name)
        if rule is None:
            raise ValueError(f"ReminderBank: unknown rule {name!r}")
        return rule

    # -- state transitions ----------------------------------------------------

    def bump(self, name: str, n: int = 1) -> bool:
        """Add ``n`` to ``name``'s counter; trip it pending on reaching threshold.

        Returns ``True`` if this bump tripped the rule (counter reached its
        threshold, so it goes pending and the counter resets), ``False`` otherwise.
        Raises on an unknown rule.
        """
        rule = self._require(name)
        count = self._counts.get(name, 0) + n
        if count >= rule.threshold:
            self._counts[name] = 0
            self._pending.add(name)
            return True
        self._counts[name] = count
        return False

    def trigger(self, name: str) -> None:
        """Trip ``name`` pending immediately, bypassing its threshold and counter."""
        self._require(name)
        self._counts[name] = 0
        self._pending.add(name)

    def reset(self, name: str) -> None:
        """Clear ``name``'s bump counter (e.g. on a success that ends a streak)."""
        self._require(name)
        self._counts[name] = 0

    def is_pending(self, name: str) -> bool:
        """Whether ``name`` is tripped and waiting for the next :meth:`drain`."""
        self._require(name)
        return name in self._pending

    def drain(self) -> list[str]:
        """Advance cooldowns and return the rules that fire on this drain.

        One drain per ``tool_result`` (the durable injection point). A rule fires
        when it is pending AND off cooldown; firing clears its pending mark and
        arms its cooldown. A rule on cooldown decrements and stays silent this
        drain even if pending. Fired names come back in registration order.
        """
        fired: list[str] = []
        for name in self._order:
            remaining = self._cooldown.get(name, 0)
            if remaining > 0:
                self._cooldown[name] = remaining - 1
                continue
            if name in self._pending:
                fired.append(name)
                self._pending.discard(name)
                self._cooldown[name] = self._rules[name].cooldown
        return fired

    # -- rendering + the durable edit -----------------------------------------

    def render(self, names: list[str]) -> str:
        """Join one ``<system-reminder>`` line per rule name (the edit's text)."""
        return "\n".join(f"{REMINDER_OPEN}{self._rules[n].text}{REMINDER_CLOSE}" for n in names)

    def patch_result(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Drain and, when anything fired, append the reminder to ``event`` content.

        The ``tool_result``-hook convenience: :meth:`drain`, and if any rule fires
        return ``{"content": [...original..., {"type": "text", "text": <reminders>}]}``
        — the durable edit the hook applies to the triggering result (the reminder
        becomes a real, reloadable part of the transcript, E5 §3.3). Returns
        ``None`` when nothing fired so the result passes through untouched.

        The original content is copied and *appended to* (never replaced), so the
        tool's own output survives beneath the nag.
        """
        fired = self.drain()
        if not fired:
            return None
        content = list(event.get("content") or [])
        content.append({"type": "text", "text": self.render(fired)})
        return {"content": content}


# ── turn debouncer ───────────────────────────────────────────────────────────


class TurnDebouncer:
    """Rate-limit an action to at most once per ``interval`` turns.

    A periodic nudge — a budget check, a "still on track?" ping, a re-plan prompt
    — should fire on a cadence, not on every ``turn_end``. Drive :meth:`tick` once
    per turn (from the ``turn_end`` hook), then gate the action on :meth:`fire`:
    it returns ``True`` at most once every ``interval`` ticks *per key* and records
    the fire, so distinct nudges (distinct ``key``s) debounce independently. The
    first :meth:`fire` for a fresh key always passes (no prior fire to space from).

    In-memory, per session — the same steering-state contract as
    :class:`ReminderBank` (a reload restarts the cadence; whatever a fire *did*
    persists in its own durable node).
    """

    def __init__(self, interval: int) -> None:
        if interval < 1:
            raise ValueError(f"TurnDebouncer: interval must be >= 1 turn, got {interval}")
        self.interval = interval
        self._turn = 0
        #: key -> the turn index at which it last fired.
        self._last_fired: dict[str, int] = {}

    def tick(self, n: int = 1) -> int:
        """Advance the turn counter by ``n`` (call once per ``turn_end``)."""
        if n < 0:
            raise ValueError(f"TurnDebouncer.tick: n must be >= 0, got {n}")
        self._turn += n
        return self._turn

    @property
    def turn(self) -> int:
        """The current turn index (number of :meth:`tick` s so far)."""
        return self._turn

    def ready(self, key: str = "") -> bool:
        """Whether ``key`` may fire now (never fired, or ``interval`` turns since)."""
        last = self._last_fired.get(key)
        return last is None or (self._turn - last) >= self.interval

    def fire(self, key: str = "") -> bool:
        """Fire ``key`` if :meth:`ready`, recording the fire; else return ``False``.

        The atomic gate: the common ``if debouncer.fire("nudge"): …`` idiom fires
        the action and stamps the current turn so the next ``interval - 1`` turns
        are suppressed for that key.
        """
        if not self.ready(key):
            return False
        self._last_fired[key] = self._turn
        return True

    def reset(self, key: str | None = None) -> None:
        """Forget the last-fire of ``key`` (or all keys when ``key`` is ``None``)."""
        if key is None:
            self._last_fired.clear()
        else:
            self._last_fired.pop(key, None)


# ── tool wrapping (the pi tool-override pattern) ─────────────────────────────

#: A tool result / arguments dict (the ``AgentToolResult.model_dump`` shape:
#: ``{"content": [...], "is_error": bool, ...}``). Kept loose — an extension may
#: hand back any content-block dict the loop accepts.
ToolResult: TypeAlias = dict[str, Any]

#: ``before(params, ctx)`` — inspect/mutate ``params`` in place, or return a
#: result dict to SHORT-CIRCUIT (a veto / canned reply; the original tool is not
#: called). Return ``None`` to proceed. May be sync or async.
BeforeHook: TypeAlias = Callable[..., Union[ToolResult, None, Awaitable["ToolResult | None"]]]

#: ``after(result, params, ctx)`` — return a replacement result dict, or ``None``
#: to pass the original result through unchanged. May be sync or async.
AfterHook: TypeAlias = Callable[..., Union[ToolResult, None, Awaitable["ToolResult | None"]]]

#: name -> the built-in tool class :func:`wrap_tool` shadows. Kept in sync with
#: ``sdk._resolve_tools``; wrap_tool builds the instance to delegate to.
_BUILTIN_TOOLS: dict[str, str] = {
    "read": "ReadTool",
    "write": "WriteTool",
    "edit": "EditTool",
    "bash": "BashTool",
    "ls": "LsTool",
    "grep": "GrepTool",
    "find": "FindTool",
}


def _resolve_builtin(name: str) -> Any:
    """Construct the built-in tool instance ``name`` shadows (Fail-Early).

    Raises ``ValueError`` on an unknown name — :func:`wrap_tool` delegates to the
    real built-in, so a typo must fail loudly rather than register a tool with
    nothing behind it.
    """
    class_name = _BUILTIN_TOOLS.get(name)
    if class_name is None:
        known = ", ".join(sorted(_BUILTIN_TOOLS))
        raise ValueError(f"wrap_tool: unknown built-in tool {name!r} (known: {known})")
    import tau_agent_core.tools as tools_pkg

    cls = getattr(tools_pkg, class_name)
    return cls()


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, else return it as-is."""
    if inspect.isawaitable(value):
        return await value
    return value


def wrap_tool(
    name: str,
    before: BeforeHook | None = None,
    after: AfterHook | None = None,
    *,
    tool: Any = None,
    label: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Build a ``register_tool`` definition that shadows the built-in ``name``.

    The pi *tool-override* pattern (``tool-override.ts``) as a helper: pass the
    returned dict to ``api.register_tool(...)`` and the extension's tool replaces
    the built-in of the same name for the LLM (τ resolves extension tools last,
    ``agent_session._build_turn_tools``). The wrapper runs, in order:

    1. ``before(params, ctx)`` — inspect the arguments, mutate ``params`` in place
       (e.g. clamp a limit, rewrite a path), or **short-circuit** by returning a
       result dict (a veto / audited-block / canned reply); when it returns
       non-``None`` the original tool is *not* called and that value is the result.
    2. the original built-in's ``execute`` (the instance resolved from ``name``,
       or the caller-supplied ``tool=`` — e.g. a pre-configured ``cwd`` instance
       or a test double), delegated exactly as the loop would call it.
    3. ``after(result, params, ctx)`` — post-process (audit-log, redact, annotate);
       return a replacement result dict, or ``None`` to pass the original through.

    ``label`` / ``description`` / ``parameters`` default to the shadowed built-in's
    (so the LLM sees the same schema); pass ``description`` to advertise the changed
    behaviour. Both hooks may be sync or async.

    Raises ``ValueError`` (via :func:`_resolve_builtin`) if ``name`` is not a known
    built-in and no ``tool=`` override is supplied.
    """
    original = tool if tool is not None else _resolve_builtin(name)

    async def _execute(
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Callable[..., Any] | None = None,
        ctx: Any = None,
    ) -> Any:
        if before is not None:
            pre = await _maybe_await(before(params, ctx))
            if pre is not None:
                return pre
        result = await _maybe_await(
            original.execute(
                tool_call_id=tool_call_id,
                args=params,
                signal=signal,
                on_update=on_update,
            )
        )
        if after is not None:
            post = await _maybe_await(after(result, params, ctx))
            if post is not None:
                return post
        return result

    return {
        "name": name,
        "label": label if label is not None else getattr(original, "label", name),
        "description": description if description is not None else original.description,
        "parameters": original.parameters,
        "execute": _execute,
    }


__all__ = [
    "AfterHook",
    "BeforeHook",
    "REMINDER_CLOSE",
    "REMINDER_OPEN",
    "ReminderBank",
    "Rule",
    "ToolResult",
    "TurnDebouncer",
    "wrap_tool",
]
