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
        def register_command(self, name: str, command: dict, *, owner: str) -> None: ...
        def bind_command(self, typed: str, qualified: str) -> str | None: ...
        def pin_command(self, typed: str, qualified: str) -> None: ...
        def get_command(self, name: str) -> dict | None: ...
        def get_commands(self) -> dict[str, dict]: ...
        def get_bindings(self) -> dict[str, str]: ...
        def command_names(self) -> set[str]: ...
        def register_shortcut(self, key: str, shortcut: dict) -> None: ...
        def get_shortcut(self, key: str) -> dict | None: ...
        def get_shortcuts(self) -> dict[str, dict]: ...

Note: ``append_entry`` is NO LONGER a registry method. Durable extension state is
persisted onto the session tree as a ``customEntry`` node via
``AgentSession._append_custom_entry`` (E6 §2 / S39), replacing the former RAM-only
``_entry_store`` that was lost on restart (G4). See ``ExtensionAPI.append_entry``.
"""

from __future__ import annotations

from tau_agent_core.capabilities import FlowDeclaration
from tau_agent_core.tools.base import ExtensionToolDefinition


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
        self._tools: dict[str, ExtensionToolDefinition] = {}  # name -> definition
        self._commands: dict[str, dict] = {}  # QUALIFIED name -> command def
        self._bindings: dict[str, str] = {}  # typed name -> qualified name
        self._pinned: dict[str, str] = {}  # typed name -> the qualified name a human chose
        self._command_owners: dict[str, str] = {}  # qualified name -> registering path
        self._shortcuts: dict[str, dict] = {}  # chord-tail key -> shortcut def
        self._active_tools: set[str] | None = None  # None = all active
        self._flows: dict[str, FlowDeclaration] = {}  # QUALIFIED name -> what it takes
        self._flows_revision = 0

    def register_tool(self, definition: dict | ExtensionToolDefinition) -> None:
        """Register a tool definition. A duplicate name **raises** (H3).

        Accepts the historical plain dict or an
        :class:`~tau_agent_core.tools.base.ExtensionToolDefinition`. A dict is
        validated into the model HERE, at the boundary, so a malformed
        registration fails where it was made rather than as a ``KeyError`` from
        inside the agent loop one turn later.

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
        if not isinstance(definition, ExtensionToolDefinition):
            definition = ExtensionToolDefinition.model_validate(definition)
        name = definition.name
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
                    description=defn.description,
                    parameters=defn.parameters,
                    source=defn.source,
                )
            )
        return result

    def set_active_tools(self, names: list[str]) -> None:
        """Enable/disable tools by name."""
        self._active_tools = set(names)

    def get_active_tools(self) -> dict[str, ExtensionToolDefinition]:
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

    def register_command(self, name: str, command: dict, *, owner: str | None = None) -> None:
        """Install a command in the PRIVATE registry, under its qualified name.

        ``name`` is an ``ext:<extension>.<command>`` name (docs/EXTENSION-NAMESPACE.md);
        :meth:`ExtensionAPI.register_command` is what mints it, so an extension never
        spells one itself. The typed name is the contested one, and that is
        :meth:`bind_command`.

        Any flow previously declared for ``name`` is dropped. ``ExtensionAPI.register_flow``
        calls this and then :meth:`register_flow`, so the order is load-bearing: a
        reload that no longer declares a flow must not keep the old declaration, which
        is how a head came to render one extension's argument form for another's handler.

        Args:
            name: The qualified name.
            command: ``{"description": str, "handler": callable, "args": str?}``.
            owner: The registering extension's path, which is what makes the one
                possible collision detectable. A reload passes the same path and
                replaces in place, because ``_unregister_bucket`` has already run.

        Raises:
            ValueError: ``name`` is held by a DIFFERENT path — two extension files
                sharing a stem. Fail-Early, and for :meth:`register_tool`'s reason:
                which one survived would be decided by load order, silently.
        """
        held_by = self._command_owners.get(name)
        if owner is not None and held_by is not None and held_by != owner:
            raise ValueError(
                f"{name!r} is already registered by {held_by!r}. Two extension files share "
                f"the stem that names it, so one would silently replace the other's commands. "
                "Rename one of the files."
            )
        if self._flows.pop(name, None) is not None:
            self._flows_revision += 1
        self._commands[name] = command
        if owner is not None:
            self._command_owners[name] = owner

    def bind_command(self, typed: str, qualified: str) -> str | None:
        """Point a typeable name at a private-registry entry. FIRST-wins.

        The only contested table. An unpinned name goes to whoever asks first, because
        the alternative — last-wins — hands it to whichever extension file happens to
        sort later, so renaming a file silently changes what ``/speak`` does. A refused
        caller loses nothing: it is still reachable at its qualified name, and a human
        can re-point ``typed`` with :meth:`pin_command`.

        Args:
            typed: What a reader types after the ``/``.
            qualified: The private-registry name it should resolve to.

        Returns:
            ``None`` when ``typed`` now resolves to ``qualified``. Otherwise the
            qualified name that holds it instead — a name the caller can call, which is
            what makes wrapping possible without capturing anyone's handler.
        """
        pinned = self._pinned.get(typed)
        if pinned is not None:
            if pinned != qualified:
                return pinned
        else:
            held = self._bindings.get(typed)
            if held is not None and held != qualified:
                return held
        if self._bindings.get(typed) != qualified:
            self._bindings[typed] = qualified
            self._flows_revision += 1
        return None

    def pin_command(self, typed: str, qualified: str) -> None:
        """Record a human's choice of what ``typed`` means, beating every registration.

        A pin is a preference rather than session state (docs/EXTENSION-NAMESPACE.md),
        so it is set from config at startup and by the binding UI. Pinning a target that
        is not loaded leaves ``typed`` unbound rather than bound to nothing: the pinned
        extension claims it whenever it registers, and no other extension can take it in
        the meantime.
        """
        self._pinned[typed] = qualified
        if qualified in self._commands:
            self._bindings[typed] = qualified
        else:
            self._bindings.pop(typed, None)
        self._flows_revision += 1

    def unregister_command(self, name: str) -> None:
        """Remove an entry, its flow, and every binding pointing at it (S70).

        Takes either name the command answers to, the way :meth:`get_command` does.
        Idempotent. A pin survives, so re-enabling the extension restores what the
        human chose and no other extension takes the typed name while it is away.
        """
        name = self._bindings.get(name, name)
        self._commands.pop(name, None)
        self._command_owners.pop(name, None)
        if self._flows.pop(name, None) is not None:
            self._flows_revision += 1
        for typed in [t for t, q in self._bindings.items() if q == name]:
            del self._bindings[typed]
            self._flows_revision += 1

    def register_flow(self, name: str, declaration: FlowDeclaration) -> None:
        """Declare what an already-registered command TAKES (docs/EXTENSION-FLOWS.md).

        Separate from :meth:`register_command` rather than a key inside the command
        dict, because the two are read by different callers: the dict carries the
        handler and only a performer runs it, while this is registry data every head
        reads to build a form, a completion list or a palette argument.

        Args:
            name: The QUALIFIED command this describes. This does not create the
                command — ``ExtensionAPI.register_flow`` registers both, so a
                declaration without a handler is not reachable. Keying by the
                qualified name is what stops a shadowed flow from being rendered for
                the shadowing extension's handler.
            declaration: The flow, with the domain and enumerator it needs.
        """
        self._flows[name] = declaration
        self._flows_revision += 1

    def get_flows(self) -> dict[str, FlowDeclaration]:
        """Every declared extension flow, by qualified command name."""
        return dict(self._flows)

    @property
    def flows_revision(self) -> int:
        """Bumped by every change to the flow table OR the bindings over it.

        What ``AgentSession.vocabulary`` caches on: rebuilding the layered registry
        runs the whole cross-check, and a head asks for it on every keystroke. A
        binding counts because the vocabulary carries the aliases that resolve a typed
        name to its flow, so a re-binding that left the cache alone would keep
        completing the previous extension's argument.
        """
        return self._flows_revision

    def get_command(self, name: str) -> dict | None:
        """Look up a command by either name it answers to (``None`` if unknown).

        A typed name resolves through :meth:`get_bindings` first, so ``speak`` and
        ``ext:pirate.speak`` reach the same dict while ``speak`` is bound there.
        """
        return self._commands.get(self._bindings.get(name, name))

    def get_commands(self) -> dict[str, dict]:
        """The private registry: qualified name to command def.

        What an extension OWNS, not what a reader types — :meth:`get_bindings` is the
        second half. Use :meth:`command_names` for the set a line is resolved against.
        """
        return dict(self._commands)

    def get_bindings(self) -> dict[str, str]:
        """Typed command name to the qualified name it resolves to."""
        return dict(self._bindings)

    def get_pins(self) -> dict[str, str]:
        """Typed command name to the qualified name a human pinned it to."""
        return dict(self._pinned)

    def command_names(self) -> set[str]:
        """Every name that resolves — bound typed names and qualified names alike.

        What ``resolve_command`` is handed, which is why ``/ext:pirate.speak`` works
        whether or not ``/speak`` is bound to it.
        """
        return set(self._bindings) | set(self._commands)

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
