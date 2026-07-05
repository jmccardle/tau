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
from tau_agent_core.events import AgentEvent
from tau_agent_core.session_log import InMemorySessionLog, SessionLog
from tau_agent_core.sdk import LoadExtensionsResult, _resolve_tools


def tau_event_to_pi_event(event: AgentEvent) -> dict[str, Any] | None:
    """Serialize one τ :class:`AgentEvent` into a pi-faithful ``AgentSessionEvent``.

    pi's ``--mode json`` writes every session-subscribe event straight to stdout
    as a ``type``-discriminated JSON line (``print-mode.ts:104-108``). τ's
    ``AgentEvent`` already carries a ``type`` discriminator and τ-snake field
    names, so the wire shape is the event's own ``model_dump(exclude_none=True)``
    — there is no legacy ``kind`` remap here (that schema is the TUI widget
    channel; this is the pi-faithful channel the delegate reads, step S8 /
    D-delegate).

    One faithfulness adjustment — dedup ``message_end``. The agent loop emits
    ``message_end`` **twice** for a tool-bearing turn: once per-completion
    (carrying ``usage``/``model``/``stop_reason``, ``agent_loop.py:485``) and once
    from ``run()``/``run_continue`` (content only). pi emits exactly **one**
    ``message_end`` per assistant message, so keep the usage-bearing one and drop
    the content-only duplicate (``None`` → the caller skips it). Every emitted
    ``message_end`` therefore carries usage/model/stop_reason, which is what the
    delegate's per-child limit / stop_reason taxonomy reads.
    """
    if event.type == "message_end":
        message = event.message or {}
        if "usage" not in message:
            return None
    return event.model_dump(exclude_none=True)


