"""The top-level keys of the config files other than `models` and `default_model` (D13): each
read from the merged tree into a plain value, with its problems added to the layers' problems.
`tui` is the terminal UI's own (D23): an object whose only key is `theme`, and the user's alone
(a project that sets it is an error, found as the layers are read)."""

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import cast

from tau._config_tree import ConfigProblem, Leaf, Problems, Tree, plain, source_of, suggest
from tau._constants import administrator_keys

ALWAYS = ("tau", "session_name")
"""The extensions every start has, outside the sources (D19): the driver's and the startup's."""
ORDER = "order"
"""The key of `extensions.NAME` that is the extension's hook order, not an option (D28)."""
TIMEOUTS = ("start_timeout", "stop_timeout")
"""The keys of `extensions.NAME` that are the extension's start and stop timeouts in seconds (D32
section 4): the administrator's, like `order`, never options a client sets."""
TUI_KEYS = ("theme",)
"""What `tui` may hold."""


def extensions(
    merged: Tree, problems: Problems
) -> tuple[dict[str, dict[str, object]], dict[str, str], dict[str, object]]:
    """The merged `extensions` object: each extension's options, an object, the layer that
    set each (`NAME`, and `NAME.FIELD` for each field), and apart from the options each
    extension's hook `order` (D28: not an option, so every extension has it; checked against the
    extension where it is loaded)."""
    node = merged.get("extensions")
    if node is None or (isinstance(node, Leaf) and node.value is None):
        return {}, {}, {}
    if not isinstance(node, dict):
        problems.add("error", "'extensions' must be an object", source_of(node), "extensions")
        return {}, {}, {}
    out: dict[str, dict[str, object]] = {}
    sources: dict[str, str] = {}
    orders: dict[str, object] = {}
    for name, options in node.items():
        if isinstance(options, Leaf):
            if options.value is not None:
                msg = f"'extensions.{name}' must be an object of options"
                problems.add("error", msg, options.source, f"extensions.{name}")
            continue
        out[name] = cast("dict[str, object]", plain(options))
        if (order := out[name].pop(ORDER, None)) is not None:
            orders[name] = order
        if (where := source_of(options)) is not None:
            sources[name] = where
        for field, setting in options.items():
            if (where := source_of(setting)) is not None:
                sources[f"{name}.{field}"] = where
    return out, sources, orders


def timeouts(
    options: dict[str, dict[str, object]], sources: dict[str, str], problems: Problems
) -> dict[str, tuple[float | None, float | None]]:
    """Each extension's `start_timeout` and `stop_timeout` (`None`: the default), taken out of
    its `options` (D32 section 4). A value that is not a positive number is an `error`."""
    found: dict[str, tuple[float | None, float | None]] = {}
    for name, values in options.items():
        pair: list[float | None] = [None, None]
        for i, key in enumerate(TIMEOUTS):
            if key not in values:
                continue
            value = values.pop(key)
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                message = f"'extensions.{name}.{key}' must be a positive number of seconds"
                where = sources.get(f"{name}.{key}") or sources.get(name)
                problems.add("error", message, where, f"extensions.{name}.{key}")
                continue
            pair[i] = float(value)
        if pair != [None, None]:
            found[name] = (pair[0], pair[1])
    return found


def timeout_problems(
    timeouts: Mapping[str, tuple[float | None, float | None]],
    extensions: Iterable[object],
    sources: Mapping[str, str],
) -> list[ConfigProblem]:
    """A `warning` for each timeout set for a loaded extension that has no `start` (or no
    `stop`): it is ignored, as other configuration that names what a start does not have."""
    found: list[ConfigProblem] = []
    for ext in extensions:
        name = cast("str", getattr(ext, "name"))  # noqa: B009
        for key, value, method in zip(
            TIMEOUTS, timeouts.get(name, (None, None)), ("start", "stop"), strict=True
        ):
            if value is not None and getattr(ext, method, None) is None:
                said = f"extension {name!r} has no {method}; {key} is ignored"
                where = sources.get(f"{name}.{key}") or sources.get(name)
                found.append(ConfigProblem("warning", said, where, f"extensions.{name}.{key}"))
    return found


