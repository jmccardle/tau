"""Tests for ParleyApp stub and AppLayout.

Reference: PHASE-4-SUBPHASE-0.md — ParleyApp stub contract
"""

import pytest

from tau_coding_agent.app import ParleyApp, AppLayout


class TestParleyAppImport:
    """Test that ParleyApp is importable."""

    def test_parley_app_is_importable(self):
        """ParleyApp must be importable from tau_coding_agent.app."""
        from tau_coding_agent.app import ParleyApp as P
        assert P is not None

    def test_app_layout_is_importable(self):
        """AppLayout must be importable from tau_coding_agent.app."""
        from tau_coding_agent.app import AppLayout as A
        assert A is not None


class TestAppLayout:
    """Tests for AppLayout dataclass."""

    def test_defaults(self):
        """AppLayout has correct defaults."""
        layout = AppLayout()
        assert layout.width == 0
        assert layout.height == 0
        assert layout.theme == "default"

    def test_custom_values(self):
        """AppLayout accepts custom values."""
        layout = AppLayout(width=120, height=40, theme="dark")
        assert layout.width == 120
        assert layout.height == 40
        assert layout.theme == "dark"

    def test_is_dataclass(self):
        """AppLayout is a dataclass."""
        import dataclasses
        assert dataclasses.is_dataclass(AppLayout)


class TestParleyAppConstruction:
    """Tests for ParleyApp construction."""

    def test_minimal_construction(self):
        """ParleyApp can be constructed with no arguments."""
        app = ParleyApp()
        assert app.session is None
        assert app.ready is False
        assert app.layout is not None

    def test_construction_with_session(self):
        """ParleyApp can be constructed with a session."""
        session_mock = object()
        app = ParleyApp(session=session_mock)
        assert app.session is session_mock
        assert app.ready is False

    def test_construction_with_layout(self):
        """ParleyApp can be constructed with a layout."""
        layout = AppLayout(width=100, height=30, theme="dark")
        app = ParleyApp(layout=layout)
        assert app.layout == layout
        assert app.layout.width == 100

    def test_construction_with_both(self):
        """ParleyApp can be constructed with session and layout."""
        session_mock = object()
        layout = AppLayout(theme="ocean")
        app = ParleyApp(session=session_mock, layout=layout)
        assert app.session is session_mock
        assert app.layout.theme == "ocean"

    def test_ready_is_false_by_default(self):
        """ParleyApp.ready is False by default."""
        app = ParleyApp()
        assert app.ready is False


class TestParleyAppMethods:
    """Tests for ParleyApp methods."""

    def test_start_sets_ready(self):
        """start() sets ready to True."""
        app = ParleyApp()
        assert app.ready is False
        # start() is async
        import asyncio
        asyncio.run(app.start())
        assert app.ready is True

    def test_stop_sets_ready(self):
        """stop() sets ready to False."""
        app = ParleyApp()
        asyncio.run(app.start())
        assert app.ready is True
        asyncio.run(app.stop())
        assert app.ready is False

    def test_stop_clears_session(self):
        """stop() clears the session reference."""
        session_mock = object()
        app = ParleyApp(session=session_mock)
        asyncio.run(app.stop())
        assert app.session is None

    def test_start_stop_lifecycle(self):
        """Full start/stop lifecycle works."""
        app = ParleyApp()
        assert not app.ready
        asyncio.run(app.start())
        assert app.ready
        asyncio.run(app.stop())
        assert not app.ready


# Import asyncio at module level for use in async tests
import asyncio
