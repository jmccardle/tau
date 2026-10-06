"""The ``codemode`` tool and its system-prompt section, as an extension.

Load with ``tau -e examples/codemode.py`` (or ``extensions=[register]`` from the
SDK). Config is the ``extensions.codemode`` slice of ``~/.tau/config.json``; see
:class:`CodeModeConfig` for the keys.

The tool is model-visible and never callable from a script: the invoker always
excludes it, which is the ``model-only`` exposure of docs/CODE-MODE.md §5.2.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from tau_agent_core.codemode.invoker import DirectToolInvoker
from tau_agent_core.codemode.prompt import merge_templates, render_prompt
from tau_agent_core.codemode.protocol import (
    LIFETIMES,
    Lifetime,
    RunLimits,
    RunResult,
    SandboxProvider,
    SandboxUnavailable,
    ToolStub,
)

TOOL_NAME = "codemode"

PROVIDERS = ("cpython", "monty", "typescript")

CONFIG_SCHEMA: dict[str, Any] = {
    "title": "Code mode",
    "fields": [
        {"name": "provider", "kind": "select", "label": "Interpreter", "options": list(PROVIDERS)},
        {
            "name": "lifetime",
            "kind": "select",
            "label": "Interpreter lifetime",
            "options": list(LIFETIMES),
        },
        {
            "name": "stubs",
            "kind": "select",
            "label": "Tool stubs in the prompt",
            "options": ["snapshot", "none"],
        },
        {"name": "type_check", "kind": "confirm", "label": "Type-check scripts first (monty)"},
        {"name": "timeout_s", "kind": "number", "label": "Wall-clock limit, seconds"},
        {"name": "memory_mb", "kind": "number", "label": "Memory limit, MiB"},
        {"name": "max_calls", "kind": "number", "label": "Tool calls per script"},
        {"name": "output_chars", "kind": "number", "label": "Output characters returned"},
    ],
}
"""The scalar keys as a settings form. ``prompt``, ``exclude`` and ``env_passthrough``
are lists and mappings a form cannot express; set them in config.json."""


@dataclass(frozen=True)
class CodeModeConfig:
    """The ``extensions.codemode`` config slice.

    Attributes:
        provider: ``cpython`` (default), ``monty`` or ``typescript`` (prompt only).
        lifetime: ``script`` (default), ``turn`` or ``session``.
        stubs: ``snapshot`` renders the tools present at the first turn into the
            system section and freezes it; ``none`` renders no stubs, leaving
            discovery to ``search_tools``. Both are byte-stable for a session.
        type_check: Monty only: run ``ty`` against generated stubs first.
        limits: Per-run limits.
        exclude: Tools never callable from a script, besides ``codemode``.
        env_passthrough: Extra host variables a CPython child inherits.
        prompt: Template overrides — ``system``, ``description``,
            ``language_notes``, ``language_label``, ``lifetime: {script|turn|session}``.
    """

    provider: str = "cpython"
    lifetime: Lifetime = "script"
    stubs: str = "snapshot"
    type_check: bool = False
    limits: RunLimits = field(default_factory=RunLimits)
    exclude: tuple[str, ...] = ()
    env_passthrough: tuple[str, ...] = ()
    prompt: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> CodeModeConfig:
        """Parse a config slice; every key is optional.

        Raises:
            ValueError: on an unknown key or an out-of-range value.
        """
        limit_keys = {"timeout_s", "memory_mb", "max_calls", "output_chars"}
        plain = {
            "provider",
            "lifetime",
            "stubs",
            "type_check",
            "exclude",
            "env_passthrough",
            "prompt",
        }
        unknown = set(raw) - limit_keys - plain
        if unknown:
            raise ValueError(f"codemode config: unknown keys {sorted(unknown)}")
        provider = str(raw.get("provider", "cpython"))
        if provider not in PROVIDERS:
            raise ValueError(f"codemode.provider must be one of {PROVIDERS}, not {provider!r}")
        lifetime = raw.get("lifetime", "script")
        if lifetime not in LIFETIMES:
            raise ValueError(f"codemode.lifetime must be one of {LIFETIMES}, not {lifetime!r}")
        stubs = str(raw.get("stubs", "snapshot"))
        if stubs not in ("snapshot", "none"):
            raise ValueError(f"codemode.stubs must be 'snapshot' or 'none', not {stubs!r}")
        defaults = RunLimits()
        limits = RunLimits(
            timeout_s=float(raw.get("timeout_s", defaults.timeout_s)),
            memory_bytes=int(raw.get("memory_mb", defaults.memory_bytes // (1024 * 1024)))
            * 1024
            * 1024,
            max_calls=int(raw.get("max_calls", defaults.max_calls)),
            output_chars=int(raw.get("output_chars", defaults.output_chars)),
        )
        if (
            limits.timeout_s <= 0
            or limits.memory_bytes <= 0
            or limits.max_calls <= 0
            or limits.output_chars <= 0
        ):
            raise ValueError("codemode limits must all be positive")
        prompt = raw.get("prompt") or {}
        if not isinstance(prompt, Mapping):
            raise ValueError("codemode.prompt must be a mapping")
        return cls(
            provider=provider,
            lifetime=lifetime,
            stubs=stubs,
            type_check=bool(raw.get("type_check", False)),
            limits=limits,
            exclude=tuple(raw.get("exclude") or ()),
            env_passthrough=tuple(raw.get("env_passthrough") or ()),
            prompt=dict(prompt),
        )


def make_provider(config: CodeModeConfig, *, cwd: str | None = None) -> SandboxProvider:
    """Construct the configured provider. Imports nothing optional until it starts."""
    if config.provider == "cpython":
        from tau_agent_core.codemode.cpython import CPythonProvider

        return CPythonProvider(config.lifetime, cwd=cwd, env_passthrough=config.env_passthrough)
    if config.provider == "monty":
        from tau_agent_core.codemode.monty import MontyProvider

        return MontyProvider(config.lifetime, type_check=config.type_check)
    from tau_agent_core.codemode.typescript import TypeScriptProvider

    return TypeScriptProvider(config.lifetime)


def format_result(result: RunResult, limits: RunLimits) -> tuple[str, str | None]:
    """The text the model reads, and the path of the full text when it was cut."""
    parts: list[str] = []
    if result.output:
        parts.append(result.output.rstrip("\n"))
    if result.value is not None:
        parts.append(f"[value] {result.value}")
    if result.error_kind is not None:
        label = (
            result.error_kind
            if result.limit is None
            else f"{result.error_kind}, {result.limit} limit"
        )
        parts.append(f"[error: {label}]\n{(result.error or '').rstrip()}")
    if result.reset:
        parts.append("[the interpreter was reset; state from earlier calls is gone]")
    if result.calls:
        counts = Counter(c.name for c in result.calls)
        failed = sum(1 for c in result.calls if not c.ok)
        summary = ", ".join(f"{k}×{n}" for k, n in counts.items())
        parts.append(
            f"[{len(result.calls)} tool calls: {summary}"
            + (f"; {failed} failed]" if failed else "]")
        )
    text = "\n".join(parts) if parts else "[no output: print what you need]"
    if len(text) <= limits.output_chars:
        return text, None
    with tempfile.NamedTemporaryFile(
        "w", prefix="tau-codemode-", suffix=".txt", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(text)
    cut = text[: limits.output_chars]
    return (
        f"{cut}\n[output cut at {limits.output_chars} of {len(text)} characters; "
        f"full text: {handle.name}]",
        handle.name,
    )


class CodeMode:
    """One session's code mode: provider, prompt and tool.

    Args:
        config: The parsed config slice.
    """

    def __init__(self, config: CodeModeConfig) -> None:
        self.config = config
        self.provider: SandboxProvider | None = None
        probe = make_provider(config)
        self.language = probe.language
        self.templates = merge_templates(probe.prompt(), config.prompt)
        self._section: str | None = None
        _, self.description = self.render(None)

    def render(self, stubs: list[ToolStub] | None) -> tuple[str, str]:
        """``(system_section, tool_description)`` for ``stubs``."""
        return render_prompt(
            self.templates,
            provider=self.config.provider,
            language=self.language,
            lifetime=self.config.lifetime,
            limits=self.config.limits,
            stubs=stubs,
            tool_name=TOOL_NAME,
        )

    def section(self, tools: list[Any]) -> str:
        """The system section, rendered once and then frozen for the session."""
        if self._section is None:
            stubs = None
            if self.config.stubs == "snapshot":
                stubs = DirectToolInvoker(tools, exclude=(TOOL_NAME, *self.config.exclude)).stubs()
            self._section, _ = self.render(stubs)
        return self._section

    def scope(self, cursor_id: str) -> str:
        """The instance key for a run on ``cursor_id``."""
        return f"{self.config.lifetime}:{cursor_id}"

    async def run(
        self,
        code: str,
        tools: list[Any],
        *,
        cursor_id: str,
        call_id: str,
        signal: Any,
        cwd: str | None,
    ) -> dict[str, Any]:
        """Run ``code`` and return the tool-result dict."""
        if self.provider is None:
            self.provider = make_provider(self.config, cwd=cwd)
        invoker = DirectToolInvoker(
            tools,
            exclude=(TOOL_NAME, *self.config.exclude),
            language=self.language,
            max_calls=self.config.limits.max_calls,
            signal=signal,
            parent_call_id=call_id,
        )
        try:
            await self.provider.start()
            result = await self.provider.run(
                code,
                tools=invoker,
                limits=self.config.limits,
                scope=self.scope(cursor_id),
                signal=signal,
            )
        except SandboxUnavailable as exc:
            result = RunResult(error=str(exc), error_kind="sandbox")
        text, full_path = format_result(result, self.config.limits)
        return {
            "content": [{"type": "text", "text": text}],
            "is_error": not result.ok,
            "details": {
                "provider": self.config.provider,
                "lifetime": self.config.lifetime,
                "error_kind": result.error_kind,
                "limit": result.limit,
                "reset": result.reset,
                "duration_ms": round(result.duration_ms, 2),
                "calls": [asdict(c) for c in result.calls],
                "full_output_path": full_path,
            },
        }

    async def end_turn(self, cursor_id: str) -> None:
        """Drop a ``turn``-lifetime instance when its user turn ends."""
        if self.provider is not None and self.config.lifetime == "turn":
            await self.provider.end_scope(self.scope(cursor_id))

    async def close(self) -> None:
        """Release the provider."""
        if self.provider is not None:
            provider, self.provider = self.provider, None
            await provider.close()


def _session_of(ctx: Any) -> Any:
    """The live AgentSession behind ``ctx`` (a private reach; see the module docstring)."""
    return ctx._require_session()


def register(api: Any, config: Mapping[str, Any] | None = None) -> None:
    """Register the ``codemode`` tool and its prompt section.

    Reaches two private seams, both named in the experiment record: the session's
    ``_turn_tools()`` for the tool list, and appending to the system prompt from
    ``before_agent_start`` because extension tools never reach ``{{tools}}``.

    Args:
        api: The extension API.
        config: A config slice for SDK callers; ``None`` reads ``api.config``.
    """
    mode = CodeMode(CodeModeConfig.from_mapping(api.config if config is None else config))

    async def execute(
        tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
    ) -> dict[str, Any]:
        session = _session_of(ctx)
        return await mode.run(
            params["code"],
            session._turn_tools(),
            cursor_id=session.acting_cursor.id,
            call_id=tool_call_id,
            signal=signal,
            cwd=ctx.cwd,
        )

    async def before_agent_start(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        section = mode.section(_session_of(ctx)._turn_tools())
        current = event.get("system_prompt") or ""
        if section.strip() in current:
            return None
        return {"system_prompt": f"{current.rstrip()}\n\n{section}"}

    async def user_turn_end(event: dict[str, Any], ctx: Any) -> None:
        await mode.end_turn(_session_of(ctx).acting_cursor.id)

    async def session_shutdown(event: dict[str, Any], ctx: Any) -> None:
        await mode.close()

    api.register_tool(
        {
            "name": TOOL_NAME,
            "label": "Code mode",
            "description": mode.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": f"The {mode.templates.language_label} source to run.",
                    }
                },
                "required": ["code"],
            },
            "execute": execute,
        }
    )
    api.on("before_agent_start", before_agent_start)
    api.on("user_turn_end", user_turn_end)
    api.on("session_shutdown", session_shutdown)