def load_switches(merged: Tree, problems: Problems) -> tuple[dict[str, bool], dict[str, str]]:
    """The merged `load` object: each extension source's key to `true` or `false`, and the
    layer that set it. A `null` dropped the key in a nearer layer."""
    node = merged.get("load")
    if node is None or (isinstance(node, Leaf) and node.value is None):
        return {}, {}
    if not isinstance(node, dict):
        problems.add("error", "'load' must be an object", source_of(node), "load")
        return {}, {}
    switches: dict[str, bool] = {}
    sources: dict[str, str] = {}
    for key, setting in node.items():
        if not isinstance(setting, Leaf):
            problems.add("error", f"'load.{key}' must be true or false", source_of(setting), "load")
        elif setting.value is None:
            continue
        elif not isinstance(setting.value, bool):
            problems.add("error", f"'load.{key}' must be true or false", setting.source, "load")
        else:
            switches[key], sources[key] = setting.value, setting.source
    return switches, sources


def system_prompt(merged: Tree, problems: Problems) -> tuple[str | None, str | None]:
    """The merged `system_prompt` (a string, possibly empty) and the layer that set it. A `null`
    dropped an inherited value."""
    node = merged.get("system_prompt")
    if node is None or (isinstance(node, Leaf) and node.value is None):
        return None, None
    if not (isinstance(node, Leaf) and isinstance(node.value, str)):
        problems.add("error", "'system_prompt' must be a string", source_of(node), "system_prompt")
        return None, None
    return node.value, node.source


def context_files(merged: Tree, problems: Problems) -> tuple[bool, str | None]:
    """The merged `context_files` (true when no layer sets it) and the layer that set it. A `null`
    drops an inherited value."""
    node = merged.get("context_files")
    if node is None or (isinstance(node, Leaf) and node.value is None):
        return True, None
    if not (isinstance(node, Leaf) and isinstance(node.value, bool)):
        problems.add(
            "error", "'context_files' must be true or false", source_of(node), "context_files"
        )
        return True, None
    return node.value, node.source


def tui_settings(merged: Tree, problems: Problems) -> dict[str, str]:
    """The merged `tui` object: each setting a string of a key `tui` has (`theme`). A `null` drops
    an inherited value."""
    node = merged.get("tui")
    if node is None or (isinstance(node, Leaf) and node.value is None):
        return {}
    if not isinstance(node, dict):
        problems.add("error", "'tui' must be an object", source_of(node), "tui")
        return {}
    settings: dict[str, str] = {}
    for key, value in cast("dict[str, object]", plain(node)).items():
        if key not in TUI_KEYS:
            msg = f"unknown key 'tui.{key}'{suggest(key, TUI_KEYS)}"
            problems.add("error", msg, source_of(node), f"tui.{key}")
        elif not isinstance(value, str):
            problems.add("error", f"'tui.{key}' must be a string", source_of(node), f"tui.{key}")
        else:
            settings[key] = value
    return settings


def match_names(names: Iterable[str], name: str) -> tuple[str, ...]:
    """A name with ':' is an exact qualified name; one without is a bare search."""
    if ":" in name:
        return tuple(q for q in names if q == name)
    return tuple(q for q in names if q.rsplit(":", 1)[-1] == name)


def default_model(merged: Tree, declared: list[str], problems: Problems) -> str | None:
    node = merged.get("default_model")
    if node is None or (isinstance(node, Leaf) and node.value is None):
        return None
    source = source_of(node)
    if not (isinstance(node, Leaf) and isinstance(node.value, str) and node.value):
        problems.add("error", "'default_model' must be a string", source, "default_model")
        return None
    name = node.value
    found = match_names(declared, name)
    if not found:
        problems.add(
            "error", f"default_model {name!r} is not in your config", source, "default_model"
        )
    elif len(found) > 1:
        msg = f"default_model {name!r} is ambiguous; use one of {list(found)}"
        problems.add("error", msg, source, "default_model")
    return name


def unprovided(
    given: Mapping[str, Mapping[str, object]],
    extensions: Sequence[object],
    off: Collection[str],
    warn: Callable[[str, str], None],
) -> None:
    """Warn of what `given` (the layers' `extensions`) sets for an extension nothing provides, or
    for one that has no options and declares no administrator keys (`tau._constants`)."""
    loaded = {str(getattr(e, "name")) for e in extensions} | set(ALWAYS)  # noqa: B009
    for key in sorted(given):
        if key not in loaded and key not in off and key != "retry":
            warn(
                f"no loaded extension is named {key!r}; its options are ignored",
                f"extensions.{key}",
            )
    keyless = [e for e in extensions if administrator_keys(e) is None]  # what it reads is no option
    bare = [str(e.name) for e in keyless if getattr(e, "options", None) is None]  # type: ignore[attr-defined]
    for key in ("retry", *bare):
        if given.get(key):
            warn(f"extension {key!r} has no options; they are ignored", f"extensions.{key}")
