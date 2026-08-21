"""τ-agent-core extension_types: Extension API surface for extensions.

Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

Components:
- ExtensionAPI: Public API exposed to extension modules
- ExtensionContext: Context passed to extension event handlers
- ExtensionUI: User interaction methods (TUI only, no-op in headless)

Constraint: Extensions must not import τ-agent-core internals.
The ui property is a no-op in headless mode (RPC, SDK).
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal
from uuid import uuid4

from tau_agent_core.compaction import estimate_context_tokens
from tau_agent_core.tools.base import ExtensionToolDefinition
from tau_agent_core.submission import (
    MultitaskStrategy,
    Submission,
    SubmissionResult,
    user_input_permitted,
)

if TYPE_CHECKING:
    from tau_agent_core.events import EventBus
    from tau_agent_core.extensions.registry import ExtensionRegistry
    from tau_agent_core.extensions.runner import ExtensionHandlers

#: Hook names that existed through E2 but were removed. ``api.on`` rejects them
#: with a Fail-Early raise (E5 §3.2 / S30) rather than binding them silently to the
#: notify ``EventBus`` (a dead no-op, since nothing emits these channels).
_RETIRED_HOOKS: frozenset[str] = frozenset({"context"})

#: The ``Submission.submitter`` a turn originated through the SHARED
#: :class:`ExtensionContext` (``ctx.prompt``) is stamped with.
#:
#: A session has exactly ONE ``ExtensionContext`` — every loaded extension's api is
#: handed the same object (``AgentSession._bind_extension_api``), because the abort
#: signal / UI delegate / record sink are bound onto it once for all of them. So the
#: context carries no per-extension identity, and ``ctx.prompt`` genuinely cannot say
#: WHICH extension submitted. Naming the last-bound extension would be worse than
#: saying nothing (a confident wrong attribution), and naming a human would be the
#: lie docs/SUBMISSION-LIFECYCLE.md phase 5 exists to stop, so it reports the source
#: honestly (``"extension"``) and the submitter as this unmistakable sentinel —
#: Python's own ``<module>``/``<lambda>`` convention: brackets a real extension stem
#: can never produce. :meth:`ExtensionAPI.submit`, which IS per-extension (bucket-
#: bound), reports the real name; that is the attributed door and what new code uses.
UNATTRIBUTED_EXTENSION = "<unattributed extension>"

#: Prefix reserving the custom inter-extension pub/sub channels (E7 §3 / S52) away
#: from the closed ``AgentEvent`` type set the same notify ``EventBus`` also carries.
#: A custom channel is always ``ext:<name>:<topic>`` — see :func:`ext_channel`.
EXT_CHANNEL_PREFIX = "ext:"


def ext_channel(name: str, topic: str) -> str:
    """The namespaced ``EventBus`` channel for an extension pub/sub topic (E7 §3 / S52).

    Returns ``ext:<name>:<topic>``. The ``ext:`` prefix keeps custom
    inter-extension channels disjoint from the closed ``AgentEvent`` type set the
    notify bus also carries, and ``<name>`` (the emitting extension's file stem —
    the same stem that keys ``api.config``) makes the channel's origin unforgeable:
    :meth:`ExtensionAPI.emit` derives ``name`` from the caller's own bucket, so an
    extension can only publish under its own namespace. A subscriber passes the full
    result string to ``api.on(...)`` to receive it.
    """
    return f"{EXT_CHANNEL_PREFIX}{name}:{topic}"


#: Headless dialog policy (E7 §3 / S48 — anchor G9, decision D-E6-2).
#:
#: Maps each BLOCKING dialog method (``confirm``/``select``/``input``) to the set
#: of answer tokens that EXPLICITLY restore an auto-answer when it fires headless
#: (no TUI delegate). With no policy entry for a method the dialog RAISES
#: (:class:`HeadlessDialogError`) instead of silently auto-resolving — the old
#: silent ``confirm→True`` / ``select→first`` was a fallback that could
#: auto-approve a permission gate, exactly the anti-pattern the standing rule
#: forbids. A user opts back into the auto-answer per method via
#: ``--ui-defaults confirm=yes,select=first`` or a config.json ``"ui_defaults"``
#: block. Tokens are lower-cased before matching.
HEADLESS_DIALOG_ANSWERS: dict[str, frozenset[str]] = {
    "confirm": frozenset({"yes", "no", "true", "false"}),
    "select": frozenset({"first"}),
    "input": frozenset({"default"}),
    # A declarative ``ui.form`` (E10 §6 / S66). The only explicit headless answer is
    # ``defaults`` — return each field's declared default (or the kind's natural
    # empty value). With no ``form`` policy the form RAISES like every other dialog;
    # it NEVER silently auto-fills a form the user did not fill.
    "form": frozenset({"defaults"}),
}

#: Confirm tokens that resolve to ``True`` (the rest of ``confirm`` → ``False``).
_CONFIRM_TRUE_TOKENS: frozenset[str] = frozenset({"yes", "true"})

#: The field kinds a ``ui.form`` spec may declare (E10 §6 / S66 — D-E6-4: a
#: DECLARATIVE spec, not a widget factory). Each frontend renders these its own way
#: (the TUI as one generic ``ExtensionFormScreen``); headless degrades to a JSON
#: record + the ``form=defaults`` policy.
FORM_FIELD_KINDS: frozenset[str] = frozenset({"text", "select", "multiselect", "confirm", "number"})

#: The natural EMPTY value per field kind, used for the headless ``form=defaults``
#: answer when a field declares no ``default`` (``select`` has no empty value — it
#: falls back to its first option, which is always concrete).
_FORM_EMPTY_VALUE: dict[str, Any] = {
    "text": "",
    "number": 0,
    "confirm": False,
    "multiselect": [],
}


def validate_form_spec(spec: Any) -> tuple[str, list[dict[str, Any]]]:
    """Validate + normalize a ``ui.form`` spec into ``(title, fields)`` (S66).

    The single source of truth for the declarative form contract, shared by
    :meth:`ExtensionUI.form` (headless path + early-fail) and the TUI's
    ``ExtensionFormScreen`` (which re-validates the same raw spec), so the two can
    never disagree about what a field means.

    ``spec`` is a plain dict ``{title?: str, fields: [field, ...]}``; each field is
    ``{name: str, kind: str, label?: str, default?: Any, options?: [str, ...]}``.
    A ``select``/``multiselect`` field MUST carry a non-empty ``options`` list of
    strings. Returns the resolved title (defaults to ``"Form"``) and the normalized
    field list (``label`` defaulted to ``name``; ``default``/``options`` preserved
    when present).

    Fail-Early: a non-dict spec, an empty/absent ``fields`` list, a field missing a
    non-empty string ``name``, a duplicate name, an unknown ``kind``, or a
    select/multiselect without a valid ``options`` list RAISES :class:`ValueError`
    rather than silently dropping the field.
    """
    if not isinstance(spec, dict):
        raise ValueError("ui.form: spec must be a dict")
    title = spec.get("title", "Form")
    if not isinstance(title, str):
        raise ValueError("ui.form: spec['title'] must be a string")
    raw_fields = spec.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError("ui.form: spec['fields'] must be a non-empty list")

    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_fields:
        if not isinstance(raw, dict):
            raise ValueError("ui.form: each field must be a dict")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("ui.form: each field needs a non-empty string 'name'")
        if name in seen:
            raise ValueError(f"ui.form: duplicate field name {name!r}")
        seen.add(name)
        kind = raw.get("kind")
        if kind not in FORM_FIELD_KINDS:
            raise ValueError(
                f"ui.form: field {name!r} has unknown kind {kind!r} "
                f"(expected one of {sorted(FORM_FIELD_KINDS)})"
            )
        label = raw.get("label", name)
        if not isinstance(label, str):
            raise ValueError(f"ui.form: field {name!r} label must be a string")
        field: dict[str, Any] = {"name": name, "kind": kind, "label": label}
        if "default" in raw:
            field["default"] = raw["default"]
        if kind in ("select", "multiselect"):
            options = raw.get("options")
            if not isinstance(options, list) or not options:
                raise ValueError(f"ui.form: {kind} field {name!r} needs a non-empty 'options' list")
            if not all(isinstance(o, str) for o in options):
                raise ValueError(f"ui.form: field {name!r} 'options' must all be strings")
            field["options"] = list(options)
        fields.append(field)
    return title, fields


def form_headless_value(field: dict[str, Any]) -> Any:
    """The ``form=defaults`` headless answer for one validated field (S66).

    Returns the field's declared ``default`` when present (the extension author's
    explicit value, trusted like ``input``'s default); otherwise the kind's natural
    empty value (``select`` → its first option, which is always concrete). This is
    only reached once the user opts in via ``--ui-defaults form=defaults`` — with no
    policy the form raises instead of fabricating an answer.
    """
    if "default" in field:
        return field["default"]
    kind = field["kind"]
    if kind == "select":
        return field["options"][0]
    return _FORM_EMPTY_VALUE[kind]


#: The body kinds a ``ui.panel`` spec may declare — EXACTLY ONE per panel (E10 §6 /
#: S68 — D-E6-4: a DECLARATIVE spec, not a widget factory). ``text`` is a paragraph,
#: ``list`` a bullet list, ``table`` a columns/rows grid. Each frontend renders these
#: its own way (the TUI as an ``ExtensionPanel`` widget); headless degrades to a JSON
#: record carrying the SAME normalized body. Ordered so :func:`validate_panel_spec`
#: reports the allowed set deterministically.
PANEL_BODY_KINDS: tuple[str, ...] = ("table", "list", "text")


def _validate_panel_body(kind: str, raw: Any) -> dict[str, Any]:
    """Validate + normalize one ``ui.panel`` body of a given kind (S68).

    Returns a ``{"kind": …, …}`` body dict: ``text`` → ``{"kind":"text","text":str}``;
    ``list`` → ``{"kind":"list","items":[str]}``; ``table`` →
    ``{"kind":"table","columns":[str],"rows":[[str, …]]}``. Cells and list items MUST
    already be strings — the extension author formats them (``"$1.42"``, ``"3"``), so
    the panel never guesses a display form (Fail-Early, same discipline as a form's
    ``options``).
    """
    if kind == "text":
        if not isinstance(raw, str):
            raise ValueError("ui.panel: 'text' body must be a string")
        return {"kind": "text", "text": raw}
    if kind == "list":
        if not isinstance(raw, list) or not all(isinstance(i, str) for i in raw):
            raise ValueError("ui.panel: 'list' body must be a list of strings")
        return {"kind": "list", "items": list(raw)}
    # table
    if not isinstance(raw, dict):
        raise ValueError("ui.panel: 'table' body must be a dict {columns, rows}")
    columns = raw.get("columns")
    if not isinstance(columns, list) or not columns or not all(isinstance(c, str) for c in columns):
        raise ValueError("ui.panel: table 'columns' must be a non-empty list of strings")
    rows_raw = raw.get("rows", [])
    if not isinstance(rows_raw, list):
        raise ValueError("ui.panel: table 'rows' must be a list")
    rows: list[list[str]] = []
    for row in rows_raw:
        if not isinstance(row, list) or not all(isinstance(c, str) for c in row):
            raise ValueError("ui.panel: each table row must be a list of strings")
        if len(row) != len(columns):
            raise ValueError(
                f"ui.panel: table row has {len(row)} cells but there are {len(columns)} columns"
            )
        rows.append(list(row))
    return {"kind": "table", "columns": list(columns), "rows": rows}


def _validate_panel_actions(raw: Any) -> list[dict[str, str]]:
    """Validate + normalize the optional ``ui.panel`` action list (S68).

    Each action is ``{label: str, command: str, args?: str}``: pressing it in the TUI
    dispatches ``command`` (a name an extension registered via ``register_command``)
    with ``args`` (default ``""``) — i.e. actions dispatch back into the extension as
    COMMAND CALLS, closing the loop from a live panel to extension logic. Returns
    ``[]`` when ``actions`` is absent. Fail-Early: a non-list, a non-dict action, or a
    missing/empty ``label``/``command`` RAISES rather than dropping a dead button.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("ui.panel: 'actions' must be a list")
    actions: list[dict[str, str]] = []
    for action in raw:
        if not isinstance(action, dict):
            raise ValueError("ui.panel: each action must be a dict")
        label = action.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError("ui.panel: each action needs a non-empty string 'label'")
        command = action.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError(f"ui.panel: action {label!r} needs a non-empty string 'command'")
        args = action.get("args", "")
        if not isinstance(args, str):
            raise ValueError(f"ui.panel: action {label!r} 'args' must be a string")
        actions.append({"label": label, "command": command, "args": args})
    return actions


