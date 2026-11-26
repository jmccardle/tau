"""`BashGuard`: the built-in extension, off by default, that asks before a `bash` call that is
likely to print a lot (`tau ext enable bash_guard`).

A `pre_tool` hook matches the command against the option `patterns` (regexes; the built-in ones
are its default, so `/config bash_guard` shows them). A command already piped to `head`, `tail`
or `wc` is never asked about. On a match it asks the clients `reject | allow | always | truncate`:

- `reject` is the call's `Err`, which tells the model to narrow the command;
- `allow` runs it;
- `always` runs it and appends the pattern to the user's config file's own `allow` (never to what
  a project layer, an override or a node adds), so it holds in every session and shows in
  `/config bash_guard`;
- `truncate` asks for a grep pattern (declined: just the tail) and rewrites the call's `command`
  to `{ cmd } 2>&1 | grep -e PATTERN | tail -n 200`. The tree keeps the model's own arguments,
  so the result starts with `[bash_guard ran: ...]`, which tells the model (and a reader) what
  really ran.

A question nobody answers within `wait` seconds (a served session waits with no client connected
too), or that is declined or aborted, is decided by `unattended`. `ask` cannot say which of those
it was, so the refusal says only that nobody answered. `tau -p` never waits: it answers by rule,
so give it `--answer bash_guard=...` or `--decline bash_guard` (decided by `unattended`); with
neither, the run stops at the question.

Its `pre_tool` asks and may rewrite the call, so it keeps the default order (an edit, D28): with a
`permissions` `ask` rule for `bash` the person is asked twice, by `permissions` first, then here;
in plan mode, `plan_mode` refuses the call before either asks.

The options are what a person may tune for themselves: what is asked about, what is allowed for
good, what happens unattended and how long to wait. They are checked when set (a bad regex is a
`ConfigError`, nothing is written).
"""

import asyncio
import json
import re
import shlex
from dataclasses import dataclass
from typing import Annotated, Literal, cast

from tau._core import Outcome
from tau._handles import NodeHandle
from tau._protocols import CallDraft

TAIL = 200
BOUNDED = re.compile(r"\|\s*(?:head|tail|wc)\b")  # already piped to something short
BUILT_IN = (
    r"\bfind\s+/(?:\s|$)",
    r"\bcat\s+\S+\.(?:bin|iso|log|gz|zip|tar|db|sqlite|so|o)\b",
    r"\bnpm\s+(?:install|ci)\b",
    r"\bcargo\s+(?:build|test)\b",
    r"(?s)\bgit\s+log\b(?!.*(?:\s-n\s*\d|\s-\d|--max-count))",
    r"(?s)\bgrep\b(?=.*\s-\w*[rR])(?!.*\s-\w*l)",  # recursive, without -l
    r"(?s)\bjournalctl\b(?!.*\s-n\s*\d)",
    r"(?s)\bdocker\s+logs\b(?!.*--tail)",
)
"""The patterns `Options.patterns` starts as: commands that tend to print a lot."""


@dataclass(frozen=True)
class Options:
    """`BashGuard`'s options (`/config bash_guard`)."""

    # A list option's default is a tuple (a dataclass refuses a list); the value is a list.
    patterns: Annotated[list[str], "regexes of commands that print a lot"] = BUILT_IN  # pyright: ignore[reportAssignmentType]
    allow: Annotated[list[str], "patterns the user allowed for good"] = ()  # pyright: ignore[reportAssignmentType]
    unattended: Annotated[
        Literal["reject", "allow", "truncate"], "what happens when nobody answers"
    ] = "reject"
    wait: Annotated[float, "seconds to wait for an answer"] = 600.0

    def __post_init__(self) -> None:
        if self.wait <= 0:
            raise ValueError(f"wait must be more than 0 seconds, not {self.wait}")
        for pattern in (*self.patterns, *self.allow):
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"pattern {pattern!r}: {e}") from e

    def hit(self, command: str) -> str | None:
        """The first pattern, not allowed for good, that `command` matches."""
        if BOUNDED.search(command):
            return None
        return next(
            (p for p in self.patterns if p not in self.allow and re.search(p, command)),
            None,
        )


def _arguments(args_raw: str) -> tuple[dict[str, object], str] | None:
    """A `bash` call's arguments and their `command`, or `None` unless they are an object with a
    string `command`: the tool owns argument checking, and its `invalid arguments` says why."""
    try:
        args: object = json.loads(args_raw)
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    command = cast("dict[str, object]", args).get("command")
    return (cast("dict[str, object]", args), command) if isinstance(command, str) else None


class BashGuard:
    """The built-in extension `bash_guard` (the module docstring)."""

    name = "bash_guard"
    options = Options

    def __init__(self) -> None:
        self._ran: dict[tuple[int, str], str] = {}  # (batch node, call id) -> what really ran

    async def _ask(self, node: NodeHandle, text: str, expect: object) -> object:
        """`node.ask`, giving up after `wait` seconds: none connected, or none looking."""
        try:
            async with asyncio.timeout(node.options().wait):
                return await node.ask(text, expect)
        except TimeoutError:
            return None

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        """Ask about a `bash` call that matches a pattern, and act on the answer."""
        read = _arguments(call.args_raw) if call.tool == "bash" else None
        if read is None:
            return None
        args, command = read
        options = node.options()
        pattern = options.hit(command)
        if pattern is None:
            return None
        answer = await self._ask(
            node,
            f"bash would print a lot (matches /{pattern}/):\n  {command}",
            Literal["reject", "allow", "always", "truncate"],
        )
        verdict = answer or options.unattended
        if verdict == "always":
            # The file's own list, read now (siblings save too), not the layers' and nodes' fold.
            saved = cast("list[str]", node.saved_options().get("allow", []))
            if pattern not in saved:
                node.save_options(allow=[*saved, pattern])
        if verdict in ("allow", "always"):
            return None
        if verdict == "truncate":
            keep = (
                await self._ask(node, "grep -e what? (decline: just the tail)", str)
                if answer
                else None
            )
            pipe = f"grep -e {shlex.quote(str(keep))} | " if keep else ""
            ran = f"{{ {command}\n}} 2>&1 | {pipe}tail -n {TAIL}"
            call.args_raw = json.dumps({**args, "command": ran})
            self._ran[node.id, call.id] = ran
            return None
        who = "the user said no" if answer else "nobody answered"
        return (
            f"bash_guard did not run `{command}`: it matches /{pattern}/ and would print a lot "
            f"({who}). Narrow it (-n, -l, --tail, a path, | head) and call bash again."
        )

    async def pre_result(self, node: NodeHandle, call: CallDraft) -> str | None:
        """The tree keeps the model's own `args_raw`; tell the model what ran instead."""
        ran = self._ran.pop((node.id, call.id), None)
        if ran is None:
            return None
        note = f"[bash_guard ran: {ran}]\n"
        match call.outcome:
            case Outcome.Ok(value=value):
                call.outcome = Outcome.Ok(note + value)
            case Outcome.Err(error=error):
                call.outcome = Outcome.Err(note + error)
            case _:
                pass  # interrupted: no result to put the note on
        return None
