"""The parts of a line-based frontend that need no terminal (D16 section 5).

`parse(line, commands, options)` turns `/name arg arg name=value` into the command, its
arguments as decoded JSON, and the required parameters still missing; `completions(...)` says
what could finish the word being typed. `value` and `values` do the same for one value of any
schema: a question's answer typed as text is read and completed as a command's argument is.
A command is typed `/name`, or `/extension:name`, the qualified name nothing can take: a bare
name that two extensions share is ambiguous (`LookupError`) and is completed qualified.
Words are shell words (`shlex`): quote a value with spaces. Positional words fill the
parameters in order, skipping those set by `name=value`; except that when the last parameter
is a plain `str` (or a `str | None`; not a `Literal` or a named type) it takes the rest of the
line from the word that would fill it, as typed, quotes and spacing included
(`/search 3 what is 2+2`), unless that is exactly one shell word, which is read as one
(`/search 3 "what is 2+2"` is `what is 2+2`); name it, `query="..."`, to give it one shell word
whatever follows. A list takes comma-separated items, or a JSON array if the text starts with `[`
(for items that hold a comma; so an item that starts with `[` is typed in an array).
Each word becomes a value by the parameter's schema (an `int` by `int()`, a `Literal` by its
values, a `bool` by `true`/`false`). Whether the value is allowed (a model that is configured, a
cursor that is open) is the driver's check, not this module's: it only spares a round trip.

`/config <extension> name=value ...` is the one line that is not a command's: the words after
the extension are read by that extension's options form (`Client.options`), which a frontend
passes as `options`, by extension name, for the extensions the `config` form lists. The flag
`--save`, anywhere after the extension, also writes the values into the user's config file, for
every session (`Client.configure(save=True)`); without it they change only the branch.
"""

import json
import shlex
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from tau._commands import CONFIG, ambiguous
from tau._values import Form, Json

if TYPE_CHECKING:  # client imports us: it returns a `Parsed`
    from tau.client import Client

NO_OPTIONS: Mapping[str, Form] = MappingProxyType({})
SAVE = "--save"
"""The flag of `/config <extension> name=value --save`: write to the user's file too."""


class UnknownCommand(LookupError):
    """The line names a command there is not. A frontend warns and sends the line as text."""

    def __init__(self, name: str) -> None:
        super().__init__(f"no command {name!r}")
        self.name = name


@dataclass(frozen=True)
class Parsed:
    """A command line, read: the `command` form, its `args` (decoded JSON), and the `missing`
    required parameters, in order, which a frontend asks for. For `/config <extension>` the
    `command` is the extension's options form, `args` the options to change and `extension`
    its name; with no `args` the line asks for the form, which a frontend shows from
    `Client.options`. `save` is whether `--save` was typed. `run` makes the call."""

    command: Form
    args: dict[str, object]
    missing: tuple[str, ...]
    extension: str | None = None
    save: bool = False

    async def run(self, client: "Client", cursor: int) -> str | None:
        """`client.run` the command, or `client.configure` the extension's options; returns the
        command's result."""
        if self.extension is None:
            return await client.run(cursor, self.command.qualified, self.args)
        await client.configure(cursor, self.extension, self.args, save=self.save)
        return None


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


def value(text: str, schema: Json) -> object:
    """The decoded JSON `text` stands for under `schema`: a command's argument, or the answer
    to a question. `ValueError` if it stands for none."""
    alternatives = cast("list[Json] | None", schema.get("anyOf"))
    if alternatives is not None:
        problems: list[str] = []
        for alternative in alternatives:
            if alternative.get("type") == "null":
                continue
            try:
                return value(text, alternative)
            except ValueError as e:
                problems.append(str(e))
        raise ValueError(" and ".join(problems))
    if schema.get("type") == "array":
        if text.startswith("["):  # a JSON array: its items may hold commas
            try:
                decoded = json.loads(text)
            except ValueError as e:
                raise ValueError(
                    f"{text!r} is not a JSON array: {e}{_array_hint(text, schema)}"
                ) from None
            return cast("list[object]", decoded)
        items = cast("Json", schema["items"])
        return [value(w, items) for w in text.split(",")] if text else []
    return _scalar(text, schema)


def _array_hint(text: str, schema: Json) -> str:
    """How to type a list whose text starts with `[` but is not JSON: such a value is read as a
    JSON array whole, so an item that starts with `[` is typed inside one."""
    hint = "; text starting with [ is read as a JSON array"
    if cast("Json", schema["items"]).get("type") == "string":
        hint += f", so for comma-separated items type {json.dumps(text.split(','))}"
    return hint


