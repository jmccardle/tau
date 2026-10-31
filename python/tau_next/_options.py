"""An extension's options (D16 section 4): a frozen dataclass of values-language fields.

An extension names its options type in its `options` attribute. Each field is a hint of
`tau_next.values` with a plain default (a field with none must be configured). What a field
holds at a node is the last one set: the layers' `extensions.<name>` (`ModelCatalog`), then the
`Config` nodes on the node's walk, root to node, each setting only the fields it names. A
named type other than `Path` is refused in options: they are checked against a session, and
stored options are read without one (an option that names a model is a `str`).
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, get_type_hints

from tau_next._config import config_fields
from tau_next._config_layers import ConfigProblem, ModelCatalog
from tau_next._core import Body, Node, Session
from tau_next.protocols import ExtensionContractError
from tau_next.values import (
    Field,
    HintError,
    Json,
    check_object,
    convert_object,
    field_of,
    object_schema,
)


class OptionsError(ValueError):
    """Options that do not fit their extension's options type: an unknown option, a wrong
    type, a missing required one, or a refusal by the type's own `__post_init__`. The message
    names the extension."""


@dataclass(frozen=True)
class OptionsForm:
    """An extension's options as a client edits them: the JSON Schema of the options object
    (what a form is built from) and its `value` at a cursor, every option present (a required
    one nobody has set is absent)."""

    extension: str
    schema: Json
    value: Json


def option_fields(extension: str, options: object) -> tuple[Field, ...]:
    """The `Field`s of an extension's `options` type. `ExtensionContractError` unless it is a
    frozen dataclass whose fields are hints of the values language with plain defaults."""
    where = f"extension {extension!r}: `options`"
    params = getattr(options, "__dataclass_params__", None)
    if not isinstance(options, type) or params is None or not params.frozen:
        raise ExtensionContractError(f"{where} must be a frozen dataclass, not {options!r}")
    hints = get_type_hints(options, include_extras=True)
    fields: list[Field] = []
    for f in dataclasses.fields(options):
        if f.default_factory is not dataclasses.MISSING:
            raise ExtensionContractError(f"{where} field {f.name!r}: use a plain default")
        try:
            made = field_of(
                f.name, hints[f.name], f.default, has_default=f.default is not dataclasses.MISSING
            )
        except HintError as e:
            raise ExtensionContractError(f"{where} field {f.name!r}: {e}") from e
        if made.kind.needs_session:
            raise ExtensionContractError(
                f"{where} field {f.name!r}: a Model, CursorId, LabelName or NodeId is checked "
                "against a session, which stored options are not; use str or int"
            )
        fields.append(made)
    return tuple(fields)


def make_options(extension: str, options: type, fields: tuple[Field, ...], raw: Json) -> Any:
    """The `options` instance for the JSON `raw` (the fields set, the rest default).
    `OptionsError` naming the extension."""
    problem = check_object(fields, raw, None, "option")
    if problem is not None:
        raise OptionsError(f"extension {extension!r}: {problem}")
    try:
        return options(**convert_object(fields, raw, None))
    except (TypeError, ValueError) as e:  # the type's own __post_init__
        raise OptionsError(f"extension {extension!r}: {e}") from e


def _plain(value: object) -> object:
    """A default as JSON."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_plain(v) for v in cast("list[object]", value)]
    return value


class ExtensionOptions:
    """The options of a driver's extensions, read at a node. `extensions` are checked here
    (`ExtensionContractError`); `catalog` says where the layers' options are now."""

    def __init__(
        self,
        extensions: Sequence[object],
        session: Session,
        catalog: Callable[[], ModelCatalog],
    ) -> None:
        self._session = session
        self._catalog = catalog
        self._types: dict[str, type] = {}
        self._fields: dict[str, tuple[Field, ...]] = {}
        # Per node, the options its walk's Config nodes set. A node never changes, so each is
        # folded once, from its parent's; a node that is no Config shares its parent's.
        self._folds: dict[int, Mapping[str, Mapping[str, object]]] = {}
        for ext in extensions:
            options = getattr(ext, "options", None)
            if options is not None:
                name = cast("str", getattr(ext, "name"))  # noqa: B009
                self._fields[name] = option_fields(name, options)
                self._types[name] = cast("type", options)

    def names(self) -> tuple[str, ...]:
        """The extensions that have options."""
        return tuple(self._types)

    def _raw(self, extension: str, node: int) -> Json:
        if extension not in self._types:
            raise OptionsError(f"extension {extension!r} has no options")
        layered = self._catalog().extensions.get(extension, {})
        return {**layered, **self._fold(node).get(extension, {})}

    def _fold(self, node: int) -> Mapping[str, Mapping[str, object]]:
        tree = self._session.tree
        chain: list[Node] = []
        base: Mapping[str, Mapping[str, object]] = {}
        current: int | None = node
        while current is not None:
            if current in self._folds:
                base = self._folds[current]
                break
            here = tree.node(current)
            chain.append(here)
            current = here.parent
        for here in reversed(chain):
            if isinstance(here.body, Body.Config):
                over = config_fields(here.body.config).extensions
                base = {**base, **{k: {**base.get(k, {}), **v} for k, v in over.items()}}
            self._folds[here.id] = base
        return base

    def at(self, extension: str, node: int) -> Any:
        """The `extension`'s options instance at `node`. `OptionsError` if it has none, or
        what is set does not fit (including a required option nobody set)."""
        raw = self._raw(extension, node)
        return make_options(extension, self._types[extension], self._fields[extension], raw)

    def form(self, extension: str, node: int) -> OptionsForm:
        """The extension's options as a client edits them at `node`."""
        raw = self._raw(extension, node)
        fields = self._fields[extension]
        value: Json = {f.name: _plain(f.default) for f in self._defaults(extension)}
        value.update({f.name: None for f in fields if f.nullable})
        value.update(raw)
        return OptionsForm(extension, object_schema(fields, model=False), value)

    def _defaults(self, extension: str) -> list["dataclasses.Field[Any]"]:
        return [
            f
            for f in dataclasses.fields(self._types[extension])
            if f.default is not dataclasses.MISSING
        ]

    def check_config(
        self, at: int, model: str | None, extensions: Mapping[str, Mapping[str, object]]
    ) -> None:
        """Raise unless a `Config` node under `at` that sets `model` and `extensions` leaves
        every extension's options valid: `ConfigError` for a model that is not in the catalog,
        `OptionsError` for an extension without options or for what each named extension's
        options would be at the node (the walk's, then these) and the type refuses."""
        if model is not None:
            self._catalog().resolve(model)
        for name, new in extensions.items():
            raw = {**self._raw(name, at), **new}
            make_options(name, self._types[name], self._fields[name], raw)


def check_options(catalog: ModelCatalog, extensions: Sequence[object]) -> tuple[ConfigProblem, ...]:
    """Every problem in what the layers set for the `extensions` that have options: an
    option the type does not have, one that does not fit, a required one nobody set. Options
    for an extension not in `extensions` are not looked at (it may be loaded elsewhere)."""
    problems: list[ConfigProblem] = []
    for ext in extensions:
        options = getattr(ext, "options", None)
        if options is None:
            continue
        name = cast("str", getattr(ext, "name"))  # noqa: B009
        raw = dict(catalog.extensions.get(name, {}))
        try:
            make_options(name, options, option_fields(name, options), raw)
        except (OptionsError, ExtensionContractError) as e:
            problems.append(ConfigProblem("error", str(e), None, f"extensions.{name}"))
    return tuple(problems)
