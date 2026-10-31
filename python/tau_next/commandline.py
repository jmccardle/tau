"""The parts of a line-based frontend that need no terminal (D16 section 5).

`parse(line, commands)` turns `/name arg arg name=value` into the command, its arguments as
decoded JSON, and the required parameters still missing; `completions(...)` says what could
finish the word being typed. Words are shell words (`shlex`): quote a value with spaces.
Positional words fill the parameters in order, skipping those set by `name=value`; a list
takes comma-separated items. Each word becomes a value by the parameter's schema (an `int` by
`int()`, a `Literal` by its values, a `bool` by `true`/`false`). Whether the value is allowed
(a model that is configured, a cursor that is open) is the driver's check, not this module's:
it only spares a round trip.
"""

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from tau_next._commands import CommandInfo
from tau_next.client import Client
from tau_next.values import Json


class UnknownCommand(LookupError):
    """The line names a command there is not. A frontend warns and sends the line as text."""

    def __init__(self, name: str) -> None:
        super().__init__(f"no command {name!r}")
        self.name = name


@dataclass(frozen=True)
class Parsed:
    """A command line, read: the `command`, its `args` (decoded JSON, ready for `Client.run`),
    and the `missing` required parameters, in order, which a frontend asks for."""

    command: CommandInfo
    args: dict[str, object]
    missing: tuple[str, ...]


_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def _scalar(word: str, schema: Json) -> object:
    kind = schema.get("type")
    value: object
    if kind == "integer":
        try:
            value = int(word)
        except ValueError:
            raise ValueError(f"{word!r} is not an integer") from None
    elif kind == "number":
        try:
            value = float(word)
        except ValueError:
            raise ValueError(f"{word!r} is not a number") from None
    elif kind == "boolean":
        if word.lower() not in (*_TRUE, *_FALSE):
            raise ValueError(f"{word!r} is not true or false")
        value = word.lower() in _TRUE
    else:
        value = word
    allowed = cast("list[object] | None", schema.get("enum"))
    if allowed is not None and value not in allowed:
        raise ValueError(f"{word!r} is not one of {allowed}")
    return value


def _convert(word: str, schema: Json) -> object:
    """The decoded JSON `word` stands for under `schema`. `ValueError` if it stands for none."""
    alternatives = cast("list[Json] | None", schema.get("anyOf"))
    if alternatives is not None:
        problems: list[str] = []
        for alternative in alternatives:
            if alternative.get("type") == "null":
                continue
            try:
                return _convert(word, alternative)
            except ValueError as e:
                problems.append(str(e))
        raise ValueError(" and ".join(problems))
    if schema.get("type") == "array":
        items = cast("Json", schema["items"])
        return [_convert(w, items) for w in word.split(",")] if word else []
    return _scalar(word, schema)


def _properties(command: CommandInfo) -> Json:
    return cast("Json", command.schema["properties"])


def _split(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError as e:
        raise ValueError(f"cannot read the line: {e}") from None


def _find(name: str, commands: Sequence[CommandInfo]) -> CommandInfo:
    for command in commands:
        if command.name == name:
            return command
    raise UnknownCommand(name)


def parse(line: str, commands: Sequence[CommandInfo]) -> Parsed:
    """Read a line that starts with `/`. `UnknownCommand` if no command has the name,
    `ValueError` naming the problem for a line that is not a command line, quotes that do not
    close, too many words, or a word that cannot be the parameter's value."""
    words = _split(line)
    if not words or not words[0].startswith("/") or words[0] == "/":
        raise ValueError("a command line starts with /name")
    command = _find(words[0][1:], commands)
    properties = _properties(command)
    args: dict[str, object] = {}
    positional: list[str] = []
    for word in words[1:]:
        key, equals, value = word.partition("=")
        if equals and key in properties:
            args[key] = _argument(command, key, value)
        else:
            positional.append(word)
    free = [name for name in properties if name not in args]
    if len(positional) > len(free):
        raise ValueError(f"/{command.name} takes {len(properties)} arguments {list(properties)}")
    for name, word in zip(free, positional, strict=False):
        args[name] = _argument(command, name, word)
    required = cast("list[str]", command.schema.get("required", []))
    return Parsed(command, args, tuple(name for name in required if name not in args))


def _argument(command: CommandInfo, name: str, word: str) -> object:
    try:
        return _convert(word, cast("Json", _properties(command)[name]))
    except ValueError as e:
        raise ValueError(f"/{command.name} {name}: {e}") from None


def _typing(line: str) -> tuple[list[str], str]:
    """The finished words of a line being typed and the word still being typed (empty after
    a space). A quote not yet closed is closed for the purpose."""
    for closing in ("", "'", '"'):
        try:
            words = shlex.split(line + closing)
        except ValueError:
            continue
        if closing or (line and not line[-1].isspace()):
            return words[:-1], words[-1] if words else ""
        return words, ""
    return [], ""


async def _values(schema: Json, prefix: str, client: Client, cursor: int) -> list[str]:
    """What could finish `prefix` as a value of `schema`."""
    named = schema.get("format")
    if isinstance(named, str):
        return await client.complete(cursor, named, prefix)
    if schema.get("type") == "array":
        head, comma, tail = prefix.rpartition(",")
        items = await _values(cast("Json", schema["items"]), tail, client, cursor)
        return [head + comma + item for item in items]
    allowed = cast("list[object] | None", schema.get("enum"))
    if allowed is not None:
        return [str(v) for v in allowed if str(v).startswith(prefix)]
    if schema.get("type") == "boolean":
        return [v for v in ("true", "false") if v.startswith(prefix)]
    found: dict[str, None] = {}
    for alternative in cast("list[Json]", schema.get("anyOf", [])):
        for value in await _values(alternative, prefix, client, cursor):
            found[value] = None
    return list(found)


async def completions(
    line: str, commands: Sequence[CommandInfo], client: Client, cursor: int
) -> list[str]:
    """What could finish the word being typed at the end of `line`, each a whole word to
    replace it with (quoted if it needs to be): command names after `/`, then for each
    parameter the values of its `Literal` and, for a named type, `client.complete` (paths on
    the driver's machine). Nothing for a line that is not a command line, an unknown command,
    or a word past the last parameter."""
    if not line.startswith("/"):
        return []
    words, current = _typing(line)
    if not words:
        return [f"/{c.name}" for c in commands if c.name.startswith(current[1:])]
    try:
        command = _find(words[0][1:], commands)
    except UnknownCommand:
        return []
    properties = _properties(command)
    named: set[str] = set()
    positional = 0
    for word in words[1:]:
        key, equals, _ = word.partition("=")
        if equals and key in properties:
            named.add(key)
        else:
            positional += 1
    key, equals, value = current.partition("=")
    if equals and key in properties:
        name, prefix, lead = key, value, f"{key}="
    else:
        free = [p for p in properties if p not in named]
        if positional >= len(free):
            return []
        name, prefix, lead = free[positional], current, ""
    values = await _values(cast("Json", properties[name]), prefix, client, cursor)
    return [lead + shlex.quote(v) for v in values]
