"""Enhanced InputBar — TextArea with @ file refs and !bash commands.

Implements InputBar — an input area with tab completion, history,
@ file references, ! bash commands, and Ctrl+Enter multiline support.

Reference: PHASE-4-SUBPHASE-3.md — Session Tree and Input Bar
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# InputSubmitted event
# ---------------------------------------------------------------------------


@dataclass
class InputSubmitted:
    """Event emitted when the input bar has content to submit.

    Attributes:
        text: The text submitted (without leading ! for bash)
        multiline: Whether this was a multiline submit (Ctrl+Enter)
        is_bash: Whether this is a bash command
        is_file_ref: Whether this is a file reference (@ prefix)
    """

    text: str
    multiline: bool = False
    is_bash: bool = False
    is_file_ref: bool = False


# ---------------------------------------------------------------------------
# InputBar — Enhanced input area
# ---------------------------------------------------------------------------


class InputBar:
    """Enhanced input area with @ file refs and !bash commands.

    This class handles:
    - Enter: submit text as user message (or bash command if starts with !)
    - Ctrl+Enter: multiline submit
    - Up/Down: history navigation
    - Tab: path completion
    - !: bash commands
    - !!: silent bash commands

    Attributes:
        _cwd: Current working directory for path resolution
        _history: List of submitted texts (history buffer)
        _history_index: Current index in history (-1 = end)
        _value: Current text value
        _on_submitted: Callback for submitted input
        _on_event: Callback for InputSubmitted events
    """

    def __init__(
        self,
        cwd: str | None = None,
        on_submitted: Callable[[str], None] | None = None,
        on_event: Callable[[InputSubmitted], None] | None = None,
    ) -> None:
        self._cwd = cwd or os.getcwd()
        self._history: list[str] = []
        self._history_index: int = -1
        self._value: str = ""
        self._on_submitted = on_submitted
        self._on_event = on_event

    # ------------------------------------------------------------------
    # Value property
    # ------------------------------------------------------------------

    @property
    def value(self) -> str:
        """Current text value of the input bar."""
        return self._value

    @value.setter
    def value(self, text: str) -> None:
        """Set the current text value."""
        self._value = text

    # ------------------------------------------------------------------
    # Key handlers (mimicking Textual key events)
    # ------------------------------------------------------------------

    def _handle_enter(self) -> None:
        """Handle Enter key — submit the current text."""
        text = self._value.strip()
        if not text:
            return

        if text.startswith("!!"):
            # Silent bash command
            self._emit_bash(text[2:], silent=True)
        elif text.startswith("!"):
            # Bash command
            self._emit_bash(text[1:])
        else:
            # Regular user message
            self._emit_message(text)

        self._value = ""
        self._history.append(text)
        self._history_index = -1

    def _handle_multiline_enter(self) -> None:
        """Handle Ctrl+Enter — multiline submit."""
        text = self._value.strip()
        if not text:
            return

        self._emit_message(text, multiline=True)
        self._value = ""
        self._history.append(text)
        self._history_index = -1

    def _handle_history_up(self) -> None:
        """Navigate up in input history."""
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._value = self._history[-self._history_index - 1]

    def _handle_history_down(self) -> None:
        """Navigate down in input history."""
        if self._history_index > 0:
            self._history_index -= 1
            self._value = self._history[-self._history_index - 1]
        else:
            self._history_index = -1
            self._value = ""

    def _handle_tab(self) -> None:
        """Tab completion for file paths."""
        text = self._value
        # Find the last word or @ reference
        match = re.search(r'(@?)([\w./\\\-]*)$', text)
        if match:
            prefix, partial = match.groups()
            if not partial:
                return

            cwd = self._cwd
            try:
                files = [
                    f.name
                    for f in Path(cwd).rglob("*")
                    if partial.lower() in f.name.lower()
                ][:10]
            except (OSError, ValueError):
                return

            if files:
                completion = files[0]
                start = match.start()
                new_text = text[:start] + prefix + completion
                self._value = new_text

    def _handle_backspace(self) -> None:
        """Handle backspace key.

        At position 0 with a leading !, removes the !.
        Otherwise, removes the character before the cursor (last char).
        """
        text = self._value
        if len(text) == 0:
            return
        if len(text) == 1 and text.startswith("!"):
            self._value = ""
        elif text.startswith("!"):
            # Remove the ! and move cursor
            self._value = text[1:]
        else:
            # Normal backspace — remove last character
            self._value = text[:-1]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_key(self, key: str) -> None:
        """Process a key press.

        Args:
            key: The key pressed (e.g. "enter", "tab", "up", "down", "backspace")
        """
        if key == "enter":
            self._handle_enter()
        elif key == "ctrl+enter":
            self._handle_multiline_enter()
        elif key == "up":
            self._handle_history_up()
        elif key == "down":
            self._handle_history_down()
        elif key == "tab":
            self._handle_tab()
        elif key == "backspace":
            self._handle_backspace()

    @property
    def history(self) -> list[str]:
        """Return the current history buffer."""
        return list(self._history)

    @property
    def history_index(self) -> int:
        """Return the current history index."""
        return self._history_index

    @property
    def cwd(self) -> str:
        """Return the current working directory."""
        return self._cwd

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_bash(self, command: str, silent: bool = False) -> None:
        """Emit a bash command event.

        Args:
            command: The bash command to execute (without leading !)
            silent: If True, suppress the leading !
        """
        if self._on_event:
            if silent:
                self._on_event(InputSubmitted(text=command, is_bash=True))
            else:
                self._on_event(InputSubmitted(text="!" + command, is_bash=True))

        if self._on_submitted:
            self._on_submitted("!command" if silent else "!" + command)

    def _emit_message(
        self, text: str, multiline: bool = False
    ) -> None:
        """Emit a user message event.

        Args:
            text: The message text
            multiline: Whether this is a multiline submission
        """
        # Check for file references
        is_file_ref = text.startswith("@")

        if self._on_event:
            self._on_event(InputSubmitted(
                text=text,
                multiline=multiline,
                is_bash=False,
                is_file_ref=is_file_ref,
            ))

        if self._on_submitted:
            self._on_submitted(text)

    def set_cwd(self, cwd: str) -> None:
        """Change the working directory for path completion."""
        self._cwd = cwd


# ---------------------------------------------------------------------------
# Textual InputBar widget (when Textual is available)
# ---------------------------------------------------------------------------

try:
    from textual.app import ComposeResult
    from textual.widgets import TextArea
    from textual.binding import Binding

    class InputBarWidget(TextArea):
        """Textual widget — TextArea with enhanced key handling.

        This is the full Textual widget implementation that wraps
        the InputBar logic with Textual's TextArea widget.
        """

        BINDINGS = [
            Binding("enter", "submit", "Submit"),
            Binding("ctrl+enter", "multiline_submit", "Multiline"),
            Binding("up", "history_up", "History Up"),
            Binding("down", "history_down", "History Down"),
        ]

        def __init__(
            self,
            cwd: str = "",
            id: str | None = "input-bar",
            **kwargs: Any,
        ) -> None:
            super().__init__(id=id, **kwargs)
            self._input_bar = InputBar(
                cwd=cwd,
                on_submitted=self._on_submitted,
            )

        @property
        def value(self) -> str:
            return self._input_bar.value

        @value.setter
        def value(self, text: str) -> None:
            self._input_bar.value = text
            self.cursor_position = len(text)

        def action_submit(self) -> None:
            self._input_bar.on_key("enter")

        def action_multiline_submit(self) -> None:
            self._input_bar.on_key("ctrl+enter")

        def action_history_up(self) -> None:
            self._input_bar.on_key("up")

        def action_history_down(self) -> None:
            self._input_bar.on_key("down")

        def on_key(self, event: Any) -> None:
            """Handle key events for the Textual widget."""
            key = getattr(event, "key", None)
            if key == "tab":
                self._input_bar.on_key("tab")
                event.prevent_default()
            elif key == "backspace":
                if self.cursor_column == 0 and self._input_bar.value.startswith("!"):
                    self._input_bar.on_key("backspace")
                    event.prevent_default()
            # Allow other keys to pass through to TextArea

        def _on_submitted(self, text: str) -> None:
            """Called when text is submitted — handled by parent."""
            pass

except ImportError:
    # Textual not available
    InputBarWidget = InputBar  # type: ignore[misc,assignment]
