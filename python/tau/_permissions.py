"""`Permissions`: the built-in extension that asks before a tool call it has a rule about (D24
section 7).

Three lists of rules, `deny`, `allow` and `ask`, checked in that order; a call no rule matches
runs. With no rules configured it asks nothing. A rule is `TOOL`, which matches every call of the
tool, or `TOOL:GLOB` for `bash` and the path tools: the glob (`fnmatch`'s, case-sensitive; `*`
crosses `/` and newlines) matches a `bash` command, or the path argument of `read`, `write`,
`edit`, `grep` or `find` (for `grep`, every one of the `files` too, and the working directory when
it names neither). Any other text is a tool's whole name (a name may hold a colon).

- **Paths** are matched as the model wrote them and as the path they name: the argument joined to
  the session's working directory and made real (`..`, `//`, `.` and symlinks resolved). A call may
  name several (`grep`'s `path` and `files`): `deny` and `ask` fire if any of them matches, in
  either spelling; `allow` needs every one to match, as resolved.
- **Commands** follow `tau._shell`: an `allow` glob never vouches for a command that chains or
  redirects (it falls to `ask`) unless the glob has the operator too, and `deny` and `ask` are tried
  on each piece of a chained command. A `bash` deny is best effort; `ask: [bash]` is the safe
  setting.

An `ask` rule holds the call with a question, `yes | always | no`. `yes` runs the call. `always`
runs it and appends the exact rule for it (the resolved path, the whole command) to the user's
file's `allow` (a `bash` or path-tool call with malformed arguments has no exact rule: nothing is
saved, and the user is told), and allows it for the rest of the run even if a higher layer hides
that list. `no`,
a decline, an abort or a go-to refuse it: the model reads `the user refused this call`. A `deny`
rule refuses a call without asking, and says which rule.

This is a guard against the model, not a sandbox: a later extension's `pre_tool` can change the
outcome (model.md section 7).
"""

import fnmatch
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

from tau import _shell
from tau._agents import Agents
from tau._base_tools import BaseTools
from tau._handles import NodeHandle
from tau._protocols import CallDraft
from tau._tools import assemble_tools

BASH = "bash"
PATH_TOOLS = ("read", "write", "edit", "grep", "find")
SEARCH_TOOLS = ("grep", "find")
"""The path tools that read every file under their root."""
PATTERN_TOOLS = (BASH, *PATH_TOOLS)
"""The tools a rule may give a glob for."""
REFUSED = "the user refused this call"
Answer = Literal["yes", "always", "no"]
Mode = Literal["deny", "allow", "ask"]
_GLOB_CHARS = re.compile(r"([*?\[])")


@dataclass(frozen=True)
class Call:
    """The part of a tool call the rules look at: its `tool`, and what a glob matches. For `bash`
    that is the `command` (`subject`). For a path tool it is the paths the call names: `written`
    as the model gave them and `resolved` as the real absolute paths they land on (same length;
    `written` has the working directory, `.`, for a `grep` or `find` that names none). A call that
    gives no such argument, or not as strings, has none, and no pattern rule matches it."""

    tool: str
    subject: str | None
    written: tuple[str, ...]
    resolved: tuple[str, ...]
    args_raw: str
    cwd: str = "/"
    """The session's working directory, made real: where a relative rule starts."""

    @classmethod
    def of(cls, draft: CallDraft, cwd: Path) -> "Call":
        args = _arguments(draft.args_raw)
        subject: str | None = None
        written: tuple[str, ...] = ()
        if draft.tool == BASH:
            command = args.get("command")
            subject = command if isinstance(command, str) else None
        elif draft.tool in PATH_TOOLS:
            written = _paths(draft.tool, args)
        resolved = tuple(os.path.realpath(os.path.join(cwd, w)) for w in written)
        return cls(draft.tool, subject, written, resolved, draft.args_raw, os.path.realpath(cwd))

    def shown(self) -> str:
        """The question's text: the command, a line per resolved path, or the raw arguments."""
        if self.tool in PATH_TOOLS and self.resolved:
            return "\n".join(f"{self.tool}: {r}" for r in self.resolved)
        return f"{self.tool}: {self.args_raw if self.subject is None else self.subject}"

    def as_written(self) -> str:
        """What the model wrote where it differs from the path it names, for the question."""
        return ", ".join(w for w, r in zip(self.written, self.resolved, strict=True) if w != r)


def _arguments(args_raw: str) -> dict[str, object]:
    try:
        args = json.loads(args_raw)
    except ValueError:
        return {}  # the tool owns argument checking
    return cast("dict[str, object]", args) if isinstance(args, dict) else {}


