"""Model sections (D28 section 7): what a model's config holds for one extension.

`models.X.NAME` is a JSON object that belongs to the extension named `NAME`. Core carries it
unread (`ModelConfig.sections`); the extension declares `model_options`, a frozen dataclass whose
fields are the section's keys and whose `__post_init__` checks and converts the values, and reads
it through a handle's `model_options`. A section its type refuses is an error at `tau config
check` and at the start; one that names no loaded extension, or one without `model_options`, is a
warning, and is ignored (an extension switched off by `load` keeps its sections quietly).
"""

import dataclasses
from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from tau._config import ConfigError
from tau._config_tree import ConfigProblem
from tau._values import DefinitionError

if TYPE_CHECKING:
    from tau._config_layers import ModelCatalog

MODEL_OPTIONS = "model_options"
"""The attribute an extension names its model section's type in."""


def model_options_type(extension: object) -> type | None:
    """The `model_options` type `extension` declares, or `None`. `DefinitionError` unless it is
    a frozen dataclass."""
    kind: object = getattr(extension, MODEL_OPTIONS, None)
    if kind is None:
        return None
    from tau._config_layers import MODEL_KEYS  # noqa: PLC0415 - it imports us

    name = cast("str", getattr(extension, "name"))  # noqa: B009
    params = getattr(kind, "__dataclass_params__", None)
    if not isinstance(kind, type) or params is None or not params.frozen:
        raise DefinitionError(
            f"extension {name!r}: `model_options` must be a frozen dataclass, not {kind!r}"
        )
    if name in MODEL_KEYS:
        raise DefinitionError(
            f"extension {name!r}: a model key has that name, so no model section can"
        )
    return kind


def build_section(kind: type, extension: str, model: str, section: Mapping[str, object]) -> Any:
    """`kind` built from `section` (its keys as keyword arguments). `ConfigError` naming
    `models.<model>.<extension>` for a key it does not have, a required one missing, or a value
    its `__post_init__` refuses."""
    fields = {f.name for f in dataclasses.fields(kind)}
    where = f"model {model!r}: {extension}"
    unknown = sorted(k for k in section if k not in fields)
    if unknown:
        raise ConfigError(f"{where} has no key {unknown}; it holds {sorted(fields)}")
    try:
        return kind(**section)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where}: {e}") from e


def section_problems(
    catalog: "ModelCatalog", extensions: Sequence[object], off: Collection[str] = ()
) -> list[ConfigProblem]:
    """Every problem in the models' sections for `extensions` (the module docstring)."""
    by_name = {cast("str", getattr(e, "name")): e for e in extensions}  # noqa: B009
    problems: list[ConfigProblem] = []
    for qualified in catalog.names():
        config = catalog.resolve(qualified)
        sources = {o.key: o.source for o in catalog.explain(qualified)}
        path = "models." + ".models.".join(qualified.split(":"))
        for name, section in config.sections.items():
            here, source = f"{path}.{name}", sources.get(name)
            extension = by_name.get(name)
            if extension is None:
                if name not in off:
                    message = f"no loaded extension is named {name!r}; {here} is ignored"
                    problems.append(ConfigProblem("warning", message, source, here))
                continue
            kind = model_options_type(extension)
            if kind is None:
                message = f"extension {name!r} reads no model section; {here} is ignored"
                problems.append(ConfigProblem("warning", message, source, here))
                continue
            try:
                build_section(kind, name, qualified, section)
            except ConfigError as e:
                problems.append(ConfigProblem("error", str(e), source, here))
    return problems
