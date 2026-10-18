"""Where an extension comes from, and the one name it goes by.

Rationale: docs/EXTENSION-PACKAGES.md. §2 makes the identifier the Python
module name for an installed package and the file stem, verbatim, for a file;
§3 makes ``:`` the only delimiter any parser splits on.
"""

from __future__ import annotations

from pathlib import PurePath

from tau_llm.docs import agent_facing

UNUSABLE_CHARACTERS = ":"
"""Characters that make a file stem unusable as an identifier (§2.1); whitespace is the other case."""


@agent_facing(topic="extensions")
def extension_identifier(label: str) -> str:
    """The identifier of the extension a runner bucket label names.

    This is the one derivation (EXTENSION-PACKAGES.md §2, #40). The config
    key, topic emitter, command owner and ``/extensions`` label all read it.

    A label is one of three things:

    - a file path: ``…/my-ext.py`` is ``my-ext`` and a package directory
      ``…/my.ext`` is ``my.ext``, the stem verbatim;
    - an installed module name, ``tau_router.planner``, returned as is;
    - an inline SDK factory's ``module:qualname``, which becomes
      ``module.qualname``, its fully qualified Python name.

    Args:
        label: The bucket label the loader bound the extension under.

    Returns:
        The identifier.

    Raises:
        ValueError: The label yields an empty identifier.
    """
    if label.endswith(".py"):
        ident = PurePath(label).name[: -len(".py")]
    elif "/" in label:
        ident = PurePath(label).name
    else:
        module, sep, qualname = label.partition(":")
        ident = f"{module}.{qualname}" if sep else label
    if not ident:
        raise ValueError(f"extension label {label!r} yields an empty identifier")
    return ident


@agent_facing(topic="extensions")
def unusable_reason(identifier: str) -> str | None:
    """Why ``identifier`` cannot name an extension, or ``None`` when it can.

    A ``:`` breaks ``--ext-config`` and topic parsing, and whitespace breaks
    commands (EXTENSION-PACKAGES.md §2.1). Such a file is listed, not loaded.

    Args:
        identifier: A candidate identifier, usually a file stem.

    Returns:
        A sentence naming the character and saying to rename the file, or
        ``None``.
    """
    for char in identifier:
        if char in UNUSABLE_CHARACTERS:
            return f"its name contains {char!r}, which τ uses as a delimiter; rename the file"
        if char.isspace():
            return f"its name contains whitespace ({char!r}); rename the file"
    return None


@agent_facing(topic="extensions")
def shorthand_segment(identifier: str) -> str:
    """The last dotted segment of ``identifier``, the shorthand a user may type (§4).

    Args:
        identifier: A full identifier.

    Returns:
        ``planner`` for ``tau_router.planner``; the identifier itself when it
        has no dot.
    """
    return identifier.rsplit(".", 1)[-1]


@agent_facing(topic="extensions")
def resolve_shorthand(token: str, identifiers: list[str]) -> str | None:
    """Resolve a typed extension name against the given identifiers, fresh (§4).

    An exact identifier wins. Otherwise ``token`` matches the identifiers whose
    last dotted segment equals it. Nothing is cached, so a new install cannot
    re-point a shorthand silently.

    Args:
        token: What the user typed.
        identifiers: The candidates, usually the enabled extensions.

    Returns:
        The one matching identifier, or ``None`` when nothing matches.

    Raises:
        ValueError: Two or more identifiers share the segment; the message
            names all of them.
    """
    if token in identifiers:
        return token
    matches = sorted({ident for ident in identifiers if shorthand_segment(ident) == token})
    if len(matches) > 1:
        raise ValueError(
            f"{token!r} is ambiguous: it matches {', '.join(matches)}. Type the full name."
        )
    return matches[0] if matches else None