def validate_panel_spec(spec: Any) -> dict[str, Any]:
    """Validate + normalize a ``ui.panel`` spec into ``{title, body, actions}`` (S68).

    The single source of truth for the declarative panel contract, shared by
    :meth:`ExtensionUI.panel` (headless record + early-fail) and the TUI's
    ``ExtensionPanel`` widget (which re-validates the same raw spec), so the two can
    never disagree about a panel's shape.

    ``spec`` is a plain dict ``{title?: str, <body>, actions?: [...]}`` where ``<body>``
    is EXACTLY ONE of ``table`` / ``list`` / ``text`` (see :func:`_validate_panel_body`)
    and ``actions`` is the optional command-dispatch list (see
    :func:`_validate_panel_actions`). Returns the normalized
    ``{"title", "body", "actions"}`` dict (title defaults to ``"Panel"``).

    Fail-Early: a non-dict spec, a non-string title, ZERO or MORE-THAN-ONE body key,
    or any malformed body/action RAISES :class:`ValueError` rather than rendering a
    half-formed panel.
    """
    if not isinstance(spec, dict):
        raise ValueError("ui.panel: spec must be a dict")
    title = spec.get("title", "Panel")
    if not isinstance(title, str):
        raise ValueError("ui.panel: spec['title'] must be a string")
    present = [k for k in PANEL_BODY_KINDS if k in spec]
    if len(present) != 1:
        raise ValueError(
            "ui.panel: spec must carry EXACTLY ONE body of "
            f"{list(PANEL_BODY_KINDS)} (got {present or 'none'})"
        )
    body = _validate_panel_body(present[0], spec[present[0]])
    actions = _validate_panel_actions(spec.get("actions"))
    return {"title": title, "body": body, "actions": actions}


class HeadlessDialogError(RuntimeError):
    """A UI dialog was opened with no human reachable and no explicit ``--ui-defaults`` policy.

    Raised by :meth:`ExtensionUI.confirm` / :meth:`ExtensionUI.select` /
    :meth:`ExtensionUI.input` / :meth:`ExtensionUI.form` when the corresponding
    method has no headless-answer policy (E7 §3 / S48) and no human can be asked.
    "No human can be asked" has TWO causes, and this one exception covers both
    because the consequence is identical:

    - **headless mode** — there is no TUI delegate at all;
    - **``allow_user_input=False``** — a delegate may well exist, but the
      submission driving this code declared that code running under it may not
      prompt a human (Jupyter's ``allow_stdin``;
      docs/SUBMISSION-LIFECYCLE.md "The dataclasses", which names this class as
      the enforcement: *"Enforcement stays HeadlessDialogError"*). A cron- or
      bus-originated turn in a TUI process is exactly this case.

    Either way, silently auto-answering would fabricate consent for whatever the
    dialog was gating — so Fail-Early: raise, naming the opt-in that restores an
    explicit auto-answer.
    """


