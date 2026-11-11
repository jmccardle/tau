"""`Permissions`: the built-in extension that asks before a tool call it has a rule about (D24
section 7).

Three lists of rules, `deny`, `allow` and `ask`, checked in that order; a call no rule matches
runs. With no rules configured it asks nothing. A rule is `TOOL`, which matches every call of the
tool, or `TOOL:GLOB` for the path tools: the glob (`fnmatch`'s, case-sensitive; `*` crosses `/`
and newlines) matches the path argument of `read`, `write`, `edit`, `grep` or `find` (for `grep`,
every one of the `files` too, and the working directory when it names neither). Any other text is a
tool's whole name (a name may hold a colon).

- **Paths** are matched as the model wrote them and as the path they name: the argument joined to
  the session's working directory and made real (`..`, `//`, `.` and symlinks resolved). A call may
  name several (`grep`'s `path` and `files`): `deny` and `ask` fire if any of them matches, in
  either spelling; `allow` needs every one to match, as resolved.
- **A rule's directories** are read two ways for `deny` and `ask`: as written (by text:
  `/x/../etc` is `/etc`) and made real, so a rule about a directory that is a link still meets the
  paths that land in it (on macOS `/etc` and `/tmp` are links into `/private`: `write:/etc/*`
  refuses a write to `/etc/x`, which lands on `/private/etc/x`). An `allow` is read only as
  written, so a link can never widen it: on macOS an allow for `/tmp` is written `/private/tmp/*`.
- **On macOS** `deny` and `ask` ignore case in a path (`FOLDS`); `allow` does not.
- **On Windows** a rule may not have a path glob (`read:GLOB` and the other path tools): how a glob
  meets a Windows path (drive letters, `\\`, names that differ only in case) is not decided
  (docs/plan.md, Phase 9), so such a rule is refused where it is configured. A bare `TOOL` works.
- **Searches** (`grep`, `find`) read everything under their root. A search whose root is inside a
  denied pattern is refused. Any other runs with `exclude: [PATTERN]` added to its arguments
  for each deny rule about it, and the tool leaves out the matching files (also through a
  symlink) and says how many. A `deny` for `read:GLOB` guards `grep` too.
- **Commands** are matched by the tool's name only: a pattern over a command's text is easy to
  step around (a shell says one thing many ways), so it would promise a guard it is not. `deny`
  and `ask` take `bash` alone. `allow` also takes `bash:COMMAND`, one exact command (what `always`
  saves; `*`, `?` and `[` are written `[*]`, `[?]`, `[[]`), which matches that text and no other.
  A finer guard belongs below τ (a separate user, a container), or in an extension of one's own.

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
import functools
import json
import os
import re
import sys
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

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
"""The tools a rule may give an argument for (`bash`: one exact command, in `allow` only)."""
REFUSED = "the user refused this call"
FOLDS = sys.platform == "darwin"
"""Whether `deny` and `ask` ignore case in a path: a macOS volume folds case unless it was made
case-sensitive, so `/Users/me/.SSH/key` is `/Users/me/.ssh/key` there, and a rule that missed it
would be no guard (an `allow` never folds: a fold can only widen it)."""
Answer = Literal["yes", "always", "no"]
Mode = Literal["deny", "allow", "ask"]
MODES: tuple[Mode, ...] = ("deny", "allow", "ask")
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


def _unsaved(call: Call) -> str:
    """Why `always` saved no rule for `call` (`Rule.exact` gave none)."""
    if call.resolved and sys.platform == "win32":
        return "path patterns are not available on Windows yet"
    return f"this {call.tool} call's arguments are malformed"


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
    def parse(cls, text: str, mode: Mode = "allow") -> "Rule":
        """The rule `text` says in the list `mode`; `ValueError` if it is not one there."""
        tool, colon, glob = text.partition(":")
        if not text:
            raise ValueError("an empty rule names no tool: write TOOL or TOOL:GLOB")
        if not colon or tool not in PATTERN_TOOLS:
            return cls(text)  # a tool's whole name, which may hold a colon
        if not glob:
            raise ValueError(f"rule {text!r} has an empty pattern after the colon")
        if tool == BASH:
            _check_command(text, glob, mode)
        else:
            try:
                _compiled(glob)
            except re.error as e:
                raise ValueError(f"rule {text!r} has an invalid pattern: {e}") from e
            if sys.platform == "win32":
                raise ValueError(
                    f"rule {text!r}: path patterns are not available on Windows yet (how a "
                    "pattern meets a Windows path is not decided); write the bare tool name, "
                    f"{tool!r}"
                )
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
        if call.tool != BASH and sys.platform == "win32":
            return []  # no path patterns there (`parse`)
        return [cls(call.tool, _GLOB_CHARS.sub(r"[\1]", str(s))) for s in dict.fromkeys(subjects)]

    def __str__(self) -> str:
        return self.tool if self.glob is None else f"{self.tool}:{self.glob}"

    def matches(self, call: Call, mode: Mode) -> bool:
        if call.tool != self.tool and not self.guards(call, mode):
            return False
        if self.glob is None:
            return True
        if call.tool == BASH:
            return call.subject is not None and call.subject == literal(self.glob)
        absolute = _absolute(self.glob, call.cwd)
        if mode == "allow":
            return bool(call.resolved) and all(_fits(r, absolute) for r in call.resolved)
        return _refuses(call, mode, (self.glob, absolute, _real(absolute)))

    def guards(self, call: Call, mode: Mode) -> bool:
        """Whether a `read` deny with a glob also covers this `grep`: grep reads the files."""
        return (
            mode == "deny" and self.tool == "read" and self.glob is not None and call.tool == "grep"
        )

    def hides_from(self, call: Call) -> list[str]:
        """For a deny rule of a search `call`: the patterns (over real absolute paths) for the
        files the search must leave out, the rule's directories as written and made real; none if
        the rule is not about this search. Any search may need them, even one whose root is
        elsewhere: a link under the root can lead to a denied file. A search whose root is, or is
        inside, a denied path was already refused."""
        if self.glob is None or call.tool not in SEARCH_TOOLS:
            return []
        if call.tool != self.tool and not self.guards(call, "deny"):
            return []
        absolute = _absolute(self.glob, call.cwd)
        patterns = [glob_regex(g) for g in dict.fromkeys((absolute, _real(absolute)))]
        return [f"(?i:{p})" for p in patterns] if FOLDS else patterns


def _refuses(call: Call, mode: Mode, forms: tuple[str, ...]) -> bool:
    """Whether a `deny` or `ask` rule whose glob reads as any of `forms` matches `call`: a path
    in either spelling fits one, or, for an `ask`, a search's root could hold what one matches."""
    seen = (*call.written, *call.resolved)
    if any(_fits(path, g, FOLDS) for path in seen for g in forms):
        return True
    return (
        mode == "ask" and call.tool in SEARCH_TOOLS and any(_searches_under(call, g) for g in forms)
    )


