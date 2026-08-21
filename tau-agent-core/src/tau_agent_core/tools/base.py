"""τ-agent-core tools: Tool definitions, AgentTool wrapper, and batch results.

Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.

Types:
- ToolDefinition: Raw tool definition (from tau-llm or extensions)
- AgentTool: Validated tool wrapper used by the agent loop
- AgentToolResult: Result from a single tool execution
- ToolBatchResult: Result from a batch of tool executions

Constraints:
- Tool names must be globally unique across all sources
- Tool arguments are validated against JSON Schema
"""

from __future__ import annotations

from typing import Any, Callable, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from tau_llm.tools import ToolDefinition as LlmToolDefinition


class ToolDefinition(LlmToolDefinition):
    """The runtime tool definition: :class:`tau_llm.tools.ToolDefinition` plus
    name-based identity.

    It SUBCLASSES the tau-llm model rather than restating its fields. It used to
    restate them — eight fields duplicated verbatim in two packages — and the copy
    was not harmless: ``define_tool()`` returns the tau-llm class, and pydantic
    compares by class identity, so the value τ's own public builder produced could
    not be handed to :class:`AgentTool`. Two identical shapes that refuse each
    other is a worse failure than one shape, because it looks like it should work.

    pi has the same layering and expresses it the same way -- ``AgentTool extends
    Tool`` (``packages/agent/src/types.ts:366``) over ``packages/ai``'s ``Tool``.
    τ had copied the fields instead of the relationship.

    Adds only identity: two definitions with one name ARE the same tool, because
    the name is what the model calls and what the registry keys on. Field-by-field
    equality (pydantic's default, which the base keeps) would call two wrappers
    around the same tool different whenever a closure differs.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
    """

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ToolDefinition):
            return False
        return self.name == other.name


class ExtensionToolDefinition(ToolDefinition):
    """A tool an extension registered, as the registry holds it.

    Third and last member of the hierarchy. It used to be a bare ``dict`` passed
    to ``api.register_tool()`` and stored as a dict, which meant the one shape
    crossing the extension boundary was the one shape with no schema — nothing to
    validate against, nothing to generate documentation from, and four separate
    ``defn["key"]`` / ``defn.get("key", default)`` readers each deciding for
    themselves what was required. A missing key surfaced as a ``KeyError`` from
    inside the agent loop, one turn after the mistake.

    ``api.register_tool()`` still accepts the plain dict, so pi parity and every
    existing extension are unaffected; the dict is validated into this model at
    the boundary instead of being carried raw.

    Two differences from the parent, both real rather than cosmetic:

    ``source`` — who registered it, for the ``/extensions`` surface. Default
    ``"built-in"``, matching what :meth:`ExtensionRegistry.get_all_tools` reported
    before; ``ExtensionAPI.register_tool`` sets ``"extension"``. Accepted under
    its historical ``_source`` spelling too, because that is the key extensions
    and tests already write.

    ``execute`` — the EXTENSION signature, ``execute(tool_call_id, params, signal,
    on_update, ctx)``, five arguments with the bound ``ExtensionContext`` last. The
    parent's is the loop's four-argument form. Nothing here can enforce that
    difference — both are ``Callable`` and a decorated or ``*args`` wrapper has no
    inspectable arity — so ``AgentSession._resolve_extension_tools`` adapts one to
    the other, and this docstring is where the difference is stated.
    """

    source: str = Field(
        default="built-in",
        validation_alias=AliasChoices("source", "_source"),
        serialization_alias="_source",
    )

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _label_defaults_to_name(cls, data: Any) -> Any:
        """``label`` is optional here and only here.

        The parent requires it, and ``define_tool()`` deliberately refuses to
        derive it — a human label invented from a wire identifier is exactly the
        fabricated default the house rule forbids. This is not that: defaulting
        ``label`` to ``name`` is ``register_tool``'s own documented contract and
        pi's behaviour (``coding-agent/src/core/extensions/types.ts``), so an
        extension written against either already relies on it. Removing it would
        break every existing extension to enforce a rule its API never made.
        """
        if isinstance(data, dict) and "label" not in data and "name" in data:
            data = {**data, "label": data["name"]}
        return data


class AgentTool(BaseModel):
    """Validated tool wrapper used by the agent loop.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.

    Wraps a ToolDefinition with validated name alias.
    The agent loop works with AgentTool (validated, wrapped),
    while extensions register with ToolDefinition (raw, unvalidated).

    ``definition`` is annotated with the tau-llm BASE class deliberately, so every
    member of the hierarchy fits: the runtime :class:`ToolDefinition`, an
    :class:`ExtensionToolDefinition`, and a bare
    :class:`tau_llm.tools.ToolDefinition` straight out of ``define_tool()``.
    Narrowing it to the subclass is what made ``AgentTool(definition=define_tool(
    ...))`` raise "Input should be a valid dictionary or instance of
    ToolDefinition" against a value of an identically-shaped class.

    Attributes:
        definition: The underlying ToolDefinition
        name: Alias for definition.name
        execute: Alias for definition.execute
        parameters: Alias for definition.parameters
        description: Alias for definition.description
        execution_mode: Alias for definition.execution_mode
    """

    definition: LlmToolDefinition

    @property
    def name(self) -> str:
        """Alias for definition.name."""
        return self.definition.name

    @property
    def execute(self) -> Callable[..., Any]:
        """Alias for definition.execute."""
        return self.definition.execute

    @property
    def parameters(self) -> dict[str, Any]:
        """Alias for definition.parameters."""
        return self.definition.parameters

    @property
    def description(self) -> str:
        """Alias for definition.description."""
        return self.definition.description

    @property
    def execution_mode(self) -> Literal["sequential", "parallel"]:
        """Alias for definition.execution_mode."""
        return self.definition.execution_mode

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AgentTool):
            return False
        return self.name == other.name


class AgentToolResult(BaseModel):
    """Result from a single tool execution.

    Attributes:
        tool_name: Name of the executed tool
        tool_call_id: ID of the tool call
        content: List of content blocks (mirrors Message content)
        is_error: Whether the execution failed
        error_message: Error description (if is_error=True)
        terminate: Whether the agent loop should terminate after this tool
    """

    tool_name: str
    tool_call_id: str | None = None
    content: list[dict[str, Any]] = Field(default_factory=list)
    is_error: bool = False
    error_message: str | None = None
    terminate: bool = False

    @classmethod
    def from_error(
        cls, tool_name: str, error_message: str, tool_call_id: str | None = None
    ) -> "AgentToolResult":
        """Create a failure result."""
        return cls(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=[{"type": "text", "text": error_message}],
            is_error=True,
            error_message=error_message,
        )


class ToolBatchResult(BaseModel):
    """Result from a batch of tool executions.

    Returned by the agent loop after executing a batch of tool calls.

    Attributes:
        messages: List of messages produced by the tool executions
        tool_results: Individual tool execution results
        terminate: Whether the agent loop should terminate
    """

    messages: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[AgentToolResult] = Field(default_factory=list)
    terminate: bool = False

    def __bool__(self) -> bool:
        return not self.terminate
