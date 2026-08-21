"""Example 3: Dynamic Environment Tool Extension

Registers an ``env_vars`` tool that reads the process environment, so the model
can ask what the environment actually is instead of guessing from the prompt.

Read ``05_custom_tool.py`` first — it is the same three pieces (schema, execute,
result dict) with nothing else in the way. This one adds the two things a real
tool needs: it **redacts** values that look like secrets, and it **bounds** its
own output so a machine with a large environment cannot flood the context.

## Redaction is not security

``_redact`` masks any variable whose name contains ``KEY``, ``SECRET``, ``TOKEN``
or ``PASSWORD``. That is a name-based heuristic, and a secret stored under a name
it does not match is returned in full. Treat it as a courtesy that keeps obvious
credentials out of the transcript, not as a control. If a session must not see a
secret at all, do not put it in the environment τ runs in.

## Usage

```python
import importlib.util
import sys

from tau_agent_core.sdk import create_agent_session

# examples/ is not an importable package: there is no __init__.py and every
# filename starts with a digit. Load the file by path, as `tau -e` does.
_spec = importlib.util.spec_from_file_location("ext", "examples/03_dynamic_env_tool.py")
ext = importlib.util.module_from_spec(_spec)
# Register BEFORE exec_module: a module using `from __future__ import
# annotations` resolves its own dataclass annotations through sys.modules.
sys.modules[_spec.name] = ext
_spec.loader.exec_module(ext)

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "bash"],
    extensions=[ext.register],
)
```

Or load the file directly through the public ``-e`` surface::

    tau -e examples/03_dynamic_env_tool.py

## Example Agent Interaction

```
User: What's the PYTHONPATH?
Agent: [calls env_vars tool with prefix=PYTHON]
Tool result: Environment variables matching 'PYTHON':
               PYTHONPATH=/usr/lib/python3.11:...

User: Show all env vars
Agent: [calls env_vars tool with no prefix]
Tool result: All environment variables: ...
```
"""

from __future__ import annotations

import os
from typing import Any

#: Substrings that mark a variable NAME as carrying a credential. Matched against
#: the upper-cased name — see the module docstring on why this is a courtesy.
SENSITIVE_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")

#: What a redacted value is replaced with.
REDACTED = "*" * 8

#: How many variables one call may report. A developer machine can carry several
#: hundred, and every one of them would land in the model's context and stay
#: there for the rest of the session. The tool says how many it withheld, so the
#: model can narrow the request with ``prefix`` rather than assume it saw all.
MAX_REPORTED = 20


def _redact(name: str, value: str) -> str:
    """The value to report for ``name`` — masked when the name looks sensitive."""
    if any(marker in name.upper() for marker in SENSITIVE_MARKERS):
        return REDACTED
    return value


def read_env(prefix: str = "", environ: dict[str, str] | None = None) -> str:
    """Build the report text. Pure — no τ types, so it is directly testable.

    ``environ`` defaults to the real ``os.environ``; tests pass their own.
    """
    source = os.environ if environ is None else environ
    selected = {k: v for k, v in source.items() if k.startswith(prefix)}

    if prefix and not selected:
        return f"No environment variables found with prefix '{prefix}'."

    header = (
        f"Environment variables matching '{prefix}':" if prefix else "All environment variables:"
    )
    lines = [header]
    for name, value in sorted(selected.items())[:MAX_REPORTED]:
        lines.append(f"  {name}={_redact(name, value)}")

    withheld = len(selected) - MAX_REPORTED
    if withheld > 0:
        lines.append(f"  ... ({withheld} more; pass a prefix to narrow the request)")
    return "\n".join(lines)


def env_vars_execute(
    tool_call_id: str,
    params: dict[str, Any],
    signal: Any,
    on_update: Any,
    ctx: Any,
) -> dict[str, Any]:
    """The registered ``execute``: τ's five-argument extension tool signature.

    See ``05_custom_tool.py`` for what each of the five arguments carries. ``prefix``
    is read with a default because the schema does not mark it required.
    """
    return {"content": [{"type": "text", "text": read_env(params.get("prefix", ""))}]}


#: The definition handed to ``api.register_tool``. ``execute`` is a required key:
#: leaving it out raises at registration rather than producing a tool the model
#: can call and that does nothing.
ENV_VAR_TOOL: dict[str, Any] = {
    "name": "env_vars",
    "label": "Environment Variables",
    "description": (
        "Read environment variables. Optionally filter by a prefix "
        "to show only variables starting with that prefix. Values of variables "
        "whose name looks like a credential are masked."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prefix": {
                "type": "string",
                "description": (
                    "Optional prefix to filter environment variables. "
                    f"If omitted, up to {MAX_REPORTED} variables are returned."
                ),
            },
        },
        "required": [],
    },
    "execute": env_vars_execute,
    "prompt_snippet": "env_vars: Read environment variables",
    "prompt_guidelines": [
        "Use this tool to check the current environment configuration.",
        "Pass a prefix (e.g., 'PYTHON') to see only matching variables.",
        f"Without a prefix at most {MAX_REPORTED} variables are shown.",
    ],
}


def dynamic_env_tool_extension(api: Any) -> None:
    """Extension entry point: register the ``env_vars`` tool."""
    api.register_tool(ENV_VAR_TOOL)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/03_dynamic_env_tool.py`` → ``getattr(module, "register")``), so the
#: demo is loadable through the public ``-e`` surface, not only by importing
#: ``dynamic_env_tool_extension`` directly.
register = dynamic_env_tool_extension
