"""The ``@agent_facing`` marker: which objects the generated reference documents.

Reference: docs/AGENT-DOCS.md

τ ships a documentation library that an agent reads with the ordinary ``read``
tool in order to extend or modify τ itself. Half of that library is hand-written
prose. The other half -- the reference section -- is generated from this marker.

The decorator is a **complete** no-op. It returns the object it decorates,
unchanged, with the same type, and leaves no trace on it at all. It never wraps,
never validates at import time, and costs nothing on a call.

Leaving no trace is deliberate, and it is not merely tidiness. An earlier
version set a ``__tau_agent_facing__`` attribute on the decorated object, for
callers who might want to introspect a live one. On a ``Protocol`` that attribute
joins ``__protocol_attrs__``, so it becomes a member every implementation must
have, and ``isinstance(x, SomeMarkedProtocol)`` starts returning ``False`` for
classes that satisfy the protocol perfectly well. ``tau_agent_core``'s
``ConversationSession`` is such a protocol. A documentation marker must not be
able to change what the program does, and the only way to guarantee that is for
it to do nothing.

**The documentation build does not import this module's decorator, or yours.**
The build reads the decorator statically, out of the source AST, using griffe --
see ``tau_agent_core.docs_build``. That is why a marker with no runtime effect
still works, and it is deliberate for a second reason: τ's Anthropic and Google
providers import their SDKs lazily so the test suite runs without them, and
``tau_coding_agent`` pulls in Textual. A build that imported every module to
find its decorators would give up both properties.

Which objects carry it
----------------------

Any object an extension author or an agent calls. That set is not ``__all__``:
``__all__`` means "public API", and the two differ in both directions. A private
helper can be exactly what an agent modifying τ needs to read, and a public name
can be an internal detail of a measured run that no extension ever touches.

Marking a class marks its public methods and attributes with it. A method may
carry its own ``@agent_facing`` to place it under a different topic; it does not
need one to be documented.

Coverage
--------

``scripts/check_docs_coverage.py`` walks the marked set and fails on a marked
object whose docstring omits a parameter, or which has no docstring at all. An
undocumented parameter is still published -- name, annotation and default come
from the signature, not the prose -- so the reference never silently omits an
argument. The check is what stops it from silently omitting the *meaning*.
"""

from __future__ import annotations

from typing import Callable, TypeVar

__all__ = ["DECORATOR_PATH", "agent_facing"]

# The canonical dotted path of the decorator, as griffe resolves it through an
# import, and the string `tau_agent_core.docs_build` matches on. All three import
# forms resolve to it:
#
#     from tau_llm.docs import agent_facing     ->  @agent_facing(...)
#     import tau_llm.docs                       ->  @tau_llm.docs.agent_facing(...)
#     from tau_llm import docs as d             ->  @d.agent_facing(...)
#
# Named here, once, so the marker and the build that finds it cannot drift.
DECORATOR_PATH = "tau_llm.docs.agent_facing"

_T = TypeVar("_T")


def agent_facing(*, topic: str, since: str | None = None) -> Callable[[_T], _T]:
    """Mark an object for the generated agent-facing reference.

    The returned decorator is the identity function, typed as such, so applying
    it changes neither the runtime behaviour of the object nor what mypy infers
    about it. Both arguments are read from the source by the documentation
    build; nothing reads them at runtime.

    Args:
        topic: The library page this object belongs on, as a bare stem --
            ``"extensions"``, not ``"docs/library/extensions.md"``. The build
            groups the reference by this value, and it must be a key of
            ``tau_agent_core.docs_build.TOPICS``: a topic with no page is an
            error, not a new page.
        since: The τ version that introduced the object, as it appears in
            ``__version__`` (``"0.9.4"``). ``None`` means "present since before
            the reference was generated", which is correct for anything that
            predates this marker.

    Returns:
        A decorator that returns its argument unchanged.

    Note:
        Both arguments must be written as literals at the call site. The build
        never imports the module, so it cannot evaluate a name or an expression,
        and it raises rather than guess.
    """

    def mark(obj: _T) -> _T:
        return obj

    return mark
