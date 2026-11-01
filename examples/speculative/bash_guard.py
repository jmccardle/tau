"""bash_guard: ask before a `bash` call that is likely to print a lot.

A `pre_tool` hook matches the command against `Options.patterns`. On a match it asks the
frontend `reject | allow | always | truncate`. `truncate` asks again for a grep pattern and
rewrites the call's `command` to `{ cmd } 2>&1 | grep -e PATTERN | tail -n 200`. A reject is the
call's `Err`, written so the model can narrow the command. `always` is saved to the user's
config file (`NodeHandle.save_options(allow=...)`), appended to the file's own `allow`
(`saved_options`), never to what the project's layer, the overrides or a node add to it. It holds
in every session and shows in `/config bash_guard`. When nobody answers within `wait` seconds (a
question waits even with no client connected) `unattended` decides.
"""

import asyncio
import json
import re
import shlex
from dataclasses import dataclass
from typing import Annotated, Literal, cast

from tau import CallDraft, NodeHandle, Outcome

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


@dataclass(frozen=True)
class Options:
    """bash_guard's options."""

    # A list option's default is a tuple (a dataclass refuses a list); the value is a list.
    patterns: Annotated[list[str], "regexes of commands that print a lot"] = BUILT_IN  # pyright: ignore[reportAssignmentType]
    allow: Annotated[list[str], "patterns the user allowed for good"] = ()  # pyright: ignore[reportAssignmentType]
    unattended: Annotated[
        Literal["reject", "allow", "truncate"], "what happens when nobody answers"
    ] = "reject"
    wait: Annotated[float, "seconds to wait for an answer"] = 600.0

    def __post_init__(self) -> None:
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


class BashGuard:
    name = "bash_guard"
    options = Options

    def __init__(self) -> None:
        self.ran: dict[tuple[int, str], str] = {}  # (batch node, call id) -> what really ran

    async def ask(self, node: NodeHandle, text: str, expect: object) -> object:
        """`node.ask`, giving up after `wait` seconds: none connected, or none looking."""
        try:
            async with asyncio.timeout(node.options().wait):
                return await node.ask(text, expect)
        except TimeoutError:
            return None

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        if call.tool != "bash":
            return None
        try:
            args = json.loads(call.args_raw)
            command = args["command"]
        except (ValueError, KeyError, TypeError):
            return None  # the tool owns argument checking
        options = node.options()
        pattern = options.hit(command)
        if pattern is None:
            return None
        answer = await self.ask(
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
                await self.ask(node, "grep -e what? (decline: just the tail)", str)
                if answer
                else None
            )
            pipe = f"grep -e {shlex.quote(str(keep))} | " if keep else ""
            ran = f"{{ {command}\n}} 2>&1 | {pipe}tail -n {TAIL}"
            call.args_raw = json.dumps({**args, "command": ran})
            self.ran[node.id, call.id] = ran
            return None
        who = "the user said no" if answer else "nobody was there to approve it"
        return (
            f"bash_guard did not run `{command}`: it matches /{pattern}/ and would print a lot "
            f"({who}). Narrow it (-n, -l, --tail, a path, | head) and call bash again."
        )

    async def pre_result(self, node: NodeHandle, call: CallDraft) -> str | None:
        """The tree keeps the model's own `args_raw`; tell the model what ran instead."""
        ran = self.ran.pop((node.id, call.id), None)
        note = f"[bash_guard ran: {ran}]\n"
        match call.outcome:
            case Outcome.Ok(value=value) if ran:
                call.outcome = Outcome.Ok(note + value)
            case Outcome.Err(error=error) if ran:
                call.outcome = Outcome.Err(note + error)
        return None


extensions = [BashGuard()]  # what `chat.py --extension` adds
