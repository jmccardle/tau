"""τ-agent-core extension_types: Extension API surface for extensions.

Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

Components:
- ExtensionAPI: Public API exposed to extension modules
- ExtensionContext: Context passed to extension event handlers
- ExtensionUI: User interaction surfaces; painted in the TUI, emitted as JSON
  records or stderr lines headless (see :class:`ExtensionUI`)

Constraint: Extensions must not import τ-agent-core internals.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal
from uuid import uuid4

from tau_agent_core.capabilities import BUILTIN, Argument, Domain, Flow, FlowDeclaration
from tau_agent_core.commands import (
    extension_owner,
    is_qualified_command,
    qualified_command,
    split_qualified_command,
)
from tau_agent_core.compaction import estimate_context_tokens
from tau_agent_core.extension_locks import REQUEST_ENTRY_TYPE, build_request_data
from tau_agent_core.tools.base import ExtensionToolDefinition
from tau_agent_core.submission import (
    MultitaskStrategy,
    Submission,
    SubmissionResult,
    user_input_permitted,
)
from tau_llm.docs import agent_facing

if TYPE_CHECKING:
    from tau_agent_core.events import EventBus
    from tau_agent_core.extensions.registry import ExtensionRegistry
    from tau_agent_core.extensions.runner import ExtensionHandlers

_RETIRED_HOOKS: frozenset[str] = frozenset({"context"})

UNATTRIBUTED_EXTENSION = "<unattributed extension>"

EXT_CHANNEL_PREFIX = "ext:"


@agent_facing(topic="extensions")
def ext_channel(name: str, topic: str) -> str:
    """Build the namespaced ``EventBus`` channel for an extension topic (E7 §3 / S52).

    The ``ext:`` prefix keeps these channels disjoint from the ``AgentEvent``
    types the bus also carries. :meth:`ExtensionAPI.emit` derives ``name`` from
    the caller's own bucket, so an extension can only publish under its own
    namespace. A subscriber passes the result to ``api.on(...)``.

    Args:
        name: The emitting extension's file stem, the one that keys ``api.config``.
        topic: The extension's own topic name.

    Returns:
        ``ext:<name>:<topic>``.
    """
    return f"{EXT_CHANNEL_PREFIX}{name}:{topic}"


HEADLESS_DIALOG_ANSWERS: dict[str, frozenset[str]] = {
    "form": frozenset({"defaults"}),
}

RETIRED_DIALOG_ANSWERS: dict[str, str] = {
    "confirm": "a confirm field on an ask (docs/EXTENSION-LOCKS.md §8)",
    "select": "a select field on an ask (docs/EXTENSION-LOCKS.md §8)",
    "input": "a text field on an ask (docs/EXTENSION-LOCKS.md §8)",
}
"""Dialog methods that no longer exist, and what replaced each.

Named rather than silently dropped, following the precedent
``build_model_from_config`` set for the retired ``prompt_cache`` string
(docs/PROMPT-CACHING.md §5): an operator with ``--ui-defaults confirm=yes`` in a
script gets a message naming the replacement instead of a policy that parses and
answers nothing.
"""

FORM_FIELD_KINDS: frozenset[str] = frozenset({"text", "select", "multiselect", "confirm", "number"})

_FORM_EMPTY_VALUE: dict[str, Any] = {
    "text": "",
    "number": 0,
    "confirm": False,
    "multiselect": [],
}


@agent_facing(topic="extensions")
def validate_form_spec(spec: Any) -> tuple[str, list[dict[str, Any]]]:
    """Validate + normalize a ``ui.form`` spec into ``(title, fields)`` (S66).

    Shared by :meth:`ExtensionUI.form` and the TUI's ``ExtensionFormScreen``.

    Args:
        spec: ``{title?: str, fields: [field, ...]}``; each field is ``{name: str,
            kind: str, label?: str, default?: Any, options?: [str, ...]}``. A
            ``select``/``multiselect`` field must carry non-empty string ``options``.

    Returns:
        The title (default ``"Form"``) and the normalized fields: ``label``
        defaulted to ``name``, ``default``/``options`` kept when present.

    Raises:
        ValueError: a non-dict spec, a non-string title or label, an empty or
            absent ``fields`` list, a field without a non-empty string ``name``, a
            duplicate name, an unknown ``kind``, or a select/multiselect without
            valid ``options``. Nothing is silently dropped.
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


@agent_facing(topic="extensions")
def form_headless_value(field: dict[str, Any]) -> Any:
    """Give the ``form=defaults`` headless answer for one validated field (S66).

    Only reached once the user opts in via ``--ui-defaults form=defaults``; with
    no policy the form raises instead of fabricating an answer.

    Args:
        field: One field from :func:`validate_form_spec`'s normalized list.

    Returns:
        The field's declared ``default`` when present; otherwise the kind's empty
        value (``""``, ``0``, ``False``, ``[]``), or a ``select``'s first option.
    """
    if "default" in field:
        return field["default"]
    kind = field["kind"]
    if kind == "select":
        return field["options"][0]
    return _FORM_EMPTY_VALUE[kind]


@agent_facing(topic="extensions")
def validate_form_values(fields: list[dict[str, Any]], values: dict[str, Any]) -> None:
    """Check answered values against the fields :func:`validate_form_spec` returned.

    The reverse direction of the form contract: ``validate_form_spec`` says what
    may be asked, this says whether an answer is admissible. Used where a form's
    answers become durable state — an extension's config slice — rather than a
    one-shot return value.

    Args:
        fields: The normalized field list, as returned by
            :func:`validate_form_spec`.
        values: The answers, keyed by field name. Every declared field must be
            present; missing is not the same as empty and is not filled in here.

    Raises:
        ValueError: an undeclared key, a missing declared field, a value whose
            type does not match its kind, or a ``select``/``multiselect`` value
            outside its declared options. Fail-Early: nothing is coerced and
            nothing is dropped.
    """
    declared = {f["name"]: f for f in fields}
    unknown = sorted(set(values) - set(declared))
    if unknown:
        raise ValueError(f"undeclared key(s) {unknown}; the schema declares {sorted(declared)}")
    missing = sorted(set(declared) - set(values))
    if missing:
        raise ValueError(f"missing value(s) for declared field(s) {missing}")
    for name, field in declared.items():
        value = values[name]
        kind = field["kind"]
        if kind == "text" and not isinstance(value, str):
            raise ValueError(f"field {name!r} is 'text' but got {type(value).__name__}")
        if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError(f"field {name!r} is 'number' but got {type(value).__name__}")
        if kind == "confirm" and not isinstance(value, bool):
            raise ValueError(f"field {name!r} is 'confirm' but got {type(value).__name__}")
        if kind == "select" and value not in field["options"]:
            raise ValueError(f"field {name!r} value {value!r} is not one of {field['options']}")
        if kind == "multiselect":
            if not isinstance(value, list):
                raise ValueError(f"field {name!r} is 'multiselect' but got {type(value).__name__}")
            stray = [v for v in value if v not in field["options"]]
            if stray:
                raise ValueError(f"field {name!r} has value(s) {stray} outside its options")


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


