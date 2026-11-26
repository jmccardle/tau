"""`trusted.json` (D13): what was approved for each project, and how it is read and written;
moved out of `_trust.py`, which decides with it.

A project's record holds the sha256 of each pinned file or set of files (behind a marker byte,
so an absent file differs from an empty one), the hash of each file of the sets (for showing what
changed), and a redacted copy of the config.
"""

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tau._config import ConfigError
from tau._project_read import refusal, unless_io_fails

_PRIVATE = 0o700
_PRIVATE_FILE = 0o600
_SECRET = "(secret)"


def digest(raw: bytes | None) -> str:
    """sha256 of the bytes behind a marker byte, so a missing file differs from an empty one."""
    marker = b"\x00" if raw is None else b"\x01" + raw
    return hashlib.sha256(marker).hexdigest()


def file_hashes(files: Mapping[str, bytes]) -> dict[str, str]:
    """The sha256 of each file, by path, in path order."""
    return {path: hashlib.sha256(raw).hexdigest() for path, raw in sorted(files.items())}


def files_digest(files: Mapping[str, bytes] | None) -> str:
    """The digest of a set of files (path and sha256 of each); a missing directory has the
    marker a missing file has, so adding the first file changes it."""
    return digest(None if files is None else json.dumps(file_hashes(files)).encode())


@dataclass(frozen=True)
class Approval:
    """One project's record in `trusted.json`."""

    sha256: str
    env_sha256: str
    extensions_sha256: str
    system_sha256: str
    prompts_sha256: str
    mcp_sha256: str
    redacted: str
    extensions: Mapping[str, str]  # path under .tau/extensions/ -> sha256, for showing diffs
    prompts: Mapping[str, str]  # path under .tau/ -> sha256 of each prompt and skill file
    mcp: Mapping[str, str]  # path under the project (`.mcp.json`, `.tau/mcp.json`) -> sha256


def trusted_path(home: Path) -> Path:
    """`TAU_HOME/trusted.json`."""
    return home / "trusted.json"


def approved(home: Path) -> dict[str, Approval]:
    """Every project's record, by its resolved root."""
    path = trusted_path(home)
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise refusal(path, f"cannot be read ({e.strerror}); fix or delete it") from e
    except ValueError as e:
        raise ConfigError(f"{path} is not tau's trusted.json ({e!r}); fix or delete it") from e
    try:
        projects = cast("dict[str, dict[str, object]]", cast("dict[str, object]", data)["projects"])
        # A record from before `.env`, `.tau/extensions/` or `.tau/SYSTEM.md` was pinned lacks
        # its hash: not approved. One from before the prompts and skills, or the MCP files, were
        # pinned approved a project with none: it matches while the project still has none.
        pinned = ("env_sha256", "extensions_sha256", "system_sha256")
        current = {root: r for root, r in projects.items() if all(k in r for k in pinned)}
        keys = ("sha256", "config", *pinned)
        if not all(isinstance(r[k], str) for r in current.values() for k in keys):
            raise TypeError("a project's record is not text")
        return {
            root: Approval(
                str(r["sha256"]),
                str(r["env_sha256"]),
                str(r["extensions_sha256"]),
                str(r["system_sha256"]),
                str(r.get("prompts_sha256", files_digest({}))),
                str(r.get("mcp_sha256", files_digest({}))),
                str(r["config"]),
                dict(cast("dict[str, str]", r["extensions"])),
                dict(cast("dict[str, str]", r.get("prompts", {}))),
                dict(cast("dict[str, str]", r.get("mcp", {}))),
            )
            for root, r in current.items()
        }
    except (KeyError, TypeError, AttributeError) as e:
        raise ConfigError(f"{path} is not tau's trusted.json ({e!r}); fix or delete it") from e


def record(home: Path, root: Path, approval: Approval) -> None:
    """Write `approval` as the record of `root`, keeping every other project's."""
    projects = approved(home)
    projects[str(root.resolve())] = approval
    body = json.dumps(
        {
            "projects": {
                r: {
                    "sha256": a.sha256,
                    "env_sha256": a.env_sha256,
                    "extensions_sha256": a.extensions_sha256,
                    "system_sha256": a.system_sha256,
                    "prompts_sha256": a.prompts_sha256,
                    "mcp_sha256": a.mcp_sha256,
                    "config": a.redacted,
                    "extensions": a.extensions,
                    "prompts": a.prompts,
                    "mcp": a.mcp,
                }
                for r, a in projects.items()
            }
        },
        indent=2,
    )
    write_atomically(trusted_path(home), body)


def _blank(value: object) -> object:
    if isinstance(value, dict):
        d = cast("dict[str, object]", value)
        return {k: _SECRET if k == "api_key" else _blank(v) for k, v in d.items()}
    if isinstance(value, list):
        return [_blank(v) for v in cast("list[object]", value)]
    return value


def redacted(text: str | None) -> str:
    """The config as canonical JSON (sorted keys, 2-space indent) with every `api_key` value
    replaced; `""` for no config. The text must be valid JSON (approval ensures it)."""
    if text is None:
        return ""
    return json.dumps(_blank(json.loads(text)), sort_keys=True, indent=2)


def write_atomically(final: Path, body: str) -> None:
    """Write `body` to `final` (private to the user, its directory created private if absent)
    as a scratch file in the same directory, flushed to disk, then renamed over `final`: a
    reader sees the old file or the new, never half. `TrustError` if the disk refuses."""
    scratch = final.with_name(f"{final.name}.{os.getpid()}.tmp")
    with unless_io_fails(final, "written"):
        final.parent.mkdir(mode=_PRIVATE, parents=True, exist_ok=True)
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_FILE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(body)
                out.flush()
                os.fsync(out.fileno())
            scratch.replace(final)
        except BaseException:
            scratch.unlink(missing_ok=True)
            raise
