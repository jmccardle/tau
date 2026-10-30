"""The `tau` command: `tau config check [--show MODEL]` and `tau trust [DIR]` (D13).

`main` takes its streams, environment, and working directory as arguments so a test
drives it with real files and real streams. Exit status: 0 success, 1 a problem
(printed as one message on stderr, never a traceback), 2 a usage error, 130 interrupted.
Everything printed from config data goes through `safe_text`.
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from tau_next._config import ConfigError
from tau_next._config_layers import ConfigProblem, LayerText, ModelCatalog, check_config
from tau_next._dotenv import DotenvError
from tau_next._startup import load_config, prompts, select_model
from tau_next._trust import (
    TAU_DIR,
    Trusted,
    TrustError,
    approve_project,
    ensure_trusted,
    find_project,
    is_trusted,
    project_config_path,
    project_environment,
    read_project_config,
    safe_block,
    safe_text,
    tau_home,
    trust_report,
    user_config_path,
)

_USAGE_ERROR = 2
_INTERRUPTED = 130
_UNTRUSTED = " [untrusted project: not used until `tau trust`]"


@dataclass(frozen=True)
class _Run:
    stdin: TextIO
    stdout: TextIO
    environ: Mapping[str, str]
    cwd: Path


def _parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    parser = argparse.ArgumentParser(prog="tau", description="τ next (Trusty).")
    sub = parser.add_subparsers(dest="command")
    config = sub.add_parser("config", help="inspect configuration")
    config_sub = config.add_subparsers(dest="config_command", metavar="{check}")
    check = config_sub.add_parser("check", help="report every problem in the config files")
    check.add_argument("--show", metavar="MODEL", help="print MODEL's resolved values and sources")
    trust = sub.add_parser("trust", help="approve a project directory's .tau/")
    trust.add_argument(
        "dir", nargs="?", type=Path, help="a directory in the project (default: the current)"
    )
    return parser, config


def _problem_line(problem: ConfigProblem, note: str) -> str:
    where = problem.source or "config"
    if problem.line is not None:
        where += f":{problem.line}:{problem.column}"
    at = f"{problem.path}: " if problem.path else ""
    return f"{where}: {problem.severity}: {at}{problem.message}{note}"


@dataclass
class _Findings:
    """What `config check` prints, and whether any of it stops startup."""

    lines: list[str] = field(default_factory=lambda: [])
    failed: bool = False

    def error(self, where: Path, message: str) -> None:
        self.lines.append(f"{where}: error: {message}")
        self.failed = True


def _project_findings(
    root: Path, home: Path, environ: Mapping[str, str], found: _Findings
) -> tuple[LayerText | None, Mapping[str, str], bool]:
    """The project's config layer, the environment to check keys against, and whether the
    project is trusted. Everything that would make startup refuse is added to `found`."""
    project = Trusted(root, None)
    layer: LayerText | None = None
    trusted = False
    try:
        layer = read_project_config(root)
    except TrustError as e:  # a symlinked or foreign file: startup refuses it too
        found.error(project_config_path(root), str(e))
    else:
        try:
            trusted = is_trusted(root, home)
        except TrustError as e:  # TAU_HOME or trusted.json writable by others
            found.error(home / "trusted.json", str(e))
        else:
            if trusted:
                project = ensure_trusted(root, home, ask=None)
                layer = project.layer()
            else:
                found.error(root / TAU_DIR, f"project is not trusted; run `tau trust {root}`")
    try:
        seen = project_environment(project, environ)
    except (TrustError, DotenvError) as e:
        found.error(root / ".env", str(e))
        seen = environ
    return layer, (seen if trusted else environ), trusted


def _check(run: _Run, show: str | None) -> int:
    home = tau_home(run.environ)
    root = find_project(run.cwd, run.environ)
    found = _Findings()
    layer: LayerText | None = None
    environ = run.environ
    trusted = True
    if root is not None:
        layer, environ, trusted = _project_findings(root, home, run.environ, found)
    problems = check_config(user_config_path(home), layer, environ=environ)
    for p in problems:
        untrusted = not trusted and layer is not None and p.source == layer.source
        found.lines.append(_problem_line(p, _UNTRUSTED if untrusted else ""))
    for line in found.lines:
        run.stdout.write(safe_text(line) + "\n")
    if found.failed or any(p.severity == "error" for p in problems):
        return 1
    if show is not None:
        loaded = load_config(run.cwd, run.environ, ask=None)
        name = select_model(loaded.catalog, show, pick=prompts(run.stdin, run.stdout).pick)
        _show(loaded.catalog, name, run.stdout)
    return 0


def _show(catalog: ModelCatalog, name: str, out: TextIO) -> None:
    config = catalog.resolve(name)
    values = {
        "model": config.model,
        "base_url": config.base_url,
        "provider": config.provider,
        "api_key_env": config.api_key_env,
        "api_key": "(set)" if config.literal_api_key is not None else None,
    }
    out.write(f"{safe_text(name)}\n")
    for origin in catalog.explain(name):
        group = f", group {origin.group}" if origin.group else ""
        value = values.get(origin.key)
        out.write(safe_text(f"  {origin.key:<12} {value}  [{origin.source}{group}]") + "\n")


def _refuse_special(path: Path, environ: Mapping[str, str]) -> None:
    """`path` is never trusted if it is `$HOME`, `TAU_HOME`, or the directory whose `.tau/`
    is `TAU_HOME` (the parent of a `TAU_HOME` named `.tau`)."""
    home = tau_home(environ).resolve()
    special = {
        "your home directory": Path(environ["HOME"]).resolve() if environ.get("HOME") else None,
        "TAU_HOME": home,
        "the parent of TAU_HOME": home.parent if home.name == TAU_DIR else None,
    }
    for what, found in special.items():
        if found is not None and path == found:
            raise TrustError(f"refusing to trust {path}: it is {what}")


def _trust(run: _Run, given: Path | None) -> int:
    home = tau_home(run.environ)
    start = (run.cwd / given).resolve() if given is not None else run.cwd.resolve()
    _refuse_special(start, run.environ)
    if not start.is_dir():
        raise TrustError(f"{start} is not a directory")
    root = find_project(start, run.environ)
    if root is None:
        raise TrustError(f"no project: no .tau/ found from {start}")
    _refuse_special(root, run.environ)
    if is_trusted(root, home):
        run.stdout.write(safe_block(f"already trusted: {root}") + "\n")
        return 0
    report = trust_report(root, home)
    shown = safe_block(f"{report}\ntrusted {root}") + "\n"  # all output made before the write
    try:
        approve_project(root, home)
    except TrustError as e:
        refusal = str(e)
        raise TrustError(refusal if refusal == report else f"{report}\n{refusal}") from e
    run.stdout.write(shown)
    return 0


def _fail(err: TextIO, message: str) -> int:
    err.write("tau: " + safe_block(message) + "\n")
    return 1


def main(  # noqa: PLR0913 - the process, injected so a test can supply real ones
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    """Run `tau` with `argv` (default `sys.argv[1:]`); the other arguments default to
    the process's streams, environment, and working directory. Returns the exit status."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    run = _Run(
        sys.stdin if stdin is None else stdin,
        out,
        os.environ if environ is None else environ,
        Path.cwd() if cwd is None else cwd,
    )
    parser, config = _parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if args.command is None:
        parser.print_help(err)
        return _USAGE_ERROR
    if args.command == "config" and args.config_command is None:
        config.print_help(err)
        return _USAGE_ERROR
    try:
        if args.command == "trust":
            return _trust(run, args.dir)
        return _check(run, args.show)
    except (ConfigError, TrustError, DotenvError) as e:
        return _fail(err, str(e))
    except KeyboardInterrupt:
        err.write("\ntau: interrupted\n")
        return _INTERRUPTED
