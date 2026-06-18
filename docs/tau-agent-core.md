# τ-agent-core Design

## Scope

τ-agent-core is the agent runtime: the loop that drives conversations, executes tools, manages sessions, and exposes the extension system. It is **TUI-agnostic** — no terminal UI, no Textual, no stdout/stdin. It can run headlessly (SDK), as a background process (RPC), or with any TUI.

## Package: `tau_agent_core`

```
src/tau_agent_core/
├── __init__.py
├── agent_loop.py          # Core turn loop
├── agent_session.py       # Session state + event subscription + prompt API
├── session_manager.py     # JSONL persistence, tree, fork, branch
├── compaction.py          # Context window management
├── system_prompt.py       # Prompt builder
├── tools/                 # Built-in tool implementations
│   ├── __init__.py
│   ├── base.py            # Tool base classes
│   ├── read.py
│   ├── write.py
│   ├── edit.py
│   ├── bash.py
│   ├── grep.py
│   ├── find.py
│   └── ls.py
├── extensions/            # Extension system
│   ├── __init__.py
│   ├── loader.py          # Discover/load Python extension modules
│   ├── registry.py        # Tool/command/event registration
│   ├── events.py          # Async event bus
│   └── types.py           # Extension API surface
├── sdk.py                 # Programmatic API (create_agent_session)
└── rpc.py                 # RPC server for external integration
```

## Module: agent_loop.py

The agent loop is a direct port of pi's `agent-loop.js` logic. It uses τ-ai for streaming and τ-agent-core types for everything else.

