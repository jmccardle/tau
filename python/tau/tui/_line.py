"""The editor's line as a command line (D23 section 4): what `/` lists, what Tab completes, and
what Enter does with a line.

Enter on a `/` line is `Client.parse_line`: `UnknownCommand` warns and sends the line as text;
`LookupError` (a bare name two extensions share) is shown and nothing is sent; a line with
`missing` arguments opens the form; otherwise `Parsed.run`, the result a toast, `CommandFailed`
and `Busy` error toasts. Completion is the session's: `complete_line` for a command line,
`complete(cursor, "path", ...)` for an `@` word of a message (`complete_line` completes command
lines only), `commandline.values` for the field of a form or a question.
"""

from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast

from textual.notifications import SeverityLevel
from textual.screen import Screen

from tau._client import Client
from tau._commands import CommandFailed
from tau._core import Busy
from tau._values import Form, Json
from tau.commandline import STDIN, Parsed, UnknownCommand, values
from tau.commandline import value as read_value
from tau.tui._choose import PickDialog
from tau.tui._editor import Completion
from tau.tui._fields import shape_of
from tau.tui._forms import FormDialog, OptionsDialog
from tau.tui._view import SessionView


class Owner(Protocol):
    """What the command line needs of the app that holds it."""

    @property
    def client(self) -> Client: ...

    @property
    def cursor(self) -> int: ...

    @property
    def session(self) -> SessionView: ...

    @property
    def home(self) -> Path: ...

    def toast(
        self, text: str, *, title: str = "", severity: SeverityLevel = "information"
    ) -> None: ...

    def restore_draft(self, text: str) -> None: ...

    async def push_screen_wait[T](self, screen: Screen[T]) -> T: ...


_PATH_MARK = "@"

CLOSED_TYPES = ("model", "cursor")
"""The named types (`Schema["format"]`) whose values are a short, closed list, so a missing
argument of one is picked from the list (`complete(cursor, named, "")`) rather than typed."""


def word_start(line: str) -> int:
    """Where the word being typed at the end of `line` starts: after the last space that is not
    inside quotes."""
    start, quote, escaped = 0, "", False
    for i, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char.isspace():
            start = i + 1
    return start


def extensions_with_options(config: Form) -> list[str]:
    """The extensions `/config` takes, from the `config` form's one argument."""
    extension = cast("dict[str, dict[str, list[str]]]", config.schema["properties"])["extension"]
    return extension["enum"]


def _hint(command: Form) -> str:
    """What a command's parameters are, as its schema describes them (a template's
    `argument-hint`); `""` if it says nothing. `stdin` is not typed."""
    properties = cast("dict[str, Json]", command.schema.get("properties", {}))
    return " ".join(
        str(p["description"]) for n, p in properties.items() if n != STDIN and "description" in p
    )