_CLASS_ESCAPES = frozenset("\\[]^&~-")
_LITERAL_ESCAPES = frozenset("\\.+*?()|[]{}^$#&-~")


def glob_regex(glob: str) -> str:
    """The regular expression a rule's glob means, for Python's `re` and Rust's `regex` alike
    (the `exclude` a search is given is these): `*` is any run of characters, `?` one, `[abc]` and
    `[!abc]` a set, an unclosed `[` itself, and everything else, `{`, `^` and `\\` included, is
    literal. It is `fnmatch`'s grammar, written once so that the deny check and the tool agree."""
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        i += 1
        if c == "*":
            if not out or out[-1] != ".*":
                out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "[":
            made, i = _char_set(glob, i)
            out.append(made)
        else:
            out.append("\\" + c if c in _LITERAL_ESCAPES else c)
    return "".join(out)


def _char_set(glob: str, i: int) -> tuple[str, int]:
    """The regular expression for the set whose `[` is just before `glob[i]`, and where the
    glob goes on; an unclosed `[` is the character itself."""
    n = len(glob)
    j = i + 1 if glob[i : i + 1] == "!" else i
    j = j + 1 if glob[j : j + 1] == "]" else j
    while j < n and glob[j] != "]":
        j += 1
    if j >= n:
        return "\\[", i
    members = glob[i:j]
    negated = members.startswith("!")
    members = members[1:] if negated else members
    return f"[{'^' if negated else ''}{_set_members(members)}]", j + 1


def _set_members(members: str) -> str:
    """The inside of a set: `x-y` is a range, and every other character is escaped if the regex
    would read it as syntax, so `--`, `&&` and `~~` are never class operators."""
    out: list[str] = []
    k = 0
    while k < len(members):
        low = members[k]
        text = "\\" + low if low in _CLASS_ESCAPES else low
        if members[k + 1 : k + 2] == "-" and k + 2 < len(members):
            high = members[k + 2]
            text += "-" + ("\\" + high if high in _CLASS_ESCAPES else high)
            k += 2
        out.append(text)
        k += 1
    return "".join(out)


@functools.cache
def _compiled(glob: str, fold: bool = False) -> re.Pattern[str]:
    """`glob` as a pattern, ignoring case if `fold`; `re.error` if it is not one (a backwards
    range, `[z-a]`)."""
    return re.compile(f"(?s:{glob_regex(glob)})", re.IGNORECASE if fold else 0)


def _fits(path: str, glob: str, fold: bool = False) -> bool:
    return _compiled(glob, fold).fullmatch(path) is not None


