"""The top-level keys of the config files other than `models` and `default_model` (D13): each
read from the merged tree into a plain value, with its problems added to the layers' problems.
`tui` is the terminal UI's own (D23): an object whose only key is `theme`, and the user's alone
(a project that sets it is an error, found as the layers are read)."""

from typing import cast

from tau._config_tree import Leaf, Problems, Tree, plain, source_of, suggest

TUI_KEYS = ("theme",)
"""What `tui` may hold."""


def extensions(
    merged: Tree, problems: Problems
) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    """The merged `extensions` object: each extension's options, an object, and the layer that
    set each (`NAME`, and `NAME.FIELD` for each field)."""
    node = merged.get("extensions")
    if node is None or (isinstance(node, Leaf) and node.value is None):
        return {}, {}
    if not isinstance(node, dict):
        problems.add("error", "'extensions' must be an object", source_of(node), "extensions")
        return {}, {}
    out: dict[str, dict[str, object]] = {}
    sources: dict[str, str] = {}
    for name, options in node.items():
        if isinstance(options, Leaf):
            if options.value is not None:
                msg = f"'extensions.{name}' must be an object of options"
                problems.add("error", msg, options.source, f"extensions.{name}")
            continue
        out[name] = cast("dict[str, object]", plain(options))
        if (where := source_of(options)) is not None:
            sources[name] = where
        for field, setting in options.items():
            if (where := source_of(setting)) is not None:
                sources[f"{name}.{field}"] = where
    return out, sources


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


def context_files(merged: Tree, problems: Problems) -> bool:
    """The merged `context_files`: true when no layer sets it. A `null` drops an inherited value."""
    node = merged.get("context_files")
    if node is None or (isinstance(node, Leaf) and node.value is None):
        return True
    if not (isinstance(node, Leaf) and isinstance(node.value, bool)):
        problems.add(
            "error", "'context_files' must be true or false", source_of(node), "context_files"
        )
        return True
    return node.value


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
