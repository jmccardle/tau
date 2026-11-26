"""The built-in extensions that `load` turns on and off (D19, D24), and how each is made from the
`LoadedConfig`; moved out of `_extensions.py`, which loads them."""

from collections.abc import Callable, Mapping
from pathlib import Path

from tau._agents import Agents
from tau._attachments import Attachments
from tau._base_tools import BaseTools
from tau._compaction import Compaction
from tau._mcp import Mcp, McpConfigError
from tau._permissions import Permissions
from tau._prune import Prune
from tau._skills import SkillFileError, Skills
from tau._startup import LoadedConfig
from tau._step_limit import StepLimit
from tau._tokens import TokenCalls, Tokens
from tau._usage import Usage

BUILTINS: Mapping[str, Callable[[LoadedConfig], object]] = {
    Agents.name: lambda loaded: Agents(),
    Attachments.name: lambda loaded: Attachments(Path.cwd()),
    BaseTools.name: lambda loaded: BaseTools(),
    Compaction.name: lambda loaded: Compaction(),
    Mcp.name: lambda loaded: Mcp.of(loaded, Path.cwd()),
    Permissions.name: lambda loaded: Permissions(Path.cwd()),
    Prune.name: lambda loaded: Prune(),
    Skills.name: Skills.of,
    StepLimit.name: lambda loaded: StepLimit(),
    Tokens.name: lambda loaded: Tokens.configured(loaded.home, loaded.catalog.extensions),
    TokenCalls.name: lambda loaded: TokenCalls(),
    Usage.name: lambda loaded: Usage(),
}
OFF_BY_DEFAULT = frozenset({Agents.name, Mcp.name, TokenCalls.name})
"""The built-ins that `load` must turn on (`load: {agents: true}`, `tau ext enable agents`)."""
FILE_ERRORS = (SkillFileError, McpConfigError)
"""What a built-in raises for a file of the user's or the project's that it cannot use: the
message names the file, and the start stops with it (no traceback: it is not τ's fault)."""