def _absolute(glob: str, cwd: str) -> str:
    """`glob` as a rule over real absolute paths: one that starts with a literal relative
    component (`src/*`, `./src/*`, `../x/*`) starts in `cwd`, as the tool's own paths do. A glob
    that starts with a wildcard is left as it is. The directories before the first wildcard are
    normalised by text (`/tmp/../etc/*` is `/etc/*`), not through symlinks: if `/tmp/x` is a link,
    `/tmp/x/../etc/*` is `/tmp/etc/*`, though the OS would walk from the link's target."""
    if glob[0] in "*?[":
        return glob
    full = glob if glob[0] == "/" else os.path.join(cwd, glob)
    cut = _first_wildcard(full)
    head, tail = full[:cut], full[cut:]
    if not tail:
        return os.path.normpath(full)
    directory = head[: head.rfind("/") + 1]
    name = head[len(directory) :]
    return os.path.normpath(directory).rstrip("/") + "/" + name + tail


def _real(glob: str) -> str:
    """An absolute `glob` with the directories before its first wildcard made real (symlinks
    followed, as the paths it is matched against are), so `/etc/*` is `/private/etc/*` on macOS. A
    glob that starts with a wildcard is left as it is."""
    if glob[0] in "*?[":
        return glob
    cut = _first_wildcard(glob)
    head, tail = glob[:cut], glob[cut:]
    if not tail:
        return os.path.realpath(glob)
    directory = head[: head.rfind("/") + 1]
    return os.path.realpath(directory).rstrip("/") + "/" + head[len(directory) :] + tail


def _first_wildcard(glob: str) -> int:
    found = _GLOB_CHARS.search(glob)
    return len(glob) if found is None else found.start()


def _searches_under(call: Call, glob: str) -> bool:
    """Whether a search's root could hold a path `glob` matches: the root is inside the glob's
    literal start (the root `/etc` for `/etc/*`, or a link that lands there) or holds it (the root
    `/` for `/etc/*`). A glob that starts with a wildcard has no literal start to compare."""
    literal = _GLOB_CHARS.split(glob, maxsplit=1)[0]
    if not literal:
        return False
    literal = literal.casefold() if FOLDS else literal
    for root in (
        *call.resolved,
        *(os.path.normpath(os.path.join(call.cwd, w)) for w in call.written),
    ):
        inside = root.rstrip("/") + "/"
        inside = inside.casefold() if FOLDS else inside
        if inside.startswith(literal) or literal.startswith(inside):
            return True
    return False