class CommandLine:
    """The commands the session has, and what to do with a line of the app's editor."""

    def __init__(self, app: Owner) -> None:
        self.app = app
        self.commands: tuple[Form, ...] = ()
        self.extensions: list[str] = []

    async def refresh(self) -> None:
        """Ask the session for its commands, and which extensions `/config` takes."""
        self.commands = await self.app.client.commands()
        config = next((c for c in self.commands if c.name == "config"), None)
        self.extensions = [] if config is None else extensions_with_options(config)

    def rows(self, typed: str) -> list[tuple[str, str]]:
        """The popup's rows for a first word `typed`: each command whose name starts with it, with
        its argument hint (what its parameters are described as) and the first line of its
        description; the qualified name where two extensions share one. For the word and one
        space (`/review `), the one row of that command, if it has a hint: what to type next."""
        shared = Counter(c.name for c in self.commands)
        named = [(c, f"/{c.qualified if shared[c.name] > 1 else c.name}") for c in self.commands]
        if typed.endswith(" "):
            word = typed.strip()
            named = [(c, name) for c, name in named if name == word or c.qualified == word[1:]]
        elif typed:
            named = [(c, name) for c, name in named if name.startswith(typed)]
        return [
            (f"{name} {_hint(c)}".rstrip(), c.description.splitlines()[0])
            for c, name in named
            if _hint(c) or not typed.endswith(" ")
        ]

    async def renew(self) -> bool:
        """Ask the session for its commands again (it may have new ones: they are files); whether
        they are not the ones the popup showed."""
        before = self.commands
        await self.refresh()
        return self.commands != before

    # Completion

    async def complete_text(self, before: str) -> Completion:
        """What could finish the last word of `before`."""
        start = word_start(before)
        client, cursor = self.app.client, self.app.cursor
        if before.startswith("/"):
            return start, await client.complete_line(cursor, before)
        word = before[start:]
        if word.startswith(_PATH_MARK):
            return start, await self._complete_path(word[1:])
        return start, []

    async def _complete_path(self, typed: str) -> list[str]:
        """The `@` words that finish `typed` (the word after the `@`): a path with a space is
        written `@"a b.txt"`, its quote left open on a directory so the name can go on."""
        quoted = typed.startswith('"')
        found = await self.app.client.complete(self.app.cursor, "path", typed.removeprefix('"'))
        words: list[str] = []
        for path in found:
            if quoted or " " in path:
                words.append(f'{_PATH_MARK}"{path}' + ("" if path.endswith("/") else '"'))
            else:
                words.append(_PATH_MARK + path)
        return words

    async def complete_value(self, schema: Json, text: str) -> list[str]:
        """What could finish `text` as a value of `schema` (a form's field, a question's answer)."""
        return await values(schema, text, self.app.client, self.app.cursor)

    # Enter

    async def submit(self, text: str) -> None:
        """Send the draft (already out of the editor) as a message, or run it as a command line.
        A line that was not taken, because it was refused, the session could not take it, or the
        form was cancelled, goes back to the editor, and a refusal is a toast saying why."""
        app = self.app
        taken = False
        try:
            if text.strip() != "/":
                taken = await (self._command(text) if text.startswith("/") else self._send(text))
        except ConnectionError as e:
            app.toast(
                f"{e}. The line may have reached the session (check the transcript); "
                "it is back in the editor",
                severity="error",
            )
        except (CommandFailed, Busy, ValueError, LookupError) as e:
            app.toast(str(e), severity="error")
        if not taken:
            app.restore_draft(text)

    async def _send(self, text: str) -> bool:
        await self.app.client.send(self.app.cursor, text)
        return True

    async def _command(self, line: str) -> bool:
        app = self.app
        try:
            parsed = await app.client.parse_line(app.cursor, line)
        except UnknownCommand as e:
            if ":" in e.name:  # `/skills:nope` names an extension: it cannot be text
                app.toast(f"no command /{e.name}", severity="error")
                return False
            app.toast(f"no command /{e.name}: sent as text", severity="warning")
            return await self._send(line)
        if parsed.extension is not None and not parsed.args:
            return await self._configure(parsed.extension, parsed.save)
        if parsed.command.name == "config" and parsed.missing:
            return await self._configure_which()
        if parsed.missing:
            return await self._ask_then_run(parsed)
        await self._run(parsed)
        return True

    async def _configure_which(self) -> bool:
        """`/config` alone: pick the extension, then edit its options."""
        picked = await self.app.push_screen_wait(
            PickDialog("Configure which extension?", self.extensions)
        )
        return picked is not None and await self._configure(picked, False)

    async def _configure(self, extension: str, save: bool) -> bool:
        """The options form of `extension` (the values now, from the session); what changed goes
        to the cursor's branch as a `Config` node, and also to the user's config file when "save
        for every session" is on."""
        app = self.app
        form = await app.client.options(app.cursor, extension)  # the values now
        dialog = OptionsDialog(f"/config {extension}", form.schema, self.complete_value, save)
        changed = await app.push_screen_wait(dialog)
        if changed is None:
            return False
        if not changed and not dialog.save:
            app.toast("nothing changed", title=f"/config {extension}")
            return True
        await app.client.configure(app.cursor, extension, changed, save=dialog.save)
        await self.refresh()  # the options now are the new branch's
        where = "for every session" if dialog.save else "on this branch"
        shown = ", ".join(f"{k}={v}" for k, v in changed.items()) or "the branch's options"
        app.toast(f"{shown} ({where})", title=f"/config {extension}")
        return True

    async def _ask_then_run(self, parsed: Parsed) -> bool:
        """Ask for the arguments a command line lacks: a closed set of names (the models) is
        picked from a list over `complete`, anything else is a field of the form."""
        app = self.app
        properties = cast("dict[str, Json]", parsed.command.schema.get("properties", {}))
        arguments, missing = dict(parsed.args), list(parsed.missing)
        for name in parsed.missing:
            schema = shape_of(properties[name])[0]
            named = schema.get("format")
            if named not in CLOSED_TYPES:
                continue
            offered = await app.client.complete(app.cursor, cast("str", named), "")
            now = app.session.views[app.cursor].model if named == "model" else None
            picked = await app.push_screen_wait(
                PickDialog(f"/{parsed.command.name}: choose {name}", offered, now)
            )
            if picked is None:
                return False
            arguments[name] = read_value(picked, schema)
            missing.remove(name)
        if missing:
            asked = await app.push_screen_wait(
                FormDialog(
                    f"/{parsed.command.qualified}",
                    parsed.command.schema,
                    self.complete_value,
                    arguments,
                    tuple(missing),
                )
            )
            if asked is None:
                return False
            arguments = asked
        await self._run(replace(parsed, args=arguments, missing=()))
        return True

    async def _run(self, parsed: Parsed) -> None:
        """Run a command whose arguments are complete; its result is a toast."""
        result = await parsed.run(self.app.client, self.app.cursor)
        if result is not None:
            self.app.toast(result, title=f"/{parsed.command.name}")
