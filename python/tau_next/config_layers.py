"""Reading model configuration from layered JSON files (D13).

Layers, nearest wins: user, project, CLI overrides. Their raw trees are merged
first (objects by key; lists and scalars replace; `null` drops), then groups
and models are resolved, so a project can tune a user's model by qualified
name. Finding the files and trusting them is not done here.
"""

import difflib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from tau_next.config import ConfigError, ModelConfig

Severity = Literal["error", "warning"]

CLI = "cli"
_MODEL_KEYS = ("model", "base_url", "api_key_env", "api_key", "provider")
# Frontend settings will widen the top level in a later part (D13: "and frontend settings").
_TOP_KEYS = ("models", "default_model")
_EXCLUSIVE = {"api_key": "api_key_env", "api_key_env": "api_key"}  # one setting, two spellings
_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PROVIDERS = ("openai",)
_RENAMED_KEY = "backend"  # rules: allow(backend): 0.15's key name, which D13 rejects by name
_RENAMED_MESSAGE = "`backend` is now `provider`"  # rules: allow(backend): 0.15's key name


@dataclass(frozen=True)
class ConfigProblem:
    """One thing wrong (or suspicious) in the config layers.

    `source` is the file path or `"cli"`; `path` the dotted location inside it
    (`models.free_tokens.models.gpt-6-sol.modle`); `line` and `column` are set
    for JSON syntax errors. The message never holds an API key.
    """

    severity: Severity
    message: str
    source: str | None = None
    path: str = ""
    line: int | None = None
    column: int | None = None

    def __str__(self) -> str:
        where = self.source or "config"
        if self.line is not None:
            where += f":{self.line}:{self.column}"
        if self.path:
            where += f" at {self.path}"
        return f"{self.severity}: {where}: {self.message}"


@dataclass(frozen=True)
class Origin:
    """Where one resolved value came from: the layer (file path or `"cli"`) and
    the qualified name of the group that set it (`None`: the model itself)."""

    key: str
    source: str
    group: str | None


@dataclass(frozen=True)
class ResolvedModel:
    """A model by qualified name: its `ModelConfig` and the `Origin` of each key."""

    name: str
    config: ModelConfig
    origins: tuple[Origin, ...]


@dataclass(frozen=True)
class ModelCatalog:
    """Every model the merged layers define, plus `default_model` and any warnings."""

    models: Mapping[str, ResolvedModel]
    default_model: str | None
    warnings: tuple[ConfigProblem, ...]

    def names(self) -> tuple[str, ...]:
        """All qualified names, in file order."""
        return tuple(self.models)

    def find(self, name: str) -> tuple[str, ...]:
        """Qualified names matching `name`. A name with `:` is an exact qualified name;
        one without is a bare search over last segments (top-level `y` and `g:y`
        both match `y`). The caller decides on 0, 1, or many."""
        return _match(self.models, name)

    def resolve(self, qualified: str) -> ModelConfig:
        """The model named exactly `qualified`; `ConfigError` if there is none."""
        return self._get(qualified).config

    def explain(self, qualified: str) -> tuple[Origin, ...]:
        """The layer and group each value of `qualified` came from."""
        return self._get(qualified).origins

    def _get(self, qualified: str) -> ResolvedModel:
        found = self.models.get(qualified)
        if found is None:
            raise ConfigError(f"model {qualified!r} is not in your config")
        return found


@dataclass(frozen=True)
class _Leaf:
    value: object
    source: str


class _Invalid:
    """The value of a setting already reported as invalid: present, so nothing
    downstream (a model inheriting it) reports it again or a 'missing' on top."""


_INVALID = _Invalid()

type _Tree = dict[str, _Tree | _Leaf]
type _Settings = dict[str, tuple[_Leaf, str | None]]  # key -> (value, group that set it)
type _Kinds = dict[tuple[str, ...], tuple[str, str]]  # path -> ("model" | "group", first layer)


class _Obj(dict[str, object]):
    """A parsed JSON object that remembers its duplicate keys."""

    def __init__(self) -> None:
        super().__init__()
        self.dups: list[str] = []


class _Problems:
    def __init__(self) -> None:
        self.found: list[ConfigProblem] = []

    def add(self, severity: Severity, message: str, source: str | None, path: str) -> None:
        self.found.append(ConfigProblem(severity, message, source, path))

    def errors_since(self, mark: int) -> bool:
        return any(p.severity == "error" for p in self.found[mark:])


def _suggest(key: str, known: tuple[str, ...]) -> str:
    close = difflib.get_close_matches(key, known, n=1)
    return f"; did you mean {close[0]!r}?" if close else f"; known keys are {list(known)}"


