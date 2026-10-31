"""An extension's options (D16 section 4): a frozen dataclass of values-language fields.

An extension names its options type in its `options` attribute. Each field is a hint of
`tau_next._values` with a plain default (a field with none must be configured). What a field
holds at a node is the last one set: the layers' `extensions.<name>` (`ModelCatalog`), then the
`Config` nodes on the node's walk, root to node, each setting only the fields it names. A
`list[T]` field's default is a tuple, since a dataclass refuses a list default; the options
hold a list either way. A
named type that is looked up in the session (`CursorId`, `LabelName`, `NodeId`) is refused in
options: stored options are read without one. A `Model` is checked against the catalog.
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast, get_type_hints

from tau_next._config import ConfigError, config_fields
from tau_next._core import Body, Node, Session
from tau_next._values import (
    Context,
    DefinitionError,
    Field,
    Form,
    Json,
    check_object,
    convert_object,
    field_of,
    object_schema,
)

if TYPE_CHECKING:  # _config_layers checks options, so it imports us
    from tau_next._config_layers import ModelCatalog


def option_fields(extension: str, options: object) -> tuple[Field, ...]:
    """The `Field`s of an extension's `options` type. `DefinitionError` unless it is a frozen
    dataclass whose fields are hints of the values language with plain defaults."""
    where = f"extension {extension!r}: `options`"
    params = getattr(options, "__dataclass_params__", None)
    if not isinstance(options, type) or params is None or not params.frozen:
        raise DefinitionError(f"{where} must be a frozen dataclass, not {options!r}")
    hints = get_type_hints(options, include_extras=True)
    fields: list[Field] = []
    for f in dataclasses.fields(options):
        if f.default_factory is not dataclasses.MISSING:
            raise DefinitionError(f"{where} field {f.name!r}: use a plain default")
        try:
            made = field_of(
                f.name, hints[f.name], f.default, has_default=f.default is not dataclasses.MISSING
            )
        except DefinitionError as e:
            raise DefinitionError(f"{where} field {f.name!r}: {e}") from e
        if made.kind.session_only:
            raise DefinitionError(
                f"{where} field {f.name!r}: a CursorId, LabelName or NodeId is looked up in a "
                "session, which stored options are read without; use int or str"
            )
        fields.append(made)
    return tuple(fields)


def make_options(
    extension: str, options: type, fields: tuple[Field, ...], raw: Json, context: Context
) -> Any:
    """The `options` instance for the JSON `raw` (the fields set, the rest default).
    `ConfigError` naming the extension."""
    problem = check_object(fields, raw, context, "option")
    if problem is not None:
        raise ConfigError(f"extension {extension!r}: {problem}")
    converted = convert_object(fields, raw, context)
    for f in fields:  # a list option's default is a tuple; its value is a list, set or not
        default: object = f.default
        if f.name not in converted and isinstance(default, tuple):
            converted[f.name] = list(cast("tuple[object, ...]", default))
    try:
        return options(**converted)
    except (TypeError, ValueError) as e:  # the type's own __post_init__
        raise ConfigError(f"extension {extension!r}: {e}") from e


def _description(options: type) -> str:
    """The options type's docstring; a dataclass without one has its signature there, which
    describes nothing."""
    doc = options.__doc__ or ""
    return "" if doc.startswith(f"{options.__name__}(") else doc.strip()


class ExtensionOptions:
    """The options of a driver's extensions, read at a node. `extensions` are checked here
    (`DefinitionError`); `catalog` says where the layers' options are now."""

    def __init__(
        self,
        extensions: Sequence[object],
        session: Session,
        catalog: Callable[[], "ModelCatalog"],
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
            raise ConfigError(f"extension {extension!r} has no options")
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

    def _make(self, extension: str, raw: Json) -> Any:
        return make_options(
            extension,
            self._types[extension],
            self._fields[extension],
            raw,
            Context(self._catalog()),
        )

    def at(self, extension: str, node: int) -> Any:
        """The `extension`'s options instance at `node`. `ConfigError` if it has none, or
        what is set does not fit (including a required option nobody set)."""
        return self._make(extension, self._raw(extension, node))

    def form(self, extension: str, node: int) -> Form:
        """The extension's options as a client edits them at `node`: the schema of the options
        object, each default the value now (a required option nobody has set has none)."""
        raw = self._raw(extension, node)
        options = self._types[extension]
        schema = object_schema(self._fields[extension], model=False, current=raw)
        return Form(extension, _description(options), schema)

    def check_config(
        self, at: int, model: str | None, extensions: Mapping[str, Mapping[str, object]]
    ) -> None:
        """Raise unless a `Config` node under `at` that sets `model` and `extensions` leaves
        every extension's options valid: `ConfigError` for a model that is not in the catalog,
        for an extension without options, or for what each named extension's options would be
        at the node (the walk's, then these) and the type refuses."""
        if model is not None:
            self._catalog().resolve(model)
        for name, new in extensions.items():
            self._make(name, {**self._raw(name, at), **new})
