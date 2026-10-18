"""One rule for narrowing and naming a single choice, shared by every head.

Reference: docs/MODEL-PICKER.md §4, §6.
"""

from __future__ import annotations

import pytest

from tau_agent_core.extension_types import filter_choices, match_choice

OPTIONS = ["claude-sonnet", "claude-opus", "local-llm", "gpt-4o"]


class TestFilterChoices:
    def test_a_substring_narrows_in_the_original_order(self):
        assert filter_choices(OPTIONS, "CLAUDE") == ["claude-sonnet", "claude-opus"]

    def test_a_blank_query_leaves_everything(self):
        assert filter_choices(OPTIONS, "  ") == OPTIONS

    def test_nothing_matching_is_an_empty_list(self):
        assert filter_choices(OPTIONS, "zzz") == []


class TestMatchChoice:
    def test_a_number_is_the_printed_position(self):
        assert match_choice(OPTIONS, "3") == "local-llm"

    def test_a_number_out_of_range_is_refused_not_read_as_a_name(self):
        with pytest.raises(ValueError, match="from 1 to 2"):
            match_choice(["qwen3-30b", "b"], "30")

    def test_an_exact_name_wins_over_being_a_prefix(self):
        assert match_choice(["gpt-4", "gpt-4o"], "GPT-4") == "gpt-4"

    def test_a_unique_prefix_names_its_option(self):
        assert match_choice(OPTIONS, "loc") == "local-llm"

    def test_a_unique_substring_names_its_option(self):
        assert match_choice(OPTIONS, "opus") == "claude-opus"

    def test_an_ambiguous_part_names_the_candidates(self):
        with pytest.raises(ValueError, match=r"matches 2 options \(claude-sonnet, claude-opus\)"):
            match_choice(OPTIONS, "claude")

    def test_a_word_naming_nothing_is_refused(self):
        with pytest.raises(ValueError, match="matches no option"):
            match_choice(OPTIONS, "burgundy")
