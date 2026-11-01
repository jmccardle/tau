"""An extension's options (D16 section 4): a frozen dataclass of values-language fields.

An extension names its options type in its `options` attribute. Each field is a hint of
`tau._values` with a plain default (a field with none must be configured). What a field
holds at a node is the last one set: the layers' `extensions.<name>` (`ModelCatalog`), then the
`Config` nodes on the node's walk, root to node, each setting only the fields it names. A
`list[T]` field's default is a tuple, since a dataclass refuses a list default; the options
hold a list either way. A
named type that is looked up in the session (`CursorId`, `LabelName`, `NodeId`) is refused in
options: stored options are read without one. A `Model` is checked against the catalog.
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast, get_type_hints

from tau._config import ConfigError, config_fields
from tau._core import Body, Node, Session
from tau._values import (
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
    from tau._config_layers import ModelCatalog
    from tau._user_config import UserConfig


@dataclass(frozen=True)
class _Fold:
    """What the `Config` nodes on one node's walk set, folded: the last `model` named, and per
    extension the options set (each option's last value)."""

    model: str | None = None
    extensions: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: {})


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


def save_options(
    user: "UserConfig",
    saving: Mapping[str, tuple[type, tuple[Field, ...], Json]],
) -> "ModelCatalog":
    """Write `changes` into the user's file: `saving` maps each extension to its options type,
    its `Field`s and the JSON values to set. Every extension's options, as the layers hold them
    with the change, are built before anything is written (`ConfigError`); returns the catalog
    the layers make now."""

    def fits(catalog: "ModelCatalog") -> None:
        for name, (options, fields, _) in saving.items():
            raw = dict(catalog.extensions.get(name, {}))
            make_options(name, options, fields, raw, Context(catalog))

    return user.save_options({name: changes for name, (_, _, changes) in saving.items()}, fits)


def _description(options: type) -> str:
    """The options type's docstring; a dataclass without one has its signature there, which
    describes nothing."""
    doc = options.__doc__ or ""
    return "" if doc.startswith(f"{options.__name__}(") else doc.strip()


class ExtensionOptions:
    """The options of a driver's extensions, read at a node, and the model a node's branch is on
    (the same fold of its `Config` nodes). `extensions` are checked here (`DefinitionError`);
    `catalog` says where the layers' options are now."""

    def __init__(
        self,
        extensions: Sequence[object],
        session: Session,
        catalog: Callable[[], "ModelCatalog"],
        user: "UserConfig | None" = None,
        use_catalog: Callable[["ModelCatalog"], None] | None = None,
    ) -> None:
        self._session = session
        self._catalog = catalog
        self._user = user
        self._use_catalog = use_catalog
        self._types: dict[str, type]
        self._fields: dict[str, tuple[Field, ...]]
        # Per node, the options its walk's Config nodes set. A node never changes, so each is
        # folded once, from its parent's; a node that is no Config shares its parent's.
        self._folds: dict[int, _Fold] = {}
        self.reload(extensions)

    def reload(self, extensions: Sequence[object]) -> None:
        """Read `extensions` as the driver's from now on (`DefinitionError`; nothing changes
        then), and forget every fold. A `Config` node holds options of an extension that is no
        longer here, or no longer fits its type: that fails when it is read, never here."""
        types: dict[str, type] = {}
        fields: dict[str, tuple[Field, ...]] = {}
        for ext in extensions:
            options = getattr(ext, "options", None)
            if options is not None:
                name = cast("str", getattr(ext, "name"))  # noqa: B009
                fields[name] = option_fields(name, options)
                types[name] = cast("type", options)
        self._types, self._fields = types, fields
        self._folds.clear()

    def names(self) -> tuple[str, ...]:
        """The extensions that have options."""
        return tuple(self._types)

    def model(self, node: int) -> str:
        """The qualified name of the last model a `Config` on `node`'s walk names: the model the
        next sample from there uses. `ConfigError` if none names one, or a `Config` on the walk
        is malformed."""
        name = self._fold(node).model
        if name is None:
            raise ConfigError("config is missing 'model'")
        return name

    def models(self) -> tuple[str, ...]:
        """The qualified names of the models the catalog defines, in file order."""
        return self._catalog().names()

    def _raw(self, extension: str, node: int) -> Json:
        if extension not in self._types:
            raise ConfigError(f"extension {extension!r} has no options")
        layered = self._catalog().extensions.get(extension, {})
        return {**layered, **self._fold(node).extensions.get(extension, {})}

    def _fold(self, node: int) -> _Fold:
        tree = self._session.tree
        chain: list[Node] = []
        base = _Fold()
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
                set_here = config_fields(here.body.config)
                over = set_here.extensions
                base = _Fold(
                    base.model if set_here.model is None else set_here.model,
                    {
                        **base.extensions,
                        **{k: {**base.extensions.get(k, {}), **v} for k, v in over.items()},
                    },
                )
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

    def save(self, extension: str, changes: Mapping[str, object]) -> None:
        """Set `changes` (JSON values) of `extension`'s options in the user's file, for every
        session, and have the driver read the layers again. `ConfigError` if the extension has no
        options, the driver was given no user config, or the options that result do not fit;
        nothing is written then."""
        if extension not in self._types:
            raise ConfigError(f"extension {extension!r} has no options")
        if self._user is None or self._use_catalog is None:
            raise ConfigError("this driver has no user config to write")
        saving = {extension: (self._types[extension], self._fields[extension], dict(changes))}
        self._use_catalog(save_options(self._user, saving))

    def saved(self, extension: str) -> Mapping[str, object]:
        """The fields of `extension`'s options that the user's file sets itself, as JSON values.
        `ConfigError` if the extension has no options, or the driver was given no user config."""
        if extension not in self._types:
            raise ConfigError(f"extension {extension!r} has no options")
        if self._user is None:
            raise ConfigError("this driver has no user config to read")
        return self._user.saved(extension)

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
