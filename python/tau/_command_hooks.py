"""`CommandHooks`: the built-in extension `hooks`, off by default. It runs shell commands at τ's
hook points, by the convention Claude Code, Codex and Gemini CLI share, so a hook script written
for one of them runs here: the command gets the event as JSON on its standard input, and answers
with its exit code (2 blocks, standard error is the reason) or a JSON object on standard output.

The commands come from `hooks.json` in `$TAU_HOME` and from `.tau/hooks.json` of a trusted
project, whose bytes trust approved (`tau trust`: a hooks file runs commands with your authority).
`_command_hooks_file` says what the file holds and `_command_hooks_run` how a command runs and is
read; this module maps the events onto τ's hook points:

- `PreToolUse` is `pre_tool`; a block is the call's error, which the model reads.
- `PostToolUse` and `PostToolUseFailure` are `pre_result`, for an `Ok` and for an `Err` outcome; a
  block is feedback appended to the result.
- `UserPromptSubmit` is `pre_input`, for what a person typed; a block halts the input, with the
  reason in a notice.
- `Stop` is `post_step`, when a person's turn ends in a reply that asks for no tool; a block is
  sent as input and the turn goes on.
- `SessionStart` and `SessionEnd` are the extension's `start` and `stop`; they block nothing, and
  a failure is told on standard error.

The commands of one event run one after another, in file order (the user's file, then the
project's), and the first block stops the rest, so what one command rewrites the next one sees.
This module is Unix-only; Windows has no POSIX shell here (as for the `bash` tool), and enabling
the extension there is an error naming the platform.
"""

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

from tau._command_hooks_file import (
    EVENTS,
    POST_TOOL,
    POST_TOOL_FAILURE,
    PRE_TOOL,
    PROMPT,
    SESSION_END,
    SESSION_START,
    STOP,
    HookFileError,
    Rule,
    parse_hooks,
)
from tau._command_hooks_run import HookFailed, Verdict, interpret, run_command
from tau._core import Author, Body, Draft, Ending, Node, Outcome, Part
from tau._events import NoticeLevel
from tau._extension_error import ExtensionLoadError
from tau._handles import Cursor, NodeHandle, Subject, TreeHandle
from tau._hook_order import GUARD, OBSERVE
from tau._protocols import CallDraft
from tau._startup import LoadedConfig

NAME = "hooks"
FILE = "hooks.json"
MAX_STOP_BLOCKS = 8
"""How many times in a row a `Stop` hook may keep one person's turn going (Claude Code's cap)."""
_UNAVAILABLE = (
    "hooks are not available on Windows: they run through a POSIX shell, which τ does not have "
    "there (the bash tool is unavailable for the same reason)"
)
NO_HOOKS = "no hooks (none in $TAU_HOME/hooks.json or the trusted project's .tau/hooks.json)"
Answer = Literal["yes", "no"]
Tell = Callable[[str, NoticeLevel], None]


class CommandHooksUnavailable(OSError):
    """The `hooks` extension on a platform it does not run on (Windows)."""


def _read_user(home: Path) -> tuple[str, str] | None:
    """`(path, text)` of the user's hooks file, or `None` if there is none."""
    path = home / FILE
    try:
        return str(path), path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise HookFileError(f"{path} cannot be read: {e}") from e


def _distinct(rules: Sequence[Rule]) -> list[Rule]:
    """`rules` with a repeat of an earlier rule left out: the same hook in the user's file and
    the project's runs once."""
    seen: list[tuple[object, ...]] = []
    out: list[Rule] = []
    for rule in rules:
        key = (rule.event, rule.matcher, rule.handler)
        if key not in seen:
            seen.append(key)
            out.append(rule)
    return out


def _arguments(call: CallDraft) -> dict[str, object] | None:
    """The call's arguments as an object, or `None` if the model wrote something else."""
    try:
        parsed: object = json.loads(call.args_raw)
    except ValueError:
        return None
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else None


def _reply_text(node: Node) -> str:
    body = node.body
    if not isinstance(body, Body.Assistant):
        return ""
    return "".join(p.text for p in body.parts if isinstance(p, Part.Text))