def _paths(tool: str, args: dict[str, object]) -> tuple[str, ...]:
    """The paths a path tool's arguments name; none if they are not strings. `grep` searches
    `files` instead of `path` when it has them; both are looked at. Neither: the working
    directory."""
    named: list[object] = []
    if "path" in args:
        named.append(args["path"])
    files = args.get("files") if tool == "grep" else None
    if files is not None:
        if not isinstance(files, list):
            return ()
        named.extend(cast("list[object]", files))
    if not named and tool in ("grep", "find"):
        named.append(".")  # the working directory
    if not all(isinstance(n, str) for n in named):
        return ()
    return tuple(cast("list[str]", named))


@dataclass(frozen=True)
class Rule:
    """`TOOL` or `TOOL:GLOB`, as written in the options."""

    tool: str
    glob: str | None = None

    @classmethod
    def parse(cls, text: str) -> "Rule":
        """The rule `text` says; `ValueError` if it is not one."""
        tool, colon, glob = text.partition(":")
        if not text:
            raise ValueError("an empty rule names no tool: write TOOL or TOOL:GLOB")
        if not colon or tool not in PATTERN_TOOLS:
            return cls(text)  # a tool's whole name, which may hold a colon
        if not glob:
            raise ValueError(f"rule {text!r} has an empty pattern after the colon")
        return cls(tool, glob)

    @classmethod
    def exact(cls, call: Call) -> list["Rule"]:
        """The rules that match this call and nothing else a glob can tell apart from it: one for
        the whole command, or one for each resolved path. A tool without patterns gets its bare
        name. A `bash` or path-tool call whose arguments name nothing (they are malformed) gets
        none: the bare name would allow every call of the tool."""
        subjects = [call.subject] if call.tool == BASH else list(call.resolved)
        if not subjects or subjects == [None]:
            return [] if call.tool in PATTERN_TOOLS else [cls(call.tool)]
        return [cls(call.tool, _GLOB_CHARS.sub(r"[\1]", str(s))) for s in dict.fromkeys(subjects)]

    def __str__(self) -> str:
        return self.tool if self.glob is None else f"{self.tool}:{self.glob}"

    def matches(self, call: Call, mode: Mode) -> bool:
        if call.tool != self.tool:
            return False
        if self.glob is None:
            return True
        if call.tool == BASH:
            match = _shell.allows if mode == "allow" else _shell.refuses
            return call.subject is not None and match(self.glob, call.subject)
        absolute = _absolute(self.glob, call.cwd)
        if mode == "allow":
            return bool(call.resolved) and all(
                fnmatch.fnmatchcase(r, absolute) for r in call.resolved
            )
        seen = (*call.written, *call.resolved)
        if any(fnmatch.fnmatchcase(path, g) for path in seen for g in {self.glob, absolute}):
            return True
        return call.tool in SEARCH_TOOLS and _searches_under(call, absolute)


def _absolute(glob: str, cwd: str) -> str:
    """`glob` as a rule over real absolute paths: one that starts with a literal relative
    component (`src/*`, `./src/*`, `../x/*`) starts in `cwd`, as the tool's own paths do. A glob
    that starts with `/` or a wildcard is left as it is."""
    if glob[0] in "/*?[":
        return glob
    return os.path.normpath(os.path.join(cwd, glob))


def _searches_under(call: Call, glob: str) -> bool:
    """Whether a search's root could hold a path `glob` matches: the root is inside the glob's
    literal start (the root `/etc` for `/etc/*`, or a link that lands there) or holds it (the root
    `/` for `/etc/*`). A glob that starts with a wildcard has no literal start to compare."""
    literal = _GLOB_CHARS.split(glob, maxsplit=1)[0]
    if not literal:
        return False
    for root in (
        *call.resolved,
        *(os.path.normpath(os.path.join(call.cwd, w)) for w in call.written),
    ):
        inside = root.rstrip("/") + "/"
        if inside.startswith(literal) or literal.startswith(inside):
            return True
    return False


def answers(glob: str, text: str) -> bool:
    """Whether an `--answer` glob answers the permissions question `text`: the glob is over the
    call alone (the `(ask rule: ...)` line is left out), written `TOOL: ARGUMENT` as the question
    shows it or in rule syntax, `TOOL:ARGUMENT`. A command is matched as `tau._shell` matches an
    allowing pattern (a chain needs the operator in the glob). A path tool's question has a line
    `TOOL: PATH` for each resolved path, and the glob must match every one (a path holding a
    newline only makes more lines). Any other tool's call is matched whole: its JSON arguments
    are no shell command."""
    call, _, _ = text.rpartition("\n(ask rule: ")
    call = call or text
    tool = call[: call.find(": ")] if ": " in call else call
    if glob.startswith(f"{tool}:") and not glob.startswith(f"{tool}: "):
        glob = f"{tool}: {glob[len(tool) + 1 :]}"
    if tool == BASH:
        return _shell.allows(glob, call)
    if tool in PATH_TOOLS:
        return all(fnmatch.fnmatchcase(line, glob) for line in call.split("\n"))
    return fnmatch.fnmatchcase(call, glob)


