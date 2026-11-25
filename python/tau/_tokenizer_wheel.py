"""Finding the tokenizer wheel (`ffwf-tau-tokens`, imported as `tau_tokens`; D26 section 7.2).

The `tokens` extension's option `tokenizers` says what to do with it:

- `auto` (the default): use it when it is installed. When it is installed but **broken** (it
  does not import, or cannot count) or **stale** (its version is not this τ's), it is not used,
  and the clients are warned once, with the command that repairs it.
- `off`: never import it.
- `require`: it must be installed, import, count, and be this τ's version; otherwise the start
  fails (`ExtensionLoadError`) with the command that installs it.

Discovery is `import tau_tokens`, a version check and one count of a fixed text, once, when the
extension is built. A `ModuleNotFoundError` whose `name` is `tau_tokens` means not installed;
any other failure means broken. Absent under `auto` is no failure: every count says how it was
made, so a weaker count never passes for an exact one.
"""

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Literal, cast, get_args

from tau._config import ConfigError

Mode = Literal["auto", "off", "require"]
MODES: tuple[str, ...] = get_args(Mode)
MODULE = "tau_tokens"
SMOKE = ("hello world", "o200k_base", 2)
"""A text, a tokenizer and its count: what a wheel that imports must also get right."""


@dataclass(frozen=True)
class Wheel:
    """What discovery found: `version` of a usable wheel (`None`: none is used), and `problem`,
    why an installed wheel is not used under `auto` (`None`: nothing to warn about)."""

    version: str | None = None
    problem: str | None = None


def install(version: str) -> str:
    """The command that installs the wheel of τ `version` (D26 decision 2)."""
    return f"pip install 'ffwf-tau[tokens]=={version}'"


def mode_of(extensions: Mapping[str, Mapping[str, object]]) -> Mode:
    """The `tokenizers` option the config layers set for `tokens` (default `auto`);
    `ConfigError` for any other value."""
    value = extensions.get("tokens", {}).get("tokenizers", "auto")
    if value not in MODES:
        raise ConfigError(
            f"extensions.tokens.tokenizers is {value!r}; it is one of {', '.join(MODES)}"
        )
    return cast("Mode", value)


def _problem(module: ModuleType, version: str) -> str | None:
    """Why an imported wheel cannot be used with τ `version`, or `None`."""
    found = getattr(module, "__version__", None)
    if found != version:
        return f"ffwf-tau-tokens {found} does not match ffwf-tau {version}: {install(version)}"
    text, name, expected = SMOKE
    try:
        counted = module.Tokenizer.tiktoken(name).count(text)
    except Exception as e:
        return f"ffwf-tau-tokens cannot count ({type(e).__name__}: {e}): {install(version)}"
    if counted != expected:
        return (
            f"ffwf-tau-tokens counts {text!r} as {counted} {name} tokens, not {expected}: "
            f"{install(version)}"
        )
    return None


def discover(
    mode: Mode,
    version: str,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> Wheel:
    """Find the wheel as `mode` says, for τ `version`. Under `require`, absent, broken or stale
    raises `WheelRequired` (an `ImportError`) naming the command that repairs it; under `auto`,
    broken or stale is a `Wheel` with the `problem` and no `version`."""
    if mode == "off":
        return Wheel()
    escape = "or set extensions.tokens.tokenizers=off"
    try:
        module = importer(MODULE)
    except ModuleNotFoundError as e:
        if e.name != MODULE:
            problem = f"ffwf-tau-tokens does not import ({e}): {install(version)}"
        elif mode == "require":
            raise WheelRequired(
                "extensions.tokens.tokenizers is require, and ffwf-tau-tokens is not installed: "
                f"{install(version)}"
            ) from None
        else:
            return Wheel()
    except Exception as e:
        problem = f"ffwf-tau-tokens does not import ({type(e).__name__}: {e}): {install(version)}"
    else:
        problem = _problem(module, version)
        if problem is None:
            return Wheel(version)
    if mode == "require":
        raise WheelRequired(f"extensions.tokens.tokenizers is require: {problem}")
    return Wheel(problem=f"{problem} ({escape})")


class WheelRequired(ImportError):
    """`extensions.tokens.tokenizers` is `require` and the tokenizer wheel cannot be used."""
