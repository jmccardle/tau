"""W3/G1 — DecodeConstraints: shape rules, wire mapping, and the two gates.

The gates are not speculative policy — both were verified live against llama.cpp
master (CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §0.1):

- ``grammar`` + tools  → server 400s.
- ``json_schema`` + tools → server returns **200 and silently stops calling tools**,
  fabricating a schema-shaped answer. τ is the only defence, so the raise covers both.

Payloads are asserted against the real provider via the existing ``_CapturingClient``
harness (test_reasoning_effort.py) — no second harness.
"""

from __future__ import annotations

import pytest
from test_reasoning_effort import _model, _patch_client, _run

from tau_ai import grammar as grammar_mod
from tau_ai.constraints import DecodeConstraints
from tau_ai.tools import ToolDefinition

READ_TOOL = ToolDefinition(
    name="read",
    label="Read",
    description="Read a file",
    parameters={"type": "object", "properties": {"file_path": {"type": "string"}}},
    execute=lambda **kwargs: None,
)

VERDICTS = ["include", "exclude", "examine-children"]


def _llg(**overrides):
    """A model that declares llguidance support."""
    return _model(grammar_dialect="llguidance", **overrides)


# The shared harness returns a canned "ok" body, which (correctly!) trips constraint
# verification. These are PAYLOAD-mapping tests — they assert what τ sends, not what
# comes back — so they use the documented verify opt-out to isolate the send path.
# Verification itself is covered in test_constraint_verification.py.
def _unchecked(**kwargs) -> DecodeConstraints:
    return DecodeConstraints(verify=lambda _: True, **kwargs)


# ---------------------------------------------------------------- shape rules


class TestShapeRules:
    def test_two_constraints_raise(self):
        """A server applies one constraint per request; picking one would drop the other."""
        with pytest.raises(ValueError, match="at most one of grammar/json_schema/choices"):
            DecodeConstraints(grammar="start: \"x\"", choices=["a", "b"])

    def test_empty_choices_raise(self):
        with pytest.raises(ValueError, match="must be non-empty"):
            DecodeConstraints(choices=[])

    def test_tool_choice_alone_is_not_a_constraint(self):
        c = DecodeConstraints(tool_choice="required")
        assert not c.has_constraint()

    def test_describe_is_display_only(self):
        assert DecodeConstraints(choices=VERDICTS).describe() == {
            "kind": "choices",
            "choices": VERDICTS,
        }


# ------------------------------------------------------------- wire mapping


class TestWireMapping:
    def test_choices_compile_to_a_headed_grammar(self, monkeypatch):
        _patch_client(monkeypatch)
        payload = _run(_llg(), {"constraints": _unchecked(choices=VERDICTS)})

        assert payload["grammar"] == (
            '%llguidance {}\nstart: "include" | "exclude" | "examine-children"'
        )

    def test_raw_grammar_gets_the_header(self, monkeypatch):
        _patch_client(monkeypatch)
        payload = _run(_llg(), {"constraints": _unchecked(grammar='start: "hi"')})

        assert payload["grammar"] == '%llguidance {}\nstart: "hi"'

    def test_header_is_never_doubled(self, monkeypatch):
        _patch_client(monkeypatch)
        already = '%llguidance {}\nstart: "hi"'
        payload = _run(_llg(), {"constraints": _unchecked(grammar=already)})

        assert payload["grammar"] == already

    def test_gbnf_grammar_is_not_prefixed(self, monkeypatch):
        """The llguidance header would corrupt a GBNF grammar."""
        _patch_client(monkeypatch)
        payload = _run(
            _model(grammar_dialect="gbnf"),
            {"constraints": _unchecked(grammar='root ::= "hi"')},
        )

        assert payload["grammar"] == 'root ::= "hi"'

    def test_json_schema_becomes_response_format(self, monkeypatch):
        """The server compiles the schema; τ does not reimplement llguidance."""
        _patch_client(monkeypatch)
        schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
        payload = _run(_llg(), {"constraints": _unchecked(json_schema=schema)})

        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["schema"] == schema
        assert "grammar" not in payload

    def test_constraints_key_never_leaks_into_the_body(self, monkeypatch):
        """`constraints` is a τ-internal option, not a request field."""
        _patch_client(monkeypatch)
        payload = _run(_llg(), {"constraints": _unchecked(choices=["a", "b"])})

        assert "constraints" not in payload

    def test_tool_choice_and_per_call_extra_body(self, monkeypatch):
        _patch_client(monkeypatch)
        payload = _run(
            _llg(extra_body={"cache_prompt": True, "min_p": 0.5}),
            {
                "constraints": _unchecked(
                    choices=["a", "b"],
                    tool_choice="none",
                    extra_body={"min_p": 0.01},
                )
            },
        )

        assert payload["tool_choice"] == "none"
        assert payload["min_p"] == 0.01  # per-call beats Model.extra_body
        assert payload["cache_prompt"] is True  # non-conflicting static param survives