```python
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Any
from tau_ai.types import (
    Message, AssistantMessage, ToolResultMessage, UserMessage,
    TextContent, ToolCall, AgentTool, AgentToolResult,
    StopReason,
)

# ─── Event Types ───────────────────────────────────────────────

@dataclass
class AgentEvent:
    type: str
    # Fields vary by type:
    #   agent_start / agent_end: messages: list[AgentMessage]
    #   turn_start / turn_end: turn_index, message
    #   message_start/update/end: message
    #   tool_execution_start/update/end: tool_call_id, tool_name, args, result
    message: Message | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    args: dict[str, Any] | None = None
    result: Any = None
    is_error: bool = False
    turn_index: int = 0

EventSink = Callable[[AgentEvent], None | Awaitable[None]]

# ─── Tool Call Preparation ─────────────────────────────────────

@dataclass
class PreparedToolCall:
    tool_call: ToolCall
    tool: AgentTool
    args: Any  # validated parameters

@dataclass
class FinalizedToolCall:
    tool_call: ToolCall
    result: AgentToolResult
    is_error: bool

# ─── Tool Execution Result ─────────────────────────────────────

@dataclass
class ToolBatchResult:
    messages: list[ToolResultMessage]
    terminate: bool  # Should agent stop after this batch?

# ─── Config ────────────────────────────────────────────────────

@dataclass
class AgentLoopConfig:
    """Configuration for the agent loop."""
    model: Any  # τ-ai Model
    system_prompt: str
    tools: list[AgentTool] | None = None
    convert_to_llm: Callable[[list[Message]], list[dict]] | None = None
    transform_context: Callable[[list[Message]], Awaitable[list[Message]]] | None = None
    get_api_key: Callable[[str], Awaitable[str | None]] | None = None
    get_steering_messages: Callable[[], Awaitable[list[Message]]] | None = None
    get_follow_up_messages: Callable[[], Awaitable[list[Message]]] | None = None
    tool_execution_mode: Literal["sequential", "parallel"] = "parallel"
    before_tool_call: Callable[[PreparedToolCall], Awaitable[dict | None]] | None = None
    after_tool_call: Callable[[FinalizedToolCall], Awaitable[dict | None]] | None = None
    max_retries: int = 3
    reasoning_level: str = "off"
    max_tokens: int | None = None
    temperature: float = 0.7


# ─── Main Loop ─────────────────────────────────────────────────

class AgentLoop:
    """Core agent loop.

    Mirrors pi's agent-loop.js architecture.
    """

    def __init__(self, config: AgentLoopConfig, emit: EventSink):
        self.config = config
        self.emit = emit
        self._turn_index = 0
        self._abort_signal = None

    async def run(
        self,
        prompts: list[UserMessage],
        context: list[Message],
    ) -> list[Message]:
        """Run the agent loop for one or more prompts."""
        new_messages = []

        # Emit agent_start
        await self._emit(AgentEvent(type="agent_start"))

        # Add prompts to context and emit them
        all_context = list(context) + list(prompts)
        for prompt in prompts:
            await self._emit(AgentEvent(type="message_start", message=prompt))
            await self._emit(AgentEvent(type="message_end", message=prompt))
            new_messages.append(prompt)

        # Main loop
        new_messages = await self._run_loop(
            all_context,
            new_messages,
        )

        await self._emit(AgentEvent(type="agent_end", messages=new_messages))
        return new_messages

    async def _run_loop(
        self,
        context: list[Message],
        new_messages: list[Message],
    ) -> list[Message]:
        """Main iteration: LLM call → tool calls → repeat."""
        first_turn = True
        pending_messages = await self._get_pending_messages()

        while True:
            has_more_tool_calls = True

            while has_more_tool_calls or pending_messages:
                if not first_turn:
                    await self._emit(AgentEvent(type="turn_start", turn_index=self._turn_index))
                first_turn = False

                # Process pending messages
                for msg in pending_messages:
                    await self._emit(AgentEvent(type="message_start", message=msg))
                    await self._emit(AgentEvent(type="message_end", message=msg))
                    context.append(msg)
                    new_messages.append(msg)
                pending_messages = []

                # Stream assistant response
                assistant = await self._stream_response(context)
                new_messages.append(assistant)
                context.append(assistant)

                if assistant.stop_reason in ("error", "aborted"):
                    await self._emit(AgentEvent(
                        type="turn_end", message=assistant,
                        turn_index=self._turn_index,
                    ))
                    return new_messages

                # Check for tool calls
                tool_calls = [c for c in assistant.content if hasattr(c, 'type') and c.type == "toolCall"]
                tool_results = []

                if tool_calls:
                    batch = await self._execute_tool_calls(context, assistant, tool_calls)
                    tool_results = batch.messages
                    has_more_tool_calls = not batch.terminate

                    for result_msg in tool_results:
                        context.append(result_msg)
                        new_messages.append(result_msg)

                await self._emit(AgentEvent(
                    type="turn_end", message=assistant,
                    tool_results=tool_results,
                    turn_index=self._turn_index,
                ))
                self._turn_index += 1

                # Check for steering messages
                pending_messages = await self._get_pending_messages()

            # Agent would stop — check for follow-ups
            follow_ups = await self._get_follow_up_messages()
            if follow_ups:
                pending_messages = follow_ups
                continue
            break

        return new_messages

    async def _stream_response(self, context: list[Message]) -> AssistantMessage:
        """Stream assistant response from LLM."""
        # Transform context
        messages = context
        if self.config.transform_context:
            messages = await self.config.transform_context(messages)

        # Convert to LLM format
        llm_messages = self.config.convert_to_llm(messages) if self.config.convert_to_llm else messages

        # Resolve API key
        api_key = None
        if self.config.get_api_key:
            api_key = await self.config.get_api_key(self.config.model.provider)

        # Stream from provider
        from tau_ai import stream_simple
        stream = await stream_simple(
            self.config.model,
            {
                "system_prompt": self.config.system_prompt,
                "messages": llm_messages,
                "tools": [self._to_llm_tool(t) for t in (self.config.tools or [])] if self.config.tools else None,
            },
            {
                "api_key": api_key,
                "reasoning": self.config.reasoning_level,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "signal": self._abort_signal,
            },
        )

        partial = None
        async for event in stream:
            if hasattr(event, 'type') and event.type == "text_delta":
                # Emit streaming update
                await self._emit(AgentEvent(
                    type="message_update",
                    message=event.partial,
                ))
            elif hasattr(event, 'type') and event.type == "toolcall_delta":
                await self._emit(AgentEvent(
                    type="message_update",
                    message=event.partial,
                ))

        final = await stream.result()
        return final

    async def _execute_tool_calls(
        self,
        context: list[Message],
        assistant: AssistantMessage,
        tool_calls: list[ToolCall],
    ) -> ToolBatchResult:
        """Execute tool calls from assistant message."""
        is_sequential = any(
            t.name == tc.name and t.execution_mode == "sequential"
            for tc in tool_calls
            for t in (self.config.tools or [])
        )

        if is_sequential or self.config.tool_execution_mode == "sequential":
            return await self._execute_sequential(context, assistant, tool_calls)
        return await self._execute_parallel(context, assistant, tool_calls)

    async def _execute_sequential(self, context, assistant, tool_calls) -> ToolBatchResult:
        """Execute tool calls sequentially."""
        finalized = []
        messages = []

        for tc in tool_calls:
            await self._emit(AgentEvent(
                type="tool_execution_start",
                tool_call_id=tc.id,
                tool_name=tc.name,
                args=tc.arguments,
            ))

            prep = await self._prepare_tool_call(context, assistant, tc)
            if prep.kind == "blocked":
                finalized.append(FinalizedToolCall(
                    tool_call=tc,
                    result=AgentToolResult(content=[TextContent(text=prep.reason)], details={}),
                    is_error=True,
                ))
            elif prep.kind == "error":
                finalized.append(FinalizedToolCall(
                    tool_call=tc,
                    result=AgentToolResult(content=[TextContent(text=prep.error)], details={}),
                    is_error=True,
                ))
            else:
                result = await self._execute_tool(prep.tool_call, prep.tool, prep.args)
                result = await self._apply_after_hooks(result)
                finalized.append(result)

            await self._emit(AgentEvent(
                type="tool_execution_end",
                tool_call_id=tc.id,
                tool_name=tc.name,
                result=result.result,
                is_error=result.is_error,
            ))

            tool_result_msg = ToolResultMessage(
                role="toolResult",
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=result.result.content,
                details=result.result.details,
                is_error=result.is_error,
                timestamp=int(asyncio.get_event_loop().time() * 1000),
            )
            await self._emit(AgentEvent(type="message_start", message=tool_result_msg))
            await self._emit(AgentEvent(type="message_end", message=tool_result_msg))
            messages.append(tool_result_msg)

        terminate = len(finalized) > 0 and all(r.result.terminate for r in finalized)
        return ToolBatchResult(messages=messages, terminate=terminate)

    async def _prepare_tool_call(self, context, assistant, tool_call) -> dict:
        """Prepare a tool call: validate, run before hooks."""
        tool = next((t for t in (self.config.tools or []) if t.name == tool_call.name), None)
        if not tool:
            return {"kind": "error", "error": f"Tool '{tool_call.name}' not found"}

        # Validate arguments
        try:
            args = validate_tool_arguments(tool, tool_call)
        except (ValueError, Exception) as e:
            return {"kind": "error", "error": str(e)}

        # Before tool call hooks (extensions)
        if self.config.before_tool_call:
            result = await self.config.before_tool_call(PreparedToolCall(
                tool_call=tool_call, tool=tool, args=args,
            ))
            if result and result.get("block"):
                return {"kind": "blocked", "reason": result.get("reason", "Tool execution blocked")}

        return {"kind": "prepared", "tool_call": tool_call, "tool": tool, "args": args}

    async def _execute_tool(self, tool_call, tool, args) -> FinalizedToolCall:
        """Execute a single tool."""
        try:
            result = await tool.execute(
                tool_call.id,
                args,
                self._abort_signal,
                lambda partial: asyncio.create_task(
                    self._emit(AgentEvent(
                        type="tool_execution_update",
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        args=tool_call.arguments,
                        result=partial,
                    ))
                ),
            )
            return FinalizedToolCall(tool_call=tool_call, result=result, is_error=False)
        except Exception as e:
            return FinalizedToolCall(
                tool_call=tool_call,
                result=AgentToolResult(content=[TextContent(text=str(e))], details={}),
                is_error=True,
            )

    async def _apply_after_hooks(self, finalized) -> FinalizedToolCall:
        """Apply after tool call hooks (extensions can modify result)."""
        if self.config.after_tool_call:
            try:
                override = await self.config.after_tool_call(finalized)
                if override:
                    finalized = FinalizedToolCall(
                        tool_call=finalized.tool_call,
                        result=AgentToolResult(
                            content=override.get("content", finalized.result.content),
                            details=override.get("details", finalized.result.details),
                            terminate=override.get("terminate", False),
                        ),
                        is_error=override.get("is_error", finalized.is_error),
                    )
            except Exception as e:
                finalized = FinalizedToolCall(
                    tool_call=finalized.tool_call,
                    result=AgentToolResult(content=[TextContent(text=f"Error in after hook: {e}")], details={}),
                    is_error=True,
                )
        return finalized
```

