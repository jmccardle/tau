"""The launch command line (D22 section 1): `tau [-p] [flags] [MESSAGE...]`.

The first argument decides which parser reads the line: the name of a subcommand (`config`, `ext`,
`sessions`, `export`, `context`, `serve`, `trust`) runs that subcommand, and anything else,
including nothing, is the launch command line. So `tau -p sessions` sends the word "sessions".

`launch_parser` builds the launch parser from the flag parsers the program already has (the
session, extension and `--set` flags). `answer_rule` reads an `--answer` argument, and
`message_of` joins the words and standard input into the message `-p` sends.
"""

import argparse
import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO


class UsageError(Exception):
    """A usage error found after the line parsed: nothing to send, a command line missing its
    arguments, `-r` with `-p`. `tau` prints it and exits 2."""


@dataclass(frozen=True)
class AnswerRule:
    """One `--answer EXTENSION[:TEXT]=VALUE` (D22 section 5): the asking `extension` (or `*`), a
    glob over the question's `text` and the `value` to answer with, read by the question's schema.
    `spec` is the argument as typed."""

    extension: str
    text: str
    value: str
    spec: str

    def matches(self, extension: str, text: str) -> bool:
        """Whether the question `text` asked by `extension` is one this rule answers."""
        return self.extension in ("*", extension) and fnmatch.fnmatchcase(text, self.text)


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
        description="τ next (Trusty). `tau -p MESSAGE` sends one message, prints the answer and "
        "exits.",
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
        "-v", "--version", action="store_true", help="print the version and the protocol, and exit"
    )
    parser.add_argument("message", nargs="*", metavar="MESSAGE", help="the words to send")
    return parser


def message_of(words: Sequence[str], stdin: TextIO) -> str:
    """The message: `words` joined with single spaces, then, if `stdin` is not a terminal, a blank
    line and what it holds. `UsageError` if neither has anything."""
    text = " ".join(words)
    text = text if text.strip() else ""
    piped = "" if stdin.isatty() else stdin.read()
    piped = piped if piped.strip() else ""
    message = f"{text}\n\n{piped}" if text and piped else text or piped
    if not message:
        raise UsageError(
            "nothing to send: give the message as arguments, or on standard input "
            "(`tau -p </dev/null` has none)"
        )
    return message