@agent_facing(topic="extensions")
def validate_panel_spec(spec: Any) -> dict[str, Any]:
    """Validate + normalize a ``ui.panel`` spec into ``{title, body, actions}`` (S68).

    Shared by :meth:`ExtensionUI.panel` and the TUI's ``ExtensionPanel`` widget.

    Args:
        spec: ``{title?: str, <body>, actions?: [...]}`` where ``<body>`` is EXACTLY
            ONE of ``table`` / ``list`` / ``text`` (see :func:`_validate_panel_body`)
            and ``actions`` is the optional command-dispatch list (see
            :func:`_validate_panel_actions`).

    Returns:
        ``{"title", "body", "actions"}``; title defaults to ``"Panel"``, actions
        to ``[]``.

    Raises:
        ValueError: a non-dict spec, a non-string title, zero or several body
            keys, or any malformed body or action.
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


@agent_facing(topic="extensions")
def validate_ask_spec(spec: Any) -> dict[str, Any]:
    """Validate + normalize an ASK spec into ``{title, body, fields, actions}``.

    Reference: docs/EXTENSION-LOCKS.md §8. The panel shape with fields added —
    :func:`validate_panel_spec`'s body and actions, :func:`validate_form_spec`'s
    fields — so a head that renders a panel and a form already renders this and
    no new field vocabulary enters the tree.

    Args:
        spec: ``{title?, text|list|table?, fields?, actions}``. The body is
            optional here where a panel requires one, because a bare
            question-and-buttons ask has nothing to put in it. ``fields`` and
            ``actions`` are :func:`validate_form_spec`'s and
            :func:`validate_panel_spec`'s, unchanged.

    Returns:
        ``{"title": str, "body": dict | None, "fields": list, "actions": list}``.

    Raises:
        ValueError: everything the two validators raise, plus: no ``actions``
            (an ask with no action is a notification, and ``ui.notify`` is how
            you send one); an action carrying ``args`` (an ask action's one
            argument is the request id — §8 — so a declared one is a conflict,
            refused rather than overridden); more than one body key.
    """
    if not isinstance(spec, dict):
        raise ValueError("ask: spec must be a dict")
    title = spec.get("title", "Request")
    if not isinstance(title, str):
        raise ValueError("ask: spec['title'] must be a string")
    present = [k for k in PANEL_BODY_KINDS if k in spec]
    if len(present) > 1:
        raise ValueError(f"ask: spec carries {len(present)} bodies {present}; at most one")
    body = _validate_panel_body(present[0], spec[present[0]]) if present else None
    fields: list[dict[str, Any]] = []
    if spec.get("fields") is not None:
        _, fields = validate_form_spec({"title": title, "fields": spec["fields"]})
    actions = _validate_panel_actions(spec.get("actions"))
    if not actions:
        raise ValueError(
            "ask: spec['actions'] must be a non-empty list; an ask with no action "
            "is a notification, which is ui.notify"
        )
    for action in actions:
        if action["args"]:
            raise ValueError(
                f"ask: action {action['label']!r} declares args {action['args']!r}, but an "
                "ask action's one argument is the request id (docs/EXTENSION-LOCKS.md §8)"
            )
        del action["args"]
    return {"title": title, "body": body, "fields": fields, "actions": actions}


@agent_facing(topic="extensions")
class HeadlessDialogError(RuntimeError):
    """A UI dialog was opened with no human reachable and no explicit ``--ui-defaults`` policy.

    Raised by :meth:`ExtensionUI.form` — the one blocking dialog left after
    docs/EXTENSION-LOCKS.md §8.2 — when it has no headless-answer policy (E7 §3 /
    S48) and no human can be asked. "No human can be asked" has TWO causes, and
    this one exception covers both because the consequence is identical:

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


@agent_facing(topic="extensions")
class ExtensionUI:
    """User interaction surfaces: notify, status, panel, form.

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface"; docs/EXTENSION-LOCKS.md §8.

    Every surface here DESCRIBES itself on the headless record stream and, where
    it can act, acts through a named command. ``confirm``/``select``/``input``
    were the exception — they emitted nothing and answered only through a bound
    TUI delegate — and are gone: an extension that wants an answer declares an
    ASK (``api.request_user_action``), which every head renders and which does
    not hold the turn lock while a human thinks.

    :meth:`form` is the one blocking dialog left, and it is not the way to ask a
    human a question that gates work; it is the way a FLOW collects a missing
    argument from whoever typed the command. Headless it obeys the
    :meth:`set_headless_defaults` policy — ``form=defaults`` returns each field's
    declared default, no policy RAISES :class:`HeadlessDialogError` — because
    silently auto-filling a form nobody filled would fabricate consent.

    **A bound delegate is not enough on its own** (docs/SUBMISSION-LIFECYCLE.md,
    ``Submission.allow_user_input`` — Jupyter's ``allow_stdin``). A form reaches
    the delegate only if the submission driving the calling code permits it:
    :func:`~tau_agent_core.submission.user_input_permitted` is ``False`` for the
    whole of a turn admitted with ``allow_user_input=False``, and the form then
    takes the headless-answer route even though a delegate and a live human
    exist. That is what makes the capability per-SUBMISSION rather than
    per-process: one embedded τ can serve an interactive session and a
    cron-triggered submission at the same time, and only the latter is barred
    from opening a dialog.

    ``notify``, ``set_status`` and ``panel`` are non-blocking (no answer to
    fabricate), so they are NOT gated by ``allow_user_input``, which is about
    asking a human rather than telling one.

    Attributes:
        _tui_delegate: TUI delegate object (set via set_ui_delegate()).
        _headless_policy: validated ``{method: token}`` headless-answer map.
    """

    def __init__(self, headless_policy: dict[str, str] | None = None) -> None:
        """Initialize ExtensionUI.

        Args:
            headless_policy: Optional ``{method: token}`` headless-answer map
                (validated via :meth:`set_headless_defaults`). Defaults to no
                policy → a headless form raises (S48).
        """
        self._tui_delegate: Any | None = None
        self._headless_policy: dict[str, str] = {}
        self._record_sink: Callable[[dict[str, Any]], None] | None = None
        if headless_policy:
            self.set_headless_defaults(headless_policy)

    def set_headless_defaults(self, policy: dict[str, str]) -> None:
        """Set (replace) the headless-answer policy, validating every entry (S48).

        Args:
            policy: Dialog method to answer token. Keys must be in
                :data:`HEADLESS_DIALOG_ANSWERS`; each token must be one of that
                method's answers, matched case-insensitively and stored lowercased.

        Raises:
            ValueError: an unknown or retired method, or a token the method does
                not allow; a ``--ui-defaults`` typo surfaces here, not mid-run.
        """
        validated: dict[str, str] = {}
        for method, token in policy.items():
            allowed = HEADLESS_DIALOG_ANSWERS.get(method)
            if allowed is None:
                replacement = RETIRED_DIALOG_ANSWERS.get(method)
                if replacement is not None:
                    raise ValueError(
                        f"ui-defaults: ui.{method}() no longer exists; use {replacement}. "
                        "An ask is answered by whoever is attached, not by a policy token, "
                        "and it does not hold the session while nobody answers."
                    )
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
        if self._tui_delegate is None or not user_input_permitted():
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
        return self._tui_delegate is not None

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

    async def form(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Show a declarative form and return ``{field_name: value}`` (E10 §6 / S66).

        The spec is validated before any UI is shown, in every mode. Routing:

        - a bound delegate and a driving submission that permits asking a human
          (:meth:`_human_delegate`) → the TUI's ``ExtensionFormScreen``;
        - otherwise, with a record sink installed (``--mode json``), one
          ``{"type": "extension", "kind": "form", …}`` record is emitted first;
        - then the headless policy: ``form=defaults`` answers each field with
          :func:`form_headless_value`, and no policy raises.

        Args:
            spec: The form spec :func:`validate_form_spec` accepts; ``kind`` is one
                of :data:`FORM_FIELD_KINDS`.

        Returns:
            Each field name mapped to its answer, or ``None`` when a TUI user
            cancels (never a fabricated set of answers). The headless ``defaults``
            answer is always a dict.

        Raises:
            ValueError: ``spec`` is malformed.
            HeadlessDialogError: no human may be asked and there is no ``form``
                policy.
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
        on the ``--mode json`` headless path only; the TUI and ``--mode text`` keep
        the delegate / stderr behaviour.

        Args:
            sink: Writes one record (the family beside the ``AgentEvent`` set) as
                one stdout line; ``None`` restores the plain stderr output.
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

        Args:
            message: The notification text.
            level: Severity label passed to the delegate or written into the record
                or stderr line, e.g. ``"info"``.
            source: The originating extension's identity when the caller knows it
                (the S44 error-surface path passes the failing extension's path).
                A plain ``api.ui.notify(...)`` cannot supply one, because every
                extension shares the session's one :class:`ExtensionUI`; the record
                then carries ``"extension": null`` rather than a fabricated name.
        """
        if self._tui_delegate is not None:
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

        Re-calling a key updates its slot in place. Non-blocking, so it routes
        like :meth:`notify`: the delegate's status strip in the TUI; one
        ``{"type": "extension", "kind": "status", …}`` record under ``--mode json``
        (a cleared slot carries ``"text": null``); otherwise a stderr line.

        Args:
            key: The slot to update or clear.
            text: The slot's new text; ``None`` clears it.
            source: The originating extension's identity, or ``None``; same
                contract as :meth:`notify`'s.

        Raises:
            ValueError: ``key`` is not a non-empty string.
        """
        if not isinstance(key, str) or not key:
            raise ValueError("ui.set_status: key must be a non-empty string")
        if self._tui_delegate is not None:
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
        """Show, update, or clear a persistent keyed panel (E10 §6 / S68).

        Pressing an action dispatches its ``command`` as a registered command. The
        spec is validated before any UI is shown, in every mode. Non-blocking, so
        it routes like :meth:`notify`: the delegate's panel host in the TUI; one
        ``{"type": "extension", "kind": "panel", "key": …, "spec": …}`` record
        under ``--mode json`` (``spec`` normalized, or ``null`` on clear), whose
        actions cannot be pressed; otherwise the title on stderr.

        Args:
            key: The panel; re-calling the same key updates it in place.
            spec: ``{title?, <body>, actions?}`` as :func:`validate_panel_spec`
                accepts; ``None`` clears the panel.
            source: The originating extension's identity, or ``None``; same
                contract as :meth:`notify`'s.

        Raises:
            ValueError: ``key`` is not a non-empty string, or ``spec`` is malformed
                (:func:`validate_panel_spec`).
        """
        if not isinstance(key, str) or not key:
            raise ValueError("ui.panel: key must be a non-empty string")
        normalized = None if spec is None else validate_panel_spec(spec)
        if self._tui_delegate is not None:
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
        here would be a duplicate. With no sink installed this is a no-op.

        Args:
            extension: The vetoing extension's path, or ``None`` if unattributed.
            tool: The blocked tool's name.
            reason: The veto reason the hook gave.
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
        <path|null>, "constraints": <summary>}``. Like :meth:`emit_veto` it never
        touches the TUI delegate or stderr, and with no sink it is a no-op.

        Args:
            summary: :meth:`DecodeConstraints.describe`'s output, ``{"kind":
                "choices"|"json_schema"|"grammar", ...}``. A ``{"kind": "none"}``
                summary is dropped rather than emitted as a placeholder record.
            source: The calling extension's path, or ``None`` if unattributed.
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


