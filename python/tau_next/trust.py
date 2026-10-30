"""Finding the project and trusting it (D13).

A project is the nearest directory upward holding `.tau/`. Its config and
`.env` are read only while it is trusted: approved by the user, with the exact
text of `.tau/config.json` recorded in `$TAU_HOME/trusted.json`. A different
text is untrusted again, so a `git pull` cannot change a trusted config unseen.
"""

import difflib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO, cast

from tau_next.config import ConfigError, ModelConfig
from tau_next.config_layers import ModelCatalog, check_config, read_models
from tau_next.dotenv import read_dotenv

TAU_DIR = ".tau"
_GITIGNORE = "*\n"
_PRIVATE = 0o700
_SECRET = re.compile(r'("api_key"\s*:\s*)"(?:[^"\\]|\\.)*"')
_FIELDS = ("model", "base_url", "provider", "api_key_env", "api_key")


class TrustError(PermissionError):
    """A project directory tau cannot or will not trust, with the reason."""


class UntrustedProject(TrustError):
    """The project is not trusted (or its config changed). `report` says what it sets and
    what is wrong with `.tau/`; the message adds the command that trusts it."""

    def __init__(self, root: Path, report: str) -> None:
        super().__init__(f"{report}\nTrust it with: tau trust {root}")
        self.root = root
        self.report = report


def _home_dir(environ: Mapping[str, str]) -> Path:
    home = environ.get("HOME")
    if not home:
        raise ConfigError("HOME is not set")
    return Path(home).resolve()


def tau_home(environ: Mapping[str, str]) -> Path:
    """`$TAU_HOME`, or `~/.tau`; a leading `~` means `$HOME` from `environ`."""
    given = environ.get("TAU_HOME")
    if not given:
        return _home_dir(environ) / TAU_DIR
    if given == "~" or given.startswith("~/"):
        return _home_dir(environ) / given[2:]
    path = Path(given)
    if not path.is_absolute():
        raise ConfigError(f"TAU_HOME must be an absolute path, not {given!r}")
    return path


def find_project(start: Path, environ: Mapping[str, str]) -> Path | None:
    """The nearest directory from `start` upward that holds `.tau/`, or `None`.

    The search stops at `$HOME` (excluded: its `.tau` is the user config) and
    never returns the directory whose `.tau` is `TAU_HOME`. Outside `$HOME` it
    runs to `/`.
    """
    home = _home_dir(environ)
    config_dir = tau_home(environ).resolve()
    here = start.resolve()
    for directory in (here, *here.parents):
        if directory == home:
            return None
        tau = directory / TAU_DIR
        if tau.is_dir() and tau.resolve() != config_dir:
            return directory
    return None


def project_config_path(root: Path) -> Path:
    """`<root>/.tau/config.json`."""
    return root / TAU_DIR / "config.json"


def _config_text(root: Path) -> str:
    path = project_config_path(root)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _trusted_path(home: Path) -> Path:
    return home / "trusted.json"


def _approved(home: Path) -> dict[str, str]:
    path = _trusted_path(home)
    if not path.exists():
        return {}
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
        projects = cast("dict[str, dict[str, dict[str, object]]]", data)["projects"]
        found = {root: record["config"] for root, record in projects.items()}
        texts = [t for t in found.values() if isinstance(t, str)]
        if len(texts) != len(found):
            raise TypeError("a project's config is not text")
        return {root: str(text) for root, text in found.items()}
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ConfigError(f"{path} is not tau's trusted.json ({e!r}); fix or delete it") from e


def _record(home: Path, root: Path, text: str) -> None:
    projects = _approved(home)
    projects[str(root.resolve())] = text
    home.mkdir(mode=_PRIVATE, parents=True, exist_ok=True)
    body = json.dumps({"projects": {r: {"config": t} for r, t in projects.items()}}, indent=2)
    final = _trusted_path(home)
    scratch = final.with_name(f"trusted.json.{os.getpid()}.tmp")
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        scratch.replace(final)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise


def is_trusted(root: Path, home: Path) -> bool:
    """Whether `root` was approved with exactly its current `.tau/config.json`."""
    approved = _approved(home).get(str(root.resolve()))
    return approved is not None and approved == _config_text(root)


def _redact(text: str) -> list[str]:
    return _SECRET.sub(r'\1"(set)"', text).splitlines()


def _shown(config: ModelConfig, key: str) -> str:
    if key == "api_key":
        return "(set)" if config.literal_api_key is not None else "(unset)"
    value = cast("str | None", getattr(config, key))
    return "(unset)" if value is None else value


def _override_lines(before: ModelCatalog, after: ModelCatalog) -> list[str]:
    lines: list[str] = []
    for name, found in after.models.items():
        old = before.models.get(name)
        if old is None:
            lines.append(f"  new model {name}")
            lines += [f"    {k}: {_shown(found.config, k)}" for k in _FIELDS]
            continue
        changes = [
            f"    {k}: {_shown(old.config, k)} -> {_shown(found.config, k)}"
            for k in _FIELDS
            if _shown(old.config, k) != _shown(found.config, k)
        ]
        if changes:
            lines += [f"  {name}", *changes]
    lines += [f"  removed model {n}" for n in before.models if n not in after.models]
    if before.default_model != after.default_model:
        lines.append(f"  default_model: {before.default_model} -> {after.default_model}")
    return lines


