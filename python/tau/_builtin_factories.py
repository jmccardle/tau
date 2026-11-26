"""How each built-in extension is made (D24 M9, D19): its key, and a factory that builds it from
the `LoadedConfig` the start read (the config layers' options and constants, the environment,
`TAU_HOME`). `tau._extensions` adds `skills`, whose factory raises that module's
`ExtensionLoadError`.
"""

from collections.abc import Callable, Mapping
from pathlib import Path

from tau._agents import Agents
from tau._attachments import Attachments
from tau._base_tools import BaseTools
from tau._compaction import Compaction
from tau._fetch import Fetch
from tau._permissions import Permissions
from tau._prune import Prune
from tau._search import Search
from tau._startup import LoadedConfig
from tau._step_limit import StepLimit
from tau._tokens import TokenCalls, Tokens
from tau._usage import Usage

FACTORIES: Mapping[str, Callable[[LoadedConfig], object]] = {
    Agents.name: lambda loaded: Agents(),
    Attachments.name: lambda loaded: Attachments(Path.cwd()),
    BaseTools.name: lambda loaded: BaseTools(),
    Compaction.name: lambda loaded: Compaction(),
    Fetch.name: lambda loaded: Fetch.configured(loaded.catalog.extensions),
    Permissions.name: lambda loaded: Permissions(Path.cwd()),
    Prune.name: lambda loaded: Prune(),
    Search.name: lambda loaded: Search.configured(loaded.catalog.extensions, loaded.environ),
    StepLimit.name: lambda loaded: StepLimit(),
    Tokens.name: lambda loaded: Tokens.configured(loaded.home, loaded.catalog.extensions),
    TokenCalls.name: lambda loaded: TokenCalls(),
    Usage.name: lambda loaded: Usage(),
}
OFF_BY_DEFAULT = frozenset({Agents.name, Search.name, TokenCalls.name})
"""The built-ins that `load` must turn on (`load: {agents: true}`, `tau ext enable agents`)."""