def literal(glob: str) -> str | None:
    """The one text `glob` matches, or `None` if it has a wildcard: `*`, `?`, or a `[...]` set
    other than one character (`[*]` is a literal star, as `Rule.exact` writes it)."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if c == "[" and i + 2 < len(glob) and glob[i + 2] == "]" and glob[i + 1] != "!":
            out.append(glob[i + 1])
            i += 3
            continue
        if c in "*?[":
            return None
        out.append(c)
        i += 1
    return "".join(out)


def _check_command(text: str, command: str, mode: Mode) -> None:
    """`ValueError` unless `bash:COMMAND` is allowed in the list `mode`: one exact command, in
    `allow`."""
    if mode != "allow":
        raise ValueError(
            f"rule {text!r} in {mode}: there a bash rule is the bare name 'bash' (a pattern over "
            "a command's text is easy to step around); for a finer guard, run tau as a user, or "
            "in a container, that cannot do what you would refuse"
        )
    if literal(command) is None:
        raise ValueError(
            f"allow rule {text!r}: an allow rule for bash names one exact command, without "
            "wildcards (write * ? [ as [*] [?] [[]), or is the bare name 'bash'"
        )


def check_answer(text: str) -> None:
    """`ValueError` unless the `--answer` TEXT for a `permissions` question is one it can mean: a
    `bash` TEXT is `bash` alone or one exact command, as an `allow` rule is."""
    tool, colon, command = text.partition(":")
    if tool == BASH and colon and literal(command.removeprefix(" ")) is None:
        raise ValueError(
            f"--answer text {text!r}: a bash text is 'bash' alone (every command) or one "
            "exact command, without wildcards (write * ? [ as [*] [?] [[])"
        )


def answers(glob: str, text: str) -> bool:
    """Whether an `--answer` glob answers the permissions question `text`: the glob is over the
    call alone (the `(ask rule: ...)` line is left out), written `TOOL: ARGUMENT` as the question
    shows it or in rule syntax, `TOOL:ARGUMENT`. A `bash` glob is `bash` alone, which answers
    every command, or one exact command (`check_answer`). A path tool's question has a line
    `TOOL: PATH` for each resolved path, and the glob must match every one (a path holding a
    newline only makes more lines). Any other tool's call is matched whole: its JSON arguments
    are no shell command."""
    call, _, _ = text.rpartition("\n(ask rule: ")
    call = call or text
    tool = call[: call.find(": ")] if ": " in call else call
    if glob.startswith(f"{tool}:") and not glob.startswith(f"{tool}: "):
        glob = f"{tool}: {glob[len(tool) + 1 :]}"
    if tool == BASH:
        if glob == BASH:
            return True
        command = literal(glob.removeprefix(f"{BASH}: "))
        return glob.startswith(f"{BASH}: ") and command == call.removeprefix(f"{BASH}: ")
    if tool in PATH_TOOLS:
        return all(fnmatch.fnmatchcase(line, glob) for line in call.split("\n"))
    return fnmatch.fnmatchcase(call, glob)


def _leave_out(call: CallDraft, patterns: list[str]) -> str | None:
    """Give a search the `exclude` patterns it must honor, beside any it has (a model's own only
    narrows). `None` is no `exclude` set (`null` is none). A call whose arguments cannot take
    them is refused, with the reason: a deny that could not be applied must not let it run."""
    if not patterns:
        return None
    args = _arguments(call.args_raw)
    given = args.get("exclude")
    if not args or not (given is None or isinstance(given, list)):
        return (
            "permissions refused this call: its arguments are not an object with a list "
            "`exclude` (or none), so the deny rules cannot be applied to the search"
        )
    args["exclude"] = [*cast("list[object]", given or []), *patterns]
    call.args_raw = json.dumps(args)
    return None


def _builtin_tools() -> set[str]:
    """The names of the tools the built-in extensions provide, loaded or not, and on every
    platform: `bash` is not on Windows, but a rule for it is no misspelling (one config file may
    serve several machines)."""
    listed, _ = assemble_tools((), [BaseTools(), Agents()])
    return {BASH, *(cast("str", getattr(t, "name")) for t in listed)}  # noqa: B009


def _rules(texts: Iterable[str], mode: Mode) -> list[Rule]:
    return [Rule.parse(t, mode) for t in texts]


@dataclass(frozen=True)
class Options:
    """`Permissions`' options (`extensions.permissions`; `/config permissions`). A rule is `TOOL`
    or `TOOL:GLOB`; with all three empty nothing is asked."""

    # A list option's default is a tuple (a dataclass refuses a list); the value is a list.
    ask: Annotated[list[str], "rules that hold a call for the user's answer"] = ()  # pyright: ignore[reportAssignmentType]
    allow: Annotated[list[str], "rules that run a call without asking (`always` adds here)"] = ()  # pyright: ignore[reportAssignmentType]
    deny: Annotated[list[str], "rules that refuse a call without asking"] = ()  # pyright: ignore[reportAssignmentType]

    def __post_init__(self) -> None:
        for name in MODES:
            _rules(getattr(self, name), name)

    def missing_tools(self, tools: Sequence[str]) -> dict[Mode, list[str]]:
        """The rule lists that name a tool no extension provides, each with those rules (a
        misspelled name would match nothing, and a `deny` that matches nothing is no guard). A
        tool of a built-in extension that is not loaded (`agent`, with `agents` off) is known: a
        rule for it names nothing missing."""
        known = {*tools, *_builtin_tools()}
        found: dict[Mode, list[str]] = {}
        for name in MODES:
            texts: list[str] = list(getattr(self, name))
            bad = [
                t
                for t, rule in zip(texts, _rules(texts, name), strict=True)
                if rule.tool not in known
            ]
            if bad:
                found[name] = bad
        return found

    def tool_problems(self, tools: Sequence[str], only: Collection[str] | None = None) -> list[str]:
        """`missing_tools` as sentences, for the fields in `only` (default: all)."""
        return [
            f"{name} rule {str(rule)!r} names the tool {rule.tool!r}, which no loaded extension "
            f"provides; the tools are {sorted(tools)}"
            for name, texts in self.missing_tools(tools).items()
            if only is None or name in only
            for rule in _rules(texts, name)
        ]

    def exclusions(self, call: Call) -> list[str]:
        """The patterns of the files a `grep` or `find` must leave out: those its `deny` rules
        name that its root holds."""
        return list(dict.fromkeys(p for r in _rules(self.deny, "deny") for p in r.hides_from(call)))

    def first(self, mode: Mode, call: Call, extra: Iterable[str] = ()) -> Rule | None:
        """The first rule of the list `mode` (and `extra`, for `allow`) that matches `call`."""
        texts = [*getattr(self, mode), *extra]
        return next((r for r in _rules(texts, mode) if r.matches(call, mode)), None)


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
        unfiltered = _leave_out(call, options.exclusions(seen))
        if unfiltered is not None:
            return unfiltered
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
                node.tell(f"permissions: {_unsaved(seen)}, so no rule was saved; it ran this once")
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
