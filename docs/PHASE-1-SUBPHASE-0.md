# Phase 1 Subphase 0 — Data Contract Definition

> **Topic**: Finalize and lock the types, tool definitions, and streaming protocol that τ-agent-core will consume.

## Scope

This subphase writes the **final, immutable** type definitions in `tau-ai/src/tau_ai/types.py`, `tau-ai/src/tau_ai/tools.py`, `tau-ai/src/tau_ai/abort.py`, and the provider interface in `tau-ai/src/tau_ai/providers/base.py`. No subsequent phase will modify these types — only consume them.

## Done Criteria

The following files are written with full type signatures and docstrings:

1. **`tau_ai/types.py`**: All message types, content blocks, model types (see `SUBPHASE-0.0.md` lines 120-160)
2. **`tau_ai/tools.py`**: `ToolDefinition`, `define_tool()`, `validate_tool_arguments()`, `validate_tool_parameters()`
3. **`tau_ai/abort.py`**: `AbortSignal` class (see `SUBPHASE-0.0.md` lines 160-180)
4. **`tau_ai/providers/base.py`**: `Provider` ABC with `stream_chat()` method signature
5. **`tau_ai/providers/registry.py`**: `Registry` with `register()`, `get()`, `list_all()`
6. **`tau_ai/client.py`**: `stream_simple()` function signature (implementation is subphase 1.3)
7. **`tau_ai/__init__.py`**: Public exports

### Key Contract Points to Document

For each type, write a docstring that includes:
- A one-line description
- A short example
- Any constraints (e.g., "tool names must be globally unique", "timestamps are UTC ms since epoch")

### Cross-Phase Reference

- `SUBPHASE-0.0.md` lines 120-260: all type contracts
- `MONOREPO-STRUCTURE.md` lines 26-32: import graph

### Testing

```python
# These tests verify the TYPE CONTRACT, not implementation:

# 1. All types import correctly
from tau_ai.types import UserMessage, AssistantMessage, ToolResultMessage, TextContent, ImageContent, ToolCall, ThinkingContent
from tau_ai.tools import ToolDefinition, define_tool, validate_tool_arguments
from tau_ai.abort import AbortSignal
from tau_ai.providers.base import Provider
from tau_ai.providers.registry import Registry
from tau_ai.client import stream_simple

# 2. Messages are dataclasses (or equivalent)
import dataclasses
assert dataclasses.is_dataclass(UserMessage)
assert dataclasses.is_dataclass(AssistantMessage)
assert dataclasses.is_dataclass(ToolResultMessage)

# 3. ToolDefinition has the right fields
import tau_ai.tools as tools
sig = inspect.signature(tools.ToolDefinition)
expected_fields = {"name", "label", "description", "parameters", "execute", "prompt_snippet", "prompt_guidelines", "execution_mode"}
actual_fields = set(sig.parameters.keys())
assert expected_fields == actual_fields

# 4. AbortSignal has the right methods
sig = inspect.signature(AbortSignal.is_aborted)
assert sig.parameters == {}  # no arguments
sig = inspect.signature(AbortSignal.abort)
assert sig.parameters == {}

# 5. Provider ABC has stream_chat
import inspect
sig = inspect.signature(Provider.stream_chat)
# Should have: model, messages, tools, options
params = list(sig.parameters.keys())
assert "model" in params
assert "messages" in params

# 6. Registry is a singleton or factory
assert isinstance(Registry(), Registry)
```

### Success Signal

An agent reading `tau_ai/__init__.py` can see exactly what types are exported. An agent reading `SUBPHASE-0.0.md` can verify that every type in the contract exists with the right signature in the source code. All 6 import assertions pass. The type signatures are the single source of truth for Phase 2.
