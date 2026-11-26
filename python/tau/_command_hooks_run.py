"""Running one hook command and reading what it answered (the `hooks` built-in).

`run_command` starts the command in its own process group with the event as JSON on its standard
input, bounded in time and in output; `interpret` turns what it wrote into a `Verdict`, by the
convention Claude Code, Codex and Gemini CLI share: exit 0 reads standard output (a JSON object is
the answer, other text is context where an event takes it), exit 2 blocks with standard error as
the reason, any other exit is a failure.

Every outcome the command can have is one of: a `Verdict`, or `HookFailed` naming the command and
what went wrong. A field τ does not support is a `HookFailed`: nothing a command returns is
dropped without a word.
"""

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from tau._command_hooks_file import (
    POST_TOOL,
    POST_TOOL_FAILURE,
    PRE_TOOL,
    PROMPT,
    SESSION_END,
    SESSION_START,
    STOP,
    Handler,
)

MAX_OUTPUT = 1 << 20
"""The most a command may write to each of standard output and standard error."""
MAX_CONTEXT = 10_000
"""The most text a command may add as context or feedback, as in Claude Code."""
_SHOWN = 160


class HookFailed(Exception):
    """A hook command that did not complete as a hook: it did not start, timed out, wrote too
    much, exited other than 0 and 2, or answered with something τ cannot act on. `message` names
    the command and why."""

    def __init__(self, handler: Handler, why: str) -> None:
        shown = handler.shown()
        shown = shown if len(shown) <= _SHOWN else shown[: _SHOWN - 1] + "…"
        super().__init__(f"hook {shown!r} {why}")


@dataclass(frozen=True)
class Ran:
    """What a finished command left: its exit code and the text of both streams."""

    code: int
    out: str
    err: str


@dataclass(frozen=True)
class Verdict:
    """What a command decided. `block` is the reason to refuse (a call, an input, or the end of a
    turn: the event says which), `ask` a question to put to the person (`""`: no reason given),
    `input` the arguments to run the call with instead, `output` the text of the result to give
    the model instead, `context` text to add (feedback, for a result), and `message` a line for
    the person, from `systemMessage`."""

    block: str | None = None
    ask: str | None = None
    input: Mapping[str, object] | None = None
    output: str | None = None
    context: str = ""
    message: str | None = None
    ignored: str | None = field(default=None, compare=False)
    """Plain text it wrote where τ has no use for it, which the caller says."""


def _kill(proc: "asyncio.subprocess.Process") -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def _read(stream: asyncio.StreamReader, what: str, handler: Handler) -> bytes:
    out = bytearray()
    while chunk := await stream.read(1 << 16):
        out += chunk
        if len(out) > MAX_OUTPUT:
            raise HookFailed(handler, f"wrote over {MAX_OUTPUT} bytes to {what}")
    return bytes(out)


async def _feed(proc: "asyncio.subprocess.Process", data: bytes) -> None:
    """Write the event, and close: a command that never reads its input is allowed to."""
    assert proc.stdin is not None
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.write(data)
        await proc.stdin.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.close()


