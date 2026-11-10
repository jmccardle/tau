"""Helpers for the base tool tests: calling a tool from a plain test."""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from tau import Outcome, Tool


def call(tool: Tool, **args: object) -> Outcome:
    """The tool's `call` with `args` as JSON, run to its `Outcome`."""
    return asyncio.run(tool.call(json.dumps(args)))


def put(path: Path, text: str) -> None:
    """Write `text` as it is: UTF-8 and `\\n`, on every platform (`write_text` translates)."""
    path.write_bytes(text.encode("utf-8"))


def text_of(path: Path) -> str:
    """The file's text as it is: UTF-8, no newline translation."""
    return path.read_bytes().decode("utf-8")


def ok(outcome: Outcome) -> str:
    assert isinstance(outcome, Outcome.Ok), outcome
    return outcome.value


def err(outcome: Outcome) -> str:
    assert isinstance(outcome, Outcome.Err), outcome
    return outcome.error


NOTE = re.compile(
    r"\[truncated: (?P<omitted>\d+) lines? omitted (?P<where>before|after) these"
    r"(?P<cut>, and one long line cut short)?; "
    r"(?:full output \((?P<size>\d+) bytes\) is in|the output is (?P<size2>\d+) bytes but)"
    r" (?P<path>[^\] ]+\.out)(?: holds only the first \d+ bytes)?\]"
)


@dataclass(frozen=True)
class Truncated:
    """A truncated answer, taken apart."""

    kept: str
    omitted: int
    where: str
    size: int
    path: Path
    cut: bool

    @property
    def full(self) -> bytes:
        return self.path.read_bytes()


def parse_truncated(text: str) -> Truncated:
    """Split an answer into the kept text and its note; the note is at the end for the
    head and at the start for the tail."""
    found = NOTE.search(text)
    assert found is not None, f"no truncation note in {text[:200]!r}"
    if found["where"] == "after":
        kept = text[: found.start()].removesuffix("\n")
    else:
        kept = text[found.end() :].removeprefix("\n")
    return Truncated(
        kept=kept,
        omitted=int(found["omitted"]),
        where=found["where"],
        size=int(found["size"] or found["size2"]),
        path=Path(found["path"]),
        cut=found["cut"] is not None,
    )
