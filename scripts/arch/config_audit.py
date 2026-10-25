"""Configuration-key and provider-capability audit of the five τ ``src`` trees.

Measures, from the AST of the tree it sits in (plus one import of the RPC
command table, which is data built at import time):

1. Configuration surfaces and their keys: ``config.json`` top level and its
   blocks, ``models.<name>`` entries, the runtime ``model_config`` dict, the
   TUI ``run_config``, per-extension config, ``settings.json`` (``Settings``),
   theme files, environment variables, CLI flags of every argparse parser, RPC
   verbs and their params, and the ``tau serve`` protocol request fields. Each
   key carries where it is read or written (path:line), the ``.get`` default,
   and the error messages raised in the same function that name the key.
2. A cross-surface concept matrix, from a concept table in this file whose
   cells are checked against the extracted keys (a cell that names a key no
   surface has is reported as ``MISSING``).
3. String vocabularies: ``Literal[...]``, ``in``/``not in`` against string
   collections, string-tuple constants, string-keyed lookup dicts, argparse
   ``choices``, JSON-schema ``enum`` lists, ``match`` string cases and Enum
   classes; grouped by value set, with near-duplicate sets that disagree.
4. A key-casing histogram per surface.
5. Provider audit: every ``Model``/``Compat`` field and which provider module
   reads it (``model.<field>`` lines), per-call option keys each provider reads
   or strips, stop-reason maps, usage-field reads, and every comparison of a
   string against a wire-protocol or provider id anywhere in ``src``.

Usage:
    python scripts/arch/config_audit.py [--out DIR]

Outputs (in ``--out``, default ``build/arch/config``):
    config_keys.json      every key access, per surface
    cli_flags.json        every add_argument call
    env_vars.json         every environment-variable read or write
    rpc_verbs.json        the RPC command table and serve protocol requests
    vocabularies.json     every string vocabulary occurrence, and the groups
    concepts.json         the cross-surface matrix
    casing.json           casing histogram per surface
    providers.json        the provider audit
    summary.md            human-readable tables of all of the above
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]

SRC_TREES = (
    "tau-llm/src",
    "tau-agent-core/src",
    "tau-coding-agent/src",
    "tau-jmfts/src",
    "tau-meta/src",
)

PROVIDER_FILES = {
    "openai-completions": "tau-llm/src/tau_llm/providers/openai.py",
    "anthropic-messages": "tau-llm/src/tau_llm/providers/anthropic.py",
    "google-generative-ai": "tau-llm/src/tau_llm/providers/google.py",
}

# (path regex, scope regex, receiver regex, surface); first match wins.
CFG_SURFACE_RULES: tuple[tuple[str, str, str, str], ...] = (
    (r"backends\.py$", r"^build_model_from_config$", r"^config$", "models.<name> entry"),
    (r"backends\.py$", r"^ConfigModelResolver", r"^self\._models\[name\]$", "models.<name> entry"),
    (r"backends\.py$", r"^(TauBackend|Backend)\b", r"^(self\.)?config$", "model_config"),
    (r"app\.py$", r".*", r"^self\._apply_run_config\(", "model_config"),
    (
        r"store_factory\.py$",
        r".*",
        r"^_session_store_config\(config\)$",
        "config.json: session_store",
    ),
    (r".*", r".*", r"^config_at\(", "session-recorded config"),
    (r"config\.py$", r"^compaction_for$", r"^entry$", "models.<name> entry"),
    (r"backends\.py$", r"^resolve_tool_(names|filters)$", r"^config$", "model_config"),
    (r"serve/remote\.py$", r".*", r"^self\.config$", "model_config"),
    (r".*", r".*", r"^(self\.)?model_config$|^mc$|^unavailable$|^override$", "model_config"),
    (r".*", r".*", r"^run_config$", "run_config (TUI launch)"),
    (r".*", r".*", r"^api\.config$", "extension config"),
    (r"tau_jmfts/ext/", r".*", r"^cfg$", "extension config"),
    (r".*", r".*", r"^serve_config$|^\(?config\.get\('serve'\) or \{\}\)?$", "config.json: serve"),
    (r"serve/cli\.py$", r".*", r"^settings$", "config.json: serve"),
    (r"store_factory\.py$", r".*", r"^store_config$|^raw$", "config.json: session_store"),
    (r"store_factory\.py$", r".*", r"^sessions$", "config.json: sessions"),
    (r"themes\.py$", r".*", r"^raw$", "theme file"),
    (r".*", r".*", r"^(prior_config|recorded)$", "session-recorded config"),
    (r"tau_jmfts/catalog\.py$", r".*", r"^self\.config$", "session-recorded config"),
    (r"tau_coding_agent/", r".*", r"^(self\.)?config$", "config.json"),
)

CFG_CONFIGISH = re.compile(r"config|cfg|settings", re.IGNORECASE)

# Concept -> {surface: key}; every key is checked against the extraction.
CFG_CONCEPTS: dict[str, dict[str, str]] = {
    "model selection": {
        "config.json": "default_model",
        "cli": "--model",
        "rpc": "set_model.name",
        "serve": "NewSession.model",
        "settings.json": "default_model",
    },
    "provider / backend": {
        "models.<name> entry": "backend",
        "cli": "--provider",
        "Model": "provider",
    },
    "wire protocol": {"models.<name> entry": "api", "Model": "api"},
    "thinking level (requested)": {
        "model_config": "thinking",
        "cli": "--thinking",
        "rpc": "set_reasoning.level",
        "settings.json": "thinking_level",
        "options": "reasoning",
    },
    "reasoning capability (bool)": {"models.<name> entry": "reasoning", "Model": "reasoning"},
    "thinking level map": {
        "models.<name> entry": "thinking_level_map",
        "Model": "thinking_level_map",
    },
    "reasoning replay": {
        "config.json": "reasoning_replay",
        "models.<name> entry": "reasoning_replay",
        "Model": "reasoning_replay",
    },
    "output token cap": {
        "models.<name> entry": "max_tokens",
        "Model": "max_tokens",
        "settings.json": "max_tokens",
        "compat": "max_tokens_field",
    },
    "context window": {"models.<name> entry": "context_window", "Model": "context_window"},
    "sampling temperature": {
        "models.<name> entry": "temperature",
        "Model": "temperature",
        "settings.json": "temperature",
    },
    "grammar dialect": {"models.<name> entry": "grammar", "Model": "grammar_dialect"},
    "prompt cache": {
        "models.<name> entry": "prompt_cache",
        "Model": "prompt_cache",
    },
    "prompt cache dialect": {
        "models.<name> entry": "prompt_cache_dialect",
        "Model": "prompt_cache_dialect",
    },
    "request timeout": {
        "models.<name> entry": "request_timeout",
        "Model": "request_timeout",
        "options": "request_timeout",
    },
    "streaming transport": {
        "models.<name> entry": "stream",
        "Model": "stream",
        "options": "stream",
    },
    "api key": {
        "models.<name> entry": "api_key",
        "settings.json": "api_keys",
        "options": "api_key",
    },
    "system prompt (replace)": {
        "config.json": "system_prompt",
        "model_config": "system_prompt",
        "cli": "--system-prompt",
        "settings.json": "custom_system_prompt",
    },
    "system prompt (append)": {
        "model_config": "append_system_prompt",
        "cli": "--append-system-prompt",
    },
    "tool allowlist": {
        "model_config": "tool_allowlist",
        "run_config (TUI launch)": "tools",
        "cli": "--tools",
    },
    "tool denylist": {
        "model_config": "exclude_tools",
        "run_config (TUI launch)": "exclude_tools",
        "cli": "--exclude-tools",
    },
    "no tools": {
        "model_config": "no_tools",
        "run_config (TUI launch)": "no_tools",
        "cli": "--no-tools",
    },
    "extension paths": {
        "model_config": "extensions",
        "run_config (TUI launch)": "extensions",
        "cli": "--extension",
        "settings.json": "extension_dirs",
    },
    "extension discovery off": {
        "model_config": "no_extensions",
        "run_config (TUI launch)": "no_extensions",
        "cli": "--no-extensions",
    },
    "per-extension config": {
        "config.json": "extensions",
        "run_config (TUI launch)": "ext_config",
        "cli": "--ext-config",
        "rpc": "set_extension_config.values",
    },
    "bus available": {
        "model_config": "bus_available",
        "run_config (TUI launch)": "bus",
        "cli": "--bus",
    },
    "turn ceiling": {
        "config.json": "max_turns",
        "model_config": "max_turns",
        "run_config (TUI launch)": "max_turns",
        "cli": "--max-turns",
    },
    "context files off": {
        "model_config": "no_context_files",
        "run_config (TUI launch)": "no_context_files",
        "cli": "--no-context-files",
    },
    "prompt template paths": {
        "config.json": "prompt_templates",
        "model_config": "prompt_templates",
        "run_config (TUI launch)": "prompt_templates",
        "cli": "--prompt-template",
    },
    "skill paths": {
        "config.json": "skills",
        "model_config": "skills",
        "run_config (TUI launch)": "skills",
        "cli": "--skill",
    },
    "compaction": {
        "config.json": "compaction",
        "models.<name> entry": "compaction",
        "rpc": "set_auto_compaction.enabled",
        "settings.json": "compaction_enabled",
    },
    "headless dialog answers": {"config.json": "ui_defaults", "cli": "--ui-defaults"},
    "session store backend": {"config.json: session_store": "backend", "cli": "--store"},
    "session directory": {"run_config (TUI launch)": "session_dir", "cli": "--session-dir"},
    "theme": {"config.json": "theme", "cli": "--theme"},
    "steering strategy": {"config.json": "steering_strategy", "rpc": "set_steering_mode"},
    "enter key": {"config.json": "enter_key"},
    "attachment inline limit": {"config.json": "attachment_inline_limit"},
    "image dimension cap": {
        "config.json": "max_image_dimension",
        "model_config": "max_image_dimension",
    },
    "pricing": {"model_config": "cost"},
    "serve listen address": {"config.json: serve": "listen", "cli": "--listen"},
    "serve web root": {"config.json: serve": "web_root", "cli": "--web-root"},
    "serve token": {
        "config.json: serve": "token",
        "env": "TAUD_TOKEN",
        "serve": "Hello.token",
    },
    "serve autostart": {"config.json: serve": "autostart"},
    "serve allowed origins": {"config.json: serve": "allowed_origins"},
    "JMFTS url": {"config.json: session_store": "url", "env": "JMFTS_API_URL"},
    "JMFTS token": {"config.json: session_store": "token", "env": "JMFTS_API_TOKEN"},
    "τ home": {"env": "TAU_HOME"},
    "session name": {"cli": "--name", "rpc": "set_session_name.name", "serve": "NewSession.name"},
    "working directory": {"model_config": "cwd", "cli": "--cwd", "serve": "NewSession.cwd"},
}

API_IDS = ("anthropic-messages", "google-generative-ai", "openai-completions")
PROVIDER_IDS = ("anthropic", "gemini", "google", "openai")


@dataclass
class KeyAccess:
    """One read or write of a string key on a dict-like receiver."""

    surface: str
    key: str
    path: str
    line: int
    scope: str
    receiver: str
    op: str
    default: str | None = None
    via_constant: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Module:
    """One parsed source file and its module-level string constants."""

    path: str
    name: str
    tree: ast.Module
    consts: dict[str, Any]
    const_lines: dict[str, int]
    imports: dict[str, tuple[str, str]]


def cfg_module_name(rel: str) -> str:
    """Dotted module name for a repo-relative ``src`` path."""
    parts = Path(rel).with_suffix("").parts
    idx = parts.index("src")
    mod = list(parts[idx + 1 :])
    if mod[-1] == "__init__":
        mod = mod[:-1]
    return ".".join(mod)


def cfg_str_seq(node: ast.AST) -> tuple[str, ...] | None:
    """The strings of a tuple/list/set literal, or a frozenset/set/tuple/list call on one."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"frozenset", "set", "tuple", "list"} and len(node.args) == 1:
            return cfg_str_seq(node.args[0])
        return None
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and node.elts:
        if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
            return tuple(e.value for e in node.elts)  # type: ignore[attr-defined]
    return None


