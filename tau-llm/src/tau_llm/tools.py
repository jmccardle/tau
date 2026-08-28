"""τ-llm tools: Tool definitions and parameter validation.

Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.

ToolDefinition is the tool shape this package owns; ToolSpec is the structural
contract a provider actually needs in order to serialize one. define_tool()
builds and validates a ToolDefinition.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Callable, Literal, Protocol

from pydantic import BaseModel, WithJsonSchema
from tau_llm.docs import agent_facing

#: ``execute`` is a live Python callable, so it has no JSON representation — and
#: without this annotation ``model_json_schema()`` does not merely omit it, it
#: RAISES ``PydanticInvalidForJsonSchema``. That takes the whole tool hierarchy
#: out of every schema-derived artifact (generated docs, an OpenAPI-style
#: description of what crosses each module boundary) for the sake of one field.
#:
#: Describing it as an opaque marker keeps the rest of the shape generable and
#: says plainly what the field is, rather than hiding it: a reader of the schema
#: learns the tool carries an implementation, and that it is not data.
ToolExecute = Annotated[
    Callable,
    WithJsonSchema(
        {
            "type": "string",
            "format": "python-callable",
            "description": (
                "The tool's implementation. A live Python callable, present on the "
                "object but not representable in JSON."
            ),
        }
    ),
]


def _validate_json_schema(schema: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Validate data against a JSON Schema using pydantic.

    Args:
        schema: JSON Schema dict to validate against.
        data: Data dict to validate.

    Returns:
        The validated data dict.

    Raises:
        ValueError: If data doesn't match the schema.
    """
    try:
        # Use a simple approach: try to validate required fields
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        errors = []
        for field_name in required:
            if field_name not in data:
                errors.append(f"Missing required field: '{field_name}'")
        # Check types for provided fields
        for field_name, value in data.items():
            if field_name in properties:
                expected_type = properties[field_name].get("type")
                if expected_type == "string" and not isinstance(value, str):
                    errors.append(
                        f"Field '{field_name}': expected string, got {type(value).__name__}"
                    )
                # `bool` subclasses `int` in Python but is a distinct type in JSON
                # Schema, so both numeric checks must exclude it explicitly —
                # otherwise a model emitting `true` for an integer parameter passes
                # validation and the tool does arithmetic on a bool.
                elif expected_type == "integer" and (
                    isinstance(value, bool) or not isinstance(value, int)
                ):
                    errors.append(
                        f"Field '{field_name}': expected integer, got {type(value).__name__}"
                    )
                elif expected_type == "number" and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                ):
                    errors.append(
                        f"Field '{field_name}': expected number, got {type(value).__name__}"
                    )
                elif expected_type == "boolean" and not isinstance(value, bool):
                    errors.append(
                        f"Field '{field_name}': expected boolean, got {type(value).__name__}"
                    )
                elif expected_type == "array" and not isinstance(value, list):
                    errors.append(
                        f"Field '{field_name}': expected array, got {type(value).__name__}"
                    )
                elif expected_type == "object" and not isinstance(value, dict):
                    errors.append(
                        f"Field '{field_name}': expected object, got {type(value).__name__}"
                    )
        if errors:
            raise ValueError("; ".join(errors))
        return data
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Schema validation failed: {e}")


@agent_facing(topic="tools")
class ToolDefinition(BaseModel):
    """Tool definition for the LLM API.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
    """

    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    execute: ToolExecute
    prompt_snippet: str | None = None
    prompt_guidelines: list[str] | None = None
    execution_mode: Literal["sequential", "parallel"] = "parallel"


