"""Reading model configuration from layered JSON files (D13).

Layers, nearest wins: user, project, CLI overrides. Their raw trees are merged
first (objects by key; lists and scalars replace; `null` drops), then groups
and models are resolved, so a project can tune a user's model by qualified
name. Finding the files and trusting them is not done here.
"""

import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from tau._config import ConfigError, ModelConfig, Price, key_problem, url_problem
from tau._config_keys import extensions, load_switches, system_prompt, tui_settings
from tau._config_tree import (
    ConfigProblem,
    Leaf,
    Problems,
    Tree,
    is_set,
    plain,
    source_of,
    suggest,
)
from tau._options import make_options, option_fields
from tau._protocols import Provider
from tau._tools import assemble_tools
from tau._values import Context, DefinitionError

CLI = "cli"
_MODEL_KEYS = (
    "model",
    "base_url",
    "api_key_env",
    "api_key",
    "provider",
    "max_tokens",
    "context_window",
    "price",
    "options",
)
_INT_KEYS = ("max_tokens", "context_window")
_OBJECT_KEYS = ("options", "price")  # settings that are objects, merged by key
_TOP_KEYS = ("models", "default_model", "extensions", "load", "system_prompt", "tui")
_EXCLUSIVE = {"api_key": "api_key_env", "api_key_env": "api_key"}  # one setting, two spellings
_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RENAMED_KEY = "backend"  # rules: allow(backend): 0.15's key name, which D13 rejects by name
_RENAMED_MESSAGE = "`backend` is now `provider`"  # rules: allow(backend): 0.15's key name


@dataclass(frozen=True)
class LayerText:
    """A layer already read: its text and the `source` (file path) problems name.
    Accepted wherever a layer takes a `Path`, so a caller that read and showed the
    text reads exactly that text."""

    source: str
    text: str


Layer = Path | LayerText
"""One config layer: the `Path` of a file, or a `LayerText` already read."""


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
    """Every model the merged layers define, plus `default_model`, any warnings, and the
    `extensions` options the layers set (`{"step_limit": {"limit": 10}}`, merged per option;
    D16 section 4). Which options an extension has is its own to say (`check_config`).
    `load` is the merged top-level `load` (source key to on or off; D19 section 3) and
    `load_sources` the layer (file path) that set each key: what the extension loader reads.
    `system_prompt` is the merged top-level key of that name (D20: the nearest layer's base text;
    `None`: no layer sets one, so the built-in text is used) and `system_prompt_source` the layer
    that set it (`CLI` for `--system-prompt`). `tui` is the merged `tui`
    object (D23: `theme`; only the user's file and the command line set it, a project's is an
    error)."""

    models: Mapping[str, ResolvedModel]
    default_model: str | None
    warnings: tuple[ConfigProblem, ...]
    extensions: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: {})
    load: Mapping[str, bool] = field(default_factory=lambda: {})
    load_sources: Mapping[str, str] = field(default_factory=lambda: {})
    system_prompt: str | None = None
    system_prompt_source: str | None = None
    tui: Mapping[str, str] = field(default_factory=lambda: {})

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


class _Invalid:
    """The value of a setting already reported as invalid: present, so nothing
    downstream (a model inheriting it) reports it again or a 'missing' on top."""


_INVALID = _Invalid()

type _Settings = dict[str, tuple[Leaf, str | None]]  # key -> (value, group that set it)
type _Kinds = dict[tuple[str, ...], tuple[str, str]]  # path -> ("model" | "group", first layer)


class _Obj(dict[str, object]):
    """A parsed JSON object that remembers its duplicate keys."""

    def __init__(self) -> None:
        super().__init__()
        self.dups: list[str] = []


def _dotted(path: tuple[str, ...]) -> str:
    return "models." + ".models.".join(path)


def _leafify(obj: dict[str, object], source: str, path: str, problems: Problems) -> Tree:
    for key in obj.dups if isinstance(obj, _Obj) else ():
        problems.add("error", f"duplicate key {key!r}", source, path)
    tree: Tree = {}
    for key, value in obj.items():
        here = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            tree[key] = _leafify(cast("dict[str, object]", value), source, here, problems)
        else:
            tree[key] = Leaf(value, source)
    return tree


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is out of range")
    return value