def _source_of(node: _Tree | _Leaf) -> str | None:
    if isinstance(node, _Leaf):
        return node.source
    for child in node.values():
        found = _source_of(child)
        if found is not None:
            return found
    return None


def _dotted(path: tuple[str, ...]) -> str:
    return "models." + ".models.".join(path)


def _leafify(obj: dict[str, object], source: str, path: str, problems: _Problems) -> _Tree:
    for key in obj.dups if isinstance(obj, _Obj) else ():
        problems.add("error", f"duplicate key {key!r}", source, path)
    tree: _Tree = {}
    for key, value in obj.items():
        here = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            tree[key] = _leafify(cast("dict[str, object]", value), source, here, problems)
        else:
            tree[key] = _Leaf(value, source)
    return tree


def _load_layer(source: str, text: str, problems: _Problems) -> _Tree | None:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        obj = _Obj()
        for k, v in items:
            if k in obj:
                obj.dups.append(k)
            obj[k] = v
        return obj

    try:
        data: object = json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as e:
        problem = ConfigProblem("error", f"invalid JSON: {e.msg}", source, "", e.lineno, e.colno)
        problems.found.append(problem)
        return None
    if not isinstance(data, dict):
        problems.add("error", f"must be a JSON object, not {type(data).__name__}", source, "")
        return None
    return _leafify(cast("dict[str, object]", data), source, "", problems)


