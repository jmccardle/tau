"""Every ``examples/NN_*.py`` honours the extension contract it advertises.

## Why this file exists

Examples 01–13 had no test of any kind, and four of the five had rotted:

* ``03_dynamic_env_tool.py`` and ``05_custom_tool.py`` defined an execute
  function, never put it in the tool dict, and gave it a one-argument signature
  instead of the five-argument extension one. ``api.register_tool`` raised
  ``ValueError: missing required key 'execute'`` on load.
* ``02_git_checkpoint.py`` registered a ``turn_end`` handler taking one argument,
  which the runner calls with two, and read ``event.message`` — attribute access
  on what is a dict, carrying a key that event does not have.
* ``02``/``03``/``04``/``05``/``23`` had no module-level ``register``, so
  ``tau -e examples/<file>.py`` — the surface several of their own docstrings
  advertise — raised ``AttributeError: has no register(api) function``.

None of that is subtle. All of it survived because nothing loaded these files.
Every example numbered 20 and above has a dedicated behavioural test and none of
them had rotted, which is the whole argument for this file: the per-example tests
prove behaviour, and this one proves the *contract* uniformly, including for
examples nobody has written a behavioural test for yet.

## What is checked

1. The module imports.
2. It exposes a module-level ``register`` — the ONLY entry point the file-path
   loader looks up (``sdk.py`` ``_load_extension_module``) — unless it is declared
   in :data:`SCRIPT_EXAMPLES` below.
3. ``register(api)`` runs against a real ``ExtensionAPI`` bound to a real
   registry and runner bucket.
4. Every tool it registers carries a ``parameters`` JSON Schema strict enough for
   ``validate_tool_arguments`` to actually enforce something.
5. Every hook handler it registers has the two-argument ``(event, ctx)``
   signature the runner dispatches with.

A new example is therefore either declared a script or held to all five.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from tau_llm.tools import _check_parameters_schema

from tau_agent_core.events import EventBus
from tau_agent_core.extension_types import ExtensionAPI, ExtensionContext
from tau_agent_core.extensions.registry import ExtensionRegistry
from tau_agent_core.extensions.runner import ExtensionHandlers, ExtensionRunner

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES_DIR = _REPO_ROOT / "examples"

#: Examples that are runnable SCRIPTS, not extensions — they call the SDK from a
#: ``main()`` rather than registering anything, so ``register`` does not apply.
#: Anything not listed here must satisfy the full extension contract. Listing is
#: deliberate: a new extension example that forgets ``register`` fails this file
#: rather than being silently reclassified as a script.
SCRIPT_EXAMPLES = frozenset(
    {
        "10_sdk_create_session.py",
        "11_sdk_subscribe_events.py",
        "12_sdk_in_memory_mode.py",
        "13_sdk_custom_system_prompt.py",
    }
)

#: The hook events whose handlers the runner calls as ``handler(event, ctx)``.
_HOOK_EVENTS = frozenset(ExtensionRunner.HOOK_EVENTS) | frozenset(ExtensionRunner.LIFECYCLE_EVENTS)


def _example_paths() -> list[Path]:
    paths = sorted(_EXAMPLES_DIR.glob("[0-9]*.py"))
    assert paths, f"no examples found under {_EXAMPLES_DIR}"
    return paths


def _load(path: Path) -> Any:
    """Import one example by file path, the way the real loader does."""
    module_name = f"tau_example_contract_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fresh_api(path: Path) -> tuple[ExtensionAPI, ExtensionRegistry, ExtensionHandlers]:
    """An ``ExtensionAPI`` wired like a session's: real registry, real runner bucket.

    The runner bucket matters. ``api.on`` routes a MUTATING hook into
    ``hook_handlers`` and raises when there is none, so an api without one cannot
    register the very handlers this file exists to inspect.
    """
    registry = ExtensionRegistry()
    handlers = ExtensionHandlers(path=str(path))
    api = ExtensionAPI(
        registry=registry,
        event_bus=EventBus(),
        context=ExtensionContext(cwd=str(_REPO_ROOT)),
        session=None,
        hook_handlers=handlers,
    )
    return api, registry, handlers


IDS = [p.name for p in _example_paths()]
PATHS = _example_paths()
EXTENSION_PATHS = [p for p in PATHS if p.name not in SCRIPT_EXAMPLES]
EXTENSION_IDS = [p.name for p in EXTENSION_PATHS]


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_example_imports(path: Path) -> None:
    """Every example imports. Catches API drift in the script-style examples too."""
    assert _load(path) is not None


def test_script_examples_all_exist() -> None:
    """``SCRIPT_EXAMPLES`` names no file that has been renamed or deleted.

    Without this the exemption list rots the other way: a renamed example stays
    listed, and its replacement is never held to the contract.
    """
    missing = sorted(name for name in SCRIPT_EXAMPLES if not (_EXAMPLES_DIR / name).exists())
    assert not missing, f"SCRIPT_EXAMPLES names files that no longer exist: {missing}"


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_script_examples_register_nothing(path: Path) -> None:
    """A file is exempted from ``register`` only if it really registers nothing.

    Guards the exemption from being used to silence a broken extension example.
    """
    module = _load(path)
    is_script = path.name in SCRIPT_EXAMPLES
    has_register = getattr(module, "register", None) is not None
    assert has_register != is_script, (
        f"{path.name}: listed in SCRIPT_EXAMPLES={is_script} but "
        f"module-level register present={has_register}"
    )


@pytest.mark.parametrize("path", EXTENSION_PATHS, ids=EXTENSION_IDS)
def test_example_is_loadable_through_dash_e(path: Path) -> None:
    """``tau -e examples/<file>.py`` finds a callable ``register``.

    ``sdk.py`` ``_load_extension_module`` looks up exactly this name and raises
    ``AttributeError`` when it is absent — a ``*_extension`` function is not a
    substitute, however conventionally it is named.
    """
    module = _load(path)
    register = getattr(module, "register", None)
    assert register is not None, f"{path.name} has no module-level register(api)"
    assert callable(register), f"{path.name} register is not callable"

    required = [
        p
        for p in inspect.signature(register).parameters.values()
        if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert len(required) == 1, (
        f"{path.name} register{inspect.signature(register)} needs {len(required)} "
        "required arguments; the loader calls register(api)"
    )


@pytest.mark.parametrize("path", EXTENSION_PATHS, ids=EXTENSION_IDS)
def test_example_registers_without_raising(path: Path) -> None:
    """``register(api)`` completes against a real API.

    This is the check that would have caught 03 and 05: a tool dict missing
    ``execute`` raises here, at registration, exactly as it does in a real run.
    """
    module = _load(path)
    api, _registry, _handlers = _fresh_api(path)
    module.register(api)


@pytest.mark.parametrize("path", EXTENSION_PATHS, ids=EXTENSION_IDS)
def test_example_tool_schemas_are_enforceable(path: Path) -> None:
    """Every registered tool's ``parameters`` is a schema τ can validate against.

    ``register_tool`` itself only checks that ``parameters`` is a dict. That is
    enough to store, and not enough to enforce: ``validate_tool_arguments`` reads
    ``properties`` and ``required`` unconditionally, so a schema without
    ``properties`` validates every call vacuously and a ``required`` name absent
    from ``properties`` makes every call fail forever. ``_check_parameters_schema``
    is the same gate ``define_tool`` applies; examples are held to it because they
    are what people copy.
    """
    module = _load(path)
    api, registry, _handlers = _fresh_api(path)
    module.register(api)

    for name, definition in registry.get_active_tools().items():
        try:
            _check_parameters_schema(definition.parameters)
        except (TypeError, ValueError) as exc:
            pytest.fail(f"{path.name} tool {name!r} has an unenforceable schema: {exc}")


#: The loader snippet an example's docstring "## Usage" block tells the reader to
#: paste. Captured so the test can execute it rather than eyeball it.
_DOCSTRING_LOADER = re.compile(
    r"(_spec = importlib\.util\.spec_from_file_location\(.*?\)\n"
    r"ext = importlib\.util\.module_from_spec\(_spec\)\n"
    r".*?_spec\.loader\.exec_module\(ext\))",
    re.S,
)


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_docstring_usage_block_actually_runs(path: Path) -> None:
    """The "## Usage" block loads the file, and the names it then uses exist.

    Every one of these docstrings used to open with
    ``from examples.<stem> import <fn>``. That raises ``ModuleNotFoundError``
    verbatim: there is no ``examples/__init__.py``, and a module whose filename
    starts with a digit cannot be named in an import statement at all. Nineteen
    shipped examples carried the same dead line.

    The replacement is a real path load, and this test runs it — which is how the
    ``sys.modules`` registration got into the snippet. Without it, loading an
    example that combines ``@dataclass`` with ``from __future__ import
    annotations`` (``37_inline_bash.py``) dies inside ``dataclasses``, because
    annotation resolution looks the module up in ``sys.modules`` and does not
    find it. Reading the snippet does not tell you that; running it does.
    """
    doc = ast.get_docstring(ast.parse(path.read_text()))
    if not doc:
        pytest.skip(f"{path.name} has no module docstring")
    match = _DOCSTRING_LOADER.search(doc)
    if not match:
        pytest.skip(f"{path.name}'s docstring has no loader snippet")

    namespace: dict[str, Any] = {"importlib": importlib, "sys": sys}
    exec(match.group(1), namespace)  # noqa: S102 - executing the doc is the point
    loaded = namespace["ext"]

    # The block goes on to reference the loaded module — `ext.register`, or a
    # factory like `ext.make_budget_extension`. A snippet that loads and then
    # names something absent is still a snippet the reader cannot run.
    referenced = sorted(set(re.findall(r"\bext\.(\w+)", doc)))
    missing = [name for name in referenced if not hasattr(loaded, name)]
    assert not missing, (
        f"{path.name}'s Usage block loads the module then references "
        f"{missing}, which it does not define"
    )


@pytest.mark.parametrize("path", EXTENSION_PATHS, ids=EXTENSION_IDS)
def test_example_hook_handlers_take_event_and_ctx(path: Path) -> None:
    """Hook handlers match the runner's ``handler(event, ctx)`` dispatch.

    The check that would have caught 02. A one-argument handler registered on a
    hook raises ``TypeError`` the first time the hook fires — which for
    ``session_shutdown`` is at the very end of a session, and for ``tool_call`` is
    in the fail-CLOSED path, where a raising handler blocks the tool call.

    Notify handlers are deliberately NOT checked here: they take one argument, so
    the two contracts have to be told apart, and ``api.on`` already routes by
    event name. A handler on the wrong side of that split shows up as an arity
    mismatch on whichever side it landed.
    """
    module = _load(path)
    api, _registry, handlers = _fresh_api(path)
    module.register(api)

    for event, registered in handlers.handlers.items():
        if event not in _HOOK_EVENTS:
            continue
        for handler in registered:
            try:
                signature = inspect.signature(handler)
            except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
                continue
            if any(p.kind is p.VAR_POSITIONAL for p in signature.parameters.values()):
                continue
            positional = [
                p
                for p in signature.parameters.values()
                if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            # A bound method's `self` is already applied, so `inspect.signature`
            # reports the call signature — no adjustment needed here.
            assert len(positional) == 2, (
                f"{path.name} hook {event!r} handler "
                f"{getattr(handler, '__name__', handler)}{signature} takes "
                f"{len(positional)} required positional argument(s); the runner "
                "dispatches handler(event, ctx)"
            )