async def run_command(
    handler: Handler, event: Mapping[str, object], cwd: Path, env: Mapping[str, str]
) -> Ran:
    """Run `handler`'s command in `cwd` with `event` as JSON on its standard input. `HookFailed`
    if it does not start, passes its `timeout`, or writes over `MAX_OUTPUT`; a timed-out command's
    whole process group is killed."""
    if sys.platform == "win32":
        raise HookFailed(handler, "cannot run: hooks are not available on Windows")
    argv = handler.args or ("/bin/sh", "-c", cast("str", handler.command))
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=dict(env),
            start_new_session=True,
        )
    except OSError as e:
        raise HookFailed(handler, f"did not start: {e.strerror or e}") from e
    assert proc.stdout is not None
    assert proc.stderr is not None
    data = json.dumps(event).encode("utf-8")
    try:
        async with asyncio.timeout(handler.timeout):
            _, out, err, code = await asyncio.gather(
                _feed(proc, data),
                _read(proc.stdout, "standard output", handler),
                _read(proc.stderr, "standard error", handler),
                proc.wait(),
            )
    except TimeoutError:
        raise HookFailed(handler, f"timed out after {handler.timeout:g} s and was killed") from None
    finally:
        _kill(proc)  # a command's own children outlive it unless the group goes too
        await proc.wait()
    return Ran(code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


# -- what a command answered

_SPECIFIC: Mapping[str, tuple[str, ...]] = {
    PRE_TOOL: ("permissionDecision", "permissionDecisionReason", "updatedInput"),
    POST_TOOL: ("additionalContext", "updatedToolOutput"),
    POST_TOOL_FAILURE: ("additionalContext",),
    PROMPT: ("additionalContext",),
    STOP: (),
    SESSION_START: (),
    SESSION_END: (),
}
"""The keys of `hookSpecificOutput` each event takes."""
_DECISIONS: Mapping[str, tuple[str, ...]] = {
    PRE_TOOL: ("approve", "block"),
    POST_TOOL: ("block",),
    POST_TOOL_FAILURE: ("block",),
    PROMPT: ("block",),
    STOP: ("block",),
}
"""The top-level `decision` values each event takes; the events not listed take none."""
_QUIET = ("suppressOutput",)
"""Keys that are accepted and mean nothing, as in Claude Code, where they have no effect."""
_SUPPORTED_WHY = {
    "defer": "τ has no deferred calls",
    "updatedMCPToolOutput": "τ has no MCP tools",
    "sessionTitle": "a hook does not name the session",
    "suppressOriginalPrompt": "a hook cannot withhold what the person typed",
    "initialUserMessage": "a hook does not start a conversation",
}


def _unsupported(handler: Handler, key: str, event: str) -> HookFailed:
    why = _SUPPORTED_WHY.get(key)
    return HookFailed(
        handler,
        f"returned {key!r}, which τ does not support at {event}" + (f": {why}" if why else ""),
    )


def _text(handler: Handler, value: object, key: str) -> str:
    if not isinstance(value, str):
        raise HookFailed(handler, f"returned {key!r} that is not text")
    if len(value) > MAX_CONTEXT:
        raise HookFailed(
            handler, f"returned {key!r} of {len(value)} characters; the limit is {MAX_CONTEXT}"
        )
    return value


def _specific(handler: Handler, event: str, raw: object) -> Verdict:
    if not isinstance(raw, dict):
        raise HookFailed(handler, "returned a hookSpecificOutput that is not an object")
    given = cast("dict[str, object]", raw)
    if given.get("hookEventName") != event:
        raise HookFailed(
            handler, f"returned hookSpecificOutput for {given.get('hookEventName')!r}, not {event}"
        )
    for key in given:
        if key != "hookEventName" and key not in _SPECIFIC[event]:
            raise _unsupported(handler, key, event)
    block = ask = output = None
    updated: Mapping[str, object] | None = None
    reason = _text(handler, given.get("permissionDecisionReason", ""), "permissionDecisionReason")
    match given.get("permissionDecision"):
        case None | "allow":
            pass
        case "deny":
            block = reason or "denied by a hook, which gave no reason"
        case "ask":
            ask = reason
        case other:
            raise (
                _unsupported(handler, str(other), event)
                if other == "defer"
                else HookFailed(handler, f"returned permissionDecision {other!r}")
            )
    if "updatedInput" in given:
        if not isinstance(given["updatedInput"], dict):
            raise HookFailed(handler, "returned an updatedInput that is not an object")
        updated = cast("dict[str, object]", given["updatedInput"])
    if "updatedToolOutput" in given:
        output = _text(handler, given["updatedToolOutput"], "updatedToolOutput")
    context = _text(handler, given.get("additionalContext", ""), "additionalContext")
    return Verdict(block=block, ask=ask, input=updated, output=output, context=context)


def _json(handler: Handler, event: str, text: str) -> Verdict:
    try:
        parsed: object = json.loads(text)
    except ValueError as e:
        raise HookFailed(handler, f"wrote JSON that does not parse: {e}") from None
    if not isinstance(parsed, dict):
        raise HookFailed(handler, "wrote JSON that is not an object")
    given = cast("dict[str, object]", parsed)
    if given.get("continue", True) is not True:
        raise _unsupported(handler, "continue", event) from None
    allowed = {"continue", "decision", "reason", "systemMessage", "hookSpecificOutput", *_QUIET}
    for key in given:
        if key not in allowed:
            raise _unsupported(handler, key, event)
    verdict = (
        _specific(handler, event, given["hookSpecificOutput"])
        if "hookSpecificOutput" in given
        else Verdict()
    )
    message = None
    if "systemMessage" in given:
        message = _text(handler, given["systemMessage"], "systemMessage")
    block = verdict.block
    if "decision" in given:
        decision = given["decision"]
        if decision not in _DECISIONS.get(event, ()):
            raise HookFailed(
                handler, f"returned decision {decision!r}, which τ does not support at {event}"
            )
        if decision == "block" and block is None:
            block = (
                _text(handler, given.get("reason", ""), "reason")
                or "blocked by a hook, which gave no reason"
            )
        elif decision == "approve":
            pass
    elif "reason" in given:
        raise HookFailed(handler, "returned a reason with no decision")
    return Verdict(block, verdict.ask, verdict.input, verdict.output, verdict.context, message)


_FEEDBACK = (POST_TOOL, POST_TOOL_FAILURE)
"""The events that have already happened: a block cannot undo them, so it is feedback."""
_CONTEXT_FROM_TEXT = (PROMPT,)
_NO_USE_FOR_TEXT = (SESSION_START,)


def interpret(handler: Handler, event: str, ran: Ran) -> Verdict:
    """What `ran` decided at `event`. `HookFailed` for an exit that is neither 0 nor 2 (and for 2
    at an event that has no one to tell), JSON that does not parse or has a field τ does not
    support at the event."""
    if ran.code == 2:  # noqa: PLR2004 - the convention's one blocking code
        reason = ran.err.strip() or "exited 2 with nothing on standard error"
        if event in (SESSION_START, SESSION_END):
            raise HookFailed(handler, f"exited 2 at {event}, which cannot be blocked: {reason}")
        if event in _FEEDBACK:
            return Verdict(context=_cap(handler, reason))
        return Verdict(block=reason)
    if ran.code != 0:
        why = f"exited {ran.code}"
        raise HookFailed(handler, why + (f": {ran.err.strip()}" if ran.err.strip() else ""))
    out = ran.out.strip()
    if out.startswith("{"):
        return _json(handler, event, out)
    if out and event in _CONTEXT_FROM_TEXT:
        return Verdict(context=_cap(handler, out))
    if out and event in _NO_USE_FOR_TEXT:
        return Verdict(ignored=out)
    return Verdict()


def _cap(handler: Handler, text: str) -> str:
    if len(text) > MAX_CONTEXT:
        raise HookFailed(
            handler, f"wrote {len(text)} characters of text; the limit is {MAX_CONTEXT}"
        )
    return text