def compute_cost_usd(
    cost: dict[str, Any] | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
) -> float | None:
    """Dollar cost of one completed exchange, or ``None`` when the price is unknown.

    Port of pi ``calculateCost`` (``models.ts:39-48``), collapsed to a single
    total: τ stores no per-key ``cost`` breakdown on the frozen ``Usage`` (the
    E4.cost decision D2 leaves ``Usage`` untouched and prices at the emit
    boundary), so this is just ``sum(price[k] / 1e6 * tokens[k])`` over the
    priced buckets.

    ``cost`` is the optional per-model ``{input, output, cache_read,
    cache_write}`` block (USD per 1M tokens) declared on a ``~/.tau/config.json``
    model entry. Fail-Early — an **absent** block returns ``None`` (the caller
    emits tokens only, never a fabricated ``$0``); a **present** block whose
    prices are all ``0`` (a genuinely free/local model) returns ``0.0``. The two
    read differently on the wire (``cost_usd`` absent vs ``cost_usd: 0.0``),
    which is the whole point of the option.
    """
    if cost is None:
        return None
    return float(
        float(cost.get("input", 0.0)) / 1_000_000 * input_tokens
        + float(cost.get("output", 0.0)) / 1_000_000 * output_tokens
        + float(cost.get("cache_read", 0.0)) / 1_000_000 * cache_read_tokens
        # cache_write is inert against today's provider: cache_write_tokens is
        # never populated (a real 0), so its price term is always 0. Left
        # commented until a provider reports cache-write tokens.
        # + float(cost.get("cache_write", 0.0)) / 1_000_000 * cache_write_tokens
    )


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
        on_pi_event: Callable[[dict], None] | None = None,
    ) -> tuple[str, dict, list[dict], list[dict]]:
        """Return (assistant_text, usage, new_messages, tool_calls).

        ``on_pi_event`` (optional) is the pi-faithful ``--mode json`` sink:
        every bus event serialized via :func:`tau_event_to_pi_event` (``type``
        discriminator, deduped ``message_end`` carrying usage/model/stop_reason).
        Distinct from ``on_event`` (the legacy ``kind`` widget-lifecycle channel).
        """

    @abstractmethod
    def abort(self) -> None:
        """Cooperatively abort the in-flight turn (LLM stream + tool loop).

        Safe to call when nothing is running. The TUI binds this to Esc so a
        long response can be cancelled mid-stream; the active ``stream_chat``
        returns with whatever streamed so far."""

    @abstractmethod
    async def load_extensions(
        self,
        explicit_paths: list[str] | None = None,
        *,
        discover: bool = True,
        user_dir: str | None = None,
        extensions_config: dict[str, dict[str, Any]] | None = None,
    ) -> LoadExtensionsResult:
        """Load file-path extensions into this backend's live session (E5 §2.2).

        Both run paths (headless ``run_print`` and the TUI ``Parley``) load
        extensions through this seam after building the backend, so a file
        extension's hooks fire in the same ``AgentSession`` the loop runs on.
        ``extensions_config`` (S40) is the per-extension config map handed to each
        extension's ``api.config``, keyed by file stem. Returns the
        :class:`LoadExtensionsResult`; the caller surfaces its ``errors`` (an
        explicit ``-e`` failure raises out of here instead — Fail-Early)."""


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
        # Kept for the tree-browser's summarizer (navigate_tree, §3.3): the
        # branch-summary ``complete_simple`` call needs the model + api key directly,
        # not via the AgentSession loop.
        self._model = model
        self._api_key = api_key

        # Discover tools from config. Defaults to all built-in tools.
        tool_names = config.get("tools", ["read", "write", "edit", "bash", "ls", "grep", "find"])
        # --exclude-tools denylist (pi excludeTools): drop the named built-ins from
        # the resolved set (E5 §2.3 / S28). Applied here so BOTH run paths honour it
        # (they build the session through this one seam). Extension-registered tools
        # are merged later in AgentSession._build_turn_tools and are NOT subject to
        # this built-in denylist — pi's excludeTools targets the built-in registry.
        exclude = set(config.get("exclude_tools") or [])
        if exclude:
            tool_names = [t for t in tool_names if t not in exclude]
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
        # SessionLog. The AgentSession is constructed against a scratch
        # ``InMemorySessionLog``, but the TUI immediately rebinds it onto its LIVE
        # ``session_store.Session`` via :meth:`bind_session_log` (E3-ctx / D3), so on
        # the interactive path this session is the SOLE persister — the turn's user +
        # assistant/tool messages append through the one on-disk log the TUI reads
        # back, and the TUI no longer double-writes them itself. Callers that own a
        # separate persistence-of-record and never rebind (headless ``run_print``,
        # the cost/json backend tests, SDK-style use) keep the scratch log: for them
        # ``prompt()`` persists to a log that is never flushed or read (context
        # arrives via ``stream_chat``'s ``messages`` argument), and they append to
        # their own ``Session`` themselves.
        #
        # Auto-compaction is disabled here: the caller's own message list — not this
        # log — is the context sent to the model, so a post-turn auto-compaction
        # would do useless work (and fire a slow summary LLM call every turn once it
        # crossed the threshold). The TUI compacts explicitly via ``/compact`` →
        # ``compact_messages``, which works with auto-compaction off.
        self.agent_session = AgentSession(
            session_log=InMemorySessionLog(),
            model=model,
            system_prompt=self.system_prompt,
            tools=tools,
            api_key=api_key,
            reasoning=reasoning_arg,
            compaction_settings=CompactionSettings(enabled=False),
        )

    def bind_session_log(self, session_log: SessionLog) -> None:
        """Point the AgentSession at the caller's authoritative ``SessionLog``.

        The TUI owns a live ``session_store.Session`` that is swapped on new-chat /
        clear / resume; each time it becomes current, the TUI rebinds this backend's
        AgentSession onto it so ``prompt()`` / ``compact`` / ``navigate`` persist
        through that one on-disk log (E3-ctx / D3 — AgentSession becomes the sole
        persister, retiring the app-side ``append_message`` double-write). The
        scratch ``InMemorySessionLog`` created in ``__init__`` is discarded on the
        first bind; a backend that is never bound (headless, tests) keeps it.
        """
        self.agent_session.session_log = session_log

    def abort(self) -> None:
        """Abort the current turn by tripping the AgentSession's abort signal.

        The signal is threaded down to the provider (agent_loop forwards it to
        ``stream_simple``), which polls it per SSE line and stops the stream — so
        an in-flight completion ends promptly instead of draining in full."""
        self.agent_session.abort()

    def set_ui_delegate(self, delegate: Any) -> None:
        """Forward a front-end UI delegate to the wrapped ``AgentSession`` (E5 §4 / S33).

        The app hands in a delegate whose ``notify`` paints on the Textual screen;
        this routes every loaded extension's ``api.ui.notify(...)`` there instead of
        the headless stderr sink. Delegates to :meth:`AgentSession.set_ui_delegate`,
        which sets it on the one shared :class:`ExtensionContext`.
        """
        self.agent_session.set_ui_delegate(delegate)

    async def load_extensions(
        self,
        explicit_paths: list[str] | None = None,
        *,
        discover: bool = True,
        user_dir: str | None = None,
        extensions_config: dict[str, dict[str, Any]] | None = None,
    ) -> LoadExtensionsResult:
        """Load file-path extensions into the wrapped ``AgentSession`` (E5 §2.2).

        Delegates to :meth:`AgentSession.load_extensions`, which binds each
        extension to this session's live :class:`ExtensionRunner` so its mutating
        hooks fire in the loop this backend drives. ``extensions_config`` (S40) is
        forwarded so each extension's ``api.config`` receives its config slice.
        """
        return await self.agent_session.load_extensions(
            explicit_paths,
            discover=discover,
            user_dir=user_dir,
            extensions_config=extensions_config,
        )

    def get_extension_commands(self) -> list[tuple[str, str]]:
        """List extension-registered slash commands as ``(name, description)`` (S35).

        Delegates to :meth:`AgentSession.get_extension_commands` — the app's
        command palette reads this to surface extension commands alongside its
        built-ins.
        """
        return self.agent_session.get_extension_commands()

    async def run_extension_command(self, name: str, args: str = "") -> bool:
        """Run an extension-registered slash command (S35).

        Delegates to :meth:`AgentSession.run_extension_command`; returns ``True``
        iff the command existed and ran (``False`` lets the caller fall through).
        """
        return await self.agent_session.run_extension_command(name, args)

    async def compact_messages(self, messages: list[dict]) -> list[dict] | None:
        """Compact the conversation the TUI sends, returning the shortened list.

        Delegates to the AgentSession's compaction engine. Operates on the
        caller's ``messages`` (the TUI's authoritative ``current_chat.messages``,
        which ``stream_chat`` passes as the LLM context) — not the parallel
        session-manager path. Returns None when there is nothing to compact.
        """
        return await self.agent_session.compact_messages(messages)

    async def navigate_tree(
        self,
        session: Any,
        target_id: str,
        *,
        summarize: bool = False,
        custom_instructions: str | None = None,
    ) -> list[dict]:
        """Move the live session's cursor to ``target_id`` and return the new context.

        Port of pi's ``AgentSession.navigateTree`` (agent-session.ts:2708). The live
        coding-agent ``Session`` is passed in (the TUI owns it, §2.6): this method
        operates on IT, not on the scratch ``InMemorySessionLog`` the AgentSession runs
        against. Two modes (§3.1):

        - ``summarize=False`` → append a ``navigate`` entry (zero LLM calls). The
          abandoned branch drops out of context via the ``parentId`` walk but stays on
          disk (append-only, still browsable).
        - ``summarize=True`` → summarize the abandoned subtree (``ConversationTree
          .subtree_text(target_id)`` → ``summarize_branch``, Fail-Early: raises on a
          failed/empty summary) and append a ``branch_summary`` parented at the branch
          point (Decision 5, fix 1). Mode-3 ``custom_instructions`` reach the summarizer
          SYSTEM prompt.

        Returns ``ConversationTree.context_for(cursor)`` — the flat message list the TUI
        swaps into ``self.messages`` and re-renders (reusing the compaction path, §3.4).
        """
        from tau_agent_core.conversation_tree import ConversationTree
        from tau_agent_core.session_manager import summarize_branch

        old_leaf = session.cursor
        if target_id == old_leaf:
            # No-op (pi navigateTree:2716) — already at the target.
            return ConversationTree(session.entries(), session.cursor).context_for()

        if summarize:
            branch_text = ConversationTree(session.entries(), old_leaf).subtree_text(target_id)
            summary = await summarize_branch(
                branch_text,
                self._model,
                api_key=self._api_key,
                custom_instructions=custom_instructions,
            )
            session.append_branch_summary(summary, target_id)
        else:
            session.append_navigate(target_id)

        return ConversationTree(session.entries(), session.cursor).context_for()

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
        on_pi_event: Callable[[dict], None] | None = None,
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

        def pi_capture(event: AgentEvent) -> None:
            """Forward each bus event to the pi-faithful ``--mode json`` sink.

            Sourced directly from the AgentEvent bus (not the ``kind`` widget
            channel above): :func:`tau_event_to_pi_event` maps each event to its
            ``type``-discriminated pi shape, deduping the double ``message_end`` so
            each assistant message yields one message_end with usage/model/
            stop_reason (step S8). ``None`` = the content-only duplicate; skip it.
            """
            if on_pi_event is None:
                return
            pi_event = tau_event_to_pi_event(event)
            if pi_event is not None:
                on_pi_event(pi_event)

        # Subscribe to events before running the prompt
        # This captures ALL events during the full agent loop (LLM calls + tool execution)
        unsubscribe = self.agent_session.subscribe(capture_event)
        unsubscribe_pi = (
            self.agent_session.subscribe(pi_capture) if on_pi_event is not None else None
        )

        # Send through the agent loop with full conversation context
        # The loop handles LLM calls, tool execution, and re-calls the LLM
        # for tool results — all streaming flows through the event bus
        new_messages = await self.agent_session.prompt(last_user_message, context=messages)

        # Unsubscribe
        unsubscribe()
        if unsubscribe_pi is not None:
            unsubscribe_pi()

        # Combine all streaming chunks
        full_content = "".join(streaming_chunks)

        # Real token usage, summed across the exchange's completions. The dict
        # keeps the prompt/completion/total key names the TUI + headless paths
        # already read, mapped from τ's input/output/total fields. No fabricated
        # fallback — if a provider reports nothing, the count is a true 0.
        usage_out: dict[str, Any] = {
            "prompt_tokens": usage_totals["input_tokens"],
            "completion_tokens": usage_totals["output_tokens"],
            "total_tokens": usage_totals["total_tokens"],
            "cache_read_tokens": usage_totals["cache_read_tokens"],
            "cache_write_tokens": usage_totals["cache_write_tokens"],
        }
        # Cost at the emit boundary (E4.cost / step S7). ``self.config`` IS the
        # resolved model_config, so its optional per-model ``cost`` block prices
        # this exchange here — the one final total, on the same usage dict the TUI
        # finalizer and headless ``done`` both read. Emit ``cost_usd`` ONLY when a
        # cost block is configured: an absent block yields tokens-only (unknown
        # price), never a fabricated ``$0`` — a real free model ``cost:{…:0}``
        # yields ``0.0`` and reads differently. The frozen ``Usage`` is untouched.
        cost_usd = compute_cost_usd(
            self.config.get("cost"),
            input_tokens=usage_totals["input_tokens"],
            output_tokens=usage_totals["output_tokens"],
            cache_read_tokens=usage_totals["cache_read_tokens"],
        )
        if cost_usd is not None:
            usage_out["cost_usd"] = cost_usd
        return (
            full_content,
            usage_out,
            new_messages,
            tool_calls_info,
        )


def create_backend(config: dict[str, Any]) -> Backend:
    """Factory function to create a tau-agent-core backend."""
    return TauBackend(config)
