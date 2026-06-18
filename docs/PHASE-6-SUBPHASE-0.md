# Phase 6 Subphase 0 — Data Contract Definition

> **Topic**: Finalize the RPC protocol and export types.

## Scope

This subphase locks the contracts for RPC communication and session export. These are self-contained within Phase 6.

## Done Criteria

The following types are defined:

1. **`tau_agent_core/rpc.py`**: RPC message types
2. **`tau_agent_core/export.py`**: Export format types

### RPC Message Format Contract

All messages are LF-delimited JSON:

```
{"jsonrpc": "2.0", "id": 1, "method": "send_prompt", "params": {"text": "hello"}}
{"jsonrpc": "2.0", "id": null, "method": "event", "params": {"type": "text_delta", "delta": "H"}}
{"jsonrpc": "2.0", "id": 1, "result": {"status": "done", "messages": [...]}}
```

### RPC Messages

```python
@dataclass
class RPCRequest:
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | None = None
    method: str  # "send_prompt", "send_tool_result", "get_commands", etc.
    params: dict | None = None

@dataclass
class RPCResponse:
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | None = None
    result: dict | None = None
    error: dict | None = None

@dataclass
class RPCEvent:
    jsonrpc: Literal["2.0"] = "2.0"
    method: Literal["event"] = "event"
    params: dict  # AgentEvent serialized as dict
```

### Export Formats

```python
@dataclass
class ExportConfig:
    format: Literal["markdown", "html"]
    include_tool_calls: bool = True
    include_thinking: bool = True
    include_timestamps: bool = False
```

## Reference

- `SUBPHASE-0.0.md` lines 260-340: AgentEvent contract (for RPC serialization)
- `docs/tau-coding-agent.md` lines 220-280: RPC mode design
- `docs/IMPLEMENTATION-PLAN.md` lines 460-500: RPC and export spec

## Success Signal

All types import. The RPC message format matches JSON-RPC 2.0 with LF-delimited framing. The export config has all required fields. This is the final contract before Phase 6 implementation.