## Module: agent_session.py

```python
import asyncio
from typing import Callable, Any

class AgentSession:
    """High-level session API. Combines agent loop, session manager, and events.

    This is the primary entry point for both SDK and TUI usage.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        model: Any,
        system_prompt: str = "",
        tools: list[AgentTool] | None = None,
        extensions: list[Callable] | None = None,
    ):
        self._session_manager = session_manager
        self._model = model
        self._system_prompt = system_prompt
        self._tools = tools or []
        self._events = EventBus()
        self._extensions = []
        self._is_streaming = False
        self._abort_controller = asyncio.get_event_loop()

        # Register extensions
        for ext in (extensions or []):
            self._extensions.append(ext)

    @property
    def messages(self) -> list[Message]:
        """Current conversation messages."""
        return self._session_manager.get_active_messages()

    @property
    def state(self):
        """Read-only access to session state."""
        return SessionState(self)

    def subscribe(self, handler: Callable[[AgentEvent], None]) -> Callable[[], None]:
        """Subscribe to agent events. Returns unsubscribe function."""
        return self._events.on("all", handler)

    async def prompt(self, text: str, images: list[dict] | None = None) -> list[Message]:
        """Send a prompt and run the agent loop."""
        # ... create UserMessage, run loop, append to session
        pass

    async def compact(self, custom_instructions: str | None = None):
        """Manually trigger compaction."""
        pass

    def abort(self):
        """Abort the current agent turn."""
        if hasattr(self._abort_controller, 'abort'):
            self._abort_controller.abort()
```