# ------------------------------------------------------------------- gate 1


class TestCapabilityGate:
    def test_constraint_without_declared_grammar_support_raises(self, monkeypatch):
        """Silent-ignore is the worst outcome: unconstrained output, sold as constrained."""
        _patch_client(monkeypatch)
        with pytest.raises(ValueError, match="declares no grammar support"):
            _run(_model(), {"constraints": _unchecked(choices=["a", "b"])})

    def test_tool_choice_only_needs_no_grammar_support(self, monkeypatch):
        """tool_choice is plain OpenAI, not a constraint — must not trip the gate."""
        _patch_client(monkeypatch)
        payload = _run(_model(), {"constraints": _unchecked(tool_choice="required")})

        assert payload["tool_choice"] == "required"


# ------------------------------------------------------------------- gate 2


class TestToolsConflictGate:
    @pytest.mark.parametrize(
        "constraint",
        [
            DecodeConstraints(choices=VERDICTS),
            DecodeConstraints(grammar=grammar_mod.fixed("x")),
            # The dangerous one: the server would return 200 and quietly stop calling
            # tools, fabricating a schema-shaped answer.
            DecodeConstraints(json_schema={"type": "object"}),
        ],
        ids=["choices", "grammar", "json_schema"],
    )
    def test_constraint_plus_tools_raises(self, monkeypatch, constraint):
        _patch_client(monkeypatch)
        provider_opts = {"constraints": constraint}

        with pytest.raises(ValueError, match="cannot be combined with tools"):
            _run_with_tools(_llg(), provider_opts)

    def test_tool_choice_none_permits_a_constraint(self, monkeypatch):
        """Verified live: tools declared + tool_choice=none + grammar → 200, grammar applies."""
        _patch_client(monkeypatch)
        payload = _run_with_tools(
            _llg(),
            {"constraints": _unchecked(choices=VERDICTS, tool_choice="none")},
        )

        assert payload["tool_choice"] == "none"
        assert payload["grammar"].endswith('"include" | "exclude" | "examine-children"')
        assert payload["tools"]  # tools still declared


class TestConstraintFieldsHaveExactlyOneDoorIn:
    """The gates in _apply_constraints are worthless if a constraint can walk around
    them. All four routes below were live-reproduced against llama-server before the
    guard existed (CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §0.2); two of them returned
    a *wrong answer that passed verification*.

    ``grammar`` / ``json_schema`` / ``response_format`` are constraint fields τ owns.
    DecodeConstraints is the only door, because it is the only door with gates.
    """

    def test_a_grammar_smuggled_via_model_extra_body_raises(self, monkeypatch):
        """Live: reached the server ungated AND unverified — _apply_constraints returns
        early when constraints is None, so the capability gate never even runs."""
        _patch_client(monkeypatch)
        model = _model(extra_body={"grammar": 'start: "include"'})

        with pytest.raises(ValueError, match="may not set decode-constraint fields"):
            _run(model, {})

    def test_a_response_format_smuggled_via_extra_body_raises(self, monkeypatch):
        """The nastiest one. llama-server guards grammar + top-level json_schema with a
        loud 500, but `response_format` parses on a different path and SILENTLY WINS: a
        real DecodeConstraints(grammar=include|exclude) came back as
        `{"verdict": "REJECT"}` and passed the old non-empty check."""
        _patch_client(monkeypatch)
        model = _model(
            grammar_dialect="llguidance",
            extra_body={"response_format": {"type": "json_object"}},
        )

        with pytest.raises(ValueError, match="may not set decode-constraint fields"):
            _run(model, {"constraints": _unchecked(choices=VERDICTS)})

    def test_a_constraint_field_in_per_call_options_raises(self, monkeypatch):
        """The per-call body splat is the EASIER route to reach than static config."""
        _patch_client(monkeypatch)

        with pytest.raises(ValueError, match="may not set decode-constraint fields"):
            _run(_llg(), {"json_schema": {"type": "object"}})

    def test_tools_smuggled_through_per_call_options_raises(self, monkeypatch):
        """`tools` was reserved for extra_body but NOT for per-call options, so passing
        it there left has_tools=False and the tools gate never fired — re-opening the
        json_schema-silently-disables-tool-calling hole the gate exists to close."""
        _patch_client(monkeypatch)

        with pytest.raises(ValueError, match="may not set τ transport fields"):
            _run(_llg(), {"tools": [READ_TOOL], "constraints": _unchecked(choices=VERDICTS)})

    def test_decode_constraints_extra_body_cannot_smuggle_a_second_constraint(
        self, monkeypatch
    ):
        """DecodeConstraints(json_schema=..., extra_body={"grammar": ...}) satisfies the
        exactly-one-of validator while shipping BOTH to the server, which then picks a
        winner silently."""
        _patch_client(monkeypatch)
        constraints = _unchecked(
            json_schema={"type": "object"}, extra_body={"grammar": 'start: "x"'}
        )

        with pytest.raises(ValueError, match="may not set decode-constraint fields"):
            _run(_llg(), {"constraints": constraints})

    def test_a_legitimate_decode_knob_in_extra_body_still_works(self, monkeypatch):
        """The guard must not block what extra_body is FOR: server decode/cache knobs."""
        _patch_client(monkeypatch)
        model = _model(grammar_dialect="llguidance", extra_body={"cache_prompt": True, "min_p": 0.05})

        payload = _run(model, {"constraints": _unchecked(choices=VERDICTS)})

        assert payload["cache_prompt"] is True
        assert payload["min_p"] == 0.05
        assert payload["grammar"].endswith('"include" | "exclude" | "examine-children"')


