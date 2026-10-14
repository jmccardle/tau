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


ToolExposure = Literal["direct", "model-only", "callable", "deferred", "hidden"]
"""Who may see and call a tool (experimental; experiments/tool-exposure/RESULTS.md).

- ``direct``: declared to the model, callable from a tool or script.
- ``model-only``: declared to the model, never callable from a tool or script.
- ``callable``: not declared to the model, callable from a tool or script.
- ``deferred``: not declared until something loads it, callable meanwhile.
- ``hidden``: neither.
"""

TOOL_EXPOSURES: tuple[str, ...] = ("direct", "model-only", "callable", "deferred", "hidden")


@agent_facing(topic="tools")
class ToolDefinition(BaseModel):
    """Tool definition for the LLM API. Build one with :func:`define_tool`.

    ``exposure``, ``namespace``, ``tags`` and ``output_schema`` are experimental
    (experiments/tool-exposure/RESULTS.md). None of them reaches the model:
    the wire carries ``name``, ``description`` and ``parameters`` only.

    Attributes:
        exposure: The author's default :data:`ToolExposure`; a session may
            override it per cursor.
        namespace: ``"core"`` for a built-in, the declaring extension's name
            (or ``<extension>.<sub>``) for an extension tool.
        tags: Free labels for discovery.
        output_schema: JSON Schema of the structured value a nested caller
            receives, or ``None`` when the tool returns text only.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.

    Attributes:
        name: The name the model calls; wire-safe (``[A-Za-z0-9_-]``, at most 64).
        label: Human-readable name for display.
        description: What the model is told the tool does.
        parameters: JSON Schema object schema for the arguments.
        execute: The implementation. tau_llm stores it and never calls it; the
            calling convention belongs to whoever runs the tool.
        prompt_snippet: The tool's line in the system prompt's tool list;
            ``None`` renders no line.
        prompt_guidelines: Usage notes rendered under the snippet.
        execution_mode: ``"sequential"`` forces the whole batch of calls it
            appears in to run one at a time in tau_agent_core's loop.
    """

    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    execute: ToolExecute
    prompt_snippet: str | None = None
    prompt_guidelines: list[str] | None = None
    execution_mode: Literal["sequential", "parallel"] = "parallel"
    exposure: ToolExposure = "direct"
    namespace: str | None = None
    tags: list[str] = []
    output_schema: dict[str, Any] | None = None


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
    def name(self) -> str:
        """The name the model calls."""
        ...

    @property
    def description(self) -> str:
        """What the model is told the tool does."""
        ...

    @property
    def parameters(self) -> dict[str, Any]:
        """JSON Schema object schema for the arguments."""
        ...


_KNOWN_FIELDS: tuple[str, ...] = tuple(ToolDefinition.model_fields)
_REQUIRED_FIELDS: tuple[str, ...] = tuple(
    name for name, field in ToolDefinition.model_fields.items() if field.is_required()
)

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

    if parameters.get("type") != "object":
        raise ValueError(
            "define_tool: 'parameters' must be a JSON Schema object schema — "
            f'expected "type": "object", got {parameters.get("type")!r}'
        )

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

    A single mapping may be passed positionally instead
    (``define_tool({"name": ..., ...})``), but not both forms.

    Not the shape ``ExtensionAPI.register_tool()`` takes: that is a plain dict
    whose ``execute`` has the five-argument extension signature
    ``execute(tool_call_id, params, signal, on_update, ctx)``. Do not pass this
    function's result to ``register_tool``.

    Args:
        definition: The fields as a single mapping, positional-only.
        fields: The fields as keywords — the :class:`ToolDefinition` attributes.

    Returns:
        The validated definition.

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

    unknown = [name for name in values if name not in _KNOWN_FIELDS]
    if unknown:
        raise ValueError(
            f"define_tool: unknown field(s) {sorted(unknown)!r}; "
            f"ToolDefinition accepts {list(_KNOWN_FIELDS)!r}"
        )

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

    for text_field in ("label", "description"):
        value = values[text_field]
        if not isinstance(value, str):
            raise TypeError(
                f"define_tool: {text_field!r} must be a string, got {type(value).__name__}"
            )
        if not value.strip():
            raise ValueError(f"define_tool: {text_field!r} must not be empty")

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
