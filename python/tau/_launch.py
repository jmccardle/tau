"""The launch command line (D22 section 1): `tau [-p] [flags] [MESSAGE...]`.

The first argument decides which parser reads the line: the name of a subcommand (`config`, `ext`,
`sessions`, `export`, `context`, `serve`, `trust`) runs that subcommand, and anything else,
including nothing, is the launch command line. So `tau -p sessions` sends the word "sessions".

The flag builders the programs share live here too (`extension_flags`, `option_flags`,
`session_flags`, ...; `tau.cli` exports them). `launch_parser` builds the launch parser from the
flag parsers the program already has (the session, extension and `--set` flags). `answer_rule`
reads an `--answer` argument, and `message_of` joins the words and standard input into the
message `-p` sends.
"""

import argparse
import fnmatch
import select
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO

from tau import _permissions

PERMISSIONS = "permissions"
"""The extension whose questions an `--answer` text is matched as a call (`tau._permissions`)."""


class UsageError(Exception):
    """A usage error found after the line parsed: nothing to send, a command line missing its
    arguments, `-r` with `-p`. `tau` prints it and exits 2."""


@dataclass(frozen=True)
class AnswerRule:
    """One `--answer EXTENSION[:TEXT]=VALUE` (D22 section 5): the asking `extension` (or `*`), a
    glob over the first line of the question's `text` and the `value` to answer with, read by the
    question's schema. For `permissions` the glob is over the whole call (its `(ask rule: ...)` line
    left out), and a call that chains commands is matched only by a glob that holds the operator.
    `spec` is the argument as typed."""

    extension: str
    text: str
    value: str
    spec: str

    def matches(self, extension: str, text: str) -> bool:
        """Whether the question `text` asked by `extension` is one this rule answers."""
        if self.extension not in ("*", extension):
            return False
        if extension == PERMISSIONS and self.text != "*":
            # The call alone (not the line naming the rule that matched), the resolved path.
            return _permissions.answers(self.text, text)
        return fnmatch.fnmatchcase(text.split("\n", 1)[0], self.text)


def answer_rule(spec: str) -> AnswerRule:
    """`EXTENSION[:TEXT]=VALUE` as a rule: the value is after the last `=`, then the extension
    is before the first `:` of the rest. `argparse.ArgumentTypeError` if either is missing."""
    head, equals, value = spec.rpartition("=")
    extension, colon, text = head.partition(":")
    if not equals or not extension or (colon and not text):
        raise argparse.ArgumentTypeError(f"{spec!r} is not EXTENSION[:TEXT]=VALUE")
    return AnswerRule(extension, text if colon else "*", value, spec)


def verbose_flag() -> argparse.ArgumentParser:
    """`--verbose` (`add_help=False`, for `parents=`), which `tau -p` and `tau serve` share."""
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "--verbose",
        action="store_true",
        help="on standard error: report the start, and under -p what the run does",
    )
    return flags


def launch_parser(
    parents: Sequence[argparse.ArgumentParser], commands: Sequence[str]
) -> argparse.ArgumentParser:
    """The parser of `tau [-p] [flags] [MESSAGE...]`, with `parents` (the session, extension and
    `--set` flags). `commands` are the subcommands' names, listed in its help."""
    parser = argparse.ArgumentParser(
        prog="tau",
        description="τ next (Trusty). `tau` opens the terminal UI; `tau -p MESSAGE` sends one "
        "message, prints the answer and exits.",
        parents=[*parents, verbose_flag()],
        epilog="commands (tau COMMAND --help): " + ", ".join(commands),
    )
    parser.add_argument(
        "-p",
        "--print",
        action="store_true",
        help="send MESSAGE (the words, then standard input after a blank line), print the "
        "answer and exit: 0 done, 1 failed, 2 usage, 3 stopped before finishing, 130 interrupted",
    )
    parser.add_argument(
        "-m", "--model", metavar="NAME", help="the model of a new session (default: default_model)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the session's events, one JSON object per line (as `tau serve` sends them)",
    )
    parser.add_argument(
        "--answer",
        action="append",
        default=[],
        type=answer_rule,
        metavar="EXTENSION[:TEXT]=VALUE",
        help="answer the questions extensions ask: EXTENSION is the asker or *, TEXT a glob over "
        "the question; the first rule that matches wins, and no match declines (repeatable)",
    )
    parser.add_argument(
        "--theme",
        metavar="NAME",
        help="the terminal UI's theme for this run (default: the config's tui.theme, else mocha)",
    )
    parser.add_argument(
        "-v", "--version", action="store_true", help="print the version and the protocol, and exit"
    )
    parser.add_argument("message", nargs="*", metavar="MESSAGE", help="the words to send")
    return parser


