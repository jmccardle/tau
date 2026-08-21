"""W4/G2 — constraint verification: τ never trusts a constraint blindly.

The failure mode this exists for is real and observed (GRAMMAR_DECODING_RECON.md:36):
llguidance can die mid-generation, log the error **server-side**, and let generation
continue **unconstrained**. The client sees a 200 and plausible text. An unconstrained
result returned as a constrained one is fabricated data — so we raise.

The provider verifies at the final-message choke point, which covers streaming and
complete_simple alike: no constrained result is ever returned unverified.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from test_reasoning_effort import _StreamCM, _model

from tau_llm import grammar
from tau_llm.client import complete_simple
from tau_llm.constraints import ConstraintViolation, DecodeConstraints
from tau_llm.types import TextContent, UserMessage


def _response_with(text: str) -> MagicMock:
    """A 200 SSE response whose content is exactly ``text``.

    The shared harness hardcodes its text; we need to control it to simulate a server
    that accepted the grammar and then dropped it.
    """
    chunks = [
        {"id": "c1", "model": "m", "choices": [{"index": 0, "delta": {"content": text}}]},
        {
            "id": "c1",
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    body = "\n".join(["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"])
    response = MagicMock()
    response.status_code = 200
    response.text = body
    response.headers = {}

    async def _aiter():
        for line in body.split("\n"):
            yield line

    response.aiter_lines = _aiter
    return response


def _patch_client(monkeypatch, *, text: str) -> None:
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def post(self, *a, **kw):
            return _response_with(text)

        def stream(self, *a, **kw):
            return _StreamCM(_response_with(text))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    monkeypatch.setattr("tau_llm.providers.openai.httpx.AsyncClient", _Client)


def _run_stream_result(model, options):
    """Drive a completion through the real client and return the final AssistantMessage.

    Uses ``complete_simple`` (i.e. ``stream.result()``), which is where a provider-side
    exception is re-raised with its type intact.
    """

    async def _go():
        return await complete_simple(
            model,
            {"messages": [UserMessage(content=[TextContent(text="hi")], timestamp=0)]},
            {"api_key": "sk-test", **(options or {})},
        )

    return asyncio.run(_go())


class TestVerifyOutputUnit:
    def test_choices_membership_passes(self):
        DecodeConstraints(choices=["include", "exclude"]).verify_output("include")

    def test_choices_violation_raises_and_carries_output(self):
        """The silent-death signature: output outside the alternative set."""
        c = DecodeConstraints(choices=["include", "exclude"])
        with pytest.raises(ConstraintViolation) as exc:
            c.verify_output("Certainly! I think this should be included.")

        assert exc.value.output == "Certainly! I think this should be included."
        assert "did not hold" in str(exc.value)

    def test_json_schema_parse_passes(self):
        DecodeConstraints(json_schema={"type": "object"}).verify_output('{"verdict": "include"}')

    def test_json_schema_unparseable_raises(self):
        c = DecodeConstraints(json_schema={"type": "object"})
        with pytest.raises(ConstraintViolation, match="not valid JSON"):
            c.verify_output("Sure! Here is the JSON you asked for:")

    def test_an_unverifiable_raw_grammar_is_refused_at_construction(self):
        """The old behaviour accepted this and checked only that the output was
        non-empty — which let a grammar restricted to include|exclude come back as
        `{"verdict": "no"}` and PASS (reproduced live). τ will not pretend."""
        with pytest.raises(ValidationError, match="needs a way to verify its output"):
            DecodeConstraints(grammar='start: "x"')

    def test_a_helper_built_grammar_carries_its_own_checker(self):
        """grammar.choice(...) and choices=[...] compile to the SAME wire grammar, so
        they must verify the same way. Before, the first was not checked at all."""
        c = DecodeConstraints(grammar=grammar.choice("include", "exclude"))

        c.verify_output("include")
        with pytest.raises(ConstraintViolation, match="does not satisfy the grammar"):
            c.verify_output('{"verdict": "no"}')

    def test_grammar_whitespace_is_significant_not_stripped(self):
        """fixed("yes ") really does force the trailing space (verified live), so the
        checker must compare exactly — stripping would make a CORRECT output fail."""
        c = DecodeConstraints(grammar=grammar.fixed("yes "))

        c.verify_output("yes ")
        with pytest.raises(ConstraintViolation):
            c.verify_output("yes")

    def test_a_sequence_grammar_checks_the_concatenation(self):
        c = DecodeConstraints(grammar=grammar.sequence("verdict: ", "include"))

        c.verify_output("verdict: include")
        with pytest.raises(ConstraintViolation):
            c.verify_output("verdict:include")

    def test_a_regex_grammar_checks_by_fullmatch(self):
        c = DecodeConstraints(grammar=grammar.regex("[0-9]{3}"))

        c.verify_output("160")
        with pytest.raises(ConstraintViolation):
            c.verify_output("16")  # a PREFIX must not pass — fullmatch, not match

    def test_caller_supplied_verify_wins(self):
        c = DecodeConstraints(grammar="start: /[0-9]+/", verify=lambda t: t.isdigit())
        c.verify_output("12345")
        with pytest.raises(ConstraintViolation, match="verify\\(\\) rejected"):
            c.verify_output("not-a-number")

    def test_verify_can_opt_out_explicitly(self):
        """The documented escape hatch — explicit, not accidental."""
        DecodeConstraints(grammar="start: x", verify=lambda _: True).verify_output("")

    def test_no_constraint_verifies_trivially(self):
        DecodeConstraints(tool_choice="none").verify_output("anything")


class TestProviderEnforcesVerification:
    def test_a_server_that_dropped_the_constraint_raises(self, monkeypatch):
        """Simulates the observed failure: server returns 200 with unconstrained prose."""
        _patch_client(monkeypatch, text="Certainly! I would include this document.")

        with pytest.raises(ConstraintViolation, match="is not one of"):
            _run_stream_result(
                _model(grammar_dialect="llguidance"),
                {"constraints": DecodeConstraints(choices=["include", "exclude"])},
            )

    def test_a_constraint_that_held_passes_through(self, monkeypatch):
        _patch_client(monkeypatch, text="include")

        msg = _run_stream_result(
            _model(grammar_dialect="llguidance"),
            {"constraints": DecodeConstraints(choices=["include", "exclude"])},
        )

        assert msg.content[0].text == "include"

    def test_unconstrained_calls_are_untouched(self, monkeypatch):
        _patch_client(monkeypatch, text="free prose, no constraint")

        msg = _run_stream_result(_model(), None)

        assert msg.content[0].text == "free prose, no constraint"
