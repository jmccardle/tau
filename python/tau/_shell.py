"""What permissions and `--answer` need to know about a shell command (D24 section 7).

A glob over a command is not a parser, so two rules keep a pattern from vouching for more than it
names. An **allowing** pattern (an `allow` rule, an `--answer` text) never matches a command that
contains a shell operator (`;` `&` `|` a backtick `$(` `<` `>` or a newline) unless the pattern
contains that operator too: such a command falls to `ask`. A **refusing or asking** pattern (a
`deny` or `ask` rule) is tried on the command whole and on each piece of it between the operators,
with a tab read as a space, leading `VAR=value` words, `then` `do` `else` `elif` `if` `while`
`until` `function NAME`, a backslash on the first word, and the wrappers in `WRAPPERS` (with their
flags) stripped (after `xargs` the command also gets a trailing space, as `xargs` adds arguments:
`xargs rm` is tried as `rm ` too, so `rm *` matches it), and the first word reduced to its
basename (`/bin/rm` is `rm`). This is best effort: quoting, `eval`, `bash -c "..."`, what
`find -exec` runs and a name built by expansion are not seen. `ask: [bash]` is the setting
that cannot be tricked.
"""

import fnmatch
import posixpath
import re

OPERATORS = (";", "&", "|", "`", "$(", "<", ">", "\n")
"""The shell operators that chain, substitute or redirect."""
WRAPPERS: dict[str, frozenset[str]] = {
    "command": frozenset(),
    "builtin": frozenset(),
    "exec": frozenset({"-a"}),
    "coproc": frozenset(),
    "nohup": frozenset(),
    "time": frozenset(),
    "setsid": frozenset(),
    "sudo": frozenset(
        {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"}
        | {"--user", "--group", "--host", "--prompt", "--close-from", "--chdir", "--chroot"}
        | {"--role", "--type", "--other-user", "--command-timeout"}
    ),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "strace": frozenset(
        {"-o", "-e", "-p", "-s", "-u", "-E", "-a", "-O", "-P", "-S", "-I"}
        | {"--output", "--trace", "--attach", "--string-limit", "--user", "--env"}
    ),
    "watch": frozenset({"-n", "--interval"}),
    "xargs": frozenset(
        {"-n", "-P", "-I", "-L", "-s", "-d", "-E", "-a"}
        | {"--max-args", "--max-procs", "--max-lines", "--max-chars", "--delimiter", "--eof"}
        | {"--arg-file"}
    ),
}
"""Words that run the command after them, stripped before a piece is matched, each with the flags
that take a value (the word after them is stripped too; `--flag=value` and `-nVALUE` have it
attached, and a cluster such as `sudo -nu root` ends in the flag that takes it)."""
_TAKES_DURATION = frozenset({"timeout"})
_KEYWORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until"})
_SPLIT = re.compile(r"[;&|<>\n`()]")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*\s+")
_FIRST = re.compile(r"(\S+)(.*)", re.DOTALL)


def operators_in(command: str) -> set[str]:
    """The operators `command` contains."""
    return {op for op in OPERATORS if op in command}


def allows(glob: str, command: str) -> bool:
    """Whether the allowing pattern `glob` matches `command`: it matches by `fnmatch`, and every
    operator the command contains is in the pattern too."""
    return fnmatch.fnmatchcase(command, glob) and all(op in glob for op in operators_in(command))


def _takes_value(wrapper: str, flag: str) -> bool:
    """Whether `flag` of `wrapper` is followed by its value as a separate word."""
    takes = WRAPPERS[wrapper]
    cluster = not flag.startswith("--") and len(flag) > 2  # noqa: PLR2004 - `-nu`: flags run together
    return flag in takes or (cluster and f"-{flag[-1]}" in takes)


def _after_flags(wrapper: str, text: str) -> str:
    """`text` (what follows `wrapper`) without its flags and the duration `timeout` takes."""
    words = text.split(None, 1)
    while words and words[0].startswith("-"):
        flag = words[0]
        words = (words[1] if len(words) > 1 else "").split(None, 1)
        if flag == "--":
            break
        if _takes_value(wrapper, flag) and words:
            words = (words[1] if len(words) > 1 else "").split(None, 1)
    if wrapper in _TAKES_DURATION and words:
        words = (words[1] if len(words) > 1 else "").split(None, 1)
    return words[0] + (" " + words[1] if len(words) > 1 else "") if words else ""


def _stripped(piece: str) -> tuple[str, bool]:
    """`piece` without what runs the command after it (the module docstring), and whether
    `xargs` was among it."""
    text = piece.lstrip(" {!")
    adds = False
    while True:
        assignment = _ASSIGNMENT.match(text)
        if assignment is not None:
            text = text[assignment.end() :]
            continue
        first = _FIRST.match(text)
        if first is None:
            return text, adds
        word = first[1]
        if word.startswith("\\") and len(word) > 1:
            text = text[1:]  # `\rm` is `rm`
        elif word in _KEYWORDS:
            text = first[2].lstrip(" {!")
        elif word == "function":  # `function NAME {` opens a body: its first command is next
            text = first[2].lstrip().partition(" ")[2].lstrip(" {!")
        elif word in WRAPPERS:
            adds = adds or word == "xargs"
            text = _after_flags(word, first[2].lstrip())
        else:
            return text, adds


def commands_of(command: str) -> list[str]:
    """What a refusing or asking pattern is tried on: `command` whole, then each piece of it
    (see the module docstring), each also with its first word reduced to its basename."""
    command = command.replace("\t", " ")
    found = [command]
    for piece in _SPLIT.split(command):
        text, adds = _stripped(piece)
        for shown in (text, text + " ") if adds and text else (text,):
            found.append(shown)
            first = _FIRST.match(shown)
            if first is not None:
                found.append(posixpath.basename(first[1]) + first[2])
    return found


def refuses(glob: str, command: str) -> bool:
    """Whether the refusing or asking pattern `glob` matches `command` or a piece of it."""
    return any(fnmatch.fnmatchcase(c, glob) for c in commands_of(command))
