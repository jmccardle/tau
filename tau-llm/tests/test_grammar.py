"""W3 — llguidance grammar composition, especially escaping.

A mis-escaped grammar is not a loud failure: the server accepts a grammar that means
something *other* than what the caller asked for, and forces a plausible-looking
generation. So escaping gets exhaustive coverage.

Reference: docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §4.
"""

from __future__ import annotations

import pytest

from tau_llm import grammar


class TestEscape:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("plain", "plain"),
            ('say "hi"', 'say \\"hi\\"'),
            ("back\\slash", "back\\\\slash"),
            ("line\nbreak", "line\\nbreak"),
            ("tab\there", "tab\\there"),
            ("carriage\rreturn", "carriage\\rreturn"),
            # The nasty one: a backslash already followed by a quote must not be
            # double-substituted into something that re-opens the literal.
            ('esc\\"seq', 'esc\\\\\\"seq'),
        ],
    )
    def test_escapes(self, raw, expected):
        assert grammar.escape(raw) == expected

    def test_literal_wraps_and_escapes(self):
        assert grammar.literal('a "b" c') == '"a \\"b\\" c"'

    def test_a_quote_cannot_break_out_of_the_literal(self):
        """The whole point: user text must never terminate the grammar string."""
        out = grammar.literal('"; start: "pwned')
        # Exactly two unescaped quotes — the ones we added.
        assert out.startswith('"') and out.endswith('"')
        assert out.count('"') - out.count('\\"') == 2


class TestChoice:
    def test_basic(self):
        assert grammar.choice("include", "exclude") == 'start: "include" | "exclude"'

    def test_escapes_alternatives(self):
        assert grammar.choice('a"b', "c\\d") == 'start: "a\\"b" | "c\\\\d"'

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="at least one alternative"):
            grammar.choice()

    def test_duplicates_raise(self):
        """An ambiguous grammar is almost always a caller bug."""
        with pytest.raises(ValueError, match="must be unique"):
            grammar.choice("a", "b", "a")

    def test_single_alternative_is_valid(self):
        assert grammar.choice("only") == 'start: "only"'


class TestOtherHelpers:
    def test_fixed(self):
        assert grammar.fixed("verdict") == 'start: "verdict"'

    def test_sequence(self):
        assert grammar.sequence("verdict: ", "include") == 'start: "verdict: " "include"'

    def test_regex(self):
        assert grammar.regex(r"\d{4}") == "start: /\\d{4}/"

    def test_regex_empty_raises(self):
        with pytest.raises(ValueError, match="non-empty pattern"):
            grammar.regex("")

    def test_sequence_empty_raises(self):
        with pytest.raises(ValueError, match="at least one part"):
            grammar.sequence()


class TestHeader:
    def test_adds_header(self):
        assert grammar.with_header('start: "x"') == '%llguidance {}\nstart: "x"'

    def test_never_doubles(self):
        already = '%llguidance {}\nstart: "x"'
        assert grammar.with_header(already) == already

    def test_tolerates_leading_whitespace_when_detecting(self):
        already = '  %llguidance {}\nstart: "x"'
        assert grammar.with_header(already) == already

    def test_helpers_emit_no_header(self):
        """The provider owns prefixing — it alone knows the model's dialect."""
        for text in (grammar.choice("a", "b"), grammar.fixed("x"), grammar.regex("y")):
            assert "%llguidance" not in text
