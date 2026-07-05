"""E10 §6 (S68) — ``ctx.ui.panel`` mounts keyed panels + dispatches actions.

Driven through the REAL Parley app (``App.run_test()`` / Pilot), matching
``test_extension_status_bar`` (S67): the ``ExtensionPanelHost`` is composed into the
live layout, and the delegate an extension's ``ctx.ui.panel`` reaches
(``_ExtensionUIDelegate.panel`` → ``Parley.set_extension_panel``) mounts / updates /
removes a keyed :class:`ExtensionPanel`. Under test: first render (title/body/action
buttons), live in-place update (panel identity + sibling order stable), clear-on-
``None`` (host collapses when empty), and an action button dispatching its command
back through the backend (the panel→extension loop).

The headless json-record / stderr half + spec validation live in
``tau-agent-core/tests/test_extension_types.py`` (TestExtensionUIPanel /
TestValidatePanelSpec).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §6 S68.
"""

from __future__ import annotations

import pytest

from tau_agent_core.agent_session import ExtensionCommandResult
from tau_coding_agent.app import (
    ExtensionPanel,
    ExtensionPanelHost,
    Parley,
    _ExtensionUIDelegate,
    _PanelActionButton,
    render_panel_body,
)


_FLEET_SPEC = {
    "title": "Fleet",
    "table": {
        "columns": ["child", "status", "cost"],
        "rows": [["c-1", "running", "$0.10"], ["c-2", "done", "$0.42"]],
    },
    "actions": [
        {"label": "Abort c-1", "command": "abort_child", "args": "c-1"},
        {"label": "Refresh", "command": "refresh_fleet"},
    ],
}


