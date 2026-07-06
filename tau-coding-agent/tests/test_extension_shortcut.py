"""E10 §6 (S69) — ``register_shortcut`` binds a key to a command in a guarded chord.

An extension's ``api.register_shortcut(key, command)`` binds ``ctrl+e`` then ``key``
to dispatch ``command`` — the SAME ``run_extension_command`` path a typed ``/name``
and a panel action (S68) use. The namespace is *guarded*: extensions only ever bind
the chord tail (never a bare global key), so they cannot clobber a core binding
(``ctrl+c``/``ctrl+n``/…). And it is *palette-discoverable* — each shortcut is also
listed in the command palette (the always-reachable dispatch path).

Under test, driven through the REAL Parley app via ``App.run_test()`` / Pilot:

- :class:`ExtensionChordScreen` (the which-key menu) captures the tail key and
  dismisses with ``(command, args)`` — or ``None`` on escape / an unbound key.
- Pressing ``ctrl+e`` then the tail key on a REAL ``TauBackend`` with a loaded file
  extension dispatches its command (a marker file it writes proves it ran).
- The shortcut is listed in :meth:`Parley.get_system_commands` and the
  ``/extensions`` listing; with NO shortcut registered ``ctrl+e`` keeps its legacy
  meaning (edit the system prompt).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §6 S69.
"""

from __future__ import annotations

import pytest

from textual.app import App

from tau_agent_core.sdk import ExtensionInfo, LoadExtensionsResult
from tau_coding_agent.app import (
    ExtensionChordScreen,
    Parley,
    SystemPromptEditor,
)
from tau_coding_agent.backends import create_backend


# A file extension that registers a command whose handler writes a marker file
# capturing its args, AND a ctrl+e chord shortcut bound to that command. The marker
# proves the shortcut dispatched the real command (not that a key was merely stored).
_SHORTCUT_EXT = '''
import pathlib

MARKER = pathlib.Path({marker!r})


def register(api):
    def _greet(args, ctx):
        MARKER.write_text("ran:" + args)

    api.register_command("greet", {{"description": "greet the user", "handler": _greet}})
    api.register_shortcut("g", "greet", description="Greet")
'''


# ── ExtensionChordScreen (which-key menu) via a minimal modal harness ──────────


class _ModalHarness(App):
    """Minimal host that pushes one modal and records its dismissal value."""

    def __init__(self, modal) -> None:
        super().__init__()
        self._modal = modal
        self.result: object = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._store)

    def _store(self, value) -> None:
        self.result = value


_SHORTCUTS = [
    ("g", "greet", "", "Greet"),
    ("1", "abort_child", "c-1", "Abort child 1"),
]


async def test_chord_screen_matching_key_dispatches_command_and_args():
    harness = _ModalHarness(ExtensionChordScreen(_SHORTCUTS))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
    assert harness.result == ("abort_child", "c-1")


async def test_chord_screen_argless_key_dispatches_empty_args():
    harness = _ModalHarness(ExtensionChordScreen(_SHORTCUTS))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
    assert harness.result == ("greet", "")


async def test_chord_screen_escape_cancels():
    harness = _ModalHarness(ExtensionChordScreen(_SHORTCUTS))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert harness.result is None


async def test_chord_screen_unbound_key_cancels():
    """An unregistered tail key dismisses with None (no fabricated dispatch)."""
    harness = _ModalHarness(ExtensionChordScreen(_SHORTCUTS))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("z")
        await pilot.pause()
    assert harness.result is None


# ── Real Parley + TauBackend: the binding dispatches the command ──────────────


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A Parley wired to REAL TauBackends, with sandboxed session storage."""
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    monkeypatch.setattr("tau_coding_agent.app.create_backend", create_backend)
    # Isolate the module-global session-event listener list (see
    # test_app_extension_commands.py for the rationale — avoids a cross-test leak).
    monkeypatch.setattr(store, "_session_listeners", [])

    a = Parley()
    a.config = {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    return a


async def test_shortcut_binding_dispatches_command(app, tmp_path):
    """ctrl+e then the tail key runs the shortcut's command (the S69 requirement)."""
    marker = tmp_path / "greet_marker.txt"
    ext = tmp_path / "shortcut_ext.py"
    ext.write_text(_SHORTCUT_EXT.format(marker=str(marker)))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        # The shortcut reached the session registry (bound to THIS backend).
        assert app.current_backend.get_extension_shortcuts() == [("g", "greet", "", "Greet")]

        # ctrl+e is non-priority (like the binding it replaced), so it only reaches
        # the app binding when the message input is NOT focused (a focused TextArea
        # takes ctrl+e for line-end). Blur it so the chord leader fires.
        app.set_focus(None)
        await pilot.press("ctrl+e")
        await pilot.pause()
        # The which-key menu is up; the tail key dispatches greet through the backend.
        assert isinstance(app.screen, ExtensionChordScreen)
        await pilot.press("g")
        await pilot.pause()

        assert marker.read_text() == "ran:"


async def test_ctrl_e_edits_prompt_when_no_shortcuts(app, tmp_path):
    """With NO extension shortcut, ctrl+e keeps its legacy meaning (edit prompt)."""
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        assert app.current_backend.get_extension_shortcuts() == []

        await app.action_extension_chord()
        await pilot.pause()
        # Fell back to the system-prompt editor, not the chord menu.
        assert isinstance(app.screen, SystemPromptEditor)


# ── Palette / listing discoverability ─────────────────────────────────────────


async def test_shortcut_listed_in_command_palette(app, tmp_path):
    """Each shortcut is a palette entry that dispatches its command (discoverable)."""
    ext = tmp_path / "shortcut_ext.py"
    ext.write_text(_SHORTCUT_EXT.format(marker=str(tmp_path / "unused.txt")))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        titles = {cmd.title: cmd for cmd in app.get_system_commands(app.screen)}
        assert "ctrl+e g  →  /greet" in titles
        assert titles["ctrl+e g  →  /greet"].help == "Greet"


def test_extensions_listing_renders_shortcut_keys(monkeypatch):
    """``_format_extensions_listing`` renders each shortcut as ``ctrl+e <key>``."""
    result = LoadExtensionsResult()
    monkeypatch.setattr(
        "tau_coding_agent.app.summarize_extensions",
        lambda r: [
            ExtensionInfo(
                name="fleet",
                path="fleet.py",
                tools=[],
                commands=["greet"],
                shortcuts=["g", "1"],
                hooks=[],
            )
        ],
    )
    text = Parley._format_extensions_listing(result)
    assert "- shortcuts: ctrl+e g, ctrl+e 1" in text
