"""`tau ext list`'s rows: a line per source, then its reason or its contents."""

from collections.abc import Sequence

from tau._extensions import ExtensionSource


def _commands_shown(source: ExtensionSource, sources: Sequence[ExtensionSource]) -> str:
    """Its commands as typed: the bare name where only one source has it, else qualified."""
    count: dict[str, int] = {}
    for other in sources:
        for command in other.commands:
            count[command.partition(":")[2]] = count.get(command.partition(":")[2], 0) + 1
    return ", ".join(
        "/" + (c.partition(":")[2] if count[c.partition(":")[2]] == 1 else c)
        for c in source.commands
    )


def ext_rows(sources: Sequence[ExtensionSource]) -> list[str]:
    """`tau ext list`'s lines: a row per source, then its reason or its contents."""
    width = max(len(s.key) for s in sources)
    lines: list[str] = []
    for s in sources:
        lines.append(f"{s.key:<{width}}  {s.state:<9}  {s.origin}")
        pad = " " * (width + 2)
        if s.detail is not None:
            lines.append(f"{pad}{s.detail}")
        if s.state in ("on", "always"):
            commands = _commands_shown(s, sources) or "none"
            tools = ", ".join(s.tools) or "none"
            lines.append(f"{pad}commands: {commands}; tools: {tools}")
    return lines
