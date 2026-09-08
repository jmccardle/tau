"""
TauApp - A minimalist, performant chat interface for LLMs.

Clean, simple, fast. Built with Textual.
"""

from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Header, Footer, TextArea
from textual.binding import Binding
from textual.reactive import reactive
from textual import on, work
from textual.timer import Timer
from datetime import datetime
import asyncio
import inspect
import os
import traceback
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from tau_coding_agent.backends import (
    DEFAULT_LANE,
    DEFAULT_MAX_TOKENS,
    Backend,
    RenderRouter,
    create_backend,
    make_model_resolver,
    prompt_tokens,
)
from tau_coding_agent.tagline import pick_tagline
from tau_coding_agent.headless import resolve_extensions_config
from tau_agent_core.agent_session_runtime import AgentSessionRuntime
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog
from tau_coding_agent.config import TAU_DIR, ConfigError, bootstrap_config, update_config
from tau_coding_agent.session_picker import SessionPickerModal
from tau_coding_agent.session_store import (
    subscribe_session_events,
)
from tau_coding_agent.store_factory import build_session_catalog, resolve_backend_name
from tau_coding_agent.themes import (
    DEFAULT_THEME_NAME,
    THEME_CONFIG_KEY,
    ThemeError,
    build_theme_registry,
    install_themes,
    resolve_theme,
)
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.tree_surgery import plan_branch, plan_paste
from tau_agent_core.agent_session import ExtensionCommandResult
from tau_agent_core.extension_locks import ExtensionRequest, refusal_reason
from tau_agent_core.sdk import BASE_SYSTEM_PROMPT, LoadExtensionsResult, summarize_extensions
from tau_agent_core.submission import Submission
from tau_agent_core.truncation import Truncation, truncation_notice
from tau_agent_core.capabilities import BUILTIN, FLOWS, Vocabulary
from tau_agent_core.flows import (
    Dispatched,
    FlowStep,
    Performed,
    Ready,
    bind_command_args,
    enumerate_domain,
    flow_arguments,
    flow_form_spec,
    next_step,
)
from tau_agent_core.commands import (
    FRONTEND_COMMANDS,
    ArgumentCompletions,
    CommandCompletions,
    UnsupportedCommandError,
    complete_command,
    complete_command_argument,
    resolve_command,
    unsupported_command_message,
)
from tau_agent_core.attachments import (
    DEFAULT_INLINE_LIMIT,
    SENDABLE_KINDS,
    Attachment,
    AttachmentCompletions,
    complete_attachment,
    elide_attachment_bodies,
    remove_attachment,
    render_attachments,
    scan_attachments,
)
from tau_agent_core.tools.image_resize import DEFAULT_MAX_IMAGE_DIMENSION
from tau_coding_agent.chat_widgets import (
    ReasoningRegion,
    ToolBox,
    format_telemetry,
    format_tokens,
    ChatInput,
    ChatSidebar,
    ReclaimPending,
    ChatSelected,
    _join_text_blocks,
    DEFAULT_ENTER_KEY_MODE,
    ENTER_KEY_MODES,
    ENTER_KEY_CONFIG_KEY,
)
from tau_coding_agent import modals, editor_widgets, extension_ui, transcript, tree_browser


STEERING_CONFIG_KEY = "steering_strategy"

STEERING_STRATEGIES = ("steer", "enqueue")

DEFAULT_STEERING_STRATEGY = "steer"

ATTACHMENT_LIMIT_CONFIG_KEY = "attachment_inline_limit"

_EXTENSION_FLOWS: dict[str, str] = {
    flow.name.removesuffix("_extension"): flow.name
    for flow in FLOWS
    if flow.mutation.endswith("_extension")
}
"""The verb a reader types inside ``/extensions``, mapped to the flow it names.

Derived so the ``/extensions`` view cannot offer a verb the registry does not declare;
it held its own ``{"enable", "disable", "reload"}`` literal until the three became
flows, and a literal is what let the view and the core disagree unnoticed.
"""

_SESSION_FLOWS: frozenset[str] = frozenset(
    flow.name
    for flow in FLOWS
    if len(flow.arguments) == 1 and flow.name not in _EXTENSION_FLOWS.values()
)
"""The flows :meth:`TauApp.action_run_session_flow` can drive with one typed word.

Derived rather than listed, and the shape of the derivation is the whole claim: a
flow that takes exactly one argument needs no modal, because the text after the
slash command IS that argument. ``/name``, ``/autocompact``, ``/model``,
``/resume`` and ``/compact`` all qualify; the last three keep their own actions
anyway — ``/model`` and ``/resume`` open a picker when the argument is missing,
and ``/compact`` re-renders the transcript and takes its argument optionally,
where this method refuses a missing one.
"""