class _FakeBackend:
    """A backend stub exposing just the panel-action seam (run_extension_command)."""

    def __init__(self, result: ExtensionCommandResult) -> None:
        self._result = result
        self.calls: list[tuple[str, str]] = []

    async def run_extension_command(self, name: str, args: str = "") -> ExtensionCommandResult:
        self.calls.append((name, args))
        return self._result


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A bare Parley with sandboxed config dir; no real backend needed for the host."""
    monkeypatch.setattr("tau_coding_agent.app.TAU_DIR", tmp_path)
    a = Parley()
    a.config = {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    a._extension_paths = []
    a._discover_extensions = False
    return a


# ── render_panel_body: pure body → text (no widget access) ────────────────────


def test_render_body_text():
    assert render_panel_body({"kind": "text", "text": "2 children running"}) == "2 children running"


def test_render_body_list():
    assert render_panel_body({"kind": "list", "items": ["one", "two"]}) == "• one\n• two"


def test_render_body_table_is_a_padded_grid():
    body = {
        "kind": "table",
        "columns": ["child", "status"],
        "rows": [["c-1", "running"], ["c-2", "done"]],
    }
    text = render_panel_body(body)
    lines = text.split("\n")
    # header, rule, then one line per row.
    assert lines[0] == "child  status "
    assert set(lines[1]) == {"─", " "}
    assert lines[2] == "c-1    running"
    assert lines[3] == "c-2    done   "


# ── TUI: host mount / update / clear ──────────────────────────────────────────


async def test_host_hidden_until_a_panel_is_set(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        host = app.query_one(ExtensionPanelHost)
        assert host.display is False
        assert host._panels == {}


async def test_set_panel_mounts_title_body_and_action_buttons(app):
    from tau_agent_core.extension_types import validate_panel_spec

    async with app.run_test() as pilot:
        await pilot.pause()
        app.set_extension_panel("fleet", validate_panel_spec(_FLEET_SPEC))
        await pilot.pause()
        host = app.query_one(ExtensionPanelHost)
        assert host.display is True
        assert list(host._panels) == ["fleet"]
        panel = host._panels["fleet"]
        # One action button per declared action, carrying its command/args.
        buttons = list(panel.query(_PanelActionButton))
        assert [(b.command, b.args) for b in buttons] == [
            ("abort_child", "c-1"),
            ("refresh_fleet", ""),
        ]


async def test_live_update_keeps_panel_identity_and_order(app):
    from tau_agent_core.extension_types import validate_panel_spec

    async with app.run_test() as pilot:
        await pilot.pause()
        app.set_extension_panel("fleet", validate_panel_spec(_FLEET_SPEC))
        app.set_extension_panel("budget", validate_panel_spec({"title": "Budget", "text": "$0/5"}))
        await pilot.pause()
        host = app.query_one(ExtensionPanelHost)
        assert list(host._panels) == ["fleet", "budget"]
        fleet_before = host._panels["fleet"]

        # Re-call the SAME key with a new spec → update in place (identity preserved),
        # sibling order unchanged.
        updated = validate_panel_spec({"title": "Fleet", "text": "all done"})
        app.set_extension_panel("fleet", updated)
        await pilot.pause()
        assert list(host._panels) == ["fleet", "budget"]
        assert host._panels["fleet"] is fleet_before  # same widget, updated content
        body = fleet_before.query_one(".ext-panel-body")
        assert "all done" in str(body.render())


async def test_clear_removes_panel_and_collapses_when_empty(app):
    from tau_agent_core.extension_types import validate_panel_spec

    async with app.run_test() as pilot:
        await pilot.pause()
        app.set_extension_panel("fleet", validate_panel_spec({"text": "x"}))
        app.set_extension_panel("budget", validate_panel_spec({"text": "y"}))
        await pilot.pause()
        host = app.query_one(ExtensionPanelHost)

        # spec=None clears just that panel; the host stays visible for the rest.
        app.set_extension_panel("fleet", None)
        await pilot.pause()
        assert list(host._panels) == ["budget"]
        assert host.display is True

        # Clearing the last panel collapses the host back to zero space.
        app.set_extension_panel("budget", None)
        await pilot.pause()
        assert host._panels == {}
        assert host.display is False


async def test_delegate_routes_panel_to_the_host(app):
    from tau_agent_core.extension_types import validate_panel_spec

    async with app.run_test() as pilot:
        await pilot.pause()
        delegate = _ExtensionUIDelegate(app)
        delegate.panel("fleet", validate_panel_spec({"title": "Fleet", "list": ["c-1", "c-2"]}))
        await pilot.pause()
        host = app.query_one(ExtensionPanelHost)
        assert list(host._panels) == ["fleet"]
        delegate.panel("fleet", None)
        await pilot.pause()
        assert host.display is False


# ── TUI: action dispatch (panel → extension command) ──────────────────────────


async def test_pressing_action_dispatches_command_through_backend(app):
    from tau_agent_core.extension_types import validate_panel_spec

    backend = _FakeBackend(ExtensionCommandResult(handled=True, output="aborted c-1"))
    app.current_backend = backend
    async with app.run_test() as pilot:
        await pilot.pause()
        app.set_extension_panel("fleet", validate_panel_spec(_FLEET_SPEC))
        await pilot.pause()
        button = next(
            b for b in app.query(_PanelActionButton) if b.command == "abort_child"
        )
        button.press()
        await pilot.pause()
        # The action dispatched its command + args back into the extension.
        assert backend.calls == [("abort_child", "c-1")]


async def test_unknown_action_command_surfaces_error(app):
    from tau_agent_core.extension_types import validate_panel_spec

    # handled=False → unknown command; the app must SURFACE it, not silently no-op.
    backend = _FakeBackend(ExtensionCommandResult(handled=False))
    app.current_backend = backend
    notices: list[tuple[str, str]] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda msg, severity="information", **kw: notices.append((msg, severity))  # type: ignore[assignment]
        app.set_extension_panel(
            "fleet",
            validate_panel_spec({"text": "x", "actions": [{"label": "Go", "command": "gone"}]}),
        )
        await pilot.pause()
        button = next(iter(app.query(_PanelActionButton)))
        button.press()
        await pilot.pause()
        assert backend.calls == [("gone", "")]
        assert any("unknown command /gone" in msg for msg, _ in notices)