def _run_with_tools(model, options):
    """_run(), but with a tools array on the request."""
    import asyncio

    from test_reasoning_effort import _CapturingClient

    from tau_ai.providers.openai import OpenAICompletionsProvider
    from tau_ai.types import TextContent, UserMessage

    _CapturingClient.last_payload = None
    provider = OpenAICompletionsProvider(api_key="sk-test")

    async def _go():
        stream = await provider.stream_chat(
            model=model,
            messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)],
            tools=[READ_TOOL],
            options=options,
        )
        async for _ in stream:
            pass

    asyncio.run(_go())
    return _CapturingClient.last_payload


class TestConstrainedCallsDoNotThink:
    """A constrained call must not THINK, and this is not a preference — it is the
    difference between getting the answer and getting an empty string.

    The grammar applies from the very first generated token. A reasoning model's first
    token belongs to its thinking, so llama-server forces the constrained answer into
    the REASONING channel and returns::

        {"content": "", "reasoning_content": "include"}

    Reproduced live against llama.cpp b1061 serving Qwen3-35B: the constraint held
    perfectly, the verdict was correct, and it simply was not where anybody reads it.
    τ reads `content`, sees "", and raises ConstraintViolation — which reads as "the
    server dropped the grammar" and is the exact opposite of what happened. EVERY
    constrained call against a thinking model produced an empty verdict this way, which
    is why the retrieval-review demo returned nothing at all.

    Thinking is pointless here regardless: the answer is grammar-forced, so there is
    nothing left for reasoning to decide.

    This applies to the grammar/choices paths only. A `json_schema`/`response_format`
    call KEEPS thinking on: that constraint is a chat-template-built grammar that
    already models the reasoning block (upstream llama.cpp #20223), so the verdict
    lands in `content` — see `test_a_json_schema_call_keeps_thinking`.
    """

    def test_a_constrained_call_disables_thinking(self, monkeypatch):
        _patch_client(monkeypatch)
        payload = _run(_llg(), {"constraints": _unchecked(choices=VERDICTS)})

        assert payload["chat_template_kwargs"] == {"enable_thinking": False}

    def test_a_raw_grammar_call_disables_thinking(self, monkeypatch):
        """Same failure mode as choices: a raw user grammar applies from token 0, so a
        thinking model lands the constrained answer in the reasoning channel."""
        _patch_client(monkeypatch)
        payload = _run(_llg(), {"constraints": _unchecked(grammar='root ::= "yes" | "no"')})

        assert payload["chat_template_kwargs"] == {"enable_thinking": False}

    def test_a_json_schema_call_keeps_thinking(self, monkeypatch):
        """json_schema → response_format is DELIBERATELY left thinking-enabled: its
        grammar is template-built and reasoning-aware (llama.cpp upstream #20223), so it
        already works with thinking ON. Suppressing it would be an unwarranted workaround."""
        _patch_client(monkeypatch)
        payload = _run(
            _llg(),
            {"constraints": _unchecked(json_schema={"type": "object", "properties": {}})},
        )

        assert "chat_template_kwargs" not in payload

    def test_an_unconstrained_call_is_left_alone(self, monkeypatch):
        """Thinking is only incompatible with a CONSTRAINT. An ordinary turn keeps
        whatever reasoning behaviour the model and config asked for — this must not
        become a global 'τ turns thinking off' regression."""
        _patch_client(monkeypatch)
        payload = _run(_llg(), {})

        assert "chat_template_kwargs" not in payload

    def test_the_caller_keeps_control(self, monkeypatch):
        """An explicit chat_template_kwargs wins. τ's job is to stop a silent empty
        verdict, not to forbid someone who knows their server from re-enabling thinking."""
        _patch_client(monkeypatch)
        payload = _run(
            _llg(),
            {
                "constraints": _unchecked(
                    choices=VERDICTS,
                    extra_body={"chat_template_kwargs": {"enable_thinking": True}},
                )
            },
        )

        assert payload["chat_template_kwargs"] == {"enable_thinking": True}
