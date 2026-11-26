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
from tau._command_hooks import CommandHooks
from tau._compaction import Compaction
from tau._permissions import Permissions
from tau._plan_mode import PlanMode
from tau._prune import Prune
from tau._router import Router
from tau._skills import Skills
from tau._startup import LoadedConfig
from tau._step_limit import StepLimit
from tau._tokens import TokenCalls, Tokens
from tau._usage import Usage

BUILTINS: Mapping[str, Callable[[LoadedConfig], object]] = {
    Agents.name: lambda loaded: Agents(),
    Attachments.name: lambda loaded: Attachments(Path.cwd()),
    BaseTools.name: lambda loaded: BaseTools(),
    BashGuard.name: lambda loaded: BashGuard(),
    Compaction.name: lambda loaded: Compaction(),
    CommandHooks.name: CommandHooks.of,  # a file it cannot use raises `HookFileError`
    Permissions.name: lambda loaded: Permissions(Path.cwd()),
    PlanMode.name: lambda loaded: PlanMode(),
    Prune.name: lambda loaded: Prune(),
    Router.name: lambda loaded: Router(),
    Skills.name: Skills.of,  # a file it cannot use raises `SkillFileError`: the start stops
    StepLimit.name: lambda loaded: StepLimit(),
    Tokens.name: lambda loaded: Tokens.configured(loaded.home, loaded.catalog.extensions),
    TokenCalls.name: lambda loaded: TokenCalls(),
    Usage.name: lambda loaded: Usage(),
}
OFF_BY_DEFAULT = frozenset({BashGuard.name, CommandHooks.name, Router.name, TokenCalls.name})
"""The built-ins that `load` must turn on (`load: {router: true}`, `tau ext enable router`)."""
