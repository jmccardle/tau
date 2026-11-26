"""The built-in extensions `load` turns on and off (D19), and which of them start off.

`BUILTINS` maps each key to how a start builds it from the `LoadedConfig`; its order is the load
order of a row (the sources of one row are sorted by key, and these are listed so, except
`token_calls`, which loads after `tokens`). `OFF_BY_DEFAULT` are the keys `load` must turn on.
`tau._extensions` plans the sources and imports these classes from nowhere else.
"""

from collections.abc import Callable, Mapping
from pathlib import Path

from tau._agents import Agents
from tau._attachments import Attachments
from tau._base_tools import BaseTools
from tau._bash_guard import BashGuard
from tau._compaction import Compaction
from tau._mcp import Mcp, McpConfigError
from tau._permissions import Permissions
from tau._plan_mode import PlanMode
from tau._prune import Prune
from tau._router import Router
from tau._session_commands import SessionCommands
from tau._skills import SkillFileError, Skills
from tau._startup import LoadedConfig
from tau._step_limit import StepLimit
from tau._tokens import TokenCalls, Tokens
from tau._undo import Undo
from tau._usage import Usage

BUILTINS: Mapping[str, Callable[[LoadedConfig], object]] = {
    Agents.name: lambda loaded: Agents(),
    Attachments.name: lambda loaded: Attachments(Path.cwd()),
    BaseTools.name: lambda loaded: BaseTools(),
    BashGuard.name: lambda loaded: BashGuard(),
    Compaction.name: lambda loaded: Compaction(),
    Mcp.name: lambda loaded: Mcp.of(
        loaded, Path.cwd()
    ),  # a file TAU_MCP_CONFIG names must be there
    Permissions.name: lambda loaded: Permissions(Path.cwd()),
    PlanMode.name: lambda loaded: PlanMode(),
    Prune.name: lambda loaded: Prune(),
    Router.name: lambda loaded: Router(),
    SessionCommands.name: lambda loaded: SessionCommands(Path.cwd()),
    Skills.name: Skills.of,  # a file it cannot use raises `SkillFileError`: the start stops
    StepLimit.name: lambda loaded: StepLimit(),
    Tokens.name: lambda loaded: Tokens.configured(loaded.home, loaded.catalog.extensions),
    TokenCalls.name: lambda loaded: TokenCalls(),
    Undo.name: lambda loaded: Undo(),
    Usage.name: lambda loaded: Usage(),
}
OFF_BY_DEFAULT = frozenset({BashGuard.name, Mcp.name, Router.name, TokenCalls.name})
"""The built-ins that `load` must turn on (`load: {router: true}`, `tau ext enable router`)."""
FILE_ERRORS = (SkillFileError, McpConfigError)
"""What a built-in raises for a user's or a project's file it cannot use: the message names the
file, and the start stops with it alone (it is not τ's fault, so no traceback)."""