def _properties(form: Form) -> Json:
    return cast("Json", form.schema["properties"])


def _split(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError as e:
        raise ValueError(f"cannot read the line: {e}") from None


def _find(name: str, commands: Sequence[Form]) -> Form:
    """The command `/name` is: a qualified `extension:name` exactly, a bare `name` if only one
    extension has it. `UnknownCommand` for none; plain `LookupError`, naming each, for several."""
    if ":" in name:
        found = [c for c in commands if c.qualified == name]
    else:
        found = [c for c in commands if c.name == name]
    if not found:
        raise UnknownCommand(name)
    if len(found) > 1:
        raise LookupError(ambiguous(name, [c.qualified for c in found]))
    return found[0]


def parse(line: str, commands: Sequence[Form], options: Mapping[str, Form] = NO_OPTIONS) -> Parsed:
    """Read a line that starts with `/`. `UnknownCommand` if no command has the name, plain
    `LookupError` ("/note is two commands: /a:note, /b:note") if a bare name is several
    extensions' commands, `ValueError` naming the problem for a line that is not a command
    line, quotes that do not close, too many words, a word that cannot be the parameter's
    value, or `/config` of an extension not in `options` (the options forms of
    `Client.options`, by extension)."""
    name, *rest = line.strip().split(None, 1)
    text = rest[0] if rest else ""
    if not name.startswith("/") or name == "/":
        raise ValueError("a command line starts with /name")
    command = _find(name[1:], commands)
    if command.name != CONFIG:
        return _read(command, *_words_and_rest(command, text))
    words = _split(text)
    if not words:
        return Parsed(command, {}, ("extension",))
    extension = _extension(words[0])
    form = options.get(extension)
    if form is None:
        raise ValueError(f"/{CONFIG}: {extension!r} has no options; those that do: {list(options)}")
    save = SAVE in words[1:]
    parsed = _read(form, [w for w in words[1:] if w != SAVE])
    if save and not parsed.args:
        raise ValueError(f"/{CONFIG}: {SAVE} needs a name=value to save")
    return replace(parsed, extension=extension, save=save)


def _takes_rest(schema: Json) -> bool:
    """Whether a parameter is a plain `str`, or a `str | None`: no choices, no named type."""
    alternatives = cast("list[Json]", schema.get("anyOf", [schema]))
    texts = [a for a in alternatives if a.get("type") != "null"]
    return (
        len(texts) == 1
        and texts[0].get("type") == "string"
        and not ("enum" in texts[0] or "format" in texts[0])
    )


def _words_and_rest(form: Form, text: str) -> tuple[list[str], str | None]:
    """The shell words of `text`, up to the word that fills the last parameter when that is a
    plain `str` (`_takes_rest`), and the rest of the text as typed, from that word on, or `None`
    if no word fills it. A `name=value` word sets its parameter; any other fills the first
    parameter not set before it."""
    properties = list(_properties(form))
    if not properties or not _takes_rest(cast("Json", _properties(form)[properties[-1]])):
        return _split(text), None
    words: list[str] = []
    named: set[str] = set()
    positional = 0
    while text.strip():
        lexer = shlex.shlex(text, posix=True)
        lexer.whitespace_split, lexer.commenters = True, ""
        word: str | None = None
        problem = ""
        try:
            word = lexer.get_token()
        except ValueError as e:  # an open quote: fine if it starts the rest, which is text
            problem = str(e)
        key, equals, _ = (word or "").partition("=")
        if equals and key in properties:
            named.add(key)
        else:
            free = [p for p in properties if p not in named]
            if positional < len(free) and free[positional] == properties[-1]:
                return words, _one_word(text.strip())
            positional += 1
        if word is None:
            raise ValueError(f"cannot read the line: {problem}")
        words.append(word)
        text = text[lexer.instream.tell() :]  # type: ignore[union-attr]
    return words, None


def _one_word(rest: str) -> str:
    """The rest of a line: the one shell word it is, if it is exactly one (`"what is 2+2"`
    is `what is 2+2`); otherwise as typed."""
    try:
        words = shlex.split(rest)
    except ValueError:  # an open quote: the text is as typed
        return rest
    return words[0] if len(words) == 1 else rest


def _read(form: Form, words: Sequence[str], rest: str | None = None) -> Parsed:
    """`words` read as the arguments of `form`, and `rest` as its last parameter."""
    properties = _properties(form)
    args: dict[str, object] = {}
    if rest is not None:
        args[next(reversed(properties))] = rest
    positional: list[str] = []
    for word in words:
        key, equals, text = word.partition("=")
        if equals and key in properties:
            args[key] = _argument(form, key, text)
        else:
            positional.append(word)
    free = [name for name in properties if name not in args]
    if len(positional) > len(free):
        raise ValueError(f"/{form.name} takes {len(properties)} arguments {list(properties)}")
    for name, word in zip(free, positional, strict=False):
        args[name] = _argument(form, name, word)
    required = cast("list[str]", form.schema.get("required", []))
    return Parsed(form, args, tuple(name for name in required if name not in args))


def _argument(form: Form, name: str, word: str) -> object:
    try:
        return value(word, cast("Json", _properties(form)[name]))
    except ValueError as e:
        raise ValueError(f"/{form.name} {name}: {e}") from None


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


async def values(schema: Json, prefix: str, client: "Client", cursor: int) -> list[str]:
    """What could finish `prefix` as a value of `schema`, each a whole value to replace it
    with (unquoted): a named type's through `client.complete` (paths on the driver's machine),
    the values of a `Literal`, `true` and `false`, the items of a list after its last comma."""
    named = schema.get("format")
    if isinstance(named, str):
        return await client.complete(cursor, named, prefix)
    if schema.get("type") == "array":
        if prefix.startswith("["):  # a JSON array is typed whole, not completed
            return []
        head, comma, tail = prefix.rpartition(",")
        items = await values(cast("Json", schema["items"]), tail, client, cursor)
        return [head + comma + item for item in items]
    allowed = cast("list[object] | None", schema.get("enum"))
    if allowed is not None:
        return [str(v) for v in allowed if str(v).startswith(prefix)]
    if schema.get("type") == "boolean":
        return [v for v in ("true", "false") if v.startswith(prefix)]
    found: dict[str, None] = {}
    for alternative in cast("list[Json]", schema.get("anyOf", [])):
        for finish in await values(alternative, prefix, client, cursor):
            found[finish] = None
    return list(found)


async def completions(
    line: str,
    commands: Sequence[Form],
    client: "Client",
    cursor: int,
    options: Mapping[str, Form] = NO_OPTIONS,
) -> list[str]:
    """What could finish the word being typed at the end of `line`, each a whole word to
    replace it with (quoted if it needs to be): command names after `/`, then for each
    parameter its `values`; after `/config <extension>`, the extension's options (`--save` is
    typed, not completed). Nothing for a line that is not a command line, an unknown command, or
    a word past the last parameter."""
    if not line.startswith("/"):
        return []
    words, current = _typing(line)
    if not words:
        return _command_names(current[1:], commands)
    try:
        form = _find(words[0][1:], commands)
    except LookupError:  # unknown, or several: nothing to complete until it is qualified
        return []
    rest = words[1:]
    if form.name == CONFIG and rest:
        chosen = options.get(_extension(rest[0]))
        if chosen is None:
            return []
        form, rest = chosen, [w for w in rest[1:] if w != SAVE]
    wanted = _wanted(form, rest, current)
    if wanted is None:
        return []
    name, prefix, lead = wanted
    found = await values(cast("Json", _properties(form)[name]), prefix, client, cursor)
    return [lead + shlex.quote(v) for v in found]


def _command_names(prefix: str, commands: Sequence[Form]) -> list[str]:
    """The names that could finish `/prefix`: the bare name of each command only one extension
    has and the qualified name of each that several share; after `extension:`, that extension's
    commands."""
    shared = Counter(c.name for c in commands)
    if ":" in prefix:
        return [f"/{c.qualified}" for c in commands if c.qualified.startswith(prefix)]
    return [
        f"/{c.name if shared[c.name] == 1 else c.qualified}"
        for c in commands
        if (c.name if shared[c.name] == 1 else c.qualified).startswith(prefix)
    ]


def _extension(word: str) -> str:
    """The extension a word after `/config` names: `step_limit` or `extension=step_limit`."""
    key, equals, named = word.partition("=")
    return named if equals and key == "extension" else word


def _wanted(form: Form, words: Sequence[str], current: str) -> tuple[str, str, str] | None:
    """The parameter the word being typed (`current`) is a value of, after `words` were
    typed: its name, the prefix of its value, and what leads the completion (`name=`);
    `None` past the last parameter."""
    properties = _properties(form)
    named: set[str] = set()
    positional = 0
    for word in words:
        key, equals, _ = word.partition("=")
        if equals and key in properties:
            named.add(key)
        else:
            positional += 1
    key, equals, text = current.partition("=")
    if equals and key in properties:
        return key, text, f"{key}="
    free = [p for p in properties if p not in named]
    return None if positional >= len(free) else (free[positional], current, "")