def _merge(base: _Tree, over: _Tree) -> _Tree:
    """Objects by key; anything else replaces. A null stays as a tombstone: it also
    drops what a group passes down. Setting one of `api_key`/`api_key_env` drops the other."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, _Leaf):
            out[key] = value
        else:
            inherited = out.get(key)
            out[key] = _merge(inherited if isinstance(inherited, dict) else {}, value)
    for key, other in _EXCLUSIVE.items():
        if _is_set(over.get(key)) and other not in over:
            out.pop(other, None)
    return out


def _is_set(node: _Tree | _Leaf | None) -> bool:
    return node is not None and not (isinstance(node, _Leaf) and node.value is None)


def _forget(kinds: _Kinds, path: tuple[str, ...]) -> None:
    for known in [k for k in kinds if k[: len(path)] == path]:
        del kinds[known]


def _merge_layer(base: _Tree, over: _Tree, kinds: _Kinds, problems: _Problems, layer: str) -> _Tree:
    out = _merge(
        {k: v for k, v in base.items() if k != "models"},
        {k: v for k, v in over.items() if k != "models"},
    )
    if "models" in over:
        out["models"] = _merge_models(
            base.get("models"), over["models"], kinds, (), problems, layer
        )
    elif "models" in base:
        out["models"] = base["models"]
    return out


def _merge_models(  # noqa: PLR0913, PLR0917 - the layer's whole merge state
    base: _Tree | _Leaf | None,
    over: _Tree | _Leaf,
    kinds: _Kinds,
    path: tuple[str, ...],
    problems: _Problems,
    layer: str,
) -> _Tree | _Leaf:
    """Merge two `models` objects. Each layer says per name whether it is a model or
    a group (it has a `models` key); a name that is both across layers is an error."""
    if isinstance(over, _Leaf):
        if over.value is None:
            _forget(kinds, path)
        return over
    out = dict(base) if isinstance(base, dict) else {}
    for name, node in over.items():
        here = (*path, name)
        if isinstance(node, _Leaf):
            if node.value is None:
                _forget(kinds, here)
            out[name] = node
            continue
        source = _source_of(node) or layer
        prev = kinds.get(here)
        if prev is not None and prev[0] == "model" and "models" in node:
            msg = f"{name!r} is a model in {prev[1]} and a group in {source}"
            problems.add("error", msg, source, _dotted(here))
            continue
        # The first layer to define a name sets its kind (`models` makes a group);
        # later layers without `models` just tune whichever it is.
        kind = prev[0] if prev is not None else ("group" if "models" in node else "model")
        kinds.setdefault(here, (kind, source))
        existing = out.get(name)
        mine = existing if isinstance(existing, dict) else {}
        if kind == "model":
            out[name] = _merge(mine, node)
            continue
        group = _merge(
            {k: v for k, v in mine.items() if k != "models"},
            {k: v for k, v in node.items() if k != "models"},
        )
        if "models" in node:
            group["models"] = _merge_models(
                mine.get("models"), node["models"], kinds, here, problems, layer
            )
        elif "models" in mine:
            group["models"] = mine["models"]
        out[name] = group
    return out


def _combine(inherited: _Settings, own: _Settings) -> _Settings:
    """`own` over `inherited`: a null removes the key, and setting one of
    `api_key`/`api_key_env` removes the other."""
    out = dict(inherited)
    for key, value in own.items():
        if value[0].value is None:
            out.pop(key, None)
            continue
        out[key] = value
        other = _EXCLUSIVE.get(key)
        if other is not None and other not in own:
            out.pop(other, None)
    return out


def _match(names: Iterable[str], name: str) -> tuple[str, ...]:
    """A name with ':' is an exact qualified name; one without is a bare search."""
    if ":" in name:
        return tuple(q for q in names if q == name)
    return tuple(q for q in names if q.rsplit(":", 1)[-1] == name)


class _Resolver:
    def __init__(self, problems: _Problems, environ: Mapping[str, str] | None, kinds: _Kinds):
        self.problems = problems
        self.environ = environ
        self.kinds = kinds
        self.models: dict[str, ResolvedModel] = {}
        self.declared: list[str] = []

    def checked(self, key: str, value: _Tree | _Leaf, label: str, here: str) -> _Leaf:
        """`value` if it is a good setting; else report it once and return a placeholder."""
        source = _source_of(value) or CLI
        bad = _Leaf(_INVALID, source)
        if not isinstance(value, _Leaf) or not isinstance(value.value, str):
            self.problems.add("error", f"{key!r} must be a string", source, here)
            return bad
        text = value.value
        if not text:
            self.problems.add("error", f"{key!r} must not be empty", source, here)
        elif key == "api_key_env" and not _VAR_NAME.fullmatch(text):
            msg = (
                f"api_key_env of {label!r} is not a variable name "
                '(did you paste the key itself? use api_key_env: "NAME")'
            )
            self.problems.add("error", msg, source, here)
        elif key == "provider" and text not in _PROVIDERS:
            msg = f"unknown provider {text!r}; known providers are {list(_PROVIDERS)}"
            self.problems.add("error", msg, source, here)
        else:
            return value
        return bad

    def settings(self, node: _Tree, path: str, label: str, group: str | None) -> _Settings:
        """The model keys set directly on `node`, each checked. A null passes through."""
        out: _Settings = {}
        for key, value in node.items():
            if key == "models":
                continue
            here = f"{path}.{key}"
            if key == _RENAMED_KEY:
                self.problems.add("error", _RENAMED_MESSAGE, _source_of(value), here)
            elif key not in _MODEL_KEYS:
                msg = f"unknown key {key!r}{_suggest(key, (*_MODEL_KEYS, 'models'))}"
                self.problems.add("error", msg, _source_of(value), here)
            elif isinstance(value, _Leaf) and value.value is None:
                out[key] = (value, group)
            else:
                out[key] = (self.checked(key, value, label, here), group)
        if all(k in out and out[k][0].value is not None for k in _EXCLUSIVE):
            msg = f"{label!r} sets both api_key and api_key_env; keep one"
            self.problems.add("error", msg, _source_of(node), path)
        return out

    def walk(self, models: _Tree, path: str, prefix: tuple[str, ...], inherited: _Settings) -> None:
        for name, node in models.items():
            here = f"{path}.{name}"
            if isinstance(node, _Leaf) and node.value is None:
                continue
            if not name:
                self.problems.add("error", "name must not be empty", _source_of(node), here)
            elif ":" in name:
                msg = f"name {name!r} must not contain ':'"
                self.problems.add("error", msg, _source_of(node), here)
            elif not isinstance(node, dict):
                self.problems.add("error", "must be an object", _source_of(node), here)
            elif "models" in node:
                self.group(name, node, here, prefix, inherited)
            else:
                self.model(name, node, here, prefix, inherited)

    def group(
        self, name: str, node: _Tree, here: str, prefix: tuple[str, ...], inherited: _Settings
    ) -> None:
        sub = node["models"]
        if isinstance(sub, _Leaf) and sub.value is None:
            sub = {}  # "models": null drops the group's models
        if not isinstance(sub, dict):
            self.problems.add("error", "'models' must be an object", _source_of(sub), here)
            return
        qualified = ":".join((*prefix, name))
        own = self.settings(node, here, qualified, qualified)
        self.walk(sub, f"{here}.models", (*prefix, name), _combine(inherited, own))

    def model(
        self, name: str, node: _Tree, here: str, prefix: tuple[str, ...], inherited: _Settings
    ) -> None:
        qualified = ":".join((*prefix, name))
        self.declared.append(qualified)
        mark = len(self.problems.found)
        merged = _combine(inherited, self.settings(node, here, qualified, None))
        source = self.kinds.get((*prefix, name), ("model", _source_of(node) or CLI))[1]
        for required in ("model", "base_url"):
            if required not in merged:
                msg = f"model {qualified!r} (from {source}) is missing {required!r}"
                self.problems.add("error", msg, source, here)
        poisoned = any(isinstance(leaf.value, _Invalid) for leaf, _ in merged.values())
        if poisoned or self.problems.errors_since(mark):
            return
        values = {k: cast("str", leaf.value) for k, (leaf, _) in merged.items()}
        self.warn(qualified, merged, values, here)
        try:
            config = ModelConfig(
                values["model"],
                values["base_url"],
                values.get("api_key_env"),
                values.get("provider", "openai"),
                values.get("api_key"),
            )
        except ConfigError as e:
            self.problems.add("error", str(e), source, here)
            return
        origins = [Origin(k, leaf.source, group) for k, (leaf, group) in merged.items()]
        if "provider" not in merged:
            origins.append(Origin("provider", "default", None))
        self.models[qualified] = ResolvedModel(qualified, config, tuple(origins))

    def warn(self, qualified: str, merged: _Settings, values: dict[str, str], here: str) -> None:
        if "api_key" in values:
            msg = (
                f"model {qualified!r} holds a literal api_key; "
                "name an environment variable with api_key_env instead"
            )
            self.problems.add("warning", msg, merged["api_key"][0].source, f"{here}.api_key")
        var = values.get("api_key_env")
        if var is not None and self.environ is not None and not self.environ.get(var):
            msg = f"environment variable {var} (api_key_env of {qualified!r}) is not set"
            source = merged["api_key_env"][0].source
            self.problems.add("warning", msg, source, f"{here}.api_key_env")


def _read_layers(
    user: Path | None, project: Path | None, cli: Mapping[str, object] | None, problems: _Problems
) -> tuple[_Tree, _Kinds]:
    texts: list[tuple[str, str]] = []
    for path in (user, project):
        if path is None:
            continue
        try:
            texts.append((str(path), path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError) as e:
            problems.add("error", f"cannot read: {e}", str(path), "")
    if cli is not None:
        try:
            texts.append((CLI, json.dumps(cli)))
        except (TypeError, ValueError) as e:
            problems.add("error", f"overrides are not JSON: {e}", CLI, "")
    merged: _Tree = {}
    kinds: _Kinds = {}
    for source, text in texts:
        tree = _load_layer(source, text, problems)
        if tree is not None:
            merged = _merge_layer(merged, tree, kinds, problems, source)
    return merged, kinds


def _default_model(merged: _Tree, declared: list[str], problems: _Problems) -> str | None:
    node = merged.get("default_model")
    if node is None or (isinstance(node, _Leaf) and node.value is None):
        return None
    source = _source_of(node)
    if not (isinstance(node, _Leaf) and isinstance(node.value, str) and node.value):
        problems.add("error", "'default_model' must be a string", source, "default_model")
        return None
    name = node.value
    found = _match(declared, name)
    if not found:
        problems.add(
            "error", f"default_model {name!r} is not in your config", source, "default_model"
        )
    elif len(found) > 1:
        msg = f"default_model {name!r} is ambiguous; use one of {list(found)}"
        problems.add("error", msg, source, "default_model")
    return name


def _build(
    user: Path | None,
    project: Path | None,
    cli: Mapping[str, object] | None,
    environ: Mapping[str, str] | None,
) -> tuple[ModelCatalog, tuple[ConfigProblem, ...]]:
    problems = _Problems()
    merged, kinds = _read_layers(user, project, cli, problems)
    for key, value in merged.items():
        if key not in _TOP_KEYS:
            msg = f"unknown key {key!r}{_suggest(key, _TOP_KEYS)}"
            problems.add("error", msg, _source_of(value), key)
    resolver = _Resolver(problems, environ, kinds)
    models = merged.get("models", {})
    if isinstance(models, _Leaf) and models.value is None:
        models = {}
    if isinstance(models, dict):
        resolver.walk(models, "models", (), {})
    else:
        problems.add("error", "'models' must be an object", _source_of(models), "models")
    default = _default_model(merged, resolver.declared, problems)
    found = tuple(problems.found)
    warnings = tuple(p for p in found if p.severity == "warning")
    return ModelCatalog(resolver.models, default, warnings), found


def check_config(
    user: Path | None = None,
    project: Path | None = None,
    cli: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[ConfigProblem, ...]:
    """Every problem in the layers, not just the first. Layers: `user` < `project` < `cli`.

    With `environ`, a model whose `api_key_env` names an unset variable is a
    warning. An empty result means the layers are sound.
    """
    return _build(user, project, cli, environ)[1]


def read_models(
    user: Path | None = None,
    project: Path | None = None,
    cli: Mapping[str, object] | None = None,
) -> ModelCatalog:
    """Merge the layers (`user` < `project` < `cli`; each may be absent) and resolve.

    Raises `ConfigError` listing every error; warnings ride on the catalog.
    """
    catalog, problems = _build(user, project, cli, None)
    errors = [p for p in problems if p.severity == "error"]
    if errors:
        raise ConfigError("\n".join(str(p) for p in errors))
    return catalog
