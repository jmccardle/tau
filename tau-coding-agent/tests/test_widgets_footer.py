"""Tests for FooterData widget data contract.

Reference: PHASE-4-SUBPHASE-0.md — FooterData Contract
"""

import dataclasses
import pytest

from tau_coding_agent.widgets.footer import FooterData


class TestFooterDataImport:
    """Test that FooterData is importable."""

    def test_footer_data_is_importable(self):
        """FooterData must be importable from widgets.footer."""
        from tau_coding_agent.widgets.footer import FooterData as F
        assert F is not None

    def test_footer_data_in_widgets_init(self):
        """FooterData must be re-exported from widgets.__init__."""
        from tau_coding_agent.widgets import FooterData as F
        assert F is FooterData


class TestFooterDataIsDataclass:
    """Test that FooterData is a proper dataclass."""

    def test_is_dataclass(self):
        """FooterData must be a dataclass."""
        assert dataclasses.is_dataclass(FooterData)

    def test_has_all_required_fields(self):
        """FooterData must have model field."""
        field_names = {f.name for f in dataclasses.fields(FooterData)}
        assert "model" in field_names

    def test_has_all_optional_fields(self):
        """FooterData must have tokens, context_percent, thinking_level, session_name."""
        field_names = {f.name for f in dataclasses.fields(FooterData)}
        assert "tokens" in field_names
        assert "context_percent" in field_names
        assert "thinking_level" in field_names
        assert "session_name" in field_names


class TestFooterDataConstruction:
    """Test FooterData construction and defaults."""

    def test_minimal_construction(self):
        """FooterData can be constructed with just model."""
        fd = FooterData(model="gpt-4")
        assert fd.model == "gpt-4"

    def test_defaults(self):
        """FooterData optional fields have correct defaults."""
        fd = FooterData(model="gpt-4")
        assert fd.tokens is None
        assert fd.context_percent is None
        assert fd.thinking_level == "off"
        assert fd.session_name is None

    def test_full_construction(self):
        """FooterData accepts all fields."""
        fd = FooterData(
            model="gpt-4",
            tokens=1500,
            context_percent=0.35,
            thinking_level="high",
            session_name="project-alpha",
        )
        assert fd.model == "gpt-4"
        assert fd.tokens == 1500
        assert fd.context_percent == 0.35
        assert fd.thinking_level == "high"
        assert fd.session_name == "project-alpha"

    def test_model_is_required(self):
        """FooterData requires model (no default)."""
        with pytest.raises(TypeError):
            FooterData()  # type: ignore  # Missing required 'model' argument

    def test_tokens_accepts_zero(self):
        """tokens accepts 0."""
        fd = FooterData(model="gpt-4", tokens=0)
        assert fd.tokens == 0

    def test_tokens_accepts_large_value(self):
        """tokens accepts large values."""
        fd = FooterData(model="gpt-4", tokens=999999999)
        assert fd.tokens == 999999999

    def test_context_percent_accepts_zero(self):
        """context_percent accepts 0.0."""
        fd = FooterData(model="gpt-4", context_percent=0.0)
        assert fd.context_percent == 0.0

    def test_context_percent_accepts_one(self):
        """context_percent accepts 1.0 (100% used)."""
        fd = FooterData(model="gpt-4", context_percent=1.0)
        assert fd.context_percent == 1.0

    def test_context_percent_accepts_fraction(self):
        """context_percent accepts fractional values."""
        fd = FooterData(model="gpt-4", context_percent=0.75)
        assert fd.context_percent == 0.75

    def test_thinking_level_default(self):
        """thinking_level defaults to 'off'."""
        fd = FooterData(model="gpt-4")
        assert fd.thinking_level == "off"

    def test_thinking_level_variants(self):
        """thinking_level accepts various string values."""
        for level in ["off", "low", "high", "max"]:
            fd = FooterData(model="gpt-4", thinking_level=level)
            assert fd.thinking_level == level

    def test_session_name_none_by_default(self):
        """session_name defaults to None."""
        fd = FooterData(model="gpt-4")
        assert fd.session_name is None

    def test_session_name_accepts_string(self):
        """session_name accepts a string."""
        fd = FooterData(model="gpt-4", session_name="my-session")
        assert fd.session_name == "my-session"


class TestFooterDataFromSessionState:
    """Test that FooterData maps correctly from session state."""

    def test_from_model_config(self):
        """FooterData.model maps from Model.id."""
        # Simulating mapping from Model configuration
        fd = FooterData(model="gpt-4-turbo-2024-04-09")
        assert fd.model == "gpt-4-turbo-2024-04-09"

    def test_from_usage(self):
        """FooterData.tokens maps from Usage.total_tokens."""
        fd = FooterData(
            model="gpt-4",
            tokens=42,
        )
        assert fd.tokens == 42

    def test_from_context_window(self):
        """FooterData.context_percent maps from tokens/context_window."""
        # Simulating: 75000 / 128000 = 0.586
        fd = FooterData(
            model="gpt-4",
            tokens=75000,
            context_percent=75000 / 128000,
        )
        assert fd.context_percent is not None
        assert 0.0 <= fd.context_percent <= 1.0

    def test_from_session_info(self):
        """FooterData.session_name maps from SessionInfo.name."""
        fd = FooterData(
            model="gpt-4",
            session_name="debug-session-42",
        )
        assert fd.session_name == "debug-session-42"
