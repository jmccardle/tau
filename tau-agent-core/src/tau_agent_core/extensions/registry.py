"""τ-agent-core extensions registry — manages tool/command/flag registration.

Reference: PHASE-3-SUBPHASE-0.md ExtensionRegistry contract.
Reference: PHASE-3-SUBPHASE-2.md ExtensionRegistry implementation.

Contract:
    class ToolInfo:
        name: str
        description: str
        parameters: dict
        source: str

    class ExtensionRegistry:
        def register_tool(self, definition: dict) -> None: ...
        def get_all_tools(self) -> list[ToolInfo]: ...
        def set_active_tools(self, names: list[str]) -> None: ...
        def get_active_tools(self) -> dict[str, dict]: ...
        def register_command(self, name: str, command: dict) -> None: ...
        def get_command(self, name: str) -> dict | None: ...
        def get_commands(self) -> dict[str, dict]: ...
        def register_shortcut(self, key: str, shortcut: dict) -> None: ...
        def get_shortcut(self, key: str) -> dict | None: ...
        def get_shortcuts(self) -> dict[str, dict]: ...

Note: ``append_entry`` is NO LONGER a registry method. Durable extension state is
persisted onto the session tree as a ``customEntry`` node via
``AgentSession._append_custom_entry`` (E6 §2 / S39), replacing the former RAM-only
``_entry_store`` that was lost on restart (G4). See ``ExtensionAPI.append_entry``.
"""

from __future__ import annotations


class ToolInfo:
    """Read-only tool information."""

    def __init__(self, name: str, description: str, parameters: dict, source: str):
        """Initialize tool info.

        Args:
            name: Tool name.
            description: Tool description.
            parameters: Tool parameters (JSON schema).
            source: Where the tool is from ("built-in" or extension name).
        """
        self.name = name
        self.description = description
        self.parameters = parameters
        self.source = source  # "built-in" or extension name

    def __repr__(self) -> str:
        return f"ToolInfo(name={self.name!r}, source={self.source!r})"


