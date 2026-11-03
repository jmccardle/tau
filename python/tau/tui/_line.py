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
from typing import Protocol, cast

from textual.notifications import SeverityLevel

from tau._client import Client
from tau._commands import CommandFailed
from tau._core import Busy
from tau._values import Form, Json
from tau.commandline import Parsed, UnknownCommand, values
from tau.tui._editor import Completion
from tau.tui._forms import FormDialog


class Owner(Protocol):
    """What the command line needs of the app that holds it."""

    @property
    def client(self) -> Client: ...

    @property
    def cursor(self) -> int: ...

    def toast(
        self, text: str, *, title: str = "", severity: SeverityLevel = "information"
    ) -> None: ...

    def restore_draft(self, text: str) -> None: ...

    async def push_screen_wait(self, screen: FormDialog) -> dict[str, object] | None: ...


_PATH_MARK = "@"


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


class CommandLine:
    """The commands the session has, and what to do with a line of the app's editor."""

    def __init__(self, app: Owner) -> None:
        self.app = app
        self.commands: tuple[Form, ...] = ()
        self.options: dict[str, Form] = {}

    async def refresh(self) -> None:
        """Ask the session for its commands, and for the options of each extension `/config`
        names."""
        client, cursor = self.app.client, self.app.cursor
        self.commands = await client.commands()
        config = next((c for c in self.commands if c.name == "config"), None)
        names = [] if config is None else extensions_with_options(config)
        self.options = {n: await client.options(cursor, n) for n in names}

    def rows(self, typed: str) -> list[tuple[str, str]]:
        """The popup's rows for a first word `typed`: each command whose name starts with it, with
        the first line of its description; the qualified name where two extensions share one."""
        shared = Counter(c.name for c in self.commands)
        rows = [
            (f"/{c.qualified if shared[c.name] > 1 else c.name}", c.description.splitlines()[0])
            for c in self.commands
        ]
        return [row for row in rows if row[0].startswith(typed)]

    # Completion

    async def complete_text(self, before: str) -> Completion:
        """What could finish the last word of `before`."""
        start = word_start(before)
        client, cursor = self.app.client, self.app.cursor
        if before.startswith("/"):
            return start, await client.complete_line(cursor, before)
        word = before[start:]
        if word.startswith(_PATH_MARK):
            found = await client.complete(cursor, "path", word[1:])
            return start, [_PATH_MARK + path for path in found]
        return start, []

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
        except (CommandFailed, Busy, ValueError, LookupError, ConnectionError) as e:
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
            app.toast(f"no command /{e.name}: sent as text", severity="warning")
            return await self._send(line)
        if parsed.extension is not None and not parsed.args:
            app.toast(self._options_text(parsed), title=f"/config {parsed.extension}")
        elif parsed.command.name == "config" and parsed.missing:
            app.toast(self._options_text(parsed), title="/config")
        elif parsed.missing:
            return await self._ask_then_run(parsed)
        else:
            await self._run(parsed)
        return True

    def _options_text(self, parsed: Parsed) -> str:
        """What `/config` takes: the extensions with options, or one extension's options."""
        if parsed.extension is None:
            listed = ", ".join(self.options)
            return f"extensions with options: {listed}\n/config EXTENSION name=value ..."
        properties = cast("dict[str, Json]", self.options[parsed.extension].schema["properties"])
        fields = "".join(
            f"\n  {name}={schema.get('default', '?')}  {schema.get('description', '')}"
            for name, schema in properties.items()
        )
        return f"/config {parsed.extension} name=value ... [--save]{fields}"

    async def _ask_then_run(self, parsed: Parsed) -> bool:
        arguments = await self.app.push_screen_wait(
            FormDialog(
                f"/{parsed.command.qualified}",
                parsed.command.schema,
                self.complete_value,
                parsed.args,
                parsed.missing,
            )
        )
        if arguments is None:
            return False
        await self._run(replace(parsed, args=arguments, missing=()))
        return True

    async def _run(self, parsed: Parsed) -> None:
        """Run a command whose arguments are complete; its result is a toast."""
        result = await parsed.run(self.app.client, self.app.cursor)
        if result is not None:
            self.app.toast(result, title=f"/{parsed.command.name}")
