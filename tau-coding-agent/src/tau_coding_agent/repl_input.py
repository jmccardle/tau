"""How the REPL head reads a line — the one seam every prompt goes through.

Reference: docs/REPL-HEAD.md §5. ``rich`` renders and ``prompt_toolkit`` reads:
one :class:`PromptToolkitReader` owns the terminal for the process's life, and
:class:`MemoryReader` answers from lists so the suite can drive ``run_repl``
where no tty exists.

The reader owns the spinner as well as the line, because both are the terminal's
and only one thing may own a terminal at a time: while no prompt is outstanding
the spinner is a ``rich`` status, and once a read is outstanding it is the
prompt's bottom toolbar. A head that drove a ``rich.live.Live`` of its own
against an open prompt would fight it for the cursor (measured in the spike
§5 records).

This is the ONLY module that imports ``prompt_toolkit``, and it imports it
lazily, so a τ installed without the ``repl`` extra reaches an instruction rather
than a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

#: Validates one typed answer: returns an error message to re-ask, or ``None`` to accept.
Validate = Callable[[str], str | None]

#: What Ctrl+C means. Returns whether it was handled — ``False`` ends the read.
Interrupt = Callable[[], bool]

#: What Up on an empty line offers back, or ``None`` to walk history instead.
Reclaim = Callable[[], str | None]

#: The byte a terminal sends for Ctrl+C, and how :class:`MemoryReader` scripts one.
INTERRUPT = "\x03"

#: The sequence a terminal sends for Up, and how :class:`MemoryReader` scripts a reclaim.
RECLAIM = "\x1b[A"


@dataclass(frozen=True)
class Candidate:
    """One thing Tab may insert, and where in the line it goes.

    Head-neutral on purpose: the head knows the three vocabularies
    (docs/REPL-HEAD.md §6) and the reader knows the key, so what crosses between
    them is a span and a replacement rather than a ``prompt_toolkit`` object.

    The span ends at the cursor — a completer is asked about ``text[:cursor]``,
    the way a shell completes the word to the left of the point — so ``start`` is
    all that is needed to place it.

    Attributes:
        text: What replaces ``line[start:cursor]``.
        start: Where that replacement begins, an offset into the line.
        display: What the menu shows; ``text`` when empty.
        meta: The dim note beside it — a size, a description, or the one warning
            line for a ``/…`` that names nothing.
    """

    text: str
    start: int
    display: str = ""
    meta: str = ""


#: Asked with ``(line, cursor)`` on every Tab; returns what may be inserted there.
Complete = Callable[[str, int], Sequence[Candidate]]


@runtime_checkable
class LineReader(Protocol):
    """What the REPL head needs from a terminal, and nothing more."""

    async def read(self, *, default: str = "") -> str | None:
        """Read one prompt line. ``None`` means EOF — the session is over."""
        ...

    async def ask(
        self, question: str, *, default: str = "", validate: Validate | None = None
    ) -> str | None:
        """Ask one question. ``None`` means the asker cancelled it."""
        ...

    def set_spinner(self, label: str | None) -> None:
        """Show *label* as the activity indicator, or clear it with ``None``."""
        ...

    def set_prefix(self, text: str) -> None:
        """Set the text shown before the prompt marker (status slots)."""
        ...

    def set_interrupt(self, handler: Interrupt | None) -> None:
        """Install what Ctrl+C means while a line is being read.

        The handler answers whether the press was handled. ``False`` — or no
        handler — ends the outstanding read the way EOF does, which is the idle
        row of docs/REPL-HEAD.md §5's state table; ``True`` leaves the read
        outstanding with its draft intact, which is the streaming row. A press
        inside :meth:`ask` never reaches the handler: it cancels the ask.
        """
        ...

    def set_reclaim(self, supplier: Reclaim | None) -> None:
        """Install what Up on an EMPTY line offers, ahead of the history it walks."""
        ...

    def set_completer(self, completer: Complete | None) -> None:
        """Install what Tab offers, or clear it with ``None``.

        Tab is the only completion key (docs/SLASH-COMMANDS.md §3), so nothing is
        offered while the reader is merely typing.
        """
        ...

    def set_draft(self, text: str) -> None:
        """Put *text* in the buffer — an outstanding read keeps reading it."""
        ...

    def draft(self) -> str:
        """Whatever is half-typed in the buffer right now."""
        ...


class MemoryReader:
    """A :class:`LineReader` that answers from lists — the suite's terminal.

    Exhausting ``lines`` is EOF, which is what makes a scripted run end the way a
    Ctrl+D does. Exhausting ``answers`` is NOT: a question the script did not
    anticipate is a broken test, so it raises rather than inventing an answer.

    A key press is scripted as the bytes a terminal sends for it: :data:`INTERRUPT`
    in ``lines`` is Ctrl+C and :data:`RECLAIM` is Up, and each behaves the way the
    real binding does — a handled press leaves the read outstanding, so the script
    continues to the next entry, and an unhandled Ctrl+C ends the read.

    Attributes:
        spinners: Every label :meth:`set_spinner` was given, in order, ``None``
            included, so a test can assert when the indicator started and stopped.
        prefixes: Every prefix :meth:`set_prefix` was given, in order.
        asked: Every question :meth:`ask` was given, in order.
        defaults: Every ``default`` :meth:`ask` was handed, in order — a real
            reader PRE-FILLS it, so a default its own validator rejects is an
            unbreakable re-ask loop this list is what makes visible.
        drafts: Every text :meth:`set_draft` put in the buffer, in order.
        completer: What Tab would offer, so a test can press it by calling it.
        completers: Every completer installed, ``None`` included — the teardown
            clears one, and a head that left one behind would complete against a
            session that is gone.
    """

    def __init__(self, lines: list[str] | None = None, answers: list[str] | None = None) -> None:
        """
        Args:
            lines: The prompt lines to hand back, in order.
            answers: The answers to hand back from :meth:`ask`, in order. An
                entry of ``None`` spelled as ``""`` is an empty answer;
                :data:`INTERRUPT` is a cancelled ask.
        """
        self._lines = list(lines or [])
        self._answers = list(answers or [])
        self.spinners: list[str | None] = []
        self.prefixes: list[str] = []
        self.asked: list[str] = []
        self.defaults: list[str] = []
        self.drafts: list[str] = []
        self.completer: Complete | None = None
        self.completers: list[Complete | None] = []
        self._draft = ""
        self._interrupt: Interrupt | None = None
        self._reclaim: Reclaim | None = None

    async def read(self, *, default: str = "") -> str | None:
        """Pop the next scripted line, acting on key presses on the way.

        Returns ``None`` once the script is spent, or when a Ctrl+C nobody handled
        ends the read — the two ways a real read ends without a line.
        """
        while self._lines:
            line = self._lines.pop(0)
            if line == INTERRUPT:
                if self._interrupt is None or not self._interrupt():
                    return None
                continue
            if line == RECLAIM:
                reclaimed = self._reclaim() if self._reclaim is not None else None
                if reclaimed is not None:
                    self.set_draft(reclaimed)
                continue
            return line
        return None

    async def ask(
        self, question: str, *, default: str = "", validate: Validate | None = None
    ) -> str | None:
        """Pop the next scripted answer, re-asking while *validate* rejects it."""
        self.asked.append(question)
        self.defaults.append(default)
        while True:
            if not self._answers:
                raise AssertionError(
                    f"MemoryReader ran out of answers at {question!r} — the script "
                    "did not anticipate this question, which is a fact about the "
                    "test, not something to guess an answer for"
                )
            answer = self._answers.pop(0)
            if answer == INTERRUPT:
                return None
            if validate is None:
                return answer
            complaint = validate(answer)
            if complaint is None:
                return answer

    def set_spinner(self, label: str | None) -> None:
        """Record the label."""
        self.spinners.append(label)

    def set_prefix(self, text: str) -> None:
        """Record the prefix."""
        self.prefixes.append(text)

    def set_interrupt(self, handler: Interrupt | None) -> None:
        """Hold what a scripted :data:`INTERRUPT` calls."""
        self._interrupt = handler

    def set_reclaim(self, supplier: Reclaim | None) -> None:
        """Hold what a scripted :data:`RECLAIM` calls."""
        self._reclaim = supplier

    def set_completer(self, completer: Complete | None) -> None:
        """Hold what Tab would offer; a test presses Tab by calling it."""
        self.completers.append(completer)
        self.completer = completer

    def set_draft(self, text: str) -> None:
        """Record the text and make it the draft."""
        self.drafts.append(text)
        self._draft = text

    def draft(self) -> str:
        """The scripted draft, which is empty unless a test set one."""
        return self._draft


class PromptToolkitReader:
    """The real terminal: one ``PromptSession`` for every line this head reads.

    One session, not one per call, is what lets a status slot or the spinner
    repaint without cancelling an outstanding read (``message`` and
    ``bottom_toolbar`` are callables) and what keeps :meth:`ask` on the same key
    bindings as :meth:`read`.

    ``prompt_toolkit`` appends each accepted line to its own history, so this
    class offers no ``history_add``: a head calling one would double-add.

    Every read runs under ``patch_stdout(raw=True)``, so anything printed while a prompt is
    open — a streamed block, a tool line, an extension's ``notify`` — lands ABOVE
    the prompt instead of through it. That is the reader's business and not each
    caller's: the head prints from a render subscription that knows nothing about
    whether a line is being typed (docs/REPL-HEAD.md §7).

    ``raw=True`` because the default proxy calls ``Output.write``, which replaces
    every ``\\x1b`` with ``?`` — so ``rich``'s own styling reached the terminal as
    literal ``?[2m`` text for the length of any read.

    Ctrl+C is a KEY here and not a signal — ``handle_sigint=False`` on every read,
    so the press reaches the head's state machine with the buffer intact instead
    of raising into whatever is awaiting (docs/REPL-HEAD.md §5).
    """

    def __init__(self, console: Any, *, history_path: Path | None = None) -> None:
        """
        Args:
            console: The ``rich`` console this head prints to; the spinner is
                driven through it while no prompt is outstanding.
            history_path: Where to persist input history. ``None`` keeps history
                in memory for the process's life.

        Raises:
            CLIError: When ``prompt_toolkit`` is not installed.
        """
        from tau_coding_agent.headless import CLIError

        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import Completer, Completion
            from prompt_toolkit.history import FileHistory, InMemoryHistory
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.patch_stdout import patch_stdout
        except ModuleNotFoundError as exc:
            raise CLIError(
                "--mode repl needs the 'repl' extra (prompt_toolkit is missing): "
                "pip install 'ffwf-tau-coding-agent[repl]'"
            ) from exc

        self._console = console
        self._patch_stdout = patch_stdout
        self._prefix = ""
        self._spinner: str | None = None
        self._status: Any = None
        self._draft = ""
        self._asking = False
        self._interrupt: Interrupt | None = None
        self._reclaim: Reclaim | None = None
        self._complete: Complete | None = None
        history: Any
        if history_path is None:
            history = InMemoryHistory()
        else:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(history_path))

        bindings = KeyBindings()

        @bindings.add("c-c")
        def _interrupt(event: Any) -> None:
            if self._asking or self._interrupt is None or not self._interrupt():
                event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

        @bindings.add("up")
        def _up(event: Any) -> None:
            buffer = event.current_buffer
            reclaimed = self._reclaim() if not buffer.text and self._reclaim else None
            if reclaimed is None:
                buffer.auto_up(count=event.arg)
                return
            buffer.text = reclaimed
            buffer.cursor_position = len(reclaimed)

        reader = self

        class _HeadCompleter(Completer):  # type: ignore[misc]
            """Adapts the head's candidates to what ``prompt_toolkit`` inserts."""

            def get_completions(self, document: Any, complete_event: Any) -> Any:
                # An ask is a form field, not a command line: the three vocabularies do not apply.
                if reader._complete is None or reader._asking:
                    return
                cursor = document.cursor_position
                for candidate in reader._complete(document.text, cursor):
                    yield Completion(
                        candidate.text,
                        start_position=candidate.start - cursor,
                        display=candidate.display or candidate.text,
                        display_meta=candidate.meta,
                    )

        self._message: Any = lambda: f"{self._prefix}› "
        self._session: Any = PromptSession(
            message=self._message,
            history=history,
            bottom_toolbar=lambda: self._spinner,
            refresh_interval=0.1,
            key_bindings=bindings,
            completer=_HeadCompleter(),
            complete_while_typing=False,
        )

    async def read(self, *, default: str = "") -> str | None:
        """Read one line. ``None`` on EOF, or on a Ctrl+C nobody handled.

        Args:
            default: Pre-filled into the buffer. Omitted, whatever
                :meth:`set_draft` left behind is used instead — that is how text
                reclaimed while no read was outstanding comes back.

        Returns:
            The line, or ``None`` when the read ended without one.
        """
        self._suspend_status()
        if not default:
            default, self._draft = self._draft, ""
        try:
            # prompt_async(message=) OVERWRITES the session's, so every read restores the marker.
            with self._patch_stdout(raw=True):
                answer: str = await self._session.prompt_async(
                    message=self._message, default=default, handle_sigint=False
                )
        except (EOFError, KeyboardInterrupt):
            return None
        return answer

    async def ask(
        self, question: str, *, default: str = "", validate: Validate | None = None
    ) -> str | None:
        """Ask *question* on the same session; ``None`` on EOF or Ctrl+C.

        Ctrl+C here cancels the ask and nothing else — it does not reach the
        head's interrupt handler, so a form raised mid-turn is dismissed without
        aborting the turn that raised it (docs/REPL-HEAD.md §5).

        Args:
            question: The prompt text, shown in place of the usual marker.
            default: Pre-filled into the buffer.
            validate: Returns a complaint to re-ask with, or ``None`` to accept.

        Returns:
            The typed answer, or ``None`` when the asker cancelled.
        """
        self._suspend_status()
        self._asking = True
        try:
            while True:
                try:
                    with self._patch_stdout(raw=True):
                        answer: str = await self._session.prompt_async(
                            message=f"{question} ", default=default, handle_sigint=False
                        )
                except (EOFError, KeyboardInterrupt):
                    return None
                if validate is None:
                    return answer
                complaint = validate(answer)
                if complaint is None:
                    return answer
                self._console.print(complaint)
        finally:
            self._asking = False

    def set_spinner(self, label: str | None) -> None:
        """Start, update or (with ``None``) stop the activity indicator.

        Two forms and never both at once: with a prompt open the label IS the
        bottom toolbar, repainted in place, and a ``rich`` status started under
        it would be a second thing driving the same terminal — which is the
        fight docs/REPL-HEAD.md §5 spiked and refused. With no prompt open the
        status is the only indicator there can be.
        """
        self._spinner = label
        if self._repaint():
            self._suspend_status()
            return
        if label is None:
            self._suspend_status()
            return
        if self._status is None:
            self._status = self._console.status(label)
            self._status.start()
        else:
            self._status.update(label)

    def set_prefix(self, text: str) -> None:
        """Set the prompt prefix; an outstanding read repaints and keeps its draft."""
        self._prefix = text
        self._repaint()

    def set_interrupt(self, handler: Interrupt | None) -> None:
        """Install what the ``c-c`` binding calls."""
        self._interrupt = handler

    def set_reclaim(self, supplier: Reclaim | None) -> None:
        """Install what the ``up`` binding offers before it walks history."""
        self._reclaim = supplier

    def set_completer(self, completer: Complete | None) -> None:
        """Install what Tab offers; the menu is built from it on every press."""
        self._complete = completer

    def set_draft(self, text: str) -> None:
        """Put *text* in the buffer, whether or not a read is outstanding.

        With one outstanding the buffer is written directly and repaints; with
        none, the text is held and becomes the next read's default. Held text is
        NOT also written to the live buffer, or the next read would show it twice.
        """
        if self._repaint(text):
            return
        self._draft = text

    def draft(self) -> str:
        """The buffer's current text, or the text held for the next read."""
        app = getattr(self._session, "app", None)
        if app is None or not app.is_running:
            return self._draft
        text: str = self._session.default_buffer.text
        return text

    def _repaint(self, text: str | None = None) -> bool:
        """Redraw an outstanding prompt, optionally replacing its text first.

        Args:
            text: What to put in the buffer, or ``None`` to leave it alone.

        Returns:
            Whether a prompt was outstanding to repaint.
        """
        app = getattr(self._session, "app", None)
        if app is None or not app.is_running:
            return False
        if text is not None:
            buffer = self._session.default_buffer
            buffer.text = text
            buffer.cursor_position = len(text)
        app.invalidate()
        return True

    def _suspend_status(self) -> None:
        """Stop the ``rich`` status so the prompt owns the terminal alone."""
        if self._status is not None:
            self._status.stop()
            self._status = None