def assignment(text: str) -> tuple[str, str, str]:
    """`EXTENSION.FIELD=VALUE` as its three parts."""
    target, equals, given = text.partition("=")
    extension, dot, name = target.partition(".")
    if not (equals and dot and extension and name):
        raise argparse.ArgumentTypeError(f"{text!r} is not EXTENSION.FIELD=VALUE")
    return extension, name, given


def extension_flags() -> argparse.ArgumentParser:
    """A parser (`add_help=False`, for `parents=`) with the flags that choose extensions:
    `-e VALUE` (repeatable; a path, or the key of a source: a discovered one or a built-in) and
    `--no-extensions`.
    `tau.start_session` reads them from the parsed args."""
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "-e",
        "--extension",
        action="append",
        default=[],
        metavar="PATH|KEY",
        help="load this extension file, package directory, or source key even if it is off "
        "(repeatable); naming it is the consent",
    )
    flags.add_argument(
        "--no-extensions",
        action="store_true",
        help="skip every discovered extension source except -e: no installed package or drop-in "
        "(the built-ins, agents, attachments, base_tools, compaction, permissions, prune, skills, "
        "step_limit and usage, still follow `load`)",
    )
    return flags


def option_flags() -> argparse.ArgumentParser:
    """A parser (`add_help=False`, for `parents=`) with `--set EXTENSION.FIELD=VALUE`
    (repeatable): the config's command-line layer, for this run only (D22). A value is read as
    `/config` reads it; `tau.start_session` applies and checks them against the loaded
    extensions' options."""
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "--set",
        action="append",
        default=[],
        dest="set_options",
        type=assignment,
        metavar="EXTENSION.FIELD=VALUE",
        help="set an extension's option for this run only (repeatable), as `tau config set` "
        "would for every run",
    )
    return flags


def session_dir_flag(*, nested: bool = False) -> argparse.ArgumentParser:
    """`--session-dir DIR`, the sessions directory for one run (default: `$TAU_HOME/sessions`).
    `nested`: for a subcommand of a command that has it too, which must not reset it."""
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "--session-dir",
        metavar="DIR",
        default=argparse.SUPPRESS if nested else None,
        help="keep and find sessions in DIR (default: $TAU_HOME/sessions)",
    )
    return flags


def prompt_flags() -> argparse.ArgumentParser:
    """`--system-prompt TEXT`, `--append-system-prompt TEXT` (repeatable) and `-nc` (D20)."""
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "--system-prompt",
        metavar="TEXT",
        help="the base text of the system prompt, in place of the built-in one (use "
        '"$(cat FILE)" for a file; empty: no base text)',
    )
    flags.add_argument(
        "--append-system-prompt",
        action="append",
        default=[],
        metavar="TEXT",
        help="add a section after the base text (repeatable)",
    )
    flags.add_argument(
        "-nc",
        "--no-context-files",
        action="store_true",
        help="leave out AGENTS.md, CLAUDE.md and .tau/SYSTEM.md",
    )
    return flags