class ExtensionUI:
    """User interaction methods (TUI delegate, or a headless policy).

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface"; E7 §3 / S48.

    In TUI mode the blocking dialogs (``confirm``/``select``/``input``) delegate
    to a TUI delegate that asks a real human. In headless mode there is no human,
    so each blocking dialog obeys the headless-answer POLICY set via
    :meth:`set_headless_defaults` (from ``--ui-defaults`` / config.json):

    - a method WITH a policy entry returns the explicitly-configured answer
      (``confirm`` → ``True``/``False``; ``select`` → first item; ``input`` →
      default);
    - a method WITHOUT one RAISES :class:`HeadlessDialogError` (S48 / D-E6-2).

    The pre-S48 behaviour auto-answered every headless dialog (``confirm→True``,
    ``select→first``, ``input→default``) with no way to opt out — a silent
    auto-approve of whatever the dialog was gating. Raising by default makes the
    auto-answer an EXPLICIT choice instead of a hidden fallback.

    **TUI mode is not enough on its own** (docs/SUBMISSION-LIFECYCLE.md,
    ``Submission.allow_user_input`` — Jupyter's ``allow_stdin``). A blocking
    dialog reaches the delegate only if the submission driving the calling code
    permits it: :func:`~tau_agent_core.submission.user_input_permitted` is
    ``False`` for the whole of a turn admitted with ``allow_user_input=False``,
    and each blocking dialog then takes the headless-answer route above even
    though a delegate and a live human exist. That is what makes the capability
    per-SUBMISSION rather than per-process: one embedded τ can serve an
    interactive session and a cron-triggered submission at the same time, and
    only the latter is barred from opening dialogs. Outside any submission-driven
    turn (a slash-command handler, ``session_start``, ``continue_conversation()``)
    nothing is published and behaviour is exactly as before.

    ``notify`` is non-blocking (no answer to fabricate): it prints to stderr
    headless and paints on the delegate in TUI mode — unchanged, and NOT gated by
    ``allow_user_input``, which is about asking a human, not telling one.

    Attributes:
        _mode: "tui" or "headless"
        _tui_delegate: TUI delegate object (set via set_ui_delegate())
        _headless_policy: validated ``{method: token}`` headless-answer map
    """

    def __init__(
        self,
        mode: Literal["tui", "headless"] = "headless",
        headless_policy: dict[str, str] | None = None,
    ) -> None:
        """Initialize ExtensionUI.

        Args:
            mode: Either 'tui' or 'headless'. Defaults to 'headless'.
            headless_policy: Optional ``{method: token}`` headless-answer map
                (validated via :meth:`set_headless_defaults`). Defaults to no
                policy → headless dialogs raise (S48).
        """
        self._mode: Literal["tui", "headless"] = mode
        self._tui_delegate: Any | None = None
        self._headless_policy: dict[str, str] = {}
        # Headless JSON record sink (E7 §3 / S49 — anchor G10). When set (only the
        # ``--mode json`` headless path does so), ``notify`` emits a structured
        # ``{"type": "extension", …}`` record through it INSTEAD of the bare stderr
        # line, so a parent reading a child ``tau -p --mode json`` stream can see the
        # child's extension activity (the isolated-agent atom stays orchestratable).
        # ``None`` → the pre-S49 stderr behaviour is unchanged (text mode, SDK).
        self._record_sink: Callable[[dict[str, Any]], None] | None = None
        if headless_policy:
            self.set_headless_defaults(headless_policy)

    def set_headless_defaults(self, policy: dict[str, str]) -> None:
        """Set (replace) the headless-answer policy, validating every entry (S48).

        ``policy`` maps a dialog method to its answer token; keys must be in
        :data:`HEADLESS_DIALOG_ANSWERS` and each token must be one of that method's
        allowed answers (case-insensitive). Fail-Early: an unknown method or token
        RAISES :class:`ValueError` rather than being silently ignored, so a
        typo in ``--ui-defaults`` / config surfaces instead of leaving a dialog
        unexpectedly raising at runtime.
        """
        validated: dict[str, str] = {}
        for method, token in policy.items():
            allowed = HEADLESS_DIALOG_ANSWERS.get(method)
            if allowed is None:
                raise ValueError(
                    f"ui-defaults: unknown dialog {method!r} "
                    f"(expected one of {sorted(HEADLESS_DIALOG_ANSWERS)})"
                )
            token_l = str(token).strip().lower()
            if token_l not in allowed:
                raise ValueError(
                    f"ui-defaults: {method}={token!r} is not a valid answer "
                    f"(expected one of {sorted(allowed)})"
                )
            validated[method] = token_l
        self._headless_policy = validated

    def _human_delegate(self) -> Any | None:
        """The TUI delegate a blocking dialog may reach right now, or ``None``.

        Two independent conditions, both required — the process must HAVE a human
        attached (TUI mode with a bound delegate), and the submission driving the
        calling code must PERMIT asking one
        (:func:`~tau_agent_core.submission.user_input_permitted`, published by
        ``AgentSession.submit()`` from ``Submission.allow_user_input``). When
        either fails there is no human to ask and the caller falls through to the
        headless-answer policy — an explicit ``--ui-defaults`` token, or
        :class:`HeadlessDialogError`.
        """
        if self._mode != "tui" or self._tui_delegate is None:
            return None
        if not user_input_permitted():
            return None
        return self._tui_delegate

    @property
    def interactive(self) -> bool:
        """Whether a human is watching a live surface right now.

        ``True`` only in TUI mode with a bound delegate — the one case that can
        paint something (``set_status``/``panel``/``notify``) without producing a
        stderr line or a JSON record instead. Extension code with a
        high-frequency ambient update (an ASR partial, a tick) checks this
        BEFORE formatting or calling ``set_status``/``panel``, so a headless
        run — including ``--mode json``, whose record schema has no room for
        arbitrary per-partial noise — pays nothing for updates nobody can see.

        Unlike :meth:`_human_delegate` this does not consult
        ``user_input_permitted()``: that gate is permission to ask a human a
        blocking question mid-submission, not whether a screen exists to paint
        ambient state on.
        """
        return self._mode == "tui" and self._tui_delegate is not None

    def _headless_token(self, method: str, detail: str) -> str:
        """The configured headless answer token for ``method``, or raise (S48).

        Fail-Early: with no policy entry there is no human to ask and no explicit
        auto-answer, so raise :class:`HeadlessDialogError` naming the opt-in. The
        message names WHICH of the two reasons applies (see :meth:`_human_delegate`)
        — telling a TUI user to "run in the TUI" because a cron submission barred
        the dialog would send them hunting the wrong thing entirely.
        """
        token = self._headless_policy.get(method)
        if token is None:
            if not user_input_permitted():
                raise HeadlessDialogError(
                    f"ui.{method}({detail!r}) was called under a submission with "
                    f"allow_user_input=False, and there is no --ui-defaults policy for "
                    f"{method!r} to answer it without a human. This is Jupyter's "
                    "allow_stdin (docs/SUBMISSION-LIFECYCLE.md): the capability is "
                    "declared PER SUBMISSION, so a bus-, timer- or extension-originated "
                    "turn cannot open a dialog even in a TUI process with a live human "
                    f"at it. Either pass --ui-defaults {method}=<answer> (allowed: "
                    f"{sorted(HEADLESS_DIALOG_ANSWERS[method])}) / set config.json "
                    '"ui_defaults", or have the submitter set allow_user_input=True.'
                )
            raise HeadlessDialogError(
                f"ui.{method}({detail!r}) was called in headless mode with no "
                f"--ui-defaults policy for {method!r}. A headless run cannot ask a "
                "human, and auto-answering would silently resolve the dialog. Pass "
                f"--ui-defaults {method}=<answer> (allowed: "
                f"{sorted(HEADLESS_DIALOG_ANSWERS[method])}) or set config.json "
                '"ui_defaults", or run in the TUI.'
            )
        return token

    async def confirm(self, title: str, message: str) -> bool:
        """Show a confirmation dialog. Returns user's choice.

        Delegates to the TUI delegate when a human is reachable
        (:meth:`_human_delegate` — TUI mode AND the driving submission's
        ``allow_user_input``). Otherwise returns the policy answer
        (``confirm=yes/true`` → ``True``, ``confirm=no/false`` → ``False``) or
        raises :class:`HeadlessDialogError` when no policy is set.
        """
        delegate = self._human_delegate()
        if delegate is not None:
            confirmed: bool = await delegate.confirm(title, message)
            return confirmed
        return self._headless_token("confirm", title) in _CONFIRM_TRUE_TOKENS

    async def select(self, title: str, items: list[str]) -> str | None:
        """Show a selection dialog. Returns selected item or None.

        Delegates to the TUI delegate when a human is reachable
        (:meth:`_human_delegate`). Otherwise ``select=first`` returns the first
        item (or None if empty); no policy raises :class:`HeadlessDialogError`.
        """
        delegate = self._human_delegate()
        if delegate is not None:
            selected: str | None = await delegate.select(title, items)
            return selected
        self._headless_token("select", title)  # raises if no policy; only "first" is valid
        return items[0] if items else None

    async def input(self, title: str, default: str = "") -> str:
        """Show an input dialog. Returns user input or default.

        Delegates to the TUI delegate when a human is reachable
        (:meth:`_human_delegate`). Otherwise ``input=default`` returns the default
        value; no policy raises :class:`HeadlessDialogError`.
        """
        delegate = self._human_delegate()
        if delegate is not None:
            entered: str = await delegate.input(title, default)
            return entered
        self._headless_token("input", title)  # raises if no policy; only "default" is valid
        return default

    async def form(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Show a DECLARATIVE form and return ``{field_name: value}`` (E10 §6 / S66).

        The τ answer to pi's ``question``/``questionnaire`` — but a plain-data SPEC,
        not a widget factory (D-E6-4): ``spec = {title?, fields: [{name, kind,
        label?, default?, options?}, ...]}`` with ``kind`` one of
        :data:`FORM_FIELD_KINDS` (``text``/``select``/``multiselect``/``confirm``/
        ``number``). The spec is validated by :func:`validate_form_spec` up front so
        a malformed form fails BEFORE any UI is shown, in every mode.

        Routing (mirrors the other blocking dialogs, S48):

        - **TUI mode** with a delegate, and a driving submission that permits
          asking a human (:meth:`_human_delegate`) → delegates to the frontend's
          single generic ``ExtensionFormScreen``; a real human fills it. Returns
          the ``{name: value}`` dict on submit, or ``None`` on cancel/Esc (a
          cancelled form is NOT a fabricated set of answers — Fail-Early, same as
          :meth:`select`). A form is a blocking dialog like any other, so
          ``allow_user_input=False`` routes it down the policy path below rather
          than putting a screen in front of a human who did not originate the
          turn.
        - **headless ``--mode json``** (a record sink is installed) → first emits one
          ``{"type": "extension", "kind": "form", …}`` record describing the request
          (visibility on the stream, like :meth:`notify`), THEN resolves via policy.
        - **headless policy** → with ``--ui-defaults form=defaults`` returns each
          field's declared default (:func:`form_headless_value`); with NO ``form``
          policy RAISES :class:`HeadlessDialogError`. It NEVER silently auto-fills a
          form the user did not fill.

        Returns:
            ``dict[str, Any]`` mapping each field name to its answer, or ``None``
            when a TUI user cancels. The headless ``defaults`` answer is always a
            dict (the user opted in — there is nothing to cancel).
        """
        title, fields = validate_form_spec(spec)
        delegate = self._human_delegate()
        if delegate is not None:
            answers: dict[str, Any] | None = await delegate.form(spec)
            return answers
        if self._record_sink is not None:
            self._record_sink(
                {
                    "type": "extension",
                    "kind": "form",
                    "extension": None,
                    "title": title,
                    "fields": fields,
                }
            )
        self._headless_token("form", title)  # raises if no policy; only "defaults" is valid
        return {field["name"]: form_headless_value(field) for field in fields}

    def set_record_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        """Install (or clear) the headless JSON record sink (E7 §3 / S49 — G10).

        The frontends call this (via :meth:`AgentSession.set_extension_record_sink`)
        on the ``--mode json`` headless path with a writer that serializes each
        record to one stdout line — the parallel record family alongside the closed
        ``AgentEvent`` set (like the session header line). Passing ``None`` restores
        the plain stderr sink. Nothing calls this in the TUI or in ``--mode text``,
        so those paths keep the delegate / stderr behaviour.
        """
        self._record_sink = sink

    def notify(self, message: str, level: str = "info", *, source: str | None = None) -> None:
        """Show a notification.

        Routing (first match wins):

        - **TUI mode** with a delegate → paints on the delegate (the Textual toast).
        - **headless ``--mode json``** (a :meth:`set_record_sink` is installed) →
          emits one ``{"type": "extension", "kind": "notify", …}`` record through the
          sink instead of stderr, so extension activity is visible in the JSON event
          stream (S49 — anchor G10).
        - otherwise (headless ``--mode text`` / SDK) → prints to stderr, unchanged.

        ``source`` is the originating extension's identity when the caller knows it
        (the S44 error-surface path passes the failing extension's path). A plain
        ``api.ui.notify(...)`` cannot supply one: every bound extension shares the
        session's ONE :class:`ExtensionUI` (``api.ui`` is that single instance — a
        test-enforced invariant), so the shared sink has no per-call attribution.
        Fail-Early: the record then carries ``"extension": null`` — the honest
        "unattributed" value — rather than a fabricated name.
        """
        if self._mode == "tui" and self._tui_delegate:
            self._tui_delegate.notify(message, level)
            return
        if self._record_sink is not None:
            self._record_sink(
                {
                    "type": "extension",
                    "kind": "notify",
                    "extension": source,
                    "level": level,
                    "message": message,
                }
            )
            return
        import sys

        print(f"[τ] {level}: {message}", file=sys.stderr)

    def set_status(self, key: str, text: str | None, *, source: str | None = None) -> None:
        """Set (or clear) a keyed slot in the extension status strip (E10 §6 / S67).

        Ports pi's ``ctx.ui.setStatus(key, text)`` (types.ts:141): ambient, live
        state painted in a one-line footer strip. ``key`` identifies a SLOT —
        re-calling the same key UPDATES that slot in place (e.g. budget proximity
        ticking each turn), never appending a new one. ``text=None`` CLEARS the slot
        (pi's "pass undefined to clear"). Unlike :meth:`confirm`/:meth:`form` this is
        non-blocking display, so it needs no headless answer policy — it routes
        exactly like :meth:`notify`:

        - **TUI mode** with a delegate → paints on the delegate's status strip.
        - **headless ``--mode json``** (a :meth:`set_record_sink` is installed) →
          emits one ``{"type": "extension", "kind": "status", …}`` record through the
          sink so a parent reading a child ``tau -p --mode json`` stream sees the
          ambient state change (S49 — anchor G10). A cleared slot rides the same
          record with ``"text": null``.
        - otherwise (headless ``--mode text`` / SDK) → prints to stderr, unchanged
          from :meth:`notify`'s fallback (honest, never a silent no-op).

        ``source`` is the originating extension's identity when the caller knows it;
        a plain ``api.ui.set_status(...)`` cannot supply one (every bound extension
        shares the session's ONE :class:`ExtensionUI`), so the record then carries
        ``"extension": null`` rather than a fabricated name — same contract as
        :meth:`notify`.

        Raises:
            ValueError: if ``key`` is not a non-empty string (Fail-Early: a status
                slot with no key has nothing to update or clear).
        """
        if not isinstance(key, str) or not key:
            raise ValueError("ui.set_status: key must be a non-empty string")
        if self._mode == "tui" and self._tui_delegate:
            self._tui_delegate.set_status(key, text)
            return
        if self._record_sink is not None:
            self._record_sink(
                {
                    "type": "extension",
                    "kind": "status",
                    "extension": source,
                    "key": key,
                    "text": text,
                }
            )
            return
        import sys

        shown = "(cleared)" if text is None else text
        print(f"[τ] status {key}: {shown}", file=sys.stderr)

    def panel(self, key: str, spec: dict[str, Any] | None, *, source: str | None = None) -> None:
        """Show, update, or clear a persistent keyed PANEL (E10 §6 / S68).

        The fleet-dashboard primitive (D-E6-4: a plain-data SPEC, not a widget
        factory). ``key`` names a persistent panel surface; re-calling the same key
        UPDATES that panel in place (a live delegate table ticking as children start /
        finish / cost), and ``spec=None`` CLEARS it (the fleet is done). ``spec`` is
        ``{title?, <body>, actions?}`` where ``<body>`` is EXACTLY ONE of
        ``table`` / ``list`` / ``text`` and ``actions`` is a list of
        ``{label, command, args?}`` — pressing an action DISPATCHES ``command`` back
        into the extension as a ``register_command`` call (the panel→extension loop).
        The spec is validated by :func:`validate_panel_spec` up front so a malformed
        panel fails BEFORE any UI is shown, in every mode.

        Like :meth:`set_status` this is NON-BLOCKING display (a panel is not a dialog
        awaiting an answer), so it needs no headless answer policy — it routes exactly
        like :meth:`notify`:

        - **TUI mode** with a delegate → paints on the delegate's panel host, which
          mounts / updates / removes the keyed :class:`ExtensionPanel`.
        - **headless ``--mode json``** (a :meth:`set_record_sink` is installed) →
          emits one ``{"type": "extension", "kind": "panel", "key": …, "spec": …}``
          record through the sink (``spec`` is the normalized dict, or ``null`` on
          clear) so a parent reading a child ``tau -p --mode json`` stream sees the
          panel and its declared actions (anchor G10). This IS the non-interactive
          headless policy (§6.3): the surface is visible on the stream, its actions
          simply cannot be pressed without a TUI — a panel is never TUI-ONLY.
        - otherwise (headless ``--mode text`` / SDK) → prints to stderr, unchanged
          from :meth:`notify`'s fallback (honest, never a silent no-op).

        ``source`` is the originating extension's identity when the caller knows it;
        a plain ``api.ui.panel(...)`` cannot supply one (every bound extension shares
        the session's ONE :class:`ExtensionUI`), so the record then carries
        ``"extension": null`` rather than a fabricated name — same contract as
        :meth:`notify`/:meth:`set_status`.

        Raises:
            ValueError: if ``key`` is not a non-empty string (Fail-Early: a panel with
                no key has nothing to update or clear); or (via
                :func:`validate_panel_spec`) if ``spec`` is malformed.
        """
        if not isinstance(key, str) or not key:
            raise ValueError("ui.panel: key must be a non-empty string")
        normalized = None if spec is None else validate_panel_spec(spec)
        if self._mode == "tui" and self._tui_delegate:
            self._tui_delegate.panel(key, normalized)
            return
        if self._record_sink is not None:
            self._record_sink(
                {
                    "type": "extension",
                    "kind": "panel",
                    "extension": source,
                    "key": key,
                    "spec": normalized,
                }
            )
            return
        import sys

        shown = "(cleared)" if normalized is None else normalized["title"]
        print(f"[τ] panel {key}: {shown}", file=sys.stderr)

    def emit_veto(self, *, extension: str | None, tool: str, reason: str) -> None:
        """Emit a blocked-tool VETO record on the headless JSON stream (E7 §3 / S50).

        Routes ONLY to the record sink — the ``--mode json`` record family (S49) —
        emitting ``{"type": "extension", "kind": "veto", "extension": <path|null>,
        "tool": <name>, "reason": <reason>, "blocked": true}`` so a parent
        orchestrating a child ``tau -p --mode json`` can tell a `tool_call` veto
        (anchor G11) from a generic errored tool result. Deliberately does NOT touch
        the TUI delegate or stderr: in the TUI the veto is rendered off the
        ``tool_execution_end`` AgentEvent's ``blocked`` field, and in ``--mode text``
        it already surfaces as the persisted errored tool-result node — a stderr line
        here would be a duplicate. With no sink installed this is a no-op (not a
        fabricated channel — the JSON record family only exists on that one path).
        """
        if self._record_sink is not None:
            self._record_sink(
                {
                    "type": "extension",
                    "kind": "veto",
                    "extension": extension,
                    "tool": tool,
                    "reason": reason,
                    "blocked": True,
                }
            )

    def emit_constraints(self, summary: dict[str, Any], *, source: str | None = None) -> None:
        """Echo the decode constraint that shaped a ``ctx.complete()`` call (G4/C).

        Routes ONLY to the record sink — the ``--mode json`` record family (S49) —
        emitting ``{"type": "extension", "kind": "constraints", "extension":
        <path|null>, "constraints": <summary>}`` where ``summary`` is
        :meth:`DecodeConstraints.describe`'s output (``{"kind": "choices"|"json_schema"
        |"grammar", ...}``). This retires the "``describe()`` has zero non-test callers"
        debt: the ONE place a real constraint exists at completion time is
        ``ctx.complete()``, so that is the honest producer of this record.

        Guard: NEVER echo ``{"kind": "none"}`` — the caller only reaches here when
        ``constraints.has_constraint()`` is true, and this second check makes the
        "no fabricated placeholder" invariant local (Fail-Early: a ``none`` summary
        is dropped rather than emitted as a meaningless record). Like :meth:`emit_veto`
        this deliberately does NOT touch the TUI delegate or stderr, and with no sink
        installed it is a no-op (the JSON record family only exists on that one path).
        """
        if summary.get("kind") == "none":
            return
        if self._record_sink is not None:
            self._record_sink(
                {
                    "type": "extension",
                    "kind": "constraints",
                    "extension": source,
                    "constraints": summary,
                }
            )


@dataclass
class BranchResult:
    """What a C2/W14 branch sub-agent came back with (``ctx.spawn_branch``).

    ``ok`` is the field callers must actually read. A sub-agent that failed returns a
    ``BranchResult`` with ``ok=False`` rather than raising, because failure containment
    is the design (§9.2/5) — one bad evaluator in a fan-out must not kill the primary
    turn. The cost of that choice is that an unchecked ``ok`` turns a failure into an
    empty-but-successful-looking answer, so the field is first and the docstrings say so.

    ``leaf`` is the branch's final entry id — the handle the spawner's **fold step** uses
    to read the verdict back (or to ``ctx.summarize_branch(leaf)`` it) before making its
    one distilled append on the primary cursor.
    """

    ok: bool
    lane: str
    label: str
    leaf: str | None
    messages: list[dict[str, Any]]
    error: str | None


class ExtensionContext:
    """Context passed to extension event handlers and tools.

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

    Attributes:
        _cwd: Current working directory.
        _session_manager: SessionManager instance (or None).
        _signal: AbortSignal for this context (or None).
        _is_idle: Whether the agent is idle.
        _ui: ExtensionUI instance.
    """

    def __init__(
        self,
        cwd: str = ".",
        session_manager: Any | None = None,
        signal: Any | None = None,
        is_idle: bool = True,
    ) -> None:
        """Initialize ExtensionContext.

        Args:
            cwd: Current working directory. Defaults to ".".
            session_manager: SessionManager instance. Defaults to None.
            signal: AbortSignal for this context. Defaults to None.
            is_idle: Whether the agent is idle. Defaults to True.
        """
        self._cwd = cwd
        self._session_manager = session_manager
        self._signal = signal
        self._is_idle = is_idle
        self._ui = ExtensionUI(mode="headless")
        # P3 (docs/REMOTE-CONTROL.md §4[7]): "extensions can request
        # shutdown, checked after each command rather than polled" — the
        # ExtensionContext-level half of that (the checking half lives in
        # whatever process-lifecycle layer owns this context, e.g.
        # AgentSession.shutdown_requested / RPCHandler). Additive: `shutdown()`
        # already existed (below) as a thin `session_manager.shutdown()`
        # pass-through that nothing observed; this flag is what a caller with
        # no `session_manager` bound (RPC mode has none) can still see.
        self._shutdown_requested = False
        # The live AgentSession, bound by ExtensionAPI so get_context_usage() can
        # read real messages + model.context_window. None until bound.
        self._session: Any | None = None

    @property
    def cwd(self) -> str:
        """Current working directory."""
        return self._cwd

    @property
    def session_manager(self) -> Any:
        """The SessionManager instance."""
        return self._session_manager

    @property
    def signal(self) -> Any | None:
        """AbortSignal for this context."""
        return self._signal

    @property
    def is_idle(self) -> bool:
        """Whether the agent is idle."""
        return self._is_idle

    @property
    def ui(self) -> ExtensionUI:
        """UI methods (TUI-only, no-ops/headless-policy elsewhere) — E9 / S60.

        The SAME shared ``ExtensionUI`` instance :attr:`ExtensionAPI.ui` exposes
        (both read ``self._ui`` off this one ``ExtensionContext``), so a hook
        handler's ``ctx.ui.notify(...)`` / ``await ctx.ui.confirm(...)`` paints on
        the identical delegate an extension's top-level ``api.ui`` would. Every
        mutating-hook handler and every ``register_command`` handler is called as
        ``handler(event_or_args, ctx)`` with THIS ``ExtensionContext`` (never the
        ``ExtensionAPI``), so without this property a hook-scoped ``ctx.ui`` call
        (pi's own idiom — ``permission-gate.ts``, ``protected-paths.ts``,
        ``claude-rules.ts`` all call ``ctx.ui.*`` from inside a
        ``pi.on(...)``/command handler) had no surface to reach the delegate
        through; ``run_extension_command``'s own docstring already promised "the
        same ``ctx.ui`` every hook reaches" — this property makes that true rather
        than aspirational.
        """
        return self._ui

    def abort(self) -> None:
        """Abort the current operation by calling signal.abort() if available."""
        if self._signal:
            self._signal.abort()

    def shutdown(self) -> None:
        """Request a shutdown: marks `shutdown_requested` and, if a
        `session_manager` is bound, additionally calls its `shutdown()` too
        (the pre-existing pass-through — kept for whatever still relies on
        it). Idempotent; safe to call more than once."""
        self._shutdown_requested = True
        if self._session_manager is not None and hasattr(self._session_manager, "shutdown"):
            self._session_manager.shutdown()

    @property
    def shutdown_requested(self) -> bool:
        """True once `shutdown()` has been called on this context (P3)."""
        return self._shutdown_requested

    def get_context_usage(self) -> dict[str, Any] | None:
        """Return context usage for the active model (pi ``ContextUsage`` shape).

        Faithful port of pi's ``getContextUsage`` (agent-session.ts:2975 →
        ``ContextUsage`` at types.ts:281-287): returns
        ``{tokens, context_window, percent}`` where ``tokens`` is the estimated
        context-token count from ``estimate_context_tokens`` — the SAME estimate
        that drives auto-compaction (agent_session.py:523) — and ``percent`` is
        ``tokens / context_window * 100``.

        Returns ``None`` when the model has no positive ``context_window`` (pi
        returns ``undefined``); that is a genuine "unknown", not a fabricated
        zero.

        Raises:
            RuntimeError: if no session is bound — there is nothing to measure.
                Replaces the old fictional ``{"total_tokens": 0}`` stub
                (Fail-Early: raise rather than fabricate).
        """
        session = self._session
        if session is None:
            raise RuntimeError("get_context_usage: no session bound to ExtensionContext")
        model = getattr(session, "_model", None)
        context_window = int(getattr(model, "context_window", 0) or 0)
        if context_window <= 0:
            return None
        tokens = estimate_context_tokens(session.messages).tokens
        percent = (tokens / context_window) * 100
        return {"tokens": tokens, "context_window": context_window, "percent": percent}

    # ------------------------------------------------------------------
    # Model + usage access (E6 §2 / S45 — anchor G14)
    #
    # Public accessors so extensions stop reaching the private ``_session._model``
    # or hand-parsing ``event.message`` for usage. Each delegates to the bound
    # ``AgentSession`` (the authoritative holder of the live model + last usage) and
    # Fail-Early raises when no session is bound — there is nothing to read/switch.
    # ------------------------------------------------------------------

    def get_model(self) -> dict[str, Any]:
        """The active model as ``{id, provider, context_window}`` (S45).

        Delegates to :meth:`AgentSession.get_model`. Mirrors pi's ``ctx.model``
        (types.ts:311) but as the three-field projection an extension needs, so it
        never has to reach the private ``ctx._session._model``.

        Raises:
            RuntimeError: if no session is bound (Fail-Early — no model to read).
        """
        model: dict[str, Any] = self._require_session().get_model()
        return model

    def set_model(self, name: str) -> dict[str, Any]:
        """Switch the active model by NAME, effective next turn (S45).

        Delegates to :meth:`AgentSession.set_model` (pi ``setModel`` parity, adapted
        to τ's name-based resolver). Returns the new :meth:`get_model` projection.

        Raises:
            RuntimeError: if no session is bound, or the session has no model
                resolver bound (both Fail-Early — no registry to resolve ``name``).
            Whatever the resolver raises for an unknown ``name`` propagates unchanged.
        """
        model: dict[str, Any] = self._require_session().set_model(name)
        return model

    def get_usage(self) -> dict[str, Any] | None:
        """The most recent completion's token usage, or ``None`` (S45).

        The public per-completion usage accessor (anchor G14): delegates to
        :meth:`AgentSession.get_usage`. Returns a copy of the last completion's
        ``usage`` dict, or ``None`` when no completion has landed yet. Read this from
        a ``message_end`` handler instead of pulling ``event.message["usage"]``.

        Raises:
            RuntimeError: if no session is bound (Fail-Early — nothing to measure).
        """
        usage: dict[str, Any] | None = self._require_session().get_usage()
        return usage

    async def prompt(self, text: str) -> list[dict[str, Any]]:
        """DEPRECATED alias for :meth:`ExtensionAPI.submit` — use ``api.submit`` instead.

        Run one agent turn on the bound session, returning this turn's messages.
        Kept working so existing extensions keep working; it is now exactly
        ``submit(text, multitask_strategy="enqueue")`` with the result's
        ``messages`` returned in place of the :class:`SubmissionResult`, which
        is the whole reason to prefer ``api.submit``: a refusal is a typed
        result there, and unreachable through this signature.

        **What changed (docs/SUBMISSION-LIFECYCLE.md phase 5).** This used to
        delegate to :meth:`AgentSession.prompt`, the *interactive* compatibility
        wrapper, so every turn an extension originated emitted lifecycle events
        stamped ``source="interactive"``, ``submitter="human"`` — a bus message
        indistinguishable from a person typing, which is precisely what phase 2's
        provenance fields exist to tell apart. It now builds its own
        :class:`~tau_agent_core.submission.Submission` with
        ``source="extension"``.

        The ``submitter`` is :data:`UNATTRIBUTED_EXTENSION`, not the calling
        extension's name: a session has ONE shared :class:`ExtensionContext`
        (see that constant), so this object cannot know which extension called
        it. :meth:`ExtensionAPI.submit` is bucket-bound and reports the real
        name — that is the attributed door, and the reason this one is
        deprecated rather than merely renamed.

        Concurrency is unchanged: ``"enqueue"``. A call from a DIFFERENT event
        source's own coroutine (a second bus message, a second timer tick) is
        genuine concurrency and waits for the in-flight turn, then runs — never
        a silent drop, never corrupted history. (``api.submit`` defaults to
        ``"reject"`` instead, the Fail-Early default; ask for ``"enqueue"``
        explicitly there if you want this behaviour.)

        Re-entrancy — a hook (``input``/``tool_call``/``turn_end``/
        ``user_turn_end``) belonging to THIS SAME in-flight turn calling
        ``ctx.prompt()`` before its own turn has returned — is NOT the caller's
        problem to arbitrate: ``submit()`` detects it (same ``asyncio.Task`` as
        the turn already holding the admission lock) and RAISES immediately
        (review fix, must_fix #2). Before this, such a call deadlocked
        silently forever — every ``multitask_strategy`` either inspects or
        waits on a lock this task already holds, so nothing could ever release
        it. Decision 3's depth cap anticipates a *bounded* form of
        self-submission; nested execution that bypasses the lock to actually
        satisfy one is not implemented, so this raises unconditionally rather
        than hanging.

        Raises:
            RuntimeError: if no session is bound (Fail-Early — nothing to prompt).
            RuntimeError: this call is reentrant on the in-flight turn's own
                asyncio task (see above).
        """
        result: SubmissionResult = await self._require_session().submit(
            Submission(
                text=text,
                source="extension",
                submitter=UNATTRIBUTED_EXTENSION,
                submission_id=uuid4().hex,
                multitask_strategy="enqueue",
            )
        )
        return result.messages

    # ------------------------------------------------------------------
    # Session-control op surface (E3-ctx / step S19)
    #
    # These expose the LANDED session-tree substrate on the base handler
    # context so agent tools can drive it (plan decision 2; the E2 gatekeeper
    # veto is the safety). Each delegates to the one authoritative session log
    # the bound ``AgentSession`` persists through, so the mutation is visible on
    # both the TUI live path and headless. pi keeps fork/navigate command-only
    # (types.ts:354-373); τ places them on the base context (decision 2 / §7
    # E3-b). Fail-Early: an unbound session, or an op the concrete log cannot
    # satisfy (e.g. exporting an in-memory log), RAISES rather than no-ops.
    # ------------------------------------------------------------------

    def _require_session(self) -> Any:
        """The bound ``AgentSession``, or raise (Fail-Early, no silent no-op)."""
        if self._session is None:
            raise RuntimeError("session-control op: no session bound to ExtensionContext")
        return self._session

    async def compact(self, custom_instructions: str | None = None, defer: bool = False) -> Any:
        """Compact the active conversation (delegates to ``AgentSession.compact``).

        Runs the full append-only compaction pipeline on the bound session's log
        (``agent_session.py`` ``compact``): build the active-path entries, summarize
        the compacted prefix via the LLM, and APPEND a compaction entry so the
        prefix drops out of future context at read time. Returns the
        ``CompactionResult`` (or ``None`` when there is nothing to compact).

        Two variants (S20 / decision 3):

        - ``defer=False`` (default): the IMMEDIATE variant — compacts now and
          returns the ``CompactionResult``.
        - ``defer=True``: the TURN-END-DEFERRED variant — a tool calling this
          mid-turn cannot compact under the live agent loop, so the intent is
          RECORDED and applied exactly once at the tail of ``prompt()`` (the same
          site as auto-compaction). Returns ``None`` immediately; the tool then
          returns its own normal result.

        Args:
            custom_instructions: Optional extra focus for the summary.
            defer: When True, record the intent for the end-of-prompt drain
                instead of compacting now.
        """
        session = self._require_session()
        if defer:
            session._defer_compact(custom_instructions=custom_instructions)
            return None
        return await session.compact(custom_instructions=custom_instructions)

    def entries(self) -> list[dict[str, Any]]:
        """The bound session log's raw, append-only entries (all kinds).

        Thin pass-through to ``SessionLog.entries()`` — the same entry list a
        ``ConversationTree`` folds into context. Read-only: a copy per the log's
        contract, so mutating the returned list does not touch the log.
        """
        entries: list[dict[str, Any]] = self._require_session().session_log.entries()
        return entries

    def resolve_model(self, model: Any = None) -> Any:
        """Resolve ``model`` to a ``tau_llm.Model``.

        ``None`` → the session's current model. A **string** → looked up in the same
        config ``models`` registry the TUI picks from, via the resolver already injected
        on the session (``AgentSession.set_model_resolver`` / ``backends.make_model_resolver``).
        A ``Model`` → used as-is.

        Model routing through the registry is the point: an extension's model choice is
        then configured inline with the agent's, in its existing config slice — e.g.
        ``"extensions": {"retrieval_review": {"model": "local-llm-small"}}`` — with no
        extension-private client plumbing.
        """
        session = self._require_session()
        if model is None:
            return session._model
        if isinstance(model, str):
            resolver = session._model_resolver
            if resolver is None:
                raise RuntimeError(
                    f"cannot resolve model {model!r} by name: no model resolver is bound to "
                    "this session (the TUI/headless frontends bind one from config 'models')"
                )
            return resolver(model)
        return model

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: Any = None,
        constraints: Any = None,
        api_key: str | None = None,
    ) -> Any:
        """One LLM request/response — no agent loop, no tree writes (C1).

        The primitive behind every "classify / extract / draft" story where the *result*
        matters, not the process. Deliberately **stateless and session-free**: it touches
        neither the entry log nor the cursor, so it is safe under ``asyncio.gather`` at
        any fan-out. Errors propagate (no retry policy hidden inside).

        With ``constraints``, this is the retrieval-review verdict primitive::

            verdicts = await asyncio.gather(*[
                ctx.complete(
                    [{"role": "user", "content": f"Include {doc}?"}],
                    model="local-llm-small",
                    constraints=DecodeConstraints(choices=["include", "exclude"]),
                )
                for doc in docs
            ])

        Each verdict is constraint-verified by the provider, so a server that dropped the
        grammar raises rather than returning free prose as a verdict.

        Args:
            messages: τ message dicts (``{"role": ..., "content": ...}``).
            model: model name (resolved via the config registry), a ``Model``, or None
                for the session's current model.
            constraints: an optional ``tau_llm.DecodeConstraints``.
            api_key: overrides the session's key.

        Returns:
            A ``tau_llm.AssistantMessage``.

        Raises:
            RuntimeError: the completion errored or was aborted.
            ConstraintViolation: the constraint did not hold (see DecodeConstraints).
        """
        from tau_agent_core.completion import CompletionFailed, resolved_complete
        from tau_agent_core.usage import usage_of

        session = self._require_session()
        resolved = self.resolve_model(model)

        options: dict[str, Any] = {}
        key = api_key if api_key is not None else session._api_key
        if key is not None:
            options["api_key"] = key
        if constraints is not None:
            options["constraints"] = constraints

        # The shared completion door (C1). It resolves the model (already resolved
        # here) and runs the shared error/aborted check, raising CompletionFailed —
        # which carries the offending response so we can still bill it below. It does
        # NOT bill and does NOT own the "length" policy: both stay right here.
        try:
            response = await resolved_complete(
                resolved, {"messages": messages}, options=options or None
            )
        except CompletionFailed as exc:
            # Bill even on error: a completion we cannot USE is not one that was free —
            # the provider charged for the tokens (tau_agent_core.usage). Then translate
            # to this door's taxonomy: a bare stop_reason="error" AssistantMessage handed
            # back to an extension would read as a successful (empty) answer.
            session.record_side_usage(usage_of(exc.response))
            raise RuntimeError(f"ctx.complete() failed: {exc.detail}") from exc

        # Bill what the provider says it spent, BEFORE the Fail-Early length check below —
        # a completion we cannot USE is not a completion that was free. The
        # stop_reason="length" case makes that concrete: the model generated a full
        # max_tokens of output and the provider charged for every one of them, and we
        # then throw the answer away as a truncated prefix. Recording only on the
        # success path would silently undercount exactly the calls that went wrong,
        # which is the failure this whole ledger exists to end. A provider that
        # reports nothing yields a true zero (tau_agent_core.usage).
        session.record_side_usage(usage_of(response))

        stop_reason = getattr(response, "stop_reason", None)

        # "length" is a truncated answer, not a short one, and NOTHING downstream looks
        # at stop_reason — so letting it through hands the caller a prefix and lets them
        # believe it is the whole thing. The extract-a-JSON-object case makes the damage
        # concrete: `{"verdict": "include", "confidence": 0.` parses as nothing, or
        # worse, a shorter truncation parses as something WRONG.
        if stop_reason == "length":
            raise RuntimeError(
                "ctx.complete() hit the token limit (stop_reason='length'): the answer is "
                "a truncated PREFIX, not a complete response. Raise Model.max_tokens."
            )

        # Observability echo (G4/C): once the completion is VERIFIED (billed above, not a
        # truncated prefix), record WHAT constrained it. ``ctx.complete()`` is the one
        # honest caller — the main agent loop applies no DecodeConstraints, so echoing
        # ``describe()`` there would emit ``{"kind":"none"}`` on every turn (a placeholder,
        # Fail-Early forbids it). Guard on ``has_constraint()`` so a bare ``tool_choice``/
        # ``extra_body`` DecodeConstraints (no real grammar) emits nothing.
        if constraints is not None and constraints.has_constraint():
            self._ui.emit_constraints(constraints.describe())

        return response

    async def complete_text(
        self,
        messages: list[dict[str, Any]],
        *,
        model: Any = None,
        constraints: Any = None,
        api_key: str | None = None,
    ) -> str:
        """:meth:`complete`, returning the response's text (the common case).

        Raises if the response carries no text — an empty answer is a failure, not an
        empty string to be silently threaded onward (Fail-Early).

        The text is returned VERBATIM, not stripped. Under a constraint, whitespace is
        part of the constrained value: ``grammar.fixed("yes ")`` really does force the
        trailing space (verified live against llguidance), and the provider verified the
        output *with* it. Stripping here would hand the caller a string the constraint
        never produced — and one that fails the very membership check it just passed.
        """
        from tau_llm.types import TextContent

        response = await self.complete(
            messages, model=model, constraints=constraints, api_key=api_key
        )
        text = "".join(c.text for c in response.content if isinstance(c, TextContent))
        if not text.strip():
            raise RuntimeError("ctx.complete_text() returned an empty response")
        return text

    async def spawn_branch(
        self,
        parent_id: str | None,
        prompt: str,
        *,
        tools: list[str],
        model: Any = None,
        max_turns: int | None = None,
        label: str | None = None,
        system_prompt: str | None = None,
    ) -> "BranchResult":
        """Run a tool-using sub-agent in its own lane of THIS conversation (C2/W14).

        The sub-agent is a real ``AgentSession`` whose log is a
        :class:`~tau_agent_core.session_log.BranchView` — a second cursor over the same
        entry log. Its turns are recorded as a real in-tree branch (not an ephemeral
        side-session grafted back as a blob), so the session tree stays the single truth
        for everything the agent did, and on the JMFTS store the finished branch is
        already a searchable subtree.

        ``parent_id`` chooses the inherited context: the fold walks up from it, so the
        sub-agent sees exactly the shared conversation prefix down to that point, plus
        its own work. Its writes are lane-tagged and can never reach the primary
        context (they are never ancestors of the primary leaf) nor move the primary
        cursor.

        ``tools`` is a **required hard allowlist**, deliberately not defaulted. Sub-agents
        share the process and cwd, so "inherit the parent's tools" would silently hand a
        retrieval evaluator ``write`` and ``bash``; and defaulting to ``[]`` would just as
        silently produce a sub-agent that cannot do the job it was spawned for. Naming the
        tools is the only option that cannot fail quietly. Pass ``[]`` to mean none.

        ``system_prompt`` defaults to ``None``, which inherits the spawning session's own
        prompt (``session._system_prompt``) — today's behaviour, unchanged for every
        existing caller. Passing a string forks with a *different* spec instead: the one
        concrete blocker on "fork at a node with a different spec"
        (NODE-ADDRESSABLE-AGENTS.md §5 recipe 2, W1) was that this call hardcoded the
        parent's prompt with no override.

        **Failure is contained, not propagated** (§9.2/5): a sub-agent that errors marks
        its own branch and returns ``ok=False``; it never aborts the primary loop. A
        raise here would mean one bad evaluator in a fan-out kills the whole turn.

        **The branch's events are bracketed**: each one is forwarded onto the primary
        bus's ``branch_event`` channel, and a single terminal ``branch_end`` (carrying
        ``lane``, ``label`` and the ``error`` that ended it, or ``None``) is emitted
        from a ``finally`` — so a consumer that opened something on the first event can
        close it whether the branch finished, failed, or was cancelled.

        **The sub-agent starts with no extensions** (NODE-ADDRESSABLE-AGENTS.md Decision
        4 / W4), by choice rather than oversight: the constructor below passes no
        ``extensions=``, so a forked session never re-registers the parent's hooks. This
        is deliberate, not a gap to file — inheriting them would make the hook runner
        re-entrant across two concurrent turns (the parent's turn still running, the
        branch's turn also running, both walking the same registered hook state), which
        is a materially larger change than this method's scope. A caller that wants the
        sub-agent to carry extensions loads them onto ``sub`` itself before ``prompt()``.

        Returns:
            A :class:`BranchResult`. **Check ``ok``** — a failed branch returns a result,
            it does not raise.
        """
        from tau_agent_core.agent_session import AgentSession
        from tau_agent_core.session_log import open_branch

        session = self._require_session()
        log = session.session_log
        branch = open_branch(log, parent_id, label=label or prompt[:60])

        missing = [t for t in tools if t not in {getattr(x, "name", None) for x in session._tools}]
        if missing:
            # Fail-Early, and BEFORE any model call: silently running a sub-agent with
            # fewer tools than asked for produces a plausible-looking wrong answer
            # ("I couldn't find it") that reads as a real verdict.
            available = sorted(str(getattr(x, "name", "?")) for x in session._tools)
            raise ValueError(
                f"spawn_branch: tool(s) {missing!r} are not available on this session "
                f"(available: {available}). A sub-agent silently missing a tool it was "
                "told to use would return a confident wrong answer."
            )
        scoped = [t for t in session._tools if getattr(t, "name", None) in set(tools)]

        sub = AgentSession(
            session_log=branch,
            model=self.resolve_model(model),
            system_prompt=session._system_prompt if system_prompt is None else system_prompt,
            tools=scoped,
            api_key=session._api_key,
            max_turns=max_turns,
            model_resolver=session._model_resolver,
        )

        # Forward the sub-agent's events onto the primary bus, lane-tagged, on their OWN
        # channel (§9.2/4). Deliberately NOT re-emitted onto the primary AgentEvent
        # stream: an ``AgentEvent`` carries no run identity, so a branch's message_update
        # deltas would be indistinguishable from the primary stream's and a TUI would
        # interleave a sub-agent's tokens into the user's answer. A separate channel means
        # a frontend OPTS IN to branch progress rather than having to filter it out —
        # and a frontend that knows nothing about branches keeps working unchanged.
        async def _forward(event: Any) -> None:
            await session._events.emit_channel(
                "branch_event", lane=branch.lane, label=branch.label, event=event
            )

        sub.subscribe(_forward)

        # The branch's TERMINAL bracket, emitted in a ``finally`` below on the
        # ``branch_end`` channel — the counterpart of ``submission_end``, and for the
        # identical reason. A consumer that opened a span when the branch's first
        # event arrived (the TUI's RenderRouter opens a render lane) has to be able to
        # close it HOWEVER the branch ended, and the sub-agent's own ``agent_end`` is
        # not that signal: ``AgentLoop.run`` emits it after its while loop rather than
        # from a ``finally``, so a branch whose turn raises (an ``ErrorEvent`` becomes
        # a ``RuntimeError``; a dropped connection) or is cancelled (``abort()``
        # cancels every forked task, and ``CancelledError`` never reaches the
        # containment handler below) emits no ``agent_end`` at all. The span left open
        # renders as a permanently "Working…" exchange — the silent-hang shape this
        # lifecycle exists to remove.
        branch_error: str | None = None
        try:
            try:
                messages = await sub.prompt(prompt)
            except Exception as exc:  # noqa: BLE001 — containment is the point (§9.2/5)
                branch_error = str(exc)
                branch.append_custom_entry(
                    "branch_error", {"lane": branch.lane, "label": branch.label, "error": str(exc)}
                )
                return BranchResult(
                    ok=False,
                    lane=branch.lane,
                    label=branch.label,
                    leaf=branch.cursor,
                    messages=[],
                    error=str(exc),
                )
            except BaseException as exc:
                # NOT containment — this re-raises. It exists so the terminal event
                # can name what actually ended the branch on the one path that is
                # not an ``Exception``: ``CancelledError`` from ``abort()`` or from
                # session shutdown. ``str()`` on a bare cancel is empty, so the type
                # name is the honest answer rather than an empty error string.
                branch_error = str(exc) or type(exc).__name__
                raise
        finally:
            await session._events.emit_channel(
                "branch_end", lane=branch.lane, label=branch.label, error=branch_error
            )

        return BranchResult(
            ok=True,
            lane=branch.lane,
            label=branch.label,
            leaf=branch.cursor,
            messages=messages,
            error=None,
        )

    async def summarize_branch(
        self, from_entry: str, custom_instructions: str | None = None
    ) -> list[dict[str, Any]]:
        """Summarize the subtree at ``from_entry`` and splice it onto the active path.

        Ports the summarize arm of ``TauBackend.navigate_tree`` (backends.py:246)
        onto the bound session's own log: extract the branch text
        (``ConversationTree.subtree_text(from_entry)``), summarize it via the module
        ``summarize_branch`` (session_manager.py:705 — already raise-based on a
        failed/empty summary, Fail-Early), then APPEND a ``branch_summary`` entry
        parented at ``from_entry`` (``SessionLog.append_branch_summary``). The
        abandoned children drop out of context via the ``parentId`` walk.

        Returns the re-rendered active-path messages (``ConversationTree.context_for``).
        """
        from tau_agent_core.conversation_tree import ConversationTree
        from tau_agent_core.session_manager import summarize_branch as _summarize_branch

        session = self._require_session()
        log = session.session_log
        old_leaf = log.cursor
        branch_text = ConversationTree(log.entries(), old_leaf).subtree_text(from_entry)
        summary, summary_usage = await _summarize_branch(
            branch_text,
            session._model,
            api_key=session._api_key,
            custom_instructions=custom_instructions,
        )
        session.record_side_usage(summary_usage)
        log.append_branch_summary(summary, from_entry)
        return ConversationTree(log.entries(), log.cursor).context_for()

    async def navigate(
        self,
        target_id: str | None,
        summarize: bool = False,
        custom_instructions: str | None = None,
    ) -> list[dict[str, Any]]:
        """Move the bound session's cursor to ``target_id`` and return the new context.

        Ports ``TauBackend.navigate_tree`` (backends.py:246) onto the bound
        session's own log. ``summarize=False`` APPENDs a ``navigate`` entry (zero
        LLM calls); the abandoned branch drops out of context via the ``parentId``
        walk but stays on disk. ``summarize=True`` delegates to
        :meth:`summarize_branch` (append a ``branch_summary`` at the branch point).
        A ``target_id`` already at the cursor is a no-op (pi ``navigateTree:2716``).

        Returns the re-rendered active-path messages (``ConversationTree.context_for``).
        """
        from tau_agent_core.conversation_tree import ConversationTree

        session = self._require_session()
        log = session.session_log
        if target_id == log.cursor:
            return ConversationTree(log.entries(), log.cursor).context_for()
        if summarize:
            if target_id is None:
                raise ValueError("navigate(summarize=True) requires a target_id to summarize")
            return await self.summarize_branch(target_id, custom_instructions=custom_instructions)
        log.append_navigate(target_id)
        return ConversationTree(log.entries(), log.cursor).context_for()

    async def fork(
        self,
        entry_id: str | None = None,
        mode: Literal["in_place", "export"] = "in_place",
        defer: bool = False,
    ) -> Any:
        """Fork the conversation — one op, two modes (plan §7 decision E3-b).

        - ``mode="in_place"`` (default): branch WITHIN the one session log by
          APPENDing a ``navigate`` to ``entry_id`` (``entry_id=None`` → pre-root),
          so the next turn appends a sibling branch off that point. Returns the
          re-rendered active-path messages (``ConversationTree.context_for``). This
          is the ``navigate+append`` in-place fork.
        - ``mode="export"``: copy the session into a NEW file via ``Session.fork``
          (session_store.py:347; the source log is never touched), optionally
          positioning the new file's cursor at ``entry_id``. Returns the new
          session file path as a string.

        ``defer=True`` (S20 / decision 3): a tool calling this mid-turn cannot
        re-parent the log under the live agent loop, so the intent is RECORDED and
        applied exactly once at the tail of ``prompt()``. Returns ``None``
        immediately; the tool then returns its own normal result.

        Fail-Early: export requires a concrete file-backed ``Session`` log — an
        in-memory SDK log cannot be exported to a file and RAISES rather than
        fabricating one.
        """
        from tau_agent_core.conversation_tree import ConversationTree

        session = self._require_session()
        if defer:
            session._defer_fork(entry_id=entry_id, mode=mode)
            return None
        log = session.session_log
        if mode == "in_place":
            log.append_navigate(entry_id)
            return ConversationTree(log.entries(), log.cursor).context_for()
        if mode == "export":
            fork_classmethod = getattr(type(log), "fork", None)
            if fork_classmethod is None:
                raise RuntimeError(
                    "fork(mode='export'): the bound session log is not file-backed and "
                    "cannot be exported to a new file"
                )
            # Fork into the source's own cwd partition (pi keeps a fork in the
            # same session dir); fall back to the context cwd for a log that does
            # not expose one.
            cwd = getattr(log, "cwd", None) or self._cwd
            forked = fork_classmethod(log, cwd)
            if entry_id is not None:
                forked.append_navigate(entry_id)
            return str(forked.path)
        raise ValueError(f"fork: unknown mode {mode!r} (expected 'in_place' or 'export')")

    def set_ui_delegate(self, delegate: Any) -> None:
        """Set the TUI delegate for UI methods.

        This enables TUI mode on the internal ExtensionUI,
        setting the delegate for all UI interactions.

        Args:
            delegate: TUI delegate object implementing confirm/select/input/notify.
        """
        self._ui._mode = "tui"
        self._ui._tui_delegate = delegate

    def set_record_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        """Install the headless JSON record sink on the shared UI (E7 §3 / S49).

        Delegates to :meth:`ExtensionUI.set_record_sink`; the headless ``--mode json``
        path calls this (via :meth:`AgentSession.set_extension_record_sink`) so every
        loaded extension's ``api.ui.notify(...)`` becomes a structured record on the
        JSON stream instead of a stderr line (anchor G10).
        """
        self._ui.set_record_sink(sink)

    def emit_veto_record(self, *, extension: str | None, tool: str, reason: str) -> None:
        """Emit a blocked-tool VETO record via the shared UI's record sink (E7 §3 / S50).

        Thin pass-through to :meth:`ExtensionUI.emit_veto`. The agent loop reaches this
        through the bound :class:`ExtensionRunner` (``emit_veto_record``) when a
        `tool_call` hook vetoes a call, so the JSON stream carries a
        ``kind: "veto"`` / ``blocked: true`` record (anchor G11) alongside the closed
        ``AgentEvent`` set. A no-op unless the headless ``--mode json`` path installed a
        sink.
        """
        self._ui.emit_veto(extension=extension, tool=tool, reason=reason)

    def set_headless_ui_defaults(self, policy: dict[str, str]) -> None:
        """Set the headless dialog-answer policy on the shared UI (E7 §3 / S48).

        Delegates to :meth:`ExtensionUI.set_headless_defaults`; the frontends call
        this (via :meth:`AgentSession.set_headless_ui_defaults`) with the resolved
        ``--ui-defaults`` / config policy so a headless dialog auto-answers only
        when the user opted in. Validation (unknown method/token) raises
        ``ValueError`` — the caller surfaces it as a clean CLI error.
        """
        self._ui.set_headless_defaults(policy)


#: Shared body of ``ExtensionAPI.set_session_name`` / ``.get_session_name``
#: AND, per docs/RPC-TIER-B.md B5, the RPC ``set_session_name``/
#: ``get_session_name`` verbs (``rpc/commands.py``) — ONE definition, not two
#: copies of the same Fail-Early raise. Module-level rather than methods on
#: any class: §1.1 forbids adding these appenders to the ``SessionLog``
#: Protocol, and the unit's own instruction is "do not add a method to
#: ``AgentSession`` if the shared helper can live elsewhere" — here, next to
#: the pre-existing reference implementation these bodies were extracted
#: FROM, is that elsewhere. `tau_agent_core.rpc.commands` sits ABOVE this
#: module (it already imports `agent_session_runtime`/`commands`/
#: `submission`; nothing here imports `rpc`), so the RPC verb handlers import
#: these two functions — never the reverse, and this module gains no new
#: dependency. Deliberately independent of `rpc.commands.require_log_appender`
#: (B0's generic "does this log have this appender" precondition, meant for a
#: verb with no pre-existing extension-API body to reuse, e.g. B1's
#: set_model): layering a second, redundant hasattr check on top of the one
#: already inside these functions would check the same fact twice for no
#: reason. `session` is typed `Any` for the same reason
#: `ExtensionAPI._session` is (see its docstring) — this module must not
#: import `AgentSession` (a real cycle: `agent_session.py` imports
#: `ExtensionAPI` from here).
def apply_session_name(session: Any, name: str) -> None:
    """Persist ``name`` as ``session``'s durable display name via
    ``append_session_info`` — the SAME entry kind the file-backed
    ``tau_coding_agent.session_store.Session`` already exposes through its
    ``.name`` property (and ``display_title()``'s "name, else first user
    message" fallback), so a name set here shows up in the session
    selector / TUI title exactly like a manually-renamed session file.
    ``ConversationTree`` never folds a ``session_info`` entry into context
    (the same non-message treatment as ``model_change``/``thinking_change``),
    so this is ambient, reload-invariant metadata: persisted, but never model
    input.

    The prior implementation looked for a ``_session_name`` attribute that
    ``AgentSession`` never defines — a silent no-op on every real session
    (only a ``MagicMock``'s auto-vivified attributes made the old tests
    pass). This corrects it to actually persist (Fail-Early: raise instead
    of silently doing nothing).

    Raises:
        RuntimeError: no session is bound, or the bound session's log has no
            ``append_session_info`` (e.g. the SDK's RAM-only
            ``InMemorySessionLog`` — session naming needs a file-backed log).
        ValueError: ``name`` is empty.
    """
    if not name:
        raise ValueError("set_session_name: name must be a non-empty string")
    log = getattr(session, "session_log", None)
    if log is None or not hasattr(log, "append_session_info"):
        raise RuntimeError(
            "set_session_name: the bound session has no append_session_info "
            "log (e.g. an in-memory SDK session) — nowhere durable to land the name"
        )
    log.append_session_info(name)


def read_session_name(session: Any) -> str | None:
    """Read ``session``'s current durable display name, or ``None`` if never
    set.

    Reads the SAME ``.name`` property the file-backed ``Session`` already
    derives from its latest ``session_info`` entry, so a fresh call always
    reflects the persisted log rather than a cached value — correct across
    a reload.

    Raises:
        RuntimeError: no session is bound, or the bound session's log has no
            ``name`` (e.g. an in-memory SDK session).
    """
    log = getattr(session, "session_log", None)
    if log is None or not hasattr(log, "name"):
        raise RuntimeError(
            "get_session_name: the bound session has no durable name to read "
            "(e.g. an in-memory SDK session)"
        )
    name = log.name
    return str(name) if name else None


class ExtensionAPI:
    """Public API exposed to extension modules.

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

    This is the ONLY API extension modules use. Extensions must not
    import τ-agent-core internals.

    Attributes:
        _registry: ExtensionRegistry for tool/command management.
        _event_bus: EventBus for event subscription.
        _context: ExtensionContext with session state.
        _session: AgentSession for messaging.
    """

    def __init__(
        self,
        registry: ExtensionRegistry | None = None,
        event_bus: EventBus | None = None,
        context: ExtensionContext | None = None,
        session: Any = None,
        hook_handlers: ExtensionHandlers | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize ExtensionAPI.

        Args:
            registry: ExtensionRegistry for tool/command/flag management.
            event_bus: EventBus for event subscription.
            context: ExtensionContext with session state.
            session: AgentSession for messaging.
            hook_handlers: This extension's OWN ``ExtensionHandlers`` bucket in the
                session ``ExtensionRunner`` (load order preserved). ``api.on()``
                routes the four MUTATING hooks (``tool_call`` / ``tool_result`` /
                ``before_agent_start`` / ``context``) here — the dispatch surface
                the loop's hook call-sites gate on. Left ``None`` for an api that is
                not bound to a runner bucket; registering a hook on such an api then
                RAISES (Fail-Early) rather than silently no-op'ing (S24).
            config: This extension's OWN per-extension config slice (E6 §2 / S40).
                Sourced from ``~/.tau/config.json`` ``"extensions": {"<name>": {…}}``
                keyed by the extension's file stem, with per-run
                ``--ext-config <name>.<key>=<value>`` overrides applied on top. The
                session slices the right entry per extension in
                ``AgentSession._bind_extension_api`` and passes it here; ``None`` →
                an empty dict (an unconfigured extension reads ``{}``, never a
                fabricated value — Fail-Early leaves defaulting to the extension).
        """
        # Lazy initialization for backward compatibility
        if registry is None:
            from tau_agent_core.extensions.registry import ExtensionRegistry

            registry = ExtensionRegistry()
        if event_bus is None:
            from tau_agent_core.events import EventBus

            event_bus = EventBus()
        if context is None:
            context = ExtensionContext()

        self._registry = registry
        self._event_bus = event_bus
        self._context = context
        self._session = session
        # This extension's own hook-handler bucket in the session's
        # ExtensionRunner (None when the api is not bound to a runner).
        self._hook_handlers = hook_handlers
        # This extension's own per-extension config slice (S40). Copied so a later
        # mutation of the source map (or another extension's slice) can't bleed in;
        # nested values are shared by reference (config is read-only by contract).
        self._config: dict[str, Any] = dict(config or {})
        # Bind the live session onto the context so ctx.get_context_usage()
        # (delegated to the session in pi) reads real messages + model window.
        self._context._session = session

    def on(self, event: str, handler: Callable) -> Callable[[], None]:
        """Subscribe to an event — routed by KIND (S24 bridge).

        The six MUTATING hooks (``ExtensionRunner.HOOK_EVENTS``: ``tool_call`` /
        ``tool_result`` / ``before_agent_start`` / ``input`` / ``turn_end`` /
        ``user_turn_end``) AND the
        two notify-grade session-lifecycle hooks (``ExtensionRunner.LIFECYCLE_EVENTS``:
        ``session_start`` / ``session_shutdown``, S41) are dispatched by the
        session's separate ``ExtensionRunner``, whose call-sites gate on
        ``has_handlers(event)``. Those registrations must land in THIS extension's
        runner bucket (``self._hook_handlers``), not on the notify ``EventBus`` —
        otherwise they are a silent no-op in a real session (the bug S24 closes),
        and the lifecycle hooks in particular route through the runner precisely so
        their handler errors are SURFACED (S44) instead of swallowed like the bus.
        Every other (notify) event keeps going to the ``EventBus``.

        ``turn_end`` (S43) is the mutating variant: ``api.on("turn_end", …)`` now
        routes to the runner, where a handler may return ``{message}`` for a durable
        append or return nothing to observe. The notify-grade ``turn_end``
        ``AgentEvent`` on the ``EventBus`` is UNCHANGED — pure observers still reach
        it via ``api.on("all", …)`` or :meth:`AgentSession.subscribe`.

        ``user_turn_end`` is ``turn_end``'s once-per-``prompt()`` sibling (§12.4 /
        §16.5). ``turn_end`` fires per AGENT-LOOP turn — six times for an utterance
        resolved in six tool round-trips — which is right for a per-completion
        observer and wrong for anything that should happen once per utterance.
        ``api.on("user_turn_end", …)`` fires exactly once, after the loop, the
        followUp drain and auto-compaction, with the same durable ``{message}``
        append. Choose by cadence: per completion, or per utterance.

        The retired ``context`` hook (E5 §3.2 / S30) is rejected UP FRONT: it was
        removed from ``HOOK_EVENTS``, so left unguarded it would fall through to the
        notify ``EventBus`` and bind silently to a channel nothing ever emits — a
        dead no-op. Fail-Early: raise an unknown-hook error naming the durable
        replacement instead.

        A custom **inter-extension channel** ``ext:<name>:<topic>`` (E7 §3 / S52) is
        neither a hook nor a retired name, so it takes the same ``EventBus`` fallthrough
        as a notify event: ``api.on("ext:pub:ping", handler)`` receives whatever the
        ``pub`` extension broadcasts via :meth:`emit`. These channels are in-RAM,
        fire-and-forget, and never model-visible — explicitly NOT a backplane.

        Args:
            event: Event type (e.g., 'agent_start', 'tool_call', 'all').
            handler: Callable that receives the event (an ``AgentEvent`` for notify
                events; a ``(event_dict, ctx)`` pair for hook events).

        Returns:
            An unsubscribe function.

        Raises:
            RuntimeError: registering the retired ``context`` hook (removed in E5
                §3.2 / S30), or registering a mutating hook on an api that was never
                bound to a runner bucket (``hook_handlers is None``). Fail-Early — a
                hook with nowhere to dispatch is a construction bug, not a no-op.
        """
        from tau_agent_core.extensions.runner import ExtensionRunner

        if event in _RETIRED_HOOKS:
            raise RuntimeError(
                f"api.on({event!r}): the {event!r} hook was removed in E5 §3.2 / S30. "
                "Under the durable-hook invariant the model's input is exactly the "
                "system prompt + the linear active path — there is no per-call "
                "message-list transform. Achieve the same effect with a durable node "
                "edit: patch the triggering `tool_result` via api.on('tool_result', …) "
                "and/or inject a pre-first-call message via "
                "api.on('before_agent_start', …)."
            )

        if event in ExtensionRunner.HOOK_EVENTS or event in ExtensionRunner.LIFECYCLE_EVENTS:
            if self._hook_handlers is None:
                raise RuntimeError(
                    f"api.on({event!r}): this ExtensionAPI is not bound to an "
                    "ExtensionRunner bucket, so the hook could never fire. "
                    "Obtain the api from AgentSession's extension load path "
                    "(each factory is handed a bucket-bound api)."
                )
            hook_bucket = self._hook_handlers
            hook_bucket.on(event, handler)

            def unsubscribe_hook() -> None:
                handlers = hook_bucket.handlers.get(event)
                if handlers is not None:
                    try:
                        handlers.remove(handler)
                    except ValueError:
                        pass

            return unsubscribe_hook

        return self._event_bus.on(event, handler)

    def _emitting_extension_name(self, op: str = "api.emit") -> str:
        """This api's OWN extension name — the unforgeable identity (E7 §3 / S52).

        Derived from THIS api's runner bucket path (``Path(bucket.path).stem`` — the
        same stem that keys :attr:`config`), so :meth:`emit` can only publish under
        the caller's own name and :meth:`submit` can only submit under it. Fail-Early:
        a bare :class:`ExtensionAPI` bound to no runner bucket has no extension
        identity, so raise rather than emit on an anonymous ``ext::<topic>`` channel
        or attribute a submission to nobody.

        Args:
            op: The calling method, for the error message only.
        """
        if self._hook_handlers is None:
            raise RuntimeError(
                f"{op}: this ExtensionAPI is not bound to an ExtensionRunner "
                "bucket, so it has no extension identity to act under. Obtain "
                "the api from AgentSession's extension load path (each factory "
                "is handed a bucket-bound api)."
            )
        return Path(self._hook_handlers.path).stem

    async def submit(
        self,
        text: str,
        *,
        multitask_strategy: MultitaskStrategy = "reject",
        images: list[dict[str, Any]] | None = None,
        correlation: dict[str, Any] | None = None,
        allow_user_input: bool = False,
    ) -> SubmissionResult:
        """Originate an agent turn as THIS extension (docs/SUBMISSION-LIFECYCLE.md).

        The extension half of "one door for every input source": an extension bound
        to an external event source (a bus subscription, a timer, a webhook) is the
        one deciding when a turn happens, and this is how it says so — the same
        :meth:`AgentSession.submit` admission point the TUI, headless, and the SDK
        funnel through, so concurrency policy is decided ONCE in the core instead of
        re-invented per extension (``nats_bus``'s hand-rolled ``turn_in_flight`` flag
        is the workaround this deletes).

        ``source="extension"`` and ``submitter=<this extension's own name>`` are
        supplied BY THIS BINDING from the caller's own runner bucket
        (:meth:`_emitting_extension_name`) and are deliberately **not parameters**
        — the same unforgeability :meth:`emit` has for ``ext:<name>:<topic>``
        channels. **An extension cannot claim to be a human**, or to be another
        extension. That matters because phase 2 put ``source``/``submitter`` on every
        :class:`~tau_agent_core.events.AgentEvent` precisely so a renderer could tell
        a bus-driven turn from a typed one; a spoofable field would make the
        distinction worthless.

        ``expand_commands`` is likewise not a parameter: it stays ``False`` (the
        :class:`~tau_agent_core.submission.Submission` default) so injected text can
        never smuggle a ``/compact`` through a bus payload — pi's
        ``sendUserMessage(expandPromptTemplates: false)``. An extension that wants to
        compact calls :meth:`ExtensionContext.compact`, the typed API, not a string.

        Args:
            text: The utterance to run a turn on.
            multitask_strategy: Policy against an in-flight turn — LangGraph's
                parameter, on the submission rather than the submitter. Defaults to
                ``"reject"`` (Fail-Early: a refusal you can see, not a queue you
                forgot about), which returns ``accepted=False`` with a
                ``rejection_reason`` rather than raising or dropping silently. See
                :meth:`AgentSession.submit` for every strategy's exact semantics.
            images: Optional image dicts for a multimodal submission.
            correlation: Free-form origin detail — bus subject + binding id, cron id,
                HTTP request id — carried onto every event this turn emits so a
                renderer can fan out to the right consumer. Validated JSON-safe at
                :class:`~tau_agent_core.submission.Submission` construction
                (decision 4): a live object here raises HERE, naming the key, rather
                than detonating in a JSON renderer three hops downstream.
            allow_user_input: Jupyter ``allow_stdin`` — whether code running under
                THIS submission may prompt a human. Default ``False``: a bus- or
                timer-driven turn has nobody at a keyboard. ENFORCED: for the whole
                of the admitted turn, :class:`ExtensionUI`'s blocking dialogs
                (``confirm``/``select``/``input``/``form``) bypass the TUI delegate
                and take the headless-answer route, so with no ``--ui-defaults``
                policy they raise :class:`HeadlessDialogError` instead of putting a
                modal in front of whoever happens to be at the terminal. Pass
                ``True`` only when a human really is expecting to be asked.

        ``depth`` is likewise not a parameter: :meth:`AgentSession.submit` derives it
        (decision 3). A self-continuing extension — one whose ``turn_end`` hook spawns
        a task that calls this method, whose turn fires ``turn_end`` again — climbs one
        per link because the spawned task inherits the turn's
        :data:`~tau_agent_core.submission.DRIVING_SUBMISSION_DEPTH`, and the eleventh
        link RAISES rather than looping forever. A submission delivered by a task that
        PREDATES the turn (a bus subscription loop, a timer) is not self-submission and
        stays at depth 0 however much traffic it delivers mid-turn.

        Returns:
            The :class:`~tau_agent_core.submission.SubmissionResult` — ``accepted``
            plus either this turn's ``messages`` or a ``rejection_reason``. A refusal
            is a RESULT (LSP's ``ApplyWorkspaceEditResult``), not an exception.

        Raises:
            RuntimeError: if this api is not bound to a runner bucket (no extension
                identity to submit under) — Fail-Early, via
                :meth:`_emitting_extension_name`.
            RuntimeError: if this api is not bound to an ``AgentSession`` (nothing to
                submit to).
            ValueError: if ``correlation`` carries a non-JSON value (decision 4).
        """
        submitter = self._emitting_extension_name("api.submit")
        if self._session is None:
            raise RuntimeError(
                "api.submit: this ExtensionAPI is not bound to an AgentSession, so "
                "there is no session to originate a turn on. Obtain the api from "
                "AgentSession's extension load path (each factory is handed a "
                "session-bound api)."
            )
        result: SubmissionResult = await self._session.submit(
            self._build_submission(
                text,
                multitask_strategy=multitask_strategy,
                images=images,
                correlation=correlation,
                allow_user_input=allow_user_input,
                submitter=submitter,
            )
        )
        return result

    def _build_submission(
        self,
        text: str,
        *,
        multitask_strategy: MultitaskStrategy,
        images: list[dict[str, Any]] | None,
        correlation: dict[str, Any] | None,
        allow_user_input: bool,
        submitter: str,
    ) -> Submission:
        """The record :meth:`submit` and :meth:`submit_threadsafe` both send.

        One constructor for both doors so the unforgeable fields — ``source``,
        ``submitter``, and ``expand_commands=False`` — cannot drift apart between
        them. A marshalled submission that could claim to be interactive, or smuggle
        a ``/compact`` through a bus payload, would be a hole in exactly the property
        :meth:`submit`'s docstring spends two paragraphs establishing.
        """
        return Submission(
            text=text,
            source="extension",
            submitter=submitter,
            submission_id=uuid4().hex,
            images=images,
            multitask_strategy=multitask_strategy,
            correlation=dict(correlation or {}),
            allow_user_input=allow_user_input,
        )

    def submit_threadsafe(
        self,
        text: str,
        *,
        multitask_strategy: MultitaskStrategy = "reject",
        images: list[dict[str, Any]] | None = None,
        correlation: dict[str, Any] | None = None,
        allow_user_input: bool = False,
    ) -> concurrent.futures.Future[SubmissionResult]:
        """Originate a turn from a FOREIGN loop or thread (docs/SUBMISSION-LIFECYCLE.md).

        The marshalling counterpart to :meth:`submit`, for the driver whose callback
        does not run on the session's loop: a ``paho-mqtt`` client thread, a
        ``watchdog`` filesystem observer, a WSGI/webhook request thread, a
        ``threading.Timer``. Those contexts have no event loop to ``await`` on, so
        this is **synchronous** — it hands the submission to the session's own loop
        (:meth:`AgentSession.submit_threadsafe`) and returns a
        :class:`concurrent.futures.Future` for the result.

        Which one to use is a question about the CALLBACK, not a matter of taste, and
        it is answerable: if the library delivers events by awaiting your coroutine
        on the loop the session runs on — as ``nats-py`` does, because its client was
        connected from a ``session_start`` handler running on that very loop — the
        callback is already home and :meth:`submit` is correct. If the library spawns
        its own thread, this method is the only correct call, and :meth:`submit` will
        say so by raising rather than corrupting a turn silently.

        Every unforgeability property of :meth:`submit` holds here identically —
        ``source="extension"`` and ``submitter`` come from this api's own runner
        bucket, and ``expand_commands`` stays ``False``. See :meth:`submit` for what
        each argument means; they are the same arguments.

        Returns:
            A :class:`concurrent.futures.Future` resolving to the
            :class:`~tau_agent_core.submission.SubmissionResult`. Block on it with
            ``.result(timeout=…)`` if the driver thread wants the answer; drop it for
            fire-and-forget (a failure is still surfaced through the session's
            extension-error sink, never swallowed).

        Raises:
            RuntimeError: if this api is bound to no runner bucket (no identity to
                submit under) or no ``AgentSession`` (nothing to submit to).
            RuntimeError: if the session is not bound to a running loop yet, or if
                this is called from the session's own loop — see
                :meth:`AgentSession.submit_threadsafe`, which owns both checks.
            ValueError: if ``correlation`` carries a non-JSON value (decision 4).
        """
        submitter = self._emitting_extension_name("api.submit_threadsafe")
        if self._session is None:
            raise RuntimeError(
                "api.submit_threadsafe: this ExtensionAPI is not bound to an "
                "AgentSession, so there is no session to originate a turn on. "
                "Obtain the api from AgentSession's extension load path (each "
                "factory is handed a session-bound api)."
            )
        future: concurrent.futures.Future[SubmissionResult] = self._session.submit_threadsafe(
            self._build_submission(
                text,
                multitask_strategy=multitask_strategy,
                images=images,
                correlation=correlation,
                allow_user_input=allow_user_input,
                submitter=submitter,
            )
        )
        return future

    async def emit(self, topic: str, payload: Any) -> None:
        """Publish ``payload`` on this extension's channel ``ext:<name>:<topic>`` (S52).

        Inter-extension pub/sub — a faithful port of pi's ``pi.events.emit``
        (``event-bus.ts``), adapted to τ's single notify
        :class:`~tau_agent_core.events.EventBus`. Fire-and-forget, in-RAM broadcast to
        every handler another (or the same) extension subscribed with
        ``api.on("ext:<name>:<topic>", handler)``. The channel is ALWAYS namespaced
        under this emitting extension's own name (:meth:`_emitting_extension_name`), so
        an extension can only publish under its own namespace and a subscriber gets
        unforgeable provenance — τ's discipline over pi's free-form channel strings.

        This is deliberately **NOT a backplane**: it touches neither the session tree,
        the session log, nor ``convert_to_llm``, so a custom-channel payload is **NEVER
        model-visible** (the tree is the only durable, model-visible channel — E5 §1).
        It is process-local and evaporates on restart; use :meth:`append_entry` /
        :meth:`send_message` for anything durable or model-facing.

        Dispatch is synchronous per the ``EventBus`` contract (subscribed handlers run
        when this coroutine is awaited); a handler that raises is SURFACED through the
        bus on_error path (S44), never swallowed.

        Raises:
            ValueError: if ``topic`` is not a non-empty string.
            RuntimeError: if this api is not bound to a runner bucket (no extension
                identity to namespace under) — Fail-Early, via
                :meth:`_emitting_extension_name`.
        """
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("api.emit: topic must be a non-empty string")
        channel = ext_channel(self._emitting_extension_name(), topic)
        await self._event_bus.emit_channel(channel, payload)

    def register_tool(self, definition: dict | ExtensionToolDefinition) -> None:
        """Register a tool callable by the LLM (pi ``ToolDefinition`` shape).

        Mirrors pi's ``registerTool(tool: ToolDefinition)``
        (coding-agent/src/core/extensions/types.ts:433). The definition is a
        plain dict — NOT a Pydantic/TypeBox model — carrying:

        - ``name`` (str): tool name used in LLM tool calls.
        - ``description`` (str): description sent to the LLM.
        - ``parameters`` (dict): JSON-schema dict for argument validation.
        - ``execute`` (callable): ``execute(tool_call_id, params, signal,
          on_update, ctx)`` returning an ``AgentToolResult``-shaped value
          (may be sync or async). ``ctx`` is the bound ``ExtensionContext``.

        Optional keys: ``label`` (defaults to ``name`` for UI), ``prompt_snippet``,
        ``prompt_guidelines``, ``execution_mode`` ("sequential"/"parallel").

        The dict is validated into an
        :class:`~tau_agent_core.tools.base.ExtensionToolDefinition` — the schema
        for this shape — with ``source="extension"``. Passing that model directly
        works too. Either way the registry stores the model, so every reader
        downstream sees typed attributes instead of a mapping whose keys it has to
        guess; the resolved tool is merged into the loop's tools next turn.

        Raises:
            ValueError: if a required key is missing.
            TypeError: if ``parameters`` is not a dict or ``execute`` is not callable.
        """
        # These four checks predate the model and are KEPT rather than delegated
        # to pydantic: they name the offending key in register_tool's own words,
        # and their exception types (ValueError for a missing key, TypeError for a
        # wrong one) are what callers and tests already handle. Pydantic would
        # raise ValidationError for both.
        if isinstance(definition, ExtensionToolDefinition):
            fields = definition.model_dump()
        else:
            fields = dict(definition)  # don't mutate the caller's dict
        for key in ("name", "description", "parameters", "execute"):
            if key not in fields:
                raise ValueError(f"register_tool: missing required key '{key}'")
        if not isinstance(fields["parameters"], dict):
            raise TypeError("register_tool: 'parameters' must be a JSON-schema dict")
        if not callable(fields["execute"]):
            raise TypeError("register_tool: 'execute' must be callable")

        fields["_source"] = "extension"
        resolved = ExtensionToolDefinition.model_validate(fields)
        self._registry.register_tool(resolved)
        # Attribute the tool to THIS extension for the /extensions surface (E5 §5 /
        # S34). The registry stores tools globally (by name); the per-extension
        # runner bucket is the only place that records which extension owns it.
        if self._hook_handlers is not None:
            self._hook_handlers.tools.append(resolved.name)

    def get_all_tools(self) -> list[Any]:
        """Get all registered tools.

        Returns:
            List of tool info from the registry.
        """
        return self._registry.get_all_tools()

    def set_active_tools(self, names: list[str]) -> None:
        """Enable/disable tools by name (forwards to the registry)."""
        self._registry.set_active_tools(names)

    def register_command(self, name: str, command: dict) -> None:
        """Register a slash command (forwards to the registry)."""
        self._registry.register_command(name, command)
        # Attribute the command to THIS extension for the /extensions surface (E5
        # §5 / S34); the registry stores commands globally, with no per-extension
        # source (see register_tool).
        if self._hook_handlers is not None:
            self._hook_handlers.commands.append(name)

    def register_shortcut(
        self,
        key: str,
        command: str,
        *,
        args: str = "",
        description: str | None = None,
    ) -> None:
        """Bind a key to a command in the guarded extension chord namespace (E10 §6 / S69).

        Mirrors pi's ``registerShortcut(shortcut, options)`` (types.ts:1182), adapted
        to τ's dispatch model: instead of a raw handler callable, a shortcut names a
        ``command`` an extension registered via :meth:`register_command`, dispatched
        exactly like a panel action (S68) or a typed ``/name args``. Keeping the
        binding a command name (not an opaque callable) means the SAME verb is
        reachable three ways — chord, palette, and ``/command`` — and stays runnable
        headless (a keyboard shortcut has no headless surface, but the command it
        fires does).

        ``key`` is the chord TAIL — the second key pressed after the ``ctrl+e``
        extension leader. This is the "guarded namespace": the TUI only ever binds
        extension shortcuts under that leader, never as bare global keys, so an
        extension physically cannot clobber a core binding (``ctrl+c``/``ctrl+n``/…).
        Registering ``key="g"`` for ``command="fleet_status"`` makes ``ctrl+e`` then
        ``g`` dispatch ``/fleet_status``.

        Args:
            key: The chord-tail key (e.g. ``"g"``, ``"1"``). A non-empty token with
                no whitespace.
            command: The name of the command to dispatch (a ``register_command``
                name). Not required to exist yet at registration time — an unknown
                command surfaces at dispatch (``handled=False``), like a panel action.
            args: Argument string passed to the command's handler (default ``""``),
                the same slot a typed ``/name args`` fills.
            description: Optional label for the chord menu / palette entry; falls back
                to the command's own registered description when omitted.

        Raises:
            ValueError: ``key`` or ``command`` is not a non-empty string, or ``key``
                contains whitespace (a chord tail is a single key token — Fail-Early
                rather than silently binding an unreachable key).
            TypeError: ``args`` is not a string, or ``description`` is neither a
                string nor ``None``.
        """
        if not isinstance(key, str) or not key:
            raise ValueError("register_shortcut: 'key' must be a non-empty string")
        if any(c.isspace() for c in key):
            raise ValueError(
                f"register_shortcut: 'key' {key!r} must be a single key token "
                "(no whitespace); it is the chord tail after the ctrl+e leader"
            )
        if not isinstance(command, str) or not command:
            raise ValueError("register_shortcut: 'command' must be a non-empty string")
        if not isinstance(args, str):
            raise TypeError("register_shortcut: 'args' must be a string")
        if description is not None and not isinstance(description, str):
            raise TypeError("register_shortcut: 'description' must be a string or None")

        self._registry.register_shortcut(
            key, {"command": command, "args": args, "description": description}
        )
        # Attribute the shortcut to THIS extension for the /extensions surface (E5 §5
        # / S34; shortcuts S69); the registry stores shortcuts globally by tail key,
        # with no per-extension source (see register_tool / register_command).
        if self._hook_handlers is not None:
            self._hook_handlers.shortcuts.append(key)

    def append_entry(self, custom_type: str, data: dict) -> None:
        """Persist durable, NON-message extension state onto the session tree (E6 §2 / S39).

        Appends a ``{customType, data}`` node of its own tree entry KIND
        (``customEntry``) to the authoritative session log via
        ``AgentSession._append_custom_entry``. This REPLACES the former RAM-only
        registry ``_entry_store``, which was lost on restart (G4): the entry now
        persists, survives a reload, and is readable back through ``ctx.entries()``
        (the reconstruction path S56's ``TreeStore`` builds on).

        It is deliberately NOT a message: ``ConversationTree`` never folds a
        ``customEntry`` into the loop context and ``convert_to_llm`` never sees it,
        so this is tree-as-backplane state — on the durable path, excluded from
        model input. To inject a node the model reads, use ``send_message``
        (``visible_to_model``) or ``send_user_message`` instead.

        Raises:
            RuntimeError: if no session with ``_append_custom_entry`` is bound (e.g.
                a bare ``ExtensionAPI()``). Fail-Early: raise rather than silently
                drop the entry into a RAM store that evaporates on restart.
            ValueError: propagated from ``_append_custom_entry`` when ``custom_type``
                is empty or ``data`` is not a dict.
        """
        if not hasattr(self._session, "_append_custom_entry"):
            raise RuntimeError(
                "append_entry: no session with a custom-entry log is bound "
                "(the entry would have nowhere durable to land)"
            )
        self._session._append_custom_entry(custom_type, data)

    def set_session_name(self, name: str) -> None:
        """Set the session's durable display name (pi ``setSessionName``, E9 / S64).

        Thin delegator to module-level :func:`apply_session_name` — docs/
        RPC-TIER-B.md B5 factors this body out to ONE definition shared with
        the RPC ``set_session_name`` verb, rather than each maintaining its
        own copy of the Fail-Early raise. See that function's docstring for
        the full behavior and the raise conditions.
        """
        apply_session_name(self._session, name)

    def get_session_name(self) -> str | None:
        """Read the session's current display name (pi ``getSessionName``), or
        ``None`` if never set.

        Thin delegator to module-level :func:`read_session_name` — see B5's
        note on :func:`apply_session_name` for why this is factored out.
        """
        return read_session_name(self._session)

    def send_user_message(self, content: str, deliver_as: str = "followUp") -> None:
        """Queue a user message for the agent (pi ``sendUserMessage``).

        ``deliver_as`` selects the delivery mode. The parameter stays a plain
        ``str`` so future modes stay extensible (decision 5), but the three modes
        the queue supports are validated here:

        - ``"followUp"`` (default): drains at the end of the current ``prompt()``
          and re-enters within the same call.
        - ``"nextTurn"``: queued for the next ``prompt()``.
        - ``"steer"`` (docs/SUBMISSION-LIFECYCLE.md phase 4): delivered by the
          loop running RIGHT NOW, before its next LLM call — pi's
          ``sendUserMessage(..., {deliverAs: "steer"})``. This is how a hook
          steers the turn it is itself running inside; ``ctx.submit()`` cannot do
          it (a submission from the in-flight turn's own task is refused, because
          it could never be admitted).

        Raises:
            ValueError: if ``deliver_as`` is not ``"followUp"``, ``"nextTurn"`` or
                ``"steer"``.
            RuntimeError: if no session with a message queue is bound (e.g. a bare
                ``ExtensionAPI()`` with no session). Fail-Early: raise rather than
                silently drop the message (the old ``hasattr`` no-op).
        """
        if deliver_as not in ("followUp", "nextTurn", "steer"):
            raise ValueError(
                "send_user_message: deliver_as must be 'followUp', 'nextTurn' or "
                f"'steer', got {deliver_as!r}"
            )
        if not hasattr(self._session, "_queue_message"):
            raise RuntimeError("send_user_message: no session with a message queue is bound")
        self._session._queue_message(content, deliver_as=deliver_as)

    def send_message(self, message: dict, options: dict | None = None) -> None:
        """Append a durable custom message node onto the active path (pi ``sendMessage``).

        Persists ``{customType, content, display?, details?}`` as a ``role:
        "custom"`` tree node via ``AgentSession._append_custom_message`` (E6 §2 /
        S38) — it renders in the transcript / tree and survives a reload, exactly
        like a ``before_agent_start`` injection.

        Per D-E6-1 the node is **display-only by default**. Pass
        ``options={"visible_to_model": True}`` to also feed it to the model
        (remapped custom→user on the wire); otherwise it is excluded from
        ``convert_to_llm`` and never reaches the LLM. This is intentional: it does
        NOT create a third model-visible default channel — ``before_agent_start``
        and ``send_user_message`` already serve that.

        Raises:
            RuntimeError: if no session with ``_append_custom_message`` is bound
                (e.g. a bare ``ExtensionAPI()``). Fail-Early: raise rather than
                silently drop the message (the old inert no-op called a nonexistent
                method and did nothing).
            ValueError: propagated from ``_append_custom_message`` when ``message``
                lacks ``content`` or ``customType``.
        """
        if not hasattr(self._session, "_append_custom_message"):
            raise RuntimeError(
                "send_message: no session with a custom-message log is bound "
                "(the message would have nowhere durable to land)"
            )
        self._session._append_custom_message(message, options or {})

    @property
    def ui(self) -> ExtensionUI:
        """UI methods (TUI-only, no-ops in headless mode).

        Returns:
            The ExtensionUI instance from the context.
        """
        return self._context._ui

    @property
    def config(self) -> dict[str, Any]:
        """This extension's per-run config slice (E6 §2 / S40).

        The dict sourced from ``~/.tau/config.json`` under
        ``"extensions": {"<name>": {…}}`` (keyed by this extension's file stem),
        with per-run ``--ext-config <name>.<key>=<value>`` overrides applied on
        top (CLI > config.json). An unconfigured extension reads ``{}`` — the
        extension supplies its own defaults (Fail-Early: the harness never
        fabricates a value it wasn't given). Values from config.json keep their
        JSON types; a ``--ext-config`` override is JSON-decoded when it parses
        (so ``ceiling=5.0`` → ``float``, ``paths=["a","b"]`` → ``list``) and kept
        as a plain string otherwise.
        """
        return self._config

    @property
    def context(self) -> ExtensionContext:
        """The ExtensionContext for this API."""
        return self._context