def _bounded_int(text: str) -> int:
    value = int(text)
    if not -(2**63) <= value < 2**63:
        raise ValueError("an integer is out of range (beyond 64 bits)")
    return value


def _load_layer(source: str, text: str, problems: Problems) -> Tree | None:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        obj = _Obj()
        for k, v in items:
            if k in obj:
                obj.dups.append(k)
            obj[k] = v
        return obj

    try:
        data: object = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=_no_constant,
            parse_float=_finite_float,
            parse_int=_bounded_int,
        )
    except json.JSONDecodeError as e:
        problem = ConfigProblem("error", f"invalid JSON: {e.msg}", source, "", e.lineno, e.colno)
        problems.found.append(problem)
        return None
    except RecursionError:
        problems.add("error", "invalid JSON: nested too deeply", source, "")
        return None
    except ValueError as e:
        problems.add("error", f"invalid JSON: {e}", source, "")
        return None
    if not isinstance(data, dict):
        problems.add("error", f"must be a JSON object, not {type(data).__name__}", source, "")
        return None
    try:
        return _leafify(cast("dict[str, object]", data), source, "", problems)
    except RecursionError:
        problems.add("error", "nested too deeply", source, "")
        return None


def _merge(base: Tree, over: Tree) -> Tree:
    """Objects by key; anything else replaces. A null stays as a tombstone: it also
    drops what a group passes down."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, Leaf):
            out[key] = value
        else:
            inherited = out.get(key)
            out[key] = _merge(inherited if isinstance(inherited, dict) else {}, value)
    return out


def _merge_settings(base: Tree, over: Tree) -> Tree:
    """`_merge` for the keys of a model or group: setting one of `api_key`/`api_key_env`
    drops the other."""
    out = _merge(base, over)
    for key, other in _EXCLUSIVE.items():
        if is_set(over.get(key)) and other not in over:
            out.pop(other, None)
    return out


def _forget(kinds: _Kinds, path: tuple[str, ...]) -> None:
    for known in [k for k in kinds if k[: len(path)] == path]:
        del kinds[known]


def _without_models(node: Tree) -> Tree:
    return {k: v for k, v in node.items() if k != "models"}


@dataclass
class _LayerMerge:
    """Folding one layer into what the layers before it made. Each layer says per name
    whether it is a model or a group (it has a `models` key); a name that is both across
    layers is an error."""

    kinds: _Kinds
    problems: Problems
    layer: str

    def node(self, base: Tree, over: Tree, path: tuple[str, ...]) -> Tree:
        """A group (or the top level) over another: its settings, then its `models`."""
        out = _merge_settings(_without_models(base), _without_models(over))
        if "models" in over:
            out["models"] = self.models(base.get("models"), over["models"], path)
        elif "models" in base:
            out["models"] = base["models"]
        return out

    def models(
        self, base: Tree | Leaf | None, over: Tree | Leaf, path: tuple[str, ...]
    ) -> Tree | Leaf:
        if isinstance(over, Leaf):
            if over.value is None:
                _forget(self.kinds, path)
            return over
        out = dict(base) if isinstance(base, dict) else {}
        for name, node in over.items():
            merged = self.named(out.get(name), node, (*path, name))
            if merged is not None:
                out[name] = merged
        return out

    def named(
        self, existing: Tree | Leaf | None, node: Tree | Leaf, here: tuple[str, ...]
    ) -> Tree | Leaf | None:
        """One name's merge; `None`: it is a model here and a group there (reported)."""
        if isinstance(node, Leaf):
            if node.value is None:
                _forget(self.kinds, here)
            return node
        source = source_of(node) or self.layer
        prev = self.kinds.get(here)
        if prev is not None and prev[0] == "model" and "models" in node:
            msg = f"{here[-1]!r} is a model in {prev[1]} and a group in {source}"
            self.problems.add("error", msg, source, _dotted(here))
            return None
        # The first layer to define a name sets its kind (`models` makes a group);
        # later layers without `models` just tune whichever it is.
        kind = prev[0] if prev is not None else ("group" if "models" in node else "model")
        self.kinds.setdefault(here, (kind, source))
        mine = existing if isinstance(existing, dict) else {}
        return _merge_settings(mine, node) if kind == "model" else self.node(mine, node, here)


