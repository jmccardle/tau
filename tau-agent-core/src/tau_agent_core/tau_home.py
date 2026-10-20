"""Where τ keeps per-user state: ``$TAU_HOME``, a portable ``.tau/``, or ``~/.tau``.

Rationale: docs/PORTABLE-HOME.md.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

HOME_ENV = "TAU_HOME"
"""Names τ's home directory outright; it must already exist."""

PORTABLE_BASE_ENV = "TAU_PORTABLE_BASE"
"""Set by a bundle's launcher to the directory that may hold a portable ``.tau/``."""

HOME_DIRNAME = ".tau"

HOME_ENV_VARS = (HOME_ENV, PORTABLE_BASE_ENV)
"""Every variable :func:`resolve_tau_home` reads besides ``$HOME``."""


class TauHomeError(Exception):
    """``$TAU_HOME`` or ``$TAU_PORTABLE_BASE`` names something that is not a directory."""


def pin_home_variables(environ: MutableMapping[str, str] | None = None) -> None:
    """Rewrite a relative :data:`HOME_ENV_VARS` value as an absolute path, in place.

    A child started from another directory (the bash tool, a delegate) then
    resolves the same home. Nothing is checked here, so this cannot fail; an
    unusable value still raises in :func:`resolve_tau_home`.

    Args:
        environ: The environment to rewrite; ``os.environ`` when omitted.
    """
    env = os.environ if environ is None else environ
    for name in HOME_ENV_VARS:
        value = env.get(name, "")
        if value and not os.path.isabs(os.path.expanduser(value)):
            env[name] = os.path.realpath(value)


def resolve_tau_home(environ: Mapping[str, str] | None = None) -> Path:
    """Return τ's home directory: config, sessions, extensions, themes, logs.

    First match wins; an empty variable counts as unset:

    1. ``$TAU_HOME``, which must be an existing directory.
    2. ``<$TAU_PORTABLE_BASE>/.tau`` when that directory exists. A base without
       it is an ordinary install (a .deb, a bare AppImage): the spec, not a
       fallback.
    3. ``~/.tau``.

    The project layer, ``<cwd>/.tau/``, is separate and not chosen here.

    Args:
        environ: The environment to read; ``os.environ`` when omitted.

    Returns:
        The absolute home directory. It is not created here.

    Raises:
        TauHomeError: ``$TAU_HOME`` is not a directory, or ``$TAU_PORTABLE_BASE``
            is set to a path that is not a directory.
    """
    env = os.environ if environ is None else environ
    explicit = env.get(HOME_ENV, "")
    if explicit:
        explicit_path = Path(explicit).expanduser()
        if not explicit_path.is_dir():
            raise TauHomeError(
                f"{HOME_ENV}={explicit} is not a directory; create it or unset the variable"
            )
        return explicit_path.resolve()
    base = env.get(PORTABLE_BASE_ENV, "")
    if base:
        base_path = Path(base)
        if not base_path.is_dir():
            raise TauHomeError(
                f"${PORTABLE_BASE_ENV}={base!r} is not a directory; the launcher that set it is broken"
            )
        portable = base_path / HOME_DIRNAME
        if portable.is_dir():
            return portable.resolve()
    return Path.home() / HOME_DIRNAME