def cfg_load_modules() -> dict[str, Module]:
    """Parse every ``.py`` under the five src trees, keyed by repo-relative path."""
    modules: dict[str, Module] = {}
    for tree_dir in SRC_TREES:
        for path in sorted((ROOT / tree_dir).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            consts: dict[str, Any] = {}
            lines: dict[str, int] = {}
            imports: dict[str, tuple[str, str]] = {}
            for node in tree.body:
                targets: list[ast.expr] = []
                value: ast.AST | None = None
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets, value = [node.target], node.value
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        imports[alias.asname or alias.name] = (node.module, alias.name)
                for target in targets:
                    if not isinstance(target, ast.Name) or value is None:
                        continue
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        consts[target.id] = value.value
                    elif (seq := cfg_str_seq(value)) is not None:
                        consts[target.id] = seq
                    else:
                        continue
                    lines[target.id] = node.lineno
            modules[rel] = Module(rel, cfg_module_name(rel), tree, consts, lines, imports)
    return modules


class Resolver:
    """Resolves a Name to a module-level string constant, following ``from x import``."""

    def __init__(self, modules: dict[str, Module]) -> None:
        """Index modules by dotted name."""
        self.by_name = {m.name: m for m in modules.values()}

    def lookup(self, mod: Module, name: str, depth: int = 0) -> tuple[Any, str] | None:
        """``(value, defining module path)`` for ``name`` seen from ``mod``."""
        if name in mod.consts:
            return mod.consts[name], mod.path
        if depth < 4 and name in mod.imports:
            src_mod, src_name = mod.imports[name]
            other = self.by_name.get(src_mod)
            if other is not None:
                return self.lookup(other, src_name, depth + 1)
        return None

    def str_of(self, mod: Module, node: ast.AST) -> tuple[str, str | None] | None:
        """``(string, constant name or None)`` when ``node`` is a str literal or str constant."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, None
        if isinstance(node, ast.Name):
            found = self.lookup(mod, node.id)
            if found is not None and isinstance(found[0], str):
                return found[0], node.id
        return None

    def seq_of(self, mod: Module, node: ast.AST) -> tuple[tuple[str, ...], str | None] | None:
        """``(strings, constant name or None)`` for a str collection literal or constant."""
        seq = cfg_str_seq(node)
        if seq is not None:
            return seq, None
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args:
            if node.func.id in {"list", "tuple", "sorted", "frozenset", "set"} and isinstance(
                node.args[0], ast.Name
            ):
                name = node.args[0].id
        if name is not None:
            found = self.lookup(mod, name)
            if found is not None and isinstance(found[0], tuple):
                return found[0], name
        return None


LOOP_BINDINGS: dict[int, dict[str, tuple[str, ...]]] = {}


def cfg_loop_bindings(node: ast.For) -> dict[str, tuple[str, ...]]:
    """Names a ``for a, b in (("x", ...), ("y", ...))`` loop binds to all-string columns."""
    if not isinstance(node.iter, (ast.Tuple, ast.List)) or not node.iter.elts:
        return {}
    targets = node.target.elts if isinstance(node.target, ast.Tuple) else [node.target]
    rows = node.iter.elts
    out: dict[str, tuple[str, ...]] = {}
    for idx, tgt in enumerate(targets):
        if not isinstance(tgt, ast.Name):
            continue
        column = []
        for row in rows:
            cells = row.elts if isinstance(row, ast.Tuple) else [row]
            cell = cells[idx] if idx < len(cells) else None
            if isinstance(cell, ast.Constant) and isinstance(cell.value, str):
                column.append(cell.value)
            elif isinstance(cell, ast.Name):
                column.append(f"<name:{cell.id}>")
            else:
                break
        else:
            out[tgt.id] = tuple(column)
    return out


def cfg_walk_scoped(tree: ast.AST) -> Iterator[tuple[ast.AST, str, ast.AST | None]]:
    """Yield ``(node, scope qualname, enclosing function)`` for every node.

    Loop bindings in force at each node are recorded in :data:`LOOP_BINDINGS`.
    """
    stack: list[tuple[ast.AST, str, ast.AST | None, dict[str, tuple[str, ...]]]] = [
        (tree, "", None, {})
    ]
    while stack:
        node, scope, func, loops = stack.pop()
        if loops:
            LOOP_BINDINGS[id(node)] = loops
        yield node, scope, func
        child_loops = {**loops, **cfg_loop_bindings(node)} if isinstance(node, ast.For) else loops
        for child in ast.iter_child_nodes(node):
            child_scope, child_func = scope, func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_scope = f"{scope}.{child.name}" if scope else child.name
                if not isinstance(child, ast.ClassDef):
                    child_func = child
            stack.append((child, child_scope, child_func, child_loops))


def cfg_classify(path: str, scope: str, receiver: str) -> str | None:
    """The surface a receiver belongs to, per :data:`CFG_SURFACE_RULES`."""
    for path_re, scope_re, recv_re, surface in CFG_SURFACE_RULES:
        if re.search(path_re, path) and re.search(scope_re, scope) and re.search(recv_re, receiver):
            return surface
    if CFG_CONFIGISH.search(receiver) and "tau_coding_agent" in path:
        return "unclassified"
    return None


def cfg_error_sites(func: ast.AST | None) -> list[tuple[int, str]]:
    """Every ``raise X(msg)`` / ``problems.append(msg)`` / ``print(msg)`` in a function."""
    if func is None:
        return []
    sites: list[tuple[int, str]] = []
    for node in ast.walk(func):
        call: ast.Call | None = None
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            call = node.exc
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"problems", "errors", "self._theme_errors"}
        ):
            call = node
        if call is not None and call.args:
            sites.append((node.lineno, ast.unparse(call.args[0])))
    return sites


def cfg_key_accesses(modules: dict[str, Module], res: Resolver) -> list[KeyAccess]:
    """Every string-keyed ``.get``/``[]``/``in``/helper read on a classified receiver."""
    out: list[KeyAccess] = []
    for mod in modules.values():
        error_cache: dict[int, list[tuple[int, str]]] = {}
        for node, scope, func in cfg_walk_scoped(mod.tree):
            hit: tuple[ast.AST, ast.AST, str, str | None] | None = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"get", "pop", "setdefault"} and node.args:
                    default = ast.unparse(node.args[1]) if len(node.args) > 1 else None
                    hit = (node.func.value, node.args[0], node.func.attr, default)
            elif isinstance(node, ast.Subscript):
                op = "write" if isinstance(node.ctx, ast.Store) else "[]"
                hit = (node.value, node.slice, op, None)
            elif isinstance(node, ast.Compare) and len(node.ops) == 1:
                if isinstance(node.ops[0], (ast.In, ast.NotIn)):
                    hit = (node.comparators[0], node.left, "in", None)
            if hit is None and isinstance(node, ast.Call) and len(node.args) >= 3:
                callee = ast.unparse(node.func)
                if callee.lstrip("_") in {"positive_int"}:
                    hit = (node.args[0], node.args[1], "helper", ast.unparse(node.args[2]))
            if hit is None:
                continue
            recv_node, key_node, op, default = hit
            loops = LOOP_BINDINGS.get(id(node), {})
            key = res.str_of(mod, key_node)
            keys: list[tuple[str, str | None]] = [key] if key is not None else []
            if key is None and isinstance(key_node, ast.Name) and key_node.id in loops:
                keys = [(k, f"loop:{key_node.id}") for k in loops[key_node.id] if "<" not in k]
            if not keys:
                continue
            receiver = ast.unparse(recv_node)
            receivers = [receiver]
            if receiver in loops:
                receivers = [r[6:-1] for r in loops[receiver] if r.startswith("<name:")]
            for recv in receivers:
                surface = cfg_classify(mod.path, scope, recv)
                if surface is None:
                    continue
                for one in keys:
                    out.append(
                        cfg_make_access(
                            mod, node, scope, func, recv, surface, one, op, default, error_cache
                        )
                    )
    out.sort(key=lambda a: (a.surface, a.key, a.path, a.line, a.op))
    return out


def cfg_make_access(
    mod: Module,
    node: ast.AST,
    scope: str,
    func: ast.AST | None,
    receiver: str,
    surface: str,
    key: tuple[str, str | None],
    op: str,
    default: str | None,
    error_cache: dict[int, list[tuple[int, str]]],
) -> KeyAccess:
    """Build one :class:`KeyAccess`, attaching error messages that name the key."""
    fid = id(func)
    if fid not in error_cache:
        error_cache[fid] = cfg_error_sites(func)
    needles = [key[0]] + ([key[1].removeprefix("loop:")] if key[1] else [])
    pattern = re.compile(
        "|".join(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])" for n in needles)
    )
    errors = [{"line": ln, "message": msg} for ln, msg in error_cache[fid] if pattern.search(msg)]
    return KeyAccess(
        surface=surface,
        key=key[0],
        path=mod.path,
        line=node.lineno,  # type: ignore[attr-defined]
        scope=scope,
        receiver=receiver,
        op=op,
        default=default,
        via_constant=key[1],
        errors=errors,
    )


def cfg_dataclass_fields(mod: Module, class_name: str) -> list[dict[str, Any]]:
    """Annotated fields of a class: name, annotation, default, line."""
    for node in ast.walk(mod.tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            rows = []
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if stmt.target.id == "model_config":
                        continue
                    rows.append(
                        {
                            "name": stmt.target.id,
                            "annotation": ast.unparse(stmt.annotation),
                            "default": ast.unparse(stmt.value) if stmt.value else None,
                            "path": mod.path,
                            "line": stmt.lineno,
                        }
                    )
            return rows
    raise SystemExit(f"config_audit: class {class_name} not found in {mod.path}")


def cfg_find_calls(modules: dict[str, Module], name: str) -> list[str]:
    """``path:line`` of every call whose callee unparses to end with ``name``."""
    hits = []
    for mod in modules.values():
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(name):
                hits.append(f"{mod.path}:{node.lineno}")
    return sorted(hits)


def cfg_name_uses(modules: dict[str, Module], name: str) -> list[str]:
    """``path:line`` of every Name/Attribute load of ``name`` outside its definition."""
    hits = []
    for mod in modules.values():
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
                hits.append(f"{mod.path}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == name:
                hits.append(f"{mod.path}:{node.lineno}")
    return sorted(set(hits))


def cfg_cli_flags(modules: dict[str, Module], res: Resolver) -> list[dict[str, Any]]:
    """Every ``add_argument``/``add_parser`` call: flags, dest, action, default, choices, type."""
    rows: list[dict[str, Any]] = []
    for mod in modules.values():
        for node, scope, _func in cfg_walk_scoped(mod.tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in {"add_argument", "add_parser"}:
                continue
            names = [a.value for a in node.args if isinstance(a, ast.Constant)]
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            row: dict[str, Any] = {
                "path": mod.path,
                "line": node.lineno,
                "scope": scope,
                "parser": ast.unparse(node.func.value),
                "kind": "subcommand" if node.func.attr == "add_parser" else "argument",
                "flags": names,
            }
            longs = [n for n in names if isinstance(n, str) and n.startswith("--")]
            if "dest" in kw:
                row["dest"] = ast.unparse(kw["dest"]).strip("'\"")
            elif longs:
                row["dest"] = longs[0][2:].replace("-", "_")
            elif names:
                row["dest"] = str(names[0]).lstrip("-").replace("-", "_")
            for key in ("action", "default", "type", "nargs", "metavar", "required", "const"):
                if key in kw:
                    row[key] = ast.unparse(kw[key])
            if "choices" in kw:
                seq = res.seq_of(mod, kw["choices"])
                row["choices"] = list(seq[0]) if seq else ast.unparse(kw["choices"])
                row["choices_source"] = (seq[1] if seq else None) or "literal"
            rows.append(row)
    rows.sort(key=lambda r: (r["path"], r["line"]))
    return rows


def cfg_env_vars(modules: dict[str, Module], res: Resolver) -> list[dict[str, Any]]:
    """Every environment-variable read/write and every ``api_key_env`` declaration."""
    rows: list[dict[str, Any]] = []
    for mod in modules.values():
        for node, scope, _func in cfg_walk_scoped(mod.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                recv = ast.unparse(node.func.value)
                is_env = recv in {"os.environ", "env", "environ"} or recv.endswith("os.environ")
                if is_env and node.func.attr in {"get", "setdefault", "pop"} and node.args:
                    key = res.str_of(mod, node.args[0])
                    if key is not None:
                        rows.append(
                            {
                                "var": key[0],
                                "via_constant": key[1],
                                "op": node.func.attr,
                                "default": ast.unparse(node.args[1])
                                if len(node.args) > 1
                                else None,
                                "path": mod.path,
                                "line": node.lineno,
                                "scope": scope,
                            }
                        )
                if ast.unparse(node.func).endswith("getenv") and node.args:
                    key = res.str_of(mod, node.args[0])
                    if key is not None:
                        rows.append(
                            {
                                "var": key[0],
                                "via_constant": key[1],
                                "op": "getenv",
                                "path": mod.path,
                                "line": node.lineno,
                                "scope": scope,
                            }
                        )
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("ProviderSpec"):
                for kw in node.keywords:
                    if kw.arg == "api_key_env":
                        seq = res.seq_of(mod, kw.value)
                        for var in seq[0] if seq else ():
                            rows.append(
                                {
                                    "var": var,
                                    "via_constant": None,
                                    "op": "ProviderSpec.api_key_env",
                                    "path": mod.path,
                                    "line": node.lineno,
                                    "scope": scope,
                                }
                            )
            if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
                key = res.str_of(mod, node.slice)
                if key is not None:
                    rows.append(
                        {
                            "var": key[0],
                            "via_constant": key[1],
                            "op": "write" if isinstance(node.ctx, ast.Store) else "[]",
                            "path": mod.path,
                            "line": node.lineno,
                            "scope": scope,
                        }
                    )
    rows.sort(key=lambda r: (r["var"], r["path"], r["line"]))
    return rows


def cfg_rpc_and_serve(modules: dict[str, Module]) -> dict[str, Any]:
    """The RPC ``COMMAND_TABLE`` (imported from this tree) and serve protocol request classes."""
    for tree_dir in reversed(SRC_TREES):
        sys.path.insert(0, str(ROOT / tree_dir))
    try:
        from tau_agent_core.rpc import commands as rpc_commands
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"config_audit: cannot import the RPC command table ({exc}); install the repo's "
            "dev dependencies: pip install -e ./tau-llm -e './tau-agent-core[dev]'"
        ) from exc
    origin = Path(rpc_commands.__file__).resolve()
    if ROOT not in origin.parents:
        raise SystemExit(f"config_audit: imported {origin}, which is not under {ROOT}")
    verbs = []
    for name, entry in sorted(rpc_commands.COMMAND_TABLE.items()):
        params = entry.params_schema.get("properties", {}) or {}
        handler = entry.handler
        verbs.append(
            {
                "verb": name,
                "declined": entry.handler is None,
                "declined_because": entry.declined_because,
                "required": entry.params_schema.get("required") or [],
                "params": {
                    k: {"type": v.get("type"), "enum": v.get("enum")}
                    for k, v in sorted(params.items())
                },
                "handler": (
                    f"{Path(handler.__code__.co_filename).resolve().relative_to(ROOT).as_posix()}"
                    f":{handler.__code__.co_firstlineno}"
                    if handler is not None and hasattr(handler, "__code__")
                    else None
                ),
            }
        )
    serve: list[dict[str, Any]] = []
    proto = modules["tau-coding-agent/src/tau_coding_agent/serve/protocol.py"]
    for node in proto.tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields_ = []
        msg_type = None
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "type" and stmt.value is not None:
                    msg_type = ast.literal_eval(stmt.value)
                    continue
                fields_.append(
                    {
                        "name": stmt.target.id,
                        "annotation": ast.unparse(stmt.annotation),
                        "default": ast.unparse(stmt.value) if stmt.value else None,
                    }
                )
        if msg_type is not None:
            serve.append(
                {"class": node.name, "type": msg_type, "line": node.lineno, "fields": fields_}
            )
    return {"rpc": verbs, "serve_requests": serve, "serve_path": proto.path}


@dataclass
class Vocab:
    """One occurrence of a fixed set of strings."""

    kind: str
    values: tuple[str, ...]
    path: str
    line: int
    name: str | None
    context: str


def cfg_literal_values(node: ast.AST) -> tuple[str, ...] | None:
    """Strings of a ``Literal[...]`` subscript whose members are all strings."""
    if not (isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith("Literal")):
        return None
    inner = node.slice
    elts = inner.elts if isinstance(inner, ast.Tuple) else [inner]
    if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in elts):
        return tuple(e.value for e in elts)  # type: ignore[attr-defined]
    return None


def cfg_vocabularies(modules: dict[str, Module], res: Resolver) -> list[Vocab]:
    """Every fixed string set: Literal, in/not in, constants, lookup dicts, choices, enum, match."""
    out: list[Vocab] = []
    for mod in modules.values():
        for name, value in mod.consts.items():
            if isinstance(value, tuple) and len(value) >= 2:
                out.append(Vocab("constant", value, mod.path, mod.const_lines[name], name, name))
        for node, scope, _func in cfg_walk_scoped(mod.tree):
            if isinstance(node, ast.Subscript):
                lit = cfg_literal_values(node)
                if lit is not None and len(lit) >= 1:
                    out.append(Vocab("Literal", lit, mod.path, node.lineno, None, scope))
            elif isinstance(node, ast.Compare) and len(node.ops) == 1:
                if isinstance(node.ops[0], (ast.In, ast.NotIn)):
                    seq = res.seq_of(mod, node.comparators[0])
                    if seq is not None and len(seq[0]) >= 2:
                        op = "not in" if isinstance(node.ops[0], ast.NotIn) else "in"
                        out.append(
                            Vocab(
                                op,
                                seq[0],
                                mod.path,
                                node.lineno,
                                seq[1],
                                f"{scope}: {ast.unparse(node.left)} {op} ...",
                            )
                        )
            elif isinstance(node, ast.Dict) and node.keys:
                keys = [
                    k.value
                    for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                ]
                if "enum" in keys:
                    for k, v in zip(node.keys, node.values):
                        if isinstance(k, ast.Constant) and k.value == "enum":
                            seq = cfg_str_seq(v)
                            if seq is None and isinstance(v, ast.List):
                                strs = [
                                    e.value
                                    for e in v.elts
                                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                                ]
                                seq = tuple(strs) if strs else None
                            if seq is None:
                                found = res.seq_of(mod, v)
                                seq = found[0] if found else None
                            if seq:
                                out.append(
                                    Vocab("schema enum", seq, mod.path, node.lineno, None, scope)
                                )
            elif isinstance(node, ast.Match):
                vals = []
                for case in node.cases:
                    for sub in ast.walk(case.pattern):
                        if isinstance(sub, ast.MatchValue) and isinstance(sub.value, ast.Constant):
                            if isinstance(sub.value.value, str):
                                vals.append(sub.value.value)
                if len(vals) >= 2:
                    out.append(
                        Vocab(
                            "match",
                            tuple(vals),
                            mod.path,
                            node.lineno,
                            None,
                            f"{scope}: match {ast.unparse(node.subject)}",
                        )
                    )
            elif isinstance(node, ast.ClassDef):
                bases = {ast.unparse(b).split(".")[-1] for b in node.bases}
                if bases & {"Enum", "StrEnum", "IntEnum", "Flag"}:
                    members = []
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                            members.append(str(stmt.value.value))
                    out.append(
                        Vocab("Enum", tuple(members), mod.path, node.lineno, node.name, scope)
                    )
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "add_argument":
                    for kw in node.keywords:
                        if kw.arg == "choices":
                            seq = res.seq_of(mod, kw.value)
                            if seq is not None:
                                flags = [a.value for a in node.args if isinstance(a, ast.Constant)]
                                out.append(
                                    Vocab(
                                        "argparse choices",
                                        seq[0],
                                        mod.path,
                                        node.lineno,
                                        seq[1],
                                        f"{scope}: {flags}",
                                    )
                                )
        for node in mod.tree.body:
            targets: list[ast.expr] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            if not isinstance(value, ast.Dict) or len(value.keys) < 2:
                continue
            keys = [
                k for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
            if len(keys) != len(value.keys):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    out.append(
                        Vocab(
                            "lookup dict",
                            tuple(k.value for k in keys),  # type: ignore[misc]
                            mod.path,
                            node.lineno,
                            target.id,
                            target.id,
                        )
                    )
    out.sort(key=lambda v: (v.path, v.line, v.kind))
    return out


def cfg_dict_lookup_sites(modules: dict[str, Module], name: str) -> dict[str, list[str]]:
    """Where a lookup dict named ``name`` is subscripted (KeyError) or ``.get``-ed."""
    sub, get = [], []
    for mod in modules.values():
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Subscript) and ast.unparse(node.value) == name:
                if isinstance(node.ctx, ast.Load):
                    sub.append(f"{mod.path}:{node.lineno}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and ast.unparse(node.func.value) == name
            ):
                get.append(f"{mod.path}:{node.lineno}")
    return {"subscript": sorted(sub), "get": sorted(get)}


JSON_SCHEMA_WORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "description", "items", "enum"}
)


def cfg_dynamic_lookups(modules: dict[str, Module]) -> dict[str, list[str]]:
    """``name -> [path:line]`` where a Name is subscripted or ``.get``-ed with a non-literal key."""
    hits: dict[str, list[str]] = defaultdict(list)
    for mod in modules.values():
        for node in ast.walk(mod.tree):
            target, key = None, None
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                target, key = node.value, node.slice
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
            ):
                target, key = node.func.value, node.args[0]
            if isinstance(target, ast.Name) and not isinstance(key, ast.Constant):
                hits[target.id].append(f"{mod.path}:{node.lineno}")
    return hits


def cfg_group_vocabs(vocabs: list[Vocab], modules: dict[str, Module]) -> list[dict[str, Any]]:
    """Group occurrences by value set; attach near-duplicates whose values disagree.

    A string-keyed module dict counts only when something looks it up with a
    non-literal key, and not when its keys are JSON-schema keywords.
    """
    dynamic = cfg_dynamic_lookups(modules)
    groups: dict[frozenset[str], list[Vocab]] = defaultdict(list)
    for v in vocabs:
        if v.kind == "lookup dict" and (
            set(v.values) <= JSON_SCHEMA_WORDS or not dynamic.get(v.name or "")
        ):
            continue
        if len(set(v.values)) >= 2:
            groups[frozenset(v.values)].append(v)
    rows = []
    for values, occ in groups.items():
        lookup_names = sorted({o.name for o in occ if o.kind == "lookup dict" and o.name})
        kinds = Counter(o.kind for o in occ)
        rows.append(
            {
                "values": sorted(values),
                "occurrences": len(occ),
                "files": len({o.path for o in occ}),
                "kinds": dict(sorted(kinds.items())),
                "typed": any(o.kind in {"Literal", "Enum"} for o in occ),
                "names": sorted({o.name for o in occ if o.name}),
                "sites": [f"{o.path}:{o.line} [{o.kind}] {o.context}" for o in occ],
                "lookup_sites": {n: cfg_dict_lookup_sites(modules, n) for n in lookup_names},
                "near": [],
            }
        )
    keyed = {frozenset(r["values"]): r for r in rows}
    sets = sorted(keyed, key=lambda s: sorted(s))
    for a in sets:
        for b in sets:
            if a is b or len(a & b) < 2:
                continue
            jac = len(a & b) / len(a | b)
            if jac >= 0.5 and (a < b or b < a or jac < 1):
                keyed[a]["near"].append(
                    {
                        "values": sorted(b),
                        "only_here": sorted(a - b),
                        "only_there": sorted(b - a),
                        "sites": keyed[b]["sites"][:3],
                    }
                )
    rows.sort(key=lambda r: (-r["occurrences"], r["values"]))
    return rows


def cfg_casing(key: str) -> str:
    """The casing style of one key."""
    bare = key.lstrip("-")
    if re.fullmatch(r"[a-z][a-z0-9]*", bare):
        return "single word"
    if re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)+", bare):
        return "snake_case"
    if re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)+", bare):
        return "kebab-case"
    if re.fullmatch(r"[a-z]+([A-Z][a-z0-9]*)+", bare):
        return "camelCase"
    if re.fullmatch(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)*", bare):
        return "UPPER_SNAKE"
    if re.fullmatch(r"([A-Z][a-z0-9]+)+", bare):
        return "PascalCase"
    return "other"


def cfg_provider_audit(
    modules: dict[str, Module], res: Resolver, model_fields: list[dict[str, Any]]
) -> dict[str, Any]:
    """Model-field reads per provider, option keys, stop maps, usage reads, identity branches."""
    names = [f["name"] for f in model_fields]
    field_reads: dict[str, dict[str, list[int]]] = {n: {} for n in names}
    elsewhere: dict[str, list[str]] = {n: [] for n in names}
    provider_paths = {v: k for k, v in PROVIDER_FILES.items()}
    for mod in modules.values():
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Attribute) and node.attr in field_reads:
                base = ast.unparse(node.value)
                if not re.search(r"(^|\.|_)model$", base):
                    continue
                api = provider_paths.get(mod.path)
                if api is not None:
                    field_reads[node.attr].setdefault(api, []).append(node.lineno)
                else:
                    elsewhere[node.attr].append(f"{mod.path}:{node.lineno}")
    option_reads: dict[str, dict[str, list[int]]] = {}
    strip_lists: dict[str, list[dict[str, Any]]] = {}
    stop_maps: dict[str, list[dict[str, Any]]] = {}
    usage_reads: dict[str, list[dict[str, Any]]] = {}
    for api, path in PROVIDER_FILES.items():
        mod = modules[path]
        reads: dict[str, list[int]] = defaultdict(list)
        for node, scope, func in cfg_walk_scoped(mod.tree):
            params = (
                {a.arg for a in func.args.args + func.args.kwonlyargs}  # type: ignore[attr-defined]
                if func is not None
                else set()
            )
            if (
                "options" in params
                and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
            ):
                if node.func.attr == "get" and ast.unparse(node.func.value) == "options":
                    key = res.str_of(mod, node.args[0]) if node.args else None
                    if key:
                        reads[key[0]].append(node.lineno)
            if (
                "options" in params
                and isinstance(node, ast.Subscript)
                and ast.unparse(node.value) == "options"
            ):
                key = res.str_of(mod, node.slice)
                if key:
                    reads[key[0]].append(node.lineno)
            if isinstance(node, (ast.Tuple, ast.Set)) or (
                isinstance(node, ast.Call) and ast.unparse(node.func) == "frozenset"
            ):
                seq = cfg_str_seq(node)
                known = {(sl["scope"], tuple(sl["keys"])) for sl in strip_lists.get(api, [])}
                if (
                    seq
                    and "api_key" in seq
                    and "abort_signal" in seq
                    and (scope, tuple(sorted(seq))) not in known
                ):
                    strip_lists.setdefault(api, []).append(
                        {"line": node.lineno, "scope": scope, "keys": sorted(seq)}
                    )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "_usage_from"
            ):
                fields_read = []
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and len(sub.args) >= 2:
                        callee = ast.unparse(sub.func)
                        if callee == "getattr" and isinstance(sub.args[1], ast.Constant):
                            fields_read.append((sub.lineno, sub.args[1].value))
                        elif callee.endswith(".get") and isinstance(sub.args[0], ast.Constant):
                            fields_read.append((sub.lineno, sub.args[0].value))
                    elif isinstance(sub, ast.Call) and callee_is_get(sub):
                        fields_read.append((sub.lineno, sub.args[0].value))  # type: ignore[attr-defined]
                usage_reads.setdefault(api, []).append(
                    {
                        "function": node.name,
                        "line": node.lineno,
                        "fields": sorted({f"{n}@{ln}" for ln, n in fields_read}),
                    }
                )
        option_reads[api] = {k: sorted(v) for k, v in sorted(reads.items())}
        for node in mod.tree.body:
            target = None
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
                target = tgt.id if isinstance(tgt, ast.Name) else None
            if target and re.search(r"STOP|FINISH", target) and isinstance(node.value, ast.Dict):
                stop_maps.setdefault(api, []).append(
                    {
                        "name": target,
                        "line": node.lineno,
                        "map": {
                            ast.literal_eval(k): ast.literal_eval(v)
                            for k, v in zip(node.value.keys, node.value.values)
                            if k is not None
                        },
                    }
                )
    identity: list[dict[str, Any]] = []
    ids = set(API_IDS) | set(PROVIDER_IDS)
    for mod in modules.values():
        for node, scope, _func in cfg_walk_scoped(mod.tree):
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                consts = [
                    o.value
                    for o in operands
                    if isinstance(o, ast.Constant) and isinstance(o.value, str) and o.value in ids
                ]
                seqs = [cfg_str_seq(o) or () for o in operands]
                consts += [s for seq in seqs for s in seq if s in ids]
                if consts:
                    identity.append(
                        {
                            "path": mod.path,
                            "line": node.lineno,
                            "scope": scope,
                            "expr": ast.unparse(node),
                            "ids": sorted(set(consts)),
                        }
                    )
            elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                last = node.values[-1]
                if isinstance(last, ast.Constant) and last.value in ids:
                    identity.append(
                        {
                            "path": mod.path,
                            "line": node.lineno,
                            "scope": scope,
                            "expr": ast.unparse(node),
                            "ids": [last.value],
                            "kind": "fallback",
                        }
                    )
            elif isinstance(node, ast.IfExp) and isinstance(node.orelse, ast.Constant):
                if node.orelse.value in ids:
                    identity.append(
                        {
                            "path": mod.path,
                            "line": node.lineno,
                            "scope": scope,
                            "expr": ast.unparse(node),
                            "ids": [node.orelse.value],
                            "kind": "fallback",
                        }
                    )
            elif isinstance(node, ast.Dict) and node.keys:
                keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
                if len(keys & set(API_IDS)) >= 2 or len(keys & set(PROVIDER_IDS)) >= 2:
                    identity.append(
                        {
                            "path": mod.path,
                            "line": node.lineno,
                            "scope": scope,
                            "expr": ast.unparse(node)[:200],
                            "ids": sorted(keys & ids),
                            "kind": "keyed dict",
                        }
                    )
    identity.sort(key=lambda r: (r["path"], r["line"]))
    registrations = []
    for mod in modules.values():
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in {
                "register_api",
                "register_provider",
            }:
                registrations.append(
                    {"path": mod.path, "line": node.lineno, "call": ast.unparse(node)[:300]}
                )
    return {
        "model_field_reads": {
            f: {
                "providers": {a: sorted(set(v)) for a, v in field_reads[f].items()},
                "elsewhere": sorted(set(elsewhere[f])),
            }
            for f in names
        },
        "option_reads": option_reads,
        "option_strip_lists": strip_lists,
        "stop_maps": stop_maps,
        "usage_reads": usage_reads,
        "identity_branches": identity,
        "registrations": sorted(registrations, key=lambda r: (r["path"], r["line"])),
    }


def callee_is_get(node: ast.Call) -> bool:
    """Whether ``node`` is ``x.get("literal")`` with exactly one argument."""
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    )


def cfg_concepts(
    accesses: list[KeyAccess],
    flags: list[dict[str, Any]],
    envs: list[dict[str, Any]],
    rpc: dict[str, Any],
    settings_fields: list[dict[str, Any]],
    model_fields: list[dict[str, Any]],
    compat_fields: list[dict[str, Any]],
    option_reads: dict[str, dict[str, list[int]]],
) -> list[dict[str, Any]]:
    """Check each concept cell against the extraction; report present/MISSING and the lines."""
    by_surface: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for a in accesses:
        by_surface[a.surface][a.key].append(f"{a.path}:{a.line}")
    for f in flags:
        for flag in f["flags"]:
            if isinstance(flag, str) and flag.startswith("--"):
                by_surface["cli"][flag].append(f"{f['path']}:{f['line']}")
    for e in envs:
        by_surface["env"][e["var"]].append(f"{e['path']}:{e['line']}")
    for v in rpc["rpc"]:
        by_surface["rpc"][v["verb"]].append(v["handler"] or "declined")
        for p in v["params"]:
            by_surface["rpc"][f"{v['verb']}.{p}"].append(v["handler"] or "declined")
    for s in rpc["serve_requests"]:
        for fld in s["fields"]:
            by_surface["serve"][f"{s['class']}.{fld['name']}"].append(
                f"{rpc['serve_path']}:{s['line']}"
            )
    for rows, surface in (
        (settings_fields, "settings.json"),
        (model_fields, "Model"),
        (compat_fields, "compat"),
    ):
        for r in rows:
            by_surface[surface][r["name"]].append(f"{r['path']}:{r['line']}")
    for api, keys in option_reads.items():
        for k, lines in keys.items():
            by_surface["options"][k].append(f"{PROVIDER_FILES[api]}:{lines[0]}")
    out = []
    for concept, cells in CFG_CONCEPTS.items():
        row = {"concept": concept, "cells": {}}
        for surface, key in cells.items():
            sites = by_surface.get(surface, {}).get(key)
            row["cells"][surface] = {
                "key": key,
                "status": "present" if sites else "MISSING",
                "sites": sorted(set(sites or []))[:6],
            }
        spellings = {
            c["key"].lstrip("-").replace("-", "_").split(".")[-1] for c in row["cells"].values()
        }
        row["distinct_spellings"] = sorted(spellings)
        out.append(row)
    return out


def cfg_fmt_sites(sites: list[str], limit: int = 4) -> str:
    """Join sites for a table cell, truncated with a count."""
    shown = ", ".join(sites[:limit])
    return shown + (f" (+{len(sites) - limit})" if len(sites) > limit else "")


def cfg_md_escape(text: str) -> str:
    """Escape pipes and newlines for a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def cfg_summary(data: dict[str, Any]) -> str:
    """Render every table as markdown."""
    lines: list[str] = ["# Configuration and provider audit (generated)", ""]
    lines += ["## Keys per surface", ""]
    by_sk: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for a in data["config_keys"]:
        by_sk[(a["surface"], a["key"])].append(a)
    surfaces = sorted({s for s, _ in by_sk})
    for surface in surfaces:
        lines += [f"### {surface}", "", "| key | reads | writes | defaults (.get) | error sites |"]
        lines += ["|---|---|---|---|---|"]
        for (s, key), rows in sorted(by_sk.items()):
            if s != surface:
                continue
            reads = [f"{r['path']}:{r['line']}" for r in rows if r["op"] != "write"]
            writes = [f"{r['path']}:{r['line']}" for r in rows if r["op"] == "write"]
            defaults = sorted({r["default"] for r in rows if r["default"] is not None})
            errs = sorted({f"{r['path']}:{e['line']}" for r in rows for e in r["errors"]})
            lines.append(
                f"| `{key}` | {cfg_fmt_sites(reads)} | {cfg_fmt_sites(writes)} | "
                f"{cfg_md_escape(', '.join(defaults))} | {cfg_fmt_sites(errs, 3)} |"
            )
        lines.append("")
    lines += ["## settings.json (`Settings` fields)", "", "| field | type | default | line |"]
    lines += ["|---|---|---|---|"]
    for f in data["settings_fields"]:
        lines.append(
            f"| `{f['name']}` | `{cfg_md_escape(f['annotation'])}` | "
            f"`{cfg_md_escape(str(f['default']))}` | {f['path']}:{f['line']} |"
        )
    lines += ["", f"`Settings.load` call sites in src: {data['settings_load_calls'] or 'none'}", ""]
    lines += [
        "## CLI flags",
        "",
        "| parser | flags | dest | action | default | choices | type | line |",
    ]
    lines += ["|---|---|---|---|---|---|---|---|"]
    for f in data["cli_flags"]:
        lines.append(
            "| {p} | {fl} | `{d}` | {a} | {de} | {c} | {t} | {path}:{line} |".format(
                p=f"{'/'.join(Path(f['path']).parts[-2:])}:{f['parser']}",
                fl=cfg_md_escape(" ".join(str(x) for x in f["flags"])),
                d=f.get("dest", ""),
                a=f.get("action", ""),
                de=cfg_md_escape(str(f.get("default", ""))),
                c=cfg_md_escape(str(f.get("choices", ""))),
                t=f.get("type", ""),
                path=f["path"],
                line=f["line"],
            )
        )
    lines += [
        "",
        "## Environment variables",
        "",
        "| var | op | default | site |",
        "|---|---|---|---|",
    ]
    for e in data["env_vars"]:
        lines.append(
            f"| `{e['var']}` | {e['op']} | {cfg_md_escape(str(e.get('default') or ''))} | "
            f"{e['path']}:{e['line']} |"
        )
    lines += ["", "## RPC verbs", "", "| verb | declined | params | required | handler |"]
    lines += ["|---|---|---|---|---|"]
    for v in data["rpc"]["rpc"]:
        params = ", ".join(
            f"{k}:{p['type']}" + (f"{p['enum']}" if p["enum"] else "")
            for k, p in v["params"].items()
        )
        lines.append(
            f"| `{v['verb']}` | {'yes' if v['declined'] else ''} | {cfg_md_escape(params)} | "
            f"{', '.join(v['required'])} | {v['handler'] or ''} |"
        )
    lines += ["", "## Serve protocol requests", "", "| type | class | fields |", "|---|---|---|"]
    for s in data["rpc"]["serve_requests"]:
        flds = ", ".join(f"{f['name']}:{f['annotation']}" for f in s["fields"])
        lines.append(f"| `{s['type']}` | {s['class']} | {cfg_md_escape(flds)} |")
    lines += ["", "## Concept matrix", ""]
    for c in data["concepts"]:
        cells = "; ".join(
            f"{s}=`{cell['key']}`" + ("" if cell["status"] == "present" else " **MISSING**")
            for s, cell in c["cells"].items()
        )
        lines.append(
            f"- **{c['concept']}**: {cells} — spellings: {', '.join(c['distinct_spellings'])}"
        )
    lines += [
        "",
        "## Casing per surface",
        "",
        "| surface | " + " | ".join(data["casing_styles"]) + " |",
    ]
    lines += ["|---|" + "---|" * len(data["casing_styles"])]
    for surface, hist in sorted(data["casing"].items()):
        lines.append(
            f"| {surface} | "
            + " | ".join(str(hist.get(s, 0)) for s in data["casing_styles"])
            + " |"
        )
    lines += ["", "## Vocabularies (value sets seen 2+ times, or with a near-duplicate)", ""]
    lines += ["| values | n | files | kinds | typed | names | near-duplicates that differ |"]
    lines += ["|---|---|---|---|---|---|---|"]
    for g in data["vocab_groups"]:
        if g["occurrences"] < 2 and not g["near"]:
            continue
        near = "; ".join(
            f"other set adds {n['only_there']}, lacks {n['only_here']}" for n in g["near"][:3]
        )
        lines.append(
            f"| {cfg_md_escape(', '.join(g['values'][:10]))}{' …' if len(g['values']) > 10 else ''} "
            f"| {g['occurrences']} | {g['files']} | {cfg_md_escape(str(g['kinds']))} | "
            f"{'yes' if g['typed'] else 'no'} | {cfg_md_escape(', '.join(g['names']))} | "
            f"{cfg_md_escape(near)} |"
        )
    p = data["providers"]
    lines += ["", "## Model fields read per provider module (`model.<field>` lines)", ""]
    apis = list(PROVIDER_FILES)
    lines += [
        "| field | default | " + " | ".join(apis) + " | elsewhere in src (receiver `*model`) |"
    ]
    lines += ["|---|---|" + "---|" * len(apis) + "---|"]
    for f in data["model_fields"]:
        reads = p["model_field_reads"][f["name"]]
        cells = [",".join(str(x) for x in reads["providers"].get(api, [])) or "—" for api in apis]
        lines.append(
            f"| `{f['name']}` | `{cfg_md_escape(str(f['default']))}` | "
            + " | ".join(cells)
            + f" | {cfg_fmt_sites(reads['elsewhere'], 3)} |"
        )
    lines += ["", "## Per-call option keys read by each provider", ""]
    for api, keys in p["option_reads"].items():
        lines.append(f"- {api}: " + ", ".join(f"`{k}`@{v}" for k, v in keys.items()))
    for api, lists in p["option_strip_lists"].items():
        for sl in lists:
            lines.append(f"- {api} strip list ({sl['scope']}:{sl['line']}): {sl['keys']}")
    lines += ["", "## Stop-reason maps", ""]
    for api, maps in p["stop_maps"].items():
        for m in maps:
            lines.append(f"- {api} `{m['name']}` ({PROVIDER_FILES[api]}:{m['line']}): {m['map']}")
    lines += ["", "## Usage fields read", ""]
    for api, fns in p["usage_reads"].items():
        for fn in fns:
            lines.append(f"- {api} `{fn['function']}`:{fn['line']}: {', '.join(fn['fields'])}")
    lines += ["", "## String compares/fallbacks on wire-protocol or provider ids", ""]
    lines += ["| site | scope | expr |", "|---|---|---|"]
    for r in p["identity_branches"]:
        lines.append(
            f"| {r['path']}:{r['line']} | {r['scope']} | `{cfg_md_escape(r['expr'][:160])}` |"
        )
    lines += ["", "## Registrations", ""]
    for r in p["registrations"]:
        lines.append(f"- {r['path']}:{r['line']} `{cfg_md_escape(r['call'][:200])}`")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the audit and write JSON plus ``summary.md`` into ``--out``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "arch" / "config")
    args = parser.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    modules = cfg_load_modules()
    res = Resolver(modules)
    accesses = cfg_key_accesses(modules, res)
    flags = cfg_cli_flags(modules, res)
    envs = cfg_env_vars(modules, res)
    rpc = cfg_rpc_and_serve(modules)
    settings_mod = modules["tau-agent-core/src/tau_agent_core/settings.py"]
    settings_fields = cfg_dataclass_fields(settings_mod, "Settings")
    model_fields = cfg_dataclass_fields(modules["tau-llm/src/tau_llm/types.py"], "Model")
    compat_fields = cfg_dataclass_fields(modules["tau-llm/src/tau_llm/compat.py"], "Compat")
    compaction_keys = [
        {"name": k, "path": "tau-coding-agent/src/tau_coding_agent/config.py"}
        for k in ("auto", "reserve_tokens", "keep_recent_tokens", "summarizer")
    ]
    vocabs = cfg_vocabularies(modules, res)
    groups = cfg_group_vocabs(vocabs, modules)
    providers = cfg_provider_audit(modules, res, model_fields)
    concepts = cfg_concepts(
        accesses,
        flags,
        envs,
        rpc,
        settings_fields,
        model_fields,
        compat_fields,
        providers["option_reads"],
    )

    casing: dict[str, Counter[str]] = defaultdict(Counter)
    seen: set[tuple[str, str]] = set()
    for a in accesses:
        if (a.surface, a.key) not in seen:
            seen.add((a.surface, a.key))
            casing[a.surface][cfg_casing(a.key)] += 1
    for f in flags:
        for flag in f["flags"]:
            if isinstance(flag, str) and flag.startswith("--"):
                casing["cli (long flags)"][cfg_casing(flag)] += 1
    for e in {e["var"] for e in envs}:
        casing["env"][cfg_casing(e)] += 1
    for v in rpc["rpc"]:
        casing["rpc verbs"][cfg_casing(v["verb"])] += 1
        for p in v["params"]:
            casing["rpc params"][cfg_casing(p)] += 1
    for s in rpc["serve_requests"]:
        casing["serve types"][cfg_casing(s["type"])] += 1
        for fld in s["fields"]:
            casing["serve fields"][cfg_casing(fld["name"])] += 1
    for name, rows in (
        ("settings.json", settings_fields),
        ("Model", model_fields),
        ("compat", compat_fields),
        ("compaction block", compaction_keys),
    ):
        for r in rows:
            casing[name][cfg_casing(r["name"])] += 1
    styles = [
        "single word",
        "snake_case",
        "kebab-case",
        "camelCase",
        "PascalCase",
        "UPPER_SNAKE",
        "other",
    ]

    data = {
        "config_keys": [asdict(a) for a in accesses],
        "settings_fields": settings_fields,
        "settings_load_calls": cfg_find_calls(modules, "Settings.load"),
        "model_fields": model_fields,
        "compat_fields": compat_fields,
        "cli_flags": flags,
        "env_vars": envs,
        "rpc": rpc,
        "vocab_groups": groups,
        "concepts": concepts,
        "casing": {k: dict(sorted(v.items())) for k, v in casing.items()},
        "casing_styles": styles,
        "providers": providers,
    }
    files = {
        "config_keys.json": {
            "keys": data["config_keys"],
            "settings_fields": settings_fields,
            "settings_load_calls": data["settings_load_calls"],
        },
        "cli_flags.json": flags,
        "env_vars.json": envs,
        "rpc_verbs.json": rpc,
        "vocabularies.json": {"occurrences": [asdict(v) for v in vocabs], "groups": groups},
        "concepts.json": concepts,
        "casing.json": data["casing"],
        "providers.json": {
            "model_fields": model_fields,
            "compat_fields": compat_fields,
            **providers,
        },
    }
    for name, payload in files.items():
        (out / name).write_text(json.dumps(payload, indent=2, sort_keys=True, default=list) + "\n")
    (out / "summary.md").write_text(cfg_summary(data))
    unclassified = sorted(
        {f"{a.path}:{a.line} {a.receiver}" for a in accesses if a.surface == "unclassified"}
    )
    print(
        f"config_audit: {len(accesses)} key accesses, {len(flags)} argparse calls, "
        f"{len(envs)} env sites, {len(rpc['rpc'])} rpc verbs, {len(groups)} vocab groups, "
        f"{len(unclassified)} unclassified accesses -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
