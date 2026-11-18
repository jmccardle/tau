"""Hook order (D28): which extension's hook runs first at each hook point.

Every hook has an **order**, an integer: what its extension declares (its `order` attribute),
unless the config sets `extensions.NAME.order`, else 0. At each point the extensions that hook it
run sorted by order, then by load position, so with no order set anywhere they run in load order,
as before D28. The conventional values are `GUARD` (-100: a hook that halts, such as a budget or
a permission), `EDIT` (0: a hook that changes the draft or the line) and `OBSERVE` (100: a hook
that records or shows what happened); an extension may use any integer.

An order, declared or configured, is an `int` (every hook of the extension) or an object from
hook point to `int` (those hooks; the others keep what they had). A declared mapping must name
hooks the extension has (`DefinitionError`); a configured one must too, and its values must be
integers (`ConfigError`). The config's mapping is laid over the declared one, hook by hook.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from tau._config import ConfigError
from tau._config_keys import ORDER
from tau._protocols import CURSOR_POINTS, TOOL_POINTS, Extension
from tau._values import DefinitionError

GUARD = -100
"""The conventional order of a hook that halts: a budget, a permission, a limit."""
EDIT = 0
"""The conventional order of a hook that changes the draft, the call or the line (the default)."""
OBSERVE = 100
"""The conventional order of a hook that only records or shows what happened."""
POINTS = (*CURSOR_POINTS, *TOOL_POINTS)
"""Every hook point, cursor points in the order a step reaches them, then the tool points."""


def hooks_of(extension: object) -> tuple[str, ...]:
    """The hook points `extension` has a hook for, in `POINTS` order."""
    return tuple(p for p in POINTS if getattr(extension, p, None) is not None)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _problem(name: str, value: object, hooks: Sequence[str], what: str) -> str | None:
    """Why `value` is not an order for the extension `name` with `hooks`, or `None`."""
    having = ", ".join(hooks) if hooks else "none"
    if _is_int(value):
        return None if hooks else f"{what}: extension {name!r} has no hooks to order"
    if not isinstance(value, Mapping):
        return f"{what} must be an integer or an object of hook point to integer, not {value!r}"
    for hook, number in cast("Mapping[object, object]", value).items():
        if hook not in hooks:
            known = "a hook point" if hook in POINTS else "not a hook point"
            return f"{what}: extension {name!r} has no hook {hook!r} ({known}); its hooks: {having}"
        if not _is_int(number):
            return f"{what}.{hook} must be an integer, not {number!r}"
    return None


def _spread(value: object, hooks: Sequence[str]) -> dict[str, int]:
    """A checked order as one integer per hook it sets."""
    if _is_int(value):
        return dict.fromkeys(hooks, cast("int", value))
    return {str(k): cast("int", v) for k, v in cast("Mapping[object, object]", value).items()}


def declared_order(extension: object) -> dict[str, int]:
    """The order `extension` declares for each hook it sets one for (`order`: an `int` for all
    its hooks, or a mapping from hook point to `int`). `DefinitionError` naming the extension for
    any other value, a hook it does not have, or an `int` on an extension with no hooks."""
    value: object = getattr(extension, ORDER, None)
    if value is None:
        return {}
    name = cast("str", getattr(extension, "name"))  # noqa: B009
    hooks = hooks_of(extension)
    problem = _problem(name, value, hooks, "`order`")
    if problem is not None:
        raise DefinitionError(f"extension {name!r}: {problem}")
    return _spread(value, hooks)


def order_problem(extension: object, value: object) -> str | None:
    """Why `value`, set as `extensions.NAME.order` in the config, is not an order for
    `extension`, or `None`."""
    name = cast("str", getattr(extension, "name"))  # noqa: B009
    return _problem(name, value, hooks_of(extension), f"extensions.{name}.{ORDER}")


@dataclass(frozen=True)
class Placed:
    """One extension's hook at a point: the extension, its `order` there, and whether the
    config set it (`configured`) rather than the extension or the default."""

    extension: Extension
    order: int
    configured: bool


@dataclass(frozen=True)
class HookOrder:
    """Each hook point's extensions in the order they run (D28)."""

    points: Mapping[str, tuple[Placed, ...]]

    @classmethod
    def of(cls, extensions: Sequence[Extension], configured: Mapping[str, object]) -> "HookOrder":
        """The order for `extensions` (in load order) under `configured`, the config's
        `extensions.NAME.order` by name (a name no extension has is not looked at).
        `DefinitionError` for a bad declared order; `ConfigError` for a bad configured one."""
        placed: dict[str, list[tuple[int, int, Placed]]] = {p: [] for p in POINTS}
        for position, extension in enumerate(extensions):
            own = declared_order(extension)
            setting = configured.get(extension.name)
            if setting is not None and (problem := order_problem(extension, setting)) is not None:
                raise ConfigError(problem)
            over = {} if setting is None else _spread(setting, hooks_of(extension))
            for hook in hooks_of(extension):
                number = over.get(hook, own.get(hook, EDIT))
                placed_hook = Placed(extension, number, hook in over)
                placed[hook].append((number, position, placed_hook))
        return cls({p: tuple(e for _, _, e in sorted(placed[p], key=_key)) for p in POINTS})

    def at(self, point: str) -> tuple[Extension, ...]:
        """The extensions with a hook at `point`, in the order they run."""
        return tuple(p.extension for p in self.points[point])


def _key(item: tuple[int, int, Placed]) -> tuple[int, int]:
    return item[0], item[1]
