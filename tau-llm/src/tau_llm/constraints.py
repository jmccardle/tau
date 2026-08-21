"""Per-call decode constraints — the channel that carries a grammar to the server.

A ``DecodeConstraints`` is part of a call's **ephemeral frame** (sampling parameters),
never a content channel: it cannot inject or reorder messages, and model input remains
the system prompt + the linear tree path (tree-as-truth, §0).

Gating lives in the provider (it owns the payload), but the *shape* rules are enforced
here at construction, so a caller bug surfaces at the call site rather than one layer
down:

- Exactly one of ``grammar`` / ``json_schema`` / ``choices``. A server takes one
  constraint per request; two is a caller bug — raise, don't silently pick.

Reference: docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §3.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, SkipValidation, model_validator


class ConstraintViolation(RuntimeError):
    """A constrained generation did not satisfy its constraint.

    Raised when verification (§4.3) finds the output outside the declared constraint —
    which, on a server where llguidance died mid-generation, means the generation ran
    **unconstrained** and the result is fabricated data. Carries the offending output.
    """

    def __init__(self, message: str, output: str) -> None:
        super().__init__(message)
        self.output = output


class DecodeConstraints(BaseModel):
    """A decode constraint for one completion.

    Attributes:
        grammar: Raw grammar text, in the model's declared dialect. The provider adds
            the ``%llguidance`` header for llguidance models (never double-prefixing,
            never prefixing a gbnf model's grammar).
        json_schema: A JSON Schema; sent as OpenAI-style ``response_format`` so the
            server does its own schema→grammar conversion (llguidance consumes JSON
            Schema natively — τ does not reimplement that compiler).
        choices: Sugar for the verdict pattern; compiled to a choice grammar.
        tool_choice: OpenAI-compat passthrough. Note ``"none"`` is what makes a
            constraint legal alongside a declared tools array (see the provider).
        extra_body: Per-call body params. Highest precedence: over ``Model.extra_body``,
            over τ defaults.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # SkipValidation, not a bare `str | None`: pydantic would coerce a
    # tau_llm.grammar.Grammar (a str subclass carrying its own output checker) down to a
    # plain str and DROP the checker — silently turning every helper-built grammar into
    # an "unverifiable" one. The field still accepts a plain str; it just is not rebuilt.
    grammar: SkipValidation[str | None] = None
    json_schema: dict[str, Any] | None = None
    choices: list[str] | None = None
    tool_choice: str | dict[str, Any] | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # The escape hatch for raw grammars, which admit no general check. Also the
    # explicit "I know what I'm doing" opt-out: pass ``verify=lambda _: True``.
    verify: Callable[[str], bool] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _exactly_one_constraint(self) -> DecodeConstraints:
        set_kinds = [
            name
            for name, value in (
                ("grammar", self.grammar),
                ("json_schema", self.json_schema),
                ("choices", self.choices),
            )
            if value is not None
        ]
        if len(set_kinds) > 1:
            raise ValueError(
                f"DecodeConstraints takes at most one of grammar/json_schema/choices; "
                f"got {set_kinds}. A server applies one constraint per request — "
                "picking one for you would silently drop the other."
            )
        if self.choices is not None and not self.choices:
            raise ValueError("DecodeConstraints.choices must be non-empty")

        # A raw grammar τ cannot check, with no verify() to check it, is an
        # UNVERIFIABLE constraint — and an unverifiable constraint is the exact thing
        # this class exists to refuse. The old behaviour (accept it, assert only that
        # the output is non-empty) was verification in name only: with the grammar
        # silently dropped server-side, a `start: "include" | "exclude"` constraint
        # returned {"verdict": "no"} and sailed through.
        #
        # grammar.choice()/fixed()/regex()/sequence() return a Grammar carrying its own
        # checker, so they need nothing extra. A hand-written grammar string does not,
        # and the caller must say how to check it.
        if self.grammar is not None and self.verify is None:
            checker = getattr(self.grammar, "check", None)
            if checker is None:
                raise ValueError(
                    "DecodeConstraints(grammar=...) needs a way to verify its output. "
                    "τ will not reimplement the grammar engine to check a hand-written "
                    "grammar, and it will not pretend a constraint held when it cannot "
                    "tell — a server that drops the grammar mid-generation (a real, "
                    "silent failure mode) would then return free prose as a constrained "
                    "answer.\n"
                    "Either build the grammar with tau_llm.grammar (choice/fixed/regex/"
                    "sequence — each carries its own checker), or pass "
                    "DecodeConstraints(grammar=..., verify=lambda out: ...)."
                )
        return self

    def has_constraint(self) -> bool:
        """Whether this carries an actual decode constraint (vs only tool_choice/extra_body)."""
        return self.grammar is not None or self.json_schema is not None or self.choices is not None

    def verify_output(self, text: str) -> None:
        """Assert ``text`` actually satisfies this constraint; raise if it does not.

        **τ never trusts a constraint blindly.** The failure mode is real and observed
        (GRAMMAR_DECODING_RECON.md:36): on some tokenizers llguidance dies mid-generation,
        logs the error *server-side*, and lets generation continue **unconstrained** —
        completely invisible to the client, which receives a 200 and plausible text.

        An unconstrained result returned as a constrained one is fabricated data, so a
        failed check raises rather than warns.

        What can be checked per kind:

        - ``choices`` — exact membership. Total.
        - ``json_schema`` — the output must parse as JSON. (Full schema validation is
          optional; a parse failure alone already catches constraint death.)
        - ``grammar`` — no general check is possible against an arbitrary grammar. The
          caller may supply ``verify``; absent that, τ asserts non-empty output and the
          residual risk is documented.
        """
        if not self.has_constraint():
            return

        if self.verify is not None:
            if not self.verify(text):
                raise ConstraintViolation(
                    f"caller-supplied verify() rejected the constrained output: {text!r}", text
                )
            return

        if self.choices is not None:
            if text not in self.choices:
                raise ConstraintViolation(
                    f"constrained output {text!r} is not one of {self.choices!r}. "
                    "The grammar did not hold — the server most likely dropped the "
                    "constraint mid-generation and ran free.",
                    text,
                )
            return

        if self.json_schema is not None:
            try:
                json.loads(text)
            except ValueError as exc:
                raise ConstraintViolation(
                    f"constrained output is not valid JSON ({exc}): {text!r}. "
                    "The schema constraint did not hold.",
                    text,
                ) from exc
            return

        # Grammar. The validator guarantees we get here only with a checker available
        # (a tau_llm.grammar Grammar) or with verify= set (handled above) — so there is
        # no unverified path left, and no "non-empty is the floor" pretence.
        checker = getattr(self.grammar, "check", None)
        if checker is None:  # pragma: no cover - _exactly_one_constraint forbids it
            raise AssertionError(
                "unverifiable grammar reached verify_output; the constructor should "
                "have rejected it"
            )
        if not checker(text):
            raise ConstraintViolation(
                f"constrained output {text!r} does not satisfy the grammar "
                f"{str(self.grammar)!r}. The grammar did not hold — the server most "
                "likely dropped the constraint (llguidance can die mid-generation and "
                "let the model run free) or silently overrode it.",
                text,
            )

    def describe(self) -> dict[str, Any]:
        """A small, display-only summary for observability (§3.3).

        Recorded on events / persisted entry payloads so *what constrained a
        generation* is inspectable after the fact. Never replayed as context.
        """
        if self.choices is not None:
            return {"kind": "choices", "choices": list(self.choices)}
        if self.json_schema is not None:
            return {"kind": "json_schema", "schema": self.json_schema}
        if self.grammar is not None:
            return {"kind": "grammar", "grammar": self.grammar}
        return {"kind": "none"}
