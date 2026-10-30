"""Reading model configuration from layered JSON files (D13).

Layers, nearest wins: user, project, CLI overrides. Their raw trees are merged
first (objects by key; lists and scalars replace; `null` drops), then groups
and models are resolved, so a project can tune a user's model by qualified
name. Finding the files and trusting them is not done here.
"""

import difflib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from tau_next.config import ConfigError, ModelConfig

Severity = Literal["error", "warning"]

CLI = "cli"
_MODEL_KEYS = ("model", "base_url", "api_key_env", "api_key", "provider")
_TOP_KEYS = ("models", "default_model")
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
        """Qualified names matching `name`: the exact qualified name, else every
        name whose last segment is `name`. The caller decides on 0, 1, or many."""
        if name in self.models:
            return (name,)
        return tuple(q for q in self.models if q.rsplit(":", 1)[-1] == name)

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


type _Tree = dict[str, _Tree | _Leaf]
type _Settings = dict[str, tuple[_Leaf, str | None]]  # key -> (value, group that set it)


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


def _leafify(
    obj: dict[str, object],
    source: str,
    path: str,
    dups: dict[int, list[str]],
    problems: _Problems,
) -> _Tree:
    for key in dups.get(id(obj), []):
        problems.add("error", f"duplicate key {key!r}", source, path)
    tree: _Tree = {}
    for key, value in obj.items():
        here = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            tree[key] = _leafify(cast("dict[str, object]", value), source, here, dups, problems)
        else:
            tree[key] = _Leaf(value, source)
    return tree


def _load_layer(source: str, text: str, problems: _Problems) -> _Tree | None:
    dups: dict[int, list[str]] = {}

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        obj: dict[str, object] = {}
        for k, v in items:
            if k in obj:
                dups.setdefault(id(obj), []).append(k)
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
    return _leafify(cast("dict[str, object]", data), source, "", dups, problems)


def _merge(base: _Tree, over: _Tree) -> _Tree:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, _Leaf):
            out[key] = value  # a null stays as a tombstone: it also drops what a group passes down
        else:
            inherited = out.get(key)
            out[key] = _merge(inherited if isinstance(inherited, dict) else {}, value)
    return out


def _combine(inherited: _Settings, own: _Settings) -> _Settings:
    """`own` over `inherited`; a null in `own` removes the inherited key."""
    return {k: v for k, v in {**inherited, **own}.items() if v[0].value is not None}


