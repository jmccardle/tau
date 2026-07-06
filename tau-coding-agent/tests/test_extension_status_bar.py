"""E10 §6 (S67) — ``ctx.ui.set_status`` paints keyed slots in the footer strip.

Driven through the REAL Parley app (``App.run_test()`` / Pilot), matching
``test_extension_notify_and_veto`` (S33): the ``ExtensionStatusBar`` is composed
into the live layout, and the delegate an extension's ``ctx.ui.set_status`` reaches
(``_ExtensionUIDelegate.set_status`` → ``Parley.set_extension_status``) updates one
keyed slot in place. Slot semantics under test: first-seen order, in-place update
on a re-call, clear-on-``None``, and the strip collapsing to zero rows when empty.

The headless json-record / stderr half lives in
``tau-agent-core/tests/test_extension_types.py`` (TestExtensionUISetStatus).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §6 S67.
"""

from __future__ import annotations

import pytest

from tau_coding_agent.app import ExtensionStatusBar, Parley, _ExtensionUIDelegate


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A bare Parley with sandboxed config dir; no backend needed for the strip."""
    monkeypatch.setattr("tau_coding_agent.app.TAU_DIR", tmp_path)
    a = Parley()
    a.config = {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    # No extension loading — the strip is exercised directly, not via a demo.
    a._extension_paths = []
    a._discover_extensions = False
    return a


def _line(bar: ExtensionStatusBar) -> str:
    return str(bar.render())


async def test_strip_hidden_until_a_slot_is_set(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(ExtensionStatusBar)
        # Composed into the real layout, but collapsed (zero rows) with no slots.
        assert bar.display is False
        assert _line(bar) == ""


async def test_set_status_shows_and_updates_slot_in_place(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(ExtensionStatusBar)

        app.set_extension_status("budget", "$1.42/2.00")
        await pilot.pause()
        assert bar.display is True
        assert _line(bar) == "$1.42/2.00"

        # Re-calling the SAME key updates that slot in place (ambient live state) —
        # one slot, not two.
        app.set_extension_status("budget", "$1.90/2.00")
        await pilot.pause()
        assert list(bar._slots) == ["budget"]
        assert _line(bar) == "$1.90/2.00"


async def test_multiple_keys_render_in_first_seen_order(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(ExtensionStatusBar)

        app.set_extension_status("budget", "$1.42/2.00")
        app.set_extension_status("turn", "Turn 3")
        await pilot.pause()
        assert list(bar._slots) == ["budget", "turn"]
        assert _line(bar) == f"$1.42/2.00{ExtensionStatusBar._SEPARATOR}Turn 3"

        # Updating the first key keeps its original position.
        app.set_extension_status("budget", "$2.00/2.00")
        await pilot.pause()
        assert _line(bar) == f"$2.00/2.00{ExtensionStatusBar._SEPARATOR}Turn 3"


async def test_clear_removes_slot_and_collapses_when_empty(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(ExtensionStatusBar)

        app.set_extension_status("budget", "$1.42/2.00")
        app.set_extension_status("turn", "Turn 3")
        await pilot.pause()

        # text=None clears just that slot; the strip stays visible for the rest.
        app.set_extension_status("budget", None)
        await pilot.pause()
        assert list(bar._slots) == ["turn"]
        assert bar.display is True
        assert _line(bar) == "Turn 3"

        # Clearing the last slot collapses the strip back to zero rows.
        app.set_extension_status("turn", None)
        await pilot.pause()
        assert bar._slots == {}
        assert bar.display is False
        assert _line(bar) == ""


async def test_delegate_routes_set_status_to_the_strip(app) -> None:
    # End-to-end: what a loaded extension's ``ctx.ui.set_status(...)`` awaits —
    # the delegate forwards to Parley.set_extension_status → the live strip.
    async with app.run_test() as pilot:
        await pilot.pause()
        delegate = _ExtensionUIDelegate(app)
        delegate.set_status("model", "🤖 gpt-4o")
        await pilot.pause()
        bar = app.query_one(ExtensionStatusBar)
        assert bar.display is True
        assert _line(bar) == "🤖 gpt-4o"

        delegate.set_status("model", None)
        await pilot.pause()
        assert bar.display is False