def _combine(inherited: _Settings, own: _Settings) -> _Settings:
    """`own` over `inherited`: a null removes the key, and setting one of
    `api_key`/`api_key_env` removes the other. `options` and `price` merge as objects."""
    out = dict(inherited)
    for key, (leaf, group) in own.items():
        if leaf.value is None:
            out.pop(key, None)
            continue
        before: object = out[key][0].value if key in out else None
        mine: object = leaf.value
        if key in _OBJECT_KEYS and isinstance(before, dict) and isinstance(mine, dict):
            merged = _merge(cast("Tree", before), cast("Tree", mine))
            out[key] = (Leaf(merged, leaf.source), group)
        else:
            out[key] = (leaf, group)
        other = _EXCLUSIVE.get(key)
        if other is not None and other not in own:
            out.pop(other, None)
    return out


def _match(names: Iterable[str], name: str) -> tuple[str, ...]:
    """A name with ':' is an exact qualified name; one without is a bare search."""
    if ":" in name:
        return tuple(q for q in names if q == name)
    return tuple(q for q in names if q.rsplit(":", 1)[-1] == name)


def _model_config(texts: dict[str, str], merged: _Settings) -> ModelConfig:
    """The `ModelConfig` of a model's merged settings; `ConfigError` if it refuses them."""
    objects = {
        k: cast("dict[str, object]", plain(cast("Tree", merged[k][0].value)))
        for k in _OBJECT_KEYS
        if k in merged
    }
    numbers = {k: cast("int", merged[k][0].value) for k in _INT_KEYS if k in merged}
    return ModelConfig(
        texts["model"],
        texts.get("base_url"),
        texts.get("api_key_env"),
        texts.get("provider", "openai"),
        texts.get("api_key"),
        numbers.get("max_tokens"),
        objects.get("options", {}),
        numbers.get("context_window"),
        Price.of(objects["price"]) if "price" in objects else None,
    )


