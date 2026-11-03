"""The merged config tree and the problems found reading it (D13): the pieces `_config_layers`
(layers, models, groups) and `_config_keys` (the other top-level keys) share."""

import difflib
from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning"]
"""How bad a `ConfigProblem` is: an `error` fails the check, a `warning` does not."""


@dataclass(frozen=True)
class ConfigProblem:
    """One thing wrong (or suspicious) in the config layers.

    `source` is the file path or `"cli"`; `path` the dotted location inside it
    (`models.free_tokens.models.gpt-6-sol.modle`); `line` and `column` are set
    for JSON syntax errors. The message never holds an API key.
    """

    severity: Severity
    message: str
    source: str | None = None
    path: str = ""
    line: int | None = None
    column: int | None = None

    def __str__(self) -> str:
        where = self.source or "config"
        if self.line is not None:
            where += f":{self.line}:{self.column}"
        at = f"{self.path}: " if self.path else ""
        return f"{where}: {self.severity}: {at}{self.message}"


@dataclass(frozen=True)
class Leaf:
    value: object
    source: str


type Tree = dict[str, Tree | Leaf]


class Problems:
    def __init__(self) -> None:
        self.found: list[ConfigProblem] = []

    def add(self, severity: Severity, message: str, source: str | None, path: str) -> None:
        self.found.append(ConfigProblem(severity, message, source, path))

    def errors_since(self, mark: int) -> bool:
        return any(p.severity == "error" for p in self.found[mark:])


def suggest(key: str, known: tuple[str, ...]) -> str:
    close = difflib.get_close_matches(key, known, n=1)
    return f"; did you mean {close[0]!r}?" if close else f"; known keys are {list(known)}"


def source_of(node: Tree | Leaf) -> str | None:
    if isinstance(node, Leaf):
        return node.source
    for child in node.values():
        found = source_of(child)
        if found is not None:
            return found
    return None


def is_set(node: Tree | Leaf | None) -> bool:
    return node is not None and not (isinstance(node, Leaf) and node.value is None)


def plain(node: Tree | Leaf) -> object:
    """A merged subtree as the JSON it came from, its nulls dropped."""
    if isinstance(node, Leaf):
        return node.value
    return {k: plain(v) for k, v in node.items() if not (isinstance(v, Leaf) and v.value is None)}
