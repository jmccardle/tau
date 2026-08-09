"""tau_coding_agent.store_factory -- resolve ``--store`` / config ``session_store``
into a :class:`~tau_agent_core.session_catalog.SessionCatalog`.

The two injection points that build a catalog today (``headless.run_print`` and
``app.Parley.__init__``) both call :func:`build_session_catalog` instead of
hardcoding ``FileSessionCatalog()``. Everything else in ``tau-coding-agent``
stays exactly as ignorant of ``tau_jmfts`` as it was before this module existed:
``tau_jmfts`` is imported **lazily, inside a function body**, only when the
resolved backend is ``"jmfts"`` -- ``tau-coding-agent``'s ``pyproject.toml``
does not (and must not) declare a dependency on ``tau-jmfts`` (the monorepo
layering: the dependency arrow points ``tau-jmfts -> tau-agent-core``, never
``tau-coding-agent -> tau-jmfts``). A configured-but-absent backend is a hard,
loud failure -- never a silent fallback to the file store (Fail-Early).

Reference: docs/JMFTS-INTEGRATION-PLAN.md §3.1 (config/CLI shape, the health
check requirement) and §3 ("lazily resolve the backend by import; raise
'tau-jmfts not installed' if configured but absent").
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tau_agent_core.session_catalog import SessionCatalog
from tau_coding_agent.config import ConfigError
from tau_coding_agent.session_store import FileSessionCatalog

if TYPE_CHECKING:  # pragma: no cover - type-checking only, no runtime import
    from tau_jmfts.client import JmftsClient

__all__ = [
    "StoreError",
    "resolve_backend_name",
    "resolve_session_dir",
    "build_session_catalog",
    "build_jmfts_client",
    "resolve_host_parent_id",
]


class StoreError(ConfigError):
    """A misconfigured, unknown, or unreachable ``session_store`` backend.

    Subclasses :class:`~tau_coding_agent.config.ConfigError` so the single
    handler in ``cli.main()`` renders it as a clean ``tau: error: ...`` line
    (exit 2) instead of a raw traceback -- the same treatment every other
    config/CLI validation error already gets.
    """


def _session_store_config(config: dict[str, Any]) -> dict[str, Any]:
    """The ``session_store`` slice of ``~/.tau/config.json`` (§3.1), validated.

    Absent -> ``{}`` (every key then falls back to its own default). Present but
    not an object is a real config error, not something to coerce.
    """
    raw = config.get("session_store", {})
    if not isinstance(raw, dict):
        raise StoreError('~/.tau/config.json "session_store" must be a JSON object')
    return raw


def resolve_backend_name(config: dict[str, Any], store_flag: str | None) -> str:
    """The backend name: ``--store`` wins, else ``config.session_store.backend``,
    else ``"file"`` (§3.1's resolution order, spelled out explicitly here rather
    than folded into :func:`build_session_catalog` so the CLI/TUI wiring and
    tests can inspect the resolved name without constructing a real catalog)."""
    if store_flag is not None:
        return store_flag
    backend = _session_store_config(config).get("backend", "file")
    if not isinstance(backend, str):
        raise StoreError('~/.tau/config.json "session_store.backend" must be a string')
    return backend


def resolve_session_dir(session_dir: str | Path | None) -> Path | None:
    """Normalize a ``--session-dir`` value into the file store's ``base_dir``.

    ``None`` (the flag absent) stays ``None`` -- the file store's own
    ``~/.tau/sessions`` default. Anything else is ``expanduser``-ed, so a
    quoted ``--session-dir '~/.tau/sessions'`` the shell did not expand means
    what it says. An EMPTY value raises rather than degrading to the default:
    ``--session-dir ""`` is a caller mistake (usually an unset variable in a
    wrapper script), and quietly writing to the user's real session list is the
    single outcome this whole unit exists to prevent.

    Public because ``rpc_mode`` needs the resolved path itself -- it decides
    between this and :func:`~tau_coding_agent.session_store.rpc_default_session_base`
    before a catalog exists.
    """
    if session_dir is None:
        return None
    text = str(session_dir).strip()
    if not text:
        raise StoreError(
            "--session-dir needs a directory path, but the value given is empty; "
            "omit the flag to use the default session directory"
        )
    return Path(text).expanduser()


def build_jmfts_client(config: dict[str, Any], *, health_check: bool = True) -> "JmftsClient":
    """Build and health-check a ``JmftsClient`` from ``config["session_store"]``.

    Shared by :func:`build_session_catalog` (the ``"jmfts"`` backend) and the
    ``--import-session``/``--export-session`` CLI commands (``cli.py``), which
    need a raw client rather than a full catalog. Performs the §3.1 startup
    health check (``GET /``) HERE, once, at construction -- never deferred to
    the first append, and never caught/retried into a degraded mode (Fail-Early:
    a configured-but-unreachable store must fail loudly and immediately).

    Raises :class:`StoreError` (not a bare ``ImportError``/``JmftsError``) so
    every caller gets the same clean, actionable message whether ``tau-jmfts``
    is missing or the server is down.

    ``health_check=False`` skips the ``GET /`` and ONLY that -- every
    configuration error above it (package missing, no URL, non-string URL or
    token) still raises, because those are wrong regardless of whether a server
    is up. It exists for ``--no-session``, and it does not weaken the rule the
    paragraph above states; it corrects its SCOPE. "Never deferred to the first
    append" prices the check against a run that will append: reaching the
    model, burning tokens, and only then discovering the transcript had nowhere
    to go. A ``--no-session`` run has no first append. Tectum's prototyping
    measured what the unscoped check did instead -- ``tau -p --no-session``
    exited 2 at startup against an unreachable store it had already been told
    not to write to -- which is a run refused over a dependency it does not
    have.

    What the caller gives up is precise and stays loud: an ``--no-session`` run
    that later asks this store for something real (``--mode rpc`` keeps
    ``list_sessions``/``switch_session``/``fork``/``new_session {"persist":
    true}`` reachable over the wire) meets the failure at that request instead
    of at startup, as a ``JmftsError`` naming the call. Nothing is retried,
    degraded, or silently substituted -- the store is simply not consulted
    until something needs it.
    """
    try:
        from tau_jmfts.client import JmftsClient, JmftsError
    except ImportError as exc:
        raise StoreError(
            "session_store backend is 'jmfts' but the tau-jmfts package is not "
            "installed; run `pip install -e ./tau-jmfts` (see "
            "docs/JMFTS-INTEGRATION-PLAN.md §3)"
        ) from exc

    store_config = _session_store_config(config)
    url = store_config.get("url") or os.environ.get("JMFTS_API_URL")
    if not url:
        raise StoreError(
            "session_store backend is 'jmfts' but no URL is configured; set "
            '~/.tau/config.json "session_store.url" or the $JMFTS_API_URL '
            "environment variable"
        )
    if not isinstance(url, str):
        raise StoreError('~/.tau/config.json "session_store.url" must be a string')

    # CR-4: shared-bearer token. Read from config first, then the environment.
    # Fail-Early: we never default this to a value -- a missing token means the
    # request goes out unauthenticated and (against an auth'd server) 401s
    # loudly below, which is the correct signal, not something to paper over.
    token = store_config.get("token") or os.environ.get("JMFTS_API_TOKEN")
    if token is not None and not isinstance(token, str):
        raise StoreError('~/.tau/config.json "session_store.token" must be a string')

    client = JmftsClient(url, token=token)
    if not health_check:
        return client
    try:
        client.health()
    except JmftsError as exc:
        # Never leave the store configured-but-unreachable half-open: close the
        # httpx.Client this JmftsClient just opened before raising, rather than
        # leaking it into a caller that is about to give up on this catalog.
        client.close()
        if exc.status_code == 401:
            raise StoreError(
                f"JMFTS store at {url!r} rejected the request (401): it requires a "
                "token. Set session_store.token in ~/.tau/config.json or the "
                "$JMFTS_API_TOKEN environment variable."
            ) from exc
        raise StoreError(f"JMFTS store at {url!r} is unreachable: {exc}") from exc
    return client


def resolve_host_parent_id(config: dict[str, Any]) -> int | None:
    """The optional ``session_store.parent_id`` (§3.1): the JMFTS document that
    hosts every conversation root this run creates or forks. ``None`` (the
    default) plants roots at the top level, matching JMFTS's own root shape.

    Public (not a catalog-construction private) because ``cli.py``'s
    ``--import-session``/``--export-session`` commands need the same value to
    call ``tau_jmfts.importer.import_session`` without duplicating the
    validation/lookup.
    """
    store_config = _session_store_config(config)
    parent_id = store_config.get("parent_id")
    if parent_id is not None and not isinstance(parent_id, int):
        raise StoreError(
            '~/.tau/config.json "session_store.parent_id" must be an integer document id or null'
        )
    return parent_id


def build_session_catalog(
    config: dict[str, Any],
    store_flag: str | None = None,
    session_dir: str | Path | None = None,
    *,
    persist: bool = True,
) -> SessionCatalog:
    """Resolve ``--store``/config into the :class:`SessionCatalog` a run uses.

    Resolution order (§3.1): ``store_flag`` (the CLI ``--store`` override) wins;
    else ``config["session_store"]["backend"]``; else ``"file"``. An unknown
    backend name RAISES (:class:`StoreError`) -- it is never silently treated as
    ``"file"``, matching the "no silent fallback" rule that governs the whole
    JMFTS integration.

    ``session_dir`` is the ``--session-dir`` override (seam 1 of
    docs/SESSION-UX-REDESIGN.md, ``session_store.py``'s ``base_dir`` slot; pi
    ``args.ts:112``). ``None`` means the file store's own default,
    ``~/.tau/sessions``. It is a general flag -- the TUI, ``--print`` and
    ``--mode rpc`` all thread it -- but only the FILE store has a directory to
    override, so combining it with ``--store jmfts`` RAISES here (Fail-Early: a
    flag that names a filesystem path is not something a document-store backend
    may accept and ignore). The check runs before ``build_jmfts_client``, so the
    refusal costs no network call.

    ``persist=False`` is ``--no-session``: this run will only ever ask the
    returned catalog for ``create_ephemeral``, whose product is in-memory under
    BOTH stores (the file store's ``path``-less ``Session``, JMFTS's
    ``_EphemeralConversationSession``) and touches neither disk nor server. The
    catalog is still the one the run's ``--store``/config asked for -- no
    substitution, and the store is still resolved and validated -- it is simply
    not CONTACTED at startup (``build_jmfts_client(health_check=False)``, see
    there for the full argument and for what a later real request still costs).

    The file store needs nothing from this flag: ``FileSessionCatalog`` opens
    no connection, and its ``create_ephemeral`` writes no file. Only the
    document store had a startup dependency to drop.
    """
    base_dir = resolve_session_dir(session_dir)
    backend = resolve_backend_name(config, store_flag)
    if backend == "file":
        return FileSessionCatalog(base_dir)
    if backend == "jmfts":
        if base_dir is not None:
            raise StoreError(
                "--session-dir overrides the FILE store's on-disk session "
                "location, and the session store for this run is 'jmfts' "
                f"(a document server, which has no directory): {str(base_dir)!r} "
                "would be ignored. Drop --session-dir, or add --store file."
            )
        # build_jmfts_client() first: it is the one place the "tau-jmfts not
        # installed" ImportError is caught and converted to a clean StoreError.
        # Importing tau_jmfts.catalog before that would raise a bare, uncaught
        # ImportError on the same missing-package case.
        client = build_jmfts_client(config, health_check=persist)
        from tau_jmfts.catalog import JmftsSessionCatalog

        return JmftsSessionCatalog(client, host_parent_id=resolve_host_parent_id(config))
    raise StoreError(f"unknown session_store backend {backend!r}; expected 'file' or 'jmfts'")