def _overrides(root: Path, home: Path) -> list[str]:
    user = home / "config.json"
    user_file = user if user.exists() else None
    project = project_config_path(root)
    project_file = project if project.exists() else None
    if project_file is None:
        return ["  (no .tau/config.json)"]
    problems = [p for p in check_config(user_file, project_file) if p.severity == "error"]
    if problems:
        return ["  config errors:", *(f"    {p}" for p in problems)]
    lines = _override_lines(read_models(user_file), read_models(user_file, project_file))
    return lines or ["  (nothing: it sets no model that differs from your config)"]


def _checks(root: Path) -> list[str]:
    tau = root / TAU_DIR
    info = tau.stat()
    owned = info.st_uid == os.geteuid()
    mode = stat.S_IMODE(info.st_mode)
    ignored = (tau / ".gitignore").exists()
    return [
        f"  [{'ok' if owned else 'FAIL'}] {TAU_DIR}/ is owned by you"
        + ("" if owned else f" (uid {info.st_uid}; approval cannot fix this)"),
        f"  [{'ok' if mode == _PRIVATE else 'fix'}] {TAU_DIR}/ mode is {mode:o}"
        + ("" if mode == _PRIVATE else ", approval sets 700"),
        f"  [{'ok' if ignored else 'fix'}] {TAU_DIR}/.gitignore "
        + ("exists" if ignored else "is missing, approval writes it"),
    ]


def trust_report(root: Path, home: Path) -> str:
    """What approving `root` consents to, as text a user reads before agreeing.

    Lists what the project config overrides in the user config (old -> new,
    `api_key` shown only as `(set)`), the three `.tau/` checks, and for a
    project whose config changed since approval, a unified diff. Reads the user
    config from `home`.
    """
    text = _config_text(root)
    approved = _approved(home).get(str(root.resolve()))
    state = "is not trusted" if approved is None else "has changed since you approved it"
    lines = [f"Project {root} {state}.", "", "Its config overrides yours:", *_overrides(root, home)]
    lines += ["", "Checks:", *_checks(root)]
    if approved is not None:
        diff = difflib.unified_diff(
            _redact(approved),
            _redact(text),
            "approved .tau/config.json",
            "current .tau/config.json",
            lineterm="",
        )
        lines += ["", "Changes since approval:", *diff]
    return "\n".join(lines)


def approve_project(root: Path, home: Path) -> None:
    """Trust `root`: make `.tau/` mode 700, write `.tau/.gitignore` (`*`) if missing,
    and record the current `.tau/config.json`. Raises `TrustError` if `.tau/` is
    not owned by the current user; ownership is never changed."""
    tau = root / TAU_DIR
    if tau.stat().st_uid != os.geteuid():
        raise TrustError(f"{tau} is not owned by you; take it over (chown) before trusting it")
    tau.chmod(_PRIVATE)
    ignore = tau / ".gitignore"
    if not ignore.exists():
        ignore.write_text(_GITIGNORE, encoding="utf-8")
    _record(home, root, _config_text(root))


def ensure_trusted(root: Path, home: Path, *, ask: Callable[[str], bool] | None) -> None:
    """Return if `root` is trusted. Otherwise, with `ask` (a live terminal), show it the
    report and approve on yes; no `ask`, or a no, raises `UntrustedProject`."""
    if is_trusted(root, home):
        return
    report = trust_report(root, home)
    if ask is None or not ask(report):
        raise UntrustedProject(root, report)
    approve_project(root, home)


def is_interactive(stdin: TextIO, stdout: TextIO) -> bool:
    """Whether both streams are terminals, so a person can be asked."""
    return stdin.isatty() and stdout.isatty()


def terminal_ask(stdin: TextIO, stdout: TextIO) -> Callable[[str], bool]:
    """An `ask` that prints the report to `stdout` and reads `y`/`yes` (else no) from `stdin`."""

    def ask(report: str) -> bool:
        stdout.write(f"{report}\n\nTrust this project? [y/N] ")
        stdout.flush()
        return stdin.readline().strip().lower() in ("y", "yes")

    return ask


def create_project_dir(root: Path, home: Path) -> Path:
    """Create `<root>/.tau/` (mode 700, `.gitignore` of `*`) and trust it, since tau made it.
    Raises `FileExistsError` if `.tau/` exists."""
    tau = root / TAU_DIR
    tau.mkdir(mode=_PRIVATE)
    tau.chmod(_PRIVATE)
    (tau / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    _record(home, root, "")
    return tau


def project_environment(
    root: Path | None, home: Path, environ: Mapping[str, str]
) -> dict[str, str]:
    """`environ` over the project's `.env`: the `.env` is read only if `root` is trusted,
    and the shell environment wins."""
    found: dict[str, str] = {}
    if root is not None and (root / ".env").exists() and is_trusted(root, home):
        found = read_dotenv(root / ".env")
    return {**found, **environ}