class TauApp(App):
    """Main TauApp application."""

    CSS_PATH = "tau.tcss"

    TITLE = "Tau"
    SUB_TITLE = "Ready"

    SIDE_COLUMNS_MIN_WIDTH = 101

    BINDINGS = [
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+n", "new_chat", "New Chat"),
        Binding("ctrl+e", "extension_chord", "Extensions"),
        Binding("ctrl+g", "browse_tree", "Tree"),
        Binding("ctrl+r", "toggle_reasoning", "Reasoning", priority=True),
        Binding("ctrl+t", "toggle_tools", "Tools", priority=True),
        Binding("ctrl+j", "focus_and_send", "^J=Send", show=True),
        Binding("enter", "focus_and_send_on_enter", "Enter=Send", show=True),
        Binding("ctrl+p", "command_palette", "Commands", show=False),
        Binding("ctrl+c", "interrupt", "Quit", priority=True, show=False),
        Binding("escape", "escape", "Cancel", show=False, priority=True),
        Binding("ctrl+z", "rollback_turn", "Rollback", show=True, priority=True),
    ]
    """The TUI's keys. Hand-written, and NOT derived from the core's flow table.

    The command palette is a projection of :data:`FRONTEND_COMMANDS`
    (:meth:`_builtin_vocabulary_commands`) and this deliberately is not, because the
    two vocabularies are not the same shape. Nine of these twelve keys are head-local
    — a sidebar, a send key, a cancel, a chord, two collapse toggles — and name no
    capability at all; the other three back ``new_session``, ``abort`` and a rollback
    ``submit``, none of which takes an argument, so a table about argument lists has
    nothing to give them. Deriving this list would mean inventing keys for flows that
    have none and finding a home in the core for keys that mean nothing outside a
    terminal.
    """

    current_session: reactive[Optional[ConversationSession]] = reactive(None)
    current_backend: Optional[Backend] = None
    config: dict = {}
    reasoning_collapsed: reactive[bool] = reactive(False)
    tools_collapsed: reactive[bool] = reactive(False)
    is_generating: reactive[bool] = reactive(False)

    def __init__(
        self,
        cli_overrides: Optional[dict] = None,
        cli_run_config: Optional[dict] = None,
        session_catalog: Optional[SessionCatalog] = None,
        fun: bool = False,
        resume: bool = False,
    ):
        super().__init__()
        self._resume_on_start: bool = resume
        self._tagline: str = pick_tagline(fun)
        self._cwd: Path = Path.cwd()
        self.messages: list[dict] = []
        self._session_event_unsub: Optional[Callable[[], None]] = None
        self._submissions_in_flight: int = 0
        self._pending_steer: list[str] = []
        self._render_router: Optional[RenderRouter] = None
        self._pending_confirm: dict[str, Timer] = {}
        self._sidebar_open: bool = False
        self._cache_warned_models: set[str] = set()
        self._working_list_lock = asyncio.Lock()
        run_config = cli_run_config or {}
        self._extension_paths: list[str] = list(run_config.get("extensions", []))
        self._discover_extensions: bool = not run_config.get("no_extensions", False)
        self._exclude_tools: list[str] = list(run_config.get("exclude_tools", []))
        self._no_tools: str | None = run_config.get("no_tools")
        self._tool_allowlist: list[str] | None = run_config.get("tools")
        self._append_system_prompt: list[str] = list(run_config.get("append_system_prompt", []))
        self._bus_available: bool = bool(run_config.get("bus", False))
        self._no_context_files: bool = bool(run_config.get("no_context_files", False))
        self._max_turns: Optional[int] = run_config.get("max_turns")
        self._ext_config_overrides: dict[str, dict[str, Any]] = dict(
            run_config.get("ext_config", {})
        )
        self.load_config()
        if cli_overrides:
            self._apply_cli_overrides(cli_overrides)

        self._configured_steering_strategy()
        # Same startup check, same reason, for the Enter key.
        self._configured_enter_key_mode()

        self._theme_errors: list[str] = []
        self._theme_registry = build_theme_registry(errors=self._theme_errors)
        self._apply_theme(self._configured_theme_name())

        self.session_catalog: SessionCatalog = (
            session_catalog
            if session_catalog is not None
            else build_session_catalog(
                self.config,
                run_config.get("store"),
                run_config.get("session_dir"),
            )
        )
        self._store_name: str = resolve_backend_name(self.config, run_config.get("store"))
        self._session_runtime: Optional[AgentSessionRuntime] = None

    def _apply_cli_overrides(self, overrides: dict) -> None:
        """Merge CLI flag overrides over the loaded config (CLI > config.json).

        Used by ``tau --model …``/``--system-prompt …`` so the TUI opens with
        the requested model/prompt instead of the config default.
        """
        models = overrides.get("models")
        if models:
            self.config.setdefault("models", {}).update(models)
        if "default_model" in overrides:
            self.config["default_model"] = overrides["default_model"]
        if "system_prompt" in overrides:
            self.config["system_prompt"] = overrides["system_prompt"]
        if THEME_CONFIG_KEY in overrides:
            self.config[THEME_CONFIG_KEY] = overrides[THEME_CONFIG_KEY]

    def load_config(self):
        """Load ``~/.tau/config.json``, creating it from the packaged template if absent.

        Delegates to the single reader in ``config.py``. The TUI used to carry its
        own hardcoded default here, which disagreed with the packaged
        ``tau_default_config.json`` — so the file a first-run user actually got was
        not the one we maintain.
        """
        self.config = bootstrap_config()
        self.log(f"Loaded config with {len(self.config.get('models', {}))} models")

    def _configured_theme_name(self) -> str:
        """The theme this run asks for — ``--theme``, else config.json, else the default.

        Absent means "no preference", which resolves to
        :data:`~tau_coding_agent.themes.DEFAULT_THEME_NAME`.

        *Present and wrong* — a name nothing answers to, or a non-string where a
        name belongs — records the reason in :attr:`_theme_errors` and also
        resolves to the default. The two outcomes look the same on screen for
        about a second, and then ``on_mount``'s toast says which one happened.
        That toast is the whole reason this can return a name instead of raising:
        without it, "mocah" would silently render as mocha and the user would have
        no way to tell a typo from a theme that looks like the default.

        ``--theme`` reaches here through ``self.config`` because
        ``_apply_cli_overrides`` wrote it there. That is a change to the config
        *in memory* only; ``update_config`` re-reads the file, so a one-run
        override cannot ride into the saved config on the back of a later swap.
        """
        configured = self.config.get(THEME_CONFIG_KEY)
        if configured is not None and not isinstance(configured, str):
            self._theme_errors.append(
                f"config key {THEME_CONFIG_KEY!r} must be a theme name (a string), "
                f"got {type(configured).__name__}"
            )
            return DEFAULT_THEME_NAME
        try:
            return resolve_theme(configured, self._theme_registry).name
        except ThemeError as exc:
            self._theme_errors.append(str(exc))
            return DEFAULT_THEME_NAME

    @property
    def _steering_strategy(self) -> str:
        """The steering strategy in force right now.

        Derived from :attr:`config` on every read rather than cached in
        ``__init__``, so a config the app was handed after construction — every
        sandboxed app, since ``testing.sandbox.build_tau_app`` assigns
        ``app.config`` once the app exists — is the one that decides. ``__init__``
        still calls :meth:`_configured_steering_strategy` for the startup check.
        """
        return self._configured_steering_strategy()

    def _configured_steering_strategy(self) -> str:
        """The delivery point for text typed during a turn (docs/TUI-STEERING.md §2).

        Reads ``steering_strategy`` from config.json. The two values are
        :data:`~tau_agent_core.submission.MultitaskStrategy` members, and they are
        spelled the way the core spells them because they ARE the field this app
        puts on the submission:

        - ``"steer"`` (the default) — the running turn takes the message before
          its next call to the model, which is after the tool results it is
          waiting on. pi binds Enter to this.
        - ``"enqueue"`` — the message waits for the turn to end and then runs as
          its own turn.

        Fail-Early: an unrecognised value RAISES rather than falling back to the
        default. The two strategies put the message in different places at
        different times, so a typo that silently selected the other one would be
        invisible until a steering message did not land where it was aimed.
        """
        configured = self.config.get(STEERING_CONFIG_KEY, DEFAULT_STEERING_STRATEGY)
        if configured not in STEERING_STRATEGIES:
            raise ConfigError(
                f"config key {STEERING_CONFIG_KEY!r} = {configured!r} is not a "
                f"steering strategy. Use one of {', '.join(sorted(STEERING_STRATEGIES))}."
            )
        return str(configured)

    @property
    def _enter_key_mode(self) -> str:
        """What Enter does in the chat editor right now.

        Derived from :attr:`config` on every read, for the same reason
        :attr:`_steering_strategy` is.
        """
        return self._configured_enter_key_mode()

    def _configured_enter_key_mode(self) -> str:
        """Read ``enter_key`` from config.json (docs/ENTER-KEY.md).

        - ``"newline"`` (the default) — Enter inserts a line break, Ctrl+J sends.
        - ``"submit"`` — Enter sends, Shift+Enter and Ctrl+J insert a line break.
          pi's pair, and what every other terminal coding agent does.

        Fail-Early: an unrecognised value RAISES. Guessing here would hand the user
        an editor whose Enter key does the opposite of what they configured, and
        the way they would find out is by sending half a prompt.
        """
        configured = self.config.get(ENTER_KEY_CONFIG_KEY, DEFAULT_ENTER_KEY_MODE)
        if configured not in ENTER_KEY_MODES:
            raise ConfigError(
                f"config key {ENTER_KEY_CONFIG_KEY!r} = {configured!r} is not an "
                f"Enter-key mode. Use one of {', '.join(sorted(ENTER_KEY_MODES))}."
            )
        return str(configured)

    @property
    def _attachment_inline_limit(self) -> int:
        """How large a ``@file`` may be before only its path is sent, in bytes.

        Reads ``attachment_inline_limit`` from config.json, derived on every read
        for the same reason :attr:`_steering_strategy` is.

        Fail-Early: a value that is not a positive integer RAISES rather than
        falling back to the default. The setting decides whether a file's CONTENT
        reaches the model, so a typo that silently restored 10 KB would be
        invisible until a large file was quietly summarised as a path.
        """
        configured = self.config.get(ATTACHMENT_LIMIT_CONFIG_KEY, DEFAULT_INLINE_LIMIT)
        if not isinstance(configured, int) or isinstance(configured, bool) or configured <= 0:
            raise ConfigError(
                f"config key {ATTACHMENT_LIMIT_CONFIG_KEY!r} = {configured!r} is not a "
                "positive integer number of bytes."
            )
        return configured

    @property
    def _max_image_dimension(self) -> int:
        """The pixel cap an attached image is scaled down to.

        The same ``max_image_dimension`` key the ``read`` tool is configured with
        (``backends.py``), read here so an image the human attaches and an image
        the agent reads are bounded by one number rather than two.
        """
        configured = self.config.get("max_image_dimension", DEFAULT_MAX_IMAGE_DIMENSION)
        if not isinstance(configured, int) or isinstance(configured, bool) or configured <= 0:
            raise ConfigError(
                f"config key 'max_image_dimension' = {configured!r} is not a positive "
                "integer number of pixels."
            )
        return configured

    def _expand_attachments(self, text: str) -> tuple[str, list[dict[str, Any]] | None]:
        """Resolve ``@file`` references in ``text`` into the prompt actually sent.

        Reference: docs/FILE-ATTACHMENTS.md §2. Called once per submission, at the
        moment the submission is built, so the content sent is the file as it
        stands then — not as it stood when the human typed the ``@``.

        The blocks go in FRONT of what the human wrote, and the ``@word`` stays
        where they typed it: the model reads the material first and the
        instruction last, and the instruction still names the file the way the
        human did.

        A file that could not be read is reported to the human here AND appears in
        the prompt as a ``<reference … error="…">``, so neither side is left
        believing an attachment landed when it did not.

        Args:
            text: The submission text as typed.

        Returns:
            ``(text_to_send, images)``. ``images`` is ``None`` when there are
            none, which is what :class:`~tau_agent_core.submission.Submission`
            expects for "no images".
        """
        attachments = self._scan_attachments(text)
        if not any(a.kind in SENDABLE_KINDS for a in attachments):
            return text, None
        rendered = render_attachments(attachments, max_image_dimension=self._max_image_dimension)
        for failure in rendered.failures:
            self.notify(f"Attachment failed: {failure}", severity="warning")
        return rendered.prefix + text, list(rendered.images) or None

    def _apply_theme(self, name: str) -> None:
        """Make *name* the live theme, at construction time or mid-session.

        Delegates to :func:`~tau_coding_agent.themes.install_themes`, which is
        also what the bare-``App`` harnesses that load ``tau.tcss`` outside
        this class call — one implementation of "make this app wear this theme",
        so a harness cannot drift into a half-registered palette.

        Passing ``self._theme_registry`` rather than letting the helper rebuild
        one matters: rebuilding re-reads ``~/.tau/themes`` from disk, so a
        mid-session swap would silently pick up a file added since startup and
        the palette listing (built from the stored registry) would disagree with
        what a swap can reach.
        """
        install_themes(self, name, registry=self._theme_registry)

    def action_set_theme(self, name: str) -> None:
        """Switch themes in-session and remember the choice.

        The second of the two surfaces §6 asked for ("selectable / swappable"):
        the config key is the standing setting, this is the live swap. It
        **persists** — a colour scheme picked once and gone at the next launch is
        a worse answer than one that sticks, and the gesture that undoes it is the
        same gesture that did it. The saving itself lives in :meth:`watch_theme`,
        which is on the far side of ``app.theme``, so Textual's own theme palette
        gets it too.

        An unknown name here cannot come from the palette, which is built from the
        registry — it comes from ``run_action`` with a name typed by hand. It
        reports and changes nothing: the app already wears a theme that works, and
        the startup rule ("fall back to the default") would be the wrong answer
        for a swap, because it would take the colours away from a user who asked
        for a different set and mistyped.
        """
        try:
            theme = resolve_theme(name, self._theme_registry)
        except ThemeError as exc:
            self.notify(str(exc), title="Theme", severity="error", timeout=10)
            return
        self._apply_theme(theme.name)  # watch_theme saves it
        self.notify(f"Theme: {theme.name}")

    def watch_theme(self, theme_name: str) -> None:
        """Remember whichever theme became live, however it became live.

        ``action_set_theme`` is not the only way in. Textual's own "Theme" system
        command opens a second palette over ``App.available_themes`` and assigns
        ``app.theme`` directly, and every theme there is now selectable
        (``themes.textual_themes`` gives Textual's 21 the ``$tau-*`` palette
        ``tau.tcss`` needs). Persisting from the action alone would mean two
        theme lists in one palette where one sticks and one is forgotten at the
        next launch, which is worse than either behaviour on its own.

        Two conditions keep this from writing when nothing was chosen. Before the
        app is running the only assignment is ``__init__``'s, which is applying
        what config.json already says. And a name that matches the in-memory
        config is a no-op, so a ``--theme`` override is not written to disk unless
        the user picks something else — ``update_config``'s read-modify-write is
        what keeps the rest of a one-run override out of the file, and this is the
        same rule for this key.
        """
        if not self.is_running:
            return
        if self.config.get(THEME_CONFIG_KEY) == theme_name:
            return
        self.config[THEME_CONFIG_KEY] = theme_name
        update_config(THEME_CONFIG_KEY, theme_name)

    def _session_facts(self) -> transcript.SessionFacts:
        """The configuration the empty chat pane states (handoff §4.4).

        Read fresh on every show — :class:`ChatDisplay` holds this method, not its
        result — because ``/model`` and a resumed session both change the answer
        while the chat is still empty.

        The tool list comes from :func:`_tools_row`, which asks
        :func:`~tau_coding_agent.backends.resolve_tool_names` over
        ``_apply_run_config``'s output — the exact call :class:`TauBackend` makes
        when it constructs them, so the pane cannot advertise a tool the next turn
        will not have. An unknown ``default_model``
        is reported as such rather than papered over: it is the same condition
        :meth:`action_new_chat` refuses to start on, and the pane is where a user
        can see it before typing.
        """
        name = self.config.get("default_model", "local-llm")
        entry = self.config.get("models", {}).get(name)
        if entry is None:
            return transcript.SessionFacts(
                tagline=self._tagline,
                model=f"{name} — not in config.json",
                endpoint="unusable until this is fixed",
                cwd=transcript._display_path(self._cwd),
                tools="",
                store=self._store_name,
            )
        resolved = self._apply_run_config(entry)
        return transcript.SessionFacts(
            tagline=self._tagline,
            model=str(resolved.get("model", name)),
            endpoint=str(resolved.get("base_url") or resolved.get("backend", "")),
            cwd=transcript._display_path(self._cwd),
            tools=transcript._tools_row(resolved),
            store=self._store_name,
        )

    def compose(self) -> ComposeResult:
        """Compose the application layout."""
        yield Header()

        with Horizontal():
            yield ChatSidebar(self.session_catalog)

            with Vertical(id="main-area"):
                yield transcript.ChatDisplay(self._session_facts)
                yield editor_widgets.PendingInput()
                yield editor_widgets.AttachmentBar()
                yield ChatInput(id="chat-input")
                yield editor_widgets.CommandPopup()

            yield extension_ui.ExtensionPanelHost()

        yield editor_widgets.LaneStrip()
        yield extension_ui.ExtensionStatusBar()
        yield Footer()

    def on_mount(self):
        """Set up the application on mount."""

        # Focus input
        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.focus()
        chat_input.reclaim_pending = self._reclaim_pending_steer
        chat_input.enter_key_mode = lambda: self._enter_key_mode
        chat_input.command_completions = self._command_completions
        chat_input.argument_completions = self._argument_completions
        chat_input.attachment_completions = self._attachment_completions

        self.query_one(transcript.ChatDisplay).set_transcript_source(lambda: self.messages)

        self._apply_side_columns()

        for message in self._theme_errors:
            self.notify(message, title="Theme", severity="error", timeout=10)

        if self._resume_on_start:
            self.call_after_refresh(self.action_resume_session)

    def _apply_side_columns(self) -> None:
        """Show or hide the sidebar for the current ``_sidebar_open``.

        The ONE place ``#sidebar``'s display is written, so mount and ctrl+b
        cannot fight over it — an inline style set by one of them is otherwise
        permanent and invisible to the other.

        There is no longer a *responsive* half to this decision. It used to hide
        the sidebar automatically when an extension panel opened on a terminal too
        narrow for both (``_sidebar_fits``), but that rule only ever decided the
        case where the user had expressed no preference — an explicit ctrl+b won
        over it by design. §8 makes "no preference" mean CLOSED, so there is
        nothing left for the rule to decide: a visible sidebar is now, always,
        one the user asked for, and it is honored at any width.
        """
        sidebar = self.query_one(ChatSidebar)
        visible = self._sidebar_open
        if sidebar.display == visible:
            return
        sidebar.display = visible
        if visible:
            sidebar.ensure_rendered()

    def set_extension_status(self, key: str, text: str | None) -> None:
        """Update one keyed slot in the extension status strip (E10 §6 / S67).

        The app-side landing for ``ctx.ui.set_status(key, text)`` — reached through
        ``_ExtensionUIDelegate.set_status``. Forwards to the single
        :class:`ExtensionStatusBar` in the layout, which updates the slot in place
        (or clears it when ``text is None``) and re-renders. The bar is composed
        unconditionally, so it is present for the whole app lifetime; the delegate is
        only bound after mount, so no pre-mount call can reach here.
        """
        self.query_one(extension_ui.ExtensionStatusBar).set_slot(key, text)

    def set_extension_panel(self, key: str, spec: dict[str, Any] | None) -> None:
        """Mount, update, or clear one keyed extension panel (E10 §6 / S68).

        The app-side landing for ``ctx.ui.panel(key, spec)`` — reached through
        ``_ExtensionUIDelegate.panel``. Forwards to the single
        :class:`ExtensionPanelHost` in the layout, which mounts a new panel, updates
        an existing key's panel in place, or removes it when ``spec is None``. The
        host is composed unconditionally, so it is present for the whole app lifetime;
        the delegate is only bound after mount, so no pre-mount call can reach here.
        """
        self.query_one(extension_ui.ExtensionPanelHost).set_panel(key, spec)

    async def _reload_transcript(self, *, open_ask: bool = False) -> None:
        """Rebuild the transcript from ``self.messages``, then redraw the request row.

        Every reload site goes through here so the row cannot be forgotten at one
        of them: a reload happens exactly when the cursor moved, and the row is a
        view of the cursor (docs/EXTENSION-LOCKS.md §9).

        Args:
            open_ask: Passed to :meth:`refresh_extension_request` — true only for
                a session being resumed or switched into, which is where §9 says
                an ask opens by itself.
        """
        await self.query_one(transcript.ChatDisplay).reload_messages(self.messages)
        self.refresh_extension_request(open_ask=open_ask)

    def pending_extension_request(self) -> "ExtensionRequest | None":
        """The extension request at the cursor, read from the live backend.

        docs/EXTENSION-LOCKS.md §2: one reader, so what the transcript draws, what
        the editor bounces against and what ``submit`` refuses are the same entry.
        ``None`` with no backend — there is no session to be locked.
        """
        backend = self.current_backend
        if backend is None:
            return None
        request: "ExtensionRequest | None" = getattr(backend, "pending_request", None)
        return request

    def refresh_extension_request(self, *, open_ask: bool = False) -> None:
        """Redraw the transcript's request row, and optionally open its ask (§9).

        Called at every point the cursor can have moved — a turn ending, a
        reload, an answer, an extension being disabled — because the row shows
        the CURSOR's request and nothing narrower would keep it honest.

        Args:
            open_ask: Also push the modal, for the two moments §9 says it opens
                by itself: a resumed session whose cursor is an ask, and a fresh
                request arriving while the user is watching. Only when the ask's
                actions resolve to registered commands — render always, auto-open
                only when the buttons would do something.
        """
        request = self.pending_extension_request()
        display = self.query_one(transcript.ChatDisplay)
        display.set_extension_request(request)
        if not open_ask or request is None or request.ask is None:
            return
        known = set(self._extension_command_names())
        if all(action["command"] in known for action in request.ask["actions"]):
            self._open_extension_ask(request)

    @work
    async def _open_extension_ask(self, request: "ExtensionRequest") -> None:
        """Push the ask modal and, on an action, answer through the core (§8).

        A worker because ``push_screen_wait`` requires one. A dismissal (``Esc``)
        answers nothing and leaves the lock exactly as it was — the row is how it
        comes back.
        """
        answer = await self.push_screen_wait(extension_ui.ExtensionAskScreen(request))
        if answer is None:
            return
        action, values = answer
        answer_request = getattr(self.current_backend, "answer_request", None)
        if answer_request is None:
            return
        try:
            result = await answer_request(request.entry_id, action, values)
        except Exception as e:
            self.notify(f"Answering {request.extension_name} failed: {e}", severity="error")
            self.log.error(f"answer_request failed: {e}", exc_info=True)
            return
        if not result.handled:
            self.notify(
                f"{request.extension_name} is not loaded, so nothing ran — "
                "the request is answered and the session is unlocked.",
                severity="warning",
            )
        else:
            self._render_command_output(result)
        self.refresh_extension_request()

    def on_extension_request_box_reopen(
        self, message: extension_ui.ExtensionRequestBox.Reopen
    ) -> None:
        """The transcript row was clicked: put the ask back on screen (§9)."""
        self._open_extension_ask(message.request)

    async def on_extension_panel_action(self, message: extension_ui.ExtensionPanel.Action) -> None:
        """Dispatch a panel action's command back into the extension (E10 §6 / S68).

        A :class:`ExtensionPanel.Action` bubbles here when a user presses a panel
        action button. It runs the action's ``command`` (a name an extension
        registered via ``api.register_command``) through the SAME
        :meth:`run_extension_command` path the palette (S35) and typed slash commands
        (S46) use, with the action's ``args`` — so a panel closes the loop from a live
        surface to extension logic. The handler's returned value renders as a
        display-only ``system`` box (:meth:`_render_command_output`), never appended to
        the active path (the tree-as-truth invariant is untouched).

        Fail-Early: an action that names an UNKNOWN command surfaces an error notice
        (``handled is False``) rather than silently doing nothing — a mis-wired action
        is a construction bug, not a no-op. A handler exception is likewise surfaced,
        never swallowed.
        """
        runner = getattr(self.current_backend, "run_extension_command", None)
        if runner is None:
            return
        try:
            result = await runner(message.command, message.args)
        except Exception as e:
            self.notify(f"Panel action /{message.command} failed: {e}", severity="error")
            self.log.error(f"Panel action /{message.command} failed: {e}", exc_info=True)
            return
        if not result.handled:
            self.notify(
                f"Panel action → unknown command /{message.command}",
                severity="error",
            )
            return
        self._render_command_output(result)

    async def on_unmount(self) -> None:
        """Fire the notify-grade ``session_shutdown`` lifecycle hook on TUI quit (S41).

        This is the teardown counterpart to the ``session_start`` fired from
        :meth:`_load_backend_extensions`; it runs while the event loop is still
        alive (Textual awaits the app's unmount handler during shutdown), covering
        both an explicit quit and a Ctrl-C — Textual routes SIGINT through its own
        shutdown, which unmounts the app. getattr-guarded so a non-``TauBackend``
        test double (or a run that never built a backend) is a no-op. An extension's
        teardown exception is surfaced by the runner (never swallowed), not
        re-raised here — a failing shutdown hook must not wedge app teardown.

        Also closes the pooled τ-llm providers' HTTP clients for this loop
        (docs/PROVIDER-LIFETIME.md §6.3) — AFTER the shutdown hook above, since
        an extension's ``session_shutdown`` handler may itself make a final LLM
        call and needs a live client to do it with. This is the last point the
        loop is still guaranteed alive; there is no later hook to defer to.
        """
        backend = getattr(self, "current_backend", None)
        emit_shutdown = getattr(backend, "emit_session_shutdown", None)
        if emit_shutdown is not None:
            await emit_shutdown("quit")

        from tau_llm.client import aclose_providers

        await aclose_providers()

    async def on_input_submitted(self, event: Input.Submitted):
        """Handle message submission — the TUI's ONE input source.

        docs/SUBMISSION-LIFECYCLE.md phase 3. What a human typing means is now a
        :class:`~tau_agent_core.submission.Submission` handed to
        :meth:`AgentSession.submit` (via ``TauBackend.submit_turn``) rather
        than a private route into ``prompt()``: ``source="interactive"``,
        ``submitter="human"`` so every event this turn emits is attributable to a
        person at a terminal (phase 2's provenance stamp), and
        ``multitask_strategy="enqueue"`` per decision 1.

        **While a turn is in flight this method does not submit at all.** The text
        goes into :attr:`_pending_steer` and :meth:`_flush_pending_steer` submits
        it later, under the configured steering strategy
        (docs/TUI-STEERING.md §2). The buffer is what makes the text
        reclaimable: once ``submit()`` has taken it, no frontend can get it back.

        ``expand_commands=True`` (B2-b): the slash-command block that used to sit in
        this method is gone. ``AgentSession.submit`` resolves ``/compact`` / ``/tree``
        / ``/fork`` / ``/extensions`` and every extension-registered ``/name args``
        through :mod:`tau_agent_core.commands`, and reports the decision on
        ``SubmissionResult.command``; :meth:`_perform_command_outcome` does the half
        only a TUI can do. A NATS or timer submission still passes ``False`` and its
        "/compact" is literal prompt text — the flag is the security boundary, and
        this call site is the one that positively declares itself a human frontend.

        The PEEK before the submission (:func:`resolve_command`, the same pure
        function ``submit()`` uses) is a rendering concern, not a second dispatch:
        the transcript must not grow a user bubble, and ``self.messages`` must not
        grow a user turn, for input that will never become one. Asking afterwards
        would mean rendering the turn and then unrendering it. ``submit()`` remains
        the authority and resolves again on the post-``input``-hook text; the two
        can only disagree if a hook rewrites one into the other, which
        :meth:`_dispatch_command_submission` and :meth:`_get_assistant_response`
        each report rather than absorb.

        ``allow_user_input=True`` is the assertion only this call site (and
        ``rollback_turn``) can honestly make: a human typed this, so an extension
        hook running under the turn may ask that same human a question.

        Input history and clearing the widget stay here and are NOT part of the
        submission: they are properties of the ChatInput widget (up-arrow recall),
        and a bus or timer submission has no widget to recall into. Session
        materialisation, the working-list append and the rendered user turn stay
        here too, now gated on the peek.
        """
        input_widget = self.query_one("#chat-input", ChatInput)

        message = event.value.strip()

        if not message:
            return

        is_command = resolve_command(message, self._extension_command_names()) is not None

        if self.is_generating:
            if is_command:
                self.notify(
                    f"{message.split()[0]} runs between turns. Press Enter again "
                    "when this one finishes.",
                    severity="warning",
                )
                return
            input_widget.add_to_history(message)
            input_widget.clear_input()
            self._queue_pending_steer(message)
            return

        if not is_command and self._bounce_if_locked():
            return

        input_widget.add_to_history(message)
        input_widget.clear_input()

        text, images = (message, None) if is_command else self._expand_attachments(message)

        # The submission record. See the docstring for every field's reason.
        submission = Submission(
            text=text,
            images=images,
            source="interactive",
            submitter="human",
            submission_id=uuid4().hex,
            multitask_strategy="enqueue",
            expand_commands=True,
            allow_user_input=True,
        )

        if self.current_session is None:
            await self.action_new_chat()
        assert self.current_session is not None  # action_new_chat sets current_session

        if is_command:
            await self._dispatch_command_submission(submission)
            return

        self._start_turn(submission)

    def _bounce_if_locked(self) -> bool:
        """Refuse a prompt while an extension holds the session, and say why (§9).

        Read here rather than after the fact because the editor clears on
        ADMISSION: :meth:`AgentSession.submit` refuses the same submission for the
        same reason, but by then :meth:`_start_turn` has already emptied the
        editor and put the text in the working list. The core's refusal is what
        every OTHER head sees; this is the same fact read one step earlier so the
        typed line stays where the user left it.

        Returns whether the prompt was bounced. Re-opens the ask when there is
        one, so the toast is not the only thing on screen that can be acted on.
        """
        request = self.pending_extension_request()
        if request is None or not request.lock:
            return False
        self.notify(refusal_reason(request), severity="warning", timeout=8)
        self.refresh_extension_request(open_ask=True)
        return True

    def _start_turn(self, submission: Submission) -> None:
        """Put ``submission``'s text in the working list and run it in a worker.

        The tail of :meth:`on_input_submitted`, extracted so
        :meth:`_flush_pending_steer` starts a turn on exactly the same terms
        rather than on a second, drifting copy of them.

        Args:
            submission: An admitted-shape prompt submission — ``expand_commands``
                already resolved to "not a command" by the caller, since this
                appends a user turn to the working list and a command produces
                none.
        """
        self.messages.append({"role": "user", "content": submission.text})

        self._submissions_in_flight += 1
        self.is_generating = True
        self.sub_title = "Thinking… (Esc to cancel)"
        self._generate_response(submission)

    # -- steering: text typed during a turn (docs/TUI-STEERING.md) ----------

    def _queue_pending_steer(self, message: str) -> None:
        """Hold ``message`` until the steering strategy's delivery point.

        One buffer, appended to, per the "all at once" convention: a second line
        typed while the first is still waiting joins it, and both are delivered
        as ONE message. pi reaches the same place from the other side — it queues
        each separately and drains the whole queue at the delivery point
        (``PendingMessageQueue`` mode ``"all"``, ``agent.ts:142``) — but joining
        here is what makes the reclaim gesture able to hand back everything the
        user typed as one editable block. τ has no "one at a time" mode: the
        core's own ``_deliver_steer`` drains the whole queue unconditionally, so
        there is nothing behind a setting that promised otherwise.
        """
        self._pending_steer.append(message)
        self._refresh_pending_input()

    def _reclaim_pending_steer(self) -> str | None:
        """Take the pending steering text back. ``None`` when there is none.

        Bound to Up on an empty editor (:meth:`ChatInput._try_reclaim`). Clears
        the buffer, so the text is in exactly one place afterwards — the editor —
        and the user can edit it, re-send it, or delete it.
        """
        if not self._pending_steer:
            return None
        text = "\n\n".join(self._pending_steer)
        self._pending_steer.clear()
        self._refresh_pending_input()
        return text

    def _refresh_pending_input(self) -> None:
        """Redraw the pending-input widget from the buffer."""
        self.query_one(editor_widgets.PendingInput).show(self._pending_steer, self._steering_note())

    def on_reclaim_pending(self, message: ReclaimPending) -> None:
        """alt+up in the editor: hand the pending buffer back (pi's dequeue key)."""
        self._return_pending_to_the_editor()

    def _return_pending_to_the_editor(self) -> None:
        """Put the pending buffer back in the editor, keeping any draft in front.

        The same move as :meth:`_reclaim_pending_steer`, for the two events that
        make a pending message undeliverable rather than merely early: Esc, and a
        session swap. Both mean the turn the message was aimed at is gone.

        Delivering it anyway is the wrong answer to both. After Esc it would
        launch a turn the user just stopped; after a swap it would put a line
        written about one conversation into another. Dropping it is the other
        wrong answer — the user typed it. So it goes back where they can see it,
        BEFORE whatever draft is already there rather than over the top of it:
        the pending message was typed first, and pi combines the two the same way
        round (``restoreQueuedMessagesToEditor``).
        """
        reclaimed = self._reclaim_pending_steer()
        if reclaimed is None:
            return
        editor = self.query_one("#chat-input", ChatInput)
        editor.text = f"{reclaimed}\n\n{editor.text}" if editor.text else reclaimed
        editor.move_cursor(editor.document.end)

    def _steering_note(self) -> str:
        """What the pending widget promises about delivery, in words.

        Both strategies can end up delivering at the turn edge — ``"steer"`` does
        when the running turn makes no further tool calls — so this names the
        strategy's own delivery point and lets :meth:`_flush_pending_steer` be
        the thing that reports what actually happened.
        """
        if self._steering_strategy == "steer":
            return "steering, at this turn's next tool call"
        return "waiting for this turn to end"

    def _flush_pending_steer(self, *, at_tool_call: bool) -> None:
        """Deliver the pending buffer, if this is its delivery point.

        Called from exactly two places, which are the two delivery points:

        - ``at_tool_call=True`` — the running turn just started a tool. Only the
          ``"steer"`` strategy delivers here, as a ``multitask_strategy="steer"``
          submission: the core queues it and the running loop takes it before its
          next call to the model, which is after this turn's tool results have
          been appended.
        - ``at_tool_call=False`` — the app has gone idle. Both strategies deliver
          here, as an ordinary turn. For ``"enqueue"`` that is the whole
          contract; for ``"steer"`` it is what happens when the turn ended
          without making another tool call, and the message becomes its own turn
          rather than being stranded in a queue no further turn would drain.

        With no backend or no session there is nowhere to deliver, so the text
        goes back to the editor instead. That is not reachable from either call
        site today — a turn cannot be in flight or have just ended without both —
        and it is here because the alternative to a wrong answer is not a
        silently emptied buffer.

        Args:
            at_tool_call: Whether this is the mid-turn delivery point.
        """
        if not self._pending_steer:
            return
        if at_tool_call and self._steering_strategy != "steer":
            return
        if self.current_backend is None or self.current_session is None:
            self._return_pending_to_the_editor()
            return
        raw = "\n\n".join(self._pending_steer)
        self._pending_steer.clear()
        self._refresh_pending_input()
        text, images = self._expand_attachments(raw)
        submission = Submission(
            text=text,
            images=images,
            source="interactive",
            submitter="human",
            submission_id=uuid4().hex,
            multitask_strategy="steer" if at_tool_call else "enqueue",
            expand_commands=False,
            allow_user_input=True,
        )
        if at_tool_call:
            self._deliver_steering(submission, raw_text=raw)
            return
        self._start_turn(submission)

    @work(group="generation")
    async def _deliver_steering(self, submission: Submission, raw_text: str) -> None:
        """Hand a ``"steer"`` submission to the core. Starts no turn of its own.

        A worker, not an ``await`` in the render path, because
        :meth:`AgentSession.submit` runs this submission through the whole
        ``input`` hook chain before queueing it and a hook is arbitrary code.

        It deliberately does NOT take :attr:`_working_list_lock`: the running
        turn holds that for its whole duration, so waiting for it would defer
        every steering message to the turn edge — the exact behaviour the
        ``"steer"`` strategy exists to avoid. The context it passes is a snapshot
        for the one case in which the core would use it: the turn finished
        between the tool-call event and this call, in which case ``submit()``
        takes the free turn slot and runs an ordinary turn instead of queueing.
        That is reported rather than hidden — the working list is reconciled
        against the session afterwards, exactly as
        :meth:`_get_assistant_response` does it.

        Args:
            submission: The steering submission, with ``@file`` references already
                expanded into blocks (docs/FILE-ATTACHMENTS.md §5).
            raw_text: The same message as the human typed it, before expansion.
                Carried separately because the one path that puts a message back
                on the pending buffer must put back something that can be expanded
                again exactly once.
        """
        if self.current_backend is None:
            self._queue_pending_steer(raw_text)
            return
        self._submissions_in_flight += 1
        try:
            result = await self.current_backend.submit_turn(submission, list(self.messages))
            if not result.accepted:
                self.notify(
                    result.rejection_reason or "The steering message was refused",
                    severity="warning",
                )
                return
            if result.messages:
                async with self._working_list_lock:
                    if self.current_session is not None:
                        self.messages = list(self.current_session.context)
        except Exception as e:
            self.notify(f"Steering failed: {e}", severity="error")
            self.log.error(f"Steering failed: {e}")
            self.log.error(traceback.format_exc())
        finally:
            self._settle_submission()

    def _settle_submission(self) -> None:
        """One submission finished; report the app idle if it was the last.

        A turn ending while another submission is still outstanding must NOT
        report idle: Esc has to keep reaching the turn that is running, and the
        pending buffer must not be flushed into the middle of it.
        """
        self._submissions_in_flight -= 1
        if self._submissions_in_flight > 0:
            return
        self._submissions_in_flight = 0
        self.is_generating = False
        self.query_one("#chat-input", ChatInput).focus()
        # A hook may have appended a request during the turn; the turn edge is when it lands.
        self.refresh_extension_request(open_ask=True)
        self._flush_pending_steer(at_tool_call=False)

    def _extension_command_names(self) -> list[str]:
        """The names extensions have registered as slash commands, for the peek.

        ``getattr``-guarded like every other backend-capability read in this class
        (:meth:`_disabled_extension_paths`, :meth:`get_system_commands`): a test
        double or a backend built before extensions loaded simply has none, which
        makes the peek fall through to the model — the same thing an unregistered
        ``/…`` has always done. The built-in commands need no backend at all; they
        are τ's own vocabulary, hardcoded in :mod:`tau_agent_core.commands`.
        """
        return list(self._extension_command_table())

    def _extension_command_table(self) -> dict[str, str]:
        """Extension-registered command names mapped to their descriptions.

        The same backend read as :meth:`_extension_command_names`, kept whole
        because completion shows the description and dispatch only needs the name.
        ``getattr``-guarded for the same reason as every other backend-capability
        read in this class: a test double or a backend built before extensions
        loaded simply has none.
        """
        lister = getattr(self.current_backend, "get_extension_commands", None)
        if lister is None:
            return {}
        return {name: description for name, description in lister()}

    def _command_completions(self, text: str) -> CommandCompletions | None:
        """Candidate commands for ``text``, for the editor's Tab cycle and popup."""
        return complete_command(text, self._extension_command_table())

    def _vocabulary(self) -> Vocabulary:
        """τ's flow registry, plus whatever this session's extensions declared.

        Read off the live session on every use rather than held, because a
        ``/reload_extension`` changes it and a held copy would go on offering a
        gesture whose handler is gone. Falls back to :data:`BUILTIN` before a
        backend exists — the same first-frame case
        :meth:`_extension_command_table` answers with an empty dict.
        """
        session = getattr(self.current_backend, "agent_session", None)
        return getattr(session, "vocabulary", BUILTIN)

    def _argument_completions(self, text: str) -> ArgumentCompletions | None:
        """Legal values for the command argument being typed, for the Tab cycle and popup.

        The head half of argument completion: the core says WHICH argument and over
        what span (:func:`~tau_agent_core.commands.complete_command_argument`,
        pure), and this enumerates it against the live session — the same two calls
        and the same two objects :meth:`_select_options` uses to fill a flow form,
        so a value offered here and a value offered in the modal cannot differ.

        A domain that cannot be enumerated is carried back as ``error`` rather than
        raised: this runs on every keystroke, and the popup showing why there are no
        models is the Fail-Early answer where a traceback and an empty list are both
        wrong.

        Args:
            text: The editor's contents as typed.

        Returns:
            The values and the span they fill, or ``None`` when nothing is being
            asked for.
        """
        vocabulary = self._vocabulary()
        slot = complete_command_argument(text, vocabulary)
        if slot is None:
            return None
        try:
            found = enumerate_domain(
                slot.domain.name,
                session=getattr(self.current_backend, "agent_session", None),
                runtime=self._session_runtime,
                scope=slot.argument.scope,
                query=slot.query,
                vocabulary=vocabulary,
            )
        except ValueError as exc:
            return ArgumentCompletions(slot=slot, matches=(), total=0, error=str(exc))
        return ArgumentCompletions(slot=slot, matches=found.values, total=found.total)

    def _attachment_completions(self, text: str, cursor: int) -> AttachmentCompletions | None:
        """Candidate paths for the ``@…`` at ``cursor``, for the Tab cycle and popup."""
        return complete_attachment(text, cursor, cwd=Path.cwd())

    @on(TextArea.Changed, "#chat-input")
    @on(TextArea.SelectionChanged, "#chat-input")
    def _refresh_command_popup(self) -> None:
        """Redraw the popup whenever the editor's text or cursor changes.

        ``TextArea`` posts ``Changed`` on every document edit AND from the ``text``
        setter (``_text_area.py:1214``, ``:1701``), so this covers typing, the Tab
        cycle's own replacement, history navigation and ``clear_input`` with one
        handler and no polling. ``SelectionChanged`` is the second trigger the
        ``@…`` vocabulary needs: which reference is being completed depends on
        where the cursor is, and an arrow key into an existing ``@…`` moves the
        cursor without changing a character.

        The three vocabularies are asked in the same order
        :meth:`ChatInput._complete` asks them — attachment first, decided by the
        cursor, then the argument value, then the command word — so what the popup
        offers and what Tab inserts cannot disagree.

        The Tab cycle's selection is read back off the editor rather than tracked
        here, because the editor sets it before replacing the text and the
        replacement is what posts the message that runs this.
        """
        editor = self.query_one("#chat-input", ChatInput)
        popup = self.query_one("#command-popup", editor_widgets.CommandPopup)
        files = self._attachment_completions(editor.text, editor.cursor_offset)
        if files is not None:
            popup.show_files(files, editor.completion_index)
            return
        values = self._argument_completions(editor.text)
        if values is not None:
            popup.show_values(values, editor.completion_index)
            return
        popup.show(self._command_completions(editor.text), editor.completion_index)

    @on(TextArea.Changed, "#chat-input")
    def _refresh_attachment_bar(self) -> None:
        """Redraw the attachment bar from the draft (docs/FILE-ATTACHMENTS.md §4).

        A second handler on the same message rather than a branch inside the first,
        because the two answer different questions: the popup is about the word the
        cursor is in, the bar is about the whole draft. Only this one needs the
        filesystem scan, and only the popup needs the cursor.
        """
        editor = self.query_one("#chat-input", ChatInput)
        self.query_one(editor_widgets.AttachmentBar).show(self._scan_attachments(editor.text))

    def _scan_attachments(self, text: str) -> tuple[Attachment, ...]:
        """The ``@file`` references in ``text``, resolved against the working directory."""
        return scan_attachments(text, cwd=Path.cwd(), inline_limit=self._attachment_inline_limit)

    @on(editor_widgets.AttachmentBar.Remove)
    def _remove_attachment(self, message: editor_widgets.AttachmentBar.Remove) -> None:
        """A bar row was clicked: delete its ``@…`` word from the editor.

        The text is the single source of truth for what is attached, so removal is
        a text edit and the redraw follows from it. A stale span — the human typed
        between the redraw and the click — raises rather than cutting the wrong
        characters, and is reported instead of crashing the app: the fix is
        visible (edit the word), and the bar is about to be redrawn correctly by
        the very keystroke that invalidated it.
        """
        editor = self.query_one("#chat-input", ChatInput)
        try:
            editor.text = remove_attachment(editor.text, message.attachment)
        except ValueError:
            self.notify(
                f"@{message.attachment.token} moved while you were typing — "
                "delete it in the editor instead.",
                severity="warning",
            )
            return
        editor.move_cursor(editor.document.end)
        editor.focus()

    async def _dispatch_command_submission(self, submission: Submission) -> None:
        """Admit a command submission through the one door and perform its outcome.

        docs/SUBMISSION-LIFECYCLE.md phase 3. The submission goes through
        ``AgentSession.submit`` exactly like a prompt does — same admission, same
        ``input`` hook chain, same provenance stamp — and comes back with a typed
        :class:`~tau_agent_core.flows.Dispatched` instead of messages.

        No worker and no ``is_generating``: a dispatched command runs no model call,
        so there is nothing to stream, nothing to cancel with Esc, and no reason to
        gate the input. That is also why it does not go through
        :meth:`_get_assistant_response` — opening an exchange and taking the display
        lock for a turn that will not happen would leave an empty collapsible box in
        the transcript.

        Four outcomes, all of which say something rather than nothing:

        - a backend with no :meth:`submit_command` (a test double, a future backend)
          RAISES — the user typed a command and there is no door to send it through.
        - a refusal the CORE raised — a word the argument's domain does not declare
          (``/autocompact yes``), or an ``/extensions`` verb that names no flow — is
          shown as an error notice. The core is right to raise rather than guess, and
          a reader is right to see one line instead of a traceback.
        - ``result.accepted is False`` surfaces the ``rejection_reason`` verbatim.
        - ``result.command is None`` means ``submit()`` ran a TURN instead: an
          ``input`` hook rewrote the text between this app's peek and the core's own
          resolution. That turn really ran, unrendered, so it is reported as an
          error rather than passed over — the transcript is now behind the session,
          and pretending otherwise is the divergence this method must not hide.
        """
        submit_command = getattr(self.current_backend, "submit_command", None)
        if submit_command is None:
            raise UnsupportedCommandError(
                f"{type(self.current_backend).__name__} has no submit_command(), so "
                f"the command {submission.text!r} cannot be admitted. Command "
                "dispatch lives in AgentSession.submit (docs/SUBMISSION-LIFECYCLE.md "
                "phase 3); a backend that cannot reach it cannot run commands, and "
                "sending the text to the model instead would be the silent fallback "
                "this lifecycle removes."
            )
        try:
            result = await submit_command(submission)
        except (ValueError, UnsupportedCommandError) as exc:
            self.notify(str(exc), severity="error")
            return
        if not result.accepted:
            self.notify(
                result.rejection_reason or f"{submission.text} was refused",
                severity="warning",
            )
            return
        if result.command is None:
            raise UnsupportedCommandError(
                f"{submission.text!r} was dispatched as a command by this app but "
                "AgentSession.submit ran a TURN for it — an `input` hook transformed "
                "the text after the app resolved it. The turn ran without being "
                "rendered; reload the transcript. Fix the hook, or stop it from "
                "rewriting text that resolves to a command."
            )
        self._resync_working_list()
        await self._perform_command_outcome(result.command)
        self.refresh_extension_request()

    async def _perform_command_outcome(self, dispatched: Dispatched) -> None:
        """Do the half of a dispatched command only a frontend can do (B2-b).

        The other side of :mod:`tau_agent_core.commands`' split, and it routes on
        WHICH ARM came back rather than on a flag saying who should have run it:

        - a :class:`~tau_agent_core.flows.Performed` is an extension-registered
          command the session already ran, and the only thing left is to show what it
          returned — the same display-only ``system`` box
          :meth:`_render_command_output` mounts, never into ``self.messages``, so a
          command's report cannot leak into model input (E5 §1 tree-as-truth).
        - a :class:`~tau_agent_core.flows.FlowStep` is a missing argument, rendered by
          :meth:`_render_flow_step` — the session picker for a ``session_id``, a
          listing of the domain otherwise.
        - a :class:`~tau_agent_core.flows.Ready` is performed by
          :meth:`_perform_ready`.
        - a :class:`~tau_agent_core.flows.View` opens the surface of that name.

        Fail-Early: an arm naming something this app has no branch for RAISES. That is
        the whole point of the seam — the core is allowed to resolve commands a given
        frontend cannot perform, and the contract is that such a frontend says so out
        loud instead of returning as though it had. A silent ``else: pass`` here would
        make :data:`FRONTEND_COMMANDS` a list of things that may or may not work
        depending on where you typed them.
        """
        if isinstance(dispatched, Performed):
            self._render_command_output(
                ExtensionCommandResult(handled=True, output=dispatched.data.get("output"))
            )
            return
        if isinstance(dispatched, FlowStep):
            self._render_flow_step(dispatched)
            return
        if isinstance(dispatched, Ready):
            await self._perform_ready(dispatched)
            return
        if dispatched.name == "tree":
            self.action_browse_tree()
            return
        if dispatched.name == "extensions":
            self.action_show_extensions()
            return
        raise UnsupportedCommandError(
            unsupported_command_message(dispatched.name, "the TauApp TUI")
        )

    def _select_options(self, step: FlowStep) -> dict[str, dict[str, str]]:
        """Enumerate every argument of ``step``'s flow that renders as a select.

        Returns a ``{argument: {label: value}}`` mapping rather than a bare list,
        because two of the three select domains label a value with something other
        than the value — ``extension_name`` reads ``"ext.py (enabled)"`` and
        ``session_id`` reads the session's name — and a form hands back the string
        it displayed. The head owns this translation: the form spec's ``options``
        are display strings (docs/TUI-STYLE-GUIDE.md §3 records the limitation).

        Args:
            step: The step whose flow is being asked about.

        Returns:
            One entry per select-rendered argument still unbound.

        Raises:
            ValueError: A domain could not be enumerated, or two of its values share
                a label — which would make the answer ambiguous rather than wrong in
                a way anyone could see.
        """
        vocabulary = self._vocabulary()
        options: dict[str, dict[str, str]] = {}
        for argument in flow_arguments(step.flow, vocabulary):
            if argument.required and argument.name in step.bound:
                continue
            domain = vocabulary.domains[argument.domain]
            if domain.field_kind != "select":
                continue
            found = enumerate_domain(
                domain.name,
                session=getattr(self.current_backend, "agent_session", None),
                runtime=self._session_runtime,
                scope=argument.scope,
                cursor=step.cursor,
                vocabulary=vocabulary,
            )
            by_label = {value.label: value.value for value in found.values}
            if len(by_label) != len(found.values):
                raise ValueError(
                    f"domain {domain.name!r} returned two values with the same label; "
                    "the form would not be able to say which was chosen"
                )
            options[argument.name] = by_label
        return options

    @work(group="flow-step")
    async def _render_flow_step(self, step: FlowStep) -> None:
        """Ask for the arguments a flow still needs, in one form.

        The generic renderer: it reads the STEP and the registry, never the flow's
        name, so a flow nobody wrote a branch for is still askable — and asked for in
        the same modal an extension's own ``ui.form`` gets, because
        :func:`~tau_agent_core.flows.flow_form_spec` puts both on one spec.

        The one head-local case is the session picker. ``session_id`` renders as
        ``text`` because a person may have hundreds of sessions, and this app has a
        filtered picker for exactly that — the substitution
        :attr:`~tau_agent_core.capabilities.Domain.field_kind` permits, richer than
        the kind names rather than poorer.

        Args:
            step: The step ``next_step`` returned.
        """
        if step.domain.name == "session_id":
            self.action_resume_session()
            return
        try:
            options = self._select_options(step)
            spec = flow_form_spec(
                step.flow,
                step.bound,
                options={n: list(m) for n, m in options.items()},
                vocabulary=self._vocabulary(),
            )
        except (ValueError, KeyError) as exc:
            self.notify(f"/{step.flow}: {exc}", severity="error")
            return
        if not spec:
            self.notify(f"/{step.flow} needs {step.argument.name}", severity="warning")
            return
        answers = await self.push_screen_wait(modals.ExtensionFormScreen(spec))
        if answers is None:
            return
        bound = dict(step.bound)
        for name, value in answers.items():
            bound[name] = options[name][value] if name in options else value
        outcome = next_step(step.flow, bound, cursor=step.cursor, vocabulary=self._vocabulary())
        if isinstance(outcome, Ready):
            await self._perform_ready(outcome)
            return
        # next_step asked for something the form was built from; the two disagree.
        raise UnsupportedCommandError(
            f"/{step.flow} still needs {outcome.argument.name!r} after its form was answered"
        )

    async def _perform_ready(self, ready: Ready) -> None:
        """Perform a bound flow, and report what came back.

        Three mutations are performed by a SCREEN rather than by a backend method —
        ``compact`` re-renders the transcript, ``fork`` and ``switch_session`` move the
        app onto another session — so they keep the actions the keybinding and the
        palette already call. Everything else goes through the backend method the
        capability names and reports its :class:`~tau_agent_core.flows.Performed`.

        This is the arm that does NOT fully converge across heads, and the reason is
        the one docs/REMOTE-CONTROL.md §6 states: a terminal, a pipe and a socket
        differ in what they can open.

        A flow an extension DECLARED is performed by its own handler rather than by a
        backend method, because that is what an extension's mutation is
        (docs/EXTENSION-FLOWS.md). It reports through the same
        :meth:`_dispatch_extension_command` an undeclared command uses, so the two
        render identically.

        Two mutations change a surface the toast does not reach, so they redraw it:
        the extension panel, and the sidebar's session list after a rename.

        Args:
            ready: The bound flow ``next_step`` returned.

        Raises:
            UnsupportedCommandError: The bound backend has no method for the mutation,
                or answered with something other than a ``Performed``.
        """
        if ready.mutation == "compact":
            await self.action_compact(ready.arguments.get("custom_instructions", ""))
            return
        if ready.mutation == "fork":
            await self.action_fork_session()
            return
        if ready.mutation == "switch_session":
            self.post_message(ChatSelected(ready.arguments["session_id"]))
            return
        if ready.mutation == "set_model":
            self.action_set_model(ready.arguments["name"])
            return

        if ready.flow in self._vocabulary().extension_flows:
            bound = " ".join(str(value) for value in ready.arguments.values())
            await self._dispatch_extension_command(ready.flow, bound)
            return

        action = getattr(self.current_backend, ready.mutation, None)
        if action is None:
            raise UnsupportedCommandError(
                f"/{ready.flow} performs {ready.mutation!r}, which this backend does not offer"
            )
        try:
            result = action(**ready.arguments)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self.notify(f"/{ready.flow} failed: {exc}", severity="error")
            self.log.error(f"/{ready.flow} failed: {exc}", exc_info=True)
            return
        if not isinstance(result, Performed):
            raise UnsupportedCommandError(
                f"/{ready.flow} performs {ready.mutation!r}, which this backend answered with "
                f"{type(result).__name__} rather than a Performed. Every mutation reports "
                "the one record; stringifying whatever came back is what showed a reader "
                "'set_auto_compaction: True'."
            )
        ok = result.data.get("ok", True)
        self.notify(result.summary(), severity="information" if ok else "warning")
        if ready.mutation.endswith("_extension"):
            # Disabling can move the cursor off that extension's lock (EXTENSION-LOCKS §6).
            self.refresh_extension_request()
            self.action_show_extensions()
        if ready.mutation == "set_session_name":
            self.query_one(ChatSidebar).refresh_chats()

    @work(group="generation")
    async def _generate_response(self, submission: Submission) -> None:
        """Background worker: admit one submission and render the turn it starts.

        Replaces the old inline ``await``. The ``finally`` settles the submission
        regardless of how the turn ended (normal, error, or cooperative abort —
        which returns the partial answer rather than raising), which is also
        where a pending steering message becomes its own turn
        (:meth:`_settle_submission`).

        **Not ``exclusive`` any more, and that is the fix, not a relaxation.** An
        exclusive group cancels the group's other workers when a new one starts, so
        a second submission arriving mid-turn hard-cancelled the first — killing an
        admitted turn inside ``submit()`` and losing both the partial answer and the
        second prompt. That is precisely the silent drop docs/SUBMISSION-LIFECYCLE.md
        exists to remove ("nats_bus.py hand-rolls state['turn_in_flight'] and
        drops"). The submissions declare ``enqueue``; the second one now waits and
        then runs. Nothing else depended on the exclusivity:
        :meth:`action_cancel_generation` has never cancelled the worker — it trips
        the backend's abort signal and lets the turn unwind through its own
        ``finally``, which is what keeps the partial answer and the persistence
        consistent.
        """
        try:
            await self._get_assistant_response(submission)
        except Exception as e:
            self.notify(f"Error: {str(e)}", severity="error")
            self.log.error(f"Error getting response: {e}")
            self.log.error(traceback.format_exc())
            self.query_one(transcript.ChatDisplay).add_message(
                "system",
                f"**Error occurred:**\n```\n{str(e)}\n{traceback.format_exc()}\n```",
                source="verbatim",
            )
        finally:
            self._settle_submission()
            self._refresh_subtitle()

    def action_cancel_generation(self) -> None:
        """Esc: cooperatively abort the in-flight response (no-op if idle).

        Trips the backend's abort signal — the provider stops at the next streamed
        delta and the agent loop unwinds, so ``_get_assistant_response`` returns
        with the partial answer and the last worker's ``finally`` re-enables input.
        No hard task-cancel, so there is no half-applied widget/persistence state.

        It aborts THE turn that is running, not everything outstanding: a submission
        queued behind it gets its own fresh ``AbortSignal`` when ``submit()`` admits
        it, so Esc cancels this answer and the next queued prompt still runs. That
        matches what Esc has always meant here ("stop this response"); "discard the
        queue" is not a thing the TUI can express today and inventing it silently —
        cancelling a submission the core has already accepted — is the drop this
        lifecycle removes rather than adds.
        """
        if not self.is_generating or self.current_backend is None:
            return
        self.current_backend.abort()
        self._return_pending_to_the_editor()
        self.sub_title = "Cancelling…"

    # -- "press it again": two keys that ask before doing something big -------

    CONFIRM_SECONDS = 3.0

    def _offer_again(self, name: str, message: str) -> bool:
        """``True`` when ``name``'s offer was already standing — so act now.

        The other half of the answer is the side effect: on a first press this
        writes ``message`` into the status bar and starts the clock. So a caller
        reads it as "has the reader already been told what this does, and said
        yes by pressing again?".

        A press of one key withdraws the OTHER key's offer. There is one status
        bar, and an offer nobody can see any longer is one that must not still be
        answerable — a reader who presses Esc and then Ctrl+C should get Ctrl+C's
        warning, not an exit.
        """
        standing = self._pending_confirm.pop(name, None)
        if standing is not None:
            standing.stop()
            self._withdraw_offers()
            return True
        self._withdraw_offers()
        self.sub_title = message
        self._pending_confirm[name] = self.set_timer(
            self.CONFIRM_SECONDS, lambda: self._withdraw_offer(name)
        )
        return False

    def _withdraw_offer(self, name: str) -> None:
        """Time is up for ``name``'s offer: forget it and put the subtitle back."""
        timer = self._pending_confirm.pop(name, None)
        if timer is not None:
            timer.stop()
        self._restore_subtitle()

    def _withdraw_offers(self) -> None:
        for name in list(self._pending_confirm):
            self._withdraw_offer(name)

    def _restore_subtitle(self) -> None:
        """Undo whatever an offer wrote. ``_refresh_subtitle`` returns early with no
        session, which would leave the offer's text standing — so say nothing
        instead, which is what the header shows before a session exists."""
        if self.current_session is None:
            self.sub_title = ""
            return
        self._refresh_subtitle()

    def action_interrupt(self) -> None:
        """``ctrl+C``, in four steps from "stop that" to "quit".

        1. Generating — abort the turn. Same as Esc; the key a terminal user
           reaches for to stop a runaway process should stop the runaway process.
        2. Something typed — clear the input. The draft is thrown away, which is
           what ``ctrl+C`` means at a shell prompt.
        3. Nothing typed, nothing offered — offer the exit and say so.
        4. Nothing typed, the offer standing — quit.

        Steps 3 and 4 are the point: ``ctrl+C`` used to be bound straight to
        ``quit``, so one mistimed keypress ended a session with unsaved input and
        no warning.
        """
        if self.is_generating:
            self.action_cancel_generation()
            return
        editor = self.query_one("#chat-input", ChatInput)
        if editor.text:
            editor.text = ""
            self._withdraw_offers()
            return
        if self._offer_again("exit", "press ctrl+C again to exit"):
            self.exit()

    def action_escape(self) -> None:
        """``Esc``: stop the turn if one is running, else offer the tree browser.

        Esc has meant "cancel this response" since the beginning and still does —
        that is checked first, and nothing about it changes. What it used to mean
        when nothing was generating is *nothing at all*: the binding is
        ``priority=True`` so the key was consumed, the action no-op'd, and the
        reader got no feedback of any kind.

        It now offers the tree, on the second press. Two presses rather than one
        because Esc is also the key people hit to mean "never mind", and opening a
        full-screen modal on that is worse than doing nothing was.
        """
        if self.is_generating:
            self.action_cancel_generation()
            return
        if self._offer_again("tree", "press Esc again to view the tree"):
            self.action_browse_tree()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Decide which of the two ``priority=True`` app bindings are live right now.

        ``False`` does two things at once, and both are load-bearing here: the Footer
        stops advertising the binding, and the key is no longer consumed — a priority
        binding whose ``check_action`` is falsy makes ``run_action`` return ``False``,
        so textual's ``_check_bindings`` keeps walking the chain down to the focused
        widget.

        - **``rollback_turn`` needs a turn to roll back.** Idle, ``ctrl+z`` falls
          through to the ``ChatInput`` TextArea as its ordinary undo; generating, it
          is Rollback, which is what the Footer says it is for exactly that long.
          The two uses DO contend now that the editor is usable during a turn
          (docs/TUI-STEERING.md §1) — see the binding's own comment.
        - **Neither survives a modal.** A ``priority=True`` App binding beats a modal's
          own bindings (the priority pass walks ``reversed(_binding_chain)``, and the
          App is at the far end of it), so while a dialog is up ``escape`` reached
          ``action_cancel_generation`` — which dispatches, and therefore CONSUMES the
          key — instead of closing the dialog. That was invisible while no modal could
          be open during a turn: the action no-op'd and Esc merely did nothing. It is
          not invisible now. :class:`RollbackPromptModal` is open precisely while a
          turn generates, so Esc would have aborted the very turn the modal exists to
          roll back, leaving the user with a dialog that will not close and a
          submission that can no longer be admitted. Ceding both keys to whatever
          dialog is on top restores "Esc closes the dialog" everywhere, and ``ctrl+z``
          becomes undo inside the rollback prompt editor rather than a second
          rollback modal stacked on the first.
        """
        if action in ("rollback_turn", "escape", "interrupt") and len(self.screen_stack) > 1:
            return False
        if action == "rollback_turn":
            return self.is_generating
        # The Enter/Ctrl+J pair: exactly one sends, and the Footer names that one.
        if action == "focus_and_send":
            return self._enter_key_mode == "newline"
        if action == "focus_and_send_on_enter":
            return self._enter_key_mode == "submit"
        return super().check_action(action, parameters)

    def watch_is_generating(self, generating: bool) -> None:
        """Re-evaluate the Footer when a turn starts or stops.

        :meth:`check_action` reads ``is_generating``, and Textual only re-queries
        bindings when it is told to; without this the "Rollback" label would appear
        and disappear a beat late (on the next focus change), which for a binding
        whose whole point is "press this DURING a turn" is the wrong beat.
        """
        self.refresh_bindings()

    @staticmethod
    def _last_user_text(messages: list[dict]) -> str:
        """The most recent user message's text, flattened — the rollback prefill.

        Mirrors ``TauBackend._extract_last_user_message``: content is a plain string
        on the TUI's own working list and a block list once it has been round-tripped
        through the session log, and both shapes reach here.
        """
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
        return ""

    @work(group="rollback")
    async def action_rollback_turn(self) -> None:
        """ctrl+z: abort the in-flight turn, un-path it, and run a prompt in its place.

        The TUI affordance for ``multitask_strategy="rollback"``
        (docs/SUBMISSION-LIFECYCLE.md decision 2), which has been in the core since
        phase 2 with no way for a human to reach it. Esc's
        :meth:`action_cancel_generation` stops a turn and leaves its partial work on
        the active path; this is the variant that also moves the cursor back to the
        leaf the aborted turn started from, so the abandoned messages fall off the
        ``parentId`` walk. Nothing is deleted — the tree browser still shows them
        (decision 2: "append-only means nothing was un-said, only un-pathed").

        Runs as a worker: it must ``push_screen_wait`` the prompt modal, and — more
        importantly — it must not block the message pump, because the turn it is
        rolling back is still streaming into the display while this runs. The group
        is deliberately NOT ``"generation"``: that group is ``exclusive``, so landing
        in it would hard-cancel the very worker whose turn ``submit()`` is about to
        abort cooperatively, and a cancelled task mid-``submit_turn`` is the
        half-applied state ``action_cancel_generation`` exists to avoid.

        The refusals, all of which say so rather than silently doing something else:

        - **Nothing generating.** ``submit()`` reads "is a turn in flight" at
          admission and, finding none, degrades to an ordinary turn at the current
          cursor — nothing is un-pathed. That is right for the core (there is nothing
          to discard) and wrong for a human who just asked to discard something, so
          the check is here, before the submission, and again after the modal closes,
          since the turn can finish while the prompt is being typed.
        - **A slash command.** ``rollback_turn`` submits with ``expand_commands``
          ``False`` and keeps it that way now that B2-b has given the flag a
          consumer: this submission's whole job is to run a MODEL turn in place of
          the one it aborted, and dispatching a command instead would leave the
          conversation un-pathed with nothing running in the discarded turn's place.
          A leading "/" is therefore refused with the reason rather than sent.
        - **``accepted=False``.** The stale-target guard (``_current_turn_token``)
          refuses when a different submission was admitted and completed while this
          one waited for the turn slot, because rolling back then would discard THAT
          submission's work. Its ``rejection_reason`` is shown verbatim: a typed
          refusal the UI swallowed is the silent drop this whole lifecycle exists to
          prevent.

        The replacement turn now DOES stream into the transcript, and this method
        did not have to ask for it: since B3-a the renderer is a persistent bus
        subscription, and a rollback submission is admitted through the same
        ``submit()`` as any other, so it opens its own lane like any other. What
        this method still does afterwards is swap ``self.messages`` and
        ``reload_messages`` — the same seam ``/compact`` and the tree browser use —
        because the un-pathing is a TREE change and only a rebuild from the
        post-rollback session shows the abandoned turn dropping out of the context.
        """
        if not self.is_generating:
            self.notify(
                "Nothing is generating — rollback discards an in-flight turn",
                severity="warning",
            )
            return
        rollback_turn = getattr(self.current_backend, "rollback_turn", None)
        if rollback_turn is None:
            self.notify("This backend does not support rollback", severity="warning")
            return

        text = await self.push_screen_wait(
            modals.RollbackPromptModal(self._last_user_text(self.messages))
        )
        if text is None:
            return
        text = text.strip()
        if not text:
            self.notify(
                "Rollback needs a prompt to run in place of the aborted turn",
                severity="warning",
            )
            return
        if text.startswith("/"):
            self.notify(
                "A rollback prompt is sent to the model as-is — slash commands are "
                "not expanded here",
                severity="warning",
            )
            return
        if not self.is_generating:
            self.notify(
                "The turn finished while you were typing — there is nothing left to roll back",
                severity="warning",
            )
            return

        self.sub_title = "Rolling back…"
        try:
            result = await rollback_turn(text)
        except Exception as e:
            self.notify(f"Rollback failed: {e}", severity="error")
            self.log.error(f"Rollback failed: {e}")
            self.log.error(traceback.format_exc())
            self._refresh_subtitle()
            return

        if not result.accepted:
            self.notify(result.rejection_reason or "Rollback was refused", severity="warning")
            self._refresh_subtitle()
            return

        assert self.current_session is not None  # is_generating implies a session
        self.messages = list(self.current_session.context)
        await self._reload_transcript()
        self._refresh_subtitle()
        self.notify("Rolled back and re-ran from before the aborted turn")

    async def _get_assistant_response(self, submission: Submission) -> None:
        """Admit ``submission`` and await the turn it starts. Renders nothing.

        B3-a. This method used to be the renderer: it opened an exchange, awaited
        ``stream_submission``, fed its ``on_event`` stream into the display, and
        closed the exchange with the returned usage. That shape is single-stream by
        construction — one awaited call, one buffer, one exchange — so a forked
        second agent and a turn originated by a bus, timer or extension had no
        representation in it. Rendering now happens in :meth:`_on_render_event`,
        off a subscription that is attached for the life of the backend and sees
        every lane, including the ones this app never submitted.

        What is left here is the half that genuinely belongs to the SUBMITTER
        rather than to the renderer: awaiting completion (so the input re-enables
        and ``is_generating`` clears when THIS turn is done), surfacing a typed
        refusal, performing a command outcome, and reconciling the working message
        list against the session that just recorded the turn.

        :attr:`_working_list_lock` is held across read-context → await → write-back
        for the reason its own comment gives: ``self.messages`` is both the context
        handed over and the thing rebuilt afterwards. It is NOT a render lock any
        more — another lane streams into the display while this is held.
        """
        assert self.current_session is not None  # set before a turn runs
        assert self.current_backend is not None  # a turn cannot start without one
        async with self._working_list_lock:
            result = await self.current_backend.submit_turn(submission, self.messages)

            if not result.accepted:
                self.notify(result.rejection_reason or "The turn was refused", severity="warning")

            if result.command is not None:
                await self._perform_command_outcome(result.command)

            self.messages = list(self.current_session.context)

            self.query_one(ChatSidebar).refresh_chats()

    @staticmethod
    def _lane_label(source: object, submitter: object) -> str | None:
        """How this lane should be marked, or ``None`` for "a human typed it here".

        The Jupyter rule the spec states and warns is easy to get backwards: a
        frontend filters on "is this mine?" to decide HOW to render, and still
        renders the rest. So this returns a LABEL, never a "drop it" — the only
        thing the answer changes is whether the exchange is badged with where it
        came from.
        """
        if source == "interactive" and submitter == "human":
            return None
        return f"{source} · {submitter}"

    @staticmethod
    def _lane_role(source: object) -> str:
        """The :class:`MessageBox` role for a foreign lane's submission bubble (B3-b).

        The SOURCE is the role, so ``ROLE_LABELS`` gives the bubble its border
        title — "Timer", "Bus", "Sub-agent" — instead of the "User" a submission
        nobody typed used to wear. An unlisted source is passed through verbatim
        and capitalized by :meth:`MessageBox.on_mount`, which is the whole reason
        this is a lookup with a fallback rather than a match: a source this build
        has never heard of must still render, attributed as best we can, because a
        renderer that hides what it does not recognise is the failure mode.

        A source that is missing or blank is the one case with nothing to say, and
        it says exactly that — ``"unknown"`` — rather than borrowing ``"user"``
        and claiming a human was involved.
        """
        text = str(source or "").strip()
        return text or "unknown"

    def _report_cache_miss(self, display: transcript.ChatDisplay, reason: str | None) -> None:
        """Say that the prompt cache should have been read this turn and was not.

        Reference: docs/PROMPT-CACHING.md §7. ``reason`` is the router's
        :class:`~tau_agent_core.prompt_cache.PromptCacheObserver` verdict, reached
        from the completions' RESULTS — the misconfiguration worth catching is a
        gateway that drops ``cache_control`` on its way to an Anthropic model, and
        such a model has nothing declared to gate on.

        The evidence gates are the observer's. This adds the one gate that is a
        display decision rather than a reading: **once per model per session**,
        because a line repeated every turn is one a reader stops seeing.
        """
        if not reason:
            return
        model = str(self.current_session.model) if self.current_session else "?"
        if model in self._cache_warned_models:
            return
        self._cache_warned_models.add(model)
        display.add_message(
            "system",
            "**This server has a prompt cache and nothing was read from it.**\n\n"
            f"{reason} A prompt this size should be read back rather than re-sent, "
            "and on an Anthropic model the repeated part then costs roughly a "
            "tenth of what it costs here.\n\n"
            f"If `{model}` reaches an Anthropic model through an OpenAI-compatible "
            "gateway (LiteLLM or similar), set `models.<name>.prompt_cache_dialect` "
            'to `"anthropic"` in `~/.tau/config.json` — τ cannot tell from a base '
            "URL which dialect an endpoint speaks, so it has to be declared.\n\n"
            "Said once per model per session. See `docs/PROMPT-CACHING.md`.",
            source="markdown",
        )
        self.notify(f"No prompt-cache reads for {model}", severity="warning")

    def _report_truncation(self, display: transcript.ChatDisplay, dropped: int) -> None:
        """Say that the last completion stopped at the output cap, not at an answer.

        Reference: docs/TRUNCATED-TOOL-CALLS.md §3. The sentence is
        :func:`~tau_agent_core.truncation.truncation_notice`'s, so print mode and an
        RPC host report the same completion the same way; this adds the cap it
        quotes and the advice, which are the head's.

        ``stop_reason="length"`` is the one stop reason an operator has to act on.
        When the cut lands inside a tool call's ``arguments`` the provider drops the
        call (it is a prefix, not a payload — see ``_build_final_message``), so the
        turn ends with nothing run, which reads as the model losing interest.

        A durable box rather than only a toast, for the same reason the error path
        mounts one: a toast that has faded cannot be scrolled back to.
        """
        cap = self._configured_max_tokens()
        cap_text = "unknown" if cap is None else str(cap)
        notice = truncation_notice(Truncation(1, dropped), max_tokens=cap)
        display.add_message(
            "system",
            "**The model stopped at its output cap, not at the end of an answer.**\n\n"
            f"{notice}\n\n"
            "Anything it was still writing is missing from the turn above. Raise "
            "`max_tokens` for this model in `~/.tau/config.json`, or lower the "
            "reasoning budget so the answer fits under the cap.",
            source="markdown",
        )
        self.notify(f"Model output truncated at max_tokens={cap_text}", severity="warning")

    def _configured_max_tokens(self) -> int | None:
        """The output cap τ sends for the default model, or ``None`` if unresolvable.

        Read from the same config entry :meth:`_apply_run_config` resolves for the
        backend, through the same default, so the number on screen is the number on
        the wire. ``None`` (rather than a stand-in figure) when the entry is missing
        or states a cap this build would refuse — reporting a cap that is not the
        one in force is worse than reporting that it is not known.
        """
        entry = self.config.get("models", {}).get(self.config.get("default_model", ""))
        if not isinstance(entry, dict):
            return None
        cap = self._apply_run_config(entry).get("max_tokens", DEFAULT_MAX_TOKENS)
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            return None
        return cap

    async def _on_render_event(self, event: dict) -> None:
        """Render one lane-tagged event from the persistent bus subscription.

        Reference: docs/SUBMISSION-LIFECYCLE.md phase 3 / B3-a. Called for EVERY
        turn the session runs — this app's own typed prompts, an extension's or a
        bus driver's submission, and a ``fork``'s second agent — because the
        subscription is attached to the session rather than to a call.
        """
        display = self.query_one(transcript.ChatDisplay)
        kind = event.get("kind")
        lane = event.get("lane") or DEFAULT_LANE
        if kind == "lane_start":
            label = self._lane_label(event.get("source"), event.get("submitter"))
            role = "user" if label is None else self._lane_role(event.get("source"))
            bubble = display.add_message(
                role,
                elide_attachment_bodies(event.get("text", "")),
                subtitle=label or "",
                source="verbatim",
            )
            if label is not None:
                bubble.add_class(transcript.LANE_FOREIGN_CLASS)
            self.query_one(editor_widgets.LaneStrip).open_lane(lane, label)
            await display.begin_exchange(lane, label=label)
            return
        if kind == "lane_end":
            self.query_one(editor_widgets.LaneStrip).close_lane(lane)
            elapsed = event.get("seconds")
            telemetry = format_telemetry(event.get("extra") or {})
            await display.finalize_exchange(
                context=int(event.get("context", 0) or 0),
                output=int(event.get("output", 0) or 0),
                seconds=elapsed,
                telemetry=telemetry,
                lane=lane,
            )
            if self.current_session is not None:
                self.messages = list(self.current_session.context)
            self._report_cache_miss(display, event.get("cache_notice"))
            self._refresh_subtitle()
            return
        if kind == "completion_end" and event.get("stop_reason") == "length":
            self._report_truncation(display, int(event.get("dropped_tool_calls", 0) or 0))
        if kind == "tool_call":
            self._flush_pending_steer(at_tool_call=True)
        await display.handle_stream_event(event)

    def _bind_render_subscription(self) -> None:
        """(Re)attach the persistent renderer to the current backend's bus.

        One subscription per backend, dropped and remade when the backend or its
        session changes — the same lifetime ``_session_event_unsub`` has, and for
        the same reason: a replaced backend's dead bus must stop reaching this
        app's widgets.

        Lanes still open on the OLD router are abandoned rather than closed,
        deliberately: every caller of this method (new-chat, clear, resume,
        model-swap) also clears or reloads the transcript, so the exchange those
        lanes were drawing no longer exists to be finalized.

        ``getattr``-guarded like every other backend-capability read in this class:
        a test double or a non-``TauBackend`` simply renders nothing.
        """
        if self._render_router is not None:
            self._render_router.detach()
            self._render_router = None
        self.query_one(editor_widgets.LaneStrip).clear_lanes()
        subscribe_render = getattr(self.current_backend, "subscribe_render", None)
        if subscribe_render is None:
            return
        self._render_router = subscribe_render(self._on_render_event, on_orphan=self._log_orphan)

    def _log_orphan(self, reason: str) -> None:
        """Report an event that named no open lane (never drop it in silence).

        These are real — ``continue_conversation()`` on resume, and a bare
        ``compact()``, emit ``agent_start``/``agent_end`` with no submission to
        stamp them — and they are not errors, so this is the Textual log rather
        than a toast. What it is not is nothing: a renderer that swallowed them
        would be indistinguishable from one that had quietly stopped working.
        """
        self.log(f"render router: {reason}")

    def _bind_backend_session(self) -> None:
        """Rebind the backend's AgentSession onto the current live ``Session``,
        without going through ``AgentSessionRuntime``.

        The fallback path for a backend with no real ``AgentSession`` to build
        a runtime around (a test double, or a non-``TauBackend`` — the same
        tolerance every backend-capability read in this class already has).
        Every call site that DOES have a real ``AgentSession`` goes through
        ``AgentSessionRuntime`` instead (H1, phase 3): the runtime performs
        this same ``session_log`` bind internally (plus the H2 veto and H3
        reset this method knows nothing about) and then invokes
        :meth:`_rebind_after_session_swap` itself, via
        ``AgentSessionRuntime.set_rebind_session``. This method exists so the
        NO-runtime case still gets the ``session_log`` bind
        :meth:`_rebind_after_session_swap` does not perform.
        """
        binder = getattr(self.current_backend, "bind_session_log", None)
        if binder is not None and self.current_session is not None:
            binder(self.current_session)
        self._rebind_after_session_swap()

    def _rebind_after_session_swap(self) -> None:
        """Reattach TUI-specific plumbing to ``self.current_backend`` (H1's
        rebind callback).

        Everything ``AgentSessionRuntime``'s own swap does NOT know how to do
        — the seam-3 extension-bus bridge, the model-name resolver, the
        renderer — because it is TUI-specific, not session-lifecycle logic
        (``agent_session_runtime.py``'s module docstring is explicit that
        this is exactly why ``set_rebind_session`` exists rather than the
        runtime hardcoding it). Two callers:

        - :meth:`_bind_backend_session` (the no-runtime fallback above), which
          calls this directly, after its own ``session_log`` bind.
        - ``AgentSessionRuntime.set_rebind_session``'s callback, registered by
          ``action_new_chat``/``action_clear_chat``/``on_chat_selected``
          BEFORE calling ``new_session``/``fork``/``switch_session`` — invoked
          by the runtime itself, AFTER the swap's ``session_log`` is already
          live and its turn lock has been released (so this method reading
          ``self.current_backend.agent_session`` fresh, right here, can never
          race the swap that produced it).

        Reads ``self.current_backend``/``self.current_session`` rather than
        taking them as parameters — both callers above have ALREADY updated
        them before this runs.
        """
        if self._session_event_unsub is not None:
            self._session_event_unsub()
            self._session_event_unsub = None
        agent_session = getattr(self.current_backend, "agent_session", None)
        if agent_session is not None:
            self._session_event_unsub = subscribe_session_events(agent_session.route_session_event)
            binder = getattr(agent_session, "set_model_resolver", None)
            if binder is not None:
                binder(make_model_resolver(self.config.get("models", {})))

        self._bind_render_subscription()

        self._return_pending_to_the_editor()

    def _build_session_runtime(
        self, backend: Any, model: str, backend_name: str
    ) -> Optional[AgentSessionRuntime]:
        """``AgentSessionRuntime`` over ``backend``'s ``AgentSession`` — or
        ``None`` when ``backend`` has none (test double / non-``TauBackend``,
        the same tolerance :meth:`_rebind_after_session_swap` already has).

        Always installs :meth:`_rebind_after_session_swap` as the rebind
        callback — the one thing every one of the three call sites needs and
        would otherwise have to register identically three times.
        """
        agent_session = getattr(backend, "agent_session", None)
        if agent_session is None:
            return None
        runtime = AgentSessionRuntime(
            agent_session, self.session_catalog, os.getcwd(), model, backend_name, self._store_name
        )
        runtime.set_rebind_session(lambda _session: self._rebind_after_session_swap())
        return runtime

    def _apply_run_config(self, model_config: dict) -> dict:
        """Inject run-level tool flags into a model_config before create_backend (S28).

        Both tool-suppression flags empty the built-in set (``tools=[]``); which
        one was given rides as ``no_tools`` — ``"all"`` (``--no-tools``) or
        ``"builtin"`` (``--no-builtin-tools``) — the single resolved policy
        ``TauBackend`` forwards to ``AgentSession``. Only ``"all"`` also withholds
        extension-registered tools; under ``"builtin"`` they merge in as usual
        (``AgentSession._build_turn_tools``). ``--exclude-tools`` rides as an
        ``exclude_tools`` denylist that ``TauBackend`` applies to the resolved
        built-ins. Returns the config unchanged when no flag is set, so a bare
        ``tau`` is untouched; otherwise a shallow copy (never mutate the shared
        ``config["models"]`` entry).

        This runs at EVERY ``create_backend``, which is what makes these flags
        survive a mid-session ``/model`` switch: the policy is re-applied to
        whichever model entry the switch selected, instead of having been baked
        into the one entry the process started on.
        """
        global_replay = self.config.get("reasoning_replay")
        inject_replay = global_replay is not None and "reasoning_replay" not in model_config
        base_prompt = model_config.get("system_prompt") or self.config.get("system_prompt")
        inject_prompt = bool(base_prompt) and "system_prompt" not in model_config
        global_max_turns = self.config.get("max_turns")
        resolved_max_turns: Optional[int] = None
        if self._max_turns is not None:
            resolved_max_turns = self._max_turns
        elif global_max_turns is not None and "max_turns" not in model_config:
            resolved_max_turns = int(global_max_turns)
        if not (
            self._exclude_tools
            or self._no_tools
            or self._tool_allowlist is not None
            or inject_replay
            or inject_prompt
            or self._append_system_prompt
            or self._bus_available
            or self._no_context_files
            or resolved_max_turns is not None
        ):
            return model_config
        mc = dict(model_config)
        if self._tool_allowlist is not None:
            mc["tools"] = list(self._tool_allowlist)
        if self._no_tools:
            mc["no_tools"] = self._no_tools
            mc["tools"] = []
        if self._exclude_tools:
            mc["exclude_tools"] = self._exclude_tools
        if inject_prompt:
            mc["system_prompt"] = base_prompt
        if self._append_system_prompt:
            mc["append_system_prompt"] = list(self._append_system_prompt)
        if inject_replay:
            mc["reasoning_replay"] = global_replay
        if self._bus_available:
            mc["bus_available"] = True
        if self._no_context_files:
            mc["no_context_files"] = True
        if resolved_max_turns is not None:
            mc["max_turns"] = resolved_max_turns
        return mc

    async def _load_backend_extensions(self) -> None:
        """Load file-path extensions into the current backend's live session (E5 §2.2).

        Called after every ``create_backend`` (new-chat, model-swap, resume) so a
        file extension's mutating hooks fire in the ``AgentSession`` that backend
        drives — the TUI half of the seam the E0–E4 loader left disconnected (§0).
        Loads the run-level explicit ``-e`` paths + ``~/.tau/extensions`` discovery
        (unless ``-ne``).

        Errors are surfaced as TUI notices, never to stderr — a stderr write during
        a live Textual render corrupts the screen (this is why the loader stopped
        printing, S25). Both *discovered* AND *explicit* ``-e`` failures are collected
        into ``result.errors`` (``collect_explicit_errors=True``) and shown as
        per-extension warnings, so the extensions that DID load stay bound and the
        ``/extensions`` listing shows them plus a "Load errors" section — a launched
        TUI can't cleanly abort mid-load, and raising past the partial result left
        the listing empty while the good extensions' tools kept working (split-brain
        fix, docs/EXTENSIONS-DEMO-ROADMAP.md). The outer ``except`` remains a
        backstop for a non-per-extension failure (e.g. config resolution). Guarded by
        ``getattr`` so a backend without the seam (a test double, a non-``TauBackend``)
        is a no-op.
        """
        set_delegate = getattr(self.current_backend, "set_ui_delegate", None)
        if set_delegate is not None:
            set_delegate(extension_ui._ExtensionUIDelegate(self))

        loader = getattr(self.current_backend, "load_extensions", None)
        if loader is None:
            return
        extensions_config = resolve_extensions_config(self.config, self._ext_config_overrides)
        try:
            result = await loader(
                self._extension_paths or None,
                discover=self._discover_extensions,
                extensions_config=extensions_config,
                collect_explicit_errors=True,
            )
        except Exception as e:
            self.notify(f"Extension failed to load: {e}", severity="error")
            self.log.error(f"Extension load failed: {e}", exc_info=True)
            return
        for err in result.errors:
            self.notify(f"Extension error ({err.path}): {err.error}", severity="warning")
        if result.extensions:
            self.log(f"Loaded {len(result.extensions)} extension(s)")

        emit_start = getattr(self.current_backend, "emit_session_start", None)
        if emit_start is not None:
            await emit_start("startup")

    async def action_new_chat(self, model: Optional[str] = None):
        """Start a new chat."""
        if model is None:
            model = self.config.get("default_model", "local-llm")

        self.log(f"Starting new chat with model: {model}")

        # Get model config
        model_config = self.config["models"].get(model)
        if not model_config:
            self.notify(f"Unknown model: {model}", severity="error")
            self.log(f"Available models: {list(self.config['models'].keys())}")
            return

        # Create backend
        try:
            self.current_backend = create_backend(self._apply_run_config(model_config))
            self.log(f"Created backend: {model_config.get('backend')} for model {model}")
        except Exception as e:
            self.notify(f"Failed to create backend: {str(e)}", severity="error")
            self.log.error(f"Backend creation failed: {e}", exc_info=True)
            return

        system_prompt = getattr(self.current_backend, "system_prompt", "") or ""
        self._session_runtime = self._build_session_runtime(
            self.current_backend, model, model_config["backend"]
        )
        if self._session_runtime is not None:
            result = await self._session_runtime.new_session(
                persist=True, system_prompt=system_prompt or None
            )
            if result.get("blocked"):
                self.notify(result["reason"], severity="warning")
                return
            if result["cancelled"]:
                self.notify("New chat cancelled by an extension", severity="warning")
                return
            self.current_session = result["session"]
        else:
            self.current_session = self.session_catalog.create(
                os.getcwd(),
                model,
                model_config["backend"],
                system_prompt=system_prompt or None,
            )
            self._bind_backend_session()
        await self._load_backend_extensions()
        self.messages = list(self.current_session.context)

        # Clear display
        display = self.query_one(transcript.ChatDisplay)
        await display.clear_messages()

        # Update UI
        self.sub_title = f"{model}"
        self.notify(f"Started new chat with {model}")

        self.query_one(ChatSidebar).refresh_chats()

    def action_toggle_sidebar(self):
        """Toggle sidebar visibility.

        Records the flip as ``_sidebar_open`` and lets ``_apply_side_columns`` do
        the write, so the key always inverts what is ON SCREEN — including opening
        the sidebar next to an extension panel on a narrow terminal, which costs
        the chat columns and is nonetheless what was asked for. The choice sticks
        (see the attribute): a keypress is not a hint the layout may overrule.

        This is the ONLY way the sidebar opens now that §8 mounts it closed, which
        is why the binding is listed in the Footer rather than hidden.
        """
        self._sidebar_open = not self.query_one(ChatSidebar).display
        self._apply_side_columns()

    def action_toggle_reasoning(self) -> None:
        """Fold/unfold every reasoning region in the transcript at once.

        A global override of the per-completion behavior (reasoning streams
        expanded then auto-folds when the answer begins): one keypress hides all
        the thinking, or expands it for review. Smart-toggle — if any region is
        open it collapses all, otherwise it expands all — so the key always does
        something visible regardless of the mixed starting states."""
        self.reasoning_collapsed = self._fold_all(self.query(ReasoningRegion), "Reasoning")

    def action_toggle_tools(self) -> None:
        """Fold/unfold every tool box (call + result) in the transcript at once."""
        self.tools_collapsed = self._fold_all(self.query(ToolBox), "Tool output")

    async def action_show_all_messages(self) -> None:
        """Mount the messages a capped reload left off screen.

        Says so when there is nothing to show, rather than looking broken. The
        conversation was never truncated — :meth:`ChatDisplay.reload_messages`
        bounds what is MOUNTED, and this lifts that bound for the current view.
        """
        display = self.query_one(transcript.ChatDisplay)
        hidden = display.hidden_count
        if not hidden:
            self.notify("The whole conversation is already on screen.")
            return
        self.notify(f"Mounting {hidden} messages…")
        await display.show_all_messages()

    def action_show_extensions(self) -> None:
        """The ``/extensions`` view: two reads, composed and rendered (E5 §5 / S34).

        ``get_extension_state`` says what is loaded and what failed; the separate
        ``list_managed_extensions`` says which are enabled. Composing two reads and
        holding no mutation is what makes this a view rather than a flow, and it is why
        the listing shows files that failed to import — those can never be a legal
        ``extension_name``, so no domain would ever offer them.

        It reads LIVE rather than from the load-time result the app used to cache, which
        went stale on reload: an extension whose registered tools changed showed its old
        list until the app was restarted.

        Display-only chrome, NOT a conversation node — neither appended to the working
        message list nor persisted, so the model's input stays system prompt plus the
        linear active path (D-E5-6 is lifted without touching that invariant).
        """
        reader = getattr(self.current_backend, "get_extension_state", None)
        if reader is None:
            self.notify("Extension state is unavailable here", severity="warning")
            return
        listing = self._format_extensions_listing(reader(), self._disabled_extension_paths())
        self.query_one(transcript.ChatDisplay).add_message("system", listing, source="markdown")

    def _disabled_extension_paths(self) -> set[str]:
        """The set of currently runtime-disabled extension paths (E10 §6 / S70).

        Read from the live backend's managed-extension state; ``getattr``-guarded so a
        non-``TauBackend`` test double (no seam) reports none disabled.
        """
        lister = getattr(self.current_backend, "list_managed_extensions", None)
        if lister is None:
            return set()
        return {path for path, enabled in lister() if not enabled}

    async def action_manage_extensions(self, verb: str, target: str) -> None:
        """The ``/extensions <verb> <name>`` shorthand — E10 §6 / S70.

        Head-local sugar over the three extension flows, kept because it is what
        readers already type. The verb table is derived from :data:`_EXTENSION_FLOWS`
        rather than written here, so the view cannot offer a verb the registry does not
        declare; an unknown verb is reported, never guessed at (Fail-Early).
        """
        flow = _EXTENSION_FLOWS.get(verb)
        if flow is None:
            legal = " | ".join(sorted(_EXTENSION_FLOWS))
            self.notify(
                f"Unknown /extensions action {verb!r} (use: {legal})",
                severity="error",
            )
            return
        await self.action_run_extension_flow(flow, target)

    async def action_run_session_flow(self, flow: str, raw: str = "") -> None:
        """Run a declared flow from the text typed after its slash command.

        The generic performer, kept as a named action because the palette and the
        ``/extensions`` verb sugar both reach it. It steps the flow through the core
        and then hands whichever arm came back to the same two renderers dispatch
        uses, so a flow driven from here and one typed as a slash line cannot come to
        behave differently.

        Args:
            flow: A declared flow name.
            raw: Everything the reader typed after the command word.
        """
        vocabulary = self._vocabulary()
        try:
            bound = bind_command_args(flow, raw, vocabulary)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        session = getattr(self.current_backend, "agent_session", None)
        log = getattr(session, "session_log", None)
        outcome = next_step(flow, bound, cursor=getattr(log, "cursor", None), vocabulary=vocabulary)
        if isinstance(outcome, Ready):
            await self._perform_ready(outcome)
            return
        self._render_flow_step(outcome)

    async def action_run_extension_flow(self, flow: str, path: str = "") -> None:
        """Run one extension flow. The same generic path, under the name it had.

        Args:
            flow: A declared flow name — one of :data:`_EXTENSION_FLOWS`'s values.
            path: The extension to act on. Empty offers the loaded ones.
        """
        await self.action_run_session_flow(flow, path)

    async def _dispatch_extension_command(self, name: str, args: str = "") -> None:
        """Run an extension-registered command from the palette (E5 §5 / S35).

        The command-palette entry (:meth:`get_system_commands`) invokes this;
        it forwards to the live backend's :meth:`run_extension_command`. A palette
        entry for a command that declares ``"args"`` first collects the arg string
        via :meth:`_prompt_command_args` (S51) and passes it here; a command without
        an ``args`` placeholder dispatches with the empty string (the palette has no
        argument line). A handler exception is surfaced as an error notice (pi's
        ``_tryExecuteExtensionCommand`` likewise reports rather than crashing the
        screen), never swallowed silently.
        """
        runner = getattr(self.current_backend, "run_extension_command", None)
        if runner is None:
            return
        try:
            result = await runner(name, args)
        except Exception as e:
            self.notify(f"Command /{name} failed: {e}", severity="error")
            self.log.error(f"Extension command /{name} failed: {e}", exc_info=True)
            return
        self._resync_working_list()
        self._render_command_output(result)
        # A command is how a lock is released, and how a new one arrives (EXTENSION-LOCKS §6).
        self.refresh_extension_request()

    @work
    async def _prompt_command_args(self, name: str, placeholder: str) -> None:
        """Collect an arg string for an ``args``-declaring palette command (E7 §3 / S51).

        A command that declares ``"args": "<placeholder>"`` expects a free-form
        argument string, exactly as if the user had typed ``/name args``. The palette
        entry has no argument line, so this opens the S47 :class:`ExtensionInputModal`
        to collect it, then dispatches through :meth:`_dispatch_extension_command`
        with the entered text. Runs as a worker because ``push_screen_wait`` requires
        one (the same context the S47 delegate uses).

        Fail-Early: a cancelled modal (Cancel → ``None``) does NOT dispatch — an
        arg-declaring command is not run on a fabricated empty argument the user
        never confirmed. An entered (possibly empty) value dispatches as typed.
        """
        collected = await self.push_screen_wait(
            modals.ExtensionInputModal(f"/{name} {placeholder}".rstrip())
        )
        if collected is None:
            return
        await self._dispatch_extension_command(name, collected)

    def _resync_working_list(self) -> None:
        """Re-read the session's context after a command ran (§9.1).

        A turn rebuilds :attr:`messages` when it ends; a command never did, so a
        message an extension appended from a handler existed on the tree and in
        no list this app reads — mounted by the ``custom_message`` channel, then
        gone at the next window rebuild, and back again only after a restart. A
        command is the second place the path can grow, so it re-reads too.

        No :attr:`_working_list_lock`: a dispatched command runs no turn, so
        there is no turn holding the lock and nothing to serialise against.
        """
        if self.current_session is not None:
            self.messages = list(self.current_session.context)

    def _render_command_output(self, result: ExtensionCommandResult) -> None:
        """Render a command's returned value as a display-only ``system`` box (S46).

        Same chrome as ``/extensions`` (:meth:`action_show_extensions`) — a
        ``system`` ``MessageBox`` mounted into the transcript view. It is
        deliberately NOT added to ``self.messages`` (the working list that becomes
        the model's context), so a command's report cannot leak into model input,
        preserving the E5 §1 tree-as-truth invariant. A command that returned
        nothing (``output_text() is None``) shows no box.
        """
        text = result.output_text()
        if text is None:
            return
        self.query_one(transcript.ChatDisplay).add_message("system", text, source="verbatim")

    @staticmethod
    def _format_extensions_listing(
        result: LoadExtensionsResult, disabled: set[str] | None = None
    ) -> str:
        """Render a ``LoadExtensionsResult`` as the ``/extensions`` listing text (S34).

        Pure (no widget access) so it is unit-testable: given the load result it
        returns the exact markdown the listing box shows — a section per loaded
        extension (name, path, tools/commands/hooks) plus a load-errors section.
        ``disabled`` is the set of runtime-disabled extension paths (E10 §6 / S70); a
        disabled extension is tagged in its heading so the listing reflects live state.
        """
        disabled = disabled or set()
        infos = summarize_extensions(result)
        if not infos and not result.errors:
            return "No extensions loaded."

        lines: list[str] = ["# Extensions"]
        for info in infos:
            lines.append("")
            status = " _(disabled)_" if info.path in disabled else ""
            lines.append(f"**{info.name}**{status} — `{info.path}`")
            lines.append(f"- hooks: {', '.join(info.hooks) if info.hooks else '(none)'}")
            lines.append(f"- tools: {', '.join(info.tools) if info.tools else '(none)'}")
            lines.append(f"- commands: {', '.join(info.commands) if info.commands else '(none)'}")
            shortcuts_disp = ", ".join(f"ctrl+e {k}" for k in info.shortcuts) or "(none)"
            lines.append(f"- shortcuts: {shortcuts_disp}")

        if result.errors:
            lines.append("")
            lines.append("## Load errors")
            for err in result.errors:
                lines.append(f"- `{err.path}`: {err.error}")

        return "\n".join(lines)

    def _fold_all(self, widgets, label: str) -> bool:
        """Collapse all ``widgets`` if any is currently expanded, else expand all.

        Returns the applied collapsed state (also recorded on the reactive for
        the binding's intent). A no-op when there are no such widgets yet."""
        items = list(widgets)
        if not items:
            return False
        target_collapsed = any(not w.collapsed for w in items)
        for w in items:
            w.collapsed = target_collapsed
        self.notify(f"{label} {'collapsed' if target_collapsed else 'expanded'}")
        return target_collapsed

    @staticmethod
    def _aggregate_label(messages: list[dict]) -> str:
        """Conversation-level rollup: tool calls, cumulative usage, current context.

        Follows pi's footer (``modes/interactive/components/footer.ts``): the
        cumulative counts are broken out per direction — ``↑`` uncached input,
        ``↓`` output, ``R`` cache reads, ``W`` cache writes — and the context size
        is a SEPARATE, non-cumulative number.

        This replaced a single ``Σ total_tokens`` over every assistant message.
        That sum was quadratic in conversation length: each completion's
        ``total_tokens`` includes its whole prompt, and each prompt contains every
        earlier turn, so an N-turn conversation counted turn 1 N times. On a real
        17-message session it read 192.9k for a 22.6k-token conversation that had
        generated 10.5k tokens.

        Derived purely from the transcript, so it reads identically for a live
        session and a reloaded one. Wall-clock time is intentionally absent — it
        is not persisted per completion, and we don't fabricate it (Fail-Early).
        Returns an empty string when there's nothing to roll up yet."""
        assistants = [m for m in messages if m.get("role") == "assistant"]
        tools = sum(
            1
            for m in assistants
            for b in (m.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "toolCall"
        )
        usages = [u for m in assistants if isinstance(u := m.get("usage"), dict)]
        totals = {
            key: sum(int(u.get(key, 0) or 0) for u in usages)
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        }
        context = prompt_tokens(usages[-1]) if usages else 0
        parts = [f"{tools} tool" + ("" if tools == 1 else "s")] if tools else []
        arrows = " ".join(
            f"{prefix}{format_tokens(totals[key])}"
            for prefix, key in (
                ("↑", "input_tokens"),
                ("↓", "output_tokens"),
                ("R", "cache_read_tokens"),
                ("W", "cache_write_tokens"),
            )
            if totals[key]
        )
        if arrows:
            parts.append(arrows)
        if context:
            parts.append(f"{format_tokens(context)} ctx")
        return " · ".join(parts)

    def _refresh_subtitle(self) -> None:
        """Set the header subtitle to the model plus the conversation rollup."""
        if not self.current_session:
            return
        agg = self._aggregate_label(self.messages)
        model = str(self.current_session.model)
        self.sub_title = f"{model} · {agg}" if agg else model

    def action_focus_and_send(self):
        """Focus input and send if focused (for global hotkey)."""
        input_widget = self.query_one(ChatInput)
        if input_widget.has_focus:
            input_widget.action_submit()
        else:
            input_widget.focus()

    def action_focus_and_send_on_enter(self):
        """The same action, under a second name, for the Enter binding.

        Two names rather than one because :meth:`check_action` is told which
        ACTION is being offered, never which key offered it. The Enter and Ctrl+J
        bindings must be gated in opposite directions, so they cannot share an
        action name and still be told apart.
        """
        self.action_focus_and_send()

    def _builtin_vocabulary_commands(self):
        """One palette entry per built-in slash command, read off the core's table.

        The palette used to hand-write an entry per built-in, which is how it
        drifted: ``/fork`` had no entry at all, and ``Resume session…`` /
        ``Compact Conversation`` were second spellings of the same two things the
        editor already offered. Both surfaces now project
        :data:`~tau_agent_core.commands.FRONTEND_COMMANDS`, so a flow added to the
        core's table appears here without an edit to this file, and a flow removed
        from it disappears from both at once.

        Each entry routes through :meth:`_perform_command_outcome` rather than
        calling an action directly, which is the same door the slash command uses —
        so there is one implementation per command and this only chooses the door.

        Yields:
            A :class:`SystemCommand` per built-in, titled ``/name`` the way the
            extension-command entries below already are.
        """
        for name, description in FRONTEND_COMMANDS.items():
            yield SystemCommand(
                f"/{name}",
                description,
                lambda n=name: self._perform_builtin_command(n),
            )

    @work
    async def _perform_builtin_command(self, name: str) -> None:
        """Run a built-in slash command from the palette, with no arguments.

        A worker because :meth:`_dispatch_command_submission` is a coroutine and
        ``SystemCommand``'s callback is not awaited — the palette calls it and
        returns. It goes through the SAME door a typed line goes through rather than
        building an arm here: which arm ``/resume`` is depends on what the core
        resolves, and a head that decided that for itself is the drift the union
        removes. The absent argument is what makes a palette entry the *first step*
        of a flow rather than a whole one: ``/resume`` with nothing bound opens the
        picker, exactly as typing it does.
        """
        await self._dispatch_command_submission(
            Submission(
                text=f"/{name}",
                source="interactive",
                submitter="human",
                submission_id=uuid4().hex,
                multitask_strategy="enqueue",
                expand_commands=True,
                allow_user_input=True,
            )
        )

    def get_system_commands(self, screen):
        """Provide commands for the command palette."""
        yield from super().get_system_commands(screen)

        # Model switching commands
        models = self.config.get("models", {})
        self.log(f"Generating commands for {len(models)} models: {list(models.keys())}")

        for model_name in models.keys():
            yield SystemCommand(
                f"New Chat: {model_name}",
                f"Start a new chat with {model_name}",
                lambda m=model_name: self.run_action(f'new_chat("{m}")'),
            )

        yield from self._builtin_vocabulary_commands()

        yield SystemCommand("Clear Chat", "Clear current conversation", self.action_clear_chat)

        yield SystemCommand("Export Chat", "Export chat to markdown", self.action_export_chat)

        yield SystemCommand(
            "Roll back the in-flight turn…",
            "Abort the running turn, drop it off the active path, and run another "
            "prompt from where it started",
            self.action_rollback_turn,
        )

        yield SystemCommand(
            "Edit System Prompt",
            "Edit the system prompt for new chats",
            self.action_edit_system_prompt,
        )

        yield SystemCommand(
            "Show earlier messages",
            "Mount the whole conversation, not just the last few turns. Slow on a long one.",
            self.action_show_all_messages,
        )

        yield SystemCommand(
            "Toggle Reasoning",
            "Collapse/expand all reasoning regions",
            self.action_toggle_reasoning,
        )

        yield SystemCommand(
            "Toggle Tool Output",
            "Collapse/expand all tool call/result boxes",
            self.action_toggle_tools,
        )

        for theme_name in sorted(self._theme_registry):
            active = " (active)" if theme_name == self.theme else ""
            yield SystemCommand(
                f"Theme: {theme_name}{active}",
                f"Switch to the {theme_name} colours and save the choice to config.json",
                lambda t=theme_name: self.run_action(f'set_theme("{t}")'),
            )

        get_commands = getattr(self.current_backend, "get_extension_commands", None)
        get_args = getattr(self.current_backend, "get_extension_command_args", None)
        if get_commands is not None:
            for cmd_name, cmd_desc in get_commands():
                help_text = cmd_desc or f"Run extension command /{cmd_name}"
                placeholder = get_args(cmd_name) if get_args is not None else None
                if placeholder:
                    yield SystemCommand(
                        f"/{cmd_name}",
                        help_text,
                        lambda n=cmd_name, p=placeholder: self._prompt_command_args(n, p),
                    )
                else:
                    yield SystemCommand(
                        f"/{cmd_name}",
                        help_text,
                        lambda n=cmd_name: self._dispatch_extension_command(n),
                    )

        get_shortcuts = getattr(self.current_backend, "get_extension_shortcuts", None)
        if get_shortcuts is not None:
            for key, command, args, desc in get_shortcuts():
                help_text = desc or f"Run extension command /{command}"
                yield SystemCommand(
                    f"ctrl+e {key}  →  /{command}",
                    help_text,
                    lambda c=command, a=args: self._dispatch_extension_command(c, a),
                )

    async def action_clear_chat(self):
        """Clear the current conversation, starting a fresh session.

        The store is append-only, so "clear" can't truncate the file in place —
        it begins a new session carrying just the system prompt (the prior
        session stays on disk as its own transcript).
        """
        if not self.current_session:
            return

        system_msg = next((m for m in self.messages if m.get("role") == "system"), None)
        system_prompt = (
            system_msg["content"]
            if system_msg and isinstance(system_msg.get("content"), str)
            else None
        )
        if self._session_runtime is not None:
            result = await self._session_runtime.new_session(
                persist=True, system_prompt=system_prompt
            )
            if result.get("blocked"):
                self.notify(result["reason"], severity="warning")
                return
            if result["cancelled"]:
                self.notify("Clear chat cancelled by an extension", severity="warning")
                return
            self.current_session = result["session"]
        else:
            self.current_session = self.session_catalog.create(
                os.getcwd(),
                self.current_session.model,
                self.current_session.backend,
                system_prompt=system_prompt,
            )
            self._bind_backend_session()
        self.messages = list(self.current_session.context)

        # Clear display
        display = self.query_one(transcript.ChatDisplay)
        await display.clear_messages()
        self.query_one(ChatSidebar).refresh_chats()

        self.notify("Chat cleared")

    def action_set_model(self, name: str = "") -> None:
        """Switch the active model, or say which names are legal when none was given.

        The acceptance test for the capability/flow model (§11 step 6): a command τ
        did not have, added as one registry row plus a backend passthrough, with no
        new modal and no new completion path. An empty ``name`` is the flow's first
        STEP, so it reports the domain's values instead of failing — which is the
        same answer ``next_step`` + ``enumerate_domain`` give a host over the wire,
        rendered for a terminal.

        Args:
            name: A config model name. Empty offers the legal ones.
        """
        set_model = getattr(self.current_backend, "set_model", None)
        if set_model is None:
            self.notify("This backend cannot switch models", severity="warning")
            return

        session = getattr(self.current_backend, "agent_session", None)
        if not name:
            try:
                found = enumerate_domain("model_name", session=session)
            except ValueError as exc:
                self.notify(str(exc), severity="error")
                return
            self.notify("Models: " + ", ".join(v.value for v in found.values))
            return

        try:
            performed = set_model(name)
        except (KeyError, ValueError, RuntimeError) as exc:
            self.notify(f"Cannot switch model: {exc}", severity="error")
            return
        self._refresh_subtitle()
        model = performed.data["model"]
        self.notify(f"Model is now {model.get('name', name)} (from the next turn)")

    async def action_fork_session(self):
        """Branch this session's active path into a new one and continue on it.

        The TUI half of the ``fork`` capability, which the RPC verb of that name
        has exposed since phase 3 and no head could reach. Delegates to
        :meth:`AgentSessionRuntime.fork`, which snapshots AFTER the turn lock so a
        fork never captures a half-written turn, and leaves the SOURCE session
        untouched — only this runtime moves onto the copy.

        The three outcomes ``fork()`` can report are each surfaced rather than
        collapsed: ``blocked`` (a turn is running), ``cancelled`` (an extension's
        ``session_before_switch`` hook vetoed), and success. Without a runtime
        there is no catalog to fork through, and this says so instead of appearing
        to work — the same refusal ``fork()`` itself makes for a session the
        catalog cannot address.
        """
        if self._session_runtime is None:
            self.notify("Forking needs a persistent session", severity="warning")
            return
        try:
            result = await self._session_runtime.fork()
        except RuntimeError as exc:
            self.notify(f"Cannot fork: {exc}", severity="error")
            return
        if result.get("blocked"):
            self.notify(result["reason"], severity="warning")
            return
        if result["cancelled"]:
            self.notify("Fork cancelled by an extension", severity="warning")
            return

        self.current_session = result["session"]
        self.messages = list(self.current_session.context)
        await self._reload_transcript()
        self.query_one(ChatSidebar).refresh_chats()
        self._refresh_subtitle()
        self.notify(f"Forked into: {self.current_session.display_title()}")

    async def action_export_chat(self):
        """Export current session to markdown."""
        if not self.current_session:
            self.notify("No chat to export", severity="warning")
            return

        # Build markdown
        created = datetime.fromisoformat(self.current_session.header["timestamp"])
        lines = [f"# {self.current_session.display_title()}\n"]
        lines.append(f"Model: {self.current_session.model}\n")
        lines.append(f"Date: {created.astimezone().strftime('%Y-%m-%d %H:%M')}\n")
        lines.append("---\n")

        for msg in self.messages:
            role = msg["role"].capitalize()
            content = _join_text_blocks(msg.get("content", ""))
            lines.append(f"## {role}\n\n{content}\n")

        # Save to file
        export_path = TAU_DIR / "exports"
        export_path.mkdir(parents=True, exist_ok=True)

        filename = f"chat_{self.current_session.id}.md"
        file_path = export_path / filename
        file_path.write_text("\n".join(lines))

        self.notify(f"Exported to {file_path}")

    async def action_compact(self, custom_instructions: str = "") -> None:
        """Compact the current conversation into a summary checkpoint.

        Summarizes the older messages via the model and replaces them with a
        single checkpoint, freeing context for the conversation to continue.
        Operates on ``self.messages`` — the live list sent to the model — then
        re-renders. The session file keeps the full transcript (append-only, no
        rewrite); compaction is a runtime context optimization on the working
        list, so a resumed session still has its complete history.

        Args:
            custom_instructions: Extra focus for the summary — everything the
                reader typed after ``/compact``. Empty (the keybinding and the
                palette, which pass nothing) becomes ``None`` at the backend
                call, because the summarizer's prompt distinguishes "no extra
                focus" from an empty focus line.

        The ``compact`` flow declares this argument as optional and
        :meth:`_perform_command_outcome` now hands it over; before that it called
        this method with no arguments and ``/compact focus on the auth bug``
        summarized without the focus (docs/SLASH-COMMANDS.md §4).
        """
        if not self.current_session:
            self.notify("No chat to compact", severity="warning")
            return

        backend = self.current_backend
        if backend is None:
            self.notify("No chat to compact", severity="warning")
            return
        if not hasattr(backend, "compact_messages"):
            self.notify("This backend does not support compaction", severity="warning")
            return

        focus = custom_instructions.strip() or None
        self.notify(
            f"Compacting conversation, focus: {focus}…" if focus else "Compacting conversation…"
        )
        self.sub_title = "Compacting…"
        before = len(self.messages)
        try:
            new_messages = await backend.compact_messages(self.messages, focus)
        except Exception as e:
            self.notify(f"Compaction failed: {e}", severity="error")
            self.log.error(f"Compaction failed: {e}")
            self.log.error(traceback.format_exc())
            self._refresh_subtitle()
            return

        if new_messages is None:
            self.notify("Nothing to compact yet")
            self._refresh_subtitle()
            return

        self.messages = new_messages
        # reload_messages lives on the ChatDisplay widget, not the app.
        await self._reload_transcript()
        self._refresh_subtitle()
        self.notify(f"Compacted {before} → {len(new_messages)} messages")

    @work(group="session-picker")
    async def action_resume_session(self) -> None:
        """Open the session picker and load whatever it returns (§6).

        A worker so ``push_screen_wait`` is legal here, exactly as
        :meth:`action_browse_tree` is one — that is the ``push_screen_wait``
        branch of the either/or §6 states, chosen once rather than tried
        alongside a ``push_screen(callback)`` fallback.

        The chosen ``ref`` is posted as a :class:`ChatSelected`, which is the
        message the sidebar already posts, so the picker adds an entry point and
        not a second loader: model lookup, backend construction, the runtime's
        switch/veto and the transcript reload all stay in
        :meth:`on_chat_selected`. ``os.getcwd()`` rather than ``self._cwd`` for
        the scope, matching ``ChatSidebar._refresh_chats_worker`` — ``_cwd`` is
        the string the empty pane prints, not the directory sessions are keyed on.
        """
        ref = await self.push_screen_wait(SessionPickerModal(self.session_catalog, os.getcwd()))
        if ref is None:
            return
        self.post_message(ChatSelected(ref))

    @work
    async def action_browse_tree(self) -> None:
        """Open the tree-browser and act on the chosen node (§3).

        Runs as a worker so it can ``push_screen_wait`` the modal steps (browse →
        mode → optional custom instructions, or → a second browse for ``elide``).
        Operates on the LIVE ``current_session`` — the TUI owns persistence (§2.6) —
        building a ``ConversationTree`` over its entries and handing the picked node
        to ``backend.navigate_tree``, which appends the ``navigate``/``branch_summary``
        entry and returns the post-navigate context. Re-renders through the same
        path ``action_compact`` uses (§3.4): swap ``self.messages`` + reload.

        The ``elide`` mode (W3) branches off to :meth:`_elide_span_flow` after the
        mode pick, because it needs a second node id rather than a summarizer.

        **A ``paste`` re-opens the browser instead of returning** (§7). Pasting
        edits the tree without moving the cursor, so returning to the conversation
        would show the reader the same transcript they left and no sign that
        anything happened; re-opening puts the copied rows on screen where they
        landed. The clipboard rides along, so one copy can be pasted in several
        places. Every other intent leaves the browser, as before.
        """
        session = self.current_session
        if session is None:
            self.notify("No conversation to browse", severity="warning")
            return
        navigate_tree = getattr(self.current_backend, "navigate_tree", None)
        if navigate_tree is None:
            self.notify("This backend does not support tree navigation", severity="warning")
            return

        copied: Optional[str] = None
        while True:
            tree = ConversationTree(session.entries(), session.cursor)
            roots = tree.tree()
            if not roots:
                self.notify("Conversation tree is empty", severity="warning")
                return

            intent = await self.push_screen_wait(tree_browser.SessionTreeModal(tree, copied=copied))
            if intent is None:
                return
            if intent.action == "elide":
                anchor_id, first_kept_id = intent.ids
                await self._elide_span_flow(session, anchor_id, first_kept_id)
                return
            if intent.action == "branch":
                await self._branch_flow(session, intent.ids)
                return
            if intent.action == "paste":
                source_id, target_id = intent.ids
                if not await self._paste_flow(session, source_id, target_id):
                    return
                copied = source_id
                continue
            break

        picked_id = intent.sole_id
        prefill: Optional[str] = None
        target_id = picked_id
        if intent.action == "revise":
            revised = tree.entry(picked_id)
            parent_id = revised.get("parentId")
            if parent_id is None:
                self.notify(
                    "That is the first message — there is no earlier point to fork from.",
                    severity="warning",
                )
                return
            target_id = str(parent_id)
            prefill = tree.message_text(picked_id)

        mode = await self.push_screen_wait(tree_browser.TreeModeModal())
        if mode is None:
            return

        if target_id == session.cursor:
            self.notify("Already at that node")
            return

        custom_instructions: Optional[str] = None
        if mode == "custom":
            custom_instructions = await self.push_screen_wait(
                tree_browser.TreeCustomInstructionsModal()
            )
            if custom_instructions is None:
                return

        summarize = mode in ("summarize", "custom")
        self.sub_title = "Summarizing branch…" if summarize else "Navigating tree…"
        try:
            new_messages = await navigate_tree(
                session,
                target_id,
                summarize=summarize,
                custom_instructions=custom_instructions,
            )
        except Exception as e:
            self.notify(f"Tree navigation failed: {e}", severity="error")
            self.log.error(f"Tree navigation failed: {e}")
            self.log.error(traceback.format_exc())
            self._refresh_subtitle()
            return

        self.messages = new_messages
        await self._reload_transcript()
        self._refresh_subtitle()
        if prefill is None:
            self.notify(
                "Summarized and moved to selected node" if summarize else "Moved to selected node"
            )
            return
        editor = self.query_one("#chat-input", ChatInput)
        editor.text = prefill
        editor.move_cursor(editor.document.end)
        editor.focus()
        self.notify("Forked from before that message — edit it and send.")

    async def _branch_flow(
        self,
        session: ConversationSession,
        ids: tuple[str, ...],
    ) -> None:
        """Ask how much context the branch keeps, then commit it (§6).

        The mode chooser is a screen of its own (:class:`BranchModeModal`) rather
        than a key in the browser, because it is a question about the branch as a
        whole and both answers are legal on the same selection — unlike the elide,
        whose legality depends on the two nodes and therefore has to be judged where
        they are visible.

        The plan is computed HERE, before the commit, so the notification can state
        what happened in the reader's terms — how many messages were reused in place
        and how many were copied. Recomputing it afterwards would describe a
        different tree. It is computed from the session as it stands NOW rather than
        from the tree the browser opened with: the mode chooser is a screen, a turn
        can finish while it is up, and a count read off a stale snapshot would
        describe a commit that did not happen.
        """
        commit_branch = getattr(self.current_backend, "commit_branch", None)
        if commit_branch is None:
            self.notify("This backend does not support branching", severity="warning")
            return

        mode = await self.push_screen_wait(tree_browser.BranchModeModal())
        if mode is None:
            return
        drop_context = mode == "only"

        try:
            tree = ConversationTree(session.entries(), session.cursor)
            plan = plan_branch(tree, ids, drop_context=drop_context)
        except ValueError as exc:
            self.notify(f"Cannot branch from this selection: {exc}", severity="warning")
            return

        self.sub_title = "Building branch…"
        try:
            new_messages = commit_branch(session, ids, drop_context=drop_context)
        except Exception as e:
            self.notify(f"Branch failed: {e}", severity="error")
            self.log.error(f"Branch failed: {e}")
            self.log.error(traceback.format_exc())
            self._refresh_subtitle()
            return

        # The same re-render seam every other tree operation uses (§3.4).
        self.messages = new_messages
        await self._reload_transcript()
        self._refresh_subtitle()
        copied = f"{plan.mints} copied" if plan.mints else "nothing copied"
        folded = f", {plan.hidden} entries folded away" if plan.elide_from else ""
        self.notify(f"Branched from {len(ids)} marked messages — {copied}{folded}.")

    async def _paste_flow(
        self,
        session: ConversationSession,
        source_id: str,
        target_id: str,
    ) -> bool:
        """Re-create the copied subtree under ``target_id`` (§7).

        Returns whether the browser should re-open. A paste changes the tree and not
        the context, so there is nothing to re-render and no cursor to move: the
        reader goes back to the rows to see where the copy landed and to decide
        whether to navigate onto it.

        A failure here is a stale-tree case — the modal checked the same rules
        against the tree it opened with — so it is reported and the browser closes,
        which is what :meth:`_elide_span_flow` does with the same class of failure.
        """
        paste_subtree = getattr(self.current_backend, "paste_subtree", None)
        if paste_subtree is None:
            self.notify("This backend does not support pasting", severity="warning")
            return False

        try:
            plan = plan_paste(
                ConversationTree(session.entries(), session.cursor), source_id, target_id
            )
            minted = paste_subtree(session, source_id, target_id)
        except Exception as e:
            self.notify(f"Paste failed: {e}", severity="error")
            self.log.error(f"Paste failed: {e}")
            self.log.error(traceback.format_exc())
            return False

        noun = "entry" if len(minted) == 1 else "entries"
        left_out = f" ({len(plan.skipped)} structural entries left out)" if plan.skipped else ""
        self.notify(f"Pasted {len(minted)} {noun} under {target_id}{left_out}.")
        return True

    async def _elide_span_flow(
        self,
        session: ConversationSession,
        anchor_id: str,
        first_kept_id: str,
    ) -> None:
        """Perform the elide the browser worked out, and re-render (W3).

        ``anchor_id`` is where the fold jumps FROM (the elide entry is appended
        under it, and the conversation continues there); ``first_kept_id`` is
        where it jumps TO — the oldest entry the fold keeps. Everything on the
        anchor's path before it leaves the context.

        **Both ids arrive together now.** This used to take the anchor plus the
        whole ``ConversationTree`` and open a SECOND full-screen browser to ask
        for the resume point, with a different caption. That is gone: the pair is
        two nodes of one line and the browser can name both of them without
        closing (``ctrl+E``), which also means an illegal pair is refused while
        the reader can still see the tree they picked it from. The rejected
        design and why it looked reasonable are in PLAN-0.9.4 §4.

        Still split out rather than inlined, and still validating on the backend
        side as well: this is called from :meth:`action_browse_tree`'s worker with
        a pair the modal checked against a tree it built at open time, and the
        session is live. ``elide_span`` checks before it writes, so a pair that
        went stale is refused rather than half-applied, and the failure is
        surfaced through ``notify(severity="error")`` like every other mode's.
        """
        elide_span = getattr(self.current_backend, "elide_span", None)
        if elide_span is None:
            self.notify("This backend does not support eliding", severity="warning")
            return

        before = len(self.messages)
        self.sub_title = "Eliding span…"
        try:
            new_messages = elide_span(session, anchor_id, first_kept_id)
        except Exception as e:
            self.notify(f"Elide failed: {e}", severity="error")
            self.log.error(f"Elide failed: {e}")
            self.log.error(traceback.format_exc())
            self._refresh_subtitle()
            return

        self.messages = new_messages
        await self._reload_transcript()
        self._refresh_subtitle()
        self.notify(f"Elided {before} → {len(new_messages)} messages")

    def _extension_shortcuts(self) -> list[tuple[str, str, str, str]]:
        """The live backend's registered key shortcuts (E10 §6 / S69).

        Returns ``(key, command, args, description)`` per shortcut, or ``[]`` when the
        backend is a non-``TauBackend`` test double / not yet built (getattr-guarded,
        matching the other extension-surface reads like :meth:`get_system_commands`).
        """
        getter = getattr(self.current_backend, "get_extension_shortcuts", None)
        if getter is None:
            return []
        result: list[tuple[str, str, str, str]] = getter()
        return result

    async def action_extension_chord(self) -> None:
        """The ``ctrl+e`` extension chord leader (E10 §6 / S69).

        When one or more extensions registered a shortcut, ``ctrl+e`` opens the
        :class:`ExtensionChordScreen` which-key menu; the picked key's command is
        dispatched through :meth:`_dispatch_extension_command` (the SAME path the
        palette and typed ``/name`` use), so a keyboard shortcut is a pure accelerator
        over an already-runnable command — nothing model-visible, no new headless
        surface (the command it fires stays reachable by name and via ``ctrl+p``).

        When NO extension registered a shortcut, ``ctrl+e`` keeps its legacy meaning
        and edits the system prompt — zero regression for the common case where the
        chord namespace is empty.

        Kept non-priority (matching the binding it replaced), so while the message
        input is focused ``ctrl+e`` still moves to line-end (the ``TextArea`` default);
        the chord fires when focus is off the input, and the palette (S69 listing) is
        the always-reachable dispatch path. The menu is pushed with a result CALLBACK
        (not ``push_screen_wait``, which requires a worker the binding action is not),
        and the async command dispatch is scheduled from that callback via
        ``run_worker``.
        """
        shortcuts = self._extension_shortcuts()
        if not shortcuts:
            await self.action_edit_system_prompt()
            return

        def _dispatch_chosen(chosen: Optional[tuple[str, str]]) -> None:
            if chosen is None:
                return
            command, args = chosen
            self.run_worker(self._dispatch_extension_command(command, args))

        await self.push_screen(modals.ExtensionChordScreen(shortcuts), _dispatch_chosen)

    async def action_edit_system_prompt(self):
        """Edit the system prompt.

        The editor opens on τ's own base text when the config names no prompt,
        not on a stand-in: this key REPLACES that text, so seeding the editor
        with anything else would offer the user a starting point their agent has
        never actually been given. What the editor shows is the base text alone —
        the project context files and the tool list compose around it and are not
        the user's to edit here.
        """
        current_prompt = self.config.get("system_prompt") or BASE_SYSTEM_PROMPT

        def handle_result(new_prompt: str | None):
            if new_prompt is not None:
                update_config("system_prompt", new_prompt)
                self.config["system_prompt"] = new_prompt
                self.notify("System prompt updated")

        await self.push_screen(modals.SystemPromptEditor(current_prompt), handle_result)

    async def on_chat_selected(self, message: ChatSelected):
        """Load a session the sidebar, the picker, or ``/resume <ref>`` named.

        The one loader, three posters (§6/§7). ``chat_ref`` is resolved with
        ``resolve_ref`` rather than ``load``: the sidebar and the picker post a
        catalog ref, but ``/resume`` posts whatever the human typed, and
        ``resolve_ref`` is the grammar ``--session REF`` already uses — a
        directly-addressable ref, an exact session id, or a unique id prefix. It
        is also what ``AgentSessionRuntime.switch_session`` uses three lines
        below, so the two resolutions in this method can no longer disagree
        about what a ref is.
        """
        try:
            session = self.session_catalog.resolve_ref(message.chat_ref, cwd=os.getcwd())

            # Get model config and create backend
            model_config = self.config["models"].get(session.model)
            if not model_config:
                self.notify(f"Model {session.model} not found in config", severity="error")
                return

            self.current_backend = create_backend(self._apply_run_config(model_config))
            self._session_runtime = self._build_session_runtime(
                self.current_backend, session.model, model_config["backend"]
            )
            if self._session_runtime is not None:
                result = await self._session_runtime.switch_session(message.chat_ref)
                if result.get("blocked"):
                    self.notify(result["reason"], severity="warning")
                    return
                if result["cancelled"]:
                    self.notify("Switch cancelled by an extension", severity="warning")
                    return
                session = result["session"]
                self.current_session = session
            else:
                self.current_session = session
                self._bind_backend_session()
            await self._load_backend_extensions()
            self.messages = list(session.context)

            await self._reload_transcript(open_ask=True)

            # Update UI — model + the reloaded conversation's rollup.
            self._refresh_subtitle()
            self.notify(f"Loaded session: {session.display_title()}")

        except Exception as e:
            self.notify(f"Error loading session: {str(e)}", severity="error")
            self.log.error(f"Failed to load session: {e}", exc_info=True)


if __name__ == "__main__":
    app = TauApp()
    app.run()
