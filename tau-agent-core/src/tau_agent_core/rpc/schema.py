"""Deriving a verb's params schema from the capability it performs.

The half of the command table that was mechanical. ``capabilities.CAPABILITIES``
already says what every capability takes — argument names, domains, cardinality,
requiredness — and the table said it again in a JSON Schema literal, so the two
could disagree and for the four capability-backed mutations they agreed only by
hand. This builds the literal from the declaration instead.

The result half, from 2026-09-04
--------------------------------

``Capability.returns`` now says what each capability gives BACK, so
``result_schema_for`` derives that half too. This reverses the line this module
shipped with — "the result schema stays hand-written" — for the reason
docs/REMOTE-CONTROL.md §6 records: τ reported the same four mutations three ways
and only the wire said whether the conversation had changed.

The asymmetry between the two halves is deliberate. Parameters are declared as
:class:`~tau_agent_core.capabilities.Argument` records over a domain vocabulary,
because a head has to RENDER a form for them and a domain is what tells it which
field to draw. A result is read, never rendered blind, so ``returns`` is the JSON
Schema itself and there is no second vocabulary to keep in step.

What is NOT derived, and why
----------------------------

**The verbs with no capability behind them.** ``prompt``, ``get_capabilities``,
``next_step`` and ``enumerate_domain`` keep hand-written literals on both halves,
which is §6 A2 holding.

**The prose.** ``Argument.description`` is a form label — "Which configured
model." — and the wire's description is reference documentation for someone
implementing a second host, which is a different register at a different length.
A verb passes ``overrides`` for a property whose wire description says more than
the label does, and for the schema keywords a domain does not carry
(``complete_path``'s ``minimum``). Structure is what drifted; prose never did.

**The domain-to-JSON-type table.** It lives here, in the layer that owns the
wire, and not on :class:`~tau_agent_core.capabilities.Domain`, which says how a
domain's values are FOUND and not what they look like on a wire it knows nothing
about (docs/REMOTE-CONTROL.md §6 A1). ``_check_domain_types`` runs at import, so
a new domain with no entry is a startup failure rather than a verb that silently
accepts anything.

Reference: docs/REMOTE-CONTROL.md §6 "Recommendation: audit, do not generate" —
this generates one half of item 1, on the ground that A1, A3 and A6 do not reach
a table the core declares for its own use. A2 and A4 still hold: the verbs that
are not capability-backed keep their literals, and a declined verb has no
capability at all.
"""

from __future__ import annotations

import copy
from typing import Any

from tau_agent_core.capabilities import CAPABILITIES, DOMAINS

__all__ = ["params_schema_for", "result_schema_for"]

_DOMAIN_TYPES: dict[str, str] = {
    "text": "string",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
    "model_name": "string",
    "session_id": "string",
    "path": "string",
    "message_id": "string",
    "message_id_scope": "string",
    "extension_name": "string",
}


def _check_domain_types() -> None:
    """Every declared domain has a wire type, checked at import.

    Without this a new domain would reach a generated schema as a missing key and
    raise from inside a request, which is a wire error reported to whichever host
    happened to call the verb first rather than to whoever added the domain.
    """
    missing = sorted(set(DOMAINS) - set(_DOMAIN_TYPES))
    if missing:
        raise ValueError(
            f"domain(s) {missing} have no wire type in rpc/schema.py. A domain the "
            "wire cannot type is a params schema that would accept anything."
        )
    unknown = sorted(set(_DOMAIN_TYPES) - set(DOMAINS))
    if unknown:
        raise ValueError(f"rpc/schema.py types domain(s) {unknown} that are not declared")


_check_domain_types()


def params_schema_for(
    capability: str, *, overrides: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The JSON Schema for ``capability``'s parameters.

    Args:
        capability: The capability's name, a key of
            :data:`~tau_agent_core.capabilities.CAPABILITIES`.
        overrides: Extra schema keywords per property, merged over what the
            declaration produces — a longer wire description, a ``minimum``. Naming
            a property the capability does not take raises, so an override cannot
            outlive the argument it describes.

    Returns:
        A schema in the vocabulary ``commands._assert_supported_schema`` accepts:
        ``type``, ``properties``, ``required``, ``additionalProperties``.

    Raises:
        KeyError: No capability has that name.
        ValueError: The capability's arguments are not expressible as domains
            (``submit``), or an override names an argument it does not take.
    """
    declared = CAPABILITIES[capability]
    if declared.arguments is None:
        raise ValueError(
            f"capability {capability!r} declares its arguments unstateable as domains, "
            "so its wire schema is the hand-written one and cannot be derived"
        )
    extra = dict(overrides or {})
    names = {argument.name for argument in declared.arguments}
    stale = sorted(set(extra) - names)
    if stale:
        raise ValueError(
            f"params_schema_for({capability!r}) was given overrides for {stale}, which "
            f"{capability!r} does not take"
        )

    properties: dict[str, Any] = {}
    required: list[str] = []
    for argument in declared.arguments:
        domain = DOMAINS[argument.domain]
        wire_type = "array" if argument.cardinality == "many" else _DOMAIN_TYPES[argument.domain]
        prop: dict[str, Any] = {"type": wire_type, "description": argument.description}
        if domain.values is not None and wire_type == "string":
            prop["enum"] = list(domain.values)
        prop.update(extra.get(argument.name, {}))
        properties[argument.name] = prop
        if argument.required:
            required.append(argument.name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def result_schema_for(
    capability: str, *, overrides: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The JSON Schema for what ``capability`` returns.

    The sibling of :func:`params_schema_for`, reading
    :attr:`~tau_agent_core.capabilities.Capability.returns` instead of
    ``arguments``. Three capabilities may share one declaration — the tree
    mutations, the lifecycle verbs, the extension actions — so the result is a
    deep copy and a caller that edits it cannot reach the registry.

    Args:
        capability: The capability's name, a key of
            :data:`~tau_agent_core.capabilities.CAPABILITIES`.
        overrides: Extra schema keywords per property, merged over what the
            declaration says — a longer wire description than the capability's
            own. Naming a property the capability does not return raises, so an
            override cannot outlive the field it describes.

    Returns:
        A schema in the vocabulary ``commands._assert_supported_schema`` accepts:
        ``type``, ``properties``, ``required``.

    Raises:
        KeyError: No capability has that name.
        ValueError: An override names a property the capability does not return.
    """
    declared = CAPABILITIES[capability].returns
    if declared is None:
        raise ValueError(
            f"capability {capability!r} declares no `returns`, which _check_registry "
            "refuses at import — this is unreachable unless that check was removed"
        )
    schema = copy.deepcopy(declared)
    properties: dict[str, Any] = schema.get("properties", {})
    extra = overrides or {}
    stale = sorted(set(extra) - set(properties))
    if stale:
        raise ValueError(
            f"result_schema_for({capability!r}) was given overrides for {stale}, which "
            f"{capability!r} does not return"
        )
    for name, keywords in extra.items():
        properties[name].update(keywords)
    return schema