class _Resolver:
    def __init__(
        self,
        problems: Problems,
        environ: Mapping[str, str] | None,
        kinds: _Kinds,
        providers: Mapping[str, Provider] | None,
    ):
        self.problems = problems
        self.environ = environ
        self.kinds = kinds
        self.providers = providers
        self.where: dict[str, tuple[str, str]] = {}  # qualified name -> (dotted path, source)
        self.models: dict[str, ResolvedModel] = {}
        self.declared: list[str] = []

    def checked(self, key: str, value: Tree | Leaf, label: str, here: str) -> Leaf:
        """`value` if it is a good setting; else report it once and return a placeholder.
        What `ModelConfig` checks of the resolved value (empty, a range) is left to it."""
        source = source_of(value) or CLI
        bad = Leaf(_INVALID, source)
        text = value.value if isinstance(value, Leaf) else None
        if key in _INT_KEYS:
            if not isinstance(text, int):
                self.problems.add("error", f"{key!r} must be a positive integer", source, here)
                return bad
        elif not isinstance(text, str):
            self.problems.add("error", f"{key!r} must be a string", source, here)
            return bad
        if key == "api_key_env" and not _VAR_NAME.fullmatch(cast("str", text)):
            msg = (
                f"api_key_env of {label!r} is not a variable name "
                '(did you paste the key itself? use api_key_env: "NAME")'
            )
            self.problems.add("error", msg, source, here)
        elif key == "base_url" and (bad_url := url_problem(cast("str", text))) is not None:
            self.problems.add("error", bad_url, source, here)
        elif key == "provider" and self.providers is not None and text not in self.providers:
            msg = f"unknown provider {text!r}; known providers are {sorted(self.providers)}"
            self.problems.add("error", msg, source, here)
        else:
            return value if isinstance(value, Leaf) else bad
        return bad

    def object_setting(self, key: str, value: Tree | Leaf, here: str) -> Leaf:
        """`options` (the provider's own settings) or `price` as an object tree (the loader
        never looks inside; a `null` inside stays until the layers are combined)."""
        source = source_of(value) or CLI
        if isinstance(value, Leaf):
            self.problems.add("error", f"{key!r} must be an object", source, here)
            return Leaf(_INVALID, source)
        return Leaf(value, source)

    def settings(self, node: Tree, path: str, label: str, group: str | None) -> _Settings:
        """The model keys set directly on `node`, each checked. A null passes through."""
        out: _Settings = {}
        for key, value in node.items():
            if key == "models":
                continue
            here = f"{path}.{key}"
            if key == _RENAMED_KEY:
                self.problems.add("error", _RENAMED_MESSAGE, source_of(value), here)
            elif key not in _MODEL_KEYS:
                msg = f"unknown key {key!r}{suggest(key, (*_MODEL_KEYS, 'models'))}"
                self.problems.add("error", msg, source_of(value), here)
            elif isinstance(value, Leaf) and value.value is None:
                out[key] = (value, group)
            elif key in _OBJECT_KEYS:
                out[key] = (self.object_setting(key, value, here), group)
            else:
                out[key] = (self.checked(key, value, label, here), group)
        if all(k in out and out[k][0].value is not None for k in _EXCLUSIVE):
            msg = f"{label!r} sets both api_key and api_key_env; keep one"
            self.problems.add("error", msg, source_of(node), path)
        return out

    def walk(self, models: Tree, path: str, prefix: tuple[str, ...], inherited: _Settings) -> None:
        for name, node in models.items():
            here = f"{path}.{name}"
            if isinstance(node, Leaf) and node.value is None:
                continue
            if not name:
                self.problems.add("error", "name must not be empty", source_of(node), here)
            elif any(unicodedata.category(c) == "Cc" for c in name):
                msg = f"name {name!r} must not contain control characters"
                self.problems.add("error", msg, source_of(node), here)
            elif ":" in name:
                msg = f"name {name!r} must not contain ':'"
                self.problems.add("error", msg, source_of(node), here)
            elif not isinstance(node, dict):
                self.problems.add("error", "must be an object", source_of(node), here)
            elif "models" in node:
                self.group(name, node, here, prefix, inherited)
            else:
                self.model(name, node, here, prefix, inherited)

    def group(
        self, name: str, node: Tree, here: str, prefix: tuple[str, ...], inherited: _Settings
    ) -> None:
        sub = node["models"]
        if isinstance(sub, Leaf) and sub.value is None:
            sub = {}  # "models": null drops the group's models
        if not isinstance(sub, dict):
            self.problems.add("error", "'models' must be an object", source_of(sub), here)
            return
        qualified = ":".join((*prefix, name))
        own = self.settings(node, here, qualified, qualified)
        self.walk(sub, f"{here}.models", (*prefix, name), _combine(inherited, own))

    def model(
        self, name: str, node: Tree, here: str, prefix: tuple[str, ...], inherited: _Settings
    ) -> None:
        qualified = ":".join((*prefix, name))
        self.declared.append(qualified)
        mark = len(self.problems.found)
        merged = _combine(inherited, self.settings(node, here, qualified, None))
        source = self.kinds.get((*prefix, name), ("model", source_of(node) or CLI))[1]
        if "model" not in merged:
            msg = f"model {qualified!r} (from {source}) is missing 'model'"
            self.problems.add("error", msg, source, here)
        poisoned = any(isinstance(leaf.value, _Invalid) for leaf, _ in merged.values())
        if poisoned or self.problems.errors_since(mark):
            return
        texts = {
            k: cast("str", leaf.value)
            for k, (leaf, _) in merged.items()
            if k not in (*_INT_KEYS, *_OBJECT_KEYS)
        }
        self.warn(qualified, merged, texts, here)
        try:
            config = _model_config(texts, merged)
        except ConfigError as e:
            self.problems.add("error", str(e), source, here)
            return
        origins = [Origin(k, leaf.source, group) for k, (leaf, group) in merged.items()]
        if "provider" not in merged:
            origins.append(Origin("provider", "default", None))
        self.models[qualified] = ResolvedModel(qualified, config, tuple(origins))
        self.where[qualified] = (here, source)

    def provider_problems(self) -> None:
        """Each resolved model checked by the provider it names (`Provider.check`)."""
        if self.providers is None:
            return
        for qualified, resolved in self.models.items():
            provider = self.providers.get(resolved.config.provider)
            if provider is None:
                continue  # `checked` reported the unknown name where it was written
            here, source = self.where[qualified]
            try:
                found = provider.check(resolved.config)
            except Exception as e:
                found = [f"check raised {type(e).__name__}: {e}"]
            for problem in found:
                msg = f"model {qualified!r} ({provider.name}): {problem}"
                self.problems.add("error", msg, source, here)

    def key_problems(self) -> None:
        """A key that cannot travel in an HTTP header: an error for a literal, a warning for
        the variable (read when a sample is made). Never quotes the key."""
        for qualified, resolved in self.models.items():
            config = resolved.config
            here, source = self.where[qualified]
            if config.literal_api_key is not None:
                if (kind := key_problem(config.literal_api_key)) is not None:
                    msg = f"model {qualified!r}: the literal api_key {kind}"
                    self.problems.add("error", msg, source, f"{here}.api_key")
            elif config.api_key_env is not None and self.environ is not None:
                value = self.environ.get(config.api_key_env)
                if value and (kind := key_problem(value)) is not None:
                    msg = f"model {qualified!r}: environment variable {config.api_key_env} {kind}"
                    self.problems.add("warning", msg, source, f"{here}.api_key_env")

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
    user: Layer | None, project: Layer | None, cli: Mapping[str, object] | None, problems: Problems
) -> tuple[Tree, _Kinds]:
    texts: list[tuple[str, str, bool]] = []  # source, text, whether it is the project's
    for path, is_project in ((user, False), (project, True)):
        if path is None:
            continue
        if isinstance(path, LayerText):
            texts.append((path.source, path.text, is_project))
            continue
        try:
            texts.append((str(path), path.read_text(encoding="utf-8"), is_project))
        except OSError as e:
            problems.add("error", f"cannot be read: {e.strerror}", str(path), "")
        except UnicodeDecodeError as e:
            problems.add("error", f"cannot be read: {e}", str(path), "")
    if cli is not None:
        try:
            texts.append((CLI, json.dumps(cli), False))
        except (TypeError, ValueError) as e:
            problems.add("error", f"overrides are not JSON: {e}", CLI, "")
    merged: Tree = {}
    kinds: _Kinds = {}
    for source, text, is_project in texts:
        tree = _load_layer(source, text, problems)
        if tree is not None and is_project and "tui" in tree:
            msg = "'tui' is a person's own setting: set it in the user's config, not a project's"
            problems.add("error", msg, source, "tui")
            del tree["tui"]
        if tree is not None:
            merged = _LayerMerge(kinds, problems, source).node(merged, tree, ())
    return merged, kinds