def _turn_ended(node: Node) -> bool:
    """Whether `node` is a reply that asks for no tool: the end of the agent's turn."""
    body = node.body
    return isinstance(body, Body.Assistant) and not any(
        isinstance(p, Part.Call) for p in body.parts
    )


def _person_turn(walk: Sequence[Node]) -> bool:
    """Whether the newest `User` node that is not a hook's own feedback was typed by a person: a
    sub-agent's task and a summary's prompt are an extension's, and are not a turn to stop."""
    for node in reversed(walk):
        if isinstance(node.body, Body.User) and node.by != Author.Extension(NAME):
            return isinstance(node.by, Author.Frontend)
    return False


class CommandHooks:
    """The built-in extension `hooks`: shell commands at τ's hook points (the module docstring).
    `CommandHooks.of(loaded)` reads the user's `hooks.json` and the trusted project's; `rules` are
    what it found."""

    name = NAME
    order: Mapping[str, int] = {
        "pre_input": GUARD,
        # Before `permissions`: a command that rewrites a call's arguments must do so before the
        # rules judge them.
        "pre_tool": GUARD - 50,
        "pre_result": OBSERVE,
        "post_reply": OBSERVE,
        "post_step": OBSERVE,
    }

    def __init__(self, rules: Sequence[Rule], cwd: Path, project: Path | None = None) -> None:
        if sys.platform == "win32":
            raise CommandHooksUnavailable(_UNAVAILABLE)
        self.rules = tuple(_distinct(rules))
        self._cwd = cwd
        self._root = project or cwd
        self._blocked: set[str] = set()  # calls a hook refused, which no result hook follows
        self._stops: dict[int, int] = {}  # cursor -> Stop blocks in a row, in this person's turn
        self._replied: dict[int, int] = {}
        self.commands: tuple[Callable[..., object], ...] = (self._listing(),)

    @classmethod
    def of(cls, loaded: LoadedConfig) -> "CommandHooks":
        """The extension for the hooks files `loaded` can read: the user's, then the trusted
        project's approved bytes. `HookFileError` names every problem if a file cannot be used."""
        rules: list[Rule] = []
        if (user := _read_user(loaded.home)) is not None:
            rules += parse_hooks(user[1], user[0])
        root = None
        if loaded.project is not None:
            root = loaded.project.root
            if loaded.project.hooks_file is not None:
                path = str(root / ".tau" / FILE)
                rules += parse_hooks(loaded.project.hooks_file.decode("utf-8"), path)
        return cls(rules, Path.cwd(), root)

    @classmethod
    def load(cls, loaded: LoadedConfig) -> "CommandHooks":
        """`of`, for the extension loader: a file it cannot use, or a platform with no shell,
        stops the start naming why."""
        try:
            return cls.of(loaded)
        except (HookFileError, CommandHooksUnavailable) as e:
            raise ExtensionLoadError(str(e)) from None

    # -- listing

    def _listing(self) -> Callable[..., object]:
        def hooks() -> str:
            """List the shell commands the `hooks` extension runs, by event."""
            if not self.rules:
                return NO_HOOKS
            return "\n".join(
                f"{r.event}"
                + (
                    ""
                    if r.event not in (PRE_TOOL, POST_TOOL, POST_TOOL_FAILURE)
                    else f" {_shown_matcher(r)}"
                )
                + f": {r.handler.shown()}  ({r.handler.timeout:g} s, on failure "
                + f"{r.handler.on_failure}; {r.source})"
                for r in self.rules
            )

        return hooks

    # -- running

    def _matching(self, event: str, tool: str | None = None) -> list[Rule]:
        return [
            r for r in self.rules if r.event == event and (tool is None or r.matcher.matches(tool))
        ]

    def _event(self, name: str, **fields: object) -> dict[str, object]:
        return {"hook_event_name": name, "cwd": str(self._cwd), **fields}

    def _environ(self, event: str) -> dict[str, str]:
        return {
            **os.environ,
            "TAU_PROJECT_DIR": str(self._root),
            "CLAUDE_PROJECT_DIR": str(self._root),
            "TAU_HOOK_EVENT": event,
        }

    async def _run(
        self, tell: Tell, rule: Rule, payload: Mapping[str, object], *, gate: bool
    ) -> Verdict:
        """The verdict of one command. A command that fails is told (`tell`) as an error naming
        it. At a `gate` (a call or an input, which a block refuses) and with `onFailure` `block`,
        the default, the failure also refuses it; elsewhere, or with `continue`, what was
        happening goes on."""
        try:
            ran = await run_command(rule.handler, payload, self._cwd, self._environ(rule.event))
            verdict = interpret(rule.handler, rule.event, ran)
        except HookFailed as e:
            tell(str(e), "error")
            return Verdict(block=str(e) if gate and rule.handler.on_failure == "block" else None)
        if verdict.message:
            tell(f"hook {rule.handler.shown()!r}: {verdict.message}", "warning")
        if verdict.ignored:
            tell(
                f"hook {rule.handler.shown()!r} wrote text at {rule.event}, which τ has no use "
                "for there; it was dropped",
                "warning",
            )
        return verdict

    # -- hooks

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        """`UserPromptSubmit`: a command may refuse what a person typed, or add context to it."""
        draft = subject.draft
        if draft is None or not isinstance(draft.by, Author.Frontend):
            return None
        if not isinstance(draft.body, Body.User):
            return None
        self._stops.pop(cursor.id, None)  # a new turn
        context: list[str] = []
        for rule in self._matching(PROMPT):
            payload = self._event(PROMPT, cursor=cursor.id, prompt=draft.body.text)
            verdict = await self._run(_to(cursor), rule, payload, gate=True)
            if verdict.block is not None:
                return verdict.block
            if verdict.context:
                context.append(verdict.context)
        if context:
            text = "\n\n".join([draft.body.text, *context])
            subject.draft = Draft(Body.User(text, draft.body.images), draft.delivers, draft.by)
        return None

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        """`PreToolUse`: a command may refuse the call, rewrite its arguments, or ask the person."""
        asked: list[str] = []
        for rule in self._matching(PRE_TOOL, call.tool):
            arguments = _arguments(call)
            payload = self._event(
                PRE_TOOL,
                cursor=node.cursor_id,
                tool_name=call.tool,
                tool_input=arguments,
                tool_use_id=call.id,
            )
            if arguments is None:
                payload["tool_input_raw"] = call.args_raw
            verdict = await self._run(_to(node), rule, payload, gate=True)
            if verdict.block is not None:
                self._blocked.add(call.id)
                return verdict.block
            if verdict.input is not None:
                call.args_raw = json.dumps(verdict.input)
            if verdict.ask is not None:
                asked.append(verdict.ask or f"hook {rule.handler.shown()!r} asks")
        if not asked:
            return None
        question = f"{call.tool}: {call.args_raw}\n(asked by a hook: {'; '.join(asked)})"
        answer: Answer = await node.ask(question, Answer)
        if answer == "yes":
            return None
        self._blocked.add(call.id)
        return "the user refused this call"

    async def pre_result(self, node: NodeHandle, call: CallDraft) -> str | None:
        """`PostToolUse` and `PostToolUseFailure`: a command sees the result, may replace its
        text (`updatedToolOutput`), and may add feedback the model reads after it."""
        if call.id in self._blocked:
            self._blocked.discard(call.id)  # a refused call did not run: no result to report
            return None
        outcome = call.outcome
        if isinstance(outcome, Outcome.Ok):
            event, result = POST_TOOL, {"tool_response": outcome.value}
        elif isinstance(outcome, Outcome.Err):
            event, result = POST_TOOL_FAILURE, {"error": outcome.error}
        else:
            return None
        text = outcome.value if isinstance(outcome, Outcome.Ok) else outcome.error
        feedback: list[str] = []
        for rule in self._matching(event, call.tool):
            payload = self._event(
                event,
                cursor=node.cursor_id,
                tool_name=call.tool,
                tool_input=_arguments(call),
                tool_use_id=call.id,
                **result,
            )
            verdict = await self._run(_to(node), rule, payload, gate=False)
            if verdict.output is not None:
                text = verdict.output
            feedback += [t for t in (verdict.block, verdict.context) if t]
        if feedback or text != (
            outcome.value if isinstance(outcome, Outcome.Ok) else outcome.error
        ):
            joined = "\n\n".join([text, *(f"hook feedback: {f}" for f in feedback)])
            call.outcome = (
                Outcome.Ok(joined, outcome.images)
                if isinstance(outcome, Outcome.Ok)
                else Outcome.Err(joined)
            )
        return None

    async def post_reply(self, cursor: Cursor, subject: Subject) -> None:
        """Note which reply this step committed: a step that only came to rest on one (a cursor
        moved there) is not a turn that ended."""
        if subject.node is not None:
            self._replied[cursor.id] = subject.node.id

    async def post_step(self, cursor: Cursor, subject: Subject) -> None:
        """`Stop`: when a person's turn ends in a reply that asks for no tool, a command may keep
        it going: its reason is sent as input. After `MAX_STOP_BLOCKS` in a row the turn ends."""
        replied = self._replied.pop(cursor.id, None)
        rules = self._matching(STOP)
        if not rules or not isinstance(subject.ending, Ending.Done):
            return
        node = cursor.node()
        if replied != node.id or not _turn_ended(node) or not _person_turn(cursor.walk()):
            return
        reasons: list[str] = []
        for rule in rules:
            payload = self._event(
                STOP,
                cursor=cursor.id,
                stop_hook_active=self._stops.get(cursor.id, 0) > 0,
                last_assistant_message=_reply_text(node),
            )
            verdict = await self._run(_to(cursor), rule, payload, gate=False)
            if verdict.block is not None:
                reasons.append(verdict.block)
                break
        if not reasons:
            self._stops.pop(cursor.id, None)
            return
        blocks = self._stops.get(cursor.id, 0) + 1
        if blocks > MAX_STOP_BLOCKS:
            cursor.tell(
                f"a Stop hook kept the turn going {MAX_STOP_BLOCKS} times in a row; "
                f"the turn ends. Its last reason: {reasons[0]}",
                level="warning",
            )
            return
        if not cursor.cursor(cursor.id).auto:
            cursor.tell(
                f"a Stop hook asked the turn to go on, but auto-step is off: {reasons[0]}",
                level="warning",
            )
            return
        self._stops[cursor.id] = blocks
        cursor.send(cursor.id, f"Stop hook feedback: {reasons[0]}")

    # -- the session's start and end

    async def start(self, tree: TreeHandle) -> None:
        """`SessionStart`: run the commands, each given `source: "startup"`."""
        await self._session(SESSION_START, source="startup")

    async def stop(self) -> None:
        """`SessionEnd`: run the commands, each given `reason: "other"`."""
        await self._session(SESSION_END, reason="other")

    async def _session(self, event: str, **fields: object) -> None:
        """A failure is on standard error, and τ goes on: a start or an end has nothing to
        refuse, whatever `onFailure` says."""
        for rule in self._matching(event):
            await self._run(_stderr, rule, self._event(event, **fields), gate=False)


def _to(handle: TreeHandle) -> Tell:
    """Tell the people connected, through `handle`."""

    def tell(text: str, level: NoticeLevel) -> None:
        handle.tell(text, level=level)

    return tell


def _stderr(text: str, level: NoticeLevel) -> None:
    """Tell standard error: at a session's start no client is connected yet, and at its end none
    is left to hear."""
    print(f"tau: {NAME}: {level}: {text}", file=sys.stderr)


def _shown_matcher(rule: Rule) -> str:
    m = rule.matcher
    if m.names is not None:
        return "|".join(sorted(m.names))
    return m.regex.pattern if m.regex is not None else "*"


__all__ = ["EVENTS", "FILE", "CommandHooks", "CommandHooksUnavailable"]
