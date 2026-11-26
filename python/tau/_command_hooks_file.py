"""The hooks file of the `hooks` built-in: `hooks.json`, read as Claude Code reads the `hooks`
key of its `settings.json`, so a block of hooks written for it can be pasted in.

```json
{"hooks": {"PreToolUse": [{"matcher": "bash", "hooks": [
    {"type": "command", "command": "./guard.sh", "timeout": 10}]}]}}
```

This module only reads: text in, `Rule`s out, or a `HookFileError` naming the file and every
problem. Nothing here is ignored silently. An event τ has no hook point for, a handler type, a
handler key or a matcher on an event that has no tool to match are each a problem naming what
τ does instead; the hooks file is never partly used.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from tau._config import ConfigError

MAX_BYTES = 1 << 16
"""A hooks file over this is refused: it is read in full by the person who trusts the project."""
DEFAULT_TIMEOUT = 60.0
"""Seconds a command may run, if its handler gives no `timeout`."""
SESSION_START_TIMEOUT = 20.0
SESSION_END_TIMEOUT = 4.0
"""The defaults for the two events that run inside τ's own limits (`START_TIMEOUT`,
`STOP_TIMEOUT`: 30 s and 5 s)."""
MAX_TIMEOUT = 3600.0

PRE_TOOL = "PreToolUse"
POST_TOOL = "PostToolUse"
POST_TOOL_FAILURE = "PostToolUseFailure"
PROMPT = "UserPromptSubmit"
STOP = "Stop"
SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
EVENTS = (PRE_TOOL, POST_TOOL, POST_TOOL_FAILURE, PROMPT, STOP, SESSION_START, SESSION_END)
"""The Claude Code events τ runs hooks for, and the τ hook point or lifecycle each is."""
TOOL_EVENTS = (PRE_TOOL, POST_TOOL, POST_TOOL_FAILURE)
"""The events whose matcher names a tool; the others have nothing to match."""

# Claude Code's other event names, so that a pasted settings block is refused with the reason
# and not with "unknown event".
_UNSUPPORTED_EVENTS = frozenset(
    [
        "Setup",
        "UserPromptExpansion",
        "PreSkillShellCommand",
        "PermissionRequest",
        "PermissionDenied",
        "PostToolBatch",
        "Notification",
        "MessageDisplay",
        "SubagentStart",
        "SubagentStop",
        "TaskCreated",
        "TaskCompleted",
        "StopFailure",
        "TeammateIdle",
        "InstructionsLoaded",
        "ConfigChange",
        "CwdChanged",
        "DirectoryAdded",
        "FileChanged",
        "WorktreeCreate",
        "WorktreeRemove",
        "PreCompact",
        "PostCompact",
        "PreModelSwitch",
        "PostModelSwitch",
        "Elicitation",
        "ElicitationResult",
    ]
)
# Handler keys Claude Code has and τ does not: each is an error, because ignoring one runs a hook
# in a way its author did not write.
_UNSUPPORTED_KEYS = {
    "if": "τ has no per-call permission-rule filter; narrow the matcher, or test in the command",
    "async": "τ runs a hook to its end before the step goes on",
    "asyncRewake": "τ runs a hook to its end before the step goes on",
    "shell": "τ runs `command` with the platform's POSIX shell (`sh`), or `args` with none",
    "statusMessage": "τ shows no status line for a hook",
    "once": "τ runs a hook every time its event happens",
}
_HANDLER_KEYS = ("type", "command", "args", "timeout", "onFailure")
_ALTERNATIVES = re.compile(r"[A-Za-z0-9_ ,|-]*")
_ALIASES = {"Bash": "bash", "Read": "read", "Write": "write", "Edit": "edit", "Grep": "grep"}
_ALIASES["Glob"] = "find"
"""Claude Code's names for the tools τ's base tools correspond to, which a matcher also takes."""

OnFailure = Literal["block", "continue"]


class HookFileError(ConfigError):
    """A hooks file that cannot be used, with every problem found in it."""


@dataclass(frozen=True)
class Matcher:
    """Which tool names a rule applies to: every one (both fields `None`), the exact names in
    `names`, or a regular expression searched in the name (Python's `re`, not JavaScript's)."""

    names: frozenset[str] | None = None
    regex: re.Pattern[str] | None = None

    @staticmethod
    def of(text: str) -> "Matcher":
        """The matcher Claude Code's syntax names: `""`, `"*"` or nothing is every tool; letters,
        digits, `_`, `-`, spaces, `,` and `|` only is a list of exact names; anything else is a
        regular expression. `re.error` if it is not one."""
        if text in ("", "*"):
            return Matcher()
        if _ALTERNATIVES.fullmatch(text):
            names = (n.strip() for n in re.split(r"[|,]", text))
            return Matcher(names=frozenset(n for n in names if n))
        return Matcher(regex=re.compile(text))

    def matches(self, tool: str) -> bool:
        """Whether `tool` is named, by its own name or by Claude Code's name for it."""
        wanted = {tool, *(cc for cc, tau in _ALIASES.items() if tau == tool)}
        if self.names is not None:
            return not self.names.isdisjoint(wanted)
        if self.regex is not None:
            return any(self.regex.search(n) for n in wanted)
        return True