@agent_facing(topic="tools")
class ToolSpec(Protocol):
    """What a provider needs in order to put a tool on the wire.

    The three attributes ``_convert_tools_to_openai`` actually reads, and nothing
    else. This exists because the annotation there used to say
    ``list[ToolDefinition]`` and that was **false at runtime**: the agent loop
    passes ``tau_agent_core.tools.base.AgentTool`` instances, which are not
    ``ToolDefinition`` at all — they *wrap* one and re-expose these three as
    properties. The call worked by duck typing and the type checker could never
    see it, because ``tau_llm`` does not import ``tau_agent_core`` (the dependency
    arrow points the other way, correctly).

    A Protocol states the real contract in the layer that owns it: any object with
    these three members may be sent, whichever package built it. Declared as
    read-only properties so an object exposing them as properties — which
    ``AgentTool`` does — satisfies it.
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict[str, Any]: ...


# The field names are read off the model rather than hardcoded so the two can
# never drift: adding a field to ToolDefinition would otherwise make define_tool
# reject it as "unknown", from a call site that looks perfectly correct.
_KNOWN_FIELDS: tuple[str, ...] = tuple(ToolDefinition.model_fields)
_REQUIRED_FIELDS: tuple[str, ...] = tuple(
    name for name, field in ToolDefinition.model_fields.items() if field.is_required()
)

# OpenAI's function-name constraint. A name outside this set is rejected by the
# API for the WHOLE request, so one bad tool takes down every completion with an
# opaque 400 that never mentions the tool. Names are also matched by exact string
# against `tool_calls[].function.name` coming back, so a space or a dot in the
# name means the returned call can never be routed to the tool that produced it.
# Deliberately NOT enforcing snake_case: SUBPHASE-0.0.md states it as a
# convention, and rejecting `wordCount` would be a rule the runtime does not have.
_WIRE_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _check_parameters_schema(parameters: Any) -> None:
    """Reject a `parameters` value that validate_tool_arguments cannot enforce.

    The checks here are bounded by what `_validate_json_schema` below actually
    reads — top-level `type`, `properties`, `required`. Anything deeper (nested
    schemas, `$ref`, `anyOf`, format keywords) is deliberately NOT inspected,
    because this module ignores it at call time too; validating it here would
    advertise an enforcement that does not exist. It also has to stay permissive
    enough to accept `pydantic.model_json_schema()` output verbatim, which emits
    `$ref`-only properties and extra keys like `title` and `$defs`.
    """
    if not isinstance(parameters, dict):
        raise TypeError(
            f"define_tool: 'parameters' must be a JSON Schema dict, got {type(parameters).__name__}"
        )

    # An OpenAI tool's top-level parameters schema is an object schema, always.
    # It matters beyond the wire format: `_validate_json_schema` reads `required`
    # and `properties` unconditionally, so a non-object schema (`{"type":
    # "string"}`) is not rejected at call time — it validates every call
    # vacuously, which is the silent pass this check exists to prevent.
    if parameters.get("type") != "object":
        raise ValueError(
            "define_tool: 'parameters' must be a JSON Schema object schema — "
            f'expected "type": "object", got {parameters.get("type")!r}'
        )

    # Required rather than defaulted: without `properties` the argument
    # validator has nothing to type-check against and passes anything the model
    # emits. A tool that takes no arguments spells that out as
    # {"type": "object", "properties": {}} — which is also what
    # pydantic.model_json_schema() emits for a field-less model.
    if "properties" not in parameters:
        raise ValueError(
            "define_tool: 'parameters' is missing 'properties'; a tool that takes "
            'no arguments declares {"type": "object", "properties": {}}'
        )
    properties = parameters["properties"]
    if not isinstance(properties, dict):
        raise TypeError(
            f"define_tool: 'parameters.properties' must be a dict, got {type(properties).__name__}"
        )
    for prop_name, prop_schema in properties.items():
        # `_validate_json_schema` calls .get("type") on each of these, so a
        # non-dict property blows up with an AttributeError mid-tool-call
        # instead of here.
        if not isinstance(prop_schema, dict):
            raise TypeError(
                f"define_tool: 'parameters.properties[{prop_name!r}]' must be a dict, "
                f"got {type(prop_schema).__name__}"
            )

    required = parameters.get("required", [])
    if not isinstance(required, list):
        raise TypeError(
            f"define_tool: 'parameters.required' must be a list, got {type(required).__name__}"
        )
    for entry in required:
        if not isinstance(entry, str):
            raise TypeError(
                f"define_tool: 'parameters.required' entries must be strings, "
                f"got {type(entry).__name__} ({entry!r})"
            )
        # A required name absent from `properties` is never described to the
        # model, so the model cannot know to send it, but the validator demands
        # it on every call — an unbreakable error loop at run time.
        if entry not in properties:
            raise ValueError(
                f"define_tool: 'parameters.required' names {entry!r}, which is not in "
                f"'properties' — the model is never told about it, so every call fails"
            )


@agent_facing(topic="tools")
def define_tool(definition: Mapping[str, Any] | None = None, /, **fields: Any) -> ToolDefinition:
    """Build a validated :class:`ToolDefinition`.

    The keyword form is the one to reach for::

        word_count = define_tool(
            name="word_count",
            label="Word count",
            description="Count the words in a string.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            execute=lambda text: {"words": len(text.split())},
        )

    A single mapping may also be passed positionally —
    ``define_tool({"name": ..., ...})`` — which is the shape this function
    originally advertised. Passing both forms, or neither, is a caller error.

    NOTE: this is not the shape ``tau_agent_core``'s
    ``ExtensionAPI.register_tool()`` takes. That one is a plain dict whose
    ``execute`` has the five-argument extension signature
    ``execute(tool_call_id, params, signal, on_update, ctx)``, and it defaults a
    missing ``label`` to ``name``. The two are separate contracts on purpose;
    do not feed the result of this function to ``register_tool``.

    Raises:
        TypeError: if both forms or neither form is used, if the positional
            argument is not a mapping, or if a field is present with the wrong
            Python type (``execute`` not callable, ``parameters`` not a dict).
        ValueError: if a required field is missing or empty, if an unknown field
            is passed, if ``name`` is not wire-safe, or if ``parameters`` is not
            a JSON Schema object schema this package can validate against.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
    """
    if definition is not None and fields:
        raise TypeError(
            "define_tool: pass EITHER a single mapping positionally OR keyword "
            f"fields, not both (got a mapping plus {sorted(fields)!r})"
        )
    if definition is None and not fields:
        raise TypeError(
            "define_tool: nothing to define — pass the fields as keywords "
            "(name=..., label=..., description=..., parameters=..., execute=...) "
            "or a single mapping positionally"
        )

    # The parameter is positional-only, so the old advertised call
    # `define_tool(definition={...})` now arrives as a field named "definition".
    # Checked here, before the missing-required-fields sweep, because otherwise
    # the caller is told five fields are missing rather than the one thing that
    # is actually wrong with the call.
    if "definition" in fields:
        raise ValueError(
            "define_tool: 'definition' is not a tool field — pass a mapping "
            "positionally, define_tool({...}), or pass the fields as keywords"
        )

    if definition is not None:
        if not isinstance(definition, Mapping):
            raise TypeError(
                f"define_tool: the positional argument must be a mapping of fields, "
                f"got {type(definition).__name__}"
            )
        # Non-string keys would otherwise reach `ToolDefinition(**values)` and
        # surface as an unhelpful "keywords must be strings" from the call
        # machinery, naming neither define_tool nor the offending key.
        bad_keys = [key for key in definition if not isinstance(key, str)]
        if bad_keys:
            raise TypeError(f"define_tool: field names must be strings, got {bad_keys!r}")
        values: dict[str, Any] = dict(definition)
    else:
        values = dict(fields)

    missing = [name for name in _REQUIRED_FIELDS if name not in values]
    if missing:
        raise ValueError(
            f"define_tool: missing required field(s) {missing!r}; "
            f"every tool needs {list(_REQUIRED_FIELDS)!r}"
        )

    # ToolDefinition is a pydantic model with the default `extra="ignore"`, so a
    # typo'd optional field (`prompt_snipet=...`) would be dropped without a
    # word and the prompt snippet would simply never appear. Catch it here
    # rather than loosening the model, which other code constructs directly.
    unknown = [name for name in values if name not in _KNOWN_FIELDS]
    if unknown:
        raise ValueError(
            f"define_tool: unknown field(s) {sorted(unknown)!r}; "
            f"ToolDefinition accepts {list(_KNOWN_FIELDS)!r}"
        )

    # `execute` is the entire point of a tool; a non-callable here fails at the
    # moment the model first calls it, which can be many turns after the mistake.
    if not callable(values["execute"]):
        raise TypeError(
            f"define_tool: 'execute' must be callable, got {type(values['execute']).__name__}"
        )

    _check_parameters_schema(values["parameters"])

    name = values["name"]
    if not isinstance(name, str):
        raise TypeError(f"define_tool: 'name' must be a string, got {type(name).__name__}")
    if not _WIRE_SAFE_NAME.match(name):
        raise ValueError(
            f"define_tool: 'name' {name!r} is not usable as a tool name — it must be "
            "1-64 characters of letters, digits, underscores or hyphens"
        )

    # An empty string satisfies pydantic's `str` but is useless where it lands:
    # a blank chip in the TUI, and a tool the model is given no reason to call.
    # `label` is required and is NEVER derived from `name` — see the docstring.
    for text_field in ("label", "description"):
        value = values[text_field]
        if not isinstance(value, str):
            raise TypeError(
                f"define_tool: {text_field!r} must be a string, got {type(value).__name__}"
            )
        if not value.strip():
            raise ValueError(f"define_tool: {text_field!r} must not be empty")

    # Everything left — prompt_snippet/prompt_guidelines types, the
    # execution_mode literal — is exactly what pydantic already checks, and
    # pydantic's ValidationError is a ValueError, so the contract above holds.
    return ToolDefinition(**values)


@agent_facing(topic="tools")
def validate_tool_arguments(tool: Any, tool_call: Any) -> dict[str, Any]:
    """Validate tool call arguments against tool schema.

    Uses the tool's JSON Schema (parameters field) to validate the
    arguments from a ToolCall. Raises ValueError if validation fails.

    Args:
        tool: Tool with a 'parameters' attribute (dict) and optionally 'name'.
        tool_call: ToolCall with an 'arguments' attribute (dict).

    Returns:
        dict: Validated parameter dict.

    Raises:
        ValueError: If arguments don't match the tool's JSON schema.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
    """
    schema = getattr(tool, "parameters", {})
    arguments = (
        getattr(tool_call, "arguments", {})
        if hasattr(tool_call, "arguments")
        else tool_call
        if isinstance(tool_call, dict)
        else {}
    )

    if isinstance(schema, dict):
        return _validate_json_schema(schema, arguments)
    return arguments