def session_flags() -> argparse.ArgumentParser:
    """A parser (`add_help=False`, for `parents=`) with the flags that choose the session
    (D18 section 5): `-c/--continue`, `-r/--resume`, `--session REF`, `--fork REF` and
    `--no-session` (at most one), `-n/--name NAME` and `--session-dir DIR`; and the prompt flags
    of a new session (D20): `--system-prompt TEXT`, `--append-system-prompt TEXT` (repeatable) and
    `-nc/--no-context-files`. Pass the parsed arguments to `start_session`, which makes paths
    absolute and rejects combinations."""
    flags = argparse.ArgumentParser(add_help=False, parents=[session_dir_flag(), prompt_flags()])
    flags.add_argument(
        "-c",
        "--continue",
        dest="continue_last",
        action="store_true",
        help="open the newest session of this directory",
    )
    flags.add_argument("-r", "--resume", action="store_true", help="choose a session to open")
    flags.add_argument(
        "--session", metavar="REF", help="open this session: a path, an id, a name or an id prefix"
    )
    flags.add_argument(
        "--fork",
        metavar="REF",
        help="start a new session as a copy of one walk of REF (REF:NODE for a node)",
    )
    flags.add_argument(
        "--no-session", action="store_true", help="keep the session in memory: never listed"
    )
    flags.add_argument("-n", "--name", metavar="NAME", help="name the session")
    return flags


STDIN_WAIT = 3.0
"""Seconds `-p` waits for standard input to say anything (a byte or the end), when it is not a
terminal and the line has no `-`."""
STDIN_NOTICE = 2.0
"""Seconds a `-` waits in silence before `-p` says what it is waiting for."""
STDIN_WORD = "-"
"""The word that means "read standard input, however long it takes" (as `cat -` does)."""


def _stdin_speaks(stdin: TextIO) -> bool:
    """Whether a read of `stdin` would not block at first: a byte or the end is there, within
    `STDIN_WAIT` seconds. A stream with no descriptor (in memory) always speaks."""
    try:
        descriptor = stdin.fileno()
    except (OSError, ValueError):  # io.UnsupportedOperation is both
        return True
    ready, _, _ = select.select([descriptor], [], [], STDIN_WAIT)
    return bool(ready)


def _read_stdin(stdin: TextIO, stderr: TextIO) -> str:
    """What `stdin` holds, to the end. If nothing has arrived after `STDIN_NOTICE` seconds, say on
    `stderr`, once, that this is what is being waited for, and go on waiting. `UsageError` if it
    is not text."""

    def say() -> None:
        stderr.write(
            "tau: waiting for standard input (end it with Ctrl-D, or EOF; `</dev/null` skips it)\n"
        )
        stderr.flush()

    timer = threading.Timer(STDIN_NOTICE, say)
    timer.daemon = True
    timer.start()
    try:
        text = stdin.read()
        text.encode("utf-8")  # the process's stdin keeps undecodable bytes as lone surrogates
    except UnicodeError as e:
        raise UsageError("standard input is not UTF-8 text") from e
    finally:
        timer.cancel()
    return text


def message_of(words: Sequence[str], stdin: TextIO, stderr: TextIO) -> tuple[str, str]:
    """The message as `words` joined with single spaces, and what standard input holds: the
    message is the first, or, if there are no words, the second. The second is otherwise kept
    apart, so a command can take it as its own and never as its arguments.

    Standard input is read, to its end, when the line has the word `-` (even from a terminal, and
    however long it takes), or when there are no words and it is not a terminal (the only possible
    message, so the same wait), or when it is not a terminal and says something (a byte or its
    end) within `STDIN_WAIT` seconds. A pipe or file that has already sent its data, `/dev/null`,
    and
    a closed descriptor are all read at once; one that stays silent (cron, ssh or a CI step that
    left it open) is said so on `stderr` and `-p` goes on without it. `UsageError` if there is
    no message."""
    text = " ".join(w for w in words if w != STDIN_WORD)
    text = text if text.strip() else ""
    piped = ""
    # no words: standard input is the only possible message, so it is waited for as `-` does
    if STDIN_WORD in words or (not text and not stdin.isatty()):
        piped = _read_stdin(stdin, stderr)
    elif not stdin.isatty():
        if _stdin_speaks(stdin):
            piped = _read_stdin(stdin, stderr)
        else:
            stderr.write(
                f"tau: no standard input within {STDIN_WAIT:g} s: going on without it "
                f"(the word `{STDIN_WORD}` waits for it)\n"
            )
    piped = piped if piped.strip() else ""
    if not text and not piped:
        raise UsageError(
            "nothing to send: give the message as arguments, or on standard input "
            f"(a pipe or file; the word `{STDIN_WORD}` waits for it, `</dev/null` has none)"
        )
    return (text, piped) if text else (piped, "")