@dataclass(frozen=True)
class Handler:
    """One command to run: `command` through the shell, or `args` as an argument vector with no
    shell; `timeout` seconds; what a failure does (`on_failure`)."""

    command: str | None
    args: tuple[str, ...] | None
    timeout: float
    on_failure: OnFailure

    def shown(self) -> str:
        """The command as a person reads it, for messages and the listing."""
        return self.command if self.command is not None else " ".join(self.args or ())


@dataclass(frozen=True)
class Rule:
    """A handler an event runs, for the tools `matcher` names, from `source` (a file's path)."""

    event: str
    matcher: Matcher
    handler: Handler
    source: str


def _timeout(value: object, event: str) -> float | str:
    if value is None:
        return {SESSION_START: SESSION_START_TIMEOUT, SESSION_END: SESSION_END_TIMEOUT}.get(
            event, DEFAULT_TIMEOUT
        )
    if isinstance(value, bool) or not isinstance(value, int | float):
        return f"timeout is {value!r}: seconds, a number"
    if not 0 < value <= MAX_TIMEOUT:
        return f"timeout is {value!r}: it must be above 0 and at most {MAX_TIMEOUT:g} seconds"
    return float(value)


def _keys(item: dict[str, object]) -> list[str]:
    """What is wrong with the keys of a handler, and with its `type`."""
    problems: list[str] = []
    for key in item:
        if key in _UNSUPPORTED_KEYS:
            problems.append(f"{key!r} is not supported: {_UNSUPPORTED_KEYS[key]}")
        elif key not in _HANDLER_KEYS:
            problems.append(f"unknown key {key!r}; a handler has {', '.join(_HANDLER_KEYS)}")
    kind = item.get("type", "command")
    if kind != "command":
        problems.append(
            f"type {kind!r} is not supported: τ runs `command` handlers only (no http, "
            "mcp_tool, prompt or agent hooks)"
        )
    return problems


def _handler(raw: object, event: str) -> Handler | list[str]:
    """The `Handler` the object says, or what is wrong with it."""
    if not isinstance(raw, dict):
        return [f"a handler must be an object, not {raw!r}"]
    item = cast("dict[str, object]", raw)
    problems = _keys(item)
    command, args = item.get("command"), item.get("args")
    if not isinstance(command, str) or not command.strip():
        problems.append("`command` is required: the command to run, as text")
    if args is not None and not (
        isinstance(args, list) and all(isinstance(a, str) for a in cast("list[object]", args))
    ):
        problems.append("`args` is a list of strings")
    timeout = _timeout(item.get("timeout"), event)
    if isinstance(timeout, str):
        problems.append(timeout)
    policy = item.get("onFailure", "block")
    if policy not in ("block", "continue"):
        problems.append(f"onFailure is {policy!r}: 'block' (the default) or 'continue'")
    if problems:
        return problems
    assert isinstance(command, str)
    assert isinstance(timeout, float)
    stop = cast("OnFailure", policy)
    if args is not None:
        return Handler(None, (command, *cast("list[str]", args)), timeout, stop)
    return Handler(command, None, timeout, stop)


