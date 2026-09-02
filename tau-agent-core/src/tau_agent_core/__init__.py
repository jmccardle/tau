"""τ-agent-core: Agent runtime, loop, tools, sessions, extensions.

Public API:
- AgentSession: The main session/loop API
- SessionManager: Session persistence
- AgentEvent: Event types from the agent loop
- ExtensionAPI: API exposed to extension modules
- create_agent_session: SDK entry point factory

Reference: SUBPHASE-0.0.md
"""

# This distribution's version, read at build time by pyproject.toml's
# [tool.setuptools.dynamic]. Kept in lockstep with the other three packages.
__version__ = "0.9.7"

from tau_agent_core.events import AgentEvent, EventBus
from tau_agent_core.session import (
    SessionEntry,
    MessageEntry,
    ToolResultEntry,
    CustomMessageEntry,
    CompactionEntry,
    SessionState,
    SessionInfo,
    BranchSummary,
    ForkResult,
    CloneResult,
)
from tau_agent_core.settings import Settings
from tau_agent_core.extension_types import (
    HEADLESS_DIALOG_ANSWERS,
    ExtensionAPI,
    ExtensionContext,
    ExtensionUI,
    HeadlessDialogError,
)
from tau_agent_core.agent_loop_types import (
    PreparedToolCall,
    FinalizedToolCall,
    AgentLoopConfig,
)
from tau_agent_core.tools.base import (
    ToolDefinition,
    ExtensionToolDefinition,
    AgentTool,
    AgentToolResult,
    ToolBatchResult,
)
from tau_agent_core.agent_session import AgentSession, ExtensionCommandResult
from tau_agent_core.conversation_tree import ConversationTree, TreeNode
from tau_agent_core.session_log import InMemorySessionLog, SessionLog, agent_spec_in_force
from tau_agent_core.session_manager import SessionManager
from tau_agent_core.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    CompactionDetails,
    CompactionError,
    CompactionResult,
    CompactionSettings,
    compact,
    estimate_context_tokens,
    estimate_span_tokens,
    prepare_compaction,
    should_compact,
)
from tau_agent_core.compaction_policy import (
    SCENARIO_POLICY_MODES,
    CompactionPolicy,
    CompactionPolicyError,
    CompactionPolicyViolation,
    policy_for_scenario,
)
from tau_agent_core.latency import (
    PromptLatencyCollector,
    PromptLatencySample,
    summarize,
)
from tau_agent_core.run_manifest import (
    HARNESS,
    build_run_manifest,
    extension_manifest_entries,
    require_compaction_policy,
    write_run_manifest,
)
from tau_agent_core.sdk import (
    ContextFile,
    ContextFileError,
    ExtensionCapabilityError,
    create_agent_session,
    load_project_context_files,
)
from tau_agent_core.session_manager import summarize_branch
from tau_agent_core.rpc import RPCRequest, RPCResponse, RPCEvent, RPCHandler
from tau_agent_core.export import (
    ExportConfig,
    MarkdownExporter,
    HTMLExporter,
    export_session,
)

__all__ = [
    "__version__",
    # Core types
    "AgentSession",
    "ExtensionCommandResult",
    "ConversationTree",
    "TreeNode",
    "SessionLog",
    "InMemorySessionLog",
    "agent_spec_in_force",
    "SessionManager",
    "AgentEvent",
    "EventBus",
    "SessionEntry",
    "MessageEntry",
    "ToolResultEntry",
    "CustomMessageEntry",
    "CompactionEntry",
    "SessionState",
    "SessionInfo",
    "BranchSummary",
    "ForkResult",
    "CloneResult",
    "Settings",
    "ExtensionAPI",
    "ExtensionContext",
    "ExtensionUI",
    "HeadlessDialogError",
    "HEADLESS_DIALOG_ANSWERS",
    "PreparedToolCall",
    "FinalizedToolCall",
    "AgentLoopConfig",
    "ToolDefinition",
    "ExtensionToolDefinition",
    "AgentTool",
    "AgentToolResult",
    "ToolBatchResult",
    # Compaction
    "CompactionSettings",
    "CompactionResult",
    "CompactionDetails",
    "CompactionError",
    "DEFAULT_COMPACTION_SETTINGS",
    "prepare_compaction",
    "compact",
    "should_compact",
    "estimate_context_tokens",
    "estimate_span_tokens",
    # Declared compaction policy for a measured run (H5 / SIM_SPEC_v2 §16.8)
    "CompactionPolicy",
    "CompactionPolicyError",
    "CompactionPolicyViolation",
    "SCENARIO_POLICY_MODES",
    "policy_for_scenario",
    # Per-prompt latency, partitioned so a compaction is never pooled (§9, §5.2)
    "PromptLatencyCollector",
    "PromptLatencySample",
    "summarize",
    # Run manifest — the mandatory partition keys this harness owns
    "HARNESS",
    "build_run_manifest",
    "extension_manifest_entries",
    "require_compaction_policy",
    "write_run_manifest",
    # SDK
    "create_agent_session",
    "ExtensionCapabilityError",
    # Project context files (AGENTS.md / CLAUDE.md discovery)
    "ContextFile",
    "ContextFileError",
    "load_project_context_files",
    # Branch summarization
    "summarize_branch",
    # RPC types (Phase 6)
    "RPCRequest",
    "RPCResponse",
    "RPCEvent",
    "RPCHandler",
    # Export types (Phase 6)
    "ExportConfig",
    "MarkdownExporter",
    "HTMLExporter",
    "export_session",
]