class _Resolver:
    def __init__(self, problems: _Problems, environ: Mapping[str, str] | None) -> None:
        self.problems = problems
        self.environ = environ
        self.models: dict[str, ResolvedModel] = {}

    def settings(self, node: _Tree, path: str, group: str | None) -> _Settings:
        """The model keys set directly on `node`, checked for known names and string type."""
        out: _Settings = {}
        for key, value in node.items():
            if key == "models":
                continue
            here = f"{path}.{key}"
            if isinstance(value, _Leaf) and value.value is None:
                out[key] = (value, group)
            elif key == _RENAMED_KEY:
                self.problems.add("error", _RENAMED_MESSAGE, _source_of(value), here)
            elif key not in _MODEL_KEYS:
                msg = f"unknown key {key!r}{_suggest(key, (*_MODEL_KEYS, 'models'))}"
                self.problems.add("error", msg, _source_of(value), here)
            elif not isinstance(value, _Leaf) or not isinstance(value.value, str):
                self.problems.add("error", f"{key!r} must be a string", _source_of(value), here)
            elif not value.value:
                self.problems.add("error", f"{key!r} must not be empty", value.source, here)
            else:
                out[key] = (value, group)
        return out

    def walk(self, models: _Tree, path: str, prefix: tuple[str, ...], inherited: _Settings) -> None:
        for name, node in models.items():
            here = f"{path}.{name}"
            if isinstance(node, _Leaf) and node.value is None:
                continue
            if ":" in name:
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
        if not isinstance(sub, dict):
            self.problems.add("error", "'models' must be an object", _source_of(sub), here)
            return
        own = self.settings(node, here, ":".join((*prefix, name)))
        self.walk(sub, f"{here}.models", (*prefix, name), _combine(inherited, own))

    def model(
        self, name: str, node: _Tree, here: str, prefix: tuple[str, ...], inherited: _Settings
    ) -> None:
        qualified = ":".join((*prefix, name))
        mark = len(self.problems.found)
        merged = _combine(inherited, self.settings(node, here, None))
        source = _source_of(node) or CLI
        for required in ("model", "base_url"):
            if required not in merged:
                msg = f"model {qualified!r} is missing {required!r}"
                self.problems.add("error", msg, source, here)
        values = {k: cast("str", leaf.value) for k, (leaf, _) in merged.items()}
        self.check_values(qualified, merged, values, here)
        if self.problems.errors_since(mark):
            return
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
        origins = tuple(Origin(k, leaf.source, group) for k, (leaf, group) in merged.items())
        self.models[qualified] = ResolvedModel(qualified, config, origins)

    def check_values(
        self, qualified: str, merged: _Settings, values: dict[str, str], here: str
    ) -> None:
        if "provider" in values and values["provider"] not in _PROVIDERS:
            msg = f"unknown provider {values['provider']!r}; known providers are {list(_PROVIDERS)}"
            self.problems.add("error", msg, merged["provider"][0].source, f"{here}.provider")
        if "api_key" in values:
            msg = (
                f"model {qualified!r} holds a literal api_key; "
                "name an environment variable with api_key_env instead"
            )
            self.problems.add("warning", msg, merged["api_key"][0].source, f"{here}.api_key")
        var = values.get("api_key_env")
        if var is not None and self.environ is not None and not self.environ.get(var):
            msg = f"environment variable {var} (api_key_env of {qualified!r}) is not set"
            self.problems.add(
                "warning", msg, merged["api_key_env"][0].source, f"{here}.api_key_env"
            )


def _read_layers(
    user: Path | None, project: Path | None, cli: Mapping[str, object] | None, problems: _Problems
) -> _Tree:
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
    for source, text in texts:
        tree = _load_layer(source, text, problems)
        if tree is not None:
            merged = _merge(merged, tree)
    return merged


def _default_model(merged: _Tree, problems: _Problems) -> str | None:
    node = merged.get("default_model")
    if node is None or (isinstance(node, _Leaf) and node.value is None):
        return None
    if isinstance(node, _Leaf) and isinstance(node.value, str) and node.value:
        return node.value
    problems.add("error", "'default_model' must be a string", _source_of(node), "default_model")
    return None


def _build(
    user: Path | None,
    project: Path | None,
    cli: Mapping[str, object] | None,
    environ: Mapping[str, str] | None,
) -> tuple[ModelCatalog, tuple[ConfigProblem, ...]]:
    problems = _Problems()
    merged = _read_layers(user, project, cli, problems)
    for key, value in merged.items():
        if key not in _TOP_KEYS and not (isinstance(value, _Leaf) and value.value is None):
            msg = f"unknown key {key!r}{_suggest(key, _TOP_KEYS)}"
            problems.add("error", msg, _source_of(value), key)
    resolver = _Resolver(problems, environ)
    models = merged.get("models", {})
    if isinstance(models, _Leaf) and models.value is None:
        models = {}
    if isinstance(models, dict):
        resolver.walk(models, "models", (), {})
    else:
        problems.add("error", "'models' must be an object", _source_of(models), "models")
    default = _default_model(merged, problems)
    found = tuple(problems.found)
    catalog = ModelCatalog(
        resolver.models, default, tuple(p for p in found if p.severity == "warning")
    )
    if default is not None and not catalog.find(default) and not problems.errors_since(0):
        source = _source_of(merged["default_model"])
        problem = ConfigProblem(
            "error", f"default_model {default!r} is not in your config", source, "default_model"
        )
        found = (*found, problem)
    return catalog, found


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
