"""The authorization store: which discovered extensions the user agreed to load.

Rationale: docs/EXTENSION-PACKAGES.md §5 (one gate for packages and drop-ins)
and §9 (the store's location and format, a proposal).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tau_agent_core.extension_sources import ExtensionSource
from tau_llm.docs import agent_facing

STORE_FILENAME = "extension-authorizations.json"
"""The store's file name, beside the extensions directory it governs (§9, proposal)."""

STORE_FORMAT = 1
"""The ``format`` member a store this module writes carries; another value is refused."""

Verdict = Literal["authorized", "new", "update"]
"""``new``: never authorized. ``update``: the same distribution at another version or digest."""


@agent_facing(topic="extensions")
class AuthorizationStoreError(Exception):
    """The store file exists but cannot be read as a store; τ refuses to guess at it."""


@agent_facing(topic="extensions")
@dataclass(frozen=True)
class AuthorizationVerdict:
    """Whether a discovered source may load, and what kind of change it is if not.

    Attributes:
        verdict: ``authorized``, ``new`` or ``update``.
        previous_version: For an ``update``, the version that was authorized.
    """

    verdict: Verdict
    previous_version: str | None = None


def store_path_for(extensions_dir: str | Path) -> Path:
    """Where the store governing ``extensions_dir`` lives: beside it.

    Args:
        extensions_dir: The drop-in directory, ``<tau home>/extensions`` by default.

    Returns:
        ``<parent of extensions_dir>/extension-authorizations.json``.
    """
    return Path(extensions_dir).expanduser().parent / STORE_FILENAME


def _now() -> str:
    """An ISO-8601 UTC timestamp for a record's ``authorized_at``."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@agent_facing(topic="extensions")
class AuthorizationStore:
    """The user's authorization records, read from and written to one JSON file.

    Packages are keyed by identifier and match on distribution, version and
    digest. Drop-ins are keyed by resolved path and carry no version (§5).

    Attributes:
        path: The store file.
        existed: Whether the file was there when this store was loaded. A
            missing file is the first 0.14.0 start, when drop-ins are
            grandfathered (§5).
    """

    def __init__(self, path: Path, data: dict[str, Any], existed: bool) -> None:
        """Wrap parsed store data; use :meth:`load` rather than calling this."""
        self.path = path
        self.existed = existed
        self._packages: dict[str, dict[str, Any]] = data.get("packages", {})
        self._dropins: dict[str, dict[str, Any]] = data.get("dropins", {})

    @classmethod
    def load(cls, path: str | Path) -> AuthorizationStore:
        """Read the store at ``path``; a missing file is an empty store.

        Args:
            path: The store file.

        Returns:
            The store.

        Raises:
            AuthorizationStoreError: The file is not JSON, not an object, has
                another ``format``, or holds malformed sections.
        """
        store_path = Path(path)
        if not store_path.exists():
            return cls(store_path, {}, existed=False)
        try:
            data = json.loads(store_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthorizationStoreError(
                f"cannot read the extension authorization store {store_path}: {exc}. "
                "Fix or delete it; deleting it re-authorizes every drop-in once."
            ) from exc
        if not isinstance(data, dict) or data.get("format") != STORE_FORMAT:
            raise AuthorizationStoreError(
                f"{store_path} is not an extension authorization store of format "
                f"{STORE_FORMAT}; fix or delete it."
            )
        for section in ("packages", "dropins"):
            value = data.get(section, {})
            if not isinstance(value, dict) or not all(isinstance(v, dict) for v in value.values()):
                raise AuthorizationStoreError(
                    f"{store_path}: {section!r} must map names to objects; fix or delete it."
                )
        return cls(store_path, data, existed=True)

    def verdict(self, source: ExtensionSource) -> AuthorizationVerdict:
        """Whether ``source`` matches a record.

        Args:
            source: A discovered package or drop-in.

        Returns:
            ``authorized`` on a match. A package whose distribution was
            authorized at another version or digest is an ``update``;
            anything else is ``new``.

        Raises:
            ValueError: ``source`` is an explicit path, which needs no record.
        """
        if source.kind == "dropin":
            key = str(Path(source.target).resolve())
            return AuthorizationVerdict("authorized" if key in self._dropins else "new")
        if source.kind != "package":
            raise ValueError(f"{source.identifier}: a {source.kind} source is never looked up")
        record = self._packages.get(source.identifier)
        if record is None or record.get("distribution") != source.distribution:
            return AuthorizationVerdict("new")
        same = (
            record.get("version") == source.version
            and record.get("digest") == source.digest
            and record.get("editable_path") == source.editable_path
        )
        if same:
            return AuthorizationVerdict("authorized")
        return AuthorizationVerdict("update", previous_version=record.get("version"))

    def authorize(self, source: ExtensionSource, *, grandfathered: bool = False) -> None:
        """Record ``source`` as authorized, replacing any earlier record for it.

        Args:
            source: A discovered package or drop-in.
            grandfathered: Mark a drop-in record as written at upgrade (§5).

        Raises:
            ValueError: ``source`` is neither a package nor a drop-in.
        """
        if source.kind == "dropin":
            record: dict[str, Any] = {"identifier": source.identifier, "authorized_at": _now()}
            if grandfathered:
                record["grandfathered"] = True
            self._dropins[str(Path(source.target).resolve())] = record
            return
        if source.kind != "package":
            raise ValueError(f"{source.identifier}: a {source.kind} source is never recorded")
        self._packages[source.identifier] = {
            "distribution": source.distribution,
            "version": source.version,
            "digest": source.digest,
            "editable_path": source.editable_path,
            "authorized_at": _now(),
        }

    def revoke(self, identifier: str) -> int:
        """Remove every record naming ``identifier``.

        Args:
            identifier: An extension identifier.

        Returns:
            How many records were removed.
        """
        removed = 1 if self._packages.pop(identifier, None) is not None else 0
        for key in [k for k, v in self._dropins.items() if v.get("identifier") == identifier]:
            del self._dropins[key]
            removed += 1
        return removed

    def save(self) -> None:
        """Write the store atomically, creating its directory if needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"format": STORE_FORMAT, "packages": self._packages, "dropins": self._dropins}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ext-auth-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.existed = True

    def grandfather(self, dropins: list[ExtensionSource]) -> list[str]:
        """On the first start with no store, authorize every usable drop-in present and save.

        Args:
            dropins: The drop-ins discovery found.

        Returns:
            The identifiers grandfathered, for the one-time notice; empty when
            the store already existed, which is every start after the first.
        """
        if self.existed:
            return []
        granted = []
        for source in dropins:
            if source.kind == "dropin" and source.unusable is None:
                self.authorize(source, grandfathered=True)
                granted.append(source.identifier)
        self.save()
        return granted