def _default_model(merged: Tree, declared: list[str], problems: Problems) -> str | None:
    node = merged.get("default_model")
    if node is None or (isinstance(node, Leaf) and node.value is None):
        return None
    source = source_of(node)
    if not (isinstance(node, Leaf) and isinstance(node.value, str) and node.value):
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
    user: Layer | None,
    project: Layer | None,
    cli: Mapping[str, object] | None,
    environ: Mapping[str, str] | None,
    providers: Mapping[str, Provider] | None,
) -> tuple[ModelCatalog, tuple[ConfigProblem, ...]]:
    problems = Problems()
    merged, kinds = _read_layers(user, project, cli, problems)
    for key, value in merged.items():
        if key not in _TOP_KEYS:
            msg = f"unknown key {key!r}{suggest(key, _TOP_KEYS)}"
            problems.add("error", msg, source_of(value), key)
    resolver = _Resolver(problems, environ, kinds, providers)
    models = merged.get("models", {})
    if isinstance(models, Leaf) and models.value is None:
        models = {}
    if isinstance(models, dict):
        resolver.walk(models, "models", (), {})
    else:
        problems.add("error", "'models' must be an object", source_of(models), "models")
    resolver.provider_problems()
    resolver.key_problems()
    default = _default_model(merged, resolver.declared, problems)
    switches, switched_by = load_switches(merged, problems)
    prompt, prompt_source = system_prompt(merged, problems)
    options, tui = extensions(merged, problems), tui_settings(merged, problems)
    found = tuple(problems.found)
    warnings = tuple(p for p in found if p.severity == "warning")
    catalog = ModelCatalog(
        models=resolver.models,
        default_model=default,
        warnings=warnings,
        extensions=options,
        load=switches,
        load_sources=switched_by,
        system_prompt=prompt,
        system_prompt_source=prompt_source,
        tui=tui,
    )
    return catalog, found


