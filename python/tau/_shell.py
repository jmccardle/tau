"""What permissions and `--answer` need to know about a shell command (D24 section 7).

A glob over a command is not a parser, so two rules keep a pattern from vouching for more than it
names. An **allowing** pattern (an `allow` rule, an `--answer` text) never matches a command that
contains a shell operator (`;` `&` `|` a backtick `$(` `<` `>` or a newline) unless the pattern
contains that operator too: such a command falls to `ask`. A **refusing or asking** pattern (a
`deny` or `ask` rule) is tried on the command whole and on each piece of it between the operators,
with a tab read as a space, leading `VAR=value` words, `then` `do` `else` `elif`, a backslash on the
first word, and the wrappers in `WRAPPERS` (with their flags) stripped, and the first word
reduced to its basename (`/bin/rm` is `rm`). This is best effort: quoting, `eval`, `bash -c "..."`
and a name built by expansion are not seen. `ask: [bash]` is the setting that cannot be tricked.
"""

import fnmatch
import posixpath
import re

OPERATORS = (";", "&", "|", "`", "$(", "<", ">", "\n")
"""The shell operators that chain, substitute or redirect."""
WRAPPERS: dict[str, frozenset[str]] = {
    "command": frozenset(),
    "builtin": frozenset(),
    "exec": frozenset(),
    "nohup": frozenset(),
    "time": frozenset(),
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k"}),
    "xargs": frozenset({"-n", "-P", "-I", "-L", "-s", "-d", "-E", "-a"}),
}
"""Words that run the command after them, stripped before a piece is matched, each with the flags
that take a value (the word after them is stripped too)."""
_TAKES_DURATION = frozenset({"timeout"})
_KEYWORDS = frozenset({"then", "do", "else", "elif"})
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


def _after_flags(wrapper: str, text: str) -> str:
    """`text` (what follows `wrapper`) without its flags and the duration `timeout` takes."""
    words = text.split(None, 1)
    while words and words[0].startswith("-"):
        flag = words[0]
        words = (words[1] if len(words) > 1 else "").split(None, 1)
        if flag == "--":
            break
        if flag in WRAPPERS[wrapper] and words:
            words = (words[1] if len(words) > 1 else "").split(None, 1)
    if wrapper in _TAKES_DURATION and words:
        words = (words[1] if len(words) > 1 else "").split(None, 1)
    return words[0] + (" " + words[1] if len(words) > 1 else "") if words else ""


def _stripped(piece: str) -> str:
    text = piece.lstrip(" {!")
    while True:
        assignment = _ASSIGNMENT.match(text)
        if assignment is not None:
            text = text[assignment.end() :]
            continue
        first = _FIRST.match(text)
        if first is None:
            return text
        word = first[1]
        if word.startswith("\\") and len(word) > 1:
            text = text[1:]  # `\rm` is `rm`
        elif word in _KEYWORDS:
            text = first[2].lstrip(" {!")
        elif word in WRAPPERS:
            text = _after_flags(word, first[2].lstrip())
        else:
            return text


def commands_of(command: str) -> list[str]:
    """What a refusing or asking pattern is tried on: `command` whole, then each piece of it
    (see the module docstring), each also with its first word reduced to its basename."""
    command = command.replace("\t", " ")
    found = [command]
    for piece in _SPLIT.split(command):
        text = _stripped(piece)
        found.append(text)
        first = _FIRST.match(text)
        if first is not None:
            found.append(posixpath.basename(first[1]) + first[2])
    return found


def refuses(glob: str, command: str) -> bool:
    """Whether the refusing or asking pattern `glob` matches `command` or a piece of it."""
    return any(fnmatch.fnmatchcase(c, glob) for c in commands_of(command))