def _builtin_tools() -> set[str]:
    """The names of the tools the built-in extensions provide, loaded or not."""
    listed, _ = assemble_tools((), [BaseTools(), Agents()])
    return {cast("str", getattr(t, "name")) for t in listed}  # noqa: B009


def _rules(texts: Iterable[str]) -> list[Rule]:
    return [Rule.parse(t) for t in texts]


@dataclass(frozen=True)
class Options:
    """`Permissions`' options (`extensions.permissions`; `/config permissions`). A rule is `TOOL`
    or `TOOL:GLOB`; with all three empty nothing is asked."""

    # A list option's default is a tuple (a dataclass refuses a list); the value is a list.
    ask: Annotated[list[str], "rules that hold a call for the user's answer"] = ()  # pyright: ignore[reportAssignmentType]
    allow: Annotated[list[str], "rules that run a call without asking (`always` adds here)"] = ()  # pyright: ignore[reportAssignmentType]
    deny: Annotated[list[str], "rules that refuse a call without asking"] = ()  # pyright: ignore[reportAssignmentType]

    def __post_init__(self) -> None:
        for name in ("deny", "allow", "ask"):
            _rules(getattr(self, name))

    def tool_problems(self, tools: Sequence[str]) -> list[str]:
        """The rules that name a tool no extension provides (a misspelled name would match
        nothing, and a `deny` that matches nothing is no guard). A tool of a built-in extension
        that is not loaded (`agent`, with `agents` off) is known: a rule for it is no error."""
        known = {*tools, *_builtin_tools()}
        found: list[str] = []
        for name in ("deny", "allow", "ask"):
            for rule in _rules(getattr(self, name)):
                if rule.tool not in known:
                    found.append(
                        f"{name} rule {str(rule)!r} names the tool {rule.tool!r}, which no loaded "
                        f"extension provides; the tools are {sorted(tools)}"
                    )
        return found

    def first(self, mode: Mode, call: Call, extra: Iterable[str] = ()) -> Rule | None:
        """The first rule of the list `mode` (and `extra`, for `allow`) that matches `call`."""
        texts = [*getattr(self, mode), *extra]
        return next((r for r in _rules(texts) if r.matches(call, mode)), None)


class Permissions:
    """Asks before, or refuses, a tool call a rule names (the module docstring). `cwd` is the
    session's working directory, which a relative path is resolved against."""

    name = "permissions"
    options = Options

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd
        self._approved: set[str] = set()  # rules `always` answered this run

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        """Refuse a denied call, let an allowed one go, and hold an asked one for an answer."""
        options = node.options()
        seen = Call.of(call, self._cwd)
        denied = options.first("deny", seen)
        if denied is not None:
            return f"permissions refused this call: it matches the deny rule {str(denied)!r}"
        if options.first("allow", seen, self._approved) is not None:
            return None
        asked = options.first("ask", seen)
        if asked is None:
            return None
        written = seen.as_written()
        line = f"(ask rule: {asked}" + (f"; as written: {written})" if written else ")")
        answer: Answer | None = await node.ask(f"{seen.shown()}\n{line}", Answer)
        if answer == "always":
            exact = Rule.exact(seen)
            if not exact:
                node.tell(
                    f"permissions: this {seen.tool} call's arguments are malformed, so no rule "
                    "was saved; it ran this once"
                )
            for rule in exact:
                self._always(node, str(rule))
        return None if answer in ("yes", "always") else REFUSED

    def _always(self, node: NodeHandle, rule: str) -> None:
        """Write `rule` to the user's file's own `allow` (read now: siblings save too, not the
        layers' and nodes' fold), allow it for the run, and say where it went."""
        saved = cast("list[str]", node.saved_options().get("allow", []))
        if rule not in saved:
            node.save_options(allow=[*saved, rule])
        self._approved.add(rule)
        said = (
            f"permissions: {rule!r} is allowed from now on (written to allow in your config file)"
        )
        if rule not in node.options().allow:
            said = (
                f"permissions: {rule!r} is allowed for this run and written to your config file, "
                "but a project layer, --set or a Config node sets permissions.allow and hides it"
            )
        node.tell(said)