def option_problems(
    catalog: ModelCatalog, extensions: Sequence[object]
) -> tuple[ConfigProblem, ...]:
    """Every problem in what the layers set for the `extensions` that have options, and for
    options set where there can be none: an extension that has no options, and `retry` (D24
    section 4: the provider wrapper, which `load` switches and nothing tunes)."""
    problems: list[ConfigProblem] = []
    bare = [str(e.name) for e in extensions if getattr(e, "options", None) is None]  # type: ignore[attr-defined]
    for key in ("retry", *bare):
        if catalog.extensions.get(key):
            message = f"extension {key!r} has no options"
            problems.append(ConfigProblem("error", message, None, f"extensions.{key}"))
    tools = _tool_names(extensions)
    for ext in extensions:
        options = getattr(ext, "options", None)
        if options is None:
            continue
        name = cast("str", getattr(ext, "name"))  # noqa: B009
        raw = dict(catalog.extensions.get(name, {}))
        try:
            made = make_options(name, options, option_fields(name, options), raw, Context(catalog))
        except (ConfigError, DefinitionError) as e:
            problems.append(ConfigProblem("error", str(e), None, f"extensions.{name}"))
            continue
        check = getattr(made, "tool_problems", None)
        if check is not None:  # an option that names tools: each must be one a start offers
            for message in check(tools):
                problems.append(ConfigProblem("error", message, None, f"extensions.{name}"))
    return tuple(problems)


def _tool_names(extensions: Sequence[object]) -> list[str]:
    """The names of the tools the `extensions` provide."""
    listed, _ = assemble_tools((), extensions)
    return [cast("str", getattr(t, "name")) for t in listed]  # noqa: B009


def check_config(  # noqa: PLR0913 - the layers, the environment, and what is checked against
    user: Layer | None = None,
    project: Layer | None = None,
    cli: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    providers: Mapping[str, Provider],
    extensions: Sequence[object] = (),
) -> tuple[ConfigProblem, ...]:
    """Every problem in the layers, not just the first. Layers: `user` < `project` < `cli`.

    `providers` is the driver's map of name to provider: a model may name exactly those, and
    each provider's `check(model)` problems for the models naming it are added (D15).

    `extensions` are those a program loads: an option the layers set for one that its options
    type does not have, one that does not fit, or a required one nobody set is a problem.
    Options for an extension not given are not looked at (it may be loaded elsewhere).

    With `environ`, a model whose `api_key_env` names an unset variable is a
    warning. An empty result means the layers are sound.
    """
    catalog, found = _build(user, project, cli, environ, providers)
    return found + option_problems(catalog, extensions)


def read_models(
    user: Layer | None = None,
    project: Layer | None = None,
    cli: Mapping[str, object] | None = None,
) -> ModelCatalog:
    """Merge the layers (`user` < `project` < `cli`; each may be absent) and resolve.

    Raises `ConfigError` listing every error; warnings ride on the catalog.
    """
    catalog, problems = _build(user, project, cli, None, None)
    errors = [p for p in problems if p.severity == "error"]
    if errors:
        raise ConfigError("\n".join(str(p) for p in errors))
    return catalog
