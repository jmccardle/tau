"""llguidance Lark-style grammar composition — string generation only.

τ does **not** reimplement llguidance: this module builds grammar *text* for the
common shapes an extension needs, and the server compiles it. No parsing, no
validation of the resulting grammar beyond escaping.

The escaping is the actual work here. A mis-escaped grammar is not a loud failure —
it is a *silently wrong constraint*: the server accepts a grammar that means
something other than what the caller asked for, and the generation it forces looks
plausible. Hence the exhaustive escaping tests.

**Every helper returns a** :class:`Grammar` **— grammar text that carries a checker
for its own output.** This exists to close a sharp edge: ``choice("a", "b")`` and
``DecodeConstraints(choices=["a", "b"])`` compile to the *identical* wire grammar,
but before this the first was verified not at all and the second by exact membership.
An author reaching for the helper module silently got the weaker path. A ``Grammar``
knows what it forces, so both are now checked the same way.

Helpers return a grammar body with **no** ``%llguidance`` header — the provider owns
prefixing (it knows the model's dialect and must never double-prefix, nor prefix a
gbnf model's grammar).

The checkers below are not guesses; each was confirmed against llama-server with
llguidance (see the table in CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §0.2). Notably
``sequence()`` concatenates with no implicit whitespace, and ``fixed("yes ")`` really
does force the trailing space — so the checkers compare EXACTLY, and callers must not
strip constrained output before verifying it.

Reference: docs/CONSTRAINED-GEN-AND-BRANCHING-PLAN.md §4.
Dialect: llguidance Lark (llama.cpp common/sampling.cpp:201 dispatches on the prefix).
"""

from __future__ import annotations

import re
from typing import Callable

# The header llama.cpp dispatches on to select llguidance over GBNF.
LLGUIDANCE_HEADER = "%llguidance {}\n"


class Grammar(str):
    """Grammar source text that knows how to check its own output.

    A ``str`` subclass, so it drops into the request payload and every string
    operation unchanged; the ``check`` attribute is what makes it verifiable.

    ``check`` is ``None`` when the shape cannot be checked from the text alone
    (``sequence`` of non-literals, or a hand-written grammar). ``DecodeConstraints``
    treats that as "τ cannot verify this" and demands an explicit ``verify=`` rather
    than pretending — an unverifiable constraint returned as verified is fabricated
    data.
    """

    check: Callable[[str], bool] | None

    def __new__(cls, text: str, check: Callable[[str], bool] | None = None) -> Grammar:
        obj = super().__new__(cls, text)
        obj.check = check
        return obj


# Lark string literals are double-quoted. Backslash and the quote itself must be
# escaped, and NO literal control character may appear raw in the grammar source.
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def escape(text: str) -> str:
    """Escape ``text`` for use inside a double-quoted Lark string literal.

    Order matters: backslash is replaced first (via the dict, which Python applies
    per-character, so there is no double-substitution hazard).

    Every C0 control character is escaped, not just the three common ones. The named
    escapes above are for readability; the ``\\xNN`` fallback is what makes the
    module's own promise ("no literal control character appears raw") true. A raw
    ``\\x0b`` in the source is exactly the silently-wrong-constraint this module
    exists to prevent.
    """
    out = []
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def literal(text: str) -> str:
    """A quoted, escaped Lark string literal — ``hi "x"`` → ``"hi \\"x\\""``.

    Returns a plain ``str``: a literal is a grammar *fragment*, not a whole grammar,
    so it has no output to check.
    """
    return f'"{escape(text)}"'


def fixed(text: str) -> Grammar:
    """Force exactly ``text``. Every token is grammar-forced (jump-forward's best case).

    Whitespace is significant: ``fixed("yes ")`` forces the trailing space, verified
    live. The checker therefore compares exactly.
    """
    return Grammar(f"start: {literal(text)}", check=lambda out: out == text)


def choice(*alternatives: str) -> Grammar:
    """Force exactly one of ``alternatives`` — the verdict pattern (§4.2).

    Under jump-forward each verdict costs ~1 forward pass regardless of the token
    length of the chosen alternative: the grammar forces everything but the decision
    point.

    Raises on an empty alternative set, and on duplicates — a duplicated alternative
    is an ambiguous grammar and almost always a caller bug (Fail-Early).
    """
    if not alternatives:
        raise ValueError("choice() requires at least one alternative")
    if len(set(alternatives)) != len(alternatives):
        raise ValueError(f"choice() alternatives must be unique; got {list(alternatives)}")
    alts = set(alternatives)
    return Grammar(
        "start: " + " | ".join(literal(a) for a in alternatives),
        check=lambda out: out in alts,
    )


def regex(pattern: str) -> Grammar:
    """Constrain to a regex terminal — ``start: /pattern/``.

    The pattern is passed through verbatim (it is regex syntax, not a Lark string),
    so ``/`` must be escaped by the caller if it appears literally.

    The checker is ``re.fullmatch``. llguidance's regex dialect (Rust ``regex``) and
    Python's ``re`` agree on the constructs reachable here — character classes,
    alternation, repetition — all confirmed live. An unparseable-by-Python pattern
    yields no checker rather than a wrong one, so the caller is told to pass
    ``verify=`` instead of being silently mis-verified.
    """
    if not pattern:
        raise ValueError("regex() requires a non-empty pattern")
    try:
        compiled = re.compile(pattern)
    except re.error:
        return Grammar(f"start: /{pattern}/", check=None)
    return Grammar(
        f"start: /{pattern}/",
        check=lambda out: compiled.fullmatch(out) is not None,
    )


def sequence(*parts: str) -> Grammar:
    """Concatenate literal parts into one forced string.

    ``sequence("verdict: ", "include")`` → ``start: "verdict: " "include"``, which
    forces exactly ``"verdict: include"`` — no implicit whitespace between terminals
    (verified live). The checker compares against that concatenation.
    """
    if not parts:
        raise ValueError("sequence() requires at least one part")
    expected = "".join(parts)
    return Grammar(
        "start: " + " ".join(literal(p) for p in parts),
        check=lambda out: out == expected,
    )


def with_header(grammar_text: str) -> str:
    """Prefix ``grammar_text`` with the llguidance header, never double-prefixing.

    The provider calls this; callers of the helpers above should not.
    """
    if grammar_text.lstrip().startswith("%llguidance"):
        return grammar_text
    return LLGUIDANCE_HEADER + grammar_text