@agent_facing(topic="extensions")
@dataclass
class BranchResult:
    """What a sub-agent came back with (``ctx.spawn_branch``, ``AgentSession.spawn``).

    A sub-agent that failed returns ``ok=False`` rather than raising: failure
    containment is the design (§9.2/5), so one bad evaluator in a fan-out does not
    kill the turn that spawned it. An unchecked ``ok`` turns a failure into an
    empty-but-successful-looking answer.

    Attributes:
        ok: Whether the sub-agent's turn was admitted and finished without
            raising. Read this first.
        cursor_id: The cursor its events carried; closed by the time this returns.
        label: The cursor's label: the ``label`` given, else the prompt's first
            60 characters.
        leaf: The sub-agent's final entry id, or ``None`` if it appended nothing;
            the handle to read the verdict back or ``ctx.summarize_branch`` it.
        messages: The sub-agent turn's messages; empty when ``ok`` is false.
        error: Why it failed, or ``None`` when ``ok``.
    """

    ok: bool
    cursor_id: str
    label: str
    leaf: str | None
    messages: list[dict[str, Any]]
    error: str | None


@agent_facing(topic="extensions")
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

        A session's context is built with every default (``ExtensionAPI`` makes
        it); only ``_session`` is bound afterwards.

        Args:
            cwd: Returned by :attr:`cwd` as given; nothing resolves it.
            session_manager: Returned by :attr:`session_manager`; ``shutdown``
                forwards to it when it has a ``shutdown()``.
            signal: The abort signal :attr:`signal` returns while no session is
                bound.
            is_idle: Returned by :attr:`is_idle` as given; nothing updates it.
        """
        self._cwd = cwd
        self._session_manager = session_manager
        self._signal = signal
        self._is_idle = is_idle
        self._ui = ExtensionUI()
        self._shutdown_requested = False
        self._session: Any | None = None

    @property
    def cwd(self) -> str:
        """The ``cwd`` given at construction, ``"."`` for a session-built context.

        Not the session's working directory: no session path passes one.
        """
        return self._cwd

    @property
    def session_manager(self) -> Any:
        """The ``session_manager`` given at construction; ``None`` in a session."""
        return self._session_manager

    @property
    def signal(self) -> Any | None:
        """The abort signal of the turn this handler runs in.

        With a session bound, that is the running turn's cursor
        (``TURN_CURSOR``), so two cursors' concurrent turns each see their own
        (docs/CURSORS.md §7). Without one, the signal this context was built with.
        """
        if self._session is not None:
            return self._session.acting_cursor.abort_signal
        return self._signal

    @property
    def is_idle(self) -> bool:
        """The ``is_idle`` given at construction (default ``True``).

        Never updated, so it does not track whether a turn is running.
        """
        return self._is_idle

    @property
    def ui(self) -> ExtensionUI:
        """The session's one :class:`ExtensionUI`, the same object as ``api.ui``.

        A hook or command handler receives ``ctx``, never the api, so this is its
        route to ``notify``, ``set_status``, ``panel`` and ``form``. A question
        that gates work goes through :meth:`ExtensionAPI.request_user_action`.
        """
        return self._ui

    def abort(self) -> None:
        """Trip :attr:`signal`, the running turn's, if there is one."""
        signal = self.signal
        if signal:
            signal.abort()

    def shutdown(self) -> None:
        """Request a shutdown by setting :attr:`shutdown_requested`; idempotent.

        Also calls ``session_manager.shutdown()`` when one with that method was
        given at construction.
        """
        self._shutdown_requested = True
        if self._session_manager is not None and hasattr(self._session_manager, "shutdown"):
            self._session_manager.shutdown()

    @property
    def shutdown_requested(self) -> bool:
        """True once `shutdown()` has been called on this context (P3)."""
        return self._shutdown_requested

    def get_context_usage(self) -> dict[str, Any] | None:
        """Measure how full the active model's context window is.

        Returns:
            ``{tokens, context_window, percent}``: ``tokens`` is
            ``estimate_context_tokens`` over the session's messages, the same
            estimate that drives auto-compaction, and ``percent`` is
            ``tokens / context_window * 100``. ``None`` when the model has no
            positive ``context_window`` — unknown, not a fabricated zero.

        Raises:
            RuntimeError: no session is bound.
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

    def get_model(self) -> dict[str, Any]:
        """Read the active model (S45), via :meth:`AgentSession.get_model`.

        Returns:
            ``{id, provider, context_window}``, so an extension never reaches the
            private ``ctx._session._model``.

        Raises:
            RuntimeError: no session is bound.
        """
        model: dict[str, Any] = self._require_session().get_model()
        return model

    def set_model(self, name: str) -> dict[str, Any]:
        """Switch the acting cursor's model by NAME, effective on its next turn (S45).

        Delegates to :meth:`AgentSession.set_model`: at the head cursor that is
        the session's model, at any other cursor that cursor's frame.

        Args:
            name: The config model name to resolve and switch to.

        Returns:
            The new :meth:`get_model` projection.

        Raises:
            RuntimeError: No session is bound, or the session has no model resolver.
            TypeError: The resolver returned something other than a ``Model``.
            Exception: Whatever the resolver raises for an unknown ``name``,
                unchanged.
        """
        model: dict[str, Any] = self._require_session().set_model(name)
        return model

    def get_usage(self) -> dict[str, Any] | None:
        """Read the most recent completion's token usage (S45, anchor G14).

        Delegates to :meth:`AgentSession.get_usage`. Read this from a
        ``message_end`` handler instead of pulling ``event.message["usage"]``.

        Returns:
            A copy of the last completion's ``usage`` dict, or ``None`` when no
            completion has landed yet.

        Raises:
            RuntimeError: no session is bound.
        """
        usage: dict[str, Any] | None = self._require_session().get_usage()
        return usage

    async def prompt(self, text: str) -> list[dict[str, Any]]:
        """DEPRECATED alias for :meth:`ExtensionAPI.submit` — use ``api.submit`` instead.

        Submits with ``multitask_strategy="enqueue"`` (waits for an in-flight
        turn), ``source="extension"`` and the submitter
        :data:`UNATTRIBUTED_EXTENSION`, because the shared context cannot know
        which extension called (docs/SUBMISSION-LIFECYCLE.md phase 5).

        Args:
            text: The prompt to run as one agent turn on the bound session.

        Returns:
            This turn's messages only; there is no refusal to see under
            ``"enqueue"``.

        Raises:
            RuntimeError: No session is bound, or :meth:`AgentSession.submit`
                refused the call: reentrant from a hook on the in-flight turn's
                own task (it would deadlock), off the session's loop, or past
                ``MAX_SUBMISSION_DEPTH``.
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

    def _require_session(self) -> Any:
        """The bound ``AgentSession``, or raise (Fail-Early, no silent no-op)."""
        if self._session is None:
            raise RuntimeError("session-control op: no session bound to ExtensionContext")
        return self._session

    async def compact(self, custom_instructions: str | None = None, defer: bool = False) -> Any:
        """Compact the active conversation (delegates to ``AgentSession.compact``).

        Summarizes the active path's prefix via the LLM and APPENDS a compaction
        entry, so the prefix drops out of future context at read time.

        Args:
            custom_instructions: Optional extra focus for the summary.
            defer: Record the intent instead of compacting now (S20 / decision 3).
                A tool calling this mid-turn cannot compact under the live agent
                loop; the intent is applied exactly once at the tail of
                ``prompt()``, the same site as auto-compaction.

        Returns:
            The ``CompactionResult``, or ``None`` when there was nothing to
            compact or ``defer`` was set.

        Raises:
            RuntimeError: No session is bound.
            CompactionError: Summary generation failed (not when deferred).
        """
        session = self._require_session()
        if defer:
            session._defer_compact(custom_instructions=custom_instructions)
            return None
        return await session.compact(custom_instructions=custom_instructions)

    @property
    def cursor(self) -> Any:
        """The :class:`~tau_agent_core.cursor.Cursor` this handler acts on.

        Inside a turn, that turn's cursor; outside one, the head's. ``ctx.cursor.leaf``
        is "where am I": a cursor is not durable, so it cannot be recovered from
        :meth:`entries` (docs/CURSORS.md §4, §7).
        """
        return self._require_session().acting_cursor

    def entries(self) -> list[dict[str, Any]]:
        """Read the bound session log's raw, append-only entries (all kinds).

        Returns:
            ``SessionLog.entries()``, the list a ``ConversationTree`` folds into
            context; a copy, so mutating it does not touch the log.

        Raises:
            RuntimeError: no session is bound.
        """
        entries: list[dict[str, Any]] = self._require_session().session_log.entries()
        return entries

    def resolve_model(self, model: Any = None) -> Any:
        """Resolve ``model`` to a ``tau_llm.Model``.

        Routing names through the config registry lets an extension's model be
        configured in its own config slice — e.g. ``"extensions":
        {"retrieval_review": {"model": "local-llm-small"}}`` — with no
        extension-private client plumbing.

        Args:
            model: ``None`` for the session's current model; a name, looked up by
                the resolver bound on the session (the config ``models`` registry
                the TUI picks from); or a ``Model``, used as-is.

        Returns:
            The resolved ``Model``.

        Raises:
            RuntimeError: no session is bound, or a name was given and the session
                has no model resolver.
        """
        session = self._require_session()
        if model is None:
            return session._model
        if isinstance(model, str):
            resolver = session.model_resolver
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

        Touches neither the entry log nor the cursor, so it is safe under
        ``asyncio.gather`` at any fan-out; its usage is banked on the session's
        side ledger. No retries. With ``constraints``::

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
            RuntimeError: No session is bound; ``model`` is a name and the session
                has no resolver; the completion errored or was aborted; or it
                stopped at the token limit (``stop_reason="length"``), which is a
                truncated prefix rather than an answer.
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

        try:
            response = await resolved_complete(
                resolved, {"messages": messages}, options=options or None
            )
        except CompletionFailed as exc:
            session.record_side_usage(usage_of(exc.response))
            raise RuntimeError(f"ctx.complete() failed: {exc.detail}") from exc

        session.record_side_usage(usage_of(response))

        stop_reason = getattr(response, "stop_reason", None)

        if stop_reason == "length":
            raise RuntimeError(
                "ctx.complete() hit the token limit (stop_reason='length'): the answer is "
                "a truncated PREFIX, not a complete response. Raise Model.max_tokens."
            )

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
        """Run :meth:`complete` and return the response's text (the common case).

        Args:
            messages: τ message dicts, as for :meth:`complete`.
            model: A name, a ``Model``, or ``None`` for the session's, as for
                :meth:`complete`.
            constraints: An optional ``tau_llm.DecodeConstraints``.
            api_key: Overrides the session's key.

        Returns:
            The text blocks joined, VERBATIM: under a constraint whitespace is part
            of the constrained value (``grammar.fixed("yes ")`` forces the trailing
            space), so stripping would yield a string the constraint never produced.

        Raises:
            RuntimeError: the response's text is empty or whitespace-only, or
                :meth:`complete` failed.
            ConstraintViolation: the constraint did not hold.
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
        hooks: bool = False,
    ) -> "BranchResult":
        """Run a tool-using sub-agent on its own cursor in THIS conversation (C2/W14).

        Delegates to :meth:`AgentSession.spawn` (docs/CURSORS.md §6).

        Args:
            parent_id: The entry the sub-agent's cursor opens at, or ``None`` for
                before the root.
            prompt: The sub-agent's task, sent as its one submission.
            tools: A required hard allowlist of tool names; ``[]`` means none.
            model: A name, a ``Model``, or ``None`` for the session's.
            max_turns: Cap on the sub-agent's loop turns, or ``None`` for no cap.
            label: The cursor's label; defaults to the prompt's first 60 characters.
            system_prompt: Replaces the session's system prompt for this branch.
            hooks: Whether extension hooks fire during the sub-agent's turn.

        Returns:
            A :class:`BranchResult`. **Check ``ok``** — a failed sub-agent returns a
            result, it does not raise.

        Raises:
            RuntimeError: No session is bound, or ``model`` is a name and the
                session has no resolver.
            ValueError: a tool is not one the spawning turn offers, or ``parent_id``
                names no entry.
        """
        session = self._require_session()
        result: BranchResult = await session.spawn(
            parent_id,
            prompt,
            tools=tools,
            model=None if model is None else self.resolve_model(model),
            max_turns=max_turns,
            label=label,
            system_prompt=system_prompt,
            hooks=hooks,
        )
        return result

    async def summarize_branch(
        self, from_entry: str, custom_instructions: str | None = None
    ) -> list[dict[str, Any]]:
        """Summarize the subtree at ``from_entry`` and splice it onto the active path.

        Binds :func:`tau_agent_core.tree_ops.summarize_and_navigate` to the session's
        head cursor, model and key, and banks the tokens the summarizer spent. It
        APPENDs a ``branch_summary`` entry parented at ``from_entry``; the abandoned
        children drop out of context via the ``parentId`` walk.

        Args:
            from_entry: The entry whose subtree is summarized.
            custom_instructions: Optional extra focus for the summary.

        Returns:
            The re-rendered active-path messages (``ConversationTree.context_for``).

        Raises:
            RuntimeError: No session is bound.
            ValueError: ``from_entry`` names no entry (checked before the model
                call), or the summarizer returned nothing usable.
        """
        from tau_agent_core.tree_ops import summarize_and_navigate

        session = self._require_session()
        messages, summary_usage = await summarize_and_navigate(
            session.cursor,
            from_entry,
            session._model,
            api_key=session._api_key,
            custom_instructions=custom_instructions,
        )
        session.record_side_usage(summary_usage)
        return messages

    async def navigate(
        self,
        target_id: str | None,
        summarize: bool = False,
        custom_instructions: str | None = None,
    ) -> list[dict[str, Any]]:
        """Move the bound session's cursor to ``target_id`` and return the new context.

        Args:
            target_id: The entry to move onto, or ``None`` for before the root.
            summarize: ``False`` writes nothing; the branch left behind drops out of
                context by ancestry. ``True`` delegates to :meth:`summarize_branch`,
                unless the cursor is already at ``target_id``.
            custom_instructions: Passed to :meth:`summarize_branch`.

        Returns:
            The context at the new position (``ConversationTree.context_for``).

        Raises:
            RuntimeError: No session is bound.
            ValueError: ``target_id`` names no entry, ``summarize`` with a ``None``
                ``target_id`` while the cursor is elsewhere, or (with ``summarize``)
                the summarizer returned nothing usable.
        """
        from tau_agent_core.tree_ops import navigate as _navigate

        session = self._require_session()
        if summarize and target_id != session.cursor.leaf:
            if target_id is None:
                raise ValueError("navigate(summarize=True) requires a target_id to summarize")
            return await self.summarize_branch(target_id, custom_instructions=custom_instructions)
        return _navigate(session.cursor, target_id)

    async def fork(
        self,
        entry_id: str | None = None,
        mode: Literal["in_place", "export"] = "in_place",
        defer: bool = False,
    ) -> Any:
        """Fork the conversation in place or into a new session file.

        Args:
            entry_id: Where to fork. In place, the entry to move onto (``None`` is
                before the root); for export, the entry whose path is copied
                (``None`` copies the whole session).
            mode: ``"in_place"`` moves the session's head cursor so the next turn
                appends a sibling branch there. ``"export"`` copies the session into
                a NEW file via ``Session.fork``, never touching the source.
            defer: Record the intent instead of forking now; it is applied once at
                the tail of ``prompt()``. A tool calling this mid-turn needs it.

        Returns:
            In place, the context at the new position; for export, the new session
            file's path as a string; ``None`` when ``defer`` is set.

        Raises:
            RuntimeError: no session is bound, or export from a log that is not
                file-backed.
            ValueError: an unknown ``mode``, or an ``entry_id`` that names no
                entry.
        """
        session = self._require_session()
        if defer:
            session._defer_fork(entry_id=entry_id, mode=mode)
            return None
        log = session.session_log
        if mode == "in_place":
            session.cursor.move(entry_id)
            return session.cursor.context()
        if mode == "export":
            fork_classmethod = getattr(type(log), "fork", None)
            if fork_classmethod is None:
                raise RuntimeError(
                    "fork(mode='export'): the bound session log is not file-backed and "
                    "cannot be exported to a new file"
                )
            cwd = getattr(log, "cwd", None) or self._cwd
            forked = fork_classmethod(log, cwd, at=entry_id)
            return str(forked.path)
        raise ValueError(f"fork: unknown mode {mode!r} (expected 'in_place' or 'export')")

    def set_ui_delegate(self, delegate: Any) -> None:
        """Bind the head's delegate, which is what makes ``ui.interactive`` true.

        There is no second flag: a bound delegate IS the live surface, since
        docs/EXTENSION-LOCKS.md §8.2 removed ``ExtensionUI._mode`` — with every
        surface emitting a record, "which mode is this" and "is a delegate
        bound" were the same question asked twice.

        Args:
            delegate: An object with ``notify``, ``set_status``, ``panel`` and
                ``form``.
        """
        self._ui._tui_delegate = delegate

    def set_record_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        """Install the headless JSON record sink on the shared UI (E7 §3 / S49).

        The headless ``--mode json`` path calls this (via
        :meth:`AgentSession.set_extension_record_sink`) so every extension's UI
        calls become records on the JSON stream instead of stderr lines (anchor G10).

        Args:
            sink: As for :meth:`ExtensionUI.set_record_sink`; ``None`` clears it.
        """
        self._ui.set_record_sink(sink)

    def emit_veto_record(self, *, extension: str | None, tool: str, reason: str) -> None:
        """Emit a blocked-tool VETO record via the shared UI's record sink (E7 §3 / S50).

        Thin pass-through to :meth:`ExtensionUI.emit_veto`, reached from the agent
        loop through :class:`ExtensionRunner` when a `tool_call` hook vetoes a call
        (anchor G11). A no-op unless the ``--mode json`` path installed a sink.

        Args:
            extension: The vetoing extension's path, or ``None`` if unattributed.
            tool: The blocked tool's name.
            reason: The veto reason the hook gave.
        """
        self._ui.emit_veto(extension=extension, tool=tool, reason=reason)

    def set_headless_ui_defaults(self, policy: dict[str, str]) -> None:
        """Set the headless dialog-answer policy on the shared UI (E7 §3 / S48).

        The frontends call this (via :meth:`AgentSession.set_headless_ui_defaults`)
        so a headless dialog auto-answers only when the user opted in.

        Args:
            policy: The resolved ``--ui-defaults`` / config policy, as for
                :meth:`ExtensionUI.set_headless_defaults`.

        Raises:
            ValueError: an unknown method or token; the caller surfaces it as a
                CLI error.
        """
        self._ui.set_headless_defaults(policy)


@agent_facing(topic="extensions")
async def apply_session_name(session: Any, name: str) -> None:
    """Append a ``session_info`` entry naming ``session``, at the cursor acting now.

    The model never sees a ``session_info``; the picker and the TUI title read
    the newest one (:func:`~tau_agent_core.session_log.session_name`). Whether
    the name outlives the process is the store's property: a host that must
    promise durability checks first (``rpc.commands.require_durable_session``).

    Args:
        session: The ``AgentSession`` to name.
        name: The new name; must be non-empty.

    Raises:
        ValueError: ``name`` is empty.
    """
    if not name:
        raise ValueError("set_session_name: name must be a non-empty string")
    await session.acting_cursor.append("session_info", name=name)


@agent_facing(topic="extensions")
def read_session_name(session: Any) -> str | None:
    """Read ``session``'s name (:func:`~tau_agent_core.session_log.session_name`).

    Args:
        session: The ``AgentSession`` whose log is read.

    Returns:
        The newest ``session_info`` name, or ``None`` if never named or the newest
        name is empty.
    """
    from tau_agent_core.session_log import session_name

    return session_name(session.session_log.entries())


@agent_facing(topic="extensions")
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
            registry: Where tools, commands and shortcuts land; ``None`` makes a
                private, empty one.
            event_bus: The notify bus :meth:`on` and :meth:`emit` use; ``None``
                makes a private one.
            context: The shared context; ``None`` makes a fresh one. Either way
                ``session`` is bound onto it.
            session: The ``AgentSession``, or ``None`` for an unbound api whose
                session-backed methods raise.
            hook_handlers: This extension's own runner bucket. ``None`` gives the
                api no identity: hook registration, :meth:`emit`, :meth:`submit`
                and command registration then raise.
            config: This extension's config slice (:attr:`config`); copied, and
                ``None`` reads as ``{}``.
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
        self._hook_handlers = hook_handlers
        self._config: dict[str, Any] = dict(config or {})
        self._context._session = session

    def on(self, event: str, handler: Callable) -> Callable[[], None]:
        """Subscribe to an event, routed by kind (S24).

        See docs/extensions.md, "Hook and event vocabulary".

        The mutating hooks (``ExtensionRunner.HOOK_EVENTS``: ``tool_call``,
        ``tool_result``, ``before_agent_start``, ``input``, ``turn_end``,
        ``user_turn_end``) and the lifecycle hooks (``LIFECYCLE_EVENTS``:
        ``session_start``, ``session_shutdown``) go to this extension's runner
        bucket, where handler errors are surfaced. Everything else — notify
        events, ``"all"``, and ``ext:<name>:<topic>`` channels from :meth:`emit` —
        goes to the notify ``EventBus``.

        ``turn_end`` fires once per agent-loop turn (per completion);
        ``user_turn_end`` fires once per utterance, after the loop, the followUp
        drain and auto-compaction. Either handler may return ``{message}`` for a
        durable append. The notify-grade ``turn_end`` ``AgentEvent`` still reaches
        ``api.on("all", …)``.

        Args:
            event: Event type (e.g., 'agent_start', 'tool_call', 'all').
            handler: Callable that receives the event (an ``AgentEvent`` for notify
                events; a ``(event_dict, ctx)`` pair for hook events).

        Returns:
            An unsubscribe function.

        Raises:
            RuntimeError: the retired ``context`` hook (E5 §3.2 / S30), or a
                runner-routed hook on an api with no runner bucket
                (``hook_handlers is None``), where it could never fire.
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
        """This api's own extension name, the identity :meth:`emit` and :meth:`submit` act under.

        Args:
            op: The calling method, for the error message only.

        Returns:
            The runner bucket path's stem, the same stem that keys :attr:`config`.

        Raises:
            RuntimeError: No runner bucket is bound, so there is no identity.
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

        Goes through :meth:`AgentSession.submit`, the one admission point. Not
        parameters, so not forgeable: ``source="extension"``, ``submitter`` (this
        extension's own name), ``expand_commands=False`` (a ``/compact`` in a bus
        payload stays text) and ``depth`` (derived; a self-continuing chain raises
        past ``MAX_SUBMISSION_DEPTH``). Call from the session's own loop; a
        thread or foreign loop uses :meth:`submit_threadsafe`.

        Args:
            text: The utterance to run a turn on.
            multitask_strategy: Policy against an in-flight turn, as
                :meth:`AgentSession.submit` defines them. ``"reject"`` by default:
                a busy session returns ``accepted=False``, never a silent queue.
            images: Optional image dicts for a multimodal submission.
            correlation: JSON-safe origin detail (bus subject, cron id, request id)
                carried onto every event this turn emits.
            allow_user_input: Whether code under this submission may prompt a
                human (Jupyter's ``allow_stdin``). ``False`` by default: for the
                whole turn :meth:`ExtensionUI.form` skips the TUI delegate and
                takes the headless policy, raising :class:`HeadlessDialogError`
                without one.

        Returns:
            The :class:`~tau_agent_core.submission.SubmissionResult`: ``accepted``
            plus this turn's ``messages`` or a ``rejection_reason``. A refusal is a
            result, not an exception.

        Raises:
            RuntimeError: No runner bucket (no identity) or no ``AgentSession`` is
                bound; or :meth:`AgentSession.submit` refused the call (off its
                loop, reentrant on the in-flight turn's task, or past
                ``MAX_SUBMISSION_DEPTH``).
            NotImplementedError: An unknown ``multitask_strategy``.
            ValueError: ``correlation`` carries a non-JSON value.
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

        One constructor, so the unforgeable fields cannot drift between the two.

        Raises:
            ValueError: ``correlation`` carries a non-JSON value.
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

        Synchronous: hands the submission to the session's own loop
        (:meth:`AgentSession.submit_threadsafe`) and returns a future. Use it when
        the callback runs on a thread the library spawned (``paho-mqtt``,
        ``watchdog``, a WSGI thread, ``threading.Timer``); a callback awaited on
        the session's loop (``nats-py`` connected from ``session_start``) uses
        :meth:`submit`. The fixed fields are :meth:`submit`'s.

        Args:
            text: The utterance to run a turn on.
            multitask_strategy: Policy against an in-flight turn; ``"reject"`` by
                default, as for :meth:`submit`.
            images: Optional image dicts for a multimodal submission.
            correlation: JSON-safe origin detail carried onto every event of the
                turn, as for :meth:`submit`.
            allow_user_input: Whether code under this submission may prompt a
                human; ``False`` by default, as for :meth:`submit`.

        Returns:
            A :class:`concurrent.futures.Future` resolving to the
            :class:`~tau_agent_core.submission.SubmissionResult`. Block on it with
            ``.result(timeout=…)`` if the driver thread wants the answer; drop it for
            fire-and-forget (a failure is still surfaced through the session's
            extension-error sink, never swallowed).

        Raises:
            RuntimeError: No runner bucket or no ``AgentSession`` is bound; the
                session is not bound to a running loop yet; or this is called
                from the session's own loop, where blocking on the future
                deadlocks.
            ValueError: ``correlation`` carries a non-JSON value.
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

        In-RAM broadcast on the notify :class:`~tau_agent_core.events.EventBus` to
        every ``api.on("ext:<name>:<topic>", handler)``; ``<name>`` is always this
        extension's own. Never written to the tree, so never model-visible and
        gone on restart — durable state uses :meth:`append_entry` or
        :meth:`send_message`. Handlers run while this is awaited; one that raises
        is surfaced through the bus's on_error path (S44), not here.

        Args:
            topic: The topic, the last segment of the channel name.
            payload: Delivered to each subscribed handler as-is.

        Raises:
            ValueError: ``topic`` is empty or whitespace.
            RuntimeError: No runner bucket is bound, so there is no name to
                namespace under.
        """
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("api.emit: topic must be a non-empty string")
        channel = ext_channel(self._emitting_extension_name(), topic)
        await self._event_bus.emit_channel(channel, payload)

    def register_tool(self, definition: dict | ExtensionToolDefinition) -> None:
        """Register a tool callable by the LLM.

        The definition is validated into an
        :class:`~tau_agent_core.tools.base.ExtensionToolDefinition` with
        ``source="extension"`` and stored in the registry; the tool joins the
        loop's tools next turn.

        Args:
            definition: A dict or an ``ExtensionToolDefinition``. Required keys:
                ``name``; ``description`` (sent to the LLM); ``parameters`` (a
                JSON-schema dict); ``execute(tool_call_id, params, signal,
                on_update, ctx)``, sync or async, returning an
                ``AgentToolResult``-shaped value, with ``ctx`` the bound
                ``ExtensionContext``. Optional: ``label`` (defaults to ``name``),
                ``prompt_snippet``, ``prompt_guidelines``, ``execution_mode``
                (``"sequential"``/``"parallel"``). A dict is not mutated.

        Raises:
            ValueError: if a required key is missing.
            TypeError: if ``parameters`` is not a dict or ``execute`` is not callable.
        """
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
        if self._hook_handlers is not None:
            self._hook_handlers.tools.append(resolved.name)

    def get_all_tools(self) -> list[Any]:
        """List every registered tool, active or not.

        Returns:
            One ``ToolInfo`` (name, description, parameters, source) per tool in
            the registry, built-in and extension alike.
        """
        return self._registry.get_all_tools()

    def set_active_tools(self, names: list[str]) -> None:
        """Enable/disable tools by name (forwards to the registry).

        Args:
            names: The registered tools to keep active; every other registered
                tool is filtered out of ``get_active_tools``. Replaces any earlier set.
        """
        self._registry.set_active_tools(names)

    @property
    def extension_name(self) -> str:
        """This extension's identity (docs/EXTENSION-NAMESPACE.md).

        The owner half of every qualified command name, normalized by
        :func:`~tau_agent_core.commands.extension_owner` — a file extension's stem,
        an inline factory's own name.

        Raises:
            RuntimeError: This api is not bound to a runner bucket, so it has no
                identity to qualify a name with. Fail-Early, and the same refusal
                :meth:`on` makes for the same reason (S24).
        """
        if self._hook_handlers is None:
            raise RuntimeError(
                "this ExtensionAPI is not bound to a loaded extension, so it has no name. "
                "Commands are registered under ext:<extension>.<command>, which needs one. "
                "Construct it with hook_handlers=ExtensionHandlers(path=…)."
            )
        return extension_owner(self._hook_handlers.path)

    def register_command(self, name: str, command: dict) -> str | None:
        """Register a slash command, and try to claim ``name`` for it.

        Two registrations, because there are two names (docs/EXTENSION-NAMESPACE.md).
        The command is installed in the private registry at
        ``ext:<extension>.<name>``, where nothing can displace it, and then ``name``
        itself is claimed. Claiming is first-wins: if another extension already holds
        it, or a human pinned it elsewhere, this one keeps only its qualified name.

        Args:
            name: The command word a reader types after the ``/``.
            command: ``{"description": str, "handler": callable, "args": str?}``.

        Returns:
            ``None`` when ``/name`` now runs this command. Otherwise the qualified name
            that holds it instead — pass it to :meth:`run_command` to wrap, chain or
            defer to whatever got there first.

        Raises:
            RuntimeError: No runner bucket is bound (:attr:`extension_name`).
        """
        qualified = qualified_command(self.extension_name, name)
        assert self._hook_handlers is not None  # extension_name raised otherwise
        self._registry.register_command(qualified, command, owner=self._hook_handlers.path)
        self._hook_handlers.commands.append(qualified)
        return self._registry.bind_command(name, qualified)

    def get_command(self, name: str) -> dict | None:
        """Look up the command ``name`` resolves to.

        Read it to decide whether to claim a typed name at all, or to see whose
        handler is behind one before calling it.

        Args:
            name: Either name a command answers to: the typed one or the qualified
                ``ext:<extension>.<name>``.

        Returns:
            The registered command dict, or ``None`` if ``name`` resolves to none.
        """
        return self._registry.get_command(name)

    def unregister_command(self, name: str) -> None:
        """Withdraw one of THIS extension's commands, and any typed name bound to it.

        Args:
            name: Either the typed name passed to :meth:`register_command` or the
                qualified one it returned.

        Raises:
            ValueError: ``name`` resolves to a command this extension does not own.
                Fail-Early: the alternative is one extension quietly deleting
                another's, which is the failure this namespace exists to remove.
        """
        qualified = (
            name if is_qualified_command(name) else qualified_command(self.extension_name, name)
        )
        owner = split_qualified_command(qualified)
        if owner is None or owner[0] != self.extension_name:
            raise ValueError(
                f"{self.extension_name!r} cannot unregister {qualified!r}, which belongs to "
                f"another extension. Only the typed NAME is shared; the command is not."
            )
        self._registry.unregister_command(qualified)

    async def run_command(self, name: str, args: str = "") -> Any:
        """Run a registered command by either of its names and return its output.

        The composition seam. An extension that was refused a typed name, or that
        wants to extend another's behaviour, calls the qualified name — resolved HERE,
        at call time, so a disabled extension is a name that no longer answers rather
        than a captured callable that still runs.

        Args:
            name: A typed or qualified command name.
            args: The argument string, exactly as a reader would have typed it.

        Returns:
            Whatever the command's handler returned.

        Raises:
            RuntimeError: No api session, or ``name`` resolves to no command.
        """
        if self._session is None:
            raise RuntimeError("run_command needs a session; this ExtensionAPI has none.")
        result = await self._session.run_extension_command(name, args)
        if not result.handled:
            raise RuntimeError(
                f"no command named {name!r}. Qualified names are ext:<extension>.<command>; "
                "a typed name only resolves while some extension holds it."
            )
        return result.output

    def register_flow(
        self,
        name: str,
        description: str,
        handler: Any,
        *,
        argument: Argument | None = None,
        domain: Domain | None = None,
        values: Any = None,
    ) -> str | None:
        """Register a slash command AND say what it takes (docs/EXTENSION-FLOWS.md).

        The declaration gives the command completion, a rendered form and a
        palette entry in every head. The handler is still called with
        ``(args, ctx)``, ``args`` being the argument's bound value as text. At
        most one argument; a gesture needing several fields drives ``ui.form``.

        Args:
            name: The slash command. A name τ already declares is refused, because
                ``resolve_command`` gives a collision to the built-in and the flow
                would be unreachable.
            description: One line, shown in completion, the palette and ``/help``.
            handler: The callable ``(args, ctx)``, sync or async, as
                :meth:`register_command` takes.
            argument: What the command takes, or ``None`` for one that takes nothing.
            domain: The argument's domain, when it is not one τ already declares.
                Its ``name`` must be what ``argument.domain`` says.
            values: How ``domain``'s values are found, when it names an enumerator:
                a callable ``(query, limit) -> [(value, label)]``. Not needed for a
                domain that is ``free`` or has fixed ``values``.

        Returns:
            What :meth:`register_command` returns — ``None`` when ``/name`` is this
            flow, else the qualified name that holds it. The flow itself is always
            reachable at ``ext:<extension>.<name>``.

        Raises:
            ValueError: ``name`` or ``description`` is empty; ``handler`` is not
                callable; ``name`` is already a built-in flow or view; ``domain``
                does not match what ``argument`` names; a domain with an enumerator
                has no ``values`` callable; or an ``argument`` came with ``values``
                but no ``domain``.
            RuntimeError: No runner bucket is bound (:attr:`extension_name`).
        """
        if not isinstance(name, str) or not name:
            raise ValueError("register_flow: 'name' must be a non-empty string")
        if not isinstance(description, str) or not description:
            raise ValueError(f"register_flow: {name!r} needs a non-empty 'description'")
        if not callable(handler):
            raise ValueError(f"register_flow: {name!r} needs a callable 'handler'")
        if BUILTIN.flow(name) is not None or name in BUILTIN.views:
            raise ValueError(
                f"register_flow: {name!r} is already declared by τ. resolve_command gives a "
                "collision to the built-in, so this flow would be listed and undispatchable. "
                "Checked here rather than when the vocabulary is next built, so the "
                "registration fails where it was made."
            )

        arguments = (argument,) if argument is not None else ()
        if argument is not None:
            if domain is not None and domain.name != argument.domain:
                raise ValueError(
                    f"register_flow: {name!r} declares domain {domain.name!r} but its "
                    f"argument names {argument.domain!r}"
                )
            if domain is not None and domain.enumerator is not None and values is None:
                raise ValueError(
                    f"register_flow: domain {domain.name!r} names an enumerator, so it needs "
                    "a 'values' callable to list them. Without one the step would offer "
                    "nothing and read as an empty set rather than a missing registration."
                )
            if domain is None and values is not None:
                raise ValueError(
                    f"register_flow: {name!r} passed 'values' with no 'domain' to attach "
                    "them to. A built-in domain is enumerated by τ, not by an extension."
                )

        claimed = self.register_command(name, {"description": description, "handler": handler})
        qualified = qualified_command(self.extension_name, name)
        self._registry.register_flow(
            qualified,
            FlowDeclaration(
                flow=Flow(
                    name=qualified,
                    description=description,
                    mutation=qualified,
                    arguments=arguments,
                ),
                domain=domain,
                enumerator=values,
            ),
        )
        return claimed

    def register_shortcut(
        self,
        key: str,
        command: str,
        *,
        args: str = "",
        description: str | None = None,
    ) -> None:
        """Bind a key to a command in the guarded extension chord namespace (E10 §6 / S69).

        ``key`` is the chord tail after the ``ctrl+e`` extension leader, so an
        extension cannot clobber a core binding: ``key="g"`` with
        ``command="fleet_status"`` makes ``ctrl+e g`` dispatch ``/fleet_status``,
        exactly as a panel action or a typed command would.

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
        if self._hook_handlers is not None:
            self._hook_handlers.shortcuts.append(key)

    async def append_entry(self, custom_type: str, data: dict) -> None:
        """Persist durable, NON-message extension state onto the session tree (E6 §2 / S39).

        Appends a ``customEntry`` node via ``AgentSession._append_custom_entry``; it
        survives a reload and reads back through ``ctx.entries()``. It is NOT a
        message: never folded into the loop context, never seen by the model. To
        inject a node the model reads, use ``send_message`` (``visible_to_model``)
        or ``send_user_message``.

        Args:
            custom_type: The entry's ``customType``, naming its kind; non-empty.
            data: The state to persist.

        Raises:
            RuntimeError: no session with ``_append_custom_entry`` is bound (e.g. a
                bare ``ExtensionAPI()``); the entry is not dropped silently.
            ValueError: propagated from ``_append_custom_entry`` when ``custom_type``
                is empty or ``data`` is not a dict.
        """
        if not hasattr(self._session, "_append_custom_entry"):
            raise RuntimeError(
                "append_entry: no session with a custom-entry log is bound "
                "(the entry would have nowhere durable to land)"
            )
        await self._session._append_custom_entry(custom_type, data)

    async def request_user_action(
        self,
        sentence: str,
        *,
        lock: bool = False,
        ask: dict[str, Any] | None = None,
        release: str | None = None,
    ) -> str:
        """Stop the session, put a request in front of whoever is attached, or both.

        Reference: docs/EXTENSION-LOCKS.md. Appends the one reserved
        ``customEntry`` (:data:`~tau_agent_core.extension_locks.REQUEST_ENTRY_TYPE`)
        carrying this extension's identity. Two independent keys, so four states
        (§3): ``lock`` refuses the next submission at this cursor; ``ask`` is a
        spec every head can render. Neither blocks a coroutine and neither
        survives on anything but the tree, which is why both survive a restart
        and why a user can branch around either.

        Args:
            sentence: The one line a head shows under τ's own framing label.
            lock: Refuse submissions while the cursor is this entry.
            ask: A spec for :func:`validate_ask_spec` — body, optional fields,
                and the actions naming the commands that answer it.
            release: The command name that clears the lock, shown as the way
                out. Advisory: commands are exempt from a lock by WHERE the
                check sits (§5), not by matching this name.

        Returns:
            The appended entry's id — the request id an action is dispatched
            with, and what :meth:`~tau_agent_core.agent_session.AgentSession.answer_request`
            takes.

        Raises:
            RuntimeError: this api is bound to no runner bucket, so it has no
                extension identity to append under (Fail-Early: the identity is
                stored in the entry, and the case this exists for is a reload
                where nobody can be asked for it).
            ValueError: from :func:`validate_ask_spec` on a malformed ask, or
                from :func:`~tau_agent_core.extension_locks.build_request_data`
                on an entry that neither locks nor asks.
        """
        if self._hook_handlers is None:
            raise RuntimeError(
                "request_user_action: this ExtensionAPI is not bound to an "
                "ExtensionRunner bucket, so it has no extension identity to "
                "append under. Obtain the api from AgentSession's extension "
                "load path (each factory is handed a bucket-bound api)."
            )
        if not hasattr(self._session, "_append_custom_entry"):
            raise RuntimeError(
                "request_user_action: no session with a custom-entry log is bound "
                "(the request would have nowhere durable to land)"
            )
        data = build_request_data(
            self._hook_handlers.path,
            sentence,
            lock=lock,
            ask=None if ask is None else validate_ask_spec(ask),
            release=release,
        )
        entry_id: str = await self._session._append_custom_entry(REQUEST_ENTRY_TYPE, data)
        return entry_id

    async def set_session_name(self, name: str) -> None:
        """Name the session (:func:`apply_session_name`, shared with the RPC verb).

        Args:
            name: The new name; must be non-empty.

        Raises:
            RuntimeError: this api is not bound to a session.
            ValueError: ``name`` is empty.
        """
        await apply_session_name(self._bound_session("set_session_name"), name)

    def get_session_name(self) -> str | None:
        """Read the session's name (:func:`read_session_name`).

        Returns:
            The newest name, or ``None`` if never set or the newest name is empty.

        Raises:
            RuntimeError: this api is not bound to a session.
        """
        return read_session_name(self._bound_session("get_session_name"))

    def _bound_session(self, verb: str) -> Any:
        """The bound ``AgentSession``, or raise naming ``verb`` (Fail-Early)."""
        if self._session is None:
            raise RuntimeError(f"api.{verb}: this ExtensionAPI is not bound to an AgentSession")
        return self._session

    def send_user_message(self, content: str, deliver_as: str = "followUp") -> None:
        """Queue a user message for the agent.

        Args:
            content: The message text.
            deliver_as: The delivery mode; a plain ``str`` so modes stay extensible
                (decision 5), validated against the three the queue supports.
                ``"followUp"`` drains at the end of the current ``prompt()`` and
                re-enters within the same call. ``"nextTurn"`` waits for the next
                ``prompt()``. ``"steer"`` (docs/SUBMISSION-LIFECYCLE.md phase 4) is
                delivered by the loop running now, before its next LLM call — how a
                hook steers the turn it runs inside, which a submission cannot.

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

    async def send_message(self, message: dict, options: dict | None = None) -> None:
        """Append a durable custom message node onto the active path (E6 §2 / S38).

        Persisted as a ``role: "custom"`` tree node via
        ``AgentSession._append_custom_message``; it renders in the transcript and
        survives a reload, like a ``before_agent_start`` injection.

        Args:
            message: ``{customType, content, display?, details?}``.
            options: ``{"visible_to_model": True}`` also feeds the node to the
                model (as a user message on the wire). By default (D-E6-1) it is
                display-only and never reaches the LLM; ``before_agent_start`` and
                ``send_user_message`` are the model-visible channels.

        Raises:
            RuntimeError: no session with ``_append_custom_message`` is bound (e.g.
                a bare ``ExtensionAPI()``); the message is not dropped silently.
            ValueError: propagated from ``_append_custom_message`` when ``message``
                lacks ``content`` or ``customType``.
        """
        if not hasattr(self._session, "_append_custom_message"):
            raise RuntimeError(
                "send_message: no session with a custom-message log is bound "
                "(the message would have nowhere durable to land)"
            )
        await self._session._append_custom_message(message, options or {})

    @property
    def ui(self) -> ExtensionUI:
        """The session's one :class:`ExtensionUI`, the same object as ``ctx.ui``.

        Painted in the TUI; headless it emits records or stderr lines, and
        ``form`` follows the ``--ui-defaults`` policy.
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
        """The shared :class:`ExtensionContext` handlers receive as ``ctx``."""
        return self._context