def _matcher(raw: object, event: str) -> Matcher | str:
    text = raw if raw is not None else ""
    if not isinstance(text, str):
        return f"matcher is {text!r}: text"
    if event not in TOOL_EVENTS:
        if text not in ("", "*"):
            return (
                f"{event} has nothing to match: τ has no matcher there, so {text!r} would be "
                "ignored; remove it"
            )
        return Matcher()
    try:
        return Matcher.of(text)
    except re.error as e:
        return f"matcher {text!r} is not a regular expression: {e}"


def _group(event: str, raw: object, source: str, where: str) -> tuple[list[Rule], list[str]]:
    if not isinstance(raw, dict):
        return [], [f"{where}: a matcher group must be an object, not {raw!r}"]
    group = cast("dict[str, object]", raw)
    problems = [
        f"{where}: unknown key {k!r}; a matcher group has `matcher` and `hooks`"
        for k in group
        if k not in ("matcher", "hooks")
    ]
    matcher = _matcher(group.get("matcher"), event)
    if isinstance(matcher, str):
        problems.append(f"{where}: {matcher}")
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return [], [*problems, f"{where}: `hooks` must be a list of handlers"]
    rules: list[Rule] = []
    for i, item in enumerate(cast("list[object]", handlers)):
        handler = _handler(item, event)
        if isinstance(handler, list):
            problems += [f"{where}.hooks[{i}]: {p}" for p in handler]
        elif isinstance(matcher, Matcher):
            rules.append(Rule(event, matcher, handler, source))
    return rules, problems


def _event_problem(event: str) -> str:
    if event in _UNSUPPORTED_EVENTS:
        return (
            f"{event} is a Claude Code event τ has no hook point for; the events τ runs are "
            f"{', '.join(EVENTS)}"
        )
    return f"unknown event {event!r}; the events τ runs are {', '.join(EVENTS)}"


def parse_hooks(text: str, source: str) -> list[Rule]:
    """The rules in a hooks file's `text` (`source` names it in every message), in file order.
    `HookFileError` lists every problem; a file with no hook is fine and gives none."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise HookFileError(f"{source} is over {MAX_BYTES} bytes")
    try:
        data: object = json.loads(text)
    except ValueError as e:
        raise HookFileError(f"{source} is not JSON: {e}") from None
    if not isinstance(data, dict):
        raise HookFileError(f'{source}: the file is an object, {{"hooks": {{...}}}}')
    top = cast("dict[str, object]", data)
    problems = [f"unknown key {k!r}; the only key is `hooks`" for k in top if k != "hooks"]
    events = top.get("hooks", {})
    rules: list[Rule] = []
    if not isinstance(events, dict):
        problems.append("`hooks` is an object from event name to a list of matcher groups")
        events = {}
    for event, groups in cast("Mapping[str, object]", events).items():
        if event not in EVENTS:
            problems.append(_event_problem(event))
        elif not isinstance(groups, list):
            problems.append(f"{event} is a list of matcher groups")
        else:
            for i, raw in enumerate(cast("list[object]", groups)):
                found, bad = _group(event, raw, source, f"{event}[{i}]")
                rules += found
                problems += bad
    if problems:
        raise HookFileError(f"{source}:\n" + "\n".join(f"  {p}" for p in problems))
    return rules
