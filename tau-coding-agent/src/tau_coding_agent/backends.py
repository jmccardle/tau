"""
Backend abstraction layer for tau-coding-agent.

Wraps tau-agent-core's AgentSession to provide Parley-compatible
Backend interfaces (chat, stream_chat).

Reference: SESSION-TREE-IMPLEMENTATION.md §2.6 (throwaway SessionManager retired;
AgentSession runs against a scratch InMemorySessionLog, caller owns persistence).
"""

from abc import ABC, abstractmethod
from typing import Any, Callable
from tau_ai.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.sdk import _resolve_tools


class Backend(ABC):
    """Abstract base class for LLM backends."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.model = config.get("model", "")

    @abstractmethod
    async def chat(self, messages: list[dict]) -> tuple[str, dict, list[dict]]:
        """Return (assistant_text, usage, new_messages)."""

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[dict],
        callback: Callable[[str], None],
        on_event: Callable[[dict], None] | None = None,
    ) -> tuple[str, dict, list[dict], list[dict]]:
        """Return (assistant_text, usage, new_messages, tool_calls)."""

    @abstractmethod
    def abort(self) -> None:
        """Cooperatively abort the in-flight turn (LLM stream + tool loop).

        Safe to call when nothing is running. The TUI binds this to Esc so a
        long response can be cancelled mid-stream; the active ``stream_chat``
        returns with whatever streamed so far."""


class TauBackend(Backend):
    """tau-agent-core backend adapter.

    Wraps tau-agent-core's AgentSession to provide Parley-compatible
    chat/stream_chat interfaces.
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)

        # Build a tau-agent-core model config from the Parley config
        model_id = config.get("model", "gpt-4")
        base_url = config.get("base_url", "https://api.openai.com/v1")
        api_key = config.get("api_key", "not-needed")
        backend_type = config.get("backend", "openai").lower()

        # Map provider name (Parley's "backend" field) to tau-agent-core provider
        provider_map = {
            "openai": "openai",
            "anthropic": "anthropic",
            "gemini": "gemini",
        }
        provider = provider_map.get(backend_type, backend_type)

        self.model_name = model_id
        self.system_prompt = config.get("system_prompt", "")

        # Thinking/reasoning level. The CLI's --thinking flag (or a model:level
        # suffix) lands here as config["thinking"]; a model config entry may also
        # declare a default "thinking" level and a "thinking_level_map". A
        # non-"off" level asserts the model is reasoning-capable (mirrors pi
        # model-resolver.ts:496), so reasoning_effort is actually sent; an
        # explicit config "reasoning": true also enables it. None/"off" → no
        # reasoning requested.
        thinking_level = config.get("thinking")
        reasoning_arg = thinking_level if thinking_level and thinking_level != "off" else None
        model_reasoning = bool(config.get("reasoning")) or reasoning_arg is not None

        model = Model(
            id=model_id,
            name=model_id,
            api="openai-completions",
            provider=provider,
            base_url=base_url,
            context_window=128000,
            max_tokens=4096,
            reasoning=model_reasoning,
            thinking_level_map=config.get("thinking_level_map"),
        )

        # Discover tools from config. Defaults to all built-in tools.
        tool_names = config.get("tools", ["read", "write", "edit", "bash", "ls", "grep", "find"])
        if tool_names:
            tools = _resolve_tools(tool_names)
        else:
            tools = []

        # Forward the configured API key to the session -> agent loop ->
        # provider. The provider requires a truthy key (Fail-Early); local
        # servers use the "not-needed" sentinel, which is passed through as-is.
        # (Previously this was stashed in an unused self._api_key and dropped,
        # so a real-OpenAI key from config never reached the provider.)
        #
        # SessionLog: the caller (TUI ``app.py`` / ``headless.py``) owns the
        # persistence-of-record — the coding-agent ``Session`` it appends each
        # produced message to, and the ``self.messages`` it passes as context.
        # This AgentSession therefore runs against a scratch ``InMemorySessionLog``
        # (never flushed, never read: context arrives via ``stream_chat``'s
        # ``messages`` argument). This retires the former throwaway ``SessionManager``
        # (System A) whose appends went to a *second* on-disk file nobody read — so
        # there is now a single live on-disk persistence path (§2.6, §4.5).
        #
        # Auto-compaction is disabled here: the caller's own message list — not this
        # log — is the context sent to the model, so a post-turn auto-compaction on
        # the scratch log would do useless work (and fire a slow summary LLM call
        # every turn once it crossed the threshold). The TUI compacts explicitly via
        # ``/compact`` → ``compact_messages``, which works with auto-compaction off.
        self.agent_session = AgentSession(
            session_log=InMemorySessionLog(),
            model=model,
            system_prompt=self.system_prompt,
            tools=tools,
            api_key=api_key,
            reasoning=reasoning_arg,
            compaction_settings=CompactionSettings(enabled=False),
        )

    def abort(self) -> None:
        """Abort the current turn by tripping the AgentSession's abort signal.

        The signal is threaded down to the provider (agent_loop forwards it to
        ``stream_simple``), which polls it per SSE line and stops the stream — so
        an in-flight completion ends promptly instead of draining in full."""
        self.agent_session.abort()

    async def compact_messages(self, messages: list[dict]) -> list[dict] | None:
        """Compact the conversation the TUI sends, returning the shortened list.

        Delegates to the AgentSession's compaction engine. Operates on the
        caller's ``messages`` (the TUI's authoritative ``current_chat.messages``,
        which ``stream_chat`` passes as the LLM context) — not the parallel
        session-manager path. Returns None when there is nothing to compact.
        """
        return await self.agent_session.compact_messages(messages)

    async def _extract_last_user_message(self, messages: list[dict]) -> str:
        """Extract the last user message text from a Parley messages list."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    return "\n".join(text_parts)
        return ""

    async def chat(self, messages: list[dict]) -> tuple[str, dict, list[dict]]:
        """Send a chat completion via tau-agent-core's AgentSession.

        Passes all messages as context so the agent loop has full
        conversation history (system prompt, prior assistant/tool results).
        Returns (assistant_text, usage, new_messages).
        """
        # Extract the last user message text
        last_user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_message = content
                elif isinstance(content, list):
                    # Multi-modal: extract text blocks
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    last_user_message = "\n".join(text_parts)
                break

        if not last_user_message:
            return "", {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}, []

        # Send through the agent loop with full conversation context
        # so the model sees prior tool calls and results
        result_messages = await self.agent_session.prompt(last_user_message, context=messages)

        # Extract the last assistant message text
        assistant_content = ""
        for msg in reversed(result_messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    assistant_content = "\n".join(text_parts)
                elif isinstance(content, str):
                    assistant_content = content
                break

        # Approximate token count
        prompt_tokens = sum(len(m.get("content", "")) // 4 for m in messages)
        completion_tokens = len(assistant_content) // 4 if assistant_content else 0

        return (
            assistant_content,
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            result_messages,
        )

    async def stream_chat(
        self,
        messages: list[dict],
        callback: Callable[[str], None],
        on_event: Callable[[dict], None] | None = None,
    ) -> tuple[str, dict, list[dict], list[dict]]:
        """Stream a chat completion via tau-agent-core's AgentSession.

        Passes ALL messages as context so the agent loop has full
        conversation history (system prompt, prior assistant/tool results).
        Returns (assistant_text, usage, new_messages, tool_calls).

        Two consumer channels are driven from the agent-core event bus:

        - ``callback(delta)`` receives raw assistant text fragments, for the
          streaming-text widget (unchanged contract).
        - ``on_event(event)`` (optional) receives *normalized, ordered*
          lifecycle events so the caller can mount/resolve widgets in true
          arrival order. Event shapes (all dicts with a ``"kind"`` key)::

              {"kind": "turn_start", "turn_index": int}
              {"kind": "text_delta", "delta": str}
              {"kind": "tool_call", "id": str, "name": str, "arguments": dict}
              {"kind": "tool_result", "id": str, "name": str,
               "result": str, "is_error": bool}

        Tool widgets are driven off ``tool_execution_start`` /
        ``tool_execution_end`` (which carry name/args/result directly),
        NOT off ``message_end`` toolCall blocks — the agent loop emits
        ``message_end`` twice per tool-bearing turn, so consuming it for
        rendering would duplicate. ``message_end`` is used only to harvest
        ``tool_calls_info`` for chat persistence (deduplicated by id).
        """
        # Extract the last user message text (same as chat())
        last_user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_message = content
                elif isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    last_user_message = "\n".join(text_parts)
                break

        if not last_user_message:
            return "", {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}, [], []

        # Capture streaming chunks — track the last accumulated text
        # per assistant message (reset on message_start for multi-turn loops)
        streaming_text: str = ""
        streaming_reasoning: str = ""
        streaming_chunks: list[str] = []

        # Track tool calls and results for display in the TUI
        tool_calls_info: list[dict] = []

        # Real token usage, summed across every completion in this exchange.
        # The agent loop attaches per-completion usage to the message_end it
        # emits once per turn (see agent_loop._stream_response), so summing is
        # double-count-safe. We surface the REAL numbers (Fail-Early: never the
        # old len//4 approximation, which fabricated a count that looked real).
        usage_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

        def _emit(structured: dict) -> None:
            """Forward a normalized lifecycle event to the optional sink."""
            if on_event is not None:
                on_event(structured)

        def capture_event(event) -> None:
            """Normalize agent-core events into ordered widget-lifecycle events.

            Text deltas drive ``callback`` (and a ``text_delta`` structured
            event); tool execution drives ``tool_call`` / ``tool_result``
            structured events. ``turn_start`` resets the per-turn text
            accumulator and signals the caller to open a fresh pending slot,
            which is what preserves true arrival order (assistant text after a
            tool call ends up *after* it, not pinned above it).
            """
            nonlocal streaming_text, streaming_reasoning
            if not hasattr(event, "type"):
                return

            if event.type == "turn_start":
                # Clean per-turn boundary. Reset the text accumulator so the
                # next turn's assistant text is a fresh delta stream (not
                # concatenated onto the previous turn's text), and tell the
                # caller to open a new pending widget for this turn.
                streaming_text = ""
                streaming_reasoning = ""
                _emit(
                    {
                        "kind": "turn_start",
                        "turn_index": getattr(event, "turn_index", None),
                    }
                )
            elif event.type == "message_update":
                message = getattr(event, "message", None)
                if message:
                    content = message.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            block_type = block.get("type")
                            if block_type == "text":
                                full_text = block.get("text", "")
                                if not full_text:
                                    continue
                                # _stream_response re-sends the full accumulated
                                # partial_text on every update within a turn, so
                                # the real delta is the suffix beyond what we've
                                # already seen this turn.
                                if full_text.startswith(streaming_text):
                                    delta = full_text[len(streaming_text) :]
                                else:
                                    # Defensive: provider replaced rather than
                                    # extended the text mid-turn.
                                    delta = full_text
                                if not delta:
                                    continue  # no actual change
                                streaming_text = full_text
                                streaming_chunks.append(delta)
                                callback(delta)
                                _emit({"kind": "text_delta", "delta": delta})
                            elif block_type == "thinking":
                                # Reasoning streams on its own channel using the
                                # same suffix-diff as text. It is deliberately NOT
                                # fed to ``callback`` (that contract is the visible
                                # answer text only) — it surfaces as a structured
                                # ``reasoning_delta`` for the reasoning-region widget.
                                full_reasoning = block.get("thinking", "")
                                if not full_reasoning:
                                    continue
                                if full_reasoning.startswith(streaming_reasoning):
                                    delta = full_reasoning[len(streaming_reasoning) :]
                                else:
                                    delta = full_reasoning
                                if not delta:
                                    continue
                                streaming_reasoning = full_reasoning
                                _emit({"kind": "reasoning_delta", "delta": delta})
            elif event.type == "message_end":
                # Harvest tool-call blocks for chat persistence only (NOT for
                # rendering — rendering is driven off tool_execution_* below).
                # The agent loop emits message_end twice per tool-bearing turn,
                # so dedupe by id.
                message = getattr(event, "message", None)
                if message:
                    content = message.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "toolCall":
                                tc_id = block.get("id", "")
                                if any(tc["id"] == tc_id for tc in tool_calls_info):
                                    continue
                                tool_calls_info.append(
                                    {
                                        "id": tc_id,
                                        "name": block.get("name", ""),
                                        "arguments": block.get("arguments", {}),
                                    }
                                )
                    # Sum the real usage carried on this completion's message_end.
                    # Only the per-completion message_end (_stream_response) carries
                    # it, so the duplicate run() emit adds nothing — no double count.
                    usage = message.get("usage")
                    if isinstance(usage, dict):
                        for key in usage_totals:
                            usage_totals[key] += int(usage.get(key, 0) or 0)
            elif event.type == "tool_execution_start":
                # Render the tool call as soon as it begins — this is the
                # authoritative, ordered signal (carries name + args directly).
                _emit(
                    {
                        "kind": "tool_call",
                        "id": getattr(event, "tool_call_id", "") or "",
                        "name": getattr(event, "tool_name", "") or "",
                        "arguments": getattr(event, "args", None) or {},
                    }
                )
            elif event.type == "tool_execution_end":
                tool_call_id = getattr(event, "tool_call_id", "") or ""
                is_error = getattr(event, "is_error", False)
                result = getattr(event, "result", "")
                if isinstance(result, list):
                    result = " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b) for b in result
                    )
                result_str = str(result)
                # Record result against the persisted tool call (if tracked).
                for tc in tool_calls_info:
                    if tc["id"] == tool_call_id:
                        tc["result"] = result_str[:200]
                        tc["error"] = is_error
                        break
                _emit(
                    {
                        "kind": "tool_result",
                        "id": tool_call_id,
                        "name": getattr(event, "tool_name", "") or "",
                        "result": result_str,
                        "is_error": is_error,
                    }
                )

        # Subscribe to events before running the prompt
        # This captures ALL events during the full agent loop (LLM calls + tool execution)
        unsubscribe = self.agent_session.subscribe(capture_event)

        # Send through the agent loop with full conversation context
        # The loop handles LLM calls, tool execution, and re-calls the LLM
        # for tool results — all streaming flows through the event bus
        new_messages = await self.agent_session.prompt(last_user_message, context=messages)

        # Unsubscribe
        unsubscribe()

        # Combine all streaming chunks
        full_content = "".join(streaming_chunks)

        # Real token usage, summed across the exchange's completions. The dict
        # keeps the prompt/completion/total key names the TUI + headless paths
        # already read, mapped from τ's input/output/total fields. No fabricated
        # fallback — if a provider reports nothing, the count is a true 0.
        return (
            full_content,
            {
                "prompt_tokens": usage_totals["input_tokens"],
                "completion_tokens": usage_totals["output_tokens"],
                "total_tokens": usage_totals["total_tokens"],
                "cache_read_tokens": usage_totals["cache_read_tokens"],
                "cache_write_tokens": usage_totals["cache_write_tokens"],
            },
            new_messages,
            tool_calls_info,
        )


def create_backend(config: dict[str, Any]) -> Backend:
    """Factory function to create a tau-agent-core backend."""
    return TauBackend(config)