## Module: session_manager.py

Direct port of pi's session management with JSONL persistence and tree structure.

```python
import json
import uuid
import time
from pathlib import Path
from typing import Iterator

class SessionManager:
    """Manage session files with tree structure."""

    def __init__(self, cwd: str | None = None, sessions_dir: str | None = None):
        self.cwd = cwd or os.getcwd()
        self._sessions_dir = sessions_dir or self._default_dir()

    @classmethod
    def in_memory(cls, cwd: str | None = None) -> SessionManager:
        """Create an in-memory session manager (no file persistence)."""
        mgr = cls(cwd)
        mgr._memory_store = []
        return mgr

    def new_session(self, model_id: str | None = None) -> str:
        """Create a new session file. Returns session path."""
        pass

    def load(self, session_path: str) -> SessionState:
        """Load a session from a JSONL file."""
        pass

    def save(self, state: SessionState) -> None:
        """Save session state to JSONL file."""
        pass

    def append_entry(self, entry: dict) -> None:
        """Append an entry to the current session file."""
        pass

    def get_active_messages(self) -> list[Message]:
        """Get messages for the current active path (respects tree/compaction)."""
        pass

    def list(self) -> list[SessionInfo]:
        """List sessions for the current working directory."""
        pass

    def list_all(self) -> list[SessionInfo]:
        """List all sessions across all directories."""
        pass

    def fork(self, entry_id: str, position: Literal["before", "at"] = "before") -> str:
        """Create a new session from a specific entry. Returns new session path."""
        pass

    def clone(self, entry_id: str) -> str:
        """Duplicate the active path at entry_id into a new session."""
        pass

    def navigate(self, entry_id: str) -> SessionState:
        """Navigate to a specific entry in the tree."""
        pass
```

## Module: extensions/

### events.py — Event Bus

```python
import asyncio
from typing import Callable, Any

class EventBus:
    """Async-safe event bus for agent events."""

    def __init__(self):
        self._listeners: dict[str, list[Callable]] = {}

    def on(self, channel: str, handler: Callable) -> Callable[[], None]:
        """Subscribe to a channel. Returns unsubscribe function."""
        safe_handler = asyncio.create_task(self._safe_call(handler))
        # ... store handler
        return lambda: self._listeners[channel].remove(handler)

    async def emit(self, event: AgentEvent) -> None:
        """Emit an event to all listeners."""
        handlers = self._listeners.get(event.type, [])
        for handler in handlers:
            await self._safe_call(handler, event)

    async def _safe_call(self, handler, *args):
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(*args)
            else:
                handler(*args)
        except Exception as e:
            print(f"Event handler error ({args[0].type if args else '?'}): {e}")
```

### loader.py — Extension Discovery

```python
from pathlib import Path
import importlib

class ExtensionLoader:
    """Discover and load Python extension modules."""

    EXTENSION_DIRS = [
        Path.home() / ".tau" / "extensions",
        Path.cwd() / ".tau" / "extensions",
    ]

    @classmethod
    def discover(cls) -> list[Path]:
        """Find all extension files."""
        extensions = []
        for ext_dir in cls.EXTENSION_DIRS:
            if not ext_dir.exists():
                continue
            for path in sorted(ext_dir.rglob("*.py")):
                if path.name == "__init__.py" or path.name.endswith(".py"):
                    extensions.append(path)
            # Also find directory-based extensions
            for subdir in ext_dir.iterdir():
                if subdir.is_dir() and (subdir / "__init__.py").exists():
                    extensions.append(subdir)
        return extensions

    @classmethod
    def load(cls, path: Path):
        """Load an extension module and call its factory function."""
        if path.is_dir():
            module_path = str(path / "__init__.py")
        else:
            module_path = str(path)

        # Import the module
        spec = importlib.util.spec_from_file_location(f"tau_ext_{path.stem}", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Call the factory function
        factory = module.register  # or module.default if it's the old style
        return factory
```

### types.py — Extension API

```python
from typing import Any, Callable

class ExtensionContext:
    """Context provided to extension handlers and tools."""

    @property
    def ui(self) -> ExtensionUI:
        """UI methods (TUI-only)."""
        ...

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    @property
    def signal(self) -> AbortSignal | None:
        return self._signal

class ExtensionUI:
    """UI interaction methods for extensions (TUI only)."""

    async def confirm(self, title: str, message: str) -> bool: ...
    async def select(self, title: str, items: list[str]) -> str | None: ...
    async def input(self, title: str, default: str = "") -> str: ...
    def notify(self, message: str, level: str = "info"): ...
```