class ExtensionRegistry:
    """Manages tool, command, and flag registration.

    Reference: PHASE-3-SUBPHASE-0.md ExtensionRegistry contract.
    Reference: PHASE-3-SUBPHASE-2.md implementation outline.
    """

    def __init__(self) -> None:
        """Initialize the registry with empty collections."""
        self._tools: dict[str, dict] = {}  # name -> definition
        self._commands: dict[str, dict] = {}  # name -> command def
        self._shortcuts: dict[str, dict] = {}  # chord-tail key -> shortcut def
        self._active_tools: set[str] | None = None  # None = all active

    def register_tool(self, definition: dict) -> None:
        """Register a tool definition. A duplicate name **raises** (H3).

        This used to log a warning and overwrite. Last-write-wins with no signal is
        the failure to close: this registry is one flat, unattributed map shared by
        every loaded extension, so which of two same-named tools survives is decided
        by the order the extension files happened to import. A scenario's behaviour
        then becomes a function of module loading — a §7.1.1 determinism break that
        does not present here at all, but as a flaky assertion in whatever subsystem
        ends up calling the wrong implementation. A warning on stderr is not a signal
        anything acts on; refusing the registration is.

        **Deliberate divergence from pi, stated because it is one.** pi never errors
        on this and never warns: each extension owns its own ``tools`` Map
        (``coding-agent/src/core/extensions/loader.ts:192-198`` — ``Map.set``, so
        silent last-wins *within* one extension), and cross-extension collisions are
        resolved silently *first*-wins when the maps are merged
        (``runner.ts:417-428``, ``if (!toolsByName.has(...))``). τ has no per-extension
        map to merge — the structure that lets pi answer the question quietly does not
        exist here — so τ answers it loudly instead. Note this also replaces τ's own
        previous divergence: warn-and-*last*-wins was already the opposite of pi's
        silent-*first*-wins.

        Reload is unaffected: :meth:`AgentSession.reload_extension` calls
        ``_unregister_bucket`` (which drives :meth:`unregister_tool` over the old
        bucket's names) *before* re-importing and re-registering, so an extension
        never collides with its own previous incarnation. A raise here therefore means
        a genuine conflict with a *different* currently-active extension.

        Raises:
            ValueError: if a tool of this name is already registered.
        """
        name = definition["name"]
        if name in self._tools:
            raise ValueError(
                f"Tool '{name}' is already registered. Two tools cannot share a name: "
                "which one survived would depend on extension load order. Rename one, "
                "or call unregister_tool() first if replacement is intended."
            )
        self._tools[name] = definition

    def get_all_tools(self) -> list[ToolInfo]:
        """Get all registered tools (built-in + extension)."""
        result = []
        for name, defn in self._tools.items():
            result.append(
                ToolInfo(
                    name=name,
                    description=defn.get("description", ""),
                    parameters=defn.get("parameters", {}),
                    source=defn.get("_source", "built-in"),
                )
            )
        return result

    def set_active_tools(self, names: list[str]) -> None:
        """Enable/disable tools by name."""
        self._active_tools = set(names)

    def get_active_tools(self) -> dict[str, dict]:
        """Get currently active tools."""
        if self._active_tools is None:
            return self._tools
        return {n: d for n, d in self._tools.items() if n in self._active_tools}

    def unregister_tool(self, name: str) -> None:
        """Remove a registered tool by name (E10 §6 / S70 — runtime disable/reload).

        Idempotent: a name that is not present is a no-op (the caller — the session's
        disable/reload path — drives this from a bucket's recorded tool names, so a
        double-disable is not an error). This is removal of a real prior registration,
        NOT fabricating absent data.

        The former "or a name a later extension overwrote" case no longer exists:
        :meth:`register_tool` refuses a duplicate rather than overwriting (H3), so a
        bucket's recorded names can only ever be its own.
        """
        self._tools.pop(name, None)

    def register_command(self, name: str, command: dict) -> None:
        """Register a slash command."""
        self._commands[name] = command

    def unregister_command(self, name: str) -> None:
        """Remove a registered slash command by name (E10 §6 / S70). Idempotent."""
        self._commands.pop(name, None)

    def get_command(self, name: str) -> dict | None:
        """Look up a registered slash command by name (``None`` if unknown)."""
        return self._commands.get(name)

    def get_commands(self) -> dict[str, dict]:
        """Get all registered slash commands (name -> command def)."""
        return dict(self._commands)

    def register_shortcut(self, key: str, shortcut: dict) -> None:
        """Register an extension key binding (E10 §6 / S69).

        ``key`` is the chord-tail key (the second key after the ``ctrl+e``
        extension leader — the guarded namespace the TUI binds these under, so an
        extension can never clobber a core global binding). ``shortcut`` carries
        the ``command`` name to dispatch (plus optional ``args``/``description``).

        Last-wins on a duplicate tail key (two extensions binding the same chord) — a
        namespace collision is an environment fact, not one extension's construction
        bug.

        **This no longer mirrors** :meth:`register_tool`, which now raises (H3); the
        cross-reference is corrected rather than the behaviour changed, because a key
        binding and a tool name are not the same stake. A shadowed chord costs the
        user one keystroke they can re-issue; a shadowed tool name silently changes
        what the *model* executes. Whether shortcuts should raise too is a real
        question and deliberately not decided here — `tau-004` scoped H3 to the two
        tool-registration sites.
        """
        if key in self._shortcuts:
            import logging

            logging.warning(f"Shortcut 'ctrl+e {key}' already registered, overwriting")
        self._shortcuts[key] = shortcut

    def unregister_shortcut(self, key: str) -> None:
        """Remove a registered shortcut by its chord-tail key (E10 §6 / S70). Idempotent."""
        self._shortcuts.pop(key, None)

    def get_shortcut(self, key: str) -> dict | None:
        """Look up a registered shortcut by its chord-tail key (``None`` if unknown)."""
        return self._shortcuts.get(key)

    def get_shortcuts(self) -> dict[str, dict]:
        """Get all registered shortcuts (chord-tail key -> shortcut def)."""
        return dict(self._shortcuts)
