"""τ-agent-core: Agent runtime, loop, tools, sessions, extensions.

Public API:
- AgentSession: The main session/loop API
- SessionManager: Session persistence
- AgentEvent: Event types from the agent loop
- ExtensionAPI: API exposed to extension modules
- create_agent_session: SDK entry point factory

Reference: SUBPHASE-0.0.md
"""

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
    ExtensionAPI,
    ExtensionContext,
    ExtensionUI,
)
from tau_agent_core.agent_loop_types import (
    PreparedToolCall,
    FinalizedToolCall,
    AgentLoopConfig,
)
from tau_agent_core.tools.base import (
    ToolDefinition,
    AgentTool,
    AgentToolResult,
    ToolBatchResult,
)
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree, TreeNode
from tau_agent_core.session_manager import SessionManager
from tau_agent_core.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    CompactionDetails,
    CompactionError,
    CompactionResult,
    CompactionSettings,
    compact,
    estimate_context_tokens,
    prepare_compaction,
    should_compact,
)
from tau_agent_core.sdk import create_agent_session
from tau_agent_core.session_manager import summarize_branch
from tau_agent_core.rpc import RPCRequest, RPCResponse, RPCEvent, RPCHandler
from tau_agent_core.export import (
    ExportConfig,
    MarkdownExporter,
    HTMLExporter,
    export_session,
)

__all__ = [
    # Core types
    "AgentSession",
    "ConversationTree",
    "TreeNode",
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
    "PreparedToolCall",
    "FinalizedToolCall",
    "AgentLoopConfig",
    "ToolDefinition",
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
    # SDK
    "create_agent_session",
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
