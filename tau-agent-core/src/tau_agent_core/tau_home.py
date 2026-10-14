"""Where τ keeps per-user state: ``~/.tau``, or a portable ``.tau/`` beside the install.

Rationale: docs/PORTABLE-HOME.md.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

PORTABLE_BASE_ENV = "TAU_PORTABLE_BASE"
"""Set by a bundle's launcher to the directory that may hold a portable ``.tau/``."""

HOME_DIRNAME = ".tau"


class TauHomeError(Exception):
    """``$TAU_PORTABLE_BASE`` names something that is not a directory."""


def resolve_tau_home(environ: Mapping[str, str] | None = None) -> Path:
    """Return τ's home directory: config, sessions, extensions, themes, logs.

    ``<$TAU_PORTABLE_BASE>/.tau`` when that directory exists, else ``~/.tau``.
    An unset or empty variable means "not a bundle". A bundle without the
    directory (a .deb, an AppImage with nothing beside it) is an ordinary
    install, which is the spec rather than a fallback.

    Args:
        environ: The environment to read; ``os.environ`` when omitted.

    Returns:
        The absolute home directory. It is not created here.

    Raises:
        TauHomeError: The variable is set to a path that is not a directory,
            meaning the launcher that set it is wrong.
    """
    env = os.environ if environ is None else environ
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
