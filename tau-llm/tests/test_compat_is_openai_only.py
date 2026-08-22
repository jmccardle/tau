"""``detect_compat`` must never run for a non-OpenAI wire protocol.

``tau_llm.compat`` answers two questions — ``max_tokens`` versus
``max_completion_tokens``, and whether the endpoint supports ``stream_options``
usage. Both are properties of the OpenAI chat-completions wire. Neither means
anything on ``anthropic-messages`` or ``google-generative-ai``.

The design doc listed this as a change to make (gate the call on ``model.api``).
It is not: the gate already exists structurally. ``detect_compat`` is reachable
only through ``resolve_compat``, and ``resolve_compat`` is called from exactly
one place — inside ``OpenAICompletionsProvider``. A new provider that simply
never calls it inherits the correct behaviour for free, the same way
``Model.grammar_dialect = None`` gives a new provider the correct constraint
refusal for free (S6).

That makes this a structural invariant rather than a branch, so the test that
protects it is structural too. It fails the day someone hoists the compat call
into a shared path — ``client.py``'s dispatch, say, or ``providers/base.py`` —
where every wire protocol would start paying for an OpenAI-only question.

Reference: docs/ANTHROPIC-GOOGLE-CLIENTS.md S7.
"""

import ast
from pathlib import Path

import tau_llm

SRC = Path(tau_llm.__file__).parent

# compat.py defines and layers them; the OpenAI provider is the one consumer.
ALLOWED_CALLERS = {"compat.py", "providers/openai.py"}

COMPAT_FUNCTIONS = {"detect_compat", "resolve_compat"}


def _call_sites() -> dict[str, set[str]]:
    """Map each compat function to the set of source files that CALL it.

    Walks the AST rather than grepping so an import, a docstring mention or a
    re-export in ``__init__.py`` does not count as a call.
    """
    found: dict[str, set[str]] = {name: set() for name in COMPAT_FUNCTIONS}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in COMPAT_FUNCTIONS:
                found[name].add(rel)
    return found


def test_compat_is_called_only_from_the_openai_path():
    sites = _call_sites()
    for name, files in sites.items():
        unexpected = files - ALLOWED_CALLERS
        assert not unexpected, (
            f"{name} is called from {sorted(unexpected)}, outside the OpenAI path. "
            "tau_llm.compat answers OpenAI-wire-only questions (max_tokens vs "
            "max_completion_tokens, stream_options usage); running it for "
            "anthropic-messages or google-generative-ai is meaningless. Keep the "
            "call inside the provider that speaks that wire "
            "(docs/ANTHROPIC-GOOGLE-CLIENTS.md S7)."
        )


def test_the_openai_provider_still_resolves_compat():
    """The other half of the invariant: the guard above would also pass if the
    OpenAI path stopped consulting compat at all, which would be a real bug."""
    assert "providers/openai.py" in _call_sites()["resolve_compat"]
